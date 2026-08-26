"""数据中台治理领域模型。

这里集中定义步骤名称、步骤状态以及 API 使用的请求模型，避免在图节点中
散落数字状态码和自由文本。模型全部是本地数据结构，不代表真实平台协议。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GovernanceStep(StrEnum):
    """治理链路中可单独执行、跳过和审计的业务步骤。"""

    COLLECT = "COLLECT"
    QUALITY_CHECK = "QUALITY_CHECK"
    CLEAN = "CLEAN"
    STORE = "STORE"
    PUBLISH = "PUBLISH"


class StepStatus(StrEnum):
    """单个步骤的事实状态，明确区分等待、成功、失败和跳过。"""

    PENDING = "PENDING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RunPhase(StrEnum):
    """治理运行对前端展示的总体阶段。"""

    WAITING_REVIEW = "WAITING_REVIEW"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class CreateRunRequest(BaseModel):
    """创建治理运行的请求。

    ``request`` 支持自然语言，例如“治理 demo.csv，跳过质检”；Mock 解析器
    只识别有限的中文关键词，因此不会因为模型幻觉而启动未知副作用。
    """

    request: str = Field(..., min_length=1, max_length=500)


class ReviewRequest(BaseModel):
    """人工审核计划的请求，decision 只允许批准或拒绝。"""

    decision: str = Field(..., min_length=1, max_length=20)
    comment: str = Field(default="", max_length=500)


class CompleteTaskRequest(BaseModel):
    """模拟外部异步任务回调的请求。"""

    success: bool = True
    message: str = Field(default="人工模拟回调", max_length=500)


class RunSnapshot(BaseModel):
    """返回给前端的最小可学习快照。

    内部检查点仍保存在 LangGraph 中，但 API 只暴露可理解的运行字段，避免把
    内部实现和潜在敏感数据直接泄漏到浏览器。
    """

    run_id: str
    thread_id: str
    request: str
    file_name: str = ""
    phase: str
    ui_message: str = ""
    pending_action: dict[str, Any] | None = None
    plan_steps: list[str] = Field(default_factory=list)
    skipped_steps: list[str] = Field(default_factory=list)
    current_index: int = 0
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    audit: list[dict[str, Any]] = Field(default_factory=list)
