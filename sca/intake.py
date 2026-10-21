"""调度需求受理（FR-01 订单接入）：把货运人员的自然语言诉求转成结构化运单。

设计：
  * 规则优先（城市词典 / 正则）：零延迟、可解释、离线可用；
  * 规则未解析出起讫地时，调用在线模型（Qwen）兜底抽取，并标注 source=model；
  * 输出结构化 order + 缺失字段清单 missing（缺什么就在前端提示补什么），
    不再随机生成订单——调度员说什么，系统就按什么落单；
  * 不做任何默认值填充（重量/时间窗等缺失即留空），未识别的字段绝不编造，
    起讫地缺失时由调用方（/api/trigger）阻断执行并要求补录。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from sca.cargo import ALIAS_INDEX as _CARGO_ALIASES
from sca.settings import settings

logger = logging.getLogger("sca.intake")

# ---------------------------------------------------------------------------
# 规则知识：货物类型 / 属性词 / 时效词
# ---------------------------------------------------------------------------
# 货类规则由 sca/cargo.py 的词典生成，不再手写一份。
# 以前这里手写、对话侧一份、问答侧又一份，三份不同步：
# 用户在对话里说"粗加工蚕丝"，对话侧认不出、这里也没有纺织品的规则，
# 于是一票纺织品被落成「普货」，而问"纺织品要注意什么"又能答得出来。
# ALIAS_INDEX 已按别名长度倒序，"医药冷链"一定先于"冷链"命中。
_CARGO_RULES: List[Tuple[str, str]] = [
    (re.escape(alias), name) for alias, name in _CARGO_ALIASES
]

_TEMP_WORDS = r"冷链|冷藏|冷冻|温控|厢温|保鲜|疫苗|医药|生鲜|冻品|2-8|零下|恒温"
_HAZ_WORDS = r"危险品|危化|易燃|易爆|腐蚀性|化工|锂电池"

# 日期词（当日/次日）与"起运时刻 + 时限"分开处理，便于组合成"当日20:00起，4天内"
_DAY_WORDS = [
    (r"今日|今天|当天", "当日"),
    (r"明日|明天|次日", "次日"),
    (r"后天", "后天"),
]
# 相对时限的其它表达（X小时内 / X点前 / HH:MM前 / M月D日）
_TIME_WORDS = [
    (r"(\d+)\s*小时内", r"\1小时内"),
    (r"(\d{1,2}):(\d{2})\s*前", r"\1:\2前"),
    (r"(\d+)\s*点前", r"\1点前"),
    # 「后天23点整」是"最晚这时送到"，不是发车时刻。之前只认"X点前"，
    # 于是"后天23点整"被抽成"后天"——承诺时刻整个丢了。
    # 负向前瞻是为了不抢 _CLOCK_RE 的"20点整起"（那是发车时刻，不是时限）。
    (r"(\d{1,2})\s*点\s*(?:整|正)(?!\s*(?:起|开始|出发|发车|起运|装车))", r"\1:00前"),
    (r"(\d{1,2})月(\d{1,2})[日号]", r"\1月\2日"),
]
_URGENT_WORDS = r"加急|紧急|尽快|马上|立刻|优先|特急"

# 起运时刻："今日20点整起" / "9:30 出发"
_CLOCK_RE = [
    re.compile(r"(\d{1,2})\s*点\s*(?:整|正)?\s*(?:起|开始|出发|发车|起运|装车)"),
    re.compile(r"(\d{1,2}):(\d{2})\s*(?:起|开始|出发|发车|起运|装车)"),
]
# 运输时限："4天" / "2周" / "48小时（内）"
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(天|日|周)\s*(?:内|之内)?")
_DURATION_H_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|钟头)\s*(?:内|之内)?")

_SEQ = {"n": 0}


def _next_order_id() -> str:
    _SEQ["n"] += 1
    return f"SO{int(time.time()) % 100000:05d}{_SEQ['n']:02d}"


def _city_spans(text: str) -> List[Tuple[int, str, str]]:
    """识别文本中的地名，返回 [(位置, 原词, 归一城市)]，按位置排序、按城市去重。

    三层词表：别名（沪/京/宁…）→ 城市库（_CITY_DB，已剔除省级条目）→ 省份名（省→省会）。
    同位置的候选按原词长度降序（"河北省"优先于"河北"），去重时保留最先出现的那条。
    保留"原词"是为了让"河北→南京"这类省份级需求在展示时仍贴近调度员原话。
    """
    from sca.mcp.map_server import (_ALIAS, _CITY_DB, _PROVINCE_CAPITAL,
                                    _REGION_SUFFIXES)

    found: List[Tuple[int, str, str]] = []
    for alias, city in _ALIAS.items():
        if len(alias) == 1:
            # 单字别名（沪/京/穗…）只在"方向词语境"里成立，否则"宁德时代""深圳"会被误判
            pat = (rf"{_ALIAS_CTX}({re.escape(alias)})"
                   rf"(?=[\s，,。；;、:：]|{_DIRECTION_WORDS}|$)")
            for m in re.finditer(pat, text):
                found.append((m.start(1), m.group(1), city))
        else:
            for m in re.finditer(re.escape(alias), text):
                found.append((m.start(), m.group(0), city))
    for city in _CITY_DB:
        if len(city) < 2:        # 单字名（如"南"）易误匹配，仅 >=2 字参与检索
            continue
        for m in re.finditer(re.escape(city), text):
            found.append((m.start(), m.group(0), city))
    # 省份名（含/不含行政区划后缀）-> 省会代表城市，使"河北→云南"类需求也能解析
    for prov, capital in _PROVINCE_CAPITAL.items():
        for suf in ("", *_REGION_SUFFIXES):
            tok = prov + suf
            if not tok or tok in _CITY_DB:   # 与真实城市同名的省份留给城市库处理
                continue
            for m in re.finditer(re.escape(tok), text):
                found.append((m.start(), m.group(0), capital))
    found.sort(key=lambda x: (x[0], -len(x[1])))
    # 去重（同一城市保留首次出现位置）
    seen, out = set(), []
    for pos, label, city in found:
        if city in seen:
            continue
        seen.add(city)
        out.append((pos, label, city))
    return out


def _cities(text: str) -> List[str]:
    """从文本中识别城市（归一后的城市名列表，保持向后兼容）。"""
    return [city for _, _, city in _city_spans(text)]


def _is_province_token(tok: str) -> bool:
    """判断原词是否是省份名（可带"省/自治区/特别行政区"后缀）。"""
    from sca.mcp.map_server import _PROVINCE_CAPITAL, _REGION_SUFFIXES
    s = str(tok or "").strip()
    if s in _PROVINCE_CAPITAL:
        return True
    for suf in _REGION_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) and s[: -len(suf)] in _PROVINCE_CAPITAL:
            return True
    return False


_DIRECTION_WORDS = r"(?:到|至|→|->|发往|送往|运往|抵达|送达|发出|出发|起运至|去往|运至)"

# 单字别名的"出现语境"：行首/标点/空格，或紧跟在方向词、"从"之后。
# 例："沪至穗""京→沪""从沪出发"命中；"宁德时代""深圳""南宁"不命中（后字非方向词）。
_ALIAS_CTX = (r"(?:^|[\s，,。；;、:：]|到|至|→|->|发往|送往|运往|抵达|送达|"
              r"发出|出发|起运至|去往|运至|从|经)")


def _parse_origin_dest(text: str,
                       spans: List[Tuple[int, str, str]]
                       ) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]:
    """判定起讫地：优先"方向词"给出的显式关系，其次按出现顺序取前两个。

    一段需求里可能同时命中多个地名候选（例如"河北到南京"既命中省名"河北"、又命中
    省会"石家庄"的映射，还有"南京"）。此时仅按出现顺序取前两个会取错，必须用
    "A 到 B" 的方向词关系消歧——这是 2026-10-15 那次"目的地被解析成石家庄"的根因。

    返回 (起运地, 目的地)，元素形如 {"label": 原词, "city": 归一城市}。
    """
    if not spans:
        return None, None
    items = [{"label": lb, "city": ct} for _, lb, ct in spans]
    if len(items) == 1:
        return items[0], None
    best: Optional[Tuple[int, Dict[str, str], Dict[str, str]]] = None
    for i, a in enumerate(items):
        for j, b in enumerate(items):
            if i == j:
                continue
            pat = (rf"{re.escape(a['label'])}.{{0,8}}?{_DIRECTION_WORDS}"
                   rf".{{0,8}}?{re.escape(b['label'])}")
            m = re.search(pat, text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), a, b)
    if best:
        return best[1], best[2]
    return items[0], items[1]


def _weight_t(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(吨|t|T)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(公斤|kg|KG)", text)
    if m:
        return round(float(m.group(1)) / 1000, 3)
    m = re.search(r"(\d+(?:\.\d+)?)\s*方(?:米)?", text)   # 轻泡货按 1方≈0.25t 估算
    if m:
        return round(float(m.group(1)) * 0.25, 2)
    return None


def _volume_m3(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:方|立方米|m3|m³)", text)
    return float(m.group(1)) if m else None


def _time_window(text: str) -> Optional[Dict[str, str]]:
    """收货时间窗：仅当文本明确给出（如 08:00-18:00 / 9:00到12:00）才记录。"""
    m = re.search(r"(\d{1,2}:\d{2})\s*(?:[-~—–至到])\s*(\d{1,2}:\d{2})", text)
    if not m:
        return None
    return {"start": m.group(1), "end": m.group(2)}


# 时段词 -> 24 小时制（2026-10-15：「明日下午3点前到」曾被算成次日 03:00，剩余
# 9.7h < ETA 16.1h，导致一张完全可执行的普通单被判「时效违约」）。
_PM_RE = re.compile(r"(?:下午|午后|傍晚|晚上|夜里|夜间|晚)\s*(\d{1,2})\s*(点|:)"
                    r"(\d{2})?")
_AM_RE = re.compile(r"(?:凌晨|清晨|早晨|早上|上午)\s*(\d{1,2})\s*(点|:)(\d{2})?")


def _norm_clock(text: str) -> str:
    """把时段表达归一为 24 小时制："下午3点"->"15点"、"晚上8:30"->"20:30"。"""
    def _pm(m):
        h = int(m.group(1)) + 12 if 1 <= int(m.group(1)) <= 11 else int(m.group(1))
        return f"{h}{m.group(2)}{m.group(3) or ''}"
    def _am(m):
        h = 0 if int(m.group(1)) == 12 else int(m.group(1))
        return f"{h}{m.group(2)}{m.group(3) or ''}"
    return _AM_RE.sub(_am, _PM_RE.sub(_pm, text))


def _start_clock(text: str) -> Optional[str]:
    """起运时刻：'20点整起' / '9:30 出发' -> '20:00' / '09:30'。"""
    text = _norm_clock(text)
    m = _CLOCK_RE[0].search(text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    m = _CLOCK_RE[1].search(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _duration(text: str) -> Tuple[Optional[str], Optional[float]]:
    """运输时限：'4天' / '2周' / '48小时内' -> ('4天内', 4.0)。"""
    m = _DURATION_RE.search(text)
    if m:
        n = float(m.group(1))
        if m.group(2) == "周":
            n *= 7
            return f"{m.group(1)}周内（约{n:g}天）", n
        return f"{n:g}天内", n
    m = _DURATION_H_RE.search(text)
    if m:
        n = float(m.group(1))
        return f"{n:g}小时内", round(n / 24.0, 2)
    return None, None


def _required_by(text: str) -> Optional[str]:
    """时效要求：日期词 + 起运时刻 + 时限组合，如"今日20点整起4天" -> "当日20:00起，4天内"。"""
    day = next((tpl for pat, tpl in _DAY_WORDS if re.search(pat, text)), None)
    text = _norm_clock(text)   # 下午/晚上/上午 -> 24 小时制（见 _norm_clock）
    clock = _start_clock(text)
    dur_label, _ = _duration(text)

    parts: List[str] = []
    if clock:
        parts.append(f"{clock}起")
    # 其余相对时限表达（X小时内 / X点前 / M月D日）
    for pat, tpl in _TIME_WORDS:
        m = re.search(pat, text)
        if m:
            try:
                parts.append(re.sub(pat, tpl, m.group(0)))
            except Exception:
                parts.append(m.group(0))
    if day:                       # 日期词并入首个时间片段，避免"当日，20:00起"的割裂
        if parts:
            parts[0] = day + parts[0]
        else:
            parts.append(day)
    if dur_label:
        parts.append(dur_label)
    return "，".join(dict.fromkeys(parts)) if parts else None


def _llm_extract(text: str) -> Dict[str, Any]:
    """在线模型兜底抽取（规则未解析出起讫地时）。失败返回空字典。"""
    key = settings.dashscope_api_key
    if not key:
        return {}
    prompt = (
        "你是供应链调度需求受理助手。从下面这段货运人员的话里抽取结构化信息，"
        "只输出 JSON，不要任何解释。字段：origin(起运地城市名)、dest(目的地城市名)、"
        "cargo_type(货物类型)、weight_t(重量吨,数字)、temp_ctrl(是否温控,布尔)、"
        "hazmat(是否危险品,布尔)、required_by(时效要求原文)、customer(客户/收货方)、"
        "contact(联系电话)。抽取不到就填 null。\n\n文本：" + text
    )
    payload = {"model": settings.intent_model,
               "messages": [{"role": "user", "content": prompt}],
               "timeout": int(settings.intent_timeout)}
    try:
        import urllib.request
        req = urllib.request.Request(
            settings.dashscope_base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=settings.intent_timeout + 5) as r:
            j = json.loads(r.read().decode("utf-8"))
        content = j["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception as exc:
        logger.warning("[intake] 模型抽取失败，按规则结果返回：%s", exc)
        return {}


def _norm_label(label: str) -> Optional[str]:
    """把任意地名原词归一为城市（模型兜底给出省份名时也要落到省会）。"""
    try:
        from sca.mcp.map_server import _norm_city
        return _norm_city(str(label or ""))
    except Exception:   # noqa: 路网不可用时不阻塞受理
        return str(label or "") or None


def _display_label(item: Optional[Dict[str, str]], role: str = "") -> Optional[str]:
    """展示用起讫地：**统一落成城市名**。

    2026-10-17 变更：原先"原词是省份就保留省份原词"（`河北` 直接进 `order.origin`），
    结果订单列表显示「河北→成都」，承运商与调度都以为起点是一个省而不是一座城。
    现在一律用归一城市（省份 -> 省会），原词只留在 `source_text` 里可追溯。

    例："河北到南京" -> ("石家庄", "南京")；`origin_city` 与 `origin` 保持一致。
    """
    if not item:
        return None
    label = str(item.get("label") or "").strip()
    city = str(item.get("city") or "").strip()
    if not label:
        return city or None
    return city or label


def parse_dispatch_request(text: str, use_model: bool = True) -> Dict[str, Any]:
    """把一段自然语言调度诉求解析为结构化运单（order）+ 缺失字段提示。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "调度需求描述为空"}

    spans = _city_spans(text)
    o_item, d_item = _parse_origin_dest(text, spans)
    origin = _display_label(o_item, "origin")
    dest = _display_label(d_item, "dest")
    origin_city = (o_item or {}).get("city")
    dest_city = (d_item or {}).get("city")

    cargo_type = "普货"
    for pat, ct in _CARGO_RULES:
        if re.search(pat, text):
            cargo_type = ct
            break
    temp_ctrl = bool(re.search(_TEMP_WORDS, text))
    hazmat = bool(re.search(_HAZ_WORDS, text))
    weight_t = _weight_t(text)
    volume_m3 = _volume_m3(text)
    time_window = _time_window(text)
    required_by = _required_by(text)
    urgent = bool(re.search(_URGENT_WORDS, text))
    start_clock = _start_clock(text)
    _, duration_days = _duration(text)

    m_order = re.search(r"(?:订单号|运单号|单号)\s*[:：]?\s*([A-Za-z0-9\-_]+)", text)
    # 客户名须以标点/空白/句尾收束，避免把"希望…必须送到"整句吞进来
    m_cust = re.search(r"(?:客户|收货方|收货人)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,10})(?=[，,。;；\s]|$)", text)
    m_tel = re.search(r"(1\d{10})", text)

    customer = m_cust.group(1) if m_cust else None
    source = "rule"
    # 关键字段缺失 -> 模型兜底
    if use_model and (not origin or not dest):
        ext = _llm_extract(text)
        if ext:
            origin = origin or ext.get("origin")
            dest = dest or ext.get("dest")
            origin_city = origin_city or _norm_label(origin)
            dest_city = dest_city or _norm_label(dest)
            cargo_type = cargo_type if cargo_type != "普货" else (ext.get("cargo_type") or cargo_type)
            weight_t = weight_t if weight_t is not None else ext.get("weight_t")
            temp_ctrl = temp_ctrl or bool(ext.get("temp_ctrl"))
            hazmat = hazmat or bool(ext.get("hazmat"))
            required_by = required_by or ext.get("required_by")
            customer = customer or ext.get("customer")
            source = "model" if (origin and dest) else "rule+model"

    # 省份级起讫地的提示（按省会计算里程，不阻塞受理）。
    # 注意：判断依据必须是**原词 label**，不能用 origin —— 它现在已经是省会名了。
    notes: List[str] = []
    for item, city, role in ((o_item, origin_city, "起运地"), (d_item, dest_city, "目的地")):
        raw = str((item or {}).get("label") or "").strip()
        # raw == city 时不提示：直辖市（上海/北京/天津/重庆）本身就是省级也是城市名，
        # 提示"只给到省份，已按省会上海计算"属于噪音。
        if raw and city and raw != city and _is_province_token(raw):
            notes.append(f"{role}只给到省份（{raw}），已按省会 {city} 计算里程/时效，建议补录具体城市")

    # 缺失字段清单（前端提示补录）
    missing: List[str] = []
    if not origin:
        missing.append("origin")
    if not dest:
        missing.append("dest")
    # 缺失字段一律留空并列入 missing —— 不做任何默认值填充（不编造需求）
    if weight_t is None:
        missing.append("weight_t")
    if not required_by:
        missing.append("required_by")

    order = {
        "order_id": m_order.group(1) if m_order else _next_order_id(),
        "cargo": {"type": cargo_type, "weight_t": weight_t, "volume_m3": volume_m3},
        "origin": origin, "dest": dest,
        "origin_city": origin_city, "dest_city": dest_city,
        "route_hint": f"{origin}→{dest}" if origin and dest else None,
        "temp_ctrl": temp_ctrl, "hazmat": hazmat,
        "time_window": time_window,   # 未提及则不臆造时间窗
        "required_by": required_by, "urgent": urgent,
        "start_clock": start_clock, "duration_days": duration_days,
        "customer": customer,
        "contact": m_tel.group(1) if m_tel else None,
        "source_text": text,
    }
    return {
        "ok": True, "order": order, "missing": missing, "source": source,
        "notes": notes,
        "confidence": round(0.95 if not missing else max(0.4, 0.95 - 0.18 * len(missing)), 2),
        "evidence": {"cities_found": [city for _, _, city in spans],
                     "city_labels": [{"label": lb, "city": ct} for _, lb, ct in spans],
                     "cargo_type": cargo_type,
                     "temp_ctrl": temp_ctrl, "hazmat": hazmat},
    }
