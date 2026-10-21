"""高德 / 和风 API Key 自检（命令行）。

用法：
    python scripts/check_keys.py            # 检查两个 Key
    python scripts/check_keys.py amap       # 只查高德
    python scripts/check_keys.py qweather   # 只查和风

退出码：0=全部可用；1=已配置但不可用；2=未配置（打印获取步骤，不联网）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sca.keys_check import check_all  # noqa: E402


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    rep = check_all(which)
    for it in rep["items"]:
        print(f"\n=== {it['name']} ===")
        print("  Key:", it["key_masked"] or "（空）")
        if it["service"] == "qweather":
            print("  Host:", it.get("host") or "（未填，自动探测）")
        if not it["configured"]:
            print("  [未配置]", it["message"])
            print("  获取步骤：")
            for i, s in enumerate(it["steps"], 1):
                print(f"    {i}. {s}")
            continue
        tag = "[OK]" if it["ok"] else "[FAIL]"
        print(f"  {tag} {it['message']}")
        if it.get("reason"):
            print("         含义：", it["reason"])
    if rep["all_ok"] and not rep["any_missing"]:
        print("\n全部通过：两个 Key 均已生效，系统使用真实地图/天气服务。")
        return 0
    code = 2 if rep["any_missing"] else 1
    print(f"\n存在未通过项（退出码 {code}）：未配置=2，已配置但不可用=1。")
    return code


if __name__ == "__main__":
    sys.exit(main())
