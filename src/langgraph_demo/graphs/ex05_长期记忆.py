"""示例 5【状态管理】：使用 Store 保存跨线程的用户长期记忆。

学习目标：理解 InMemoryStore、namespace、user_id 与 thread_id 的区别。
对应文档：docs/03_持久化记忆与时间旅行.md
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_store
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore


class MemoryState(TypedDict, total=False):
    message: str
    remembered_profile: dict[str, Any]
    response: str


def _user_id(config: RunnableConfig) -> str:
    """真实系统应从认证信息获取 user_id，本示例从 configurable 读取。"""

    value = config.get("configurable", {}).get("user_id")
    if not value:
        raise ValueError("长期记忆示例要求 configurable.user_id。")
    return str(value)


def load_profile(state: MemoryState, config: RunnableConfig) -> dict[str, object]:
    """Store 的 namespace 与 key 一起唯一定位一条长期记忆。"""

    del state  # 本节点只需要配置和 Store，显式标明未读取业务状态。
    store = get_store()
    item = store.get(("users", _user_id(config)), "profile")
    return {"remembered_profile": item.value if item else {"visits": 0}}


def personalize(state: MemoryState) -> dict[str, str]:
    profile = state.get("remembered_profile", {})
    previous_topic = profile.get("last_message", "暂无")
    visits = int(profile.get("visits", 0)) + 1
    return {
        "response": (
            f"这是你的第 {visits} 次访问；上次消息：{previous_topic}；"
            f"本次消息：{state['message']}"
        )
    }


def save_profile(state: MemoryState, config: RunnableConfig) -> dict[str, object]:
    old_profile = state.get("remembered_profile", {})
    new_profile = {
        "visits": int(old_profile.get("visits", 0)) + 1,
        "last_message": state["message"],
    }
    store = get_store()
    store.put(("users", _user_id(config)), "profile", new_profile)
    return {"remembered_profile": new_profile}


def build_memory_graph(store: Any | None = None):
    builder = StateGraph(MemoryState)
    builder.add_node("load_profile", load_profile)
    builder.add_node("personalize", personalize)
    builder.add_node("save_profile", save_profile)
    builder.add_edge(START, "load_profile")
    builder.add_edge("load_profile", "personalize")
    builder.add_edge("personalize", "save_profile")
    builder.add_edge("save_profile", END)
    return builder.compile(store=store or InMemoryStore(), name="long_term_memory_graph")


memory_store = InMemoryStore()
graph = build_memory_graph(memory_store)
