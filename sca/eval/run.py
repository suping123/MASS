"""评估 CLI：python -m sca.eval.run

原型期模拟论证入口。输出 L0/L1 指标判定表、基线假设、敏感性分析，并写
results/eval_result.json（可复现，验收会据此审查"假设合理性 + 脚本可复现"）。
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from sca.eval.harness import run_all


async def _print() -> None:
    rep = await run_all()
    raw = rep["raw"]
    print("=" * 70)
    print("供应链多智能体系统 —— 原型期模拟论证评估（L0/L1 分层）")
    print(f"生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"能力就绪: tongyi-intent-detect-v3(API Key)={raw.get('model_available')} | "
          f"PG+pgvector={raw.get('pg_available')}")
    print("=" * 70)

    metrics = rep["metrics"]
    passed = sum(1 for m in metrics if m.status == "pass")
    failed = sum(1 for m in metrics if m.status == "fail")
    na = sum(1 for m in metrics if m.status == "na")
    print(f"\n指标判定：通过 {passed} / 失败 {failed} / 需外部能力验收(na) {na}\n")
    for m in metrics:
        print(m)
        if m.note:
            print(f"       └─ {m.note}")

    if rep["intent_misses"]:
        print("\n--- 意图路由未命中样本（前 10）---")
        for inp, got, exp in rep["intent_misses"]:
            print(f"  话术={inp!r} 预测={got} 期望={exp}")
    if rep["sop_misses"]:
        print("\n--- SOP 匹配未命中样本（前 10）---")
        for code, got, exp in rep["sop_misses"]:
            print(f"  异常={code} 预测={got} 期望={exp}")

    # 写结果（best-effort）
    out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "results")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "eval_result.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "metrics": [m.__dict__ for m in rep["metrics"]],
                "raw": {k: v for k, v in raw.items() if not isinstance(v, dict)},
            }, fh, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已写入: {os.path.abspath(path)}")
    except Exception as exc:  # noqa
        print(f"\n结果写盘跳过: {exc}")


if __name__ == "__main__":
    asyncio.run(_print())
