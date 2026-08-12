"""示例 9【高级模式】：RetryPolicy、瞬时异常重试和显式降级分支。

学习目标：掌握 RetryPolicy 配置、retry_on 异常过滤、降级路径设计。
对应文档：docs/02_路由并行与容错.md
"""

from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy


class TemporaryServiceError(ConnectionError):
    """模拟适合重试的网络瞬时错误。"""


class ResilienceState(TypedDict, total=False):
    query: str
    force_fallback: bool
    service_result: str
    attempts: int
    final_result: str


def build_resilience_graph(failures_before_success: int = 1):
    """每次构图都创建独立计数器，避免不同测试之间共享失败次数。"""

    attempt_counter = {"value": 0}

    def call_service(state: ResilienceState) -> dict[str, object]:
        attempt_counter["value"] += 1
        current = attempt_counter["value"]
        if current <= failures_before_success:
            raise TemporaryServiceError(f"第 {current} 次调用发生模拟网络抖动。")
        if state.get("force_fallback", False):
            return {"service_result": "", "attempts": current}
        return {
            "service_result": f"主服务成功处理：{state['query']}",
            "attempts": current,
        }

    def route_result(state: ResilienceState) -> Literal["success", "fallback"]:
        return "success" if state.get("service_result") else "fallback"

    def success(state: ResilienceState) -> dict[str, str]:
        return {"final_result": state["service_result"]}

    def fallback(state: ResilienceState) -> dict[str, str]:
        return {"final_result": f"降级回答：暂时无法处理 {state['query']}。"}

    builder = StateGraph(ResilienceState)
    builder.add_node(
        "call_service",
        call_service,
        retry=RetryPolicy(
            initial_interval=0.01,
            backoff_factor=1.0,
            max_interval=0.01,
            max_attempts=3,
            jitter=False,
            retry_on=TemporaryServiceError,
        ),
    )
    builder.add_node("success", success)
    builder.add_node("fallback", fallback)
    builder.add_edge(START, "call_service")
    builder.add_conditional_edges(
        "call_service", route_result, ["success", "fallback"]
    )
    builder.add_edge("success", END)
    builder.add_edge("fallback", END)
    return builder.compile(name="resilience_graph")


graph = build_resilience_graph()
