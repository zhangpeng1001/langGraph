"""示例 10【模型集成】：手写 ReAct 循环，而不是隐藏在高级封装后面。

学习目标：理解 model -> tools -> model 循环、bind_tools、ToolNode、条件路由。
执行模式：模型节点 -> 如果有 tool_calls 则进入 ToolNode -> 回到模型节点；
当模型给出不含 tool_calls 的 AIMessage 时结束。
对应文档：docs/05_工具与ReAct.md
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from langgraph_demo.llm import create_chat_model
from langgraph_demo.tools import TOOLS


class ReActState(MessagesState):
    """MessagesState 已为 messages 字段配置 add_messages reducer。"""

    iterations: int


SYSTEM_PROMPT = """你是一名 LangGraph 中文助教。
你可以使用提供的本地工具完成计算、时间查询、术语查询和文本统计。
需要工具时必须发出规范 tool call；获得工具结果后再给出简洁、准确的中文回答。
不要声称使用了未提供的工具，也不要编造工具结果。
"""


def build_react_graph(
    model: Any | None = None,
    tools: list[BaseTool] | None = None,
):
    """构建可注入模型的 ReAct 图。

    ``model=None`` 时不会在导入阶段读取环境变量，而是在第一个模型节点真正
    执行时才创建真实 ChatOpenAI。这使 Studio 可以先加载图定义再配置运行。
    """

    active_tools = tools or TOOLS
    cache: dict[str, Any] = {}

    def get_bound_model() -> Any:
        if "bound" not in cache:
            raw_model = model or create_chat_model()
            cache["bound"] = raw_model.bind_tools(active_tools)
        return cache["bound"]

    def call_model(state: ReActState) -> dict[str, object]:
        """把完整消息历史发给模型，并把新 AIMessage 追加回状态。"""

        response = get_bound_model().invoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state.get("messages", [])]
        )
        if not isinstance(response, AIMessage):
            raise TypeError("聊天模型必须返回 AIMessage。")
        return {
            "messages": [response],
            "iterations": state.get("iterations", 0) + 1,
        }

    def route_after_model(state: ReActState) -> Literal["tools", "__end__"]:
        """最后一条 AIMessage 有工具调用就执行工具，否则结束。"""

        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage):
            raise TypeError("模型节点之后的最后一条消息应为 AIMessage。")
        return "tools" if last_message.tool_calls else END

    builder = StateGraph(ReActState)
    builder.add_node("model", call_model)
    builder.add_node("tools", ToolNode(active_tools, handle_tool_errors=True))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route_after_model, ["tools", END])
    builder.add_edge("tools", "model")
    return builder.compile(name="manual_react_graph")


graph = build_react_graph()
