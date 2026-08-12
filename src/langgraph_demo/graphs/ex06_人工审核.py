"""示例 6【人机交互】：interrupt 暂停、检查点保存和 Command 恢复。

学习目标：掌握 interrupt 暂停机制、Command(resume=...) 恢复执行、人工审核流程。
对应文档：docs/04_人工介入与流式输出.md
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ReviewState(TypedDict, total=False):
    proposal: str
    decision: str
    feedback: str
    status: str


def request_review(
    state: ReviewState,
) -> Command[Literal["approved", "rejected"]]:
    """首次执行会暂停；恢复后 interrupt 表达式的值就是 resume 数据。"""

    answer = interrupt(
        {
            "type": "human_review",
            "question": "是否批准下面的方案？",
            "proposal": state["proposal"],
            "expected": "输入 approve，或输入修改意见",
        }
    )
    normalized = str(answer).strip()
    if normalized.lower() in {"approve", "approved", "yes", "y", "批准", "同意"}:
        return Command(
            update={"decision": normalized, "status": "approved"}, goto="approved"
        )
    return Command(
        update={
            "decision": normalized,
            "feedback": normalized,
            "status": "rejected",
        },
        goto="rejected",
    )


def approved(state: ReviewState) -> dict[str, str]:
    return {"status": "方案已批准，可以继续执行。"}


def rejected(state: ReviewState) -> dict[str, str]:
    return {"status": f"方案未批准，反馈：{state.get('feedback', '无')}"}


def build_human_review_graph(checkpointer: Any | None = None):
    builder = StateGraph(ReviewState)
    builder.add_node(
        "request_review", request_review, destinations=("approved", "rejected")
    )
    builder.add_node("approved", approved)
    builder.add_node("rejected", rejected)
    builder.add_edge(START, "request_review")
    builder.add_edge("approved", END)
    builder.add_edge("rejected", END)
    return builder.compile(
        checkpointer=checkpointer or MemorySaver(), name="human_review_graph"
    )


checkpointer = MemorySaver()
graph = build_human_review_graph(checkpointer)
