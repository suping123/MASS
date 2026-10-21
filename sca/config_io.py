"""运行时配置读写：把"系统设置"界面的修改写回 .env 并热更新 settings。

前端"系统设置"页保存时调用 update_config()：
  1. 持久化到项目根 .env（保留注释与其它键，只更新/追加目标键）；
  2. 同步 os.environ（供零依赖回退路径后续读取）；
  3. 就地修改 settings 实例属性（pydantic 与标准库回退两套都生效），
     因此无需重启进程即可让新配置对后续请求生效（意图模型在每次调用时重建）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from sca.settings import settings

# settings 属性 -> (env 键, 类型)
_FIELDS: Dict[str, tuple] = {
    "dashscope_api_key": ("SCA_DASHSCOPE_API_KEY", "str"),
    "dashscope_base_url": ("SCA_DASHSCOPE_BASE_URL", "str"),
    "intent_model": ("SCA_INTENT_MODEL", "str"),
    "reason_model": ("SCA_REASON_MODEL", "str"),
    "embedding_model": ("SCA_EMBEDDING_MODEL", "str"),
    "intent_timeout": ("SCA_INTENT_TIMEOUT", "float"),
    "hitl_enabled": ("SCA_HITL_ENABLED", "bool"),
    "dispatch_review_enabled": ("SCA_DISPATCH_REVIEW_ENABLED", "bool"),
    "baseline_auto_success_rate": ("SCA_BASELINE_AUTO_SUCCESS_RATE", "float"),
    "host": ("SCA_HOST", "str"),
    "port": ("SCA_PORT", "int"),
    "log_level": ("SCA_LOG_LEVEL", "str"),
    "api_prefix": ("SCA_API_PREFIX", "str"),
    "cors_origins": ("SCA_CORS_ORIGINS", "str"),
    "frontend_dir": ("SCA_FRONTEND_DIR", "str"),
    "use_pg": ("SCA_USE_PG", "bool"),
    "pg_dsn": ("SCA_PG_DSN", "str"),
    "dingtalk_webhook": ("SCA_DINGTALK_WEBHOOK", "str"),
    "dingtalk_secret": ("SCA_DINGTALK_SECRET", "str"),
    "qweather_key": ("SCA_QWEATHER_KEY", "str"),
    "qweather_host": ("SCA_QWEATHER_HOST", "str"),
    "amap_key": ("SCA_AMAP_KEY", "str"),
}


def _env_path() -> Path:
    # 与 settings._load_dotenv 的候选路径保持一致（优先项目根）
    return Path(__file__).resolve().parent.parent / ".env"


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(str(value).strip())
    if kind == "float":
        return float(str(value).strip())
    return str(value)


def _to_env_str(value: Any, kind: str) -> str:
    if kind == "bool":
        return "true" if _coerce(value, "bool") else "false"
    return str(value)


def read_env() -> Dict[str, str]:
    """解析 .env 为 dict（不含注释）。"""
    out: Dict[str, str] = {}
    p = _env_path()
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def update_config(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """更新一组 settings 属性：写回 .env + 热更新 settings。返回生效后的配置快照。

    仅接受 _FIELDS 白名单内的键，忽略未知键，避免任意注入。
    """
    applied: Dict[str, Any] = {}
    env_updates: Dict[str, str] = {}
    for attr, value in mapping.items():
        if attr not in _FIELDS or value is None:
            continue
        env_key, kind = _FIELDS[attr]
        try:
            coerced = _coerce(value, kind)
        except (ValueError, TypeError):
            continue
        # 热更新 settings 实例
        try:
            setattr(settings, attr, coerced)
        except Exception:  # noqa
            pass
        os.environ[env_key] = _to_env_str(coerced, kind)
        env_updates[env_key] = _to_env_str(coerced, kind)
        applied[attr] = coerced

    # 写回 .env：替换已存在的键，追加缺失的键
    p = _env_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in env_updates:
                new_lines.append(f"{k}={env_updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in env_updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return applied


def masked_config() -> Dict[str, Any]:
    """返回给前端的当前配置（API Key 打码，不回传明文）。"""
    key = getattr(settings, "dashscope_api_key", "") or ""
    masked = (key[:7] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else "")
    dt_wh = getattr(settings, "dingtalk_webhook", "") or ""
    dt_sec = getattr(settings, "dingtalk_secret", "") or ""
    qw = getattr(settings, "qweather_key", "") or ""
    amap = getattr(settings, "amap_key", "") or os.environ.get("SCA_AMAP_KEY", "").strip()
    return {
        "dashscope_api_key_set": bool(key),
        "dashscope_api_key_masked": masked,
        "dashscope_base_url": getattr(settings, "dashscope_base_url", ""),
        "intent_model": getattr(settings, "intent_model", ""),
        "reason_model": getattr(settings, "reason_model", ""),
        "embedding_model": getattr(settings, "embedding_model", ""),
        "intent_timeout": getattr(settings, "intent_timeout", 5.0),
        "hitl_enabled": getattr(settings, "hitl_enabled", True),
        "dispatch_review_enabled": getattr(settings, "dispatch_review_enabled", True),
        "baseline_auto_success_rate": getattr(settings, "baseline_auto_success_rate", 0.0),
        "use_pg": getattr(settings, "use_pg", False),
        "model_available": bool(key),
        "dingtalk_configured": bool(dt_wh and dt_sec),
        "qweather_configured": bool(qw),
        "qweather_key_masked": (qw[:6] + "…" + qw[-4:]) if len(qw) > 12
                               else ("已配置" if qw else ""),
        "qweather_host": getattr(settings, "qweather_host", ""),
        "amap_configured": bool(amap),
        "amap_key_masked": (amap[:6] + "…" + amap[-4:]) if len(amap) > 12
                           else ("已配置" if amap else ""),
    }
