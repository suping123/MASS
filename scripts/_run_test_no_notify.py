"""在不真实推送钉钉的前提下运行项目内测试脚本。

为什么需要：notify_server._config() 优先读 data/credentials.json 的「工具 Key 清单」，
优先级高于环境变量，所以仅设 SCA_DINGTALK_WEBHOOK 空挡不住真实推送。任何会走到
notify 节点的测试都必须先在此拦截。

用法：
  .venv/Scripts/python.exe -u scripts/_run_test_no_notify.py scripts/test_dispatch_review.py
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: _run_test_no_notify.py <测试脚本路径>")
        return 2

    os.environ["SCA_NOTIFY_DESKTOP"] = "false"   # 也不弹本机桌面窗口
    # 干跑：堵住唯一对外 HTTP 出口。仅改 notify_server._config 是不够的 ——
    # send_dingtalk 会先走 sca.credentials.run_chain 用真实凭证直连 webhook，
    # 该路径绕过 _config，实测把测试消息真发进了群（2026-10-16）。
    os.environ["SCA_NOTIFY_DRY_RUN"] = "true"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import sca.mcp.notify_server as ns
    ns._config = lambda: ("", "")   # 无凭据 -> send_dispatch_notice 走审计通道

    target = Path(sys.argv[1])
    if not target.is_file():
        target = ROOT / sys.argv[1]
    print(f"[runner] 已阻断外部通知，运行 {target.name}\n")
    sys.argv = [str(target)] + sys.argv[2:]
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
