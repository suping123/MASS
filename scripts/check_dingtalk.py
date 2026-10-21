"""钉钉群机器人自检：确认"能否在这个群里通过机器人发出消息"。

用法：
    python scripts/check_dingtalk.py                # 发一条测试消息到群
    python scripts/check_dingtalk.py 13800138000    # 额外验证 @ 到本人

行为：
  - 未配置 webhook：打印获取指引并退出（退出码 2），**不做任何网络请求、不假装成功**；
  - 已配置：真实 POST 到钉钉，打印 errcode/耗时；被 @ 的手机号会按钉钉规则写进正文。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sca.settings import settings  # noqa: E402  导入即加载 .env
from sca.mcp import notify_server as ns  # noqa: E402


def mask(s: str) -> str:
    return (s[:7] + "…" + s[-4:]) if len(s) > 12 else ("已配置" if s else "（空）")


async def main() -> int:
    phone = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    webhook, secret = ns._config()
    print("== 钉钉群机器人配置状态 ==")
    print("  webhook :", mask(webhook))
    print("  secret  :", mask(secret))
    print("  桌面弹窗:", "开" if ns._desktop_enabled() else "关")
    print("  审计日志:", ns._AUDIT_FILE)

    if not webhook:
        print("\n[未配置] 还缺 Webhook，无法发送。获取方式：")
        print("  钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义（Webhook）")
        print("  → 安全设置选「加签」→ 复制 Webhook 地址")
        print("  → 写入 .env：SCA_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxxx")
        return 2
    if not secret:
        print("\n[警告] 已配 webhook 但无加签 Secret：若机器人安全设置选的是「自定义关键词」，")
        print("       本系统走的是加签通道，会验签失败（errcode=310000）。请把机器人改为「加签」并填 Secret。")

    at = [phone] if ns.valid_phone(phone) else []
    if phone and not at:
        print(f"\n[警告] {phone} 不是合法手机号，本次不 @ 任何人。")

    title = "供应链调度系统 · 连通性自检"
    text = ("- **来源**：supply-chain-agent check_dingtalk 自检\n"
            "- **目的**：确认本群机器人可正常接收调度通知\n"
            f"- **@ 验证**：{'是（应为被 @ 人收到提醒）' if at else '否（未给手机号）'}")
    print("\n== 发送中 ==")
    r = await ns.send_dispatch_notice(target="调度群", text=text, title=title,
                                      at_mobiles=at)
    print("  通道   :", r.get("channel"))
    print("  是否送达:", "是" if r.get("sent") else "否")
    if r.get("sent"):
        print("  耗时   :", r.get("latency_ms"), "ms")
        print("  @ 号码 :", r.get("at_mobiles_masked") or "无")
        print("\n[OK] 群里应已收到这条消息；若配置了 @ 且号码在群内，被 @ 人会收到提醒。")
        return 0
    print("  原因   :", r.get("reason") or r.get("error"))
    if r.get("reason") == "api_error":
        print("\n[FAIL] 钉钉拒绝：多为加签 Secret 与机器人不匹配，或安全设置不是「加签」。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
