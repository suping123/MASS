"""初始化异常处置知识库（PG + pgvector）。

    ./.venv/Scripts/python.exe scripts/init_knowledge_pg.py            # 建表 + 全量导入
    ./.venv/Scripts/python.exe scripts/init_knowledge_pg.py --schema   # 只建表
    ./.venv/Scripts/python.exe scripts/init_knowledge_pg.py --stats    # 只看状态

前置：PostgreSQL 已启动，且 pgvector 可用（pg_install/pgsql/lib/vector.dll 存在）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.platform import kb  # noqa: E402


def main(argv: list[str]) -> int:
    if "--stats" in argv:
        print(json.dumps(kb.stats(), ensure_ascii=False, indent=2))
        return 0

    print("[1/2] 建表（幂等）…")
    r = kb.ensure_schema()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if not r.get("ok"):
        if r.get("hint"):
            print("提示：", r["hint"])
        return 1
    if "--schema" in argv:
        return 0

    print("\n[2/2] 全量导入（生成 embedding 并写入 pgvector）…")
    s = kb.sync(full=True)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if not s.get("ok"):
        return 1

    print("\n当前状态：")
    print(json.dumps(kb.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
