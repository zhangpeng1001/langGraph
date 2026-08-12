"""示例 2【基础入门】：Command 动态跳转、条件边和有限循环。

学习目标：掌握 Command 同时更新状态和跳转、条件边、recursion limit。
对应文档：docs/02_路由并行与容错.md
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command


class RoutingState(TypedDict, total=False):
    number: int
    parity: str
    remaining: int
    history: Annotated[list[str], operator.add]
    result: str


def decide_parity(
    state: RoutingState,
) -> Command[Literal["even_path", "odd_path"]]:
    """同时更新状态并决定下一节点，展示 Command 的两个职责。"""

    number = state["number"]
    if number % 2 == 0:
        return Command(
            update={"parity": "偶数", "history": [f"{number} 被判断为偶数。"]},
            goto="even_path",
        )
    return Command(
        update={"parity": "奇数", "history": [f"{number} 被判断为奇数。"]},
        goto="odd_path",
    )


def enter_even_path(state: RoutingState) -> dict[str, object]:
    return {
        "remaining": min(abs(state["number"]), 5),
        "history": ["进入偶数处理分支。"],
    }


def enter_odd_path(state: RoutingState) -> dict[str, object]:
    return {
        "remaining": min(abs(state["number"]), 5),
        "history": ["进入奇数处理分支。"],
    }


def countdown(state: RoutingState) -> dict[str, object]:
    """每次执行减少一次计数；条件边会决定是否再次运行本节点。"""

    current = state.get("remaining", 0)
    next_value = max(current - 1, 0)
    return {
        "remaining": next_value,
        "history": [f"循环计数：{current} -> {next_value}"],
    }


def continue_or_finish(state: RoutingState) -> Literal["countdown", "finish"]:
    """条件边函数只返回目标节点名称，不修改状态。"""

    return "countdown" if state.get("remaining", 0) > 0 else "finish"


def finish(state: RoutingState) -> dict[str, str]:
    return {"result": f"数字 {state['number']} 是{state['parity']}，循环已结束。"}


def build_routing_graph():
    builder = StateGraph(RoutingState)
    # destinations 主要用于让图形渲染器知道 Command 可能前往哪些节点。
    builder.add_node(
        "decide", decide_parity, destinations=("even_path", "odd_path")
    )
    builder.add_node("even_path", enter_even_path)
    builder.add_node("odd_path", enter_odd_path)
    builder.add_node("countdown", countdown)
    builder.add_node("finish", finish)
    builder.add_edge(START, "decide")
    builder.add_edge("even_path", "countdown")
    builder.add_edge("odd_path", "countdown")
    builder.add_conditional_edges(
        "countdown", continue_or_finish, ["countdown", "finish"]
    )
    builder.add_edge("finish", END)
    return builder.compile(name="routing_graph")


graph = build_routing_graph()
