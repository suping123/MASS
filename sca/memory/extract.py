"""记忆抽取器：Extractor 端口 + 规则版最小实现（竖切片）。

端口约定：propose(text, ctx) -> List[MemoryOp]，只做"提案"——产出什么键、
什么值、信度多少、证据是什么；是否落库由 Guard + Resolver + Committer 决定。
LLM 版后置（同端口替换，输出受 JSON Schema 约束），规则版先保证链路确定可测。

覆盖的键（均在 KEY_REGISTRY 白名单内）：
  偏好：通知渠道（短信/钉钉/电话/邮件/微信，含"别再发X"否定式 -> delete）
  档案：称呼（叫我X）、城市（我在/搬到/现居X）、岗位（我的岗位是X）
  偏好：语言（说/用英语）、回答风格（简洁点/详细点）
  情景：记住/记一下 + 子句（同一句里命中更具体的键时，具体键优先）
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .models import (MEM_TYPE_EPISODIC, MEM_TYPE_PREFERENCE, MEM_TYPE_PROFILE,
                     OP_DELETE, OP_UPSERT, SOURCE_EXPLICIT, VALUE_MAX_LEN,
                     KEY_REGISTRY, MemoryOp)

# 通知渠道词表（中文词即入库值，人可读）
_CHANNEL_WORDS = ("短信", "钉钉", "电话", "邮件", "微信")
_CHANNEL_CONTEXT = ("通知", "提醒", "联系", "发消息", "联络")
_NEGATION = re.compile(r"别再|不要|别给|禁止|无需|不用")

_RE_NAME = re.compile(r"叫我(?P<v>[\u4e00-\u9fa5A-Za-z·]{2,10})")
_RE_CITY = re.compile(r"(?:我现在在|我在|现居|搬到|迁到|常驻)(?P<v>[\u4e00-\u9fa5]{2,10}?[市区县]?)")
_RE_ROLE = re.compile(r"我(?:的)?(?:岗位|职位|角色)是(?P<v>[\u4e00-\u9fa5]{2,8})")
_RE_LANG = re.compile(r"(?:说|用|回复|讲)(?P<v>中文|英语|英文|日语|文言文)")
_RE_STYLE_CONCISE = re.compile(r"(?:简洁|简单|简短|精炼)一点|别啰嗦|少说废话")
_RE_STYLE_DETAIL = re.compile(r"(?:详细|具体|展开)一点|说清楚|讲明白")
_RE_NOTE = re.compile(r"(?:记住|记一下|记下)[:：，,]?\s*(?P<v>.{2,80})")
_RE_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")

# 城市误识别的停用值（"我在开会"这类）
_CITY_STOPWORDS = {"开会", "忙", "线", "想", "说", "问", "想办法", "考虑"}


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in _RE_SENT_SPLIT.split(text or "") if s.strip()]


def _match_channel(sent: str) -> Optional[Tuple[str, bool]]:
    """返回 (渠道词, 是否否定式)；句子既无渠道词也无通知语境时返回 None。"""
    if not any(w in sent for w in _CHANNEL_CONTEXT):
        return None
    hit = next((w for w in _CHANNEL_WORDS if w in sent), None)
    if hit is None:
        return None
    return hit, bool(_NEGATION.search(sent))


class RuleExtractor:
    """规则版抽取器：确定、零成本、可单测。propose 产出已过 Guard 的提案。"""

    name = "rule-v1"

    def propose(self, text: str, ctx: Optional[Dict[str, Any]] = None) -> List[MemoryOp]:
        ops: Dict[str, MemoryOp] = {}   # 同键多句命中时，后一句覆盖前一句
        for sent in _split_sentences(text):
            self._from_sentence(sent, ops)
        return list(ops.values())

    def _from_sentence(self, sent: str, ops: Dict[str, MemoryOp]) -> None:
        specific = False

        ch = _match_channel(sent)
        if ch is not None:
            word, negated = ch
            self._put(ops, "preference.notify_channel",
                      None if negated else word, MEM_TYPE_PREFERENCE, sent,
                      action=OP_DELETE if negated else OP_UPSERT)
            specific = True

        m = _RE_NAME.search(sent)
        if m:
            self._put(ops, "profile.display_name", m.group("v"), MEM_TYPE_PROFILE, sent)
            specific = True

        m = _RE_CITY.search(sent)
        if m and m.group("v") not in _CITY_STOPWORDS:
            self._put(ops, "profile.city", m.group("v"), MEM_TYPE_PROFILE, sent)
            specific = True

        m = _RE_ROLE.search(sent)
        if m:
            self._put(ops, "profile.role", m.group("v"), MEM_TYPE_PROFILE, sent)
            specific = True

        m = _RE_LANG.search(sent)
        if m:
            self._put(ops, "preference.language", m.group("v"), MEM_TYPE_PREFERENCE, sent)
            specific = True

        if _RE_STYLE_CONCISE.search(sent):
            self._put(ops, "preference.response_style", "简洁", MEM_TYPE_PREFERENCE, sent)
            specific = True
        elif _RE_STYLE_DETAIL.search(sent):
            self._put(ops, "preference.response_style", "详细", MEM_TYPE_PREFERENCE, sent)
            specific = True

        if not specific:
            m = _RE_NOTE.search(sent)
            if m:
                self._put(ops, "episodic.note", m.group("v").strip(), MEM_TYPE_EPISODIC, sent)

    def _put(self, ops: Dict[str, MemoryOp], key: str, value: Optional[str],
             mtype: str, evidence: str, *, action: str = OP_UPSERT) -> None:
        ops[key] = MemoryOp(
            op_id=uuid.uuid4().hex, action=action, key=key, value=value,
            type=mtype, source=SOURCE_EXPLICIT, confidence=1.0,
            evidence=[evidence[:120]], actor="用户")


def guard(ops: List[MemoryOp]) -> Tuple[List[MemoryOp], List[MemoryOp]]:
    """校验闸口：白名单键 / 值形态 / 长度 / 置信度。返回 (放行, 丢弃)。"""
    kept: List[MemoryOp] = []
    dropped: List[MemoryOp] = []
    for op in ops:
        pol = KEY_REGISTRY.get(op.key)
        if pol is None:
            op.reason = "guard_unregistered_key"
            dropped.append(op)
            continue
        if op.action == OP_UPSERT:
            v = op.value
            if not isinstance(v, str) or not v.strip():
                op.reason = "guard_bad_value"
                dropped.append(op)
                continue
            if len(v) > VALUE_MAX_LEN:
                op.reason = "guard_value_too_long"
                dropped.append(op)
                continue
            if not (0.0 <= op.confidence <= 1.0):
                op.reason = "guard_bad_confidence"
                dropped.append(op)
                continue
        kept.append(op)
    return kept, dropped
