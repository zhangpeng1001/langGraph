"""数据中台治理 LangGraph 编排图。

图只编排确定性的治理步骤，外部平台由 ``MockPlatform`` 适配器提供。图中的
两个 ``interrupt`` 分别代表：计划人工审核、外部异步任务回调等待。生产入口会将
MongoDBSaver 传给本模块的图工厂，因此相同 ``thread_id`` 能跨进程恢复检查点。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

# 兼容两种运行方式：
# 1) 作为包模块被导入（通过 uvicorn / python -m data_platform）→ 使用相对导入
# 2) 直接作为脚本运行（python graph.py）→ 补 sys.path 后用顶层绝对导入
if __package__ in (None, ""):
    # 脚本直跑：把 src 目录推到 sys.path 最前，按顶层模块解析同包兄弟模块
    _SRC_DIR = Path(__file__).resolve().parent.parent
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from data_platform.domain import GovernanceStep, RunPhase, StepStatus
    from data_platform.mock_platform import mock_platform
else:
    # 包内导入：保持相对导入，不破坏包上下文
    from .domain import GovernanceStep, RunPhase, StepStatus
    from .mock_platform import mock_platform


class GovernanceState(TypedDict, total=False):
    """图状态；字段均可选，首个节点负责建立本次运行的完整上下文。"""

    run_id: str
    thread_id: str
    request: str
    file_name: str
    plan_steps: list[str]
    skipped_steps: list[str]
    current_index: int
    steps: dict[str, dict[str, Any]]
    current_task: dict[str, Any] | None
    # interrupt 恢复值只在 wait_external -> resolve_external 之间短暂存在，
    # 显式声明它可以避免 LangGraph 按状态 schema 丢弃这个关联字段。
    callback: Any
    pending_action: dict[str, Any] | None
    phase: str
    ui_message: str
    result: dict[str, Any]
    audit: list[dict[str, Any]]


_STEP_LABELS = {
    GovernanceStep.COLLECT.value: "采集",
    GovernanceStep.QUALITY_CHECK.value: "质检",
    GovernanceStep.CLEAN.value: "清洗",
    GovernanceStep.STORE.value: "入库",
    GovernanceStep.PUBLISH.value: "发布服务",
}


def _audit(state: GovernanceState, event: str, **details: Any) -> list[dict[str, Any]]:
    """追加一条审计事件；保留完整过程是学习检查点和可观测性的重点。"""

    previous = list(state.get("audit", []))
    previous.append({"event": event, **details})
    return previous


def _parse_request(request: str) -> tuple[str, list[str]]:
    """用可解释的规则解析文件名和跳过步骤，未知表达不会触发副作用。"""

    file_match = re.search(r"([\w\-\u4e00-\u9fff]+\.(?:csv|json|xlsx|txt|parquet))", request, re.I)
    file_name = file_match.group(1) if file_match else "demo_vector.csv"
    skip_map = {
        "采集": GovernanceStep.COLLECT.value,
        "质检": GovernanceStep.QUALITY_CHECK.value,
        "清洗": GovernanceStep.CLEAN.value,
        "入库": GovernanceStep.STORE.value,
        "发布": GovernanceStep.PUBLISH.value,
    }
    skipped = [step for keyword, step in skip_map.items() if f"跳过{keyword}" in request]
    return file_name, skipped


def prepare_plan(state: GovernanceState) -> dict[str, Any]:
    """建立本次运行独立的执行计划，不读取或篡改文件历史状态。"""

    file_name, skipped = _parse_request(state["request"])
    steps = [step.value for step in GovernanceStep if step.value not in skipped]
    # 即使步骤被跳过也保留一条 SKIPPED 事实记录，前端和审计人员可以区分
    # “尚未执行”与“依据本次计划主动跳过”，而不是把跳过步骤从结果中抹掉。
    step_state = {
        step.value: {
            "status": StepStatus.SKIPPED.value if step.value in skipped else StepStatus.PENDING.value,
            "label": _STEP_LABELS[step.value],
        }
        for step in GovernanceStep
    }
    return {
        "file_name": file_name,
        "plan_steps": steps,
        "skipped_steps": skipped,
        "current_index": 0,
        "steps": step_state,
        "phase": RunPhase.WAITING_REVIEW.value,
        "ui_message": "执行计划已生成，请先进行人工审核。",
        "pending_action": {
            "type": "human_review",
            "title": "治理计划审核",
            "question": "是否批准执行下面的治理计划？",
        },
        "audit": _audit(state, "PLAN_CREATED", file_name=file_name, skipped_steps=skipped),
    }


def review_plan(
        state: GovernanceState,
) -> Command[Literal["execute_next", "finalize"]]:
    """暂停等待审核；恢复后只接受明确批准/拒绝，并记录审核意见。"""

    answer = interrupt({
        "type": "human_review",
        "title": "治理计划审核",
        "file_name": state.get("file_name", ""),
        "plan_steps": state.get("plan_steps", []),
        "skipped_steps": state.get("skipped_steps", []),
        "expected": "approve/批准 或 reject/拒绝",
    })
    if isinstance(answer, dict):
        decision = str(answer.get("decision", "")).strip().lower()
        comment = str(answer.get("comment", "")).strip()
        if answer.get("task_id") == "__cancel__":
            return Command(
                update={
                    "phase": RunPhase.CANCELLED.value,
                    "pending_action": None,
                    "ui_message": "审核前已取消治理运行。",
                    "result": {"reason": "用户取消"},
                    "audit": _audit(state, "RUN_CANCELLED"),
                },
                goto="finalize",
            )
    else:
        decision, comment = str(answer).strip().lower(), ""
    approved = decision in {"approve", "approved", "yes", "y", "批准", "同意"}
    if not approved:
        return Command(
            update={
                "phase": RunPhase.REJECTED.value,
                "pending_action": None,
                "ui_message": "计划未批准，治理运行已安全结束。",
                "result": {"reason": comment or "人工审核拒绝"},
                "audit": _audit(state, "PLAN_REJECTED", comment=comment),
            },
            goto="finalize",
        )
    return Command(
        update={
            "phase": RunPhase.RUNNING.value,
            "pending_action": None,
            "ui_message": "计划已批准，开始执行治理步骤。",
            "audit": _audit(state, "PLAN_APPROVED", comment=comment),
        },
        goto="execute_next",
    )


def execute_next(state: GovernanceState) -> dict[str, Any]:
    """以幂等键启动当前外部步骤，并把等待动作写入检查点。"""

    steps = state.get("plan_steps", [])
    index = state.get("current_index", 0)
    if index >= len(steps):
        return {
            "phase": RunPhase.SUCCEEDED.value,
            "ui_message": "所有治理步骤均已完成。",
            "pending_action": None,
        }
    step = GovernanceStep(steps[index])
    key = f"{state['run_id']}:{step.value}:{index + 1}"
    task = mock_platform.start_task(run_id=state["run_id"], step=step, idempotency_key=key)
    step_updates = dict(state.get("steps", {}))
    step_updates[step.value] = {
        **step_updates.get(step.value, {}),
        "status": StepStatus.WAITING_EXTERNAL.value,
        "task_id": task["task_id"],
        "idempotency_key": key,
    }
    return {
        "phase": RunPhase.WAITING_EXTERNAL.value,
        "current_task": task,
        "steps": step_updates,
        "pending_action": {
            "type": "external_task",
            "title": f"等待{_STEP_LABELS[step.value]}任务",
            "task_id": task["task_id"],
            "step": step.value,
            "message": "这是一个异步 Mock 任务，请点击按钮模拟平台回调。",
        },
        "ui_message": f"{_STEP_LABELS[step.value]}任务已提交，正在异步等待回调。",
        "audit": _audit(state, "TASK_STARTED", step=step.value, task_id=task["task_id"], idempotency_key=key),
    }


def route_after_execute(state: GovernanceState) -> Literal["wait_external", "finalize"]:
    """根据当前计划位置选择等待外部任务或进入最终收尾。"""

    return "finalize" if state.get("current_index", 0) >= len(state.get("plan_steps", [])) else "wait_external"


def wait_external(state: GovernanceState) -> dict[str, Any]:
    """在节点中断，等待可信的 Mock 平台回调；无同步 sleep，不阻塞事件循环。"""

    task = state.get("current_task") or {}
    answer = interrupt({
        "type": "external_task",
        "task_id": task.get("task_id"),
        "step": task.get("step"),
        "message": "等待后台异步任务完成。恢复后会校验任务归属和终态。",
    })
    # interrupt 恢复值只作为回调关联线索，最终状态必须从 Mock 平台查询取得。
    return {"callback": answer}


def resolve_external(
        state: GovernanceState,
) -> Command[Literal["execute_next", "finalize"]]:
    """校验回调 task_id 与当前任务一致，再推进或阻断流程。"""

    callback = state.get("callback")
    callback_id = callback.get("task_id") if isinstance(callback, dict) else str(callback)
    current = state.get("current_task") or {}
    expected_id = current.get("task_id")
    # 取消是用户主动发起的控制动作，不应被误报为“非法回调”；它仍然通过
    # 同一个 interrupt 恢复边界进入图，便于学习取消与外部回调的差异。
    if callback_id == "__cancel__":
        return Command(
            update={
                "phase": RunPhase.CANCELLED.value,
                "pending_action": None,
                "current_task": None,
                "ui_message": "运行已取消，未再启动新的外部任务。",
                "result": {"reason": "用户取消"},
                "audit": _audit(state, "RUN_CANCELLED"),
            },
            goto="finalize",
        )
    task = mock_platform.get_task(str(callback_id)) if callback_id else None
    if not task or str(callback_id) != str(expected_id) or task.get("run_id") != state.get("run_id"):
        return Command(
            update={
                "phase": RunPhase.FAILED.value,
                "pending_action": None,
                "ui_message": "回调关联校验失败，已阻断流程。",
                "result": {"error": "INVALID_CALLBACK", "message": "任务 ID、运行 ID 或任务状态不匹配。"},
                "audit": _audit(state, "CALLBACK_REJECTED", callback_id=callback_id),
            },
            goto="finalize",
        )
    step = str(current.get("step"))
    step_updates = dict(state.get("steps", {}))
    if task.get("status") != "SUCCEEDED":
        step_updates[step] = {**step_updates.get(step, {}), "status": StepStatus.FAILED.value,
                              "message": task.get("message")}
        return Command(
            update={
                "phase": RunPhase.FAILED.value,
                "steps": step_updates,
                "current_task": None,
                "pending_action": None,
                "ui_message": f"{_STEP_LABELS.get(step, step)}失败，流程已阻断，不会自动进入后续步骤。",
                "result": {"error": "STEP_FAILED", "failed_step": step, "message": task.get("message")},
                "audit": _audit(state, "TASK_FAILED", step=step, task_id=task["task_id"]),
            },
            goto="finalize",
        )
    step_updates[step] = {
        **step_updates.get(step, {}),
        "status": StepStatus.SUCCEEDED.value,
        "artifact": task.get("artifact"),
        "service_url": task.get("service_url"),
    }
    result = dict(state.get("result", {}))
    if step == GovernanceStep.PUBLISH.value:
        result.update({"service_id": task.get("service_id"), "service_url": task.get("service_url")})
    return Command(
        update={
            "phase": RunPhase.RUNNING.value,
            "steps": step_updates,
            "current_index": state.get("current_index", 0) + 1,
            "current_task": None,
            "callback": None,
            "pending_action": None,
            "ui_message": f"{_STEP_LABELS.get(step, step)}已完成，继续执行下一步。",
            "result": result,
            "audit": _audit(state, "TASK_SUCCEEDED", step=step, task_id=task["task_id"]),
        },
        goto="execute_next",
    )


def finalize(state: GovernanceState) -> dict[str, Any]:
    """统一写入最终用户提示；失败/拒绝不会伪装成治理成功。"""

    phase = state.get("phase", RunPhase.FAILED.value)
    if phase == RunPhase.SUCCEEDED.value:
        if GovernanceStep.PUBLISH.value in state.get("skipped_steps", []):
            # 跳过发布是一个真实的 SKIPPED 结果，不能把“流程结束”误说成“服务已发布”。
            message = "数据治理步骤已完成，发布服务按本次计划跳过。"
        else:
            message = "数据治理完成。发布服务已由 Mock 平台确认成功。"
    elif phase == RunPhase.REJECTED.value:
        message = "治理计划被人工拒绝，未启动任何外部任务。"
    else:
        message = state.get("ui_message", "治理流程未完成，请查看失败步骤。")
    return {"pending_action": None, "ui_message": message, "audit": _audit(state, "RUN_FINISHED", phase=phase)}


def build_governance_graph(checkpointer: Any):
    """组装并编译治理图。

    生产环境必须显式传入 MongoDBSaver，避免误用进程内 MemorySaver 导致服务重启
    后丢失人工审核和异步任务等待状态。单元测试可显式注入 MemorySaver，以保持
    测试独立、快速且不触碰真实 MongoDB。
    """

    builder = StateGraph(GovernanceState)
    builder.add_node("prepare_plan", prepare_plan)
    builder.add_node("review_plan", review_plan, destinations=("execute_next", "finalize"))
    builder.add_node("execute_next", execute_next)
    builder.add_node("wait_external", wait_external)
    builder.add_node("resolve_external", resolve_external, destinations=("execute_next", "finalize"))
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare_plan")
    builder.add_edge("prepare_plan", "review_plan")
    builder.add_conditional_edges("execute_next", route_after_execute,
                                  {"wait_external": "wait_external", "finalize": "finalize"})
    builder.add_edge("wait_external", "resolve_external")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer, name="data_platform_governance")


async def main():
    """用 MongoDB 检查点生成当前工作流图，供直接运行本文件时学习。"""

    # 延迟导入使单元测试只导入图工厂时无需读取环境变量或连接数据库。
    if __package__ in (None, ""):
        from data_platform.checkpoint import MongoCheckpointRuntime
    else:
        from .checkpoint import MongoCheckpointRuntime

    runtime = MongoCheckpointRuntime()
    try:
        graph = build_governance_graph(runtime.open())
        graph.get_graph().draw_mermaid_png(output_file_path="data_platform_graph.png")
    finally:
        runtime.close()


if __name__ == '__main__':
    asyncio.run(main())
