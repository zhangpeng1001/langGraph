"""LangGraph 运行管理器、自动任务模拟器和实时事件发布入口。

HTTP、WebSocket 与后台协程都通过本模块操作同一条 LangGraph 运行。管理器负责把
“检查点状态发生变化”转换成两种实时通道：详细事件流，以及只提示前端刷新快照的
Mock MQTT 通知，从而清晰展示它们各自的职责边界。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

from langgraph.types import Command

from .mock_platform import mock_platform
from .realtime import RealtimeService, realtime_service


_TERMINAL_PHASES = {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"}
_APPROVED_DECISIONS = {"approve", "approved", "yes", "y", "批准", "同意"}


class RunManager:
    """管理可恢复治理运行，并协调自动任务、事件流和 Mock MQTT 通知。"""

    def __init__(self, graph: Any | None = None, *, realtime: RealtimeService | None = None) -> None:
        # 图在 FastAPI lifespan 中由 MongoDBSaver 编译后注入；测试可传入使用
        # MemorySaver 编译的图，确保测试不依赖外部 MongoDB。
        self._graph = graph
        self._realtime = realtime or realtime_service
        self._state_lock = threading.RLock()
        # 自动协程必须运行在 FastAPI 主事件循环；图 invoke 则位于线程池，因此保存
        # 启动期事件循环并通过 call_soon_threadsafe 安全调度。
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._auto_tasks: dict[str, asyncio.Task[Any]] = {}
        # Mock 回调和人工按钮可能在相近时间完成。同一运行使用可重入锁串行化，保证
        # 只有一个调用能完成“校验任务 -> 写终态 -> Command(resume)”这一原子步骤。
        self._run_locks: dict[str, threading.RLock] = {}

    @property
    def realtime(self) -> RealtimeService:
        """暴露实时服务给 API WebSocket 路由，避免路由绕过管理器创建另一套事件表。"""

        return self._realtime

    def configure(self, graph: Any, *, event_loop: asyncio.AbstractEventLoop | None = None) -> None:
        """绑定已编译图和可调度后台模拟器的主事件循环。"""

        with self._state_lock:
            self._graph = graph
            if event_loop is not None:
                self._event_loop = event_loop

    def reset(self) -> None:
        """服务关闭时取消后台任务并释放仅进程内存在的实时资源。"""

        with self._state_lock:
            auto_tasks = list(self._auto_tasks.values())
            loop = self._event_loop
            self._auto_tasks.clear()
            self._run_locks.clear()
            self._graph = None
            self._event_loop = None
        for task in auto_tasks:
            if not task.done() and loop is not None and not loop.is_closed():
                # Task 归属于 FastAPI 主循环，跨线程直接 cancel 可能造成竞态，所以始终
                # 让所属事件循环完成取消操作。
                loop.call_soon_threadsafe(task.cancel)
        self._realtime.clear()

    def _require_graph(self) -> Any:
        """返回已配置图；未启动完成时给出明确的服务状态错误。"""

        if self._graph is None:
            raise RuntimeError("治理服务尚未完成 MongoDB 检查点初始化。")
        return self._graph

    def _run_lock(self, run_id: str) -> threading.RLock:
        """获取运行级锁；锁按运行隔离，不会让不同任务互相阻塞。"""

        with self._state_lock:
            return self._run_locks.setdefault(run_id, threading.RLock())

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        """生成 LangGraph 要求的稳定 configurable.thread_id。"""

        return {"configurable": {"thread_id": thread_id}}

    @classmethod
    def _config_for_run(cls, run_id: str) -> dict[str, Any]:
        """从 run_id 推导稳定 thread_id，避免仅依赖进程内映射表。"""

        return cls._config(f"thread_{run_id}")

    @staticmethod
    def _public_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
        """过滤外部任务的内部字段，只留下前端展示进度所需的安全数据。"""

        if not task:
            return None
        return {
            "task_id": str(task.get("task_id", "")),
            "step": str(task.get("step", "")),
            "status": str(task.get("status", "PENDING")),
            "progress": max(0, min(int(task.get("progress", 0)), 100)),
            "message": str(task.get("message", "")),
        }

    def _snapshot_unlocked(self, run_id: str) -> dict[str, Any]:
        """读取检查点并叠加 Mock 平台的实时进度；调用方必须已持有运行锁。"""

        graph = self._require_graph()
        config = self._config_for_run(run_id)
        values = dict(graph.get_state(config).values)
        # run_id 是本图的首个输入字段，以它核对 thread_id 推导结果，防止调用方
        # 使用格式相似但实际指向其他运行的 ID。
        if values.get("run_id") != run_id:
            raise KeyError(f"不存在的运行：{run_id}")
        values.setdefault("run_id", run_id)
        values.setdefault("thread_id", config["configurable"]["thread_id"])
        checkpoint_task = values.get("current_task")
        if isinstance(checkpoint_task, dict):
            # 进度更新频繁且只是外部任务事实，若每 25% 都恢复图会人为增加 checkpoint
            # 噪声。因此查询快照时读取 Mock 平台最新副本，再输出脱敏字段。
            live_task = mock_platform.get_task(str(checkpoint_task.get("task_id", "")))
            values["current_task"] = self._public_task(live_task or checkpoint_task)
        else:
            values["current_task"] = None
        return values

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """读取可 JSON 序列化的运行快照，供 REST 和 WebSocket 初始状态共用。"""

        with self._run_lock(run_id):
            return self._snapshot_unlocked(run_id)

    def _resume_unlocked(self, run_id: str, value: Any) -> dict[str, Any]:
        """用 Command(resume=...) 恢复图；调用方必须已持有对应运行锁。"""

        graph = self._require_graph()
        config = self._config_for_run(run_id)
        # 先确认检查点存在；避免对不存在的 thread_id 发送 resume 后得到误导结果。
        if not graph.get_state(config).values:
            raise KeyError(f"不存在的运行：{run_id}")
        graph.invoke(Command(resume=value), config=config)
        return self._snapshot_unlocked(run_id)

    def _publish(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        task: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        notify_mqtt: bool = True,
    ) -> None:
        """统一发布事件，确保 MQTT 通知和详细 WebSocket 事件使用同一序号。"""

        payload = dict(data or {})
        public_task = self._public_task(task)
        if public_task is not None:
            payload["task"] = public_task
        self._realtime.publish_run_event(
            run_id=run_id,
            event_type=event_type,
            message=message,
            task_id=public_task["task_id"] if public_task else None,
            data=payload,
            notify_mqtt=notify_mqtt,
        )

    def _publish_transition(self, before: dict[str, Any], after: dict[str, Any], *, trigger: str) -> None:
        """依据状态前后差异发布业务事件，并在新任务出现时启动自动模拟。"""

        run_id = str(after["run_id"])
        if trigger == "review_approved":
            self._publish(run_id, "review.approved", "人工审核已批准，治理步骤开始执行。")
        elif trigger == "review_rejected":
            self._publish(run_id, "review.rejected", "人工审核已拒绝，治理运行安全结束。")
        elif trigger == "run_cancelled":
            self._publish(run_id, "run.cancelled", "用户已取消治理运行。")

        previous_task = before.get("current_task") or {}
        current_task = after.get("current_task") or {}
        if current_task and current_task.get("task_id") != previous_task.get("task_id"):
            step = str(current_task.get("step", "未知步骤"))
            self._publish(
                run_id,
                "task.started",
                f"{step} Mock 外部任务已启动，后台将自动上报进度。",
                task=current_task,
            )
            self._schedule_auto_task(run_id, current_task)

        previous_phase = str(before.get("phase", ""))
        current_phase = str(after.get("phase", ""))
        if (
            current_phase in _TERMINAL_PHASES
            and current_phase != previous_phase
            # 取消恢复本身已经发布 run.cancelled，避免同一事实出现两条完全相同通知。
            and not (trigger == "run_cancelled" and current_phase == "CANCELLED")
        ):
            terminal_event = {
                "SUCCEEDED": ("run.completed", "治理运行已完成，可以进入交流页面查看实时记录。"),
                "FAILED": ("run.failed", "治理运行失败，后续步骤已停止。"),
                "REJECTED": ("run.rejected", "治理计划被人工拒绝，未启动外部任务。"),
                "CANCELLED": ("run.cancelled", "治理运行已取消。"),
            }[current_phase]
            self._publish(run_id, terminal_event[0], terminal_event[1], data={"phase": current_phase})

    @staticmethod
    def _review_trigger(value: Any) -> str | None:
        """从审核恢复值判断应发布批准、拒绝还是取消事件。"""

        if not isinstance(value, dict):
            return None
        if value.get("task_id") == "__cancel__":
            return "run_cancelled"
        if "decision" not in value:
            return None
        decision = str(value.get("decision", "")).strip().lower()
        return "review_approved" if decision in _APPROVED_DECISIONS else "review_rejected"

    def create_run(self, request: str) -> dict[str, Any]:
        """创建运行、执行到人工审核断点，并发布首次 MQTT/事件流消息。"""

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        thread_id = f"thread_{run_id}"
        with self._run_lock(run_id):
            graph = self._require_graph()
            graph.invoke(
                {"run_id": run_id, "thread_id": thread_id, "request": request},
                config=self._config_for_run(run_id),
            )
            snapshot = self._snapshot_unlocked(run_id)
        self._publish(run_id, "run.created", "治理运行已创建，已生成待审核计划。")
        self._publish(run_id, "run.waiting_review", "LangGraph 已在人工审核断点暂停。")
        return snapshot

    def resume(self, run_id: str, value: Any) -> dict[str, Any]:
        """恢复审核或取消断点，并在新的外部任务出现时自动启动进度模拟。"""

        with self._run_lock(run_id):
            before = self._snapshot_unlocked(run_id)
            if isinstance(value, dict) and value.get("task_id") == "__cancel__":
                # 取消外部等待时先停止自动模拟，避免图结束后仍有后台任务继续产生进度。
                current_task = before.get("current_task") or {}
                if current_task.get("task_id"):
                    self._cancel_auto_task(str(current_task["task_id"]))
            after = self._resume_unlocked(run_id, value)
            self._publish_transition(before, after, trigger=self._review_trigger(value) or "resume")
            return after

    def _complete_task(
        self,
        run_id: str,
        task_id: str,
        *,
        success: bool,
        message: str,
        cancel_simulator: bool,
    ) -> dict[str, Any]:
        """原子化完成任务并恢复图，供手动回调和自动模拟共同复用。"""

        with self._run_lock(run_id):
            before = self._snapshot_unlocked(run_id)
            current = before.get("current_task") or {}
            if current.get("task_id") != task_id:
                raise ValueError("该任务不是此运行当前等待的任务")
            task = mock_platform.complete_task(task_id, success=success, message=message)
            if cancel_simulator:
                self._cancel_auto_task(task_id)
            event_type = "task.succeeded" if success else "task.failed"
            self._publish(run_id, event_type, message, task=task)
            after = self._resume_unlocked(run_id, {"task_id": task_id})
            self._publish_transition(before, after, trigger="task_callback")
            return after

    def complete_task(self, run_id: str, task_id: str, *, success: bool, message: str) -> dict[str, Any]:
        """手动模拟可信回调；手动操作优先结束同一任务的自动进度协程。"""

        return self._complete_task(
            run_id,
            task_id,
            success=success,
            message=message,
            cancel_simulator=True,
        )

    def _update_progress(self, run_id: str, task_id: str, *, progress: int, message: str) -> bool:
        """写入一段外部任务进度；任务已被人工处理时安全停止自动模拟。"""

        with self._run_lock(run_id):
            snapshot = self._snapshot_unlocked(run_id)
            current = snapshot.get("current_task") or {}
            if current.get("task_id") != task_id:
                return False
            task = mock_platform.update_progress(task_id, progress=progress, message=message)
            if task.get("status") in {"SUCCEEDED", "FAILED"}:
                return False
            self._publish(run_id, "task.progress", message, task=task, data={"progress": progress})
            return True

    async def _simulate_task(
        self,
        run_id: str,
        task_id: str,
        *,
        delay: float,
        success: bool,
        message: str,
    ) -> None:
        """以四段非阻塞等待模拟外部平台，最后用正常回调路径恢复 LangGraph。"""

        current_task = asyncio.current_task()
        try:
            interval = max(0.2, min(delay, 30.0)) / 4
            for progress in (25, 50, 75):
                await asyncio.sleep(interval)
                updated = await asyncio.to_thread(
                    self._update_progress,
                    run_id,
                    task_id,
                    progress=progress,
                    message=f"Mock 后台任务执行中：{progress}%",
                )
                if not updated:
                    return
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                self._complete_task,
                run_id,
                task_id,
                success=success,
                message=message,
                cancel_simulator=False,
            )
        except asyncio.CancelledError:
            # 人工成功/失败或应用关闭会取消协程；这是预期控制流，不能伪造失败事件。
            raise
        except (KeyError, ValueError, RuntimeError) as exc:
            # 在取消与回调并发的极小窗口内，图可能已经推进到下一任务。记录学习用途的
            # 事件即可，不重新抛出造成“Task exception was never retrieved”。
            self._publish(run_id, "task.simulation_stopped", f"自动模拟已停止：{exc}")
        finally:
            with self._state_lock:
                if self._auto_tasks.get(task_id) is current_task:
                    self._auto_tasks.pop(task_id, None)

    def _schedule_auto_task(
        self,
        run_id: str,
        task: dict[str, Any],
        *,
        delay: float = 2.0,
        success: bool = True,
        message: str = "Mock 后台异步任务已完成",
        replace: bool = False,
    ) -> None:
        """把自动模拟注册到主事件循环；重复注册默认复用已有协程。"""

        task_id = str(task.get("task_id", ""))
        if not task_id:
            return
        with self._state_lock:
            loop = self._event_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            # 直接在同步脚本中使用 RunManager 时没有 Web 服务事件循环，此时仍可依赖
            # 手动回调完成学习流程，不应偷偷创建无法清理的线程循环。
            return

        def install() -> None:
            with self._state_lock:
                # reset 可能发生在 call_soon_threadsafe 排队之后；此时不能在已关闭
                # 管理器上重新创建后台协程，否则服务关闭后仍会有悬挂任务。
                if self._event_loop is not loop:
                    return
                existing = self._auto_tasks.get(task_id)
                if existing and not existing.done():
                    if not replace:
                        return
                    existing.cancel()
                self._auto_tasks[task_id] = asyncio.create_task(
                    self._simulate_task(
                        run_id,
                        task_id,
                        delay=delay,
                        success=success,
                        message=message,
                    )
                )

        loop.call_soon_threadsafe(install)

    def _cancel_auto_task(self, task_id: str) -> None:
        """请求取消指定任务的自动协程；真正取消必须在协程所属循环执行。"""

        with self._state_lock:
            task = self._auto_tasks.get(task_id)
            loop = self._event_loop
        if task and not task.done() and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    async def auto_complete(
        self,
        run_id: str,
        task_id: str,
        *,
        success: bool,
        delay: float,
        message: str,
    ) -> dict[str, Any]:
        """兼容旧自动回调接口，并允许调用方重设当前任务的自动终态场景。"""

        snapshot = await asyncio.to_thread(self.snapshot, run_id)
        current = snapshot.get("current_task") or {}
        if current.get("task_id") != task_id:
            raise ValueError("该任务不是此运行当前等待的任务")
        self._schedule_auto_task(
            run_id,
            current,
            delay=delay,
            success=success,
            message=message,
            replace=True,
        )
        return snapshot

    def send_chat(self, run_id: str, content: str) -> None:
        """记录用户交流消息并生成确定性本地回复，不调用任何真实模型服务。"""

        cleaned = content.strip()
        if not cleaned:
            raise ValueError("交流消息不能为空")
        if len(cleaned) > 500:
            raise ValueError("交流消息不能超过 500 个字符")
        snapshot = self.snapshot(run_id)
        self._publish(
            run_id,
            "chat.user",
            f"用户：{cleaned}",
            data={"role": "user", "content": cleaned},
            notify_mqtt=False,
        )
        phase = str(snapshot.get("phase", "未知状态"))
        task = snapshot.get("current_task") or {}
        if task:
            reply = (
                f"本地学习助手：当前运行处于 {phase}，正在等待 {task.get('step', '外部')}任务"
                f"（{task.get('progress', 0)}%）。已收到你的消息，会继续通过实时事件更新。"
            )
        elif phase in _TERMINAL_PHASES:
            reply = f"本地学习助手：当前运行已进入 {phase}。你可以结合上方事件记录回顾断点与恢复过程。"
        else:
            reply = f"本地学习助手：当前运行处于 {phase}，请根据页面提示继续审核或等待任务状态变化。"
        self._publish(
            run_id,
            "chat.assistant",
            reply,
            data={"role": "assistant", "content": reply},
            notify_mqtt=False,
        )


run_manager = RunManager()
