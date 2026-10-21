# 供应链多智能体调度与异常处置系统

面向干线运输全链路的智能调度与异常处置平台，覆盖**运力智能匹配、实时路径规划、在途异常自动处置、履约复盘优化**四大环节。系统采用「大模型 + RAG + MCP + LangGraph」混合架构，以多智能体协作承载跨角色业务流转，以状态机编排单票处置主链，为货主、调度、承运商三方提供统一工作台。

## 系统定位

- **货主端**：自然语言下单，结构化解析运单，全程可视化跟踪订单状态。
- **调度平台**：运力匹配、路径规划、定价挂牌、招标裁决、异常分级与处置，高危场景人工审批。
- **承运商端**：订单池报价、接单、竞价参与，成本与收益透明。

系统以确定性规则为骨架、大模型为增强，任何外部能力不可用时均自动降级到可用的本地通道，保证业务主链不中断。

## 技术栈

| 层 | 技术选型 |
| --- | --- |
| 编排 | LangGraph StateGraph（单票处置状态机，检查点持久化 + 人工审批中断） |
| 多智能体 | 独立 Agent 进程 + 消息总线协作，私有信息隔离 |
| 工具接入 | MCP（Model Context Protocol）标准化封装异构数据源 |
| 模型 | Qwen 系列（推理）、tongyi-intent-detect-v3（意图）、text-embedding-v3（向量） |
| 存储 | MySQL（业务主库）、Redis（登录态 / 门铃 / 记忆热层）、PostgreSQL + pgvector（知识库向量检索） |
| 服务 | FastAPI + Uvicorn（纯 JSON REST，`/api` 前缀） |
| 外部数据 | TMS / GPS / 气象 / 高精地图（MCP 统一接入，凭证清单化管理 + 自动降级） |

## 核心能力

### 多智能体协作

平台层由四个具备独立目标与私有视图的决策型 Agent 构成，通过消息总线协作，而非单一控制流：

| Agent | 职责 | 私有视图 | 信息边界 |
| --- | --- | --- | --- |
| 货主 Agent | 下单、订单跟踪 | 自有订单（含预算） | 看不到承运商成本 |
| 调度 Agent | 定价挂牌、招标裁决、异常处置 | 全量订单 + 方案 + 报价 | 不替人签字，高危需人工审批 |
| 承运商 Agent | 报价、接单、竞价 | 订单池 + 自有报价 | 看不到货主预算与原始描述 |
| 异常 Agent | 异常分级、SOP 检索 | 描述 + 货类 + 吨位 + SOP | 不看任何金额，防止定级被利润污染 |

两条硬约束由工程机制保障：

1. **信息隔离**：`views.ROLE_FIELDS` 白名单投影，越权字段读取即抛 `IsolationError`；`check_isolation.py` 静态扫描拦截越权字段与 Agent 间直接 import。
2. **只经消息通信**：`bus.py` + `contract.py` 定义消息类型、必填字段与通信方向白名单，Agent 之间不直接调用对方实现，也不读对方记忆。

写操作由常驻 Agent 进程消费消息后执行，人只投 `invoke` 委托、HTTP 等待回执。Agent 进程未运行时，成交类写接口返回 504 且数据库无残留——执行权真实下沉到 Agent，而非"单进程多角色"。

### 招标竞价与裁决

- **挂牌即开标**：定价挂牌与招标单在同一事务内创建，招标状态独立于订单状态机。
- **多轮竞价**：承运商报价（运费口径），调度按「价格 / 时效 / 评分 / 空驶」加权比价，综合分最高者中标，而非简单最低价。
- **协商还价**：超预算但差距 ≤15% 进入多轮协商，调度只能重申上限（无加价权，提价属人工审批项），承运商按自身成本底线回应，轮次有上限，谈不拢升级货主并留痕。
- **可解释裁决**：比价表（报价 / 时效 / 评分 / 综合分 / 淘汰原因）、裁决权重、还价逐轮轨迹、Agent 消息回放，完整还原"为什么是这家"。

自动裁决由调度 Agent 的定时任务驱动（`due()` 每 15s 扫描到期标的），手动强制截止与自动裁决走同一份证据链。

### 在途异常处置

- **故障谱分级**：44 类异常 + 15 类 SOP，覆盖 14 个子系统，P0–P3 分级。
- **五类处置动作**：恢复在途（`resume`）、改道重规划（`replan`）、改期（`extend`）、送达（`deliver`）、改派（`reassign`），均有真实业务实现。
- **改道可校验**：剔除受影响城市后按 polyline 采样点校验是否真实绕开，绕不开则明确拒绝，不宣称已改道。
- **高危审批（HITL）**：P0/P1 异常在审批闸口挂起，人工通过后从原中断点继续，审批驳回则不下发。
- **不编造**：未命中故障谱标 `E-UNKNOWN` 转人工，不随机注入异常；无定位信息不虚拟绕行里程。

### 知识库与 RAG

异常处置知识库基于 PostgreSQL + pgvector：

- **混合检索**：向量语义召回 + 中文 2-gram 关键词召回 + 融合重排。
- **重排序**：对召回候选做二次打分（覆盖率、命中密度、词项邻近度、结构化字段命中），提升故障案例与 SOP 的匹配精度；支持本地确定性重排与模型交叉编码重排两档，外部模型不可用时自动降级。
- **熔断降级**：PG 不可用时回退关键词检索并熔断 30s，避免每次回退都付一次连接超时 + embedding 往返。

### 规划—执行—反思状态机

单票处置主链由 LangGraph StateGraph 编排，三层循环：

- **规划层**：订单受理 → 运力匹配 → 路径规划 → 高危审批，路径规划与前置取证并行扇出、仲裁汇聚。
- **执行层**：下发 → 监控 → 异常检测 → 处置 / 重规划。
- **反思层**：复盘 → 指标 → 知识沉淀，形成处置经验闭环。

检查点持久化（PG 优先、内存回退）支持审批现场跨进程 / 重启恢复。

## 目录结构

```
supply-chain-agent/
├── requirements.txt          # 依赖清单
├── .env.example              # 配置样例
├── sca/
│   ├── platform/             # 三端业务平台（货主 / 调度 / 承运商）
│   │   ├── main.py           # 唯一入口 sca.platform.main:app（端口 8000）
│   │   ├── agents/           # 多智能体：对话状态机 + 常驻 daemon + 消息总线
│   │   ├── services/         # 业务服务：订单 / 调度 / 定价 / 招标 / 异常 / 审计
│   │   ├── routers/          # 四套路由前缀 /api/shipper|dispatch|carrier|admin
│   │   └── models.py         # SQLAlchemy 数据模型
│   ├── agents/               # 内核状态机节点 + 图编译（LangGraph StateGraph）
│   ├── mcp/                  # MCP Server + 客户端 + 注册表
│   ├── rag/                  # 混合检索 + 重排序
│   ├── intent/               # 意图识别与工具路由（规则优先 + 模型兜底）
│   ├── intake.py             # 自然语言需求受理 → 结构化运单
│   ├── knowledge_base.py     # SOP 知识库
│   ├── simulation/           # 仿真数据引擎 + 故障谱 + 评估集
│   └── eval/                 # 评估引擎（L0 工程实测 / L1 仿真论证）
├── frontend_v2/              # 三端前端（login / shipper / carrier / dispatch）
├── sql/                      # 数据库脚本（schema + 平台迭代迁移）
└── scripts/                  # 自检 / 种子 / 迁移 / 冒烟脚本
```

## 快速开始

### 环境要求

- MySQL 8.0（业务主库，端口 3306）
- Redis（登录态 / 门铃 / 记忆热层，端口 6379）
- PostgreSQL + pgvector（知识库向量检索，端口 5432，可选，缺失时自动降级关键词检索）

### 启动

```bat
start_all.bat
```

脚本会先检查 MySQL 与 Redis：任一不可用即报错退出，不会起一个"页面能打开但无法使用"的服务。

首次初始化（只需一次）：

```bat
mysql -uroot -p --default-character-set=utf8mb4 < sql/schema.sql
.venv\Scripts\python.exe -m sca.platform.seed
```

启动多智能体（四个常驻 Agent 进程，负责消费写操作与定时裁决）：

```bat
start_agents.bat
```

演示账号（密码均为 `123456`）：`shipper1` / `dispatcher1` / `carrier1`。

### 服务入口

| 入口 | 端口 | 说明 |
| --- | --- | --- |
| `sca.platform.main:app` | 8000 | 三端业务平台（当前唯一入口） |

## 自检

系统提供分层自检，覆盖真实依赖冒烟、逻辑回归、静态隔离扫描与前端契约校验：

```bat
.venv\Scripts\python.exe scripts\smoke_platform.py          # 59 项：真实 HTTP + MySQL/Redis/PG
.venv\Scripts\python.exe scripts\test_platform_multiagent.py # 34 项：信息边界 / 契约 / 记忆 / 总线
.venv\Scripts\python.exe scripts\test_platform_reroute.py    # 42 项：改道 / 改期 / 改派
.venv\Scripts\python.exe scripts\test_platform_agent_flow.py # 20 项：协作链
.venv\Scripts\python.exe scripts\test_bidding_board.py       # 75 项：竞价看板 / 比价 / 还价 / 手动裁决
.venv\Scripts\python.exe scripts\test_cargo_dialog.py        # 47 项：货类识别 / 意图 / 追问策略
.venv\Scripts\python.exe scripts\test_rag_rerank.py          # 23 项：检索重排序
.venv\Scripts\python.exe scripts\check_isolation.py          # 静态闸门：信息隔离
.venv\Scripts\python.exe scripts\check_frontend.py           # 前端契约
```

## 指标体系

- **L0 工程实测**：异常重规划 p95 < 500ms、工具调用准确率 95%、SOP 匹配 91%、故障谱覆盖 ≥12 子系统。
- **L1 仿真论证**：调度效率 +70%、异常响应 30min → 3min、自愈率 82%、单票成本 -12%、准时率 96.5%、赔付率 -42%、接入周期 -60%。
- **L2 远期目标**：新调度员上岗周期、团队人效提升。

## 配置

所有参数以 `SCA_` 前缀通过环境变量配置，`.env` 一键覆盖。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCA_HITL_ENABLED` | `true` | 人工审批中断（P0/P1 挂起） |
| `SCA_DASHSCOPE_API_KEY` | 空 | 百炼 Key，配后启用真实意图识别与向量召回 |
| `SCA_INTENT_MODEL` | `tongyi-intent-detect-v3` | 意图识别模型 |
| `SCA_USE_PG` | `false` | 启用 PG/pgvector 检查点 + 向量检索 |
| `SCA_AMAP_KEY` | 空 | 高德路径规划 + 天气预报（共用一个 Key） |
| `SCA_SUPERVISOR_MODE` | `off` | 规划段调度方式（off / shadow / on） |
| `SCA_RAG_RERANK` | `local` | 检索重排序档位（off / local / api） |

模型与工具凭证采用清单化管理：同一服务可配多条凭证，按优先级调用，失败自动切换下一条，连续失败进入冷却，无需重启。

## 数据安全与合规

- **信息隔离**：货主预算不下发承运商，承运商成本不回流货主，三个 Agent 各有独立数据库账号入口，越权在数据库层不可行。
- **脱敏**：手机号、API Key、Secret 在落盘与回显时一律脱敏，完整号码仅在本机与消息渠道内流转。
- **审计**：订单每次状态变更先写只追加的 `order_event`，Agent 代执行动作留痕并标注来源，全程可追溯。
- **密钥安全**：凭证文件与 `.env` 不进版本库，接口与界面只返回掩码。

## 外部数据源接入

| 链路 | 免 Key 通道 | 配 Key 后升级 |
| --- | --- | --- |
| 气象 | Open-Meteo 真实预报 | 高德官方预报 / 和风官方预警 |
| 通知 | Windows 桌面弹窗 + 审计日志 | 钉钉群机器人推送（@ 到人） |
| 路径 | 内置全国城市库（2000+ 城市）真实坐标 + 大圆距离 | 高德真实路径规划 |

未配置时各链路明确标注当前通道，不假装成功。
