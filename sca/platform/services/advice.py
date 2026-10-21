"""订单问答：把"这单要注意什么"这类问题答准，而不是把问题当成槽位输入。

三个平台的 Agent 共用这一层，原因很实际：
  - 货主问"危化品要什么车"、承运商问"冷链能不能拉"、调度问"医药和生鲜差在哪"，
    本质是同一个知识问题，只是各自看得到的订单字段不同；
  - 答案必须**只依赖订单里真实存在的字段**，不能凭猜测补细节（例如订单没写温区，
    就不能说"按 2-8℃ 运输"）。

回答分两类：
  1) 知识类（货类区别、车辆要求、注意事项、费用构成、状态含义）——模板 + 词表；
  2) 异常类（"温控超标怎么办"）——交给知识库检索 SOP，检索不到就直说检索不到，
     不编造处置步骤。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 货类词典只有一份，在 sca/cargo.py。这里 import 进来而不是再抄一份：
# 三份词表不同步的后果是「同一票货在三个地方叫三个名字」。
from sca.cargo import (ALIAS_INDEX as _ALIAS_INDEX, CARGO_KB,  # noqa: E402
                       cargo_meta, find_cargo)

# ---------------------------------------------------------------- 意图识别
_Q_MARK = re.compile(r"[？?]|吗|呢|怎么|怎样|如何|为什么|为啥|什么|是否|能不能|可不可以|"
                     r"要注意|注意什么|区别|差别|不同|要求|需要|流程|多久|多长时间|"
                     r"多少|算|解释|说明|介绍|区分|怎么办|怎么处理|啥")
_SLOT_LIKE = re.compile(r"^\s*(从|由)?[\u4e00-\u9fa5]{2,8}\s*(到|至|发往|运往)\s*[\u4e00-\u9fa5]{2,8}"
                        r"[\s，,]|^\s*\d+(\.\d+)?\s*(吨|t|T|方|m3)")
_FEE_WORDS = re.compile(r"服务费|手续费|佣金|抽成|费率|扣多少|为什么扣|怎么算|价格|定价|报价|成本")
_STATUS_WORDS = re.compile(r"状态|进度|到哪了|什么阶段|为什么还是|还没|卡在")
_ETA_WORDS = re.compile(r"多久|多长时间|几天|时效|什么时候到|预计到|能不能到")
_ORDER_WORDS = re.compile(r"这单|这票|这个订单|订单情况|订单信息|货主|收发货|起运|目的地|线路|路线")
_EXC_WORDS = re.compile(r"异常|故障|事故|抛锚|爆胎|堵|延误|晚点|温控|厢温|温度|偏航|绕路|"
                        r"扣车|查扣|泄漏|起火|停运|滞留|拒收|破损|丢货")
_PRECAUTION_WORDS = re.compile(r"注意|留意|小心|要求|资质|证件|手续|能不能拉|能不能运|可不可以运|"
                               r"风险|禁忌|限制")
_DIFF_WORDS = re.compile(r"区别|差别|不同|差异|有什么不一样|哪个|对比")


def _is_question(text: str) -> bool:
    return bool(_Q_MARK.search(text or ""))


# 「有什么货类 / 货类怎么填」——用户在问可选值，不是在报货类。
# 这类问题以前落到 kind=none，于是助手只会把同一句反问再发一遍，
# 用户连续问两次还是同一个问题，最后只能瞎猜一个词。
_OPTION_CARGO = re.compile(
    r"(有什么|有哪些|哪些|什么|几种|都有啥|几个).{0,6}(货类|货物类型|货种|品类|类型)"
    r"|(货类|货种|货物类型|品类).{0,6}(有什么|有哪些|几个|分几种|怎么填|怎么选|能填什么)")

_ALIAS_SET = {a for a, _ in _ALIAS_INDEX}


def _norm(text: str) -> str:
    """去掉空白与标点，只留字面内容（判"整句是不是一个词"时需要）。"""
    return re.sub(r"[\s，。！？、,.!?~:：;；\"'“”（）()]+", "",
                  (text or "").strip()).lower()


def looks_like_plain_answer(text: str) -> bool:
    """整句就是一个货类词 —— 这是在答槽位，不是提问。

    ``"普货"`` 没有问号也没有疑问词，但它是对"运的是什么货"最标准的回答。
    旧逻辑用 ``cargo and not has_q`` 判 ``cargo_care``，把这类回答吃进了知识
    问答：用户看到「「普货」的运输要点：…」，而货类槽位还空着、下一轮又问一遍。
    这不是"识别错意图"那么轻——槽位永远填不上，对话走不下去。
    """
    t = _norm(text)
    if not t or len(t) > 10:
        return False
    # 去掉数量与单位后，剩下的必须"整句就是一个合法货类词"
    core = re.sub(r"\d+(?:\.\d+)?|吨|米|方|元|块|t|m3|kg|公斤", "", t)
    core = re.sub(r"^(货类是|货物是|货是|就|按|用|走|是)", "", core)
    return bool(core) and core in _ALIAS_SET


def cargo_options_text() -> str:
    """「有什么货类」的答案。给选项而不是替用户选一个。"""
    from sca.cargo import options_text

    return options_text(
        "拿不准就先按「普货」报，之后还能改；货物对温度或合规有要求时一定要说清楚，"
        "冷链和危险品会改变可用车辆与合规校验。")


def looks_like_slot_input(text: str) -> bool:
    """像"槽位答案"的输入（起讫地 / 重量），不要被问答分支截走。"""
    return bool(_SLOT_LIKE.match(text or ""))


def classify_question(text: str) -> str:
    """把一句话归到问答意图。返回 none 表示不按问答处理。"""
    t = (text or "").strip()
    if not t or looks_like_slot_input(t):
        return "none"
    # 裸货类词（"普货" / "农产品 2000"）是槽位回答，先让位给槽位解析
    if looks_like_plain_answer(t):
        return "none"
    # 一条完整的下单描述（"20 吨冷链，济南到西昌"）不是问题：
    # 数字吨位 + 起讫关系同时出现，一律交给槽位解析，避免把订单截成问答。
    if re.search(r"\d+(\.\d+)?\s*(吨|t|T|方|m3)", t) and \
            re.search(r"(到|至|发往|运往|起运|目的地)", t):
        return "none"
    has_q = _is_question(t)
    cargo = find_cargo(t)

    if _OPTION_CARGO.search(t):
        return "cargo_options"
    if _EXC_WORDS.search(t):
        return "exception"
    if _FEE_WORDS.search(t):
        return "fee"
    if _DIFF_WORDS.search(t) or (cargo and re.search(r"和|与|跟", t) and
                                 len([1 for a, _ in _ALIAS_INDEX if a in t]) >= 2):
        return "cargo_diff"
    if _STATUS_WORDS.search(t):
        return "status"
    if cargo and (_PRECAUTION_WORDS.search(t) or not has_q):
        return "cargo_care"
    if _PRECAUTION_WORDS.search(t):
        return "cargo_care"
    if _ETA_WORDS.search(t):
        return "eta"
    if _ORDER_WORDS.search(t) and has_q:
        return "order_info"
    if cargo and has_q:
        return "cargo_what"
    return "none"


def _ordered_care(items: List[str], limit: int = 4) -> List[str]:
    return [f"{i + 1}. {x}" for i, x in enumerate(items[:limit])]


def cargo_care_text(cargo_type: str, box: bool = True) -> str:
    meta = cargo_meta(cargo_type)
    name = cargo_type if cargo_type in CARGO_KB else "普货"
    lines = [f"「{name}」的运输要点：", f"- 是什么：{meta['what']}",
             f"- 用车：{meta['vehicle']}", "- 注意事项："]
    lines += ["  " + x for x in _ordered_care(meta["care"])]
    lines.append("- 常见风险：" + "、".join(meta["risks"]))
    body = "\n".join(lines)
    if box:
        body += "\n\n如果这票货有特别约定（温区、时效、装卸条件），以订单和随货文件为准。"
    return body


def cargo_diff_text(a: str, b: str) -> str:
    ma, mb = cargo_meta(a), cargo_meta(b)
    return (f"「{a}」和「{b}」的主要区别：\n"
            f"- 货物性质：{ma['what']}\n"
            f"  vs {mb['what']}\n"
            f"- 用车：{ma['vehicle']}\n"
            f"  vs {mb['vehicle']}\n"
            f"- 关键注意点：{ma['care'][0]}\n"
            f"  vs {mb['care'][0]}\n"
            f"- 主要风险：{'、'.join(ma['risks'])} / {'、'.join(mb['risks'])}\n\n"
            f"要判断这票货归哪类，看三个字段：是否温控、是否危险品、单件是否超限。"
            f"订单里的「冷链/危化」标签优先于货名描述。")


STATUS_MEANING = {
    "draft": "草稿，还没提交给平台。",
    "submitted": "已提交，等调度出方案。此刻还没有报价，承运商也看不到。",
    "planned": "调度已出方案（路线、里程、时效、成本），但还没定价挂牌。",
    "published": "已定价并挂牌，承运商订单池里能看到，等待接单。",
    "candidate": "已有承运商接单进入候选，尚未最终确认。",
    "claimed": "承运商已接单，货权与运输责任转到承运方。",
    "in_transit": "在途运输中，会持续记录位置与异常。",
    "exception": "在途异常，已挂起等调度处置；处置完成会回到在途状态。",
    "delivered": "货物已送达，等待结算。",
    "settled": "已结算，订单闭环。",
    "cancelled": "已取消（终态），已接单的承运商会同时释放。",
    "rejected": "被驳回（终态），常见于审批未通过。",
}


def fee_text(gross: Optional[float], shipper_fee: Optional[float],
             publish: Optional[float], carrier_fee_rate: Optional[float] = None) -> str:
    """解释费用构成。只用传进来的真实数字，不补。"""
    if gross in (None, 0):
        return ("服务费按订单金额比例计，两端各算一次：\n"
                "- 货主侧：预算 − 货主侧服务费 = 挂牌价（承运商看到的就是这个数）\n"
                "- 承运商侧：挂牌价 − 承运商侧服务费 − 干线成本 − 空驶成本 = 承运商收益\n"
                "比例以平台当前生效的服务费规则为准，规则调整只对之后发布的单生效。")
    lines = [f"这单的钱是这么走的（货主预算 ¥{gross:,.2f}）："]
    if shipper_fee is not None:
        lines.append(f"- 扣货主侧服务费 ¥{shipper_fee:,.2f}")
    if publish is not None:
        lines.append(f"- 挂牌价 ¥{publish:,.2f}（承运商看到的就是这个数）")
    lines.append("- 承运商接单后：挂牌价 − 承运商侧服务费 − 干线成本 − 空驶成本 = 承运商收益")
    if carrier_fee_rate is not None:
        lines.append(f"- 承运商侧费率 {carrier_fee_rate * 100:.2f}%（按挂牌价计）")
    lines.append("\n服务费是平台收的，两端各按规则比例计；比例调整只对调整之后发布的单生效。")
    return "\n".join(lines)


def order_brief_text(order: Any, include_budget: bool = True) -> str:
    """订单摘要：只写订单里真的有的字段。"""
    def g(name: str, default: str = "-") -> Any:
        v = getattr(order, name, None)
        return default if v in (None, "") else v

    lines = [f"订单 {g('order_no')}｜{g('origin')} → {g('dest')}",
             f"- 货物：{g('cargo_type')}，{g('weight_t')} 吨"
             + ("，温控" if getattr(order, "temp_ctrl", False) else "")
             + ("，危险品" if getattr(order, "hazmat", False) else ""),
             f"- 要求：{g('required_by')} 前送达",
             f"- 当前状态：{STATUS_MEANING.get(g('status', ''), g('status'))}"]
    if include_budget:
        lines.append(f"- 预算上限：¥{g('budget_max')}")
    return "\n".join(lines)


def answer(text: str, order: Any = None, *, include_budget: bool = True,
           fee_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """问答入口。返回 {kind, text, sop}；kind=none 表示这不是一个问题。"""
    kind = classify_question(text)
    if kind == "none":
        return {"kind": "none", "text": "", "sop": []}

    if kind == "exception":
        return {"kind": kind, "text": "", "sop": _sop_for(text, order)}

    if kind == "cargo_options":
        # 返回 options 供前端渲染成可点的按钮：让人从"猜一个词"变成"点一下"。
        from sca.cargo import CATEGORIES

        return {"kind": kind, "text": cargo_options_text(), "sop": [],
                "options": list(CATEGORIES)}

    if kind == "cargo_diff":
        names: List[str] = []
        for alias, name in _ALIAS_INDEX:
            if alias in text and name not in names:
                names.append(name)
        if len(names) >= 2:
            return {"kind": kind, "text": cargo_diff_text(names[0], names[1]), "sop": []}
        return {"kind": "cargo_care",
                "text": "要区分货类，先看三件事：是否温控、是否危险品、单件是否超限。\n"
                        + cargo_care_text(order.cargo_type if order is not None else "普货"),
                "sop": []}

    if kind == "fee":
        ctx = fee_ctx or {}
        return {"kind": kind, "sop": [],
                "text": fee_text(ctx.get("gross"), ctx.get("shipper_fee"),
                                ctx.get("publish"), ctx.get("carrier_fee_rate"))}

    if kind == "status":
        if order is not None:
            st = getattr(order, "status", "") or ""
            return {"kind": kind, "sop": [],
                    "text": f"这单现在是「{st}」：{STATUS_MEANING.get(st, '状态含义未收录')}\n"
                            "下一步该谁动、能做什么，页面上的操作按钮就是当前允许的动作。"}
        return {"kind": kind, "sop": [],
                "text": "订单状态按顺序走：已提交 → 已出方案 → 已挂牌 → 已接单 → 在途 → 送达 → 结算。\n"
                        "中间可能停在「异常」等待处置，或走到「已取消 / 驳回」终态。"}

    if kind == "eta":
        eta = getattr(order, "eta_h", None) if order is not None else None
        req = getattr(order, "required_by", None) if order is not None else None
        if eta:
            return {"kind": kind, "sop": [],
                    "text": f"按当前方案，预计 {eta} 小时到达，要求 {req or '未填'} 前送达。\n"
                            "实际时效会受路况、装卸和天气影响，在途异常会重新估算。"}
        return {"kind": kind, "sop": [],
                "text": "这单还没有方案，所以还没有预计到达时间。出方案后会给出里程与预计时长。\n"
                        "要求送达时间是 " + str(req or "未填") + "。"}

    if kind in ("cargo_care", "cargo_what"):
        ct = getattr(order, "cargo_type", None) if order is not None else None
        cargo = ct or find_cargo(text) or "普货"
        if find_cargo(text):
            cargo = find_cargo(text) or cargo
        return {"kind": kind, "sop": [], "text": cargo_care_text(cargo)}

    if kind == "order_info":
        if order is None:
            return {"kind": kind, "sop": [],
                    "text": "你还没指定是哪一单。发订单号（SO 开头），或者说「第一单」，我再展开。"}
        return {"kind": kind, "sop": [],
                "text": order_brief_text(order, include_budget=include_budget)}

    return {"kind": "none", "text": "", "sop": []}


def _sop_for(text: str, order: Any) -> List[Dict[str, Any]]:
    """异常类问题走知识库检索；检索不到就返回空，由调用方如实说明。"""
    try:
        from sca.knowledge_base import search_sop
    except Exception:  # noqa: BLE001 知识库不可用时问答仍要能答
        return []
    parts = [text]
    if order is not None:
        parts.append(getattr(order, "cargo_type", "") or "")
    try:
        hits = search_sop(" ".join(p for p in parts if p), top_k=3)
    except Exception:  # noqa: BLE001
        return []
    return [{"sop_id": h.get("sop_id"), "title": h.get("title"), "level": h.get("level"),
             "steps": h.get("steps") or [], "channel": h.get("channel"),
             "owner": h.get("owner")} for h in hits]


def render_sop(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "知识库里没检索到对应的处置记录。这类情况按通用流程走：先保人保货、再上报、再定责。"
    h = hits[0]
    lines = [f"知识库命中「{h.get('title')}」（{h.get('sop_id')}，等级 {h.get('level')}）："]
    for i, st in enumerate((h.get("steps") or [])[:6], 1):
        lines.append(f"{i}. {st}")
    if h.get("owner"):
        lines.append(f"责任方：{h['owner']}")
    if len(hits) > 1:
        lines.append("其它相关：" + "、".join(
            f"{x.get('sop_id')} {x.get('title')}" for x in hits[1:3]))
    return "\n".join(lines)
