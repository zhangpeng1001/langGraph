"""示例 11【模型集成】：综合项目，带真实模型、工具、审核、并行执行和记忆的学习规划助手。

学习目标：整合路由、ReAct 子图、结构化输出、人工审核、Send 并行、长期记忆、流式输出。
对应文档：docs/06_综合助手解析.md
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_store
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, RetryPolicy, Send, interrupt
from pydantic import BaseModel, Field

from langgraph_demo.config import ModelConfigurationError
from langgraph_demo.graphs.ex10_ReAct智能体 import build_react_graph
from langgraph_demo.llm import create_chat_model
from langgraph_demo.tools import TOOLS


class IntentDecision(BaseModel):
    """结构化路由结果，避免依赖自然语言字符串做脆弱判断。"""

    route: Literal["quick", "plan"] = Field(
        description="quick 表示直接问答，plan 表示需要制定并执行学习计划"
    )
    reason: str = Field(description="选择该路由的简短原因")


class LearningPlan(BaseModel):
    """模型生成、可供人工审核的结构化学习计划。"""

    title: str
    goal: str
    steps: list[str] = Field(min_length=1, max_length=5)


class AssistantState(MessagesState):
    """综合图状态。

    ``topic_results`` 使用加法 reducer 接收 Send 创建的多个并行 Worker 结果。
    每条结果带有 request 字段，防止同一 thread 后续运行混入旧任务结果。
    """

    request: str
    memory: dict[str, Any]
    route: Literal["quick", "plan"]
    route_reason: str
    plan: dict[str, Any]
    review_feedback: str
    topic_results: Annotated[list[dict[str, str]], operator.add]
    final_answer: str


def _configurable_id(config: RunnableConfig, name: str) -> str:
    value = config.get("configurable", {}).get(name)
    if not value:
        raise ValueError(f"综合助手要求 configurable.{name}。")
    return str(value)


def _message_text(message: Any) -> str:
    """兼容字符串内容和部分网关返回的内容块列表。"""

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content)


def _should_retry_model_error(exc: Exception) -> bool:
    """配置错误重试没有意义；网络和模型解析错误则允许节点重试。"""

    return not isinstance(exc, (ModelConfigurationError, KeyboardInterrupt))


def build_assistant_graph(
    model: Any | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
    tools: list[BaseTool] | None = None,
):
    """构建综合学习助手；依赖均可注入，便于无网络单元测试。"""

    active_tools = tools or TOOLS
    model_cache: dict[str, Any] = {}
    structured_cache: dict[type[BaseModel], Any] = {}

    def get_model() -> Any:
        if "model" not in model_cache:
            model_cache["model"] = model or create_chat_model()
        return model_cache["model"]

    def structured_model(schema: type[BaseModel]) -> Any:
        if schema not in structured_cache:
            structured_cache[schema] = get_model().with_structured_output(schema)
        return structured_cache[schema]

    def invoke_structured(schema: type[BaseModel], prompt: str) -> BaseModel:
        result = structured_model(schema).invoke(prompt)
        return result if isinstance(result, schema) else schema.model_validate(result)

    def load_memory(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        """读取跨 thread 共享的用户档案，并确定本轮请求文本。"""

        user_id = _configurable_id(config, "user_id")
        item = get_store().get(("learning-users", user_id), "profile")
        request = state.get("request", "").strip()
        if not request:
            human_messages = [
                message
                for message in state.get("messages", [])
                if isinstance(message, HumanMessage)
            ]
            if not human_messages:
                raise ValueError("综合助手需要 request 或至少一条 HumanMessage。")
            request = _message_text(human_messages[-1]).strip()
        return {"request": request, "memory": item.value if item else {}}

    def classify_intent(state: AssistantState) -> dict[str, str]:
        memory = state.get("memory", {})
        decision = invoke_structured(
            IntentDecision,
            """你是学习任务路由器。定义解释、单次计算、简短事实问答选 quick；
需要多步骤学习安排、课程设计、系统研究或用户明确要求计划时选 plan。
只按照指定结构输出。\n\n"""
            f"用户请求：{state['request']}\n用户历史摘要：{memory}",
        )
        assert isinstance(decision, IntentDecision)
        return {"route": decision.route, "route_reason": decision.reason}

    def route_intent(state: AssistantState) -> Literal["react_agent", "generate_plan"]:
        return "react_agent" if state["route"] == "quick" else "generate_plan"

    def generate_plan(state: AssistantState) -> dict[str, Any]:
        result = invoke_structured(
            LearningPlan,
            """你是资深中文学习规划师。请把用户目标拆成 2 到 5 个具体、互不重复、
可以独立研究的步骤。步骤应由浅入深，并能在之后并行生成学习说明。
只按照指定结构输出。\n\n"""
            f"用户请求：{state['request']}\n已有用户记忆：{state.get('memory', {})}",
        )
        assert isinstance(result, LearningPlan)
        return {"plan": result.model_dump()}

    def review_plan(
        state: AssistantState,
    ) -> Command[Literal["dispatch", "revise_plan"]]:
        """暂停后，approve 进入并行执行；其他文本作为修订意见。"""

        answer = interrupt(
            {
                "type": "learning_plan_review",
                "question": "请审核学习计划：输入 approve 批准，或直接输入修改意见。",
                "plan": state["plan"],
            }
        )
        if isinstance(answer, dict):
            approved = bool(answer.get("approved"))
            feedback = str(answer.get("feedback", ""))
        else:
            text = str(answer).strip()
            approved = text.lower() in {
                "approve",
                "approved",
                "yes",
                "y",
                "批准",
                "同意",
            }
            feedback = "" if approved else text
        if approved:
            return Command(update={"review_feedback": ""}, goto="dispatch")
        return Command(
            update={"review_feedback": feedback or "请重新调整计划。"},
            goto="revise_plan",
        )

    def revise_plan(state: AssistantState) -> dict[str, Any]:
        result = invoke_structured(
            LearningPlan,
            "请根据用户反馈修订学习计划，仍然只输出指定结构。\n\n"
            f"用户目标：{state['request']}\n原计划：{state['plan']}\n"
            f"反馈：{state['review_feedback']}",
        )
        assert isinstance(result, LearningPlan)
        return {"plan": result.model_dump()}

    def dispatch_steps(state: AssistantState) -> list[Send] | str:
        steps = list(state.get("plan", {}).get("steps", []))
        if not steps:
            return "aggregate"
        return [
            Send(
                "study_worker",
                {
                    "request": state["request"],
                    "step": step,
                    "memory": state.get("memory", {}),
                },
            )
            for step in steps
        ]

    def study_worker(state: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        """多个实例可并行调用同一个真实模型，再由 reducer 汇总。"""

        response = get_model().invoke(
            [
                SystemMessage(
                    content=(
                        "你是中文学习助教。围绕指定步骤给出：核心概念、实践任务、"
                        "完成标准。内容控制在 300 字以内，不要虚构外部引用。"
                    )
                ),
                HumanMessage(
                    content=f"总目标：{state['request']}\n当前步骤：{state['step']}"
                ),
            ]
        )
        return {
            "topic_results": [
                {
                    "request": str(state["request"]),
                    "step": str(state["step"]),
                    "content": _message_text(response),
                }
            ]
        }

    def aggregate(state: AssistantState) -> dict[str, str]:
        current_results = [
            item
            for item in state.get("topic_results", [])
            if item.get("request") == state["request"]
        ]
        materials = "\n\n".join(
            f"## {item['step']}\n{item['content']}" for item in current_results
        )
        response = get_model().invoke(
            [
                SystemMessage(
                    content=(
                        "你是课程编辑。把各步骤材料整理成完整中文学习指南，保留清晰"
                        "标题、实践任务和验收标准，并在结尾给出推荐执行顺序。"
                    )
                ),
                HumanMessage(
                    content=f"用户目标：{state['request']}\n计划：{state['plan']}\n\n{materials}"
                ),
            ]
        )
        return {"final_answer": _message_text(response)}

    def extract_quick_answer(state: AssistantState) -> dict[str, str]:
        ai_messages = [
            message
            for message in state.get("messages", [])
            if isinstance(message, AIMessage) and not message.tool_calls
        ]
        if not ai_messages:
            raise ValueError("ReAct 子图没有生成最终 AIMessage。")
        return {"final_answer": _message_text(ai_messages[-1])}

    def save_memory(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        user_id = _configurable_id(config, "user_id")
        old = state.get("memory", {})
        updated = {
            **old,
            "runs": int(old.get("runs", 0)) + 1,
            "last_request": state["request"],
            "last_route": state["route"],
            "last_plan_title": state.get("plan", {}).get("title", ""),
        }
        get_store().put(("learning-users", user_id), "profile", updated)

        # 规划分支尚未把最终答案写入 messages，在这里统一追加，便于下一轮查看。
        messages: list[AIMessage] = []
        if state["route"] == "plan":
            messages.append(AIMessage(content=state["final_answer"], name="course_editor"))
        return {"memory": updated, "messages": messages}

    retry_policy = RetryPolicy(
        initial_interval=0.5,
        backoff_factor=2,
        max_interval=4,
        max_attempts=3,
        jitter=True,
        retry_on=_should_retry_model_error,
    )

    # quick 路由使用真正的已编译 ReAct 子图，并复用同一模型和工具集合。
    react_subgraph = build_react_graph(model=model, tools=active_tools)

    builder = StateGraph(AssistantState)
    builder.add_node("load_memory", load_memory)
    builder.add_node("classify_intent", classify_intent, retry=retry_policy)
    builder.add_node("react_agent", react_subgraph)
    builder.add_node("extract_quick_answer", extract_quick_answer)
    builder.add_node("generate_plan", generate_plan, retry=retry_policy)
    builder.add_node(
        "review_plan", review_plan, destinations=("dispatch", "revise_plan")
    )
    builder.add_node("revise_plan", revise_plan, retry=retry_policy)
    builder.add_node("dispatch", lambda state: {})
    builder.add_node("study_worker", study_worker, retry=retry_policy)
    builder.add_node("aggregate", aggregate, retry=retry_policy)
    builder.add_node("save_memory", save_memory)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "classify_intent")
    builder.add_conditional_edges(
        "classify_intent", route_intent, ["react_agent", "generate_plan"]
    )
    builder.add_edge("react_agent", "extract_quick_answer")
    builder.add_edge("extract_quick_answer", "save_memory")
    builder.add_edge("generate_plan", "review_plan")
    builder.add_edge("revise_plan", "review_plan")
    builder.add_conditional_edges(
        "dispatch", dispatch_steps, ["study_worker", "aggregate"]
    )
    builder.add_edge("study_worker", "aggregate")
    builder.add_edge("aggregate", "save_memory")
    builder.add_edge("save_memory", END)

    return builder.compile(
        checkpointer=checkpointer or MemorySaver(),
        store=store or InMemoryStore(),
        name="learning_assistant_graph",
    )


assistant_checkpointer = MemorySaver()
assistant_store = InMemoryStore()
graph = build_assistant_graph(
    checkpointer=assistant_checkpointer,
    store=assistant_store,
)
