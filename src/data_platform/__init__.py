"""数据中台治理图学习项目。

本包刻意使用本地 Mock 平台模拟数据任务，但 LangGraph 检查点使用 MongoDB，
便于学习者观察跨服务重启的人工中断、异步任务回调和前后端协作。
"""

from .graph import build_governance_graph

__all__ = ["build_governance_graph"]
