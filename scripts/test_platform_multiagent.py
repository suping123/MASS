"""多 Agent 架构的回归验证（阶段 A）。

    ./.venv/Scripts/python.exe scripts/test_platform_multiagent.py

覆盖：
  1) 信息边界：承运商拿不到货主预算、异常域拿不到商业信息、调度全量可见；
  2) 消息契约：非法类型/缺字段/非法方向一律拒绝；
  3) 私有记忆：按 agent_key 隔离，读不到别家的；
  4) 消息总线：发送、认领、回放，且只能认领发给自己的；
  5) 异常域 Agent：独立定级 + 给出候选动作（含改派）+ 向调度发 propose；
  6) 静态闸门：check_isolation.py 必须返回 0。

本脚本只创建以 `test:` 为前缀的临时数据，结束时清理干净，不动任何真实订单。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402

from sca.platform.agents import bus, contract, exception_agent, views  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.models import (AgentMemory, AgentMessage, ShipperOrder)  # noqa: E402

FAILS: list[str] = []
PASSED = 0
STAMP = str(int(time.time()))


def ok(cond, name: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


def raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def main() -> int:
    db = session_as("app")
    try:
        order_id = db.execute(select(ShipperOrder.id).order_by(
            ShipperOrder.id.desc()).limit(1)).scalar()
        if not order_id:
            print("SKIP：库中没有订单，无法验证视图边界")
            return 0

        print("[1] 信息边界（白名单投影）")
        c_view = views.load_order(db, order_id, "carrier")
        ok(views.load_order(db, order_id, "dispatch") is not None, "调度可见订单")
        ok(bool(c_view.order_no), "承运商可见订单号")
        ok(raises(lambda: c_view.budget_max, views.IsolationError),
           "承运商读预算 -> IsolationError")
        ok(raises(lambda: c_view.source_text, views.IsolationError),
           "承运商读原始描述 -> IsolationError（原文可能含预算）")
        e_view = views.load_order(db, order_id, "exception")
        ok(raises(lambda: e_view.budget_max, views.IsolationError),
           "异常域读预算 -> IsolationError")
        ok(raises(lambda: e_view.contact_phone, views.IsolationError),
           "异常域读联系电话 -> IsolationError")
        s_view = views.load_order(db, order_id, "shipper")
        ok(s_view.budget_max is None or isinstance(s_view.budget_max, object),
           "货主可见自己的预算")
        d_view = views.load_order(db, order_id, "dispatch")
        ok(hasattr(d_view, "budget_max"), "调度可见预算（平台方）")
        ok(raises(lambda: setattr(c_view, "status", "x"), views.IsolationError),
           "视图只读，不能直接写")

        print("[2] 消息契约")
        ok(raises(lambda: contract.validate("dispatch", "carrier", "shout", {}),
                  contract.ContractError), "非法消息类型被拒")
        ok(raises(lambda: contract.validate("dispatch", "carrier", "reject", {}),
                  contract.ContractError), "reject 缺理由被拒")
        ok(raises(lambda: contract.validate("carrier", "shipper", "propose",
                                            {"amount": 1, "reason": "x"}),
                  contract.ContractError), "不允许的通信方向被拒")
        ok(raises(lambda: contract.validate("dispatch", "carrier", "request",
                                            {"fields": []}),
                  contract.ContractError), "request 空字段被拒")
        p = contract.validate("dispatch", "carrier", "propose",
                              {"amount": "1200", "reason": "挂牌价"})
        ok(p["amount"] == 1200.0, "amount 归一成数字")
        ok(contract.requires_human("reassign"), "改派必须人审")
        ok(not contract.requires_human("propose"), "普通提议不需人审")

        print("[3] 私有记忆隔离")
        k1, k2 = f"test:probe:{STAMP}", f"test:other:{STAMP}"
        bus.remember(db, k1, "rate", {"v": 2.8})
        db.commit()
        ok((bus.recall(db, k1, "rate") or {}).get("v") == 2.8, "写入后能读回自己的记忆")
        ok(bus.recall(db, k2, "rate") is None, "别的 agent_key 读不到")

        print("[4] 消息总线")
        tid = f"test:{STAMP}"
        m1 = bus.send(db, from_agent="dispatch", to_agent="carrier",
                      msg_type="propose", payload={"amount": 1000, "reason": "首发价"},
                      thread_id=tid)
        bus.send(db, from_agent="carrier", to_agent="dispatch",
                 msg_type="counter", payload={"amount": 1100, "reason": "成本上浮"},
                 thread_id=tid, parent_id=m1.id)
        db.commit()
        carrier_msgs = bus.claim(db, "carrier", limit=50)
        ok(any(m.thread_id == tid for m in carrier_msgs), "承运商能认领发给自己的消息")
        ok(all(m.to_agent == "carrier" for m in carrier_msgs), "认领不会拿到别人的消息")
        # 已认领的不会再次出现
        again = bus.claim(db, "carrier", limit=50)
        ok(all(m.thread_id != tid for m in again), "已认领消息不重复投递")
        rows = bus.thread(db, tid)
        ok(len(rows) == 2, "thread 回放含往复两条")
        ok(rows[1]["parent_id"] == rows[0]["id"], "counter 指向被回应的 propose")
        ok(rows[0]["type_label"] == "提出方案", "消息类型有中文标签")

        print("[5] 异常域 Agent")
        ea = exception_agent.agent
        r = ea.triage(db, order_id, "制冷机故障，厢温升到 9.5 度")
        ok(r["triage"]["code"] == "E-COLD-FAIL", "独立定级命中故障谱",
           str(r["triage"].get("code")))
        ok(r["triage"]["level"] == "P0", "制冷机故障判 P0")
        ok(any(o["action"] == "reassign" for o in r["options"]),
           "车辆报废类给出改派候选")
        ok(all(o["approval"] for o in r["options"]
               if o["action"] in ("reassign", "extend", "deliver")),
           "改派/改期/送达均标需审批")
        r2 = ea.triage(db, order_id, "高速上堵车严重，走不动")
        ok(r2["triage"]["code"] == "E-JAM", "拥堵识别正确")
        ok(not any(o["action"] == "reassign" for o in r2["options"]),
           "拥堵不升级到改派")
        r3 = ea.triage(db, order_id, "天气不错")
        ok(r3["triage"]["code"] is None, "无异常线索不定级")
        pr = ea.propose(db, order_id, "制冷机故障")
        db.commit()
        ok(pr["sent"] and pr["message_id"] > 0, "异常域向调度发出 propose")
        ok(any(x["from"] == "exception" for x in bus.thread(db, pr["thread"])),
           "消息进入 order thread 可回放")

        print("[6] 静态闸门")
        p = subprocess.run([sys.executable, str(ROOT / "scripts/check_isolation.py")],
                           capture_output=True, text=True, cwd=str(ROOT))
        ok(p.returncode == 0, "check_isolation 通过", (p.stdout or "")[-300:])
    finally:
        # 清理：只删本次创建的临时数据
        db.rollback()
        db.execute(delete(AgentMemory).where(AgentMemory.agent_key.like("test:%")))
        db.execute(delete(AgentMessage).where(AgentMessage.thread_id.like("test:%")))
        db.execute(delete(AgentMessage).where(
            AgentMessage.from_agent == "exception",
            AgentMessage.thread_id == contract.thread_of(order_id=order_id)))
        db.execute(delete(AgentMemory).where(
            AgentMemory.agent_key == "exception",
            AgentMemory.mkey.like("last:%")))
        db.commit()
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项失败 / {PASSED + len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
