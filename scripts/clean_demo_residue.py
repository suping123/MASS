"""清掉测试/演示留下的残留数据。

    ./.venv/Scripts/python.exe scripts/clean_demo_residue.py --orphans
    ./.venv/Scripts/python.exe scripts/clean_demo_residue.py --ids 559 592
    ./.venv/Scripts/python.exe scripts/clean_demo_residue.py --ids 559 --confirm

默认只**列出**会删什么（`--dry-run` 是默认行为），加 `--confirm` 才真删。

两类残留，来源不同：

* **孤儿招标单 / 投标**：`tender` / `carrier_bid` 的 `order_id` 已经不存在了。
  历史版本的 `smoke_platform.py` 清理时没删 `tender`，每跑一次自检就留一张
  （订单删了、招标单还在）。看板会因为订单查不到而跳过它们，界面上看不见，
  但表会一直涨，`_bid_stats` 也白扫。
* **测试订单**：只认显式给出来的 id。**不按时间、不按 order_no 前缀猜**——
  猜着删迟早删到真实业务数据。

为什么不塞进服务代码：这是运维动作，不该在业务路径上有个"顺便删点东西"的分支。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402

from sca.platform import models as M  # noqa: E402
from sca.platform.db import session_as  # noqa: E402

# 删订单时按外键依赖排序：publish 引用 plan，plan 必须在 publish 之后删
ORDER_CHILDREN = (M.Tender, M.CarrierBid, M.OrderEvent, M.ExceptionReport,
                  M.Notification, M.Settlement, M.ChatSession, M.OrderPublish,
                  M.OrderPlan)


def _orphans(db) -> tuple[list[int], int, int]:
    live = {row[0] for row in db.execute(select(M.ShipperOrder.id)).all()}
    tids = [t.order_id for t in db.execute(select(M.Tender)).scalars() if t.order_id not in live]
    n_t = db.execute(delete(M.Tender).where(M.Tender.order_id.in_(tids))).rowcount if tids else 0
    n_b = 0
    if tids:
        n_b = db.execute(delete(M.CarrierBid).where(
            M.CarrierBid.order_id.in_(tids))).rowcount
    return tids, n_t, n_b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orphans", action="store_true",
                    help="清掉 order 已不存在的招标单与投标")
    ap.add_argument("--ids", nargs="*", type=int, default=[],
                    help="要整单删除的订单 id（必须显式给出）")
    ap.add_argument("--confirm", action="store_true", help="真的删；不加只预览")
    args = ap.parse_args()
    if not args.orphans and not args.ids:
        print("给一个范围：--orphans 或 --ids 1 2 3")
        return 1

    db = session_as("app")
    try:
        if args.orphans:
            live = {row[0] for row in db.execute(select(M.ShipperOrder.id)).all()}
            dead = sorted({t.order_id for t in db.execute(select(M.Tender)).scalars()
                           if t.order_id not in live})
            n_bid = db.execute(select(M.CarrierBid).where(
                M.CarrierBid.order_id.in_(dead))).scalars().all() if dead else []
            print(f"孤儿招标单所在订单 {len(dead)} 个：{dead[:20]}"
                  f"{' …' if len(dead) > 20 else ''}")
            print(f"  将删除 tender {len(dead)} 张 / carrier_bid {len(n_bid)} 条")
            if args.confirm and dead:
                _, n_t, n_b = _orphans(db)
                db.commit()
                print(f"  已删除 tender {n_t} 张 / carrier_bid {n_b} 条")
            elif dead:
                print("  预览模式，未删除（加 --confirm）")

        if args.ids:
            exist = [row[0] for row in db.execute(
                select(M.ShipperOrder.id).where(M.ShipperOrder.id.in_(args.ids))).all()]
            print(f"要整单删除的订单：{exist}")
            if not args.confirm:
                print("  预览模式，未删除（加 --confirm）")
            else:
                for model in ORDER_CHILDREN:
                    db.execute(delete(model).where(model.order_id.in_(args.ids)))
                db.execute(delete(M.AgentMessage).where(M.AgentMessage.thread_id.in_(
                    [f"order:{i}" for i in args.ids])))
                db.execute(delete(M.ShipperOrder).where(M.ShipperOrder.id.in_(args.ids)))
                db.commit()
                left = db.execute(select(M.ShipperOrder).where(
                    M.ShipperOrder.id.in_(args.ids))).scalars().all()
                print(f"  已删除；残留 {len(left)} 张")

        print(f"现在库里有订单 {db.query(M.ShipperOrder).count()} 张 / "
              f"招标单 {db.query(M.Tender).count()} 张")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
