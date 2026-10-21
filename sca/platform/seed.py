"""种子数据：演示组织与账号。

幂等：已存在的组织/账号跳过，重复执行不会产生重复行。

    ./.venv/Scripts/python.exe -m sca.platform.seed
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from .db import session_as
from .models import CarrierProfile, SysOrg, SysUser
from .security import hash_password

DEMO_PASSWORD = "123456"

ORGS = [
    ("平台运营中心", "platform", "李调度", "400-000-0000"),
    ("恒通物流有限公司", "shipper", "张经理", "13800000001"),
    ("顺达运输有限公司", "carrier", "王师傅", "13900000002"),
]

USERS = [
    # account,     role,         display_name, org_name
    ("shipper1", "shipper", "张经理", "恒通物流有限公司"),
    ("dispatcher1", "dispatcher", "李调度", "平台运营中心"),
    ("carrier1", "carrier", "王师傅", "顺达运输有限公司"),
    ("admin", "admin", "平台管理员", "平台运营中心"),
]


def run() -> int:
    db = session_as("app")
    created = []
    try:
        orgs = {}
        for name, otype, contact, phone in ORGS:
            org = db.execute(select(SysOrg).where(SysOrg.name == name)).scalar_one_or_none()
            if not org:
                org = SysOrg(name=name, org_type=otype, contact_name=contact,
                             contact_phone=phone, status=1)
                db.add(org)
                db.flush()
                created.append(f"组织:{name}")
            orgs[name] = org

        for account, role, display, org_name in USERS:
            user = db.execute(select(SysUser).where(
                SysUser.account == account)).scalar_one_or_none()
            if user:
                continue
            org = orgs[org_name]
            user = SysUser(account=account,
                           password_hash=hash_password(DEMO_PASSWORD),
                           display_name=display, role=role, org_id=org.id,
                           phone=org.contact_phone, status=1)
            db.add(user)
            db.flush()
            created.append(f"账号:{account}")

            if role == "carrier":
                prof = db.execute(select(CarrierProfile).where(
                    CarrierProfile.org_id == org.id)).scalar_one_or_none()
                if not prof:
                    db.add(CarrierProfile(org_id=org.id, carrier_user_id=user.id,
                                          cost_per_km=2.60, capacity_t=28.00,
                                          current_position="石家庄正定物流园"))
                    created.append("承运商画像:顺达运输")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if created:
        print("已创建：" + "、".join(created))
    else:
        print("种子数据已存在，无需创建")
    print(f"\n演示账号（密码均为 {DEMO_PASSWORD}）：")
    for account, role, display, _ in USERS:
        print(f"  {account:<12} {role:<12} {display}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
