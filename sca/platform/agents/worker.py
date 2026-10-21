"""单个 Agent 进程的入口。

::

    python -m sca.platform.agents.worker --kind dispatch
    python -m sca.platform.agents.worker --kind carrier --once
    python -m sca.platform.agents.worker --kind exception --poll-ms 200

为什么用 ``python -m`` 而不是给四个 agent 各写一个 ``if __name__`` 脚本：
``-m`` 的 ``sys.path[0]`` 是当前目录，从哪一层启动都能找到 ``sca`` 包；
四个独立脚本指向同一个 main 也只是复制粘贴。

日志
----
每个进程写自己的 ``logs/agent_{kind}.log``，同时打到控制台。
**不要合并成一个文件**：验收时要看的是"杀掉 dispatch 之后 carrier 的日志还在长"，
合并成一个文件这件事就看不出来了。进程被杀时日志立刻断，是故障域独立的直接证据。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"


def setup_logging(kind: str, level: str = "INFO", echo: bool = True) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"agent_{kind}.log"
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(process)d] %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if echo:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    # SQLAlchemy 的 DEBUG 会把每条 SQL 都打出来，日志会失去可读性
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    return path


def main(argv: list[str] | None = None) -> int:
    from .daemons import KINDS, build

    ap = argparse.ArgumentParser(prog="sca-agent", description="启动单个 Agent 常驻进程")
    ap.add_argument("--kind", required=False, choices=KINDS,
                    help="哪个 agent：" + "/".join(KINDS))
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮就退出（测试/巡检用）")
    ap.add_argument("--poll-ms", type=int, default=None,
                    help="没活时最多等多久（覆盖默认值）")
    ap.add_argument("--lease-s", type=int, default=None, help="消息租约秒数")
    ap.add_argument("--due-s", type=float, default=None, help="到期检查间隔秒数")
    ap.add_argument("--org", default="",
                    help="只服务这些承运商组织（逗号分隔），其余收件箱不动")
    ap.add_argument("--log-level", default=os.environ.get("SCA_AGENT_LOG", "INFO"))
    ap.add_argument("--no-echo", action="store_true", help="只写文件，不打控制台")
    ap.add_argument("--list", action="store_true", help="列出可启动的 agent")
    args = ap.parse_args(argv)

    if args.list or not args.kind:
        print("可启动的 agent：" + ", ".join(KINDS))
        return 0

    path = setup_logging(args.kind, args.log_level, echo=not args.no_echo)
    logging.getLogger("sca.platform.agents.worker").info(
        "日志写入 %s", path)

    only = []
    for x in str(args.org or "").split(","):
        x = x.strip()
        if not x:
            continue
        # 允许写 9912 或 carrier:9912 两种形式
        only.append(x if ":" in x else f"carrier:{x}")

    d = build(args.kind, once=args.once, only_keys=only)
    if args.poll_ms:
        d.poll_ms = int(args.poll_ms)
    if args.lease_s:
        d.lease_s = int(args.lease_s)
    if args.due_s:
        d.due_interval_s = float(args.due_s)
    return d.run()


if __name__ == "__main__":
    raise SystemExit(main())
