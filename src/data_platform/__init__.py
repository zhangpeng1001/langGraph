"""数据中台治理图学习项目。

本包刻意使用本地 Mock 平台，不连接真实的数据中台或模型服务，便于学习者
观察 LangGraph 的状态、检查点、人工中断、异步任务回调和前后端协作。
"""

from .graph import governance_graph

__all__ = ["governance_graph"]
