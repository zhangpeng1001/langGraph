"""示例 8【高级模式】：把多角色子图作为父图中的一个节点。

学习目标：理解子图编译、Runnable 接口复用、父图调用子图、多角色协作。
对应文档：docs/02_路由并行与容错.md
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class SharedState(TypedDict, total=False):
    topic: str
    draft: str
    expert_notes: Annotated[list[str], operator.add]
    final_report: str


def researcher(state: SharedState) -> dict[str, object]:
    return {
        "draft": f"{state['topic']} 包含状态、节点、边和运行时四个观察角度。",
        "expert_notes": ["研究员：先形成事实草稿。"],
    }


def teacher(state: SharedState) -> dict[str, object]:
    return {
        "draft": state["draft"] + " 建议通过小图逐步验证每个概念。",
        "expert_notes": ["教师：补充循序渐进的学习建议。"],
    }


def build_expert_subgraph():
    builder = StateGraph(SharedState)
    builder.add_node("researcher", researcher)
    builder.add_node("teacher", teacher)
    builder.add_edge(START, "researcher")
    builder.add_edge("researcher", "teacher")
    builder.add_edge("teacher", END)
    return builder.compile(name="expert_subgraph")


def editor(state: SharedState) -> dict[str, str]:
    roles = "；".join(state.get("expert_notes", []))
    return {"final_report": f"{state['draft']}\n协作轨迹：{roles}"}


def build_parent_graph():
    builder = StateGraph(SharedState)
    # 已编译图实现 Runnable 接口，因此可以像普通函数一样注册为节点。
    builder.add_node("expert_team", build_expert_subgraph())
    builder.add_node("editor", editor)
    builder.add_edge(START, "expert_team")
    builder.add_edge("expert_team", "editor")
    builder.add_edge("editor", END)
    return builder.compile(name="parent_with_subgraph")


expert_graph = build_expert_subgraph()
graph = build_parent_graph()
