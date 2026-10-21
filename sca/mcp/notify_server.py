"""MCP 工具层（FR-11 / 通知）：消息通知 Server（钉钉群机器人 / 本机桌面通知）。

通知链路（``send_dispatch_notice``，按优先级自动降级）：
  1. 钉钉群机器人 webhook（配置 ``SCA_DINGTALK_WEBHOOK`` + ``SCA_DINGTALK_SECRET``
     后走真实 HTTP POST，markdown + HMAC-SHA256 加签）；
  2. **Windows 桌面弹窗**（本机免账号真实通知：PowerShell NotifyIcon 气泡，
     调度员坐在这台机器前即可收到；``SCA_NOTIFY_DESKTOP=false`` 可关）；
  3. 审计日志 ``data/notifications.jsonl``（任何情况都落盘，可追溯）。

  未配置钉钉且桌面弹窗不可用时，明确返回 sent=false 并注明只写了审计日志，
  绝不假装推送成功。

钉钉凭证获取（管理员/任意群成员均可，建议用测试群）：
  钉钉群 -> 群设置 -> 智能群助手 -> 添加机器人 -> 自定义 -> 安全设置选"加签"
  -> 复制 Webhook 与加签 Secret，写入项目 .env。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.notify")
mcp = FastMCP("notify-mcp") if HAVE_MCP else _StubMCP()

_AUDIT_FILE = Path(__file__).resolve().parents[2] / "data" / "notifications.jsonl"


def _config() -> tuple:
    """读取钉钉配置：优先取「工具 Key 清单」中当前可用的一条，回退环境变量。

    每次现读，支持清单/环境变量热更新后即时生效。
    """
    try:
        from sca.credentials import first_credential
        c = first_credential("dingtalk")
        if c:
            d = c.get("data") or {}
            wh, sec = (d.get("webhook") or "").strip(), (d.get("secret") or "").strip()
            if wh and sec:
                return wh, sec
    except Exception:  # noqa: 清单不可用 -> 走环境变量
        pass
    webhook = os.environ.get("SCA_DINGTALK_WEBHOOK", "").strip()
    secret = os.environ.get("SCA_DINGTALK_SECRET", "").strip()
    return webhook, secret


def _signed_url(webhook: str, secret: str) -> str:
    """钉钉加签：timestamp + "\\n" + secret 做 HMAC-SHA256 -> base64 -> urlencode。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    sign = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                 hashlib.sha256).digest()).decode("utf-8")
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def valid_phone(v) -> Optional[str]:
    """校验中国大陆手机号；非法/空返回 None（不编造、不猜测）。"""
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace("-", "")
    return s if _PHONE_RE.match(s) else None


def mask_phone(v) -> Optional[str]:
    """审计落盘用的脱敏手机号：138****1234（@ 推送仍用完整号，仅在群内与本机流转）。"""
    p = valid_phone(v)
    return (p[:3] + "****" + p[-4:]) if p else None


def _post_markdown(title: str, text: str, at_mobiles: Optional[list] = None) -> Dict:
    """阻塞式发送（在线程池里跑，避免阻塞事件循环）。

    at_mobiles：需要被 @ 的手机号列表。钉钉规则要求正文里必须出现 `@手机号`
    才能真正触发被 @ 提醒，故由调用方在 text 中追加（见 send_dispatch_notice）。
    """
    webhook, secret = _config()
    return _post_markdown_with(webhook, secret, title, text, at_mobiles)


def _post_markdown_with(webhook: str, secret: str, title: str, text: str,
                        at_mobiles: Optional[list] = None) -> Dict:
    """用**指定钉钉凭证**发送（多凭证降级时逐条调用）。

    干跑（``SCA_NOTIFY_DRY_RUN=true``）：本函数是全系统唯一的对外 HTTP 出口，
    闸口设在此处，任何调用路径（直连 send_dingtalk / 凭证链 run_chain / 单点回退）
    都绕不过去 —— 回归测试与演练不会把消息真的发进群里。
    """
    if _dry_run():
        return {"ok": True, "dry_run": True, "latency_ms": 0.0}
    url = _signed_url(webhook, secret)
    md = {"title": title, "text": text}
    body: Dict[str, Any] = {"msgtype": "markdown", "markdown": md}
    mobiles = [m for m in (at_mobiles or []) if valid_phone(m)]
    if mobiles:
        body["at"] = {"atMobiles": mobiles, "isAtAll": False}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=8) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        return {"ok": False, "error": f"钉钉返回 errcode={resp.get('errcode')} "
                                      f"{resp.get('errmsg')}"}
    return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1)}


@mcp.tool()
async def send_dingtalk(target: str, text: str, title: str = "供应链调度通知",
                        at_mobiles: Optional[list] = None) -> dict:
    """发送钉钉群通知（markdown）。未配置 webhook 时明确返回 not_configured。

    at_mobiles：要 @ 的手机号列表（被 @ 人须在该群内且手机号已绑定钉钉）。
    """
    webhook, secret = _config()
    mobiles = [m for m in (at_mobiles or []) if valid_phone(m)]
    if not webhook:
        return {"channel": "dingtalk", "target": target, "sent": False,
                "reason": "not_configured", "at_mobiles_masked":
                    [mask_phone(m) for m in mobiles],
                "hint": "在 .env 配置 SCA_DINGTALK_WEBHOOK 与 SCA_DINGTALK_SECRET 后即可真实推送"}
    masked = [mask_phone(m) for m in mobiles]
    attempts: list = []
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            d = cred.get("data") or {}
            wh, sec = (d.get("webhook") or "").strip(), (d.get("secret") or "").strip()
            if not (wh and sec):
                return None
            r = await asyncio.to_thread(_post_markdown_with, wh, sec,
                                        title or "供应链调度通知", text, mobiles)
            if not r.get("ok"):
                raise RuntimeError(r.get("error") or "钉钉返回失败")
            return r

        r, used, attempts = await run_chain(KIND_TOOL, "dingtalk", _one)
    except Exception as exc:  # noqa: 凭证链不可用 -> 单点回退
        logger.warning("[notify] 钉钉凭证链不可用（%s），回退单凭证", exc)
        try:
            r = await asyncio.to_thread(_post_markdown,
                                        title or "供应链调度通知", text, mobiles)
            used = None
        except Exception as exc2:
            r, used = None, None
            attempts = attempts or [{"ok": False, "error": str(exc2)[:200]}]

    if r and r.get("ok"):
        logger.info("[notify] 钉钉真实推送成功 -> %s（%sms，@%d 人，凭证 %s）",
                    target, r.get("latency_ms"), len(mobiles),
                    (used or {}).get("name", "默认"))
        return {"channel": "dingtalk", "target": target, "sent": True,
                "latency_ms": r.get("latency_ms"), "title": title,
                "credential": (used or {}).get("name", ""),
                "at_mobiles_masked": masked}
    # 全部凭证失败：如实返回，不假装成功
    err = next((a.get("error") for a in (attempts or []) if a.get("error")),
               "未知错误")
    logger.warning("[notify] 钉钉全部凭证推送失败：%s", err)
    return {"channel": "dingtalk", "target": target, "sent": False,
            "reason": "all_credentials_failed", "error": err,
            "attempts": attempts, "at_mobiles_masked": masked}


def _audit(entry: Dict) -> None:
    """审计日志：每条通知（无论成败）落盘 data/notifications.jsonl。"""
    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), **entry},
                               ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[notify] 审计日志写入失败：%s", exc)


# ---------------- Windows 桌面弹窗（免账号真实通知） ----------------

def _desktop_enabled() -> bool:
    if _dry_run():   # 干跑：连本机弹窗也一并关掉，避免测试期间打扰调度员
        return False
    return os.environ.get("SCA_NOTIFY_DESKTOP", "true").strip().lower() \
        not in ("0", "false", "off", "no")


def _dry_run() -> bool:
    """干跑开关（``SCA_NOTIFY_DRY_RUN``）：只构造与审计，不发任何真实请求。

    为什么需要：钉钉推送是本系统唯一的对外副作用，且发出后不可撤回。
    回归测试 / 演练 / 演示若直连真实群，会造成消息污染（实测已发生）。
    开关放在唯一网络出口，保证"忘了打桩"也不会误发。
    """
    return os.environ.get("SCA_NOTIFY_DRY_RUN", "").strip().lower() \
        in ("1", "true", "on", "yes")


def _plain(text: str, limit: int = 220) -> str:
    """markdown -> 纯文本，气泡正文截断（NotifyIcon 上限约 255 字符）。"""
    t = re.sub(r"\*\*|__|`", "", text or "")
    t = re.sub(r"\s*\n\s*", "；", t).strip()
    return t[:limit]


def _ps_toast(title: str, text: str) -> Tuple[bool, Optional[str]]:
    """PowerShell NotifyIcon 气泡通知。返回 (是否成功, 错误信息)。

    用 -EncodedCommand（UTF-16LE base64）传脚本，避免命令行中文编码问题。
    """
    def ps_str(s: str) -> str:  # PowerShell 单引号字符串转义
        return "'" + (s or "").replace("'", "''") + "'"

    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        f"$n.BalloonTipTitle = {ps_str(title[:60])};"
        f"$n.BalloonTipText = {ps_str(text)};"
        "$n.ShowBalloonTip(10000);"
        "Start-Sleep -Seconds 4;"
        "$n.Dispose()"
    )
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, timeout=20)
        if r.returncode == 0:
            return True, None
        return False, (r.stderr or b"").decode("utf-8", "ignore")[:200] \
               or f"powershell exit={r.returncode}"
    except Exception as exc:
        return False, str(exc)


@mcp.tool()
def _with_at(text: str, mobiles: list, role: str) -> str:
    """钉钉规则：被 @ 的手机号必须出现在正文中，才会触发被 @ 提醒。"""
    if not mobiles:
        return text
    at_line = " ".join(f"@{m}" for m in mobiles)
    return f"{text}\n\n> {role}请查收：{at_line}"


@mcp.tool()
async def send_dispatch_notice(target: str, text: str,
                               title: str = "供应链调度通知",
                               at_mobiles: Optional[list] = None) -> dict:
    """统一调度通知：钉钉 webhook（已配置时）-> Windows 桌面弹窗（免账号）-> 审计日志。

    at_mobiles：该 target 对应人员的手机号（司机 / 收货方），用于钉钉群内 @ 到本人；
    桌面弹窗是本机真实通知（调度员屏幕右下角弹出）；审计日志始终落盘且手机号脱敏。
    """
    mobiles = [m for m in (at_mobiles or []) if valid_phone(m)]
    masked = [mask_phone(m) for m in mobiles]
    webhook, _ = _config()
    if webhook:
        r = await send_dingtalk(target, _with_at(text, mobiles, target), title,
                                at_mobiles=mobiles)
        r["at_mobiles_masked"] = masked
        _audit({"channel": "dingtalk", "target": target, "title": title,
                "sent": r.get("sent"), "at_mobiles_masked": masked,
                **({"dry_run": True} if _dry_run() else {}),
                "detail": {k: r.get(k) for k in
                ("reason", "error", "latency_ms") if r.get(k) is not None}})
        return r
    # 免账号通道：Windows 桌面弹窗
    toast_ok, toast_err = False, None
    if _desktop_enabled() and sys.platform.startswith("win"):
        toast_ok, toast_err = await asyncio.to_thread(
            _ps_toast, title or "供应链调度通知", _plain(text))
    if toast_ok:
        logger.info("[notify] 桌面弹窗已弹出 -> %s", target)
        _audit({"channel": "desktop_toast", "target": target, "title": title,
                "sent": True, "text": _plain(text), "at_mobiles_masked": masked})
        return {"channel": "desktop_toast", "target": target, "sent": True,
                "title": title, "audit": str(_AUDIT_FILE),
                "at_mobiles_masked": masked,
                "note": "本机桌面弹窗（免账号真实通知）；配置钉钉 webhook 后改推群并 @ 到人"}
    _audit({"channel": "audit_only", "target": target, "title": title,
            "sent": False, "text": _plain(text),
            "reason": toast_err or "desktop_unavailable"})
    return {"channel": "audit_only", "target": target, "sent": False,
            "reason": "desktop_unavailable", "error": toast_err,
            "audit": str(_AUDIT_FILE),
            "hint": "仅写入审计日志；配置 SCA_DINGTALK_WEBHOOK 或在本机 Windows "
                    "上运行即可收到真实通知"}


@mcp.tool()
async def send_email(target: str, subject: str, body: str) -> dict:
    """发送邮件通知（未接 SMTP 前明确返回 not_configured，不假装成功）。"""
    return {"channel": "email", "target": target, "sent": False,
            "reason": "not_configured",
            "hint": "接入 SMTP（SCA_SMTP_HOST 等）后启用"}


@mcp.tool()
async def notify_config_status() -> dict:
    """通知渠道配置状态（前端/看板用）。"""
    webhook, secret = _config()
    return {"dingtalk": {"configured": bool(webhook), "signed": bool(secret)},
            "desktop_toast": {"enabled": _desktop_enabled(),
                              "available": sys.platform.startswith("win")},
            "audit_log": str(_AUDIT_FILE),
            "active_channel": ("dingtalk" if webhook else
                               "desktop_toast" if (_desktop_enabled()
                               and sys.platform.startswith("win")) else "audit_only")}


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
