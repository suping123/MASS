"""方案 A 验证：钉钉群机器人 atMobiles 精准 @ 到司机与收货方。

验证点：
  1) 手机号校验/脱敏（合法才 @，绝不生成号码）；
  2) send_dispatch_notice -> 钉钉 body 含 at.atMobiles，且正文中出现 @手机号（钉钉规则）；
  3) agents.notify 节点：调度员审核通过后分别对"司机/收货方"下发并 @ 对应手机号；
     号码缺失时只推送不 @，并诚实标注。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["SCA_NOTIFY_DESKTOP"] = "false"   # 测试期间不弹桌面窗

from sca.mcp import notify_server as ns

DRIVER = "13900139000"
RECEIVER = "13800138000"


def test_phone_helpers() -> None:
    assert ns.valid_phone("13800138000") == "13800138000"
    assert ns.valid_phone("138 0013 8000") == "13800138000"
    assert ns.valid_phone("12345") is None
    assert ns.valid_phone("") is None
    assert ns.valid_phone(None) is None
    assert ns.mask_phone("13800138000") == "138****8000"
    assert ns.mask_phone("123") is None
    print("[1] 手机号校验/脱敏 OK ->", ns.mask_phone(RECEIVER))


async def test_dingtalk_body() -> None:
    """用假 webhook 捕获真实请求体（不联网）。"""
    captured = {}

    def fake_post(title, text, at_mobiles=None):
        captured["title"] = title
        captured["text"] = text
        captured["at_mobiles"] = at_mobiles
        return {"ok": True, "latency_ms": 1.0}

    ns._config = lambda: ("https://example.invalid/robot/send?access_token=x", "SECtest")
    # 桩必须打在**唯一网络出口** `_post_markdown_with` 上：send_dingtalk 会优先走
    # sca.credentials.run_chain，用真实凭证直调 `_post_markdown_with`，**不经过**
    # `_post_markdown`。只桩后者会漏——实测会把测试消息真发进钉钉群（2026-10-16）。
    def fake_post_with(webhook, secret, title, text, at_mobiles=None):
        return fake_post(title, text, at_mobiles)

    ns._post_markdown = fake_post
    ns._post_markdown_with = fake_post_with

    r = await ns.send_dispatch_notice(target="司机", text="- 订单：T001",
                                      title="调度通知", at_mobiles=[DRIVER])
    assert r["sent"] is True, r
    assert captured["at_mobiles"] == [DRIVER], captured
    assert f"@{DRIVER}" in captured["text"], "正文必须出现 @手机号才能触发钉钉被@提醒"
    assert r["at_mobiles_masked"] == ["139****9000"], r
    print("[2] 钉钉请求体 at.atMobiles OK；正文含 @手机号；回执脱敏 ->",
          r["at_mobiles_masked"])

    # 非法号码：不 @，也不报错
    r2 = await ns.send_dispatch_notice(target="收货方", text="- 订单：T001",
                                       title="调度通知", at_mobiles=["123"])
    assert r2["at_mobiles_masked"] == [], r2
    assert "@" not in captured["text"].split("订单：T001")[-1], "非法号码不应写入正文"
    print("[3] 非法手机号不 @、不写入正文 OK")


async def test_notify_node() -> None:
    """审核通过后分别 @ 司机与收货方；缺号时只推送。"""
    from sca.agents import SupplyChainGraph
    from sca.settings import settings

    settings.dispatch_review_enabled = False   # 直接走下发，专测 @ 组装
    g = SupplyChainGraph()

    state = {
        "order": {"order_id": "T-AT-001", "origin": "上海", "dest": "北京",
                  "cargo": {"type": "普货", "weight_t": 10}, "contact": RECEIVER},
        "exception": {"code": "E-DELAY", "name": "严重延误", "level": "P2"},
        "current_plan": {"distance_km": 1210, "eta_h": 18, "cost": 3200,
                         "carrier": {"id": "C002", "name": "顺达快运",
                                     "driver_name": "张师傅", "driver_phone": DRIVER}},
        "sop": {"sop_id": "SOP-P2-DELAY", "title": "延误改配"},
        "eta": {"est_h": 20}, "cost": {"est": 3400},
        "metrics": {"replan_ms": 12}, "trace": [], "notifications": [], "mcp_calls": [],
    }
    out = await g.notify(state)
    review = out.get("dispatch_review") or {}
    notes = out.get("notifications") or []
    by = {n["target"]: n for n in notes}
    assert set(by) == {"司机", "收货方"}, by
    assert by["司机"]["at"] is True and by["司机"]["at_mobiles_masked"] == ["139****9000"], by["司机"]
    assert by["收货方"]["at"] is True and by["收货方"]["at_mobiles_masked"] == ["138****8000"], by["收货方"]
    print("[4] 审核通过 -> 司机/收货方各 @ 本人：",
          {k: v["at_mobiles_masked"] for k, v in by.items()})

    # 司机无手机号：只推送不 @，回执标明
    state2 = dict(state)
    state2["current_plan"] = {**state["current_plan"],
                              "carrier": {"id": "C002", "name": "顺达快运"}}
    out2 = await g.notify(state2)
    n2 = {n["target"]: n for n in (out2.get("notifications") or [])}
    assert n2["司机"]["at"] is False and n2["司机"]["at_mobiles_masked"] == []
    assert n2["收货方"]["at"] is True
    print("[5] 司机未配手机号 -> 只推送不 @（未编造号码）：", n2["司机"]["at_mobiles_masked"])
    assert review is not None


async def main() -> None:
    test_phone_helpers()
    await test_dingtalk_body()
    await test_notify_node()
    print("\nALL OK：方案 A（atMobiles @ 到司机与收货方）验证通过")


if __name__ == "__main__":
    asyncio.run(main())
