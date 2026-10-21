"""凭证清单（多模型 / 多工具 Key）+ 失败自动降级内核。

设计目标
--------
1. **清单化**：模型与工具各维护一张凭证表（落盘 ``data/credentials.json``），
   每条含 名称 / 服务 / 凭证字段 / 启用 / 优先级；密钥只存本机，对外一律脱敏。
2. **自动降级**：调用按 priority 从小到大依次尝试，某条失败（异常或返回空）
   自动切下一条；成功的条目标记健康，失败的记入失败计数并进入冷却期。
3. **向后兼容**：清单为空时，从 ``settings``（.env 单项配置）自动迁移为首条凭证，
   旧的配置方式继续可用。

不编造原则
----------
- 凭证全部来自用户配置或 .env，**不会自动生成任何 Key**；
- 全部候选失败时如实返回失败原因链（attempts），不伪造结果。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("sca.credentials")

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "credentials.json"

MAX_FAILS = 3          # 连续失败达到该次数 -> 进入冷却
COOLDOWN_S = 300.0     # 冷却时长（秒），冷却期内跳过该凭证

_LOCK = threading.Lock()

KIND_MODEL = "model"
KIND_TOOL = "tool"

# 服务 -> 该服务凭证包含哪些字段（用于校验与脱敏）
MODEL_FIELDS = ("base_url", "api_key", "intent_model", "reason_model",
                "embedding_model", "timeout")
TOOL_SERVICES = {
    "amap": ("key",),
    "qweather": ("key", "host"),
    "dingtalk": ("webhook", "secret"),
}
SECRET_FIELDS = {"api_key", "key", "secret", "webhook"}


def _default_data() -> Dict[str, Any]:
    return {"models": [], "tools": [], "health": {}}


def _load_raw() -> Dict[str, Any]:
    if _PATH.is_file():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("models", [])
                data.setdefault("tools", [])
                data.setdefault("health", {})
                return data
        except Exception as exc:
            logger.warning("[credentials] 读取失败，回退空清单：%s", exc)
    return _default_data()


def _save_raw(data: Dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) <= 12:
        return "*" * len(v)
    return f"{v[:6]}…{v[-4:]}"


def _section(kind: str) -> str:
    return "models" if kind == KIND_MODEL else "tools"


# ---------------------------------------------------------------------------
# 迁移：把 .env / settings 里的单项配置补成清单首条
# ---------------------------------------------------------------------------
def _seed_from_settings() -> None:
    try:
        from sca.settings import settings
    except Exception:  # noqa
        return
    data = _load_raw()
    changed = False

    if not data["models"]:
        key = (getattr(settings, "dashscope_api_key", "") or "").strip()
        if key:
            data["models"].append({
                "id": "m-default", "name": "默认模型（来自 .env）", "service": "llm",
                "enabled": True, "priority": 10,
                "data": {"base_url": getattr(settings, "dashscope_base_url", ""),
                         "api_key": key,
                         "intent_model": getattr(settings, "intent_model", ""),
                         "reason_model": getattr(settings, "reason_model", ""),
                         "embedding_model": getattr(settings, "embedding_model", ""),
                         "timeout": getattr(settings, "intent_timeout", 5.0)},
            })
            changed = True

    have = {t.get("service") for t in data["tools"]}
    for service, attrs in (("amap", ("amap_key",)),
                           ("qweather", ("qweather_key", "qweather_host")),
                           ("dingtalk", ("dingtalk_webhook", "dingtalk_secret"))):
        if service in have:
            continue
        if service == "amap":
            key = (getattr(settings, "amap_key", "") or "").strip()
            payload = {"key": key}
        elif service == "qweather":
            key = (getattr(settings, "qweather_key", "") or "").strip()
            if not key:
                continue
            payload = {"key": key, "host": getattr(settings, "qweather_host", "") or ""}
        else:
            wh = (getattr(settings, "dingtalk_webhook", "") or "").strip()
            sec = (getattr(settings, "dingtalk_secret", "") or "").strip()
            if not (wh and sec):
                continue
            payload = {"webhook": wh, "secret": sec}
        if service == "amap" and not key:
            continue
        data["tools"].append({
            "id": f"t-{service}-default", "name": f"{service}（来自 .env）",
            "service": service, "enabled": True, "priority": 10, "data": payload,
        })
        changed = True

    if changed:
        _save_raw(data)
        logger.info("[credentials] 已从 .env 迁移既有凭证为清单首条")


# ---------------------------------------------------------------------------
# 清单 CRUD
# ---------------------------------------------------------------------------
def list_credentials(kind: str, service: Optional[str] = None,
                     masked: bool = True) -> List[Dict[str, Any]]:
    """列出凭证（默认脱敏）。"""
    with _LOCK:
        _seed_from_settings()
        data = _load_raw()
        items = [c for c in data[_section(kind)]
                 if service is None or c.get("service") == service]
        items.sort(key=lambda c: (c.get("priority", 100), c.get("name", "")))
        health = data.get("health", {})
        out = []
        for c in items:
            d = dict(c)
            dd = dict(c.get("data") or {})
            if masked:
                for f in list(dd.keys()):
                    if f in SECRET_FIELDS:
                        dd[f + "_set"] = bool(dd.get(f))
                        dd[f] = _mask(str(dd.get(f) or ""))
            d["data"] = dd
            h = health.get(c.get("id", ""), {})
            d["health"] = {
                "fail_count": h.get("fail_count", 0),
                "last_error": h.get("last_error", ""),
                "cooling": _is_cooling(h),
                "last_ok_ts": h.get("last_ok_ts"),
                "last_fail_ts": h.get("last_fail_ts"),
            }
            out.append(d)
        return out


def get_credential(kind: str, cid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load_raw()
        for c in data[_section(kind)]:
            if c.get("id") == cid:
                return c
    return None


def upsert_credential(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新一条凭证；返回脱敏后的记录。"""
    if not isinstance(payload, dict) or not (payload.get("name") or "").strip():
        return {"ok": False, "error": "名称 name 必填"}
    service = (payload.get("service") or "").strip()
    if kind == KIND_TOOL and service not in TOOL_SERVICES:
        return {"ok": False, "error": f"未知工具服务：{service or '空'}，可选 {list(TOOL_SERVICES)}"}
    data_in = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # 兼容：字段也可平铺在 payload 上
    merged = dict(data_in)
    for f in (MODEL_FIELDS if kind == KIND_MODEL
              else TOOL_SERVICES.get(service, ())):
        if f in payload and f not in merged:
            merged[f] = payload[f]
    # 空字符串表示"不修改既有密钥"
    cid = (payload.get("id") or "").strip()

    with _LOCK:
        _seed_from_settings()
        data = _load_raw()
        items = data[_section(kind)]
        rec = next((c for c in items if c.get("id") == cid), None)
        if rec is None:
            rec = {"id": cid or f"{'m' if kind == KIND_MODEL else 't'}-{uuid.uuid4().hex[:8]}",
                   "name": payload["name"].strip(), "service": service or "llm",
                   "enabled": bool(payload.get("enabled", True)),
                   "priority": int(payload.get("priority", 100) or 100),
                   "data": {}}
            items.append(rec)
        else:
            if payload.get("name"):
                rec["name"] = payload["name"].strip()
            if service:
                rec["service"] = service
            if "enabled" in payload:
                rec["enabled"] = bool(payload["enabled"])
            if payload.get("priority") is not None:
                rec["priority"] = int(payload["priority"])
        old = rec.get("data") or {}
        for k, v in merged.items():
            if v is None:
                continue
            if isinstance(v, str) and v.strip() == "" and old.get(k):
                continue  # 留空 = 保持原值
            old[k] = v
        rec["data"] = old
        _save_raw(data)
        return {"ok": True, "credential": _masked(rec, data.get("health", {}))}


def delete_credential(kind: str, cid: str) -> Dict[str, Any]:
    with _LOCK:
        data = _load_raw()
        items = data[_section(kind)]
        before = len(items)
        data[_section(kind)] = [c for c in items if c.get("id") != cid]
        data.get("health", {}).pop(cid, None)
        if len(data[_section(kind)]) == before:
            return {"ok": False, "error": "未找到该凭证"}
        _save_raw(data)
        return {"ok": True, "id": cid}


def _masked(rec: Dict[str, Any], health: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(rec)
    dd = {}
    for f, v in (rec.get("data") or {}).items():
        dd[f] = _mask(str(v or "")) if f in SECRET_FIELDS else v
        if f in SECRET_FIELDS:
            dd[f + "_set"] = bool(v)
    d["data"] = dd
    h = health.get(rec.get("id", ""), {})
    d["health"] = {"fail_count": h.get("fail_count", 0),
                   "last_error": h.get("last_error", ""),
                   "cooling": _is_cooling(h),
                   "last_ok_ts": h.get("last_ok_ts"),
                   "last_fail_ts": h.get("last_fail_ts")}
    return d


# ---------------------------------------------------------------------------
# 健康状态与冷却
# ---------------------------------------------------------------------------
def _is_cooling(h: Dict[str, Any]) -> bool:
    if int(h.get("fail_count", 0) or 0) < MAX_FAILS:
        return False
    ts = float(h.get("last_fail_ts", 0) or 0)
    return (time.time() - ts) < COOLDOWN_S


def mark_ok(kind: str, cid: str) -> None:
    with _LOCK:
        data = _load_raw()
        h = data.setdefault("health", {}).setdefault(cid, {})
        if h.get("fail_count") or h.get("last_error"):
            h["fail_count"] = 0
            h["last_error"] = ""
            h["last_ok_ts"] = time.time()
            _save_raw(data)
        else:
            h["last_ok_ts"] = time.time()


def mark_fail(kind: str, cid: str, error: str) -> None:
    with _LOCK:
        data = _load_raw()
        h = data.setdefault("health", {}).setdefault(cid, {})
        h["fail_count"] = int(h.get("fail_count", 0) or 0) + 1
        h["last_error"] = str(error)[:200]
        h["last_fail_ts"] = time.time()
        _save_raw(data)
    logger.warning("[credentials] %s %s 调用失败(%d/%d)：%s",
                   kind, cid, h["fail_count"], MAX_FAILS, str(error)[:120])


def reset_health() -> Dict[str, Any]:
    with _LOCK:
        data = _load_raw()
        n = len(data.get("health", {}))
        data["health"] = {}
        _save_raw(data)
    return {"ok": True, "reset": n}


# ---------------------------------------------------------------------------
# 候选与降级执行
# ---------------------------------------------------------------------------
def candidates(kind: str, service: Optional[str] = None,
               include_cooling: bool = True) -> List[Dict[str, Any]]:
    """按优先级返回候选凭证（含冷却标记，调用方决定是否跳过）。"""
    with _LOCK:
        _seed_from_settings()
        data = _load_raw()
        health = data.get("health", {})
        items = [c for c in data[_section(kind)]
                 if c.get("enabled", True)
                 and (service is None or c.get("service") == service)]
        items.sort(key=lambda c: (c.get("priority", 100), c.get("name", "")))
        if include_cooling:
            return items
        return [c for c in items if not _is_cooling(health.get(c.get("id", ""), {}))]


def _usable(cred: Dict[str, Any]) -> bool:
    """凭证是否具备最低可用字段（避免空 Key 混入调用链）。"""
    d = cred.get("data") or {}
    if cred.get("service") == "llm" or "api_key" in d:
        return bool((d.get("api_key") or "").strip())
    svc = cred.get("service")
    if svc == "dingtalk":
        return bool((d.get("webhook") or "").strip() and (d.get("secret") or "").strip())
    return bool((d.get("key") or "").strip())


async def run_chain(kind: str, service: Optional[str],
                    fn: Callable[[Dict[str, Any]], Any],
                    attempts_limit: int = 0) -> Tuple[Any, Optional[Dict[str, Any]], List[Dict]]:
    """按清单顺序依次尝试，失败自动降级到下一条。

    ``fn`` 可为同步/异步函数，签名 ``fn(cred) -> result``；
    返回 ``None`` 或抛出异常均视为该凭证失败，自动切换下一条。

    返回 ``(result, used_credential, attempts)``，全部失败时 result 为 None。
    """
    import asyncio
    import inspect

    items = [c for c in candidates(kind, service) if _usable(c)]
    if attempts_limit:
        items = items[:attempts_limit]
    attempts: List[Dict[str, Any]] = []
    last: Optional[Dict[str, Any]] = None
    result = None
    for cred in items:
        cid = cred.get("id", "")
        try:
            r = fn(cred)
            if inspect.isawaitable(r):
                r = await r
        except Exception as exc:  # noqa: 该凭证失败 -> 换下一条
            mark_fail(kind, cid, str(exc))
            attempts.append({"id": cid, "name": cred.get("name"), "ok": False,
                             "error": str(exc)[:200]})
            last = cred
            continue
        if r is None:
            mark_fail(kind, cid, "返回空结果")
            attempts.append({"id": cid, "name": cred.get("name"), "ok": False,
                             "error": "返回空结果"})
            last = cred
            continue
        mark_ok(kind, cid)
        attempts.append({"id": cid, "name": cred.get("name"), "ok": True})
        result, last = r, cred
        break
    return result, (last if result is not None else None), attempts


def first_credential(service: str) -> Optional[Dict[str, Any]]:
    """取该服务当前可用的第一条凭证（优先跳过冷却中的），无则返回 None。"""
    try:
        items = [c for c in candidates(KIND_TOOL, service, include_cooling=False)
                 if _usable(c)]
        if items:
            return items[0]
        items = [c for c in candidates(KIND_TOOL, service) if _usable(c)]
        return items[0] if items else None
    except Exception:  # noqa: 清单不可用 -> 由调用方回退 settings
        return None


def summary() -> Dict[str, Any]:
    """供 /api/health 与前端展示的清单概览（脱敏）。"""
    models = list_credentials(KIND_MODEL)
    tools = list_credentials(KIND_TOOL)
    return {
        "models": models,
        "tools": tools,
        "model_count": len(models),
        "tool_count": len(tools),
        "model_ready": any(m.get("enabled", True) and not m["health"]["cooling"] for m in models),
        "degrade_enabled": True,
        "max_fails": MAX_FAILS,
        "cooldown_s": COOLDOWN_S,
    }
