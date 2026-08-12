from __future__ import annotations

import unittest
import io
from typing import Any
from contextlib import redirect_stdout

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from langgraph_demo.graphs.ex11_综合学习助手 import (
    IntentDecision,
    LearningPlan,
    build_assistant_graph,
)
from langgraph_demo.graphs.ex10_ReAct智能体 import build_react_graph
from langgraph_demo.cli import _stream_assistant


class ScriptedReActModel:
    """只供测试注入：先请求真实本地工具，再产生最终消息。"""

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools: list[Any]) -> "ScriptedReActModel":
        self.tool_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "safe_calculator",
                        "args": {"expression": "6 * 7"},
                        "id": "test-tool-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="计算器返回 42。")


class StructuredWrapper:
    def __init__(self, owner: "AssistantFakeModel", schema: type[Any]) -> None:
        self.owner = owner
        self.schema = schema

    def invoke(self, prompt: str) -> Any:
        if self.schema is IntentDecision:
            return IntentDecision(route=self.owner.route, reason="测试路由")
        if self.schema is LearningPlan:
            self.owner.plan_calls += 1
            suffix = "（已修订）" if "反馈" in prompt else ""
            return LearningPlan(
                title=f"LangGraph 学习计划{suffix}",
                goal="掌握核心图能力",
                steps=["理解状态", "实践路由"],
            )
        raise AssertionError(f"未支持的 schema：{self.schema}")


class BoundAssistantReAct:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "langgraph_glossary",
                        "args": {"term": "state"},
                        "id": "assistant-tool-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="State 是节点之间传递的数据。")


class AssistantFakeModel:
    """覆盖结构化输出、普通消息和工具绑定三种模型接口。"""

    def __init__(self, route: str) -> None:
        self.route = route
        self.plan_calls = 0
        self.bound = BoundAssistantReAct()

    def with_structured_output(self, schema: type[Any]) -> StructuredWrapper:
        return StructuredWrapper(self, schema)

    def bind_tools(self, tools: list[Any]) -> BoundAssistantReAct:
        return self.bound

    def invoke(self, messages: list[Any]) -> AIMessage:
        system = str(messages[0].content)
        if "课程编辑" in system:
            return AIMessage(content="这是汇总后的完整学习指南。")
        return AIMessage(content="这是并行 Worker 生成的步骤说明。")


class ReActTests(unittest.TestCase):
    def test_tool_loop_preserves_messages_and_finishes(self) -> None:
        model = ScriptedReActModel()
        result = build_react_graph(model=model).invoke(
            {"messages": [HumanMessage(content="计算 6*7")], "iterations": 0}
        )
        self.assertEqual(result["iterations"], 2)
        self.assertTrue(any(isinstance(item, ToolMessage) for item in result["messages"]))
        self.assertEqual(result["messages"][-1].content, "计算器返回 42。")
        self.assertIn("safe_calculator", model.tool_names)


class AssistantTests(unittest.TestCase):
    def test_cli_stream_handles_subgraph_three_tuple_events(self) -> None:
        graph = build_assistant_graph(
            model=AssistantFakeModel("quick"),
            checkpointer=MemorySaver(),
            store=InMemoryStore(),
        )
        config = {"configurable": {"thread_id": "stream-thread", "user_id": "u0"}}
        output = io.StringIO()
        with redirect_stdout(output):
            _stream_assistant(
                graph,
                {
                    "request": "State 是什么？",
                    "messages": [HumanMessage(content="State 是什么？")],
                    "topic_results": [],
                },
                config,
            )
        self.assertIn("State", output.getvalue())

    def test_quick_route_uses_react_and_saves_memory(self) -> None:
        store = InMemoryStore()
        graph = build_assistant_graph(
            model=AssistantFakeModel("quick"),
            checkpointer=MemorySaver(),
            store=store,
        )
        config = {"configurable": {"thread_id": "quick-thread", "user_id": "u1"}}
        result = graph.invoke(
            {
                "request": "State 是什么？",
                "messages": [HumanMessage(content="State 是什么？")],
                "topic_results": [],
            },
            config,
        )
        self.assertEqual(result["route"], "quick")
        self.assertIn("State", result["final_answer"])
        self.assertFalse(graph.get_state(config).next)
        item = store.get(("learning-users", "u1"), "profile")
        self.assertEqual(item.value["last_route"], "quick")

    def test_plan_route_interrupts_then_runs_parallel_workers(self) -> None:
        model = AssistantFakeModel("plan")
        store = InMemoryStore()
        graph = build_assistant_graph(
            model=model, checkpointer=MemorySaver(), store=store
        )
        config = {"configurable": {"thread_id": "plan-thread", "user_id": "u2"}}
        graph.invoke(
            {
                "request": "制定学习计划",
                "messages": [HumanMessage(content="制定学习计划")],
                "topic_results": [],
            },
            config,
        )
        self.assertEqual(graph.get_state(config).next, ("review_plan",))

        result = graph.invoke(Command(resume="approve"), config)
        self.assertEqual(result["route"], "plan")
        self.assertEqual(len(result["topic_results"]), 2)
        self.assertIn("完整学习指南", result["final_answer"])
        self.assertEqual(
            store.get(("learning-users", "u2"), "profile").value["runs"], 1
        )

    def test_rejected_plan_is_revised_and_interrupts_again(self) -> None:
        model = AssistantFakeModel("plan")
        graph = build_assistant_graph(
            model=model, checkpointer=MemorySaver(), store=InMemoryStore()
        )
        config = {"configurable": {"thread_id": "revise-thread", "user_id": "u3"}}
        graph.invoke(
            {
                "request": "制定学习计划",
                "messages": [HumanMessage(content="制定学习计划")],
                "topic_results": [],
            },
            config,
        )
        graph.invoke(Command(resume="增加更多实践"), config)
        snapshot = graph.get_state(config)
        self.assertEqual(snapshot.next, ("review_plan",))
        self.assertIn("已修订", snapshot.values["plan"]["title"])
        self.assertEqual(model.plan_calls, 2)


if __name__ == "__main__":
    unittest.main()
