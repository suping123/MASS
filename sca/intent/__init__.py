"""意图识别与工具路由（FR-12）：规则优先、模型兜底。

默认实现 = RuleFirstIntentRouter 包裹 TongyiIntentDetectV3（阿里云百炼在线 API，
已核实可用，INTENT_MODE 直接输出意图 + 工具调用 JSON）。无 API Key 时退化为纯规则，
骨架仍可直接运行。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sca.intent")


@dataclass
class IntentResult:
    intent: Optional[str]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "rule"  # rule | model
    model_name: str = ""      # 命中的模型凭证名（多模型降级时可追溯）
    attempts: List[Dict[str, Any]] = field(default_factory=list)  # 降级尝试链


class IntentModel(ABC):
    @abstractmethod
    async def classify(
        self, text: str, tools: List[Dict[str, Any]], ctx: Dict[str, Any]
    ) -> IntentResult:
        ...


# ---------------------------------------------------------------------------
# 规则优先：关键词 / 正则先判，命中即高置信返回，不调模型（成本 + 延迟双优）
# ---------------------------------------------------------------------------
class RuleFirstIntentRouter(IntentModel):
    RULES: List[tuple] = [
        (r"偏航|偏离路线|走错", "exception.deviation", []),
        (r"延误|晚点|超时|未按时", "exception.delay", []),
        (r"承运商|运力|配车|找车", "capacity.match", []),
        (r"温控|冷链失效|温度超标|冻坏|厢温", "exception.temp", []),
        (r"换路|改道|重规划|重新规划|绕行", "plan.replan", []),
        (r"查(询|一下)?(运单|订单|状态)", "order.query", []),
        (r"创建运单|下单|建单|开单", "order.create", []),
        (r"天气|气象|预警", "weather.query", []),
    ]

    def __init__(self, fallback: Optional[IntentModel] = None):
        self.fallback = fallback

    async def classify(self, text, tools, ctx):
        for pat, intent, tcalls in self.RULES:
            if re.search(pat, text):
                logger.debug("[intent] rule hit: %s -> %s", intent, text)
                return IntentResult(intent=intent, tool_calls=tcalls, confidence=0.9, source="rule")
        # 低置信 -> 模型兜底
        if self.fallback:
            logger.debug("[intent] rule miss, fallback to model")
            return await self.fallback.classify(text, tools, ctx)
        return IntentResult(intent=None, confidence=0.0, source="rule")


# ---------------------------------------------------------------------------
# TongyiIntentDetectV3（阿里云百炼，OpenAI 兼容）：意图 + 工具联合预测
# 文档：https://help.aliyun.com/zh/model-studio/tongyi-intent-detect-v3
# ---------------------------------------------------------------------------
class TongyiIntentDetectV3(IntentModel):
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 5.0):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    @staticmethod
    def _build_system(tools: List[Dict[str, Any]]) -> str:
        tools_str = json.dumps(tools, ensure_ascii=False)
        return (
            "You are Qwen, created by Alibaba Cloud. You are a helpful assistant. "
            "You may call one or more tools to assist with the user query. "
            f"The tools you can use are as follows:\n{tools_str}\nResponse in INTENT_MODE."
        )

    @staticmethod
    def _parse(content: str) -> List[Dict[str, Any]]:
        m = re.search(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
        if not m:
            return []
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            logger.warning("[intent] parse tool_call failed: %s", content[:200])
            return []

    @staticmethod
    def _to_result(content: str) -> IntentResult:
        calls = TongyiIntentDetectV3._parse(content)
        intent = calls[0].get("name") if calls else None
        return IntentResult(intent=intent, tool_calls=calls, confidence=0.7, source="model")

    def _http_call(self, text: str, tools: List[Dict[str, Any]]) -> str:
        """零依赖兜底：标准库直连 OpenAI 兼容端点（无需 openai 包）。

        返回与 openai 路径一致的 message.content 文本（非整段 HTTP JSON）。
        """
        import urllib.error
        import urllib.request

        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._build_system(tools)},
                {"role": "user", "content": text},
            ],
            "timeout": self.timeout,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout + 5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (data.get("choices", [{}])[0]
                .get("message", {}).get("content") or "")

    async def try_classify(self, text, tools) -> Optional["IntentResult"]:
        """不吞异常的调用入口：失败返回 None，供多模型降级链切换下一条凭证。

        与 ``classify`` 的区别：``classify`` 捕获异常后返回空结果（供单点调用），
        ``try_classify`` 把失败抛给调用方，由 ModelChain 决定降级。
        """
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._build_system(tools)},
                    {"role": "user", "content": text},
                ],
                timeout=self.timeout,
            )
            content = resp.choices[0].message.content or ""
        except ImportError:
            content = await asyncio.to_thread(self._http_call, text, tools)
        if not content.strip():
            return None
        return self._to_result(content)

    async def classify(self, text, tools, ctx):
        # 优先 openai（装了则用，异步且体验更好）
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            try:
                resp = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self._build_system(tools)},
                        {"role": "user", "content": text},
                    ],
                    timeout=self.timeout,
                )
                content = resp.choices[0].message.content or ""
                return self._to_result(content)
            except Exception as exc:  # 在线API超时/限流 -> 返回空，由上层降级（FR §9）
                logger.warning("[intent] openai call failed (%s), degrade", exc)
                return IntentResult(intent=None, confidence=0.0, source="model")
        except ImportError:
            # 未装 openai -> 标准库直连（零依赖也能用真实模型）
            logger.debug("[intent] openai 未安装，使用标准库直连 DashScope")
            try:
                content = await asyncio.to_thread(self._http_call, text, tools)
                return self._to_result(content)
            except Exception as exc:  # noqa
                logger.warning("[intent] 标准库调用失败 (%s)，降级", exc)
                return IntentResult(intent=None, confidence=0.0, source="model")


# ---------------------------------------------------------------------------
# 多模型降级链：按「模型配置清单」优先级依次尝试，失败自动切下一条
# ---------------------------------------------------------------------------
class ModelChain(IntentModel):
    """按凭证清单顺序调用在线模型；某条失败（异常/空响应）自动降级到下一条。

    全部失败时返回 ``intent=None`` 的 IntentResult（source="model"），
    由上层规则/默认策略兜底——不伪造意图。
    """

    def __init__(self, entries: Optional[List[Dict[str, Any]]] = None):
        self.entries = entries if entries is not None else []
        self.last_attempts: List[Dict[str, Any]] = []
        self.last_model: str = ""

    @staticmethod
    def _build(cred: Dict[str, Any]) -> TongyiIntentDetectV3:
        d = cred.get("data") or {}
        return TongyiIntentDetectV3(
            api_key=(d.get("api_key") or "").strip(),
            base_url=(d.get("base_url") or "").strip()
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=(d.get("intent_model") or "").strip() or "tongyi-intent-detect-v3",
            timeout=float(d.get("timeout") or 5.0),
        )

    async def classify(self, text, tools, ctx):
        from sca.credentials import KIND_MODEL, _usable, candidates, mark_fail, mark_ok

        items = [c for c in (self.entries or candidates(KIND_MODEL)) if _usable(c)]
        self.last_attempts = []
        for cred in items:
            cid = cred.get("id", "")
            name = cred.get("name", cid)
            try:
                r = await self._build(cred).try_classify(text, tools)
            except Exception as exc:  # noqa: 该凭证失败 -> 自动降级下一条
                mark_fail(KIND_MODEL, cid, str(exc))
                self.last_attempts.append({"id": cid, "name": name, "ok": False,
                                           "error": str(exc)[:200]})
                logger.warning("[intent] 模型 %s 调用失败，降级下一条：%s", name, exc)
                continue
            if r is None:
                mark_fail(KIND_MODEL, cid, "模型返回空响应")
                self.last_attempts.append({"id": cid, "name": name, "ok": False,
                                           "error": "模型返回空响应"})
                logger.warning("[intent] 模型 %s 返回空响应，降级下一条", name)
                continue
            mark_ok(KIND_MODEL, cid)
            self.last_attempts.append({"id": cid, "name": name, "ok": True})
            self.last_model = name
            r.model_name = name
            r.attempts = list(self.last_attempts)
            return r
        return IntentResult(intent=None, confidence=0.0, source="model",
                            attempts=list(self.last_attempts),
                            model_name="")


def build_intent_model(api_key: str = "", base_url: str = "", model: str = "") -> IntentModel:
    """工厂：规则优先 + **模型清单降级链**兜底；无可用模型凭证则纯规则。

    清单来自 ``data/credentials.json``（由「模型配置清单」维护），按 priority 依次尝试，
    单条失败自动切下一条。显式传入 api_key 时退化为单点模型（兼容旧调用）。
    """
    from sca.settings import settings

    if api_key or base_url or model:
        single = TongyiIntentDetectV3(
            api_key or settings.dashscope_api_key,
            base_url or settings.dashscope_base_url,
            model or settings.intent_model,
            timeout=settings.intent_timeout)
        return RuleFirstIntentRouter(fallback=single)

    try:
        from sca.credentials import KIND_MODEL, candidates, _usable
        items = [c for c in candidates(KIND_MODEL) if _usable(c)]
    except Exception as exc:  # noqa: 清单不可用 -> 退化为 settings 单点
        logger.warning("[intent] 模型清单不可用（%s），退化为 .env 单点模型", exc)
        items = []
    if items:
        return RuleFirstIntentRouter(fallback=ModelChain(items))
    if settings.dashscope_api_key:
        return RuleFirstIntentRouter(fallback=TongyiIntentDetectV3(
            settings.dashscope_api_key, settings.dashscope_base_url,
            settings.intent_model, timeout=settings.intent_timeout))
    return RuleFirstIntentRouter(fallback=None)
    return RuleFirstIntentRouter(fallback=fallback)
