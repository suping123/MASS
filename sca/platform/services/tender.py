"""招标规则：报价、硬过滤、打分、裁决、还价。

为什么单独一个文件、且全是纯函数：

* 四个 agent 里只有调度有裁决权，裁决必须可解释（写进 ``tender.arbitration``）、
  可断言。规则里混进 Session 就没法拿假数据把边界测干净。
* 承运商的报价策略、货主的提价建议也在这里——它们是"各方的立场"，
  放在一起才能一眼看出谁和谁在冲突：承运商要 ``cost × (1+利润率)``，
  货主要 ``预算 − 服务费``，调度在两者之间按权重裁决。

金额一律 Decimal，量化口径复用 ``pricing.CENT``，不允许浮点算钱。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from .pricing import CENT

# ---------------------------------------------------------------------------
# 规则常量。改这些等于改业务口径，必须同步改文档与测试。
# ---------------------------------------------------------------------------
RULE_VERSION = "v1"

# 四因子权重。价格是主因子但不是唯一：只看谁便宜会选到评分 2.1 的承运商。
WEIGHTS_DEFAULT: Dict[str, float] = {
    "price": 0.45, "eta": 0.20, "rating": 0.20, "deadhead": 0.15,
}

# 超预算多少以内值得谈。再高就不是"让一让"，是这单预算本身错了。
NEGOTIATE_MAX_RATIO = Decimal("0.15")

MAX_ROUNDS = 3                 # 协商轮次上限，超过即流标交人
EXTEND_MINUTES = 5             # 零投标时延期时长
DEADHEAD_MAX_KM = Decimal("800")
MIN_PROFIT_RATE = Decimal("0.05")   # 承运商成本底线之上的最低利润率
TARGET_PROFIT_RATE = Decimal("0.08")  # 默认目标利润率（承运商档案可覆盖）
SHIPPER_RAISE_CAP = Decimal("0.10")   # 货主最多接受提价 10%（仅为建议上限）

# ---------------------------------------------------------------------------
# 目标函数 -> 决策参数
#
# 「每个 agent 有自己的目标」这句话如果不落到数字上，`agent_actor.utility`
# 就只是一张存了权重的表，没有代码读它——那么四个 agent 的"目标不同"就只是
# 文档里的说法。下面两个换算把它变成可测的东西：改 utility，行为就变。
#
# 两个默认值都刻意复现改造前的口径（8% / 15%），这样接上目标函数不会
# 悄悄改变历史单子的结果；改权重才会。
# ---------------------------------------------------------------------------
PROFIT_BASE = Decimal("0.05")        # 利润率下限
PROFIT_SPAN = Decimal("0.05")        # profit 权重 0..1 映射到 +0..5%
DEFAULT_PROFIT_W = Decimal("0.6")    # 承运商默认 utility.profit

NEGOTIATE_RATIO_BASE = Decimal("0.05")
NEGOTIATE_RATIO_SPAN = Decimal("0.25")   # fulfilment 0..1 映射到 +0..25%
DEFAULT_FULFILMENT_W = Decimal("0.4")    # 调度默认 utility.fulfilment


def _unit(v: Any, default: Decimal) -> Decimal:
    try:
        d = Decimal(str(v if v is not None else default))
    except (TypeError, ValueError, ArithmeticError):
        return default
    return min(max(d, Decimal("0")), Decimal("1"))


def profit_rate_from(utility: Optional[Dict[str, Any]] = None) -> Decimal:
    """承运商报价利润率 ← 它自己的 ``utility.profit``。

    更想赚钱的承运商报得更高，这不是"参数可调"，是它的立场：
    平台无权替承运商决定"便宜点卖"。默认权重 0.6 → 8%，与改造前一致。
    """
    u = utility or {}
    return PROFIT_BASE + PROFIT_SPAN * _unit(u.get("profit"), DEFAULT_PROFIT_W)


def negotiate_ratio_from(utility: Optional[Dict[str, Any]] = None) -> Decimal:
    """调度愿意把"超预算多少以内"的单子拉进谈判 ← 它自己的 ``utility.fulfilment``。

    越看重履约率，越不愿意直接判流标——宁可多谈一轮。默认 0.4 → 15%。
    """
    u = utility or {}
    return NEGOTIATE_RATIO_BASE + NEGOTIATE_RATIO_SPAN * _unit(
        u.get("fulfilment"), DEFAULT_FULFILMENT_W)

REASON_LABEL = {
    "over_budget": "报价超预算",
    "eta_miss": "承诺时效超时限",
    "capacity": "载重不足",
    "temp": "无温控资质",
    "hazmat": "无危品资质",
    "deadhead": "空驶距离过远",
}


def _q(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(v: Any, default: str = "0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    return Decimal(str(v))


def _pct(v: Decimal) -> str:
    return f"{float(v) * 100:.2f}%"


# ---------------------------------------------------------------------------
# 承运商：我该报多少钱
# ---------------------------------------------------------------------------
def carrier_quote(cost_total: Any, *,
                  target_profit_rate: Any = TARGET_PROFIT_RATE,
                  min_profit_rate: Any = MIN_PROFIT_RATE,
                  budget: Any = None) -> Dict[str, Any]:
    """承运商报价 = 成本 × (1 + 目标利润率)，且不超过预算。

    两个边界要在这里挡住，否则会产出荒唐的报价：

    * 利润率低于成本底线 → 抬到 ``min_profit_rate``。报一个亏本价接单，
      不是"有竞争力"，是把亏损留到运输途中爆出来（拒单、扣货、甩挂）。
    * 成本加成后超过预算 → 顶格报预算（``capped=True``），不弃标。
      顶格是真实的商务行为；弃标只应发生在成本本身高于预算时。

    返回 ``quote`` / ``floor`` / ``capped`` / ``below_cost``（成本高于预算，
    应当弃标）。
    """
    cost = _q(cost_total)
    target = max(_dec(target_profit_rate, str(TARGET_PROFIT_RATE)),
                 _dec(min_profit_rate, str(MIN_PROFIT_RATE)))
    raw = _q(cost * (Decimal("1") + target))
    floor = _q(cost * (Decimal("1") + _dec(min_profit_rate, str(MIN_PROFIT_RATE))))

    capped = False
    quote = raw
    below_cost = False
    if budget is not None and _dec(budget) > 0:
        b = _q(budget)
        if raw > b:
            quote, capped = b, True
        if floor > b:
            below_cost = True
    return {"quote": quote, "raw": raw, "floor": floor,
            "capped": capped, "below_cost": below_cost,
            "profit_rate": target}


# ---------------------------------------------------------------------------
# 硬过滤：不合格的投标不进打分
# ---------------------------------------------------------------------------
def hard_filter(bid: Dict[str, Any], req: Dict[str, Any]) -> Tuple[bool, str, str]:
    """返回 ``(是否合规, 原因码, 人话说明)``。

    顺序是刻意的：先看钱，再看时效，最后看资质。原因码要稳定，
    因为 ``decide()`` 靠 ``over_budget`` 判断"值不值得还价"——
    资质不符的便宜报价不该触发协商。
    """
    quote = _q(bid.get("quote") or 0)
    budget = _q(req.get("budget") or 0)
    if budget > 0 and quote > budget:
        over = (quote - budget) / budget
        return False, "over_budget", f"报价 ¥{quote} 超预算 ¥{budget}（+{_pct(over)}）"

    need_eta = req.get("required_eta_h")
    eta_h = _dec(bid.get("eta_h"))
    if need_eta not in (None, "", 0) and eta_h > _dec(need_eta):
        return False, "eta_miss", f"承诺 {eta_h}h 超过时限 {_dec(need_eta)}h"

    capacity = _dec(bid.get("capacity_t"))
    weight = _dec(req.get("weight_t"))
    if weight > 0 and capacity < weight:
        return False, "capacity", f"载重 {capacity}t 不足 {weight}t"

    if req.get("need_temp") and not bid.get("temp_ctrl"):
        return False, "temp", "冷链货需要温控资质，该承运商没有"
    if req.get("need_haz") and not bid.get("hazmat"):
        return False, "hazmat", "危化货需要危品资质，该承运商没有"

    deadhead = _dec(bid.get("deadhead_km"))
    limit = _dec(req.get("deadhead_max_km"), str(DEADHEAD_MAX_KM))
    if deadhead > limit:
        return False, "deadhead", f"空驶 {deadhead}km 超过上限 {limit}km"

    return True, "", ""


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def score_bid(bid: Dict[str, Any], req: Dict[str, Any],
              weights: Optional[Dict[str, float]] = None, *,
              fastest_eta: Any = None) -> Tuple[float, Dict[str, float]]:
    """四因子加权。返回 ``(总分, 各因子分)``，因子分都归一到 [0,1]。

    ``fastest_eta`` 是本批次里最快的承诺时效，用来给时效因子做相对基准。
    不传它就退化成"满足时限即满分"——那样合规投标的 f_eta 恒为 1，
    0.2 的权重等于白给，排序实际只看价格和评分。
    """
    w = dict(weights or WEIGHTS_DEFAULT)
    total_w = sum(w.values()) or 1.0
    # 权重被改坏（合计不是 1）时归一化而不是抛错：配置错误不该让裁决停摆，
    # 但归一化的痕迹会随 arbitration 一起落库，事后能看出来。
    w = {k: v / total_w for k, v in w.items()}

    budget = _q(req.get("budget") or 0)
    quote = _q(bid.get("quote") or 0)
    f_price = float(1 - (quote / budget)) if budget > 0 else 0.0
    f_price = max(0.0, min(1.0, f_price))

    need_eta = req.get("required_eta_h")
    eta_h = _dec(bid.get("eta_h"), "0")
    if need_eta not in (None, "", 0) and eta_h > 0:
        # 以"本批次最快承诺"为基准，不设绝对奖励：满足时限的都合格，
        # 但要能区分 20h 和 24h 的差别，否则时效权重形同虚设。
        ref = _dec(need_eta)
        if fastest_eta not in (None, "", 0):
            ref = min(ref, _dec(fastest_eta))
        f_eta = min(float(ref / eta_h), 1.0)
    else:
        f_eta = 0.5   # 没有时限要求时给中性分，并在 factors 里留有据可查

    rating = _dec(bid.get("rating"), "5")
    f_rating = max(0.0, min(1.0, float(rating / Decimal("5"))))

    limit = _dec(req.get("deadhead_max_km"), str(DEADHEAD_MAX_KM))
    deadhead = _dec(bid.get("deadhead_km"))
    f_deadhead = 1 - float(deadhead / limit) if limit > 0 else 0.0
    f_deadhead = max(0.0, min(1.0, f_deadhead))

    factors = {"price": round(f_price, 4), "eta": round(f_eta, 4),
               "rating": round(f_rating, 4), "deadhead": round(f_deadhead, 4)}
    score = (w["price"] * f_price + w["eta"] * f_eta +
             w["rating"] * f_rating + w["deadhead"] * f_deadhead)
    return round(score, 4), factors


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------
def decide(bids: List[Dict[str, Any]], req: Dict[str, Any], *,
           weights: Optional[Dict[str, float]] = None,
           extended: bool = False,
           max_rounds: int = MAX_ROUNDS,
           negotiate_max_ratio: Any = NEGOTIATE_MAX_RATIO) -> Dict[str, Any]:
    """收标截止后的裁决。只读，不改任何状态。

    返回的 ``status`` 有四种，调用方按它决定下一步：

    * ``awarded``      —— 有合规投标，``winner`` 是分数最高的那家
    * ``negotiating``  —— 没有合规投标，但有报价超预算且在 15% 以内，
                          ``target`` 是最低报价那家，可以还价
    * ``extended``     —— 零投标且还没延期过，建议延 ``EXTEND_MINUTES`` 分钟
    * ``failed``       —— 流标，交人
    """
    w = dict(weights or WEIGHTS_DEFAULT)
    rows: List[Dict[str, Any]] = []
    eligible: List[Dict[str, Any]] = []
    over_budget: List[Dict[str, Any]] = []

    for b in bids:
        ok, code, msg = hard_filter(b, req)
        row = dict(b)
        row["verdict"] = "eligible" if ok else "rejected"
        row["reason"] = msg
        row["reason_code"] = code
        rows.append(row)
        if ok:
            eligible.append(row)
        elif code == "over_budget":
            over_budget.append(row)

    base = {"rule_version": RULE_VERSION, "weights": w, "candidates": rows}

    if not bids:
        if not extended:
            return {**base, "status": "extended", "winner": None, "target": None,
                    "reason": f"零投标，建议延期 {EXTEND_MINUTES} 分钟"}
        return {**base, "status": "failed", "winner": None, "target": None,
                "reason": "延期后仍零投标，交人工处理"}

    if eligible:
        # 先取本批次最快承诺，再打分——时效因子要相对可比，见 score_bid 说明
        fastest = min((_dec(r.get("eta_h")) for r in eligible
                       if _dec(r.get("eta_h")) > 0), default=None)
        for row in eligible:
            row["score"], row["factors"] = score_bid(row, req, w, fastest_eta=fastest)
        # 分数相同时按报价低者优先，再按承运商 id 稳定排序——
        # 不能让 dict 顺序决定谁中标。
        winner = sorted(eligible, key=lambda r: (-r["score"],
                                                 _dec(r.get("quote")),
                                                 int(r.get("carrier_org_id") or 0)))[0]
        winner["verdict"] = "winner"
        return {**base, "status": "awarded", "winner": winner, "target": None,
                "reason": f"{winner.get('carrier_org_id')} 综合分 {winner['score']:.4f} 最高"}

    if over_budget:
        low = min(over_budget, key=lambda r: (_dec(r.get("quote")),
                                              int(r.get("carrier_org_id") or 0)))
        budget = _q(req.get("budget") or 0)
        ratio = ((_dec(low.get("quote")) - budget) / budget) if budget > 0 else Decimal("1")
        cap = _dec(negotiate_max_ratio, str(NEGOTIATE_MAX_RATIO))
        if ratio <= cap:
            return {**base, "status": "negotiating", "winner": None, "target": low,
                    "over_ratio": ratio, "negotiate_max_ratio": float(cap),
                    "reason": f"最低报价超预算 {_pct(ratio)}，在 {_pct(cap)} 以内，可还价"}
        return {**base, "status": "failed", "winner": None, "target": None,
                "over_ratio": ratio, "negotiate_max_ratio": float(cap),
                "reason": f"最低报价超预算 {_pct(ratio)}，超过可谈上限 {_pct(cap)}，交人工"}

    codes = sorted({r["reason_code"] for r in rows if r["reason_code"]})
    why = "、".join(REASON_LABEL.get(c, c) for c in codes) or "无可用运力"
    return {**base, "status": "failed", "winner": None, "target": None,
            "reason": f"没有承运商满足条件（{why}）"}


# ---------------------------------------------------------------------------
# 协商：双方各自的立场
# ---------------------------------------------------------------------------
def carrier_reply(*, counter_amount: Any, floor_price: Any, own_quote: Any,
                  round_no: int, max_rounds: int = MAX_ROUNDS) -> Dict[str, Any]:
    """承运商收到还价后的确定性策略。

    * 还价高于成本底线 → 接受
    * 已到末轮且仍低于底线 → 拒绝
    * 自己上一轮已经报到成本线 → 无法再让，拒绝
    * 否则让到成本线（必须低于自己上轮报价，不退让的回价没有意义）
    """
    counter = _q(counter_amount)
    floor = _q(floor_price)
    own = _q(own_quote)

    if counter >= floor:
        return {"action": "accept", "amount": counter, "reason": "还价高于成本底线"}
    if round_no >= max_rounds:
        return {"action": "reject", "amount": None,
                "reason": f"已到第 {max_rounds} 轮上限，还价 ¥{counter} 仍低于成本线 ¥{floor}"}
    if floor >= own:
        return {"action": "reject", "amount": None,
                "reason": f"上轮已报 ¥{own}，已是成本线，无法再让"}
    return {"action": "counter", "amount": floor,
            "reason": f"最多让到成本线 ¥{floor}"}


def dispatch_reply(*, offer: Any, counter: Any, round_no: int,
                   max_rounds: int = MAX_ROUNDS) -> Dict[str, Any]:
    """调度收到承运商回价后的确定性策略。

    调度的出价上限就是 ``Tender.budget``（= 挂牌价 = 扣完货主侧服务费后
    平台能付出去的钱）。它没有"再加一点"这个动作——加价等于改
    ``budget_max``，那是 ``HUMAN_REQUIRED.price_increase``。

    所以真实局面只有三种：

    * 承运商回价 ≤ 预算 → 成交（它让步了）
    * 承运商回价 > 预算，且这是第一次还价 → 再报一次预算价（重申上限）
    * 回价仍高于预算，或轮次用尽 → 无交集，交人

    第三条里"无交集"是数学结论不是态度：调度已报到上限、承运商已报到成本线，
    两个区间不相交，再谈多少轮都不会相交。这时候继续往复就是演给对方看。
    """
    off = _q(offer)
    cnt = _q(counter)
    r = max(1, int(round_no or 1))

    if cnt <= off:
        return {"action": "accept", "amount": cnt, "round": r,
                "reason": f"回价 ¥{cnt} 未超过可付上限 ¥{off}，成交"}
    if r >= max_rounds:
        return {"action": "fail", "amount": None, "round": r,
                "reason": f"已到第 {max_rounds} 轮上限，回价 ¥{cnt} 仍高于上限 ¥{off}"}
    if r >= 2:
        return {"action": "fail", "amount": None, "round": r,
                "reason": f"上一轮已报到上限 ¥{off}，无新筹码可让，回价 ¥{cnt} 无交集"}
    return {"action": "counter", "amount": off, "round": r + 1,
            "reason": f"重申可付上限 ¥{off}"}


def shipper_advice(*, gap: Any, budget: Any,
                   cap_rate: Any = SHIPPER_RAISE_CAP) -> Dict[str, Any]:
    """货主对"提价"的建议。**只是建议，实际改预算属人审项。**

    改 ``budget_max`` 会连带改变挂牌价与两侧服务费，属
    ``contract.HUMAN_REQUIRED`` 的 ``price_increase``，agent 不得自动执行。
    """
    g = _q(gap)
    b = _q(budget)
    ratio = (g / b) if b > 0 else Decimal("1")
    cap = _dec(cap_rate, str(SHIPPER_RAISE_CAP))
    if ratio <= cap:
        return {"advice": "accept", "gap": g, "ratio": ratio,
                "reason": f"需补 ¥{g}（{_pct(ratio)}），在可接受上限 {_pct(cap)} 内"}
    return {"advice": "reject", "gap": g, "ratio": ratio,
            "reason": f"需补 ¥{g}（{_pct(ratio)}），超过上限 {_pct(cap)}，建议改期或拆单"}
