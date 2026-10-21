"""故障谱目录（FR-05/FR-06/FR-08 + v2 §3.5 L1 自愈率 82% 论证基础）。

≥30 类可枚举异常，覆盖 12 子系统。每类标注：
  - level   : P0 高危需审批 / P1 重规划+确认 / P2 自动换路 / P3 记录建议
  - sop_id  : 命中的 SOP（FR-10）
  - auto    : 是否可由系统自动处置（自愈）
  - subsystems: 所属子系统（用于覆盖率论证）

自愈率论证口径（v2 §3.5）：自愈率 = 自动处置成功数 / 异常发生总数；谱外异常不计入。
本目录即"谱内"全集，覆盖率由 coverage_report() 给出，须业务方确认覆盖度达标。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from sca.core.decision import requires_approval


@dataclass(frozen=True)
class FaultType:
    code: str
    name: str
    level: str            # P0 / P1 / P2 / P3
    sop_id: str           # 命中 SOP（FR-10）
    auto: bool            # True = 可由系统自动处置（自愈）
    subsystems: Tuple[str, ...]
    desc: str = ""

    @property
    def needs_approval(self) -> bool:
        return requires_approval(self.level)


# 40 类异常，覆盖 12 子系统：冷链 / 危险品 / 交通 / 天气 / 运力 / TMS(运输执行) /
# 海关 / 系统(IoT) / 客户 / 履约(SLA) / 仓储(WMS) / 订单(OMS)
FAULT_SPECTRUM: List[FaultType] = [
    # --- 冷链 ---
    FaultType("E-COLD-FAIL", "制冷机故障", "P0", "SOP-P0-COLD", False, ("冷链", "IoT"), "制冷机组停机"),
    FaultType("E-COLD-OVER", "温度超标", "P0", "SOP-P0-COLD", False, ("冷链", "IoT"), "厢温超过阈值"),
    FaultType("E-COLD-DRIFT", "温度漂移", "P1", "SOP-P1-COLD", True, ("冷链",), "温度渐进偏离"),
    FaultType("E-COLD-DOOR", "厢门未关严", "P2", "SOP-P2-COLD", True, ("冷链", "WMS"), "开门报警"),

    # --- 危险品 ---
    FaultType("E-DG-LEAK", "危险品泄漏", "P0", "SOP-P0-DG", False, ("危险品",), "泄漏报警"),
    FaultType("E-DG-PKG", "包装破损", "P1", "SOP-P1-DG", False, ("危险品", "WMS"), "外包装破损"),
    FaultType("E-DG-OVER", "危险品超温", "P0", "SOP-P0-DG", False, ("危险品", "IoT"), "超温报警"),
    FaultType("E-DG-LABEL", "标签缺失", "P2", "SOP-P2-DG", True, ("危险品", "WMS"), "标签漏贴"),

    # --- 交通 ---
    FaultType("E-JAM", "道路拥堵", "P2", "SOP-P2-REROUTE", True, ("交通", "地图"), "拥堵缓行"),
    FaultType("E-ROAD-CLOSED", "道路封闭", "P1", "SOP-P1-REROUTE", True, ("交通", "地图"), "封路"),
    FaultType("E-ACCIDENT", "交通事故", "P1", "SOP-P1-REROUTE", True, ("交通", "地图"), "事故占道"),
    FaultType("E-CONSTR", "施工绕行", "P2", "SOP-P2-REROUTE", True, ("交通", "地图"), "施工改道"),

    # --- 天气 ---
    FaultType("E-WX-RAIN", "暴雨预警", "P2", "SOP-P2-WX", True, ("天气",), "暴雨"),
    FaultType("E-WX-SNOW", "暴雪", "P1", "SOP-P1-WX", True, ("天气",), "暴雪"),
    FaultType("E-WX-TYPHOON", "台风", "P0", "SOP-P0-WX", False, ("天气",), "台风红色预警"),
    FaultType("E-WX-FOG", "大雾", "P2", "SOP-P2-WX", True, ("天气",), "大雾"),
    FaultType("E-WX-HOT", "高温预警", "P2", "SOP-P2-WX", True, ("天气",), "高温"),

    # --- 运力 ---
    FaultType("E-CAP-BREAK", "车辆故障", "P1", "SOP-P1-CAP", True, ("运力",), "在途抛锚"),
    FaultType("E-CAP-DRIVER", "司机失联", "P1", "SOP-P1-CAP", False, ("运力",), "超时无上报"),
    FaultType("E-CAP-BREACH", "承运商违约", "P0", "SOP-P0-CAP", False, ("运力",), "擅自撤单"),
    FaultType("E-CAP-GAP", "运力缺口", "P2", "SOP-P2-CAP", True, ("运力",), "临时无车"),
    FaultType("E-CAP-LOAD", "装载超限", "P2", "SOP-P2-CAP", True, ("运力", "WMS"), "超载"),

    # --- TMS 运输执行 ---
    FaultType("E-TMS-DEV", "偏航", "P1", "SOP-P1-DEV", True, ("TMS", "地图"), "偏离规划路线"),
    FaultType("E-TMS-STOP", "停留超时", "P2", "SOP-P2-STOP", True, ("TMS", "IoT"), "异常长时间停留"),
    FaultType("E-TMS-SIGN", "签收异常", "P2", "SOP-P2-SIGN", True, ("TMS",), "拒签/漏签"),
    FaultType("E-TMS-LOST", "运单丢失", "P1", "SOP-P1-TMS", False, ("TMS", "OMS"), "单号缺失"),
    FaultType("E-TMS-DUP", "重复派单", "P3", "SOP-P3-TMS", True, ("TMS", "OMS"), "重复下发"),

    # --- 海关 / 口岸 ---
    FaultType("E-CUS-HOLD", "查验扣留", "P1", "SOP-P1-CUS", True, ("海关",), "海关查验"),
    FaultType("E-CUS-DOC", "单证不符", "P2", "SOP-P2-CUS", True, ("海关", "OMS"), "单据错误"),
    FaultType("E-CUS-TAX", "税费争议", "P3", "SOP-P3-CUS", True, ("海关",), "税费异议"),

    # --- 系统 / IoT ---
    FaultType("E-SYS-TIMEOUT", "接口超时", "P3", "SOP-P3-SYS", True, ("系统",), "API 超时"),
    FaultType("E-SYS-GPSLOST", "GPS 失联", "P2", "SOP-P2-SYS", True, ("系统", "IoT"), "定位中断"),
    FaultType("E-SYS-DRIFT", "定位漂移", "P3", "SOP-P3-SYS", True, ("系统", "IoT"), "漂移"),
    FaultType("E-SYS-DATA", "数据缺失", "P3", "SOP-P3-SYS", True, ("系统",), "字段缺失"),

    # --- 客户 ---
    FaultType("E-CUST-READDR", "客户改址", "P2", "SOP-P2-CUST", True, ("客户", "OMS"), "收货地址变更"),
    FaultType("E-CUST-REJECT", "客户拒收", "P1", "SOP-P1-CUST", False, ("客户",), "到货拒收"),
    FaultType("E-CUST-URGENT", "订单加急", "P2", "SOP-P2-CUST", True, ("客户", "OMS"), "临时加急"),

    # --- 履约 / SLA ---
    FaultType("E-SLA-DELAY", "时效违约", "P1", "SOP-P1-SLA", False, ("履约",), "超时效"),
    FaultType("E-SLA-DAMAGE", "货损", "P0", "SOP-P0-DMG", False, ("履约", "WMS"), "破损/丢失/受潮/雨淋"),
    FaultType("E-SLA-MINOR", "轻微延误", "P3", "SOP-P3-SLA", True, ("履约",), "小幅延误"),

    # --- 仓储 WMS ---
    FaultType("E-WMS-OUT", "出库异常", "P2", "SOP-P2-WMS", True, ("WMS",), "出库延误"),
    FaultType("E-WMS-STOCK", "库存不符", "P1", "SOP-P1-WMS", False, ("WMS",), "账实不符"),

    # --- 订单 OMS ---
    FaultType("E-OMS-CANCEL", "订单取消", "P2", "SOP-P2-OMS", True, ("OMS",), "中途取消"),
    FaultType("E-OMS-ERR", "下单信息错误", "P3", "SOP-P3-OMS", True, ("OMS",), "信息录错"),
]


def sop_for(code: str) -> str:
    """异常码 -> 命中 SOP（FR-10）。"""
    for f in FAULT_SPECTRUM:
        if f.code == code:
            return f.sop_id
    return "SOP-UNKNOWN"


# 文本 -> 故障类型 关键词表（FR-05 分级自动化：调度员不再人工判定等级）。
# 顺序即优先级：高危词在前，防止"温度超标+轻微"被低等级截胡；长词在前防短词误命中。
_TEXT_KEYWORDS: List[Tuple[str, str]] = [
    # --- P0 高危 ---
    ("制冷机故障", "E-COLD-FAIL"), ("制冷机停机", "E-COLD-FAIL"), ("机组停机", "E-COLD-FAIL"),
    ("温度超标", "E-COLD-OVER"), ("厢温超", "E-COLD-OVER"), ("冷链失效", "E-COLD-OVER"),
    ("泄漏", "E-DG-LEAK"), ("泄露", "E-DG-LEAK"), ("危险品洒", "E-DG-LEAK"),
    ("危险品超温", "E-DG-OVER"),
    ("台风", "E-WX-TYPHOON"),
    ("承运商违约", "E-CAP-BREACH"), ("擅自撤单", "E-CAP-BREACH"), ("撤单", "E-CAP-BREACH"),
    ("货损", "E-SLA-DAMAGE"), ("货物损毁", "E-SLA-DAMAGE"), ("货物丢失", "E-SLA-DAMAGE"),
    ("货物破损", "E-SLA-DAMAGE"), ("淋雨", "E-SLA-DAMAGE"), ("雨淋", "E-SLA-DAMAGE"),
    ("受潮", "E-SLA-DAMAGE"), ("湿损", "E-SLA-DAMAGE"), ("浸水", "E-SLA-DAMAGE"),
    ("泡水", "E-SLA-DAMAGE"), ("货物湿", "E-SLA-DAMAGE"), ("进水", "E-SLA-DAMAGE"),
    # --- P1 重大 ---
    ("温度漂移", "E-COLD-DRIFT"),
    ("包装破损", "E-DG-PKG"), ("外包装破", "E-DG-PKG"),
    ("道路封闭", "E-ROAD-CLOSED"), ("封路", "E-ROAD-CLOSED"), ("交通管制", "E-ROAD-CLOSED"),
    ("交通事故", "E-ACCIDENT"), ("事故占道", "E-ACCIDENT"), ("车祸", "E-ACCIDENT"),
    ("暴雪", "E-WX-SNOW"), ("大雪", "E-WX-SNOW"),
    ("车辆故障", "E-CAP-BREAK"), ("抛锚", "E-CAP-BREAK"), ("在途坏车", "E-CAP-BREAK"),
    ("司机失联", "E-CAP-DRIVER"), ("司机联系不上", "E-CAP-DRIVER"),
    ("偏航", "E-TMS-DEV"), ("偏离路线", "E-TMS-DEV"), ("偏离规划", "E-TMS-DEV"),
    ("运单丢失", "E-TMS-LOST"), ("单号缺失", "E-TMS-LOST"),
    ("海关查验", "E-CUS-HOLD"), ("查验扣留", "E-CUS-HOLD"), ("被扣", "E-CUS-HOLD"),
    ("客户拒收", "E-CUST-REJECT"), ("拒收", "E-CUST-REJECT"),
    ("时效违约", "E-SLA-DELAY"), ("超时效", "E-SLA-DELAY"), ("严重延误", "E-SLA-DELAY"),
    ("库存不符", "E-WMS-STOCK"), ("账实不符", "E-WMS-STOCK"),
    # --- P2 一般 ---
    ("厢门未关", "E-COLD-DOOR"), ("开门报警", "E-COLD-DOOR"),
    ("标签缺失", "E-DG-LABEL"), ("标签漏贴", "E-DG-LABEL"),
    ("拥堵", "E-JAM"), ("堵车", "E-JAM"), ("塞车", "E-JAM"), ("缓行", "E-JAM"),
    ("施工", "E-CONSTR"), ("绕行施工", "E-CONSTR"),
    ("暴雨", "E-WX-RAIN"), ("大雾", "E-WX-FOG"), ("高温预警", "E-WX-HOT"),
    ("运力缺口", "E-CAP-GAP"), ("临时无车", "E-CAP-GAP"), ("没车", "E-CAP-GAP"),
    ("超载", "E-CAP-LOAD"), ("装载超限", "E-CAP-LOAD"),
    ("停留超时", "E-TMS-STOP"), ("长时间停留", "E-TMS-STOP"), ("异常停留", "E-TMS-STOP"),
    ("签收异常", "E-TMS-SIGN"), ("拒签", "E-TMS-SIGN"), ("漏签", "E-TMS-SIGN"),
    ("单证不符", "E-CUS-DOC"), ("单据错误", "E-CUS-DOC"),
    ("GPS失联", "E-SYS-GPSLOST"), ("定位中断", "E-SYS-GPSLOST"), ("定位丢失", "E-SYS-GPSLOST"),
    ("客户改址", "E-CUST-READDR"), ("收货地址变更", "E-CUST-READDR"), ("改地址", "E-CUST-READDR"),
    ("订单加急", "E-CUST-URGENT"), ("临时加急", "E-CUST-URGENT"), ("加急", "E-CUST-URGENT"),
    ("出库异常", "E-WMS-OUT"), ("出库延误", "E-WMS-OUT"),
    ("订单取消", "E-OMS-CANCEL"), ("中途取消", "E-OMS-CANCEL"),
    # --- P3 轻微 ---
    ("重复派单", "E-TMS-DUP"), ("重复下发", "E-TMS-DUP"),
    ("税费争议", "E-CUS-TAX"), ("税费异议", "E-CUS-TAX"),
    ("接口超时", "E-SYS-TIMEOUT"), ("API超时", "E-SYS-TIMEOUT"),
    ("定位漂移", "E-SYS-DRIFT"), ("数据缺失", "E-SYS-DATA"), ("字段缺失", "E-SYS-DATA"),
    ("轻微延误", "E-SLA-MINOR"), ("小幅延误", "E-SLA-MINOR"),
    ("下单信息错误", "E-OMS-ERR"), ("信息录错", "E-OMS-ERR"),
    # --- 兜底泛化词（放最后，命中即按语义分级） ---
    ("延误", "E-SLA-MINOR"), ("故障", "E-CAP-BREAK"),
]


def infer_from_text(text: str) -> Dict:
    """异常描述文本 -> 故障类型/等级自动识别（规则优先，可被模型兜底增强）。

    返回 {"matched": bool, "code","name","level","sop_id","keyword"}；
    未命中时 matched=False，level 走默认 P2 由调用方决定。
    """
    text = (text or "").strip()
    if not text:
        return {"matched": False, "code": None, "name": None, "level": None,
                "sop_id": None, "keyword": None}
    for kw, code in _TEXT_KEYWORDS:
        if kw in text:
            f = by_code(code)
            if f:
                return {"matched": True, "code": f.code, "name": f.name,
                        "level": f.level, "sop_id": f.sop_id, "keyword": kw}
    return {"matched": False, "code": None, "name": None, "level": None,
            "sop_id": None, "keyword": None}


# ---------------------------------------------------------------------------
# 异常线索词（2026-10-15）：把"在报告出了点事但没落进故障谱"与"纯下单需求"分开。
# 背景：原来只要 infer_from_text 未命中，一律塞 E-UNKNOWN P2「未识别异常·需人工」。
# 于是一条纯下单需求（"河北到南京 制造业器械 今日20点整起4天 20t"）也会被扣上
# "系统未能在故障谱中匹配该描述"的帽子——描述里根本没有异常可匹配。
# 现在的口径：
#   命中线索词 -> E-UNKNOWN，P2，提示人工确认（不编造具体故障）；
#   无线索词   -> 无异常，常规调度（不进入异常处置链、不做应急绕行重规划）。
# ---------------------------------------------------------------------------
_ANOMALY_HINTS: Tuple[str, ...] = (
    "异常", "故障", "事故", "告警", "报警", "洒落", "断网", "断电", "掉线", "离线",
    "滞留", "晚点", "堵", "延误", "超时", "抛锚", "坏车", "失联", "偏航", "偏离",
    "破损", "损坏", "丢失", "丢件", "淋雨", "受潮", "泡水", "进水", "泄漏", "泄露",
    "超温", "超载", "超限", "拒收", "拒签", "漏签", "改址", "撤单", "违约", "查验",
    "扣留", "扣关", "封路", "管制", "绕行", "施工", "预警", "无车", "没车", "缺车",
)
# 显式否认句：出现这些说法说明"没出事"，不能仅因含"异常"二字就判为异常线索
_NO_ANOMALY_PAT = re.compile(r"无异常|没有异常|未见异常|一切正常|正常(?:运输|发车|配送|执行)")


def has_anomaly_hint(text: str) -> str | None:
    """文本是否含"异常线索"：命中返回线索词，无则返回 None。"""
    t = str(text or "")
    if _NO_ANOMALY_PAT.search(t):
        return None
    for w in _ANOMALY_HINTS:
        if w in t:
            return w
    return None


def by_code(code: str) -> FaultType | None:
    for f in FAULT_SPECTRUM:
        if f.code == code:
            return f
    return None


def coverage_report() -> Dict[str, int]:
    """按子系统统计故障谱覆盖度（论证覆盖率达标用）。"""
    cov: Dict[str, int] = {}
    for f in FAULT_SPECTRUM:
        for sub in f.subsystems:
            cov[sub] = cov.get(sub, 0) + 1
    return dict(sorted(cov.items(), key=lambda kv: -kv[1]))


def summary() -> Dict[str, int]:
    total = len(FAULT_SPECTRUM)
    auto = sum(1 for f in FAULT_SPECTRUM if f.auto)
    by_level = {}
    for f in FAULT_SPECTRUM:
        by_level[f.level] = by_level.get(f.level, 0) + 1
    return {
        "total": total,
        "auto_recoverable": auto,
        "manual_only": total - auto,
        "by_level": by_level,
        "subsystems_covered": len(coverage_report()),
    }
