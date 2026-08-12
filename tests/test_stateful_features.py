from __future__ import annotations

import unittest

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from langgraph_demo.graphs.ex06_人工审核 import build_human_review_graph
from langgraph_demo.graphs.ex05_长期记忆 import build_memory_graph
from langgraph_demo.graphs.ex04_检查点与时间旅行 import build_persistence_graph, thread_config
from langgraph_demo.graphs.ex07_流式输出 import build_streaming_graph


class PersistenceTests(unittest.TestCase):
    def test_same_thread_continues_and_different_thread_isolated(self) -> None:
        graph = build_persistence_graph(MemorySaver())
        first_config = thread_config("thread-a")
        second_config = thread_config("thread-b")

        graph.invoke({"message": "a1"}, first_config)
        continued = graph.invoke({"message": "a2"}, first_config)
        isolated = graph.invoke({"message": "b1"}, second_config)

        self.assertEqual(continued["turn"], 2)
        self.assertEqual(isolated["turn"], 1)
        self.assertGreaterEqual(len(list(graph.get_state_history(first_config))), 2)

    def test_update_state_creates_editable_timeline(self) -> None:
        graph = build_persistence_graph(MemorySaver())
        config = thread_config("time-travel")
        graph.invoke({"message": "开始"}, config)
        snapshot = graph.get_state(config)

        edited = graph.update_state(snapshot.config, {"turn": 10}, as_node="record_turn")
        result = graph.invoke({"message": "从编辑状态继续"}, edited)
        self.assertEqual(result["turn"], 11)


class LongTermMemoryTests(unittest.TestCase):
    def test_store_memory_crosses_threads_for_same_user(self) -> None:
        store = InMemoryStore()
        graph = build_memory_graph(store)
        graph.invoke(
            {"message": "State"},
            {"configurable": {"user_id": "user-1", "thread_id": "a"}},
        )
        result = graph.invoke(
            {"message": "Reducer"},
            {"configurable": {"user_id": "user-1", "thread_id": "b"}},
        )
        self.assertEqual(result["remembered_profile"]["visits"], 2)
        self.assertIn("State", result["response"])

    def test_store_namespaces_isolate_users(self) -> None:
        store = InMemoryStore()
        graph = build_memory_graph(store)
        graph.invoke(
            {"message": "用户 A"}, {"configurable": {"user_id": "a"}}
        )
        result = graph.invoke(
            {"message": "用户 B"}, {"configurable": {"user_id": "b"}}
        )
        self.assertEqual(result["remembered_profile"]["visits"], 1)
        self.assertNotIn("用户 A", result["response"])


class HumanReviewTests(unittest.TestCase):
    def test_interrupt_and_resume_approved(self) -> None:
        graph = build_human_review_graph(MemorySaver())
        config = {"configurable": {"thread_id": "approved-review"}}
        graph.invoke({"proposal": "测试计划"}, config)
        self.assertEqual(graph.get_state(config).next, ("request_review",))

        result = graph.invoke(Command(resume="approve"), config)
        self.assertFalse(graph.get_state(config).next)
        self.assertIn("已批准", result["status"])

    def test_interrupt_and_resume_rejected(self) -> None:
        graph = build_human_review_graph(MemorySaver())
        config = {"configurable": {"thread_id": "rejected-review"}}
        graph.invoke({"proposal": "测试计划"}, config)
        result = graph.invoke(Command(resume="增加练习"), config)
        self.assertIn("增加练习", result["status"])


class StreamingTests(unittest.TestCase):
    def test_updates_and_custom_streams(self) -> None:
        events = list(
            build_streaming_graph().stream(
                {"topic": "stream"}, stream_mode=["updates", "custom"]
            )
        )
        custom = [chunk for mode, chunk in events if mode == "custom"]
        updates = [chunk for mode, chunk in events if mode == "updates"]
        self.assertEqual([item["progress"] for item in custom], [25, 75, 100])
        self.assertEqual(len(updates), 3)
        self.assertIn("complete", updates[-1])


if __name__ == "__main__":
    unittest.main()

