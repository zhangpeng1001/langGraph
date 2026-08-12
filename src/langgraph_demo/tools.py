"""供 ReAct Agent 使用的安全本地工具。

计算器只解释明确允许的算术 AST 节点，绝不使用 ``eval`` 或 ``exec``，从而
避免把模型生成的任意文本当成 Python 代码执行。
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, tool


_BINARY_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _evaluate_ast(node: ast.AST) -> float:
    """递归计算经过白名单约束的表达式节点。"""

    if isinstance(node, ast.Expression):
        return _evaluate_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate_ast(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_ast(node.left)
        right = _evaluate_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("为了避免超大数运算，指数绝对值不能超过 12。")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if abs(result) > 1e100:
            raise ValueError("计算结果过大，已停止计算。")
        return result
    raise ValueError(f"表达式包含不允许的语法：{type(node).__name__}")


@tool
def safe_calculator(expression: str) -> str:
    """计算只包含数字、括号、加减乘除、取模和乘方的算术表达式。"""

    if not expression.strip():
        raise ValueError("表达式不能为空。")
    if len(expression) > 200:
        raise ValueError("表达式过长，最多允许 200 个字符。")
    tree = ast.parse(expression, mode="eval")
    result = _evaluate_ast(tree)
    return str(int(result)) if result.is_integer() else str(result)


@tool
def current_time(timezone: str = "Asia/Shanghai") -> str:
    """返回指定 IANA 时区的当前时间，例如 Asia/Shanghai 或 UTC。"""

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone}") from exc
    return datetime.now(zone).isoformat(timespec="seconds")


_GLOSSARY = {
    "state": "State（状态）是图在节点之间传递和累积的数据。",
    "node": "Node（节点）是读取当前状态并返回状态更新的函数或 Runnable。",
    "edge": "Edge（边）定义节点之间的执行顺序；条件边可以动态选择下一节点。",
    "reducer": "Reducer 决定同一状态字段收到多个更新时如何合并。",
    "checkpoint": "Checkpoint（检查点）保存某个线程在一个执行步骤后的状态快照。",
    "interrupt": "Interrupt 会暂停图并保存状态，之后可使用 Command(resume=...) 恢复。",
    "send": "Send 用于动态创建并行任务，常与 reducer 组合实现 Map-Reduce。",
    "subgraph": "Subgraph（子图）是作为父图节点运行的已编译图。",
}


@tool
def langgraph_glossary(term: str) -> str:
    """查询 LangGraph 核心术语的简短中文解释。"""

    normalized = term.strip().lower()
    if normalized in _GLOSSARY:
        return _GLOSSARY[normalized]
    available = "、".join(sorted(_GLOSSARY))
    return f"暂未收录 {term!r}。可查询：{available}。"


@tool
def text_statistics(text: str) -> dict[str, int]:
    """统计文本的字符数、非空白字符数和按空白分隔的词数。"""

    return {
        "characters": len(text),
        "non_whitespace_characters": sum(not char.isspace() for char in text),
        "words": len(text.split()),
    }


TOOLS: list[BaseTool] = [
    safe_calculator,
    current_time,
    langgraph_glossary,
    text_statistics,
]

