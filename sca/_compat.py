"""LangGraph 兼容层（零依赖垫片）。

设计目标：让整套系统在**未安装 langgraph 时也能完整运行**（含 HITL 中断恢复，
需求 §8/§10）。若运行环境已 `pip install langgraph`，则自动改用官方实现，行为一致。

覆盖 API：
  - langgraph.graph.StateGraph / START / END
  - langgraph.types.interrupt / Command / GraphInterrupt
  - langgraph.checkpoint.memory.MemorySaver
  - langgraph.checkpoint.postgres.PostgresSaver  (无 psycopg 时退化为内存，不阻塞)
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, get_type_hints

logger = logging.getLogger("sca._compat")

END = "__end__"
START = "__start__"

# 单次 ainvoke 的最大"批"数（超步数）。防图成环时死循环。
#
# **必须与官方引擎的 recursion_limit 同源**：官方默认 10007（≈无上限），垫片原先写死
# 500，同一张图会在两条引擎的不同超步数处中断，双引擎等价性对照（verify_p0p1.py
# --shim）就失去意义。因此两边都读 SCA_RECURSION_LIMIT（见 sca/settings.py）。
_DEFAULT_MAX_STEPS = 200


def _max_steps() -> int:
    """超步上限。每次 ainvoke 现读，保证能拿到 .env 注入后的值。"""
    for key in ("SCA_RECURSION_LIMIT", "LANGGRAPH_DEFAULT_RECURSION_LIMIT"):
        raw = os.environ.get(key)
        if not raw:
            continue
        try:
            val = int(raw.strip())
        except ValueError:
            continue
        if val > 0:
            return val
    return _DEFAULT_MAX_STEPS


class GraphInterrupt(Exception):
    """HITL 中断信号（与 langgraph.types.GraphInterrupt 同名同义）。"""

    def __init__(self, payload: Any = None):
        self.payload = payload
        super().__init__("Graph interrupted (HITL)")


class GraphRecursionError(RecursionError):
    """超步上限耗尽（与 langgraph.errors.GraphRecursionError 同名同义）。

    官方实现是 RecursionError 的子类，这里保持一致：调用方即使只按 RecursionError
    捕获也能命中，两条引擎的异常类型不会分叉。
    """


class Command:
    """恢复指令（与 langgraph.types.Command 同义）。"""

    def __init__(self, *, resume: Any = None, update: Any = None, goto: Any = None):
        self.resume = resume
        self.update = update
        self.goto = goto


# ---- 中断上下文（按线程隔离，支持并发）----
class _Ctx:
    __slots__ = ("pending_resume", "resume_node")

    def __init__(self, pending_resume: Any = None, resume_node: Optional[str] = None):
        self.pending_resume = pending_resume
        self.resume_node = resume_node


_local = threading.local()


def _ctx() -> Optional[_Ctx]:
    return getattr(_local, "ctx", None)


def interrupt(payload: Any = None) -> Any:
    """在节点内调用：有挂起的恢复值则返回，否则抛出 GraphInterrupt 暂停。"""
    ctx = _ctx()
    if ctx is not None and ctx.pending_resume is not None:
        val = ctx.pending_resume
        ctx.pending_resume = None
        return val
    raise GraphInterrupt(payload)


# ---------------------------------------------------------------------------
# 状态通道 reducer（与官方 langgraph 的 Annotated[..., reducer] 语义对齐）
# ---------------------------------------------------------------------------
def _reducers(schema: Any) -> Dict[str, Callable]:
    """从状态 schema 的 Annotated 元数据里取出各字段的 reducer。

    官方 langgraph 用 BinaryOperatorAggregate 承载 `Annotated[List, operator.add]`
    这类累加通道；垫片必须做等价处理，否则并行/多次写入时后写者会把先写者的数据
    整个覆盖掉（表现为 trace 只剩最后一个节点），两条执行路径行为就漂移了。

    注意：state 模块用了 `from __future__ import annotations`，`__annotations__` 里是
    字符串而非类型对象，必须经 get_type_hints(include_extras=True) 解析才能拿到
    Annotated 的 metadata（官方 langgraph 解析 schema 走的是同一条路）。
    """
    try:
        hints = get_type_hints(schema, include_extras=True)
    except Exception:  # 解析失败（前向引用等）-> 退回原始注解，最多丢掉 reducer
        hints = getattr(schema, "__annotations__", {}) or {}
    out: Dict[str, Callable] = {}
    for name, tp in hints.items():
        for m in (getattr(tp, "__metadata__", None) or ()):
            if callable(m):
                out[name] = m
                break
    return out


def _apply_update(state: Dict[str, Any], out: Any, reducers: Dict[str, Callable]) -> None:
    """把节点返回值合并进 state：带 reducer 的字段 fold，其余按覆盖语义（同官方）。"""
    if not isinstance(out, dict):
        return
    for k, v in out.items():
        red = reducers.get(k)
        if red is not None:
            cur = state.get(k)
            state[k] = red(cur, v) if cur is not None else v
        else:
            state[k] = v


def _goto_list(goto: Any) -> List[str]:
    """把 `Command.goto` 归一成目标节点名列表。

    官方形态（langgraph/graph/state.py::_control_branch）：
      goto 是 str/Send 时视为**单目标**，否则按序列逐项展开；`END` 不是真实节点，
      走到它就等于终止（由调用方过滤）。垫片没有 Send/TASKS 通道，遇到非字符串目标
      只能忽略并告警，绝不能静默当成"没有目标"——那会让图提前终止而不报错。
    """
    if goto is None:
        return []
    if isinstance(goto, str):
        return [goto]
    if isinstance(goto, (list, tuple, set)):
        out: List[str] = []
        for item in goto:
            if isinstance(item, str):
                out.append(item)
            else:
                logger.warning(
                    "[compat] Command.goto 含非字符串目标 %r，垫片已忽略"
                    "（官方引擎走 Send/TASKS 通道，垫片无对应实现）", item)
        return out
    logger.warning("[compat] Command.goto 类型不支持：%r", goto)
    return []


# ---------------------------------------------------------------------------
# 检查点（兼容 langgraph checkpointer 的最小接口）
# ---------------------------------------------------------------------------
class _BaseSaver:
    def save(self, thread_id: str, data: Dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class MemorySaver(_BaseSaver):
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def save(self, thread_id: str, data: Dict[str, Any]) -> None:
        self._store[thread_id] = data

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(thread_id)


class PostgresSaver(_BaseSaver):
    """无 psycopg 时安全退化为内存检查点，保证 demo 可离线跑通。"""

    def __init__(self, dsn: str = ""):
        self.dsn = dsn
        self._mem = MemorySaver()
        logger.warning("[compat] PostgresSaver 退化为内存检查点（psycopg 不可用或离线）")

    @classmethod
    def from_conn_string(cls, conn_string: str) -> "PostgresSaver":
        return cls(conn_string)

    def save(self, thread_id: str, data: Dict[str, Any]) -> None:
        self._mem.save(thread_id, data)

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        return self._mem.load(thread_id)


# ---------------------------------------------------------------------------
# 图定义与编译
# ---------------------------------------------------------------------------
class StateGraph:
    def __init__(self, state_schema: Any = None):
        self.state_schema = state_schema
        self.nodes: Dict[str, Callable] = {}
        self._edges: Dict[str, List[Any]] = {}
        self.entry: Optional[str] = None
        self.finish: Optional[str] = None

    def add_node(self, name: str, func: Callable) -> "StateGraph":
        self.nodes[name] = func
        self._edges.setdefault(name, [])
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        self.entry = name
        return self

    def set_finish_point(self, name: str) -> "StateGraph":
        self.finish = name
        return self

    def add_edge(self, a: str, b: str) -> "StateGraph":
        self._edges.setdefault(a, []).append(("edge", b))
        return self

    def add_conditional_edges(self, source: str, path: Callable, path_map: Dict[str, str]) -> "StateGraph":
        self._edges.setdefault(source, []).append(("cond", path, path_map))
        return self

    def compile(self, checkpointer: Optional[_BaseSaver] = None,
                interrupt_before: Optional[List[str]] = None) -> "CompiledGraph":
        if checkpointer is None:
            checkpointer = MemorySaver()
        return CompiledGraph(
            nodes=self.nodes,
            edges=self._edges,
            entry=self.entry,
            finish=self.finish,
            checkpointer=checkpointer,
            interrupt_before=list(interrupt_before or []),
            state_schema=self.state_schema,
        )


class CompiledGraph:
    def __init__(self, nodes, edges, entry, finish, checkpointer, interrupt_before,
                 state_schema: Any = None):
        self.nodes = nodes
        self.edges = edges
        self.entry = entry
        self.finish = finish
        self.checkpointer = checkpointer
        self.interrupt_before = interrupt_before
        self.state_schema = state_schema
        # 累加通道（Annotated[..., reducer]）：与官方 langgraph 的 fold 语义保持一致
        self.reducers = _reducers(state_schema)
        # 节点注册顺序：同一批（超步）内按此顺序执行，保证 trace 追加顺序确定
        self._order = {n: i for i, n in enumerate(nodes)}

    # ---- 路由：返回该节点的全部后继 ----
    def _next(self, node: str, state: Dict[str, Any]) -> List[str]:
        """一个节点的全部后继。

        多条出边 = 扇出：这些后继属于**同一批**（垫片用串行依次执行来模拟 LangGraph
        的同超步并行），因此下游的 fan-in 汇聚点会在所有分支都完成后才被执行。
        无条件边全部生效；条件边只取实际选中的那一个。
        """
        out: List[str] = []
        for spec in self.edges.get(node, []):
            if spec[0] == "edge":
                out.append(spec[1])
                continue
            if spec[0] == "cond":
                _, path_fn, path_map = spec
                key = path_fn(state)
                if key == END:
                    out.append(END)
                    continue
                if key in path_map:
                    out.append(path_map[key])
                    continue
                raise ValueError(f"conditional edge 返回值 '{key}' 不在 path_map 中")
        return out or [END]   # 无出边 -> 终止

    async def ainvoke(self, input: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        thread_id = (config.get("configurable") or {}).get("thread_id", "default")

        # 恢复模式
        if isinstance(input, Command):
            saved = self.checkpointer.load(thread_id)
            if saved is None:
                raise RuntimeError(f"无可恢复的检查点（thread_id={thread_id}）")
            state = saved["state"]
            next_node = saved["next"]
            ctx = _Ctx(pending_resume=input.resume, resume_node=next_node)
        else:
            state = dict(input) if isinstance(input, dict) else {}
            next_node = self.entry
            ctx = _Ctx(pending_resume=None, resume_node=None)
            self.checkpointer.save(thread_id, {"state": state, "next": next_node})

        _local.ctx = ctx
        cur_node = next_node
        try:
            batch = [] if next_node in (END, None) else [next_node]
            steps = 0
            limit = _max_steps()
            while batch:
                steps += 1
                if steps > limit:
                    # 与官方同语义：超限**抛异常**而非静默 break。
                    # 静默 break 会把"没跑完"伪装成"跑完了"（下游拿到半截状态还不自知）；
                    # 且官方抛 GraphRecursionError 而垫片不抛，本身就是一处引擎分叉。
                    raise GraphRecursionError(
                        f"图在 {limit} 个超步内未收敛（图可能成环）。"
                        f"可调大 SCA_RECURSION_LIMIT，或让 supervisor 收敛后 goto END。")

                # interrupt_before：恢复进入该节点时不再暂停
                for node in batch:
                    if node in self.interrupt_before and ctx.resume_node != node:
                        cur_node = node
                        self.checkpointer.save(thread_id, {"state": state, "next": node})
                        raise GraphInterrupt({"interrupted_before": node})

                # 同批节点按注册顺序依次执行（串行模拟同超步并行），共享同一 state。
                # 节点允许返回两种形态：
                #   * dict            —— 按 reducer / 覆盖语义合并进 state
                #   * Command(goto=…) —— 动态路由：update 合并进 state，goto **追加**路由目标
                #
                # ⚠️ goto 与静态出边的关系是**叠加**而非覆盖（实测官方 langgraph：节点既有
                # 静态边 sup->x 又返回 Command(goto="y") 时，x 与 y 都会被执行；goto=END 也
                # 压不住静态边）。这条语义必须照抄——否则"给决策节点留一条静态兜底边"
                # 在两个引擎上会得到不同的图。
                # 推论（设计约定）：要让 Command 完全掌控路由，该节点就**不能声明静态出边**。
                goto_extra: Dict[str, List[str]] = {}
                for node in sorted(batch, key=lambda n: self._order.get(n, 0)):
                    cur_node = node
                    fn = self.nodes[node]
                    res = fn(state)
                    # 官方允许同步节点函数；垫片此前只支持 async，这里对齐
                    out = await res if inspect.isawaitable(res) else res
                    if isinstance(out, Command):
                        _apply_update(state, out.update, self.reducers)
                        if out.goto is not None:
                            goto_extra[node] = _goto_list(out.goto)
                    else:
                        _apply_update(state, out, self.reducers)

                # 收集下一批：静态出边 + Command.goto 追加目标；
                # 各分支后继合并去重（去重即 fan-in 语义——汇聚点只跑一次）
                nxt: List[str] = []
                for node in batch:
                    targets = list(self._next(node, state))
                    targets.extend(goto_extra.get(node) or [])
                    for t in targets:
                        if t == END or t in nxt:
                            continue
                        if t not in self.nodes:
                            # 与官方一致：未知路由目标被**静默忽略**（官方只往 branch 通道
                            # 写，没有消费者的写不报错）。这里只 warn 便于排查，不抛异常——
                            # 抛异常会让两条引擎分叉。目标合法性由上层自行校验：supervisor
                            # 的 precondition / ELIGIBLE 表必须在返回 Command 前就把非法目标挡住。
                            logger.warning("[compat] 路由目标 '%s' 不在图中，已忽略"
                                           "（上层应在 precondition 阶段拦截非法目标）", t)
                            continue
                        nxt.append(t)
                batch = nxt

            self.checkpointer.save(thread_id, {"state": state, "next": END})
            return state
        except GraphInterrupt:
            # 保存中断点，供 Command(resume=...) 恢复
            self.checkpointer.save(thread_id, {"state": state, "next": cur_node})
            raise
        finally:
            _local.ctx = None

    # 同步便捷封装
    def invoke(self, input: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return asyncio.get_event_loop().run_until_complete(self.ainvoke(input, config))


# ---------------------------------------------------------------------------
# 导出：优先使用真实 langgraph，否则使用本垫片
# ---------------------------------------------------------------------------
def _try_real() -> bool:
    try:
        from langgraph.graph import StateGraph as _SG, START as _S, END as _E  # noqa
        from langgraph.types import interrupt as _i, Command as _C  # noqa
        try:  # langgraph >= 1.x：GraphInterrupt 在 langgraph.errors
            from langgraph.errors import GraphInterrupt as _GI  # noqa
        except ImportError:  # 旧版：在 langgraph.types
            from langgraph.types import GraphInterrupt as _GI  # noqa
        from langgraph.checkpoint.memory import MemorySaver as _MS  # noqa
        try:
            from langgraph.checkpoint.postgres import PostgresSaver as _PS  # noqa
        except ImportError:  # 未装 langgraph-checkpoint-postgres 不阻塞
            _PS = PostgresSaver
        try:  # langgraph >= 1.x：GraphRecursionError 在 langgraph.errors
            from langgraph.errors import GraphRecursionError as _GRE  # noqa
        except ImportError:  # 极旧版本没有该异常 -> 保留垫片定义
            _GRE = GraphRecursionError
        # 覆盖本模块符号为官方实现
        globals()["StateGraph"] = _SG
        globals()["START"] = _S
        globals()["END"] = _E
        globals()["interrupt"] = _i
        globals()["Command"] = _C
        globals()["GraphInterrupt"] = _GI
        globals()["GraphRecursionError"] = _GRE
        globals()["MemorySaver"] = _MS
        globals()["PostgresSaver"] = _PS
        return True
    except Exception:
        return False


USING_REAL_LANGGRAPH = _try_real()
if not USING_REAL_LANGGRAPH:
    logger.info("[compat] 未检测到 langgraph，使用纯标准库垫片（功能等价，含 HITL 恢复）")
