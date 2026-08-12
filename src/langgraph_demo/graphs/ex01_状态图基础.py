"""示例 1【基础入门】：最小 StateGraph、普通边和输入/输出状态。

学习目标：理解 State、Node、Edge、START/END、输入输出 schema 和 reducer。
对应文档：docs/01_核心概念与状态.md
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class BasicInput(TypedDict):
    """调用方真正需要提供的字段。"""

    topic: str


class BasicOutput(TypedDict):
    """图完成后向调用方暴露的字段。"""

    summary: str
    notes: list[str]


class BasicState(TypedDict, total=False):
    """图内部完整状态。

    ``notes`` 使用 ``operator.add`` 作为 reducer。每个节点只需返回自己新增的
    一小段列表，LangGraph 会把它追加到已有列表，而不是覆盖旧值。
    """

    topic: str
    normalized_topic: str
    notes: Annotated[list[str], operator.add]
    summary: str


def normalize_topic(state: BasicState) -> dict[str, object]:
    """节点只返回状态更新，不需要复制整个 state。"""

    normalized = " ".join(state["topic"].strip().split())
    return {
        "normalized_topic": normalized,
        "notes": ["normalize_topic：已清理主题两侧及重复空白。"],
    }


def build_summary(state: BasicState) -> dict[str, object]:
    """读取上个节点写入的字段，并生成最终输出。"""

    topic = state["normalized_topic"] or "未命名主题"
    return {
        "summary": f"你的学习主题是：{topic}",
        "notes": ["build_summary：已生成面向调用方的摘要。"],
    }


def build_basic_graph():
    """构建并编译图，分离构建函数便于测试和二次修改。"""

    builder = StateGraph(BasicState, input=BasicInput, output=BasicOutput)
    builder.add_node("normalize_topic", normalize_topic)
    builder.add_node("build_summary", build_summary)
    builder.add_edge(START, "normalize_topic")
    builder.add_edge("normalize_topic", "build_summary")
    builder.add_edge("build_summary", END)
    return builder.compile(name="basic_graph")


graph = build_basic_graph()
