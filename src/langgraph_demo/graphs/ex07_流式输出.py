"""示例 7【人机交互】：updates、values、custom 等流式模式。

学习目标：掌握 stream_mode 多模式流式输出、get_stream_writer 自定义事件。
对应文档：docs/04_人工介入与流式输出.md
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph


class StreamingState(TypedDict, total=False):
    topic: str
    events: Annotated[list[str], operator.add]
    result: str


def collect(state: StreamingState) -> dict[str, list[str]]:
    """get_stream_writer 写出的对象只会出现在 custom 流中。"""

    writer = get_stream_writer()
    writer({"stage": "collect", "progress": 25, "topic": state["topic"]})
    return {"events": ["已收集主题"]}


def process(state: StreamingState) -> dict[str, list[str]]:
    writer = get_stream_writer()
    writer({"stage": "process", "progress": 75})
    return {"events": ["已处理主题"]}


def complete(state: StreamingState) -> dict[str, str]:
    writer = get_stream_writer()
    writer({"stage": "complete", "progress": 100})
    return {"result": f"{state['topic']} 的流式演示完成。"}


def build_streaming_graph():
    builder = StateGraph(StreamingState)
    builder.add_node("collect", collect)
    builder.add_node("process", process)
    builder.add_node("complete", complete)
    builder.add_edge(START, "collect")
    builder.add_edge("collect", "process")
    builder.add_edge("process", "complete")
    builder.add_edge("complete", END)
    return builder.compile(name="streaming_graph")


graph = build_streaming_graph()
