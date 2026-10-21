"""前端静态契约检查（没有浏览器时的最低保障）。

    ./.venv/Scripts/python.exe scripts/check_frontend.py

它不是"页面好不好看"的检查，是**页面会不会白屏**的检查。四类问题在真实
浏览器里表现为"整块没反应"，而单测与 HTTP 冒烟都发现不了：

1. ``$("x")`` 引用的 id 在页面里根本没有 —— JS 抛 TypeError，后面全不执行；
2. ``querySelectorAll("[data-x]")`` 绑定了没人产出的属性 —— 按钮点了没反应；
3. ``class="..."`` 用了 app.css 里不存在的类 —— 元素在，但看着像没做样式；
4. 四页引用的静态资源版本号不一致 —— 浏览器混用新旧 common.js，
   这正是改造前"货主订单列表整块空白"的原因。

**为什么用 Python 而不跑浏览器**：本机没有浏览器自动化，且 .bat 启动的
服务在 Windows 上还要特定事件循环；能静态确认的就不加运行期依赖。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend_v2"
PAGES = ["login.html", "shipper.html", "carrier.html", "dispatch.html"]
CSS = FE / "app.css"

FAILS: list[str] = []
PASSED = 0
WARNS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def ids_in(html: str) -> set[str]:
    return set(re.findall(r'\bid="([A-Za-z][\w:-]*)"', html))


def id_refs(html: str) -> set[str]:
    """``$("x")`` / ``getElementById("x")`` 的引用。"""
    return set(re.findall(r'\$\("([A-Za-z][\w:-]*)"\)', html)) | \
        set(re.findall(r'getElementById\("([A-Za-z][\w:-]*)"\)', html))


def data_bindings(html: str) -> set[str]:
    """``querySelectorAll("...[data-x]")`` 里绑定的属性名。"""
    return set(re.findall(r'querySelectorAll\("[^"]*\[data-([\w-]+)\]', html))


def data_produced(html: str) -> set[str]:
    """页面里真正产出的 data 属性（含 ``data-x="' + ...`` 这种拼接）。"""
    return set(re.findall(r"\bdata-([\w-]+)=", html))


def css_classes() -> set[str]:
    text = CSS.read_text(encoding="utf-8")
    # 去掉注释再扫，避免注释里的 .xxx 被当成真类名
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return set(re.findall(r"\.([A-Za-z][\w-]*)", text))


def used_classes(html: str) -> set[str]:
    """``class="a b"`` 里出现的类名。

    只取字面部分：``class="tag' + (t ? ...)`` 这种拼接，截到引号/加号为止，
    否则会把 JS 变量名（``cls``）当成类名误报。
    """
    out: set[str] = set()
    for m in re.findall(r'class="([^"\'+]*)', html):
        for c in re.split(r"\s+", m):
            if re.fullmatch(r"[A-Za-z][\w-]*", c):
                out.add(c)
    return out


def main() -> int:
    print("[1] id 引用：每个 $(\"x\") 都要有对应的 id")
    for page in PAGES:
        html = (FE / page).read_text(encoding="utf-8")
        have = ids_in(html)
        miss = sorted(id_refs(html) - have)
        check(f"{page} 的 id 引用都能落地", not miss, f"缺 {miss}")

    print("\n[2] data 绑定：绑了事件却没人产出该属性 = 点了没反应")
    for page in PAGES:
        html = (FE / page).read_text(encoding="utf-8")
        miss = sorted(data_bindings(html) - data_produced(html))
        check(f"{page} 的事件绑定都有对应的 data 属性", not miss, f"缺 {miss}")

    print("\n[3] class 契约：JS 生成的 HTML 用的类必须在 app.css 里有定义")
    known = css_classes()
    all_used: dict[str, set[str]] = {}
    for page in PAGES:
        html = (FE / page).read_text(encoding="utf-8")
        all_used[page] = used_classes(html)
    common = (FE / "common.js").read_text(encoding="utf-8")
    all_used["common.js"] = used_classes(common)
    unknown = sorted({c for v in all_used.values() for c in v} - known)
    # 纯语义类（如 wide、on、off）有些是由 JS 后加的修饰类，这里只报告不判死
    hard = [c for c in unknown if c in {
        "card", "panel-title", "kv", "tag", "row-actions", "empty", "muted",
        "amsg", "amsg-list", "detail-section", "amount-list", "cmp", "warnbox",
        "agstrip", "ags-item", "ags-dot", "ags-meta", "bid-table", "neg-head",
        "wbar", "wseg", "cnt", "drawer", "drawer-mask", "toast", "link",
    }]
    check("契约类都有样式定义", not hard, f"缺 {hard}")
    if unknown:
        WARNS.append("未在 app.css 定义（可能只是语义类）：" + "、".join(unknown))

    print("\n[4] 静态资源版本号：四页必须一致（混用新旧 common.js 会白屏）")
    vers: dict[str, set[str]] = {}
    for page in PAGES:
        html = (FE / page).read_text(encoding="utf-8")
        vers[page] = set(re.findall(r"\?v=([\w]+)", html))
    flat = {v for s in vers.values() for v in s}
    check("四页版本号一致且非空", len(flat) == 1 and bool(next(iter(flat), "")),
          str({k: sorted(v) for k, v in vers.items()}))

    print("\n[5] 关键函数：新页面用到的公共渲染函数确实在 common.js 里")
    need = ["bidTable", "negTimeline", "tenderTag", "countdown", "weightBar"]
    miss = [n for n in need if f"function {n}(" not in common]
    check("公共渲染函数齐全", not miss, f"缺 {miss}")
    for page, funcs in (("dispatch.html", ["renderBidding", "loadBoard", "boardTable",
                                           "openBidding", "agStripHtml", "stopBidTimer"]),
                        ("carrier.html", ["renderBids", "askBid", "openBid"])):
        html = (FE / page).read_text(encoding="utf-8")
        miss = [f for f in funcs if f"function {f}(" not in html]
        check(f"{page} 的新函数都有定义", not miss, f"缺 {miss}")

    print("\n" + "=" * 60)
    for w in WARNS:
        print("  提示：", w)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
