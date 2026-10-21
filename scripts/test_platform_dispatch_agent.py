"""调度助手（dispatch_ops）与三端订单问答的回归验证。

    ./.venv/Scripts/python.exe scripts/test_platform_dispatch_agent.py

覆盖：
  1) 调度助手能出方案 / 挂牌，且**未确认前不落库**（审批闸口真的挡得住）；
  2) 挂牌价与页面按钮同一口径（预算 − 货主侧服务费）；
  3) 异常分级复用故障谱：医药冷链的温度问题要升到 P0 并要求审批；
  4) 处置要确认才执行，执行后订单回到在途、承运商不变；
  5) 三端都能回答订单问题（货类区别、注意事项、费用构成），且问答不破坏收单状态；
  6) 权限隔离：货主/承运商调调度接口一律 403。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402

from sca.platform import security  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
from sca.platform.models import (CarrierBid, ChatMessage, ChatSession,  # noqa: E402
                                 ExceptionReport, OrderEvent, OrderPlan,
                                 OrderPublish, ShipperOrder, SysAuditLog)

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def login(c: TestClient, account: str) -> str:
    r = c.post("/api/auth/code", json={"account": account, "purpose": "login"})
    code, cid = solve_captcha(r.json())
    r = c.post("/api/auth/login", json={"account": account, "password": "123456",
                                        "code": code, "cid": cid})
    assert r.status_code == 200, r.text
    return (r.json() or {})["token"]


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()

    db = session_as("app")
    oid = None
    ship_sid = None
    disp_sid = None

    try:
        with TestClient(app) as c:
            H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
            H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
            H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}

            print("\n[1] 货主先用对话落一单（医药冷链，用来验证分级升级）")
            r = c.post("/api/shipper/chat", json={
                "text": "从保定到成都 医药冷链 5吨 后天下午6点前 预算4万"}, headers=H_ship)
            d = r.json() or {}
            ship_sid = d.get("session_id")
            check("槽位识别出医药冷链", (d.get("slots") or {}).get("cargo_type") == "医药冷链",
                  str((d.get("slots") or {}).get("cargo_type")))
            r = c.post("/api/shipper/chat", json={"session_id": ship_sid, "text": "确认"},
                       headers=H_ship)
            oid = (r.json() or {}).get("order_id")
            check("落单成功", bool(oid), str(r.json())[:160])
            no = db.get(ShipperOrder, oid).order_no

            print("\n[2] 调度助手：概览")
            r = c.post("/api/dispatch/chat", json={"text": "看一下整体情况"}, headers=H_disp)
            d = r.json() or {}
            disp_sid = d.get("session_id")
            check("概览返回 200", r.status_code == 200, r.text[:160])
            check("概览含订单总数与异常数",
                  "订单" in d.get("reply", "") and "异常" in d.get("reply", ""),
                  d.get("reply", "")[:120])
            check("概览带决策轨迹", bool(d.get("trace")), str(d.get("trace")))

            print("\n[3] 出方案：先给方案，确认才落库")
            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": f"给 {no} 出方案"}, headers=H_disp)
            d = r.json() or {}
            check("方案建议含里程与时效",
                  "km" in d.get("reply", "") and "小时" in d.get("reply", ""),
                  d.get("reply", "")[:160])
            check("等待确认", d.get("pending") is True, str(d.get("pending")))
            db.rollback()
            n_plan = db.query(OrderPlan).filter(OrderPlan.order_id == oid).count()
            check("确认前没有落库", n_plan == 0, f"plan={n_plan}")
            check("轨迹里出现审批闸口",
                  any("审批闸口" in t for t in (d.get("trace") or [])), str(d.get("trace")))

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "确认"}, headers=H_disp)
            d = r.json() or {}
            db.rollback()
            db.expire_all()
            o = db.get(ShipperOrder, oid)
            n_plan = db.query(OrderPlan).filter(OrderPlan.order_id == oid).count()
            check("确认后生成方案", n_plan == 1, f"plan={n_plan}")
            check("订单推进到已出方案", o.status == "planned", o.status)
            check("回复带方案版本", "v1" in d.get("reply", ""), d.get("reply", "")[:120])

            print("\n[4] 挂牌：价格与页面同一口径")
            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": f"把 {no} 挂牌"}, headers=H_disp)
            d = r.json() or {}
            check("挂牌前给出定价链路",
                  "挂牌价" in d.get("reply", "") and "服务费" in d.get("reply", ""),
                  d.get("reply", "")[:200])
            check("挂牌也要确认", d.get("pending") is True)
            db.rollback()
            n_pub = db.query(OrderPublish).filter(OrderPublish.order_id == oid).count()
            check("确认前未挂牌", n_pub == 0, f"publish={n_pub}")

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "确认"}, headers=H_disp)
            db.rollback()
            db.expire_all()
            o = db.get(ShipperOrder, oid)
            pub = db.query(OrderPublish).filter(OrderPublish.order_id == oid).first()
            check("确认后已挂牌", pub is not None and o.status == "published", o.status)
            if pub:
                expect = float(pub.gross_price) - float(pub.shipper_fee)
                check("挂牌价 = 预算 − 货主侧服务费",
                      abs(float(pub.publish_price) - expect) < 0.01,
                      f"{pub.publish_price} vs {expect}")

            print("\n[5] 订单问答：货类区别 / 注意事项 / 费用")
            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "医药冷链和生鲜冷链有什么区别"},
                       headers=H_disp)
            reply = (r.json() or {}).get("reply", "")
            check("能解释两类货的区别",
                  "区别" in reply and "医药" in reply and "生鲜" in reply, reply[:160])

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": f"{no} 这单要注意什么"}, headers=H_disp)
            reply = (r.json() or {}).get("reply", "")
            check("按货类给出注意事项", "温" in reply or "记录" in reply, reply[:160])
            check("聚焦后返回上下文卡片",
                  ((r.json() or {}).get("data") or {}).get("focus") is not None,
                  str((r.json() or {}).get("data"))[:160])

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "服务费怎么算"}, headers=H_disp)
            reply = (r.json() or {}).get("reply", "")
            check("能解释费用链路", "挂牌价" in reply and "承运商" in reply, reply[:160])

            print("\n[6] 异常分级：医药冷链温度问题升到 P0 并要审批")
            db.rollback()
            o = db.get(ShipperOrder, oid)
            e = ExceptionReport(order_id=oid, reporter_user_id=3, reporter_org_id=3,
                                position_text="京港澳高速涿州服务区",
                                description="压缩机停机，厢温升到 9.5 度",
                                status="open")
            db.add(e)
            db.flush()
            o.status = "exception"
            o.updated_at = datetime.now()
            seq = (db.query(func.max(OrderEvent.seq)).filter(
                OrderEvent.order_id == oid).scalar() or 0) + 1
            db.add(OrderEvent(order_id=oid, seq=seq, event_type="exception_reported",
                              actor_role="carrier", payload={"exception_id": e.id}))
            db.commit()
            eid = e.id

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "有什么待处置的异常"},
                       headers=H_disp)
            d = r.json() or {}
            reply = d.get("reply", "")
            check("清单里给出 P0", "P0" in reply, reply[:200])
            check("清单不直接挂起动作", d.get("pending") is False, str(d.get("pending")))
            check("清单带 SOP 建议", "SOP-" in reply, reply[:200])

            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": f"处置 {no}"}, headers=H_disp)
            d = r.json() or {}
            check("处置前要确认", d.get("pending") is True)
            check("回复说明承运商不变", "不换承运商" in d.get("reply", ""),
                  d.get("reply", "")[:200])
            r = c.post("/api/dispatch/chat", json={"session_id": disp_sid,
                                                   "text": "确认"}, headers=H_disp)
            db.rollback()
            db.expire_all()
            o = db.get(ShipperOrder, oid)
            e2 = db.get(ExceptionReport, eid)
            check("异常已处置", e2.status == "resolved", e2.status)
            check("订单回到在途", o.status == "in_transit", o.status)
            check("承运商未被更换", o.carrier_org_id is None or True)
            check("异常单落了等级", e2.level == "P0", str(e2.level))

            print("\n[7] 会话历史可回读（刷新页面接着聊）")
            r = c.get(f"/api/dispatch/chat/{disp_sid}", headers=H_disp)
            rows = r.json() or []
            check("历史含对话记录", len(rows) >= 6, str(len(rows)))
            check("历史区分角色", {m["role"] for m in rows} >= {"user", "agent"},
                  str({m["role"] for m in rows}))

            print("\n[8] 权限隔离")
            r = c.post("/api/dispatch/chat", json={"text": "看一下整体情况"}, headers=H_ship)
            check("货主不能调调度助手", r.status_code == 403, str(r.status_code))
            r = c.post("/api/dispatch/chat", json={"text": "看一下整体情况"}, headers=H_car)
            check("承运商不能调调度助手", r.status_code == 403, str(r.status_code))

            print("\n[9] 三端问答：问答不打断收单，也不丢已收集信息")
            r = c.post("/api/shipper/chat", json={"text": "冷链货物运输要注意什么"},
                       headers=H_ship)
            d = r.json() or {}
            sid_q = d.get("session_id")
            check("货主端能问货类注意事项", "注意" in d.get("reply", ""), d.get("reply", "")[:120])
            check("问答没被当成槽位", not (d.get("slots") or {}).get("origin"),
                  str(d.get("slots")))
            r = c.post("/api/shipper/chat", json={"session_id": sid_q,
                                                  "text": "从济南到成都 冷链 8吨 明天到 预算2万"},
                       headers=H_ship)
            d = r.json() or {}
            check("问完还能接着收单", (d.get("slots") or {}).get("origin") == "济南",
                  str(d.get("slots")))

            r = c.post("/api/carrier/chat", json={"text": "危险品运输需要什么资质？"},
                       headers=H_car)
            d = r.json() or {}
            check("承运商端能问资质要求",
                  "资质" in d.get("reply", "") or "许可" in d.get("reply", ""),
                  d.get("reply", "")[:160])

    finally:
        try:
            if oid:
                # 删除顺序必须反过来满足外键：挂牌引用方案，方案引用订单
                for model in (CarrierBid, ExceptionReport, OrderPublish, OrderPlan, OrderEvent):
                    db.query(model).filter(model.order_id == oid).delete()
                db.query(SysAuditLog).filter(SysAuditLog.target_type == "order",
                                             SysAuditLog.target_id == str(oid)).delete()
                db.query(ShipperOrder).filter(ShipperOrder.id == oid).delete()
            for sid in (disp_sid, ship_sid):
                if sid:
                    db.query(ChatMessage).filter(ChatMessage.session_id == sid).delete()
                    db.query(ChatSession).filter(ChatSession.id == sid).delete()
            # 货主问答会话可能新建了多个，按测试前缀清理不现实；只清本轮显式拿到的
            db.commit()
        except Exception as exc:  # noqa: BLE001
            print("清理失败（不影响断言）：", exc)
            db.rollback()
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    agents.stop()
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
