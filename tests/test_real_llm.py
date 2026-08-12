from __future__ import annotations

import os
import unittest

from langchain_core.messages import HumanMessage

from langgraph_demo.config import load_model_settings
from langgraph_demo.graphs.ex10_ReAct智能体 import build_react_graph
from langgraph_demo.llm import create_chat_model


@unittest.skipUnless(
    os.getenv("RUN_REAL_LLM_TESTS") == "1",
    "设置 RUN_REAL_LLM_TESTS=1 后才调用真实模型，避免默认产生费用。",
)
class RealModelSmokeTests(unittest.TestCase):
    def test_real_model_can_complete_tool_loop(self) -> None:
        settings = load_model_settings()
        graph = build_react_graph(model=create_chat_model(settings))
        result = graph.invoke(
            {
                "messages": [
                    HumanMessage(content="请调用计算器计算 7 * 8，然后只告诉我结果。")
                ],
                "iterations": 0,
            },
            {"recursion_limit": 20},
        )
        self.assertTrue(result["messages"][-1].content)
        self.assertGreaterEqual(result["iterations"], 1)


if __name__ == "__main__":
    unittest.main()

