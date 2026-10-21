"""造两家"陪标"承运商，让竞价看板上有真的选择。

    ./.venv/Scripts/python.exe scripts/seed_demo_carriers.py            # 只看会做什么
    ./.venv/Scripts/python.exe scripts/seed_demo_carriers.py --apply    # 真的建
    ./.venv/Scripts/python.exe scripts/seed_demo_carriers.py --remove   # 撤掉

**为什么需要这个**：库里默认只有一家承运商（顺达）。竞价看板、比价表、
"为什么是这家"的裁决证据，在一家独报的情况下全都无从展示——不是页面没做，
是数据里没有分歧。多一家报价才有比价，多两家才看得出"综合分不等于最低价"。

刻意让三家的画像有冲突（便宜但评分低 / 贵但评分高 / 中间偏快），
否则裁决永远是"选最便宜的"，看不出打分里价格只占一部分。

**只建账号与档案，不建订单**：陪标承运商要不要报价由人（或它自己的
carrier agent）决定，脚本替它接单就等于伪造业务动作。

`--remove` 会拒绝删除**报过价或接过单**的承运商：那些行是协作记录，
删掉账号会留下指向空机构的历史，比留着更难查。
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from sca.platform.db import session_as  # noqa: E402
from sca.platform.models import (AgentActor, CarrierBid, CarrierProfile,  # noqa: E402
                                 ShipperOrder, SysOrg, SysUser)
from sca.platform.security import hash_password  # noqa: E402

# account, 机构名, 每公里成本, 评分, 可载吨位, 当前位置
DEMO = [
    ("carrier2", "中远运力有限公司", "2.35", "4.20", "32.00", "石家庄正定物流园"),
    ("carrier3", "冀运集团有限公司", "2.85", "4.80", "30.00", "保定市莲池区"),
]
PASSWORD = "123456"


def _desc(row) -> str:
    account, org_name, cost, rating, cap, pos = row
    return (f"{org_name}（{account}）成本 ¥{cost}/km · 评分 {rating} · "
            f"{cap} 吨 · 位置 {pos}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库")
    ap.add_argument("--remove", action="store_true", help="撤掉这些承运商")
    args = ap.parse_args()

    db = session_as("app")
    try:
        if args.remove:
            return _remove(db, apply=args.apply)
        return _create(db, apply=args.apply)
    finally:
        db.close()


def _create(db, *, apply: bool) -> int:
    print("将按以下三家承运商的画像补齐陪标账号（已存在则跳过）：")
    for row in DEMO:
        print("  -", _desc(row))

    todo = []
    for account, org_name, cost, rating, cap, pos in DEMO:
        if db.execute(select(SysUser).where(SysUser.account == account)).scalars().first():
            print(f"  跳过 {account}：账号已存在")
            continue
        org = db.execute(select(SysOrg).where(SysOrg.name == org_name)).scalars().first()
        if org is None:
            org = SysOrg(name=org_name, org_type="carrier", status=1)
            db.add(org)
            db.flush()
        user = SysUser(account=account, display_name=org_name,
                       password_hash=hash_password(PASSWORD), role="carrier",
                       org_id=org.id, status=1)
        db.add(user)
        db.flush()
        db.add(CarrierProfile(org_id=org.id, carrier_user_id=user.id,
                              cost_per_km=Decimal(cost), capacity_t=Decimal(cap),
                              rating=Decimal(rating), current_position=pos))
        db.flush()
        todo.append((account, org.id, user.id))

    if not apply:
        db.rollback()
        print("\n（预演，未写库。加 --apply 执行）")
        return 0
    db.commit()
    print(f"\n已建立 {len(todo)} 个承运商账号，密码统一 {PASSWORD}")
    print("提示：carrier agent 是**一个进程管所有承运商 key**，不必为它们各起进程；")
    print("      进程重启后会自动为新的 carrier:* 注册心跳。")
    return 0


def _remove(db, *, apply: bool) -> int:
    """撤掉陪标账号。有业务留痕的一律不删，只报告。"""
    blockers = []
    removable = []
    for account, org_name, *_ in DEMO:
        user = db.execute(select(SysUser).where(
            SysUser.account == account)).scalars().first()
        org = db.execute(select(SysOrg).where(SysOrg.name == org_name)).scalars().first()
        if user is None and org is None:
            continue
        org_id = (user.org_id if user else None) or (org.id if org else None)
        bids = db.execute(select(CarrierBid).where(
            CarrierBid.carrier_org_id == org_id)).scalars().all() if org_id else []
        orders = db.execute(select(ShipperOrder).where(
            ShipperOrder.carrier_org_id == org_id)).scalars().all() if org_id else []
        if bids or orders:
            blockers.append(f"{org_name}：{len(bids)} 条投标 / {len(orders)} 张订单，"
                            "属协作留痕，不删")
            continue
        removable.append((account, org_name, user, org))

    for account, org_name, user, org in removable:
        print(f"  删 {org_name}（{account}）")
    if not apply:
        print("（预演，未写库。加 --apply 执行）")
        return 0
    for account, org_name, user, org in removable:
        org_id = (user.org_id if user else None) or (org.id if org else None)
        if org_id:
            db.query(CarrierProfile).filter(
                CarrierProfile.org_id == org_id).delete()
            db.query(AgentActor).filter(AgentActor.org_id == org_id).delete()
        if user is not None:
            db.delete(user)
        if org is not None:
            db.delete(org)
    db.commit()
    for b in blockers:
        print("  保留", b)
    print(f"已删除 {len(removable)} 个陪标账号，保留 {len(blockers)} 个有留痕的")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
