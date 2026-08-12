from __future__ import annotations

import unittest

from langgraph_demo.graphs.ex01_状态图基础 import build_basic_graph
from langgraph_demo.graphs.ex03_并行与MapReduce import build_parallel_graph
from langgraph_demo.graphs.ex09_容错与重试 import (
    TemporaryServiceError,
    build_resilience_graph,
)
from langgraph_demo.graphs.ex02_条件路由与循环 import build_routing_graph
from langgraph_demo.graphs.ex08_子图协作 import build_parent_graph
from langgraph_demo.tools import safe_calculator, text_statistics


class CoreGraphTests(unittest.TestCase):
    """验证不依赖模型的基础图行为。"""

    def test_basic_graph_merges_reducer_and_limits_output_schema(self) -> None:
        result = build_basic_graph().invoke({"topic": "  LangGraph   入门  "})
        self.assertEqual(result["summary"], "你的学习主题是：LangGraph 入门")
        self.assertEqual(len(result["notes"]), 2)
        self.assertNotIn("normalized_topic", result)

    def test_routing_command_and_loop_exit(self) -> None:
        result = build_routing_graph().invoke({"number": 3, "history": []})
        self.assertEqual(result["parity"], "奇数")
        self.assertEqual(result["remaining"], 0)
        self.assertIn("循环已结束", result["result"])
        countdown_events = [item for item in result["history"] if "循环计数" in item]
        self.assertEqual(len(countdown_events), 3)

    def test_parallel_send_merges_all_unique_subjects(self) -> None:
        result = build_parallel_graph().invoke(
            {"subjects": ["State", "Reducer", "State", ""], "reports": []}
        )
        self.assertEqual(len(result["reports"]), 2)
        self.assertIn("State", result["final_report"])
        self.assertIn("Reducer", result["final_report"])

    def test_parallel_empty_input_uses_aggregate_directly(self) -> None:
        result = build_parallel_graph().invoke({"subjects": [], "reports": []})
        self.assertEqual(result["final_report"], "没有可学习的主题。")

    def test_subgraph_updates_shared_parent_state(self) -> None:
        result = build_parent_graph().invoke({"topic": "子图", "expert_notes": []})
        self.assertIn("研究员", result["final_report"])
        self.assertIn("教师", result["final_report"])
        self.assertEqual(len(result["expert_notes"]), 2)

    def test_retry_policy_recovers_from_transient_error(self) -> None:
        result = build_resilience_graph(failures_before_success=2).invoke(
            {"query": "检查重试"}
        )
        self.assertEqual(result["attempts"], 3)
        self.assertIn("主服务成功", result["final_result"])

    def test_retry_policy_raises_after_attempts_exhausted(self) -> None:
        graph = build_resilience_graph(failures_before_success=3)
        with self.assertRaises(TemporaryServiceError):
            graph.invoke({"query": "必然失败"})

    def test_explicit_fallback_branch(self) -> None:
        result = build_resilience_graph(failures_before_success=0).invoke(
            {"query": "降级", "force_fallback": True}
        )
        self.assertIn("降级回答", result["final_result"])


class SafeToolTests(unittest.TestCase):
    def test_safe_calculator(self) -> None:
        value = safe_calculator.invoke({"expression": "(12 + 8) * 3"})
        self.assertEqual(value, "60")

    def test_safe_calculator_rejects_code(self) -> None:
        with self.assertRaises(ValueError):
            safe_calculator.invoke({"expression": "__import__('os').getcwd()"})

    def test_text_statistics(self) -> None:
        result = text_statistics.invoke({"text": "LangGraph 中文 demo"})
        self.assertEqual(result["words"], 3)
        self.assertGreater(result["characters"], result["words"])


if __name__ == "__main__":
    unittest.main()

