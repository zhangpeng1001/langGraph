"""本地 Mock 数据平台任务引擎。

真实系统中的采集、质检、清洗、入库和发布通常由外部任务服务异步执行。
本模块用内存字典模拟同一契约：创建任务返回 external_task_id，稍后由按钮
触发回调。实现幂等键，因此 LangGraph 节点即使在检查点恢复时被重跑也不会
重复创建同一个外部任务。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .domain import GovernanceStep


def _now() -> str:
    """返回统一的 UTC ISO 时间，便于审计记录按字典序排序。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MockPlatform:
    """线程安全的本地任务引擎，模拟外部平台启动、回调和查询接口。"""

    def __init__(self) -> None:
        # API 请求与异步后台回调可能并发访问任务表，所以使用锁保护状态。
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}

    def start_task(self, *, run_id: str, step: GovernanceStep, idempotency_key: str) -> dict[str, Any]:
        """创建或复用一个 Mock 外部任务。

        输入的幂等键由 ``run_id + step + attempt`` 组成。重复调用只返回原任务，
        这正是 interrupt 恢复模型下保护外部副作用的关键。
        """

        with self._lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                return dict(self._tasks[existing_id])
            task_id = f"mock_{uuid.uuid4().hex[:10]}"
            task = {
                "task_id": task_id,
                "run_id": run_id,
                "step": step.value,
                "status": "PENDING",
                # 进度属于外部任务引擎的实时事实，不写入 LangGraph 检查点；管理器
                # 每次生成 API 快照时都会重新读取它，借此演示“状态快照 + 事件流”。
                "progress": 0,
                "idempotency_key": idempotency_key,
                "created_at": _now(),
                "message": "任务已提交，等待 Mock 平台回调",
            }
            self._tasks[task_id] = task
            self._idempotency[idempotency_key] = task_id
            return dict(task)

    def update_progress(self, task_id: str, *, progress: int, message: str) -> dict[str, Any]:
        """更新运行中任务的进度，供后台模拟器逐段报告状态。

        真实任务平台可能乱序或重复上报进度。这里拒绝终态后的更新，并把进度限定为
        0 到 99，保证只有 ``complete_task`` 才能写入 100% 和最终结果，避免前端把
        “看起来完成”误判为 LangGraph 已经恢复。
        """

        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(f"不存在的 Mock 任务：{task_id}")
            task = self._tasks[task_id]
            if task["status"] in {"SUCCEEDED", "FAILED"}:
                return dict(task)
            task["status"] = "RUNNING"
            task["progress"] = max(0, min(int(progress), 99))
            task["message"] = message
            return dict(task)

    def complete_task(self, task_id: str, *, success: bool, message: str) -> dict[str, Any]:
        """模拟异步平台回调并生成产物；未知任务会明确报错。"""

        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(f"不存在的 Mock 任务：{task_id}")
            task = self._tasks[task_id]
            # 回调天然可能重复投递；终态任务直接返回，保证恢复不会重复推进。
            if task["status"] in {"SUCCEEDED", "FAILED"}:
                return dict(task)
            task["status"] = "SUCCEEDED" if success else "FAILED"
            # 失败也代表模拟器已停止推进；以 100% 表示任务已经得到确定结果，而非
            # 把百分比错误地当作“成功率”。最终业务成功与否仍由 status 区分。
            task["progress"] = 100
            task["message"] = message
            task["completed_at"] = _now()
            if success and task["step"] == GovernanceStep.PUBLISH.value:
                task["service_url"] = f"http://mock.local/services/{task_id}"
                task["service_id"] = f"service_{task_id[5:]}"
            if success:
                task["artifact"] = f"artifact://{task['step'].lower()}/{task_id}"
            return dict(task)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """读取任务快照，返回副本避免调用方绕过锁修改内部状态。"""

        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def reset(self) -> None:
        """清空进程内 Mock 任务，主要供独立测试隔离全局模拟器状态。"""

        with self._lock:
            self._tasks.clear()
            self._idempotency.clear()


mock_platform = MockPlatform()
