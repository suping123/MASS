"""SOP 知识库（FR-10）：真正可检索、有内容的知识底座。

设计：
  * 以故障谱（sca/simulation/fault_spectrum.py）为唯一事实来源，为每个 sop_id
    生成一份结构化 SOP 文档（标题 / 等级 / 触发异常 / 分步处置 / 责任方 / 工具 / 通知）。
  * 提供两种消费形态：
      - list_sops()  -> 结构化文档列表，供前端"知识库"视图浏览；
      - build_chunks()-> 检索块（doc/content/metadata），供 RAG 检索与种子脚本入库；
      - search_sop() -> 关键词 + 伪向量融合检索（离线可跑），返回带匹配度的 SOP。
  * 文档正文由"类目处置模板 + 等级升级策略"合成，保证 33 个 sop_id 全部有内容，
    不再是 4 条占位文本。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Tuple

from sca.rag.hybrid import Chunk, MockHybridRetriever
from sca.simulation.fault_spectrum import FAULT_SPECTRUM

# ---------------------------------------------------------------------------
# 自定义 SOP 持久化（知识库更新，FR-10 运维闭环）
#   内置 SOP 由故障谱生成（只读）；调度员/工程师新增或修改的 SOP 落盘到
#   data/custom_sops.json，同名 sop_id 覆盖内置（标记 source=custom/overridden）。
# ---------------------------------------------------------------------------
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "custom_sops.json"
)
_CUSTOM: Dict[str, Dict[str, Any]] | None = None

# ---------------------------------------------------------------------------
# 类目处置模板：按 SOP 类目给出 P0(高危审批)/P1(重规划+确认)/P2P3(自动) 的处置步骤
# {steps} 为类目通用动作；首步会注入具体异常名。
# ---------------------------------------------------------------------------
_CATEGORY: Dict[str, Dict[str, Any]] = {
    "COLD": {
        "name": "冷链温控",
        "owner": "冷链工程师 / 调度主管",
        "tools": ["gps-mcp.get_telemetry", "capacity-mcp.match_capacity", "notify-mcp.send_dingtalk"],
        "notify": "收货方 + 冷链值班",
        "p0": ["立即冻结该运单自动调度，置为【待审批】", "调取近15分钟厢温/制冷机组遥测，定位失效范围",
               "发起 HITL 人工审批，通知调度主管与冷链工程师", "匹配30km内备用冷链车/移动冷源，生成转运方案",
               "审批通过后下发换车/换路指令，同步收货方预计影响", "全程留痕(计划版本递增)，复盘沉淀知识库"],
        "p1": ["标记温度漂移趋势，评估剩余可接受时长", "重规划优先路径缩短在途时间", "通知值班工程师远程复核制冷参数",
               "人工确认后继续执行，必要时就近补冷"],
        "p2": ["检测厢门/密封状态并远程提醒司机", "自动记录事件并下发处置建议", "纳入到达前复检清单"],
    },
    "DG": {
        "name": "危险品",
        "owner": "危险品专员 / 安全应急",
        "tools": ["gps-mcp.get_latest_position", "notify-mcp.send_dingtalk", "tms-mcp.update_status"],
        "notify": "安全应急 + 监管报备",
        "p0": ["立即就地安全停车并划定隔离区", "触发应急审批，上报安全应急与监管", "调取货物 MSDS 与随船单证",
               "调度专业处置力量与备用危品车辆", "审批后按预案转运/封存，全程双人复核", "形成事故报告并沉淀"],
        "p1": ["评估包装/温控异常等级，远程指导现场加固", "重规划至最近可处理站点", "通知危险品专员现场确认",
               "人工确认后继续或转运"],
        "p2": ["核对危险品标签/随附单证完整性", "自动下发整改建议并记录", "纳入下一节点复检"],
    },
    "REROUTE": {
        "name": "交通改道",
        "owner": "调度员",
        "tools": ["map-mcp.plan_route", "map-mcp.estimate_eta", "gps-mcp.replay_track", "notify-mcp.send_dingtalk"],
        "notify": "司机 + 收货方",
        "p1": ["确认封路/事故范围与预计持续时长", "调用路径规划生成2条备选路线并比较ETA/成本",
               "推送司机确认新路线(人工确认)", "更新运单ETA并通知收货方"],
        "p2": ["检测拥堵/施工路段，自动重规划绕行", "保留计划版本(vN→vN+1)，下发新路线", "同步ETA变化给收货方"],
    },
    "WX": {
        "name": "天气气象",
        "owner": "调度主管",
        "tools": ["weather-mcp.get_warning", "map-mcp.plan_route", "notify-mcp.send_dingtalk"],
        "notify": "司机 + 调度主管",
        "p0": ["台风/红色预警：暂停相关线路发运", "触发应急审批，评估在途车辆就近避险", "重新排定发运计划",
               "审批后统一下发，通知全部受影响方"],
        "p1": ["暴雪/强降雨前重规划途经路线", "评估时效影响并通知值班", "人工确认后执行，必要时压车等待"],
        "p2": ["接收天气预警，自动评估途经影响", "必要时微调路线/出发时刻", "推送司机天气提示"],
    },
    "CAP": {
        "name": "运力资源",
        "owner": "运力调度",
        "tools": ["capacity-mcp.match_capacity", "capacity-mcp.rank_carriers", "tms-mcp.update_status"],
        "notify": "承运商 + 调度",
        "p0": ["承运商违约/重大缺口：启动应急审批", "启用备用承运商池重新竞价", "评估改约/拆分方案",
               "审批后重新派单并追责记录"],
        "p1": ["车辆故障/司机失联：就近匹配备用运力 Top-N", "重规划接驳方案并人工确认", "更新运单状态并通知收货方"],
        "p2": ["临时运力缺口/装载超限：自动重匹配运力", "按评分排序选最优承运", "记录并跟踪履约"],
    },
    "DEV": {
        "name": "偏航纠正",
        "owner": "调度员",
        "tools": ["gps-mcp.replay_track", "map-mcp.plan_route", "notify-mcp.send_dingtalk"],
        "notify": "司机",
        "p1": ["比对实际轨迹与规划路线，确认偏航里程", "联系司机核实原因(导航/加油/异常)", "重规划纠偏路线并人工确认",
               "更新ETA并留痕"],
    },
    "STOP": {
        "name": "停留超时",
        "owner": "调度员",
        "tools": ["gps-mcp.get_latest_position", "notify-mcp.send_dingtalk"],
        "notify": "司机",
        "p2": ["检测异常停留(超时阈值)", "自动外呼/推送司机确认状况", "记录事件，必要时上报"],
    },
    "SIGN": {
        "name": "签收异常",
        "owner": "客服 / 调度",
        "tools": ["tms-mcp.update_status", "notify-mcp.send_email"],
        "notify": "客户 + 客服",
        "p2": ["登记拒签/漏签原因与凭证", "通知客服介入协调", "更新运单签收状态并跟进"],
    },
    "TMS": {
        "name": "运输执行",
        "owner": "TMS 运维 / 调度",
        "tools": ["tms-mcp.query_order", "tms-mcp.update_status"],
        "notify": "调度",
        "p1": ["运单丢失/信息异常：锁定相关单证", "人工核查并重建运单关联", "确认后恢复执行"],
        "p3": ["检测重复派单/信息录入错误", "自动合并/标记并提醒复核", "记录防再发"],
    },
    "CUS": {
        "name": "海关口岸",
        "owner": "关务专员",
        "tools": ["tms-mcp.query_order", "notify-mcp.send_email"],
        "notify": "关务 + 客户",
        "p1": ["海关查验扣留：调取单证与货物信息", "关务专员跟进查验进度", "评估时效影响并重排后续段", "人工确认后放行续运"],
        "p2": ["单证不符：核对并补正单据", "自动提醒关务复核", "记录避免重复出错"],
        "p3": ["税费争议：登记异议并转关务", "跟踪处理进度"],
    },
    "SYS": {
        "name": "系统 / IoT",
        "owner": "系统运维",
        "tools": ["gps-mcp.get_telemetry", "notify-mcp.send_dingtalk"],
        "notify": "运维值班",
        "p2": ["GPS失联/定位异常：切换备用定位源", "自动降级为基站/惯性估算", "记录并跟踪恢复"],
        "p3": ["接口超时/数据缺失：自动重试与补偿", "标记降级运行并提醒运维", "记录可用性指标"],
    },
    "CUST": {
        "name": "客户变更",
        "owner": "客服 / 调度",
        "tools": ["tms-mcp.update_status", "map-mcp.plan_route", "notify-mcp.send_email"],
        "notify": "客户 + 司机",
        "p1": ["客户拒收：登记原因与凭证", "评估返程/转运/暂存方案", "人工确认后执行并核算费用"],
        "p2": ["客户改址/加急：校验可行性", "自动重规划配送路径", "更新运单并通知司机客户"],
    },
    "SLA": {
        "name": "履约时效",
        "owner": "履约经理",
        "tools": ["map-mcp.estimate_eta", "notify-mcp.send_email"],
        "notify": "客户 + 履约经理",
        "p0": ["货损/重大履约事故：启动应急审批与定损", "封存证据并通知保险/客户", "审批后按赔付流程处理", "复盘并改进"],
        "p1": ["时效违约风险：评估剩余里程与ETA", "重规划加急方案并人工确认", "通知客户并协商"],
        "p3": ["轻微延误：自动记录并预警", "纳入履约看板跟踪"],
    },
    "WMS": {
        "name": "仓储作业",
        "owner": "仓储主管",
        "tools": ["tms-mcp.query_order", "notify-mcp.send_dingtalk"],
        "notify": "仓储 + 调度",
        "p1": ["库存账实不符：冻结相关批次", "人工盘点核查差异", "确认后调整并恢复出库"],
        "p2": ["出库异常/延误：自动调整波次", "通知仓储优先处理", "同步调度装车时刻"],
    },
    "OMS": {
        "name": "订单管理",
        "owner": "OMS 运营",
        "tools": ["tms-mcp.query_order", "tms-mcp.update_status"],
        "notify": "客户 + 调度",
        "p2": ["订单取消/变更：校验在途状态", "自动拦截或改派", "更新状态并通知相关方"],
        "p3": ["下单信息错误：自动校验并提示", "转人工修正后重发"],
    },
}


def _category_of(sop_id: str) -> str:
    # SOP-P0-COLD / SOP-P2-REROUTE / SOP-P0-DMG -> 类目键
    parts = sop_id.split("-")
    tail = parts[-1] if parts else ""
    return {"DMG": "SLA", "DRIFT": "COLD", "DOOR": "COLD"}.get(tail, tail if tail in _CATEGORY else "SYS")


def _level_key(level: str) -> str:
    return {"P0": "p0", "P1": "p1", "P2": "p2", "P3": "p3"}.get(level, "p2")


def _build_doc(sop_id: str) -> Dict[str, Any]:
    """由故障谱 + 类目模板合成一份结构化 SOP 文档。"""
    faults = [f for f in FAULT_SPECTRUM if f.sop_id == sop_id]
    if not faults:
        return {}
    level = faults[0].level
    cat_key = _category_of(sop_id)
    cat = _CATEGORY.get(cat_key, _CATEGORY["SYS"])
    lk = _level_key(level)
    steps = list(cat.get(lk) or cat.get("p2") or [])
    # P3 复用 P2 自动处置模板（属自动自愈类）
    if lk == "p3":
        steps = list(cat.get("p3") or cat.get("p2") or [])
    fault_names = "、".join(sorted({f.name for f in faults}))
    first = f"识别到【{fault_names}】异常（{level}），定位受影响运单与货物"
    full_steps = [first] + steps
    return {
        "sop_id": sop_id,
        "title": f"{cat['name']}异常处置（{level}）",
        "category": cat["name"],
        "level": level,
        "trigger_faults": [{"code": f.code, "name": f.name, "desc": f.desc} for f in faults],
        "needs_approval": faults[0].needs_approval,
        "auto": faults[0].auto,
        "owner": cat["owner"],
        "tools": cat["tools"],
        "notify": cat["notify"],
        "steps": full_steps,
    }


# 缓存：模块级构建一次
_CACHE: Dict[str, Dict[str, Any]] = {}


def _ensure() -> None:
    if _CACHE:
        return
    seen = []
    for f in FAULT_SPECTRUM:
        if f.sop_id not in seen:
            seen.append(f.sop_id)
    for sid in seen:
        doc = _build_doc(sid)
        if doc:
            doc["source"] = "builtin"
            _CACHE[sid] = doc


# ---------------------------------------------------------------------------
# 自定义 SOP 读写
# ---------------------------------------------------------------------------
def _load_custom() -> Dict[str, Dict[str, Any]]:
    global _CUSTOM
    if _CUSTOM is not None:
        return _CUSTOM
    data: Dict[str, Dict[str, Any]] = {}
    try:
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:  # 存储损坏不应影响主流程
        data = {}
    _CUSTOM = data
    return _CUSTOM


def _save_custom() -> None:
    data = _load_custom()
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _invalidate() -> None:
    """自定义 SOP 变更后重建检索索引（内存版需重建）。"""
    global _RETRIEVER
    _RETRIEVER = None


def _merge_custom() -> List[Dict[str, Any]]:
    """内置 SOP 叠加自定义覆盖，返回合并后的有序列表。"""
    _ensure()
    docs: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _CACHE.items()}
    for sid, cd in _load_custom().items():
        merged = dict(cd)
        merged["source"] = "custom" if sid not in _CACHE else "overridden"
        merged["sop_id"] = sid
        docs[sid] = merged
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return sorted(docs.values(), key=lambda d: (order.get(d.get("level"), 9), d["sop_id"]))


def list_sops() -> List[Dict[str, Any]]:
    """全部 SOP 文档（内置 + 自定义覆盖，按等级排序），供前端知识库视图浏览。"""
    return _merge_custom()


def get_sop(sop_id: str) -> Dict[str, Any]:
    for d in _merge_custom():
        if d["sop_id"] == sop_id:
            return d
    return {}


def _norm_steps(steps: Any) -> List[str]:
    """步骤归一化：支持数组或换行/分号分隔文本。"""
    if isinstance(steps, list):
        return [str(s).strip() for s in steps if str(s).strip()]
    text = str(steps or "")
    for sep in ("\r\n", "；", ";"):
        text = text.replace(sep, "\n")
    return [ln.strip(" 　·-—") for ln in text.split("\n") if ln.strip(" 　·-—")]


def _norm_tags(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    text = str(v or "")
    for sep in ("、", "，", ",", ";", "；"):
        text = text.replace(sep, " ")
    return [x for x in text.split() if x]


def _auto_sop_id(level: str, title: str) -> str:
    base = f"SOP-{level}-CUSTOM"
    data = _load_custom()
    i = 1
    while f"{base}{i}" in data or f"{base}{i}" in _CACHE:
        i += 1
    return f"{base}{i}"


def upsert_sop(payload: Dict[str, Any]) -> Dict[str, Any]:
    """新增/更新一份 SOP（知识库更新）。

    必填：title、level(P0-P3)、steps；可选：sop_id（不传自动生成）、category、
    owner、tools、notify、trigger_faults、auto。
    同名 sop_id 覆盖内置（前端可"恢复内置"撤销覆盖）。
    """
    title = str(payload.get("title", "") or "").strip()
    level = str(payload.get("level", "") or "").strip().upper()
    steps = _norm_steps(payload.get("steps"))
    if not title:
        return {"ok": False, "error": "标题不能为空"}
    if level not in ("P0", "P1", "P2", "P3"):
        return {"ok": False, "error": "等级须为 P0/P1/P2/P3"}
    if not steps:
        return {"ok": False, "error": "处置步骤不能为空（每行一步）"}

    sop_id = str(payload.get("sop_id", "") or "").strip() or _auto_sop_id(level, title)
    faults = _norm_tags(payload.get("trigger_faults"))
    doc = {
        "sop_id": sop_id,
        "title": title,
        "category": str(payload.get("category", "") or "自定义").strip() or "自定义",
        "level": level,
        "needs_approval": level in ("P0", "P1"),
        "auto": bool(payload.get("auto", level in ("P2", "P3"))),
        "owner": str(payload.get("owner", "") or "调度员").strip() or "调度员",
        "tools": _norm_tags(payload.get("tools")),
        "notify": str(payload.get("notify", "") or "").strip(),
        "steps": steps,
        "trigger_faults": [{"code": "", "name": n, "desc": ""} for n in faults],
        "updated_at": int(time.time()),
    }
    data = _load_custom()
    data[sop_id] = doc
    _save_custom()
    _invalidate()
    builtin = sop_id in _CACHE
    return {"ok": True, "sop_id": sop_id, "sop": doc,
            "mode": "overridden" if builtin else "created"}


def delete_sop(sop_id: str) -> Dict[str, Any]:
    """删除自定义 SOP；对内置 SOP 则是撤销覆盖（恢复内置版本）。"""
    if not sop_id:
        return {"ok": False, "error": "sop_id 不能为空"}
    data = _load_custom()
    if sop_id not in data:
        if sop_id in _CACHE:
            return {"ok": False, "error": "内置 SOP 未被修改，无需删除"}
        return {"ok": False, "error": f"SOP {sop_id} 不存在"}
    data.pop(sop_id)
    _save_custom()
    _invalidate()
    return {"ok": True, "sop_id": sop_id,
            "mode": "reverted" if sop_id in _CACHE else "deleted"}


def rebuild() -> Dict[str, Any]:
    """重建知识库：内置 SOP 由故障谱重新生成 + 重建检索索引（自定义条目保留）。"""
    global _CACHE
    _CACHE = {}
    _ensure()
    _invalidate()
    _retriever()  # 立即重建索引，确保检索可用
    custom = _load_custom()
    return {"ok": True, "builtin": len(_CACHE), "custom": len(custom),
            "total": len(_merge_custom())}


def _doc_text(doc: Dict[str, Any]) -> str:
    faults = "、".join(f["name"] for f in doc["trigger_faults"])
    return (f"{doc['title']}。触发异常：{faults}。等级 {doc['level']}。"
            f"处置步骤：" + "；".join(doc["steps"]) +
            f"。责任方：{doc['owner']}。通知：{doc['notify']}。")


def build_chunks() -> List[Chunk]:
    """检索块：供 RAG 检索与种子脚本入库（含自定义 SOP）。"""
    return [
        Chunk(doc_id=d["sop_id"], content=_doc_text(d),
              metadata={"sop_id": d["sop_id"], "level": d["level"],
                        "category": d["category"], "title": d["title"]})
        for d in _merge_custom()
    ]


# 模块级检索器（懒构建，内存版，离线可跑）
_RETRIEVER: MockHybridRetriever | None = None


def _retriever() -> MockHybridRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        r = MockHybridRetriever()
        r.add(build_chunks())
        _RETRIEVER = r
    return _RETRIEVER


def search_sop(query: str, top_k: int = 3, code: str = "") -> List[Dict[str, Any]]:
    """检索 SOP：先按异常码精确命中，再按查询文本语义/关键词召回补充。

    检索通道（按优先级）：
      1) PG + pgvector 真实向量混合检索（use_pg 且已入库且 embedding 可用）；
      2) 内存关键词检索（内置 2-gram + 包含匹配，离线兜底）。
    返回 [{sop_id,title,level,steps,owner,tools,notify,match,exact,channel}]，match 0~1。
    """
    docs = {d["sop_id"]: d for d in _merge_custom()}
    results: List[Dict[str, Any]] = []
    seen = set()
    # 1) 异常码精确命中（确定性最强）
    if code:
        for f in FAULT_SPECTRUM:
            if f.code == code and f.sop_id in docs:
                d = dict(docs[f.sop_id]); d["match"] = 1.0; d["exact"] = True
                d["channel"] = "code-exact"
                results.append(d); seen.add(f.sop_id)
                break
    # 2) 文本召回补充：优先 PG 真实向量，失败回退内存关键词
    if query:
        channel = "pg-vector"
        hits: List[Tuple[str, float]] = []
        try:
            from sca.rag.hybrid import pg_vector_search
            hits = [(doc_id, score) for doc_id, score, _meta
                    in pg_vector_search(query, top_k=top_k * 2)]
        except Exception:  # PG 未启用/未入库/API 失败 -> 内存关键词兜底
            channel = "kw-fallback"
            hits = [(c.metadata.get("sop_id"), score)
                    for c, score in _retriever().search(query, top_k=top_k * 2)]
        for sid, score in hits:
            if sid in seen or sid not in docs:
                continue
            d = dict(docs[sid]); d["match"] = round(float(score), 4); d["exact"] = False
            d["channel"] = channel
            results.append(d); seen.add(sid)
            if len(results) >= top_k:
                break
    return results[:top_k]


def summary() -> Dict[str, Any]:
    docs = _merge_custom()
    custom = _load_custom()
    return {
        "total": len(docs),
        "builtin": len(_CACHE),
        "custom": len(custom),
        "by_level": {lv: sum(1 for d in docs if d.get("level") == lv) for lv in ("P0", "P1", "P2", "P3")},
        "categories": sorted({d.get("category", "") for d in docs if d.get("category")}),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
    print("示例检索『冷链 温度超标』：")
    for r in search_sop("冷链 温度超标 改道", code="E-COLD-OVER"):
        print(" -", r["sop_id"], r["title"], f"match={r['match']}", f"steps={len(r['steps'])}")
