"""示例 4【状态管理】：检查点、线程隔离、状态历史和时间旅行。

学习目标：掌握 MemorySaver、thread_id 会话隔离、状态历史回溯和状态编辑。
对应文档：docs/03_持久化记忆与时间旅行.md
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


class ConversationState(TypedDict, total=False):
    message: str
    turn: int
    history: Annotated[list[str], operator.add]
    reply: str


def record_turn(state: ConversationState) -> dict[str, object]:
    """同一 thread_id 再次调用时，会从上次检查点继续累加 turn。"""

    turn = state.get("turn", 0) + 1
    message = state["message"]
    return {
        "turn": turn,
        "history": [f"第 {turn} 轮：{message}"],
        "reply": f"线程已记录第 {turn} 轮消息。",
    }


def build_persistence_graph(checkpointer: Any | None = None):
    builder = StateGraph(ConversationState)
    builder.add_node("record_turn", record_turn)
    builder.add_edge(START, "record_turn")
    builder.add_edge("record_turn", END)
    return builder.compile(
        checkpointer=checkpointer or MemorySaver(), name="persistence_graph"
    )


def thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """检查点必须通过 configurable.thread_id 区分会话。"""

    return {"configurable": {"thread_id": thread_id}}


# 模块级 checkpointer 让 CLI 和 Studio 在当前进程内持续保存状态。
checkpointer = MemorySaver()
graph = build_persistence_graph(checkpointer)
