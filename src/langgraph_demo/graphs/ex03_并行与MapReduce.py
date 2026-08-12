"""示例 3【基础入门】：Send 动态并行分发与 reducer Map-Reduce 汇总。

学习目标：理解 Send 创建并行任务、Worker 状态隔离、reducer 合并结果。
对应文档：docs/02_路由并行与容错.md
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


class ParallelState(TypedDict, total=False):
    subjects: list[str]
    cleaned_subjects: list[str]
    reports: Annotated[list[str], operator.add]
    final_report: str


class WorkerState(TypedDict):
    """每个并行 Worker 只接收自己需要的主题。"""

    subject: str


def prepare_subjects(state: ParallelState) -> dict[str, list[str]]:
    """去除空主题和重复主题，避免创建无意义任务。"""

    cleaned = list(dict.fromkeys(item.strip() for item in state["subjects"] if item.strip()))
    return {"cleaned_subjects": cleaned}


def dispatch_workers(state: ParallelState) -> list[Send] | str:
    """为每个主题动态创建一次 ``study_worker`` 执行。"""

    subjects = state.get("cleaned_subjects", [])
    if not subjects:
        return "aggregate"
    return [Send("study_worker", {"subject": subject}) for subject in subjects]


def study_worker(state: WorkerState) -> dict[str, list[str]]:
    """Worker 返回单元素列表，父状态 reducer 会合并所有并行结果。"""

    subject = state["subject"]
    return {"reports": [f"{subject}：先理解概念，再运行示例，最后修改代码。"]}


def aggregate(state: ParallelState) -> dict[str, str]:
    """并行结果的到达顺序不应成为业务约束，因此展示时主动排序。"""

    reports = sorted(state.get("reports", []))
    if not reports:
        return {"final_report": "没有可学习的主题。"}
    return {"final_report": "\n".join(f"- {item}" for item in reports)}


def build_parallel_graph():
    builder = StateGraph(ParallelState)
    builder.add_node("prepare", prepare_subjects)
    builder.add_node("study_worker", study_worker)
    builder.add_node("aggregate", aggregate)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare", dispatch_workers, ["study_worker", "aggregate"]
    )
    builder.add_edge("study_worker", "aggregate")
    builder.add_edge("aggregate", END)
    return builder.compile(name="parallel_map_reduce_graph")


graph = build_parallel_graph()
