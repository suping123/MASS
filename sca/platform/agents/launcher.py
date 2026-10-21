"""四个 Agent 进程的启动器（不参与业务）。

它只做三件事：起进程、看着它们、把死掉的那个报出来。

**刻意不做自动重启**（`--restart` 才开）：一个 agent 被杀掉又悄悄被拉起来，
"故障域独立"这件事就永远演示不出来，出事时也没人知道曾经断过。
默认行为是——死了就报，其余三个继续跑。

::

    python -m sca.platform.agents.launcher            # 起四个
    python -m sca.platform.agents.launcher --status    # 看心跳
    python -m sca.platform.agents.launcher --stop      # 全停（含孤儿进程）
    python -m sca.platform.agents.launcher --only dispatch,carrier
    python -m sca.platform.agents.launcher --restart   # 挂了自动拉起
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"
KINDS = ("dispatch", "carrier", "exception", "shipper")


def _python() -> str:
    """用当前解释器启动子进程——虚拟环境里的 python 才是装了依赖的那个。"""
    return sys.executable or "python"


def _cmd(kind: str, extra: List[str]) -> List[str]:
    return [_python(), "-m", "sca.platform.agents.worker", "--kind", kind, *extra]


def _pidfile(kind: str) -> Path:
    return LOG_DIR / f"agent_{kind}.pid"


def stop_all() -> int:
    """停掉所有在跑的 agent。

    不用 pid 文件当唯一依据：Windows 上 launcher 被强杀时子进程会变成孤儿，
    pid 文件就可能是上一轮的。**心跳表才是真相**——谁最近还写过心跳，
    谁就是真的在跑；pid 文件只作为补充。
    """
    from sqlalchemy import select, update

    from sca.platform.agents import bus
    from sca.platform.db import session_as
    from sca.platform.models import AgentActor

    pids: dict[int, str] = {}
    db = session_as()
    try:
        for a in db.execute(select(AgentActor).where(
                AgentActor.pid.isnot(None))).scalars().all():
            age = bus.heartbeat_age(db, a.agent_key)
            if age is not None and age <= 3600:
                pids[int(a.pid)] = a.agent_key
    except Exception:                                  # noqa: BLE001
        pass
    finally:
        db.close()

    for f in LOG_DIR.glob("agent_*.pid"):
        try:
            pid = int((f.read_text(encoding="utf-8") or "0").strip() or 0)
        except (OSError, ValueError):
            continue
        if pid and pid not in pids:
            pids[pid] = f.stem
        try:
            f.unlink()
        except OSError:
            pass

    pids.pop(os.getpid(), None)
    if not pids:
        print("[launcher] 没有正在运行的 agent")
        return 0

    for pid, label in pids.items():
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        # taskkill 的 128 = 进程不存在。它跟"杀不掉"是两件事：
        # 前者说明心跳/pid 文件过期了，后者才是真问题。
        if r.returncode == 0:
            print(f"[launcher] 已停止 {label} pid={pid}")
        elif r.returncode == 128:
            print(f"[launcher] 已不在运行 {label} pid={pid}（清理记录）")
        else:
            msg = (r.stdout or b"").decode("gbk", "ignore").strip()[:70]
            print(f"[launcher] 停止失败 {label} pid={pid}  {msg}")

    # 心跳清零：不这样做，页面上还会显示"在跑"最多 90 秒
    db = session_as()
    try:
        db.execute(update(AgentActor).where(
            AgentActor.pid.isnot(None)).values(pid=None, last_seen_at=None))
        db.commit()
    except Exception:                                  # noqa: BLE001
        db.rollback()
    finally:
        db.close()
    return 0


def _print_status() -> int:
    from sqlalchemy import select

    from sca.platform.agents import bus
    from sca.platform.db import session_as
    from sca.platform.models import AgentActor

    db = session_as()
    try:
        rows = db.execute(select(AgentActor).order_by(AgentActor.kind,
                                                      AgentActor.agent_key)).scalars().all()
        if not rows:
            print("还没有任何 agent 注册过——先跑一次 start_agents.bat")
            return 1
        print(f"{'agent_key':<18}{'kind':<11}{'pid':<8}{'心跳':<10}状态")
        for a in rows:
            age = bus.heartbeat_age(db, a.agent_key)
            alive = age is not None and age <= 90
            beat = "从未" if age is None else f"{age:.0f}s 前"
            pid_cell = str(a.pid) if (alive and a.pid) else "-"
            print(f"{a.agent_key:<18}{a.kind:<11}{pid_cell:<8}{beat:<10}"
                  f"{'在跑' if alive else '未运行'}")
        return 0
    finally:
        db.close()


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sca-launcher", description="四个 Agent 常驻进程管理")
    ap.add_argument("--only", default="", help="只起这几个，逗号分隔")
    ap.add_argument("--status", action="store_true", help="打印各 agent 心跳后退出")
    ap.add_argument("--stop", action="store_true", help="停掉所有在跑的 agent 后退出")
    ap.add_argument("--restart", action="store_true", help="子进程退出后自动重启")
    ap.add_argument("--poll-ms", type=int, default=0)
    ap.add_argument("--due-s", type=float, default=0.0)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    if args.status:
        return _print_status()
    if args.stop:
        return stop_all()

    kinds = [k.strip() for k in args.only.split(",") if k.strip()] or list(KINDS)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        print(f"未知 agent：{', '.join(bad)}（可用：{', '.join(KINDS)}）")
        return 2

    LOG_DIR.mkdir(exist_ok=True)
    extra: List[str] = []
    if args.poll_ms:
        extra += ["--poll-ms", str(args.poll_ms)]
    if args.due_s:
        extra += ["--due-s", str(args.due_s)]
    if args.log_level:
        extra += ["--log-level", args.log_level]

    procs: Dict[str, subprocess.Popen] = {}
    stopping = False

    def _stop(*_a):
        nonlocal stopping
        stopping = True
        print("\n[launcher] 收到停止信号，正在停止所有 agent …")
        for k, p in procs.items():
            if p.poll() is None:
                p.terminate()
        for k, p in procs.items():
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                print(f"[launcher] {k} 没在 8 秒内退出，强制结束")
                p.kill()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    for k in kinds:
        p = subprocess.Popen(_cmd(k, extra), cwd=str(ROOT))
        procs[k] = p
        _pidfile(k).write_text(str(p.pid), encoding="utf-8")
        print(f"[launcher] 启动 {k:<10} pid={p.pid}  日志 logs/agent_{k}.log")

    print(f"[launcher] {len(procs)} 个 agent 已在运行。Ctrl+C 停止。"
          f"（不自动重启；要自动拉起加 --restart）")

    try:
        while procs and not stopping:
            time.sleep(1.0)
            for k, p in list(procs.items()):
                code = p.poll()
                if code is None:
                    continue
                print(f"[launcher] ⚠ {k} 已退出（exit={code}）。"
                      f"其余 {len(procs) - 1} 个继续运行，详见 logs/agent_{k}.log")
                if args.restart and not stopping:
                    time.sleep(3.0)
                    np = subprocess.Popen(_cmd(k, extra), cwd=str(ROOT))
                    procs[k] = np
                    _pidfile(k).write_text(str(np.pid), encoding="utf-8")
                    print(f"[launcher] 已重启 {k} pid={np.pid}")
                else:
                    procs.pop(k, None)
    except KeyboardInterrupt:
        _stop()

    if not stopping:
        print("[launcher] 所有 agent 都已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
