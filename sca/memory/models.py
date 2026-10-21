"""长短期记忆的数据模型与键注册表。

抽象模型（三层）：
  L0 工作记忆（短）= 当前会话上下文，由 LangGraph 状态 + 本轮召回块承载，可重建；
  L1 会话状态（中）= sca/sessions.py 的 8 态会话机 + 结构化槽位；
  L2 长期记忆（长）= 本包的 memory_items：跨会话的档案/偏好/情景。

记忆条目 = (user_id, key, value) 三元组 + 元数据 + 双时态版本：
  - valid_from  事实为真的时间（用户什么时候搬去上海）
  - recorded_at 我们得知的时间（系统什么时候记下来）
  两者分离才能回答"当时我们知道什么"——审计与故障复盘都靠它。

键注册表是 Guard 的白名单：抽取器只能产出注册内的 key，未注册的一律
丢弃并记原因码，防止规则/LLM 抽取器凭空造键把存储写脏。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# —— 记忆类型 ——
MEM_TYPE_PROFILE = "profile"        # 档案事实（低变）：姓名/城市/角色
MEM_TYPE_PREFERENCE = "preference"  # 偏好（中变）：通知渠道/语言/风格
MEM_TYPE_EPISODIC = "episodic"      # 情景（追加 + TTL）：某日问过/决定过什么

# —— 来源权威序：explicit（用户明示） > tool（工具/系统） > inferred（推断）——
SOURCE_EXPLICIT = "explicit"
SOURCE_TOOL = "tool"
SOURCE_INFERRED = "inferred"

# —— 条目状态 ——
STATUS_VALID = "valid"
STATUS_SUPERSEDED = "superseded"    # 被同键新值取代（保留历史，valid_to=取代时刻）
STATUS_DELETED = "deleted"          # 软删除（墓碑；物理清除由合规作业执行）

# —— 记忆操作 ——
OP_UPSERT = "upsert"
OP_DELETE = "delete"
OP_ERASE = "erase"                  # 合规擦除（软删 + 墓碑 + 审计）

# —— PII 分级 ——
PII_PUBLIC = "public"
PII_INTERNAL = "internal"
PII_SENSITIVE = "sensitive"         # 日志脱敏；不进向量库（embedding 本身是泄露面）

# 推断类提案的置信度下限：低于它不直接落库（标 contested，交由调用方澄清）
CONFIDENCE_FLOOR = 0.6

VALUE_MAX_LEN = 200                 # 单条记忆值的长度上限（Guard）


@dataclass
class KeyPolicy:
    """每个记忆键的治理策略（PolicyEngine 的静态版；数据驱动，可热扩充）。"""

    type: str = MEM_TYPE_PREFERENCE
    pii_level: str = PII_INTERNAL
    pinned: bool = False            # 固定块：上下文裁剪时永不淘汰
    ttl_days: int = 0               # 0 = 不过期（仅 supersede / 显式删除）
    authority: str = SOURCE_EXPLICIT  # 该键的权威来源（冲突消解时用）


KEY_REGISTRY: Dict[str, KeyPolicy] = {
    "profile.display_name": KeyPolicy(MEM_TYPE_PROFILE, PII_SENSITIVE, pinned=True),
    "profile.city": KeyPolicy(MEM_TYPE_PROFILE, PII_INTERNAL),
    "profile.role": KeyPolicy(MEM_TYPE_PROFILE, PII_INTERNAL),
    "preference.notify_channel": KeyPolicy(MEM_TYPE_PREFERENCE, PII_INTERNAL, pinned=True),
    "preference.language": KeyPolicy(MEM_TYPE_PREFERENCE, PII_INTERNAL),
    "preference.response_style": KeyPolicy(MEM_TYPE_PREFERENCE, PII_INTERNAL),
    "episodic.note": KeyPolicy(MEM_TYPE_EPISODIC, PII_INTERNAL, ttl_days=90),
}


def content_hash(value: Any) -> str:
    """值的指纹：同值重复陈述走 dedup，不产生新版本（幂等）。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class MemoryOp:
    """一条记忆变更提案（Extractor 产出；Guard/Resolver 处置后由 Committer 执行）。

    LLM/规则只负责"提案"；是否落库、落成什么版本，由确定性管线决定——
    这是整套记忆系统可测试、可回放、可回滚的根。
    """

    op_id: str
    action: str                      # upsert / delete / erase
    key: str
    value: Any = None
    type: str = MEM_TYPE_PREFERENCE
    source: str = SOURCE_EXPLICIT
    confidence: float = 1.0
    evidence: List[str] = field(default_factory=list)
    actor: str = "用户"
    user_id: str = ""
    session_id: str = ""
    ts: float = 0.0
    reason: str = ""                 # Guard/Resolver 写的处置原因码（审计用）

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryItem:
    """一条（当前有效的）记忆条目。Redis 热层与 PG 真相层保持同构。"""

    key: str
    value: Any
    type: str = MEM_TYPE_PREFERENCE
    version: int = 1
    status: str = STATUS_VALID
    valid_from: float = 0.0
    valid_to: Optional[float] = None
    recorded_at: float = 0.0
    updated_at: float = 0.0
    source: str = SOURCE_EXPLICIT
    confidence: float = 1.0
    evidence: List[str] = field(default_factory=list)
    pii_level: str = PII_INTERNAL
    content_hash: str = ""
    user_id: str = ""
    session_id: str = ""

    def __post_init__(self) -> None:
        now = time.time()
        if not self.recorded_at:
            self.recorded_at = now
        if not self.updated_at:
            self.updated_at = now
        if not self.valid_from:
            self.valid_from = now
        if not self.content_hash:
            self.content_hash = content_hash(self.value)
