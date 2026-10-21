"""全局配置（环境变量驱动，前缀 SCA_；.env 自动加载）。

所有可调参数集中在此，业务方/部署方只需改 .env 即可灵活配置，无需动代码。
两套实现：装了 pydantic_settings 用 BaseSettings（自动读 .env）；未装则用标准库
os.environ + 手写 .env 解析（零依赖回退），保证离线也能读到 .env 里的 Key 与参数。
"""
from __future__ import annotations

import os


def _env(key: str, default: str) -> str:
    return os.environ.get(f"SCA_{key}", default)


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(f"SCA_{key}")
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(f"SCA_{key}")
    if v is None:
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(f"SCA_{key}")
    if v is None:
        return default
    try:
        return float(v.strip())
    except ValueError:
        return default


def _load_dotenv() -> None:
    """零依赖回退：未装 pydantic_settings 时，手动把 .env 注入 os.environ。

    仅 setdefault（不覆盖已存在的环境变量），路径优先 cwd/.env 与项目根 .env。
    """
    try:
        from pathlib import Path

        candidates = [Path.cwd() / ".env",
                      Path(__file__).resolve().parent.parent / ".env"]
        for p in candidates:
            if p.is_file():
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
                break
    except Exception:  # noqa: 解析失败不影响主流程
        pass


try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Settings(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=".env", env_prefix="SCA_", extra="ignore", case_sensitive=False
        )
        # —— 模型（阿里云百炼 / DashScope，OpenAI 兼容）——
        dashscope_api_key: str = ""
        dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        intent_model: str = "tongyi-intent-detect-v3"
        reason_model: str = "qwen-plus"
        embedding_model: str = "text-embedding-v3"
        intent_timeout: float = 5.0  # 意图模型调用超时（秒）

        # —— 存储 ——
        pg_dsn: str = "postgresql://sca:sca@localhost:5432/sca"
        use_pg: bool = False

        # —— 长短期记忆（sca.memory）——
        # 写入异步：op 先写穿透 Redis（请求内完成，保证会话内读个人写）+ append WAL
        # stream，worker 再 fold 进 PG；Redis 不可达时自动降级 pg_direct（同步落 PG），
        # 正确性不变、延迟升一档。use_redis=false 可强制 PG-only。
        memory_enabled: bool = True
        use_redis: bool = True
        redis_url: str = "redis://127.0.0.1:6379/0"
        memory_wal_stream: str = "mem:wal"        # WAL stream 键名
        memory_wal_group: str = "memfold"         # fold worker 消费组
        memory_pinned_keys: str = ("profile.display_name,"
                                   "preference.notify_channel")  # 固定块，永不淘汰
        memory_context_budget: int = 1024         # 每轮记忆注入的近似 token 预算

        # —— 外部真实数据源（留空用内置档）——
        dingtalk_webhook: str = ""   # 钉钉群机器人 webhook（notify-mcp 真实推送）
        dingtalk_secret: str = ""    # 钉钉加签 secret
        qweather_key: str = ""       # 和风天气 API Key（weather-mcp 真实预报/预警）
        qweather_host: str = ""      # 和风专属 API Host（新版账号需要，形如 abc.re.qweatherapi.com）
        amap_key: str = ""           # 高德 Web 服务 Key（map-mcp 真实路径规划，白名单内合规地图服务）

        # —— 仿真（原型期只做模拟论证）——
        sim_fidelity: str = "F1"  # F1 全合成 / F2 回放增强 / F3 影子模式
        baseline_auto_success_rate: float = 0.0  # =0 时用 baseline.py 默认；>0 覆盖自动处置成功率假设

        # —— Web 服务（uvicorn 启动相关，均可经 .env 调整）——
        host: str = "0.0.0.0"
        port: int = 8000
        log_level: str = "info"
        api_prefix: str = "/api"  # 所有 JSON 接口统一前缀，便于前后端分离与网关路由
        frontend_dir: str = ""    # 前端静态目录；为空则取 <项目根>/frontend
        cors_origins: str = "*"  # 逗号分隔的允许跨域来源；* 为全部（前后端分离开发用）

        # —— 流程开关 ——
        hitl_enabled: bool = True  # 关闭后 P0/P1 不再挂起，自动审批通过（演示/压测用）
        dispatch_review_enabled: bool = True  # 调度结果经调度员审核后才发钉钉给司机/收货方

        # —— 有界自主（Supervisor）——
        # 图超步上限。官方 langgraph 的 recursion_limit 默认 10007（≈无上限，且 api.py
        # 此前从未设置过）；垫片读同一个环境变量，保证两条引擎在同一超步数处中断。
        recursion_limit: int = 200
        supervisor_mode: str = "off"      # off 关闭 / shadow 只记录不生效 / on 生效
        supervisor_max_rounds: int = 12   # supervisor 最多决策轮次（有界自主的硬上限）

    settings = Settings()
except Exception:  # 未安装 pydantic_settings -> 标准库回退
    _load_dotenv()  # 手动注入 .env，保证零依赖也能读到 Key 与参数

    class Settings:
        dashscope_api_key: str = _env("DASHSCOPE_API_KEY", "")
        dashscope_base_url: str = _env(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        intent_model: str = _env("INTENT_MODEL", "tongyi-intent-detect-v3")
        reason_model: str = _env("REASON_MODEL", "qwen-plus")
        embedding_model: str = _env("EMBEDDING_MODEL", "text-embedding-v3")
        intent_timeout: float = _env_float("INTENT_TIMEOUT", 5.0)

        pg_dsn: str = _env("PG_DSN", "postgresql://sca:sca@localhost:5432/sca")
        use_pg: bool = _env_bool("USE_PG", False)

        memory_enabled: bool = _env_bool("MEMORY_ENABLED", True)
        use_redis: bool = _env_bool("USE_REDIS", True)
        redis_url: str = _env("REDIS_URL", "redis://127.0.0.1:6379/0")
        memory_wal_stream: str = _env("MEMORY_WAL_STREAM", "mem:wal")
        memory_wal_group: str = _env("MEMORY_WAL_GROUP", "memfold")
        memory_pinned_keys: str = _env("MEMORY_PINNED_KEYS",
                                       "profile.display_name,preference.notify_channel")
        memory_context_budget: int = _env_int("MEMORY_CONTEXT_BUDGET", 1024)

        dingtalk_webhook: str = _env("DINGTALK_WEBHOOK", "")
        dingtalk_secret: str = _env("DINGTALK_SECRET", "")
        qweather_key: str = _env("QWEATHER_KEY", "")
        qweather_host: str = _env("QWEATHER_HOST", "")
        amap_key: str = _env("AMAP_KEY", "")

        sim_fidelity: str = _env("SIM_FIDELITY", "F1")
        baseline_auto_success_rate: float = _env_float("BASELINE_AUTO_SUCCESS_RATE", 0.0)

        host: str = _env("HOST", "0.0.0.0")
        port: int = _env_int("PORT", 8000)
        log_level: str = _env("LOG_LEVEL", "info")
        api_prefix: str = _env("API_PREFIX", "/api")
        frontend_dir: str = _env("FRONTEND_DIR", "")
        cors_origins: str = _env("CORS_ORIGINS", "*")

        hitl_enabled: bool = _env_bool("HITL_ENABLED", True)
        dispatch_review_enabled: bool = _env_bool("DISPATCH_REVIEW_ENABLED", True)

        # —— 有界自主（Supervisor）——
        recursion_limit: int = _env_int("RECURSION_LIMIT", 200)
        supervisor_mode: str = _env("SUPERVISOR_MODE", "off")
        supervisor_max_rounds: int = _env_int("SUPERVISOR_MAX_ROUNDS", 12)

    settings = Settings()
