"""验证 plan_route_smart 是否真的接入模型。

用法（项目根目录，必须用 .venv）：
  .venv\Scripts\python.exe scripts/test_route_model_intervention.py

判读：看打印的 model_decision.source 与日志里的 [route_llm] S2/S4/S5 行。
- source=model   -> 模型真实介入
- source=fallback-> 模型/凭证不可用，已 fail-open 退回纯几何/高德初稿
- source=skipped -> 按需跳过（无可提取的自然语言约束，或 allow_model=False；
                    应急重规划未给「受影响路段」时走这一档，省 12-30s）
"""
import asyncio
import logging
import os
import sys

# 把项目根加入 sys.path（脚本可放在任意位置运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sca.mcp.map_server import plan_route_smart


async def main() -> None:
    print("=" * 60)
    print("用例 A：带自然语言路线偏好（应触发 S2+S4+S5 全模型介入）")
    print("=" * 60)
    r = await plan_route_smart(
        "上海", "北京", cargo_type="冷链", temp_ctrl=True,
        natural_lang_req="尽量走高速，避开山区，夜间优先发运，成本敏感",
    )
    _show(r)

    print()
    print("=" * 60)
    print("用例 B：无自然语言偏好（无可提取约束 -> S2/S4/S5 全跳过，source=skipped）")
    print("=" * 60)
    r2 = await plan_route_smart("成都", "广州", cargo_type="普货")
    _show(r2)
    wp = r2.get("waypoints") or []
    print("途经点粒度       :", len(wp), "个 ->", "→".join(wp))
    print("                  （应连续覆盖县级节点，不再只有省会级）")

    print()
    print("=" * 60)
    print("用例 C：离线枢纽优选 _pick_hubs 直接调用（不联网，证明候选池已细化到县级）")
    print("=" * 60)
    from sca.mcp.map_server import _pick_hubs
    for od in [("上海", "北京"), ("广州", "西安"), ("成都", "上海")]:
        hubs = _pick_hubs(*od)
        print(f"  {od[0]}→{od[1]} 枢纽({len(hubs)}): {hubs}")

    print()
    print("=" * 60)
    print("用例 D：allow_model=False（应急重规划未给受影响路段时的档位）")
    print("=" * 60)
    r4 = await plan_route_smart("石家庄", "南京", cargo_type="危险品", hazmat=True,
                                avoid="道路拥堵（石家庄→南京 行程中受影响，具体路段未提供）",
                                natural_lang_req="应急重规划：在该走廊内做局部稳妥绕行",
                                allow_model=False)
    _show(r4)
    print("   notes         :")
    for n in (r4.get("notes") or []):
        print("     -", n)


def _show(r: dict) -> None:
    md = r.get("model_decision") or {}
    print("smart            :", r.get("smart"))
    print("provider         :", r.get("provider"), " distance_km:", r.get("distance_km"))
    print("waypoints        :", r.get("waypoints"))
    print("via_cities       :", r.get("via_cities"), "  <- 高德真实途经城市")
    print("model_constraints:", r.get("model_constraints"))
    print("model_decision   :")
    print("   source        :", md.get("source"), "  <- model=介入 / fallback=未介入")
    print("   model_name    :", md.get("model_name"))
    print("   decision      :", md.get("decision"))
    print("   avoid_cities  :", md.get("avoid_cities"))
    print("   swap_hub      :", md.get("swap_hub"))
    print("   reasons       :", md.get("reasons"))
    print("   risks         :", md.get("risks"))
    print("summary          :", (r.get("summary") or "")[:120])


if __name__ == "__main__":
    asyncio.run(main())
