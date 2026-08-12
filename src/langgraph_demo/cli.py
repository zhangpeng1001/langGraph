"""统一命令行入口，让每个知识点都可以独立运行。

示例按学习难度分为五大类，序号即推荐学习顺序：
  基础入门（01-03）→ 状态管理（04-05）→ 人机交互（06-07）→ 高级模式（08-09）→ 模型集成（10-11）
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from langgraph_demo.config import ModelConfigurationError, load_model_settings


@dataclass(frozen=True)
class Demo:
    """命令行展示所需的示例元数据。"""

    number: str
    category: str
    description: str
    runner: Callable[[str | None], None]
    requires_model: bool = False
    interactive: bool = False


def _print_json(value: Any) -> None:
    """使用 default=str 展示 LangChain Message、Interrupt 等教学对象。"""

    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _run_basic(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex01_状态图基础 import graph

    _print_json(graph.invoke({"topic": user_input or "LangGraph 状态图"}))


def _run_routing(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex02_条件路由与循环 import graph

    number = int(user_input or "4")
    _print_json(graph.invoke({"number": number, "history": []}))


def _run_parallel(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex03_并行与MapReduce import graph

    subjects = (
        [part.strip() for part in user_input.split(",")]
        if user_input
        else ["State", "Reducer", "Send"]
    )
    _print_json(graph.invoke({"subjects": subjects, "reports": []}))


def _run_persistence(user_input: str | None) -> None:
    """连续调用、查看历史、修改状态并从新分支继续执行。"""

    from langgraph_demo.graphs.ex04_检查点与时间旅行 import (
        build_persistence_graph,
        thread_config,
    )

    local_graph = build_persistence_graph(MemorySaver())
    config = thread_config(f"persistence-{uuid.uuid4().hex[:8]}")
    first = local_graph.invoke({"message": user_input or "第一条消息"}, config)
    second = local_graph.invoke({"message": "第二条消息"}, config)
    snapshots = list(local_graph.get_state_history(config))

    # update_state 会创建新检查点；随后从该检查点继续，形成一条新时间线。
    edited_config = local_graph.update_state(
        snapshots[0].config, {"turn": 40}, as_node="record_turn"
    )
    branched = local_graph.invoke({"message": "从修改后的状态继续"}, edited_config)
    _print_json(
        {
            "first": first,
            "second": second,
            "checkpoint_count": len(snapshots),
            "branched_after_edit": branched,
        }
    )


def _run_memory(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex05_长期记忆 import build_memory_graph

    local_graph = build_memory_graph(InMemoryStore())
    user_id = f"student-{uuid.uuid4().hex[:6]}"
    first = local_graph.invoke(
        {"message": user_input or "我正在学习 State"},
        {"configurable": {"user_id": user_id, "thread_id": "thread-a"}},
    )
    second = local_graph.invoke(
        {"message": "我接下来学习 Reducer"},
        {"configurable": {"user_id": user_id, "thread_id": "thread-b"}},
    )
    _print_json({"thread_a": first, "thread_b_same_user": second})


def _run_human_review(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex06_人工审核 import build_human_review_graph

    local_graph = build_human_review_graph(MemorySaver())
    config = {"configurable": {"thread_id": f"review-{uuid.uuid4().hex[:8]}"}}
    local_graph.invoke({"proposal": user_input or "用两天学习 StateGraph"}, config)
    snapshot = local_graph.get_state(config)
    print("图已暂停，下一节点：", snapshot.next)
    _print_json(snapshot.values)
    answer = input("请输入 approve，或输入拒绝/修改意见：").strip()
    _print_json(local_graph.invoke(Command(resume=answer), config))


def _run_streaming(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex07_流式输出 import graph

    for mode, chunk in graph.stream(
        {"topic": user_input or "LangGraph 流式输出"},
        stream_mode=["updates", "custom"],
    ):
        print(f"[{mode}]")
        _print_json(chunk)


def _run_subgraph(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex08_子图协作 import graph

    _print_json(
        graph.invoke({"topic": user_input or "LangGraph 子图", "expert_notes": []})
    )


def _run_resilience(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex09_容错与重试 import build_resilience_graph

    local_graph = build_resilience_graph(failures_before_success=1)
    success = local_graph.invoke({"query": user_input or "重试策略"})
    fallback_graph = build_resilience_graph(failures_before_success=0)
    fallback = fallback_graph.invoke(
        {"query": "降级路径", "force_fallback": True}
    )
    _print_json({"retry_then_success": success, "explicit_fallback": fallback})


def _run_react(user_input: str | None) -> None:
    from langgraph_demo.graphs.ex10_ReAct智能体 import graph

    question = user_input or "请用计算器算出 (12 + 8) * 3，并解释你调用了什么工具。"
    result = graph.invoke(
        {"messages": [HumanMessage(content=question)], "iterations": 0},
        {"recursion_limit": 20},
    )
    print(result["messages"][-1].content)


# fmt: off
DEMOS: dict[str, Demo] = {
    "basic":       Demo("01", "基础入门", "State、节点、普通边、输入输出 schema",         _run_basic),
    "routing":     Demo("02", "基础入门", "Command、条件边和有限循环",                    _run_routing),
    "parallel":    Demo("03", "基础入门", "Send 并行 Map-Reduce 与 reducer",              _run_parallel),
    "persistence": Demo("04", "状态管理", "检查点、线程、历史、状态编辑和时间旅行",       _run_persistence),
    "memory":      Demo("05", "状态管理", "Store 与跨线程用户长期记忆",                   _run_memory),
    "human-review":Demo("06", "人机交互", "interrupt 与 Command(resume) 人工审核",        _run_human_review, interactive=True),
    "streaming":   Demo("07", "人机交互", "updates 与 custom 流式输出",                   _run_streaming),
    "subgraph":    Demo("08", "高级模式", "父图、子图和多角色协作",                       _run_subgraph),
    "resilience":  Demo("09", "高级模式", "RetryPolicy 和显式降级路径",                   _run_resilience),
    "react":       Demo("10", "模型集成", "真实模型、ToolNode 和手写 ReAct 循环",         _run_react, True),
}
# fmt: on


def run_doctor() -> int:
    """检查 Python、核心包版本和模型环境，但不发起收费请求。"""

    print(f"Python: {sys.version.split()[0]}")
    required = {
        "langgraph": "0.3.34",
        "langgraph-checkpoint": "2.1.0",
        "langgraph-prebuilt": "0.1.8",
        "langchain-core": "0.3.66",
        "langchain-openai": "0.3.25",
    }
    healthy = (3, 11) <= sys.version_info[:2] < (3, 14)
    for package, expected in required.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "未安装"
        status = "OK" if actual == expected else f"期望 {expected}"
        print(f"{package}: {actual} ({status})")
        healthy = healthy and actual == expected

    try:
        settings = load_model_settings()
        base_url = settings.base_url or "OpenAI 默认地址"
        print(f"模型配置: OK（model={settings.model}, base_url={base_url}）")
    except ModelConfigurationError as exc:
        print(f"模型配置: 未就绪（{exc}）")
        healthy = False

    print("诊断结果：", "可以运行全部示例" if healthy else "请按上面提示修复")
    return 0 if healthy else 1


def _stream_assistant(
    graph: Any,
    graph_input: dict[str, Any] | Command,
    config: dict[str, Any],
) -> None:
    """同时消费 messages token 流和节点 updates 流。"""

    printed_token = False
    for event in graph.stream(
        graph_input,
        config,
        stream_mode=["messages", "updates"],
        subgraphs=True,
    ):
        # 0.3.34 开启 subgraphs 后返回 (namespace, mode, chunk)；没有命名空间
        # 的普通多模式流则返回 (mode, chunk)。这里同时兼容两种事件形态。
        if not isinstance(event, tuple):
            continue
        if len(event) == 3:
            _namespace, mode, chunk = event
        elif len(event) == 2:
            mode, chunk = event
        else:
            continue

        if mode == "messages":
            # messages 的 chunk 形如 (BaseMessage, metadata)。工具消息也会经过
            # 这里；仅打印非空字符串内容，结构化 tool_calls 不会被误当正文。
            if isinstance(chunk, tuple) and chunk:
                message = chunk[0]
                content = getattr(message, "content", "")
                if isinstance(content, str) and content:
                    print(content, end="", flush=True)
                    printed_token = True
        elif mode == "updates" and isinstance(chunk, dict):
            node_names = ", ".join(str(name) for name in chunk)
            if node_names and "__interrupt__" not in node_names:
                print(f"\n[完成节点] {node_names}")
    if printed_token:
        print()


def run_assistant(user_input: str | None, thread_id: str, user_id: str) -> None:
    """运行综合图，并在每次人工审核点从终端获取恢复数据。"""

    from langgraph_demo.graphs.ex11_综合学习助手 import graph

    request = user_input or input("请输入学习问题或学习规划目标：").strip()
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id, "user_id": user_id},
        "recursion_limit": 50,
    }
    initial = {
        "request": request,
        "messages": [HumanMessage(content=request)],
        "topic_results": [],
    }
    _stream_assistant(graph, initial, config)

    while graph.get_state(config).next:
        snapshot = graph.get_state(config)
        print("\n图已暂停，等待人工审核。当前计划：")
        _print_json(snapshot.values.get("plan", {}))
        answer = input("输入 approve，或直接输入修改意见：").strip()
        _stream_assistant(graph, Command(resume=answer), config)

    final_state = graph.get_state(config).values
    print("\n=== 最终回答 ===")
    print(final_state.get("final_answer", "图已结束，但没有 final_answer。"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="langgraph-demo", description="LangGraph 0.3.34 中文学习项目"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="检查环境、依赖版本和模型配置")
    subparsers.add_parser("list", help="列出所有可运行示例")

    run_parser = subparsers.add_parser("run", help="运行一个独立知识点示例")
    run_parser.add_argument("demo", choices=sorted(DEMOS))
    run_parser.add_argument("--input", help="覆盖示例的默认输入")

    assistant_parser = subparsers.add_parser("assistant", help="运行综合学习助手")
    assistant_parser.add_argument("--thread-id", default="learning-thread-1")
    assistant_parser.add_argument("--user-id", default="student-1")
    assistant_parser.add_argument("--input", help="学习问题或学习规划目标")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        raise SystemExit(run_doctor())
    if args.command == "list":
        current_category = ""
        for name, demo in DEMOS.items():
            if demo.category != current_category:
                current_category = demo.category
                print(f"\n【{current_category}】")
            flags: list[str] = []
            if demo.requires_model:
                flags.append("真实模型")
            if demo.interactive:
                flags.append("交互")
            suffix = f" [{' / '.join(flags)}]" if flags else ""
            print(f"  {demo.number}  {name:14} {demo.description}{suffix}")
        return
    if args.command == "run":
        demo = DEMOS[args.demo]
        try:
            demo.runner(args.input)
        except ModelConfigurationError as exc:
            raise SystemExit(f"模型配置错误：{exc}") from exc
        return
    if args.command == "assistant":
        try:
            run_assistant(args.input, args.thread_id, args.user_id)
        except ModelConfigurationError as exc:
            raise SystemExit(f"模型配置错误：{exc}") from exc


if __name__ == "__main__":
    main()
