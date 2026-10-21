"""生成离线看板快照（自包含 HTML，无需后端即可打开查看 / 评审截图）。

做法：把 frontend/index.html 的 /style.css、/app.js 内联，并注入
window.__SEED__（来自 sca.dashboard.dashboard_data()），输出 dashboard.html。

运行：python scripts/render_dashboard.py  ->  dashboard.html（项目根）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sca.dashboard import dashboard_data  # noqa: E402


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fe_dir = os.path.join(root, "frontend")
    html_path = os.path.join(fe_dir, "index.html")
    css_path = os.path.join(fe_dir, "style.css")
    js_path = os.path.join(fe_dir, "app.js")
    if not (os.path.isfile(html_path) and os.path.isfile(css_path) and os.path.isfile(js_path)):
        print("[render] 缺少 frontend/ 静态资源，无法生成快照")
        return

    html = open(html_path, encoding="utf-8").read()
    css = open(css_path, encoding="utf-8").read()
    js = open(js_path, encoding="utf-8").read()

    seed = asyncio.run(dashboard_data())
    html = html.replace('<link rel="stylesheet" href="/style.css">', f"<style>{css}</style>")
    html = html.replace('<script src="/app.js"></script>', f"<script>{js}</script>")
    html = html.replace("<!--SEED-->",
                        f"<script>window.__SEED__ = {json.dumps(seed, ensure_ascii=False)}</script>")

    out = os.path.join(root, "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"离线看板快照已生成：{out}")


if __name__ == "__main__":
    main()
