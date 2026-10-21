"""路线规划模型客户端（S2 约束提取 / S4 选路裁决 / S5 解释生成）。

设计（与意图识别模块同源、但独立）：
  * 复用 sca.credentials 的 KIND_MODEL 清单 + run_chain 多模型降级链；
  * 零依赖 urllib 直连 OpenAI 兼容 chat/completions（与 intent.TongyiIntentDetectV3._http_call 同思路）；
  * 模型名优先 reason_model，否则 intent_model，再回退 qwen-plus；
  * 全程 fail-open：模型不可用 / 返回空 / 解析失败 -> 返回合理默认，绝不抛异常、不阻塞规划。

三个职责：
  - extract_constraints(natural_lang) -> RouteConstraints   （S2）
  - adjudicate(candidate, constraints, cargo, tool_executor) -> dict  （S4，原生 function calling：模型可真调 geocode/estimate_eta；含 avoid_cities/swap_hub/reasons/risks）
  - summarize(route, constraints, decision) -> str           （S5，自然语言解释）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sca.route_llm")


@dataclass
class RouteConstraints:
    """模型从自然语言提取出的结构化路线约束（S2 产物）。"""
    prefer_highway: bool = False
    avoid_terrain: List[str] = field(default_factory=list)   # 如 ["山区", "隧道"]
    time_window: str = ""           # 如 "夜间优先" / "白天"
    cost_sensitivity: str = "normal"   # low / normal / high
    risk_aversion: str = "normal"      # low / normal / high
    notes: str = ""                 # 原始自然语言片段（截断）
    raw: str = ""                   # 模型原始输出（审计）

    def to_prompt(self) -> str:
        parts: List[str] = []
        if self.prefer_highway:
            parts.append("优先走高速")
        if self.avoid_terrain:
            parts.append("避开" + "、".join(self.avoid_terrain))
        if self.time_window:
            parts.append(f"时段偏好：{self.time_window}")
        if self.cost_sensitivity == "high":
            parts.append("成本敏感（优先低价）")
        elif self.cost_sensitivity == "low":
            parts.append("时效优先（可接受更高成本）")
        if self.risk_aversion == "high":
            parts.append("风险厌恶（优先稳妥绕行）")
        if self.notes:
            parts.append(self.notes)
        return "；".join(parts) if parts else "无特殊约束"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prefer_highway": self.prefer_highway,
            "avoid_terrain": list(self.avoid_terrain),
            "time_window": self.time_window,
            "cost_sensitivity": self.cost_sensitivity,
            "risk_aversion": self.risk_aversion,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# 零依赖 HTTP（与 intent 模块一致）
# ---------------------------------------------------------------------------
def _chat(base_url: str, api_key: str, model: str,
          messages: List[Dict[str, str]], timeout: float = 8.0) -> Optional[str]:
    """OpenAI 兼容 chat/completions 同步调用；失败返回 None（供 fail-open）。"""
    if not (base_url and api_key):
        return None
    url = (base_url.rstrip("/") + "/chat/completions")
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (data.get("choices", [{}])[0].get("message", {}).get("content") or "") or None
    except Exception as exc:  # noqa: 任何网络/解析错误都 fail-open
        logger.warning("[route_llm] chat 调用失败：%s", exc)
        return None


def _chat_with_tools(base_url: str, api_key: str, model: str,
                     messages: List[Dict[str, str]], tools: List[Dict[str, Any]],
                     executor, timeout: float = 8.0) -> Optional[str]:
    """OpenAI 兼容 chat/completions + 原生 function calling（稳健回灌版）。

    流程：带 tools 发首轮 -> 若模型返回 tool_calls，则在本地执行（executor 同步调用
    geocode/estimate_eta 等），把**真实结果以纯文本拼进一条 user 消息**回灌（而非
    role=tool 格式，规避部分端点对 tool-message 轮次返回空/400 的问题），再发第二轮
    取最终 JSON。任何环节失败返回 None（供降级链切下一条模型）。无 tool_calls 时直接
    返回内容。
    """
    if not (base_url and api_key):
        return None
    url = (base_url.rstrip("/") + "/chat/completions")
    payload = {"model": model, "messages": messages, "tools": tools,
               "tool_choice": "auto", "temperature": 0.2}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as r:
            data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    except Exception as exc:
        logger.warning("[route_llm] chat(tools) 首轮失败：%s", exc)
        return None
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return (msg.get("content") or "") or None
    # 执行工具调用，把真实结果拼成文本（回灌为普通 user 消息，规避 tool-message 兼容问题）
    lines = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        try:
            out = executor(name, args)
            logger.info("[route_llm] S4 模型调用工具：%s(%s)", name,
                        json.dumps(args, ensure_ascii=False))
        except Exception as exc:
            out = json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False)
        lines.append(f"工具 {name}({json.dumps(args, ensure_ascii=False)}) 返回：{out}")
    tool_report = "\n".join(lines)
    follow = list(messages) + [{
        "role": "user",
        "content": (
            f"以下是你调用的工具返回的真实结果，请据此做出最终路线裁决并只输出 JSON：\n"
            f"{tool_report}\n\n"
            f"JSON 字段：decision(str: accept|adjust), avoid_cities(list[str]), "
            f"swap_hub(str|null), reasons(list[str]), risks(list[str])。不要解释。"
        ),
    }]
    payload2 = {"model": model, "messages": follow, "temperature": 0.2}
    req2 = urllib.request.Request(
        url, data=json.dumps(payload2).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req2, timeout=timeout + 5) as r:
            data2 = json.loads(r.read().decode("utf-8"))
        return (data2.get("choices", [{}])[0].get("message", {}).get("content") or "") or None
    except Exception as exc:
        logger.warning("[route_llm] chat(tools) 第二轮失败：%s", exc)
        return None


def _extract_json(text: str) -> Dict[str, Any]:
    """从模型输出中容错提取第一个 JSON 对象。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa
        return {}


def _model_for(cred: Dict[str, Any]) -> str:
    d = cred.get("data") or {}
    return (d.get("reason_model") or d.get("intent_model")
            or "qwen-plus").strip() or "qwen-plus"


_SYSTEM_EXTRACT = (
    "你是物流路线规划助手。把用户的自然语言路线偏好解析为结构化 JSON，"
    "字段：prefer_highway(bool), avoid_terrain(list[str]), time_window(str), "
    "cost_sensitivity(str: low|normal|high), risk_aversion(str: low|normal|high), "
    "notes(str)。只输出 JSON，不要解释。"
)
_SYSTEM_ADJUDICATE = (
    "你是物流路线规划助手。给定候选路线与约束，判断是否调整。输出 JSON："
    "decision(str: accept|adjust), avoid_cities(list[str], 需规避的城市), "
    "swap_hub(str|null, 建议替换的枢纽城市), reasons(list[str]), risks(list[str])。\n"
    "裁决纪律：候选路线的途经城市来自**真实高速路径**，不得视为『低等级路段』而剔除；"
    "avoid_cities 只能填约束中点名的城市、或明显不符合约束偏好（如『避开山区』对应的"
    "山区途经点）的城市；只允许局部绕行，不得整体更换走廊（绕行后里程不得超过原里程"
    "1.30 倍）；连一个明确规避对象都找不到时才输出 accept；reasons 不得编造未提供的数据。"
    "只输出 JSON，不要解释。"
)
_SYSTEM_SUMMARY = (
    "你是物流路线规划助手。用一句简洁中文解释为何这样规划该路线，"
    "说明对约束（高速/山区/时段/成本/风险）的权衡。不要使用 JSON，直接给自然语言。"
)

# S4 原生 function calling 的工具清单（与 map_server.geocode / estimate_eta 语义一致）。
# 模型可在裁决前调用它们核对城市真实性与两段时效，再输出最终 JSON 决策。
_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "将城市/地名解析为内置路网节点（经纬度）。用于核对候选路线中的城市是否真实存在、坐标是否正确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "城市或地名，如 '武汉'、'襄阳'"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_eta",
            "description": "估算给定起讫城市的运输 ETA（小时）。用于在 accept/adjust 决策时核对时效与绕行代价。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "起点城市"},
                    "dest": {"type": "string", "description": "终点城市"},
                    "mode": {"type": "string", "description": "车型 truck/cold/hazmat/express", "default": "truck"},
                },
                "required": [],
            },
        },
    },
]

_SYSTEM_ADJUDICATE_FC = (
    "你是物流路线规划助手。给定候选路线与约束，判断是否调整，并输出 JSON："
    "decision(str: accept|adjust), avoid_cities(list[str], 需规避的城市), "
    "swap_hub(str|null, 建议替换的枢纽城市), reasons(list[str]), risks(list[str])。\n"
    "裁决纪律（必须遵守，违反会导致方案被系统驳回）：\n"
    "1) 候选路线的 waypoints / via_cities 是**真实高速路径**的车行途经点（含县级市如"
    "元氏、宁晋属正常现象，高速沿线本就串接县级节点），**不得**把它当作"
    "『低等级道路清单』而剔除；不得以『含县级城市』为由判定路线不合规。\n"
    "2) avoid_cities 只能填**约束中明确点名**的城市/路段，或确知的受影响城市。"
    "不得因为『某路段可能受影响』就大范围剔除途经点。\n"
    "3) 一次调整只允许**局部绕行**，不得整体更换走廊："
    "例如『石家庄→南京』不得整体改走郑州；绕行后里程应保持在原里程 1.30 倍以内。\n"
    "   3a) 约束**点名**了需避开的城市/路段 → 针对性剔除对应途经点；\n"
    "   3b) 约束只是**偏好**（如『避开山区』『优先高速』『夜间发运』）→ 可剔除明显"
    "不符合该偏好的途经点，但要说明依据；\n"
    "   3c) 连一个明确的规避对象都找不到 → 才输出 accept。\n"
    "4) reasons 只能引用给定事实（给定里程/途经城市/已提取约束）。"
    "**不得**编造未提供的数据（例如凭空的『专用路径 ETA 20.5h』），"
    "也不得把自己的估算写成既成事实。\n"
    "你可以用 geocode 核对城市是否存在、用 estimate_eta 核对两段时效来辅助判断；"
    "只输出最终 JSON，不要解释。"
)


class RoutePlannerLLM:
    """路线规划模型客户端：S2/S4/S5 三个调用点，均 fail-open。"""

    def __init__(self):
        self.last_source: str = "fallback"
        self.last_model: str = ""
        self.last_attempts: List[Dict[str, Any]] = []

    async def _chain(self, messages: List[Dict[str, str]]) -> tuple:
        """走 KIND_MODEL 多模型降级链，返回 (text, used_cred, attempts)。"""
        try:
            from sca.credentials import KIND_MODEL, run_chain
        except Exception as exc:  # noqa: 清单不可用 -> 直接 fail-open
            logger.warning("[route_llm] 凭证清单不可用：%s", exc)
            return None, None, []

        async def _one(cred):
            d = cred.get("data") or {}
            model = _model_for(cred)
            return await asyncio.to_thread(
                _chat, d.get("base_url"), d.get("api_key"), model, messages)

        try:
            result, used, attempts = await run_chain(KIND_MODEL, None, _one)
        except Exception as exc:  # noqa
            logger.warning("[route_llm] run_chain 异常：%s", exc)
            return None, None, []
        self.last_attempts = list(attempts or [])
        self.last_model = (used or {}).get("name", "") if result is not None else ""
        return result, used, attempts

    async def _chain_with_tools(self, system: str, user: str,
                                tools: List[Dict[str, Any]], executor) -> tuple:
        """KIND_MODEL 多模型降级链（S4 专用）：带原生 function calling。

        对每个候选模型执行「首轮 tools -> 本地执行工具 -> 第二轮取 JSON」。
        任一模型失败（异常/空响应）自动切下一条；全部失败返回 (None, None, attempts)。
        """
        try:
            from sca.credentials import KIND_MODEL, candidates, _usable, mark_fail, mark_ok
        except Exception as exc:
            logger.warning("[route_llm] 凭证清单不可用：%s", exc)
            return None, None, []
        items = [c for c in candidates(KIND_MODEL) if _usable(c)]
        attempts: List[Dict[str, Any]] = []
        used = None
        for cred in items:
            cid = cred.get("id", "")
            name = cred.get("name", cid)
            d = cred.get("data") or {}
            model = _model_for(cred)
            try:
                text = await asyncio.to_thread(
                    _chat_with_tools, d.get("base_url"), d.get("api_key"), model,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    tools, executor)
            except Exception as exc:  # noqa
                mark_fail(KIND_MODEL, cid, str(exc)[:200])
                attempts.append({"id": cid, "name": name, "ok": False,
                                 "error": str(exc)[:200]})
                continue
            if text is None:
                mark_fail(KIND_MODEL, cid, "模型返回空响应")
                attempts.append({"id": cid, "name": name, "ok": False,
                                 "error": "空响应"})
                continue
            mark_ok(KIND_MODEL, cid)
            attempts.append({"id": cid, "name": name, "ok": True})
            used = cred
            self.last_attempts = list(attempts)
            self.last_model = name
            return text, used, attempts
        return None, None, attempts

    # ---- S2 约束提取 ----
    async def extract_constraints(self, text: str) -> RouteConstraints:
        if not text or not text.strip():
            return RouteConstraints()  # 无自然语言 -> 空约束（fail-open 等价）
        result, used, attempts = await self._chain([
            {"role": "system", "content": _SYSTEM_EXTRACT},
            {"role": "user", "content": text},
        ])
        self.last_source = "model" if result is not None else "fallback"
        logger.info("[route_llm] S2 约束提取：source=%s", self.last_source)
        if result is None:
            return RouteConstraints(notes=text[:80])
        j = _extract_json(result)
        return RouteConstraints(
            prefer_highway=bool(j.get("prefer_highway")),
            avoid_terrain=list(j.get("avoid_terrain") or []),
            time_window=str(j.get("time_window") or ""),
            cost_sensitivity=str(j.get("cost_sensitivity") or "normal"),
            risk_aversion=str(j.get("risk_aversion") or "normal"),
            notes=text[:80],
            raw=result or "",
        )

    # ---- S4 选路裁决（原生 function calling，模型可真调 geocode/estimate_eta）----
    async def adjudicate(self, candidate: Dict[str, Any], constraints: RouteConstraints,
                         cargo_type: str, temp_ctrl: bool, hazmat: bool,
                         tool_executor=None) -> Dict[str, Any]:
        if not candidate:
            return self._accept_fallback("无候选路线")
        prov = str(candidate.get("provider") or "")
        # 来源标注：高德真实路径的途经点是"高速沿线节点"，不是低等级路段清单。
        # 明确告知可显著降低模型把县级途经点误判为不合规而整体改道（2026-10-15 实测）。
        prov_hint = ("（上述途经城市为高德真实路径的沿线节点，高速串联县级节点属正常，"
                     "不得据此判定路线不合规）" if prov.startswith("amap") else "")
        user = (
            f"候选路线：{'→'.join(str(w) for w in candidate.get('waypoints', []))}；"
            f"里程 {candidate.get('distance_km')}km；ETA {candidate.get('eta_h')}h；"
            f"provider={prov}{prov_hint}。\n"
            f"货物：{cargo_type}（温控={temp_ctrl}，危品={hazmat}）。\n"
            f"约束：{constraints.to_prompt()}。\n请裁决。"
        )
        if tool_executor is not None:
            result, used, attempts = await self._chain_with_tools(
                _SYSTEM_ADJUDICATE_FC, user, _TOOL_SCHEMAS, tool_executor)
        else:
            result, used, attempts = await self._chain([
                {"role": "system", "content": _SYSTEM_ADJUDICATE},
                {"role": "user", "content": user},
            ])
        src = "model" if result is not None else "fallback"
        if result is None:
            return self._accept_fallback("模型不可用，采用初稿")
        j = _extract_json(result)
        decision = str(j.get("decision") or "accept")
        logger.info("[route_llm] S4 裁决完成：source=%s model=%s decision=%s reasons=%d",
                    src, (used or {}).get("name", ""), decision, len(j.get("reasons", [])))
        return {
            "decision": decision,
            "avoid_cities": list(j.get("avoid_cities") or []),
            "swap_hub": j.get("swap_hub"),
            "reasons": list(j.get("reasons") or []),
            "risks": list(j.get("risks") or []),
            "source": src,
            "model_name": (used or {}).get("name", ""),
            "attempts": attempts or [],
        }

    # ---- S5 解释生成 ----
    async def summarize(self, route: Dict[str, Any], constraints: RouteConstraints,
                        decision: Dict[str, Any]) -> str:
        if not route:
            return ""
        user = (
            f"路线：{'→'.join(str(w) for w in route.get('waypoints', []))}；"
            f"里程 {route.get('distance_km')}km；ETA {route.get('eta_h')}h；"
            f"provider={route.get('provider')}。\n"
            f"约束：{constraints.to_prompt()}。\n"
            f"模型决策：{decision.get('decision')}；理由：{decision.get('reasons')}。"
        )
        result, _, _ = await self._chain([
            {"role": "system", "content": _SYSTEM_SUMMARY},
            {"role": "user", "content": user},
        ])
        logger.info("[route_llm] S5 解释生成：len=%d source=%s",
                    len(result or ""), "model" if result else "fallback")
        return (result or "").strip()

    @staticmethod
    def _accept_fallback(reason: str) -> Dict[str, Any]:
        return {
            "decision": "accept",
            "avoid_cities": [],
            "swap_hub": None,
            "reasons": [reason],
            "risks": [],
            "source": "fallback",
            "model_name": "",
            "attempts": [],
        }
