"""对话 Agent 的公共件：槽位定义、完备性判定、复述文本、意图识别。

设计原则（对话最容易做假的地方就在这里）：
  * **不猜**。解析不到就问，不用默认值把槽位填满——填满的假槽位会一路流到
    调度侧的定价和派单，用户却以为系统"听懂了"。
  * **一次只问最关键的 1-2 项**。连问 5 项没人愿意答。
  * **复述用结构化清单**，不用自然语言散文——用户核对时逐行看比读一段话快。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from sca import cargo as C

# (key, 中文名, 反问话术)
SHIPPER_SLOTS: List[Tuple[str, str, str]] = [
    ("origin", "起运地", "货从哪个城市发运？"),
    ("dest", "目的地", "送到哪个城市？"),
    ("cargo_type", "货类", "运的是什么货？（普货、冷链、危险品…）"),
    ("weight_t", "重量", "大概多少吨？"),
    ("required_by", "时效要求", "最晚什么时候必须送到？"),
    ("budget_max", "预算上限", "这趟运费预算上限是多少？（只用于报价匹配，不会展示给承运商）"),
]

SLOT_LABEL = {k: lab for k, lab, _ in SHIPPER_SLOTS}
SLOT_QUESTION = {k: q for k, _, q in SHIPPER_SLOTS}
REQUIRED = [k for k, _, _ in SHIPPER_SLOTS]

# 用户连着答不对时，第二次追问要换说法：给可选值 / 给示例，
# 而不是把同一句话再发一遍。同一句反问出现三次，用户只能放弃。
SLOT_HINT = {
    "cargo_type": lambda: C.options_text(),
    "origin": lambda: "给城市名就行，例如「石家庄」「成都」；只给省份会按省会算。",
    "dest": lambda: "给城市名就行，例如「南京」「广州」；只给省份会按省会算。",
    "weight_t": lambda: "说数字加吨，例如「8 吨」「12.5 吨」。",
    "required_by": lambda: "说绝对时间最稳，例如「后天 23 点前」「10 月 20 日 18 点前」。",
    "budget_max": lambda: "说个上限就行，例如「1.2 万」「8000」；只用于报价匹配，不会给承运商看。",
}

_AFFIRM = ("是", "对", "确认", "没问题", "正确", "可以", "好", "嗯", "无误",
           "ok", "okay", "yes", "就这样", "没错", "对的", "是的")
_NEGATE = ("不", "不是", "不对", "有误", "错了", "要改", "修改", "改一下",
           "改成", "换成", "no")
# 修正语义可以出现在句中：「货类改成农产品」「目的地换成南京」。
# 这类话不能靠 is_negative 识别——它只看句首，而句首是"货类"。
_CORRECT = re.compile(r"改成|改为|换成|换为|改到|换到|不是.{0,8}是|记错|说错|弄错")


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~]+", "", (text or "").strip()).lower()


def is_affirmative(text: str) -> bool:
    t = _norm(text)
    if not t or len(t) > 12:
        return False
    if t in _AFFIRM:
        return True
    return any(t.startswith(w) for w in _AFFIRM if len(w) >= 2)


def is_negative(text: str) -> bool:
    t = _norm(text)
    if not t or len(t) > 16:
        return False
    if t in _NEGATE:
        return True
    # 只在句首判否定：避免"不着急"这类正常描述被当成驳回
    return any(t.startswith(w) for w in _NEGATE)


def is_correction(text: str) -> bool:
    """句中是否出现明确修正语义。

    与 ``is_negative`` 分工：后者只看句首，是为了不让"不着急"被当成驳回；
    但「货类改成农产品」的修正意图在句中，句首是"货类"。没有这个判据，
    用户在收集阶段改口永远不会生效——说了"改成X"，系统还是按旧值复述。
    """
    return bool(_CORRECT.search(text or ""))


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_budget(text: str) -> Any:
    """从一句话里取预算（元）。取不到返回 None——不猜、不给默认值。

    支持：1.2万 / 一万二 / 8000元 / 8千 / 1.2w
    """
    t = (text or "").replace(",", "").replace("，", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|W)", t)
    if m:
        return Decimal(str(round(float(m.group(1)) * 10000, 2)))
    m = re.search(r"([一二两三四五六七八九十]+)\s*万\s*([一二三四五六七八九])?", t)
    if m:
        wan = 10 if m.group(1) == "十" else _CN_NUM.get(m.group(1), 0)
        tail = _CN_NUM.get(m.group(2) or "", 0)
        if wan:
            return Decimal(str(wan * 10000 + tail * 1000))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(千|k|K)", t)
    if m:
        return Decimal(str(round(float(m.group(1)) * 1000, 2)))
    m = re.search(r"(\d{3,7})(?:\s*(?:元|块|rmb))?", t)
    if m:
        return Decimal(str(int(m.group(1))))
    return None


def cargo_mentioned(text: str) -> bool:
    """判断用户是否真的提了货类。

    `parse_dispatch_request` 在没提货类时会默认填「普货」，直接采信等于替用户
    编了一个货类；冷链/危险品会影响选车与合规校验，猜错代价很高。

    词表在 sca/cargo.py（与落单规则、知识问答共用一份）。以前这里自带一份，
    只有 16 个词，用户说「蚕丝」「农产品」一律判"没提货类"。
    """
    return C.cargo_mentioned(text)


def apply_parse(slots: Dict[str, Any], text: str, overwrite: bool = False) -> Dict[str, Any]:
    """把一句话解析进槽位。

    `overwrite` 控制能否覆盖已有值：
      * False（默认，收集阶段）—— 只填空位。用户分几句补充时，后一句通常只讲一件
        事，绝不能因为「这句话里没提目的地」就把已确认的目的地抹掉。
      * True（用户明确在改）—— 允许覆盖。判据是句子里有「不对/改成/换成」这类
        修正语义，或重述了完整路线（从X到Y）。两者都是用户的明确意图，不是猜测。
    """
    from sca.intake import parse_dispatch_request

    out = dict(slots or {})

    # ---- 编号回答 ----
    # 「有什么货类」的答案带 1..10 编号，用户很可能直接回「2」。
    # 只在"正在问货类"时把纯数字当编号：否则「20」会被读成第 20 类（不存在），
    # 更糟的是把 8 吨读成"第 8 类 = 易碎品"。
    if out.get("_asking") == "cargo_type":
        idx = C.by_index(text)
        if idx:
            _set_cargo(out, idx, overwrite=True, note=C.guess_note(idx))
            return out

    try:
        # use_model=False：走确定性规则，响应快且可复现；规则解析不到就走反问，
        # 比让模型"猜一个城市"更诚实。需要模型兜底时改配置即可。
        r = parse_dispatch_request(text, use_model=False)
    except Exception:  # noqa: BLE001 解析器异常不能打断对话
        r = {"ok": False}

    if r.get("ok"):
        o = r.get("order") or {}
        if o.get("origin") and (overwrite or not out.get("origin")):
            out["origin"] = o["origin"]
        if o.get("dest") and (overwrite or not out.get("dest")):
            out["dest"] = o["dest"]

        if cargo_mentioned(text):
            name, alias = C.classify_cargo(text)
            _set_cargo(out, name or o.get("cargo", {}).get("type"),
                       overwrite=overwrite, note=C.guess_note(alias))

        wt = o.get("cargo", {}).get("weight_t")
        if wt is not None and (overwrite or out.get("weight_t") is None):
            out["weight_t"] = wt
        if o.get("required_by") and (overwrite or out.get("required_by") is None):
            out["required_by"] = o["required_by"]
        if o.get("contact") and not out.get("contact_phone"):
            out["contact_phone"] = o["contact"]
        if r.get("notes"):
            # 「只给了省份，按省会算」这类说明：不阻塞受理，但必须让用户看见，
            # 否则复述里出现"广州"而用户写的是"广东"，会以为是抽错了。
            out["_notes"] = list(r["notes"])

    b = parse_budget(text)
    if b is not None and (overwrite or out.get("budget_max") is None):
        out["budget_max"] = str(b)
    return out

def _set_cargo(out: Dict[str, Any], name: Any, *, overwrite: bool,
               note: str = "") -> None:
    """写货类，并按标准类推导温控/危险品标志。

    temp_ctrl / hazmat 只从货类推，不从句子里的零散词猜——「农产品」里有
    "农"字不代表要温控，而"冷链"一定意味着温控。
    """
    if not name:
        return
    if not overwrite and out.get("cargo_type"):
        return
    out["cargo_type"] = name
    f = C.flags(name)
    out["temp_ctrl"] = f["temp_ctrl"]
    out["hazmat"] = f["hazmat"]
    if note:
        out["_cargo_note"] = note
    else:
        out.pop("_cargo_note", None)


def explicit_route(text: str) -> bool:
    """句子里出现「从X到Y」这类完整路线表述 —— 是重述，不是补充。"""
    return bool(re.search(r"(?:从|由)\s*[\u4e00-\u9fa5]{2,8}\s*(?:到|至|发往|开往)", text or ""))


def missing_slots(slots: Dict[str, Any]) -> List[str]:
    return [k for k in REQUIRED if slots.get(k) in (None, "", [])]


def next_questions(slots: Dict[str, Any], limit: int = 2) -> List[str]:
    return [SLOT_QUESTION[k] for k in missing_slots(slots)[:limit]]


def summary_rows(slots: Dict[str, Any]) -> List[Dict[str, str]]:
    """复述清单：结构化返回，前端可直接渲染成两列表格。"""
    rows: List[Dict[str, str]] = []
    wt = slots.get("weight_t")
    ct = slots.get("cargo_type") or "-"
    if slots.get("temp_ctrl"):
        ct += "（温控）"
    if slots.get("hazmat"):
        ct += "（危险品）"
    rows.append({"label": "起运地", "value": str(slots.get("origin") or "-")})
    rows.append({"label": "目的地", "value": str(slots.get("dest") or "-")})
    rows.append({"label": "货物", "value": ct})
    rows.append({"label": "重量", "value": f"{wt} 吨" if wt not in (None, "") else "-"})
    rows.append({"label": "时效要求", "value": str(slots.get("required_by") or "-")})
    b = slots.get("budget_max")
    rows.append({"label": "预算上限", "value": f"¥{float(b):,.0f}" if b not in (None, "") else "-"})
    return rows


def summary_text(slots: Dict[str, Any]) -> str:
    lines = [f"· {r['label']}：{r['value']}" for r in summary_rows(slots)]
    body = "请核对以下信息：\n" + "\n".join(lines)
    notes: List[str] = []
    if slots.get("_cargo_note"):
        notes.append(str(slots["_cargo_note"]))
    notes += [str(x) for x in (slots.get("_notes") or []) if x]
    if notes:
        body += "\n\n说明：" + "；".join(notes)
    return (body + "\n\n无误请回复「确认」；需要修改直接说要改哪一项"
                   "（例如「起运地改成保定」）。")
