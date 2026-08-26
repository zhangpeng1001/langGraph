"""LangGraph 运行管理器与本地异步回调桥。

管理器把 HTTP 的 run_id 映射到稳定的 LangGraph thread_id，并将所有恢复操作
集中到这里。这样 API 层不需要理解 interrupt 的内部结构，前端也只需点击按钮
模拟审核或平台回调即可。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langgraph.types import Command

from .graph import governance_graph
from .mock_platform import mock_platform


class RunManager:
    """在单进程内管理多个可恢复治理运行。"""

    def __init__(self) -> None:
        self._configs: dict[str, dict[str, Any]] = {}
        self._auto_tasks: dict[str, asyncio.Task[Any]] = {}

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        """生成 LangGraph 要求的稳定 configurable.thread_id。"""

        return {"configurable": {"thread_id": thread_id}}

    def create_run(self, request: str) -> dict[str, Any]:
        """创建运行并执行到首次人工审核中断。"""

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        thread_id = f"thread_{run_id}"
        config = self._config(thread_id)
        self._configs[run_id] = config
        governance_graph.invoke({"run_id": run_id, "thread_id": thread_id, "request": request}, config=config)
        return self.snapshot(run_id)

    def resume(self, run_id: str, value: Any) -> dict[str, Any]:
        """用 Command(resume=...) 恢复检查点，并返回新的状态快照。"""

        config = self._configs.get(run_id)
        if not config:
            raise KeyError(f"不存在的运行：{run_id}")
        governance_graph.invoke(Command(resume=value), config=config)
        return self.snapshot(run_id)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """读取 LangGraph 当前检查点，并整理成可 JSON 序列化的字典。"""

        config = self._configs.get(run_id)
        if not config:
            raise KeyError(f"不存在的运行：{run_id}")
        values = dict(governance_graph.get_state(config).values)
        values.setdefault("run_id", run_id)
        values.setdefault("thread_id", config["configurable"]["thread_id"])
        return values

    def complete_task(self, run_id: str, task_id: str, *, success: bool, message: str) -> dict[str, Any]:
        """先写入 Mock 平台终态，再恢复对应图线程，模拟可信回调入口。"""

        snapshot = self.snapshot(run_id)
        current = snapshot.get("current_task") or {}
        if current.get("task_id") != task_id:
            raise ValueError("该任务不是此运行当前等待的任务")
        mock_platform.complete_task(task_id, success=success, message=message)
        return self.resume(run_id, {"task_id": task_id})

    async def auto_complete(self, run_id: str, task_id: str, *, success: bool, delay: float, message: str) -> dict[str, Any]:
        """启动非阻塞后台等待，延迟后自动完成任务并恢复图。"""

        async def later() -> None:
            # asyncio.sleep 只挂起当前协程，不会像 time.sleep 一样阻塞整个 Web 服务。
            await asyncio.sleep(max(0.1, min(delay, 30.0)))
            try:
                self.complete_task(run_id, task_id, success=success, message=message)
            except (KeyError, ValueError):
                # 用户可能在延时期间取消运行或手动完成任务；此时外部回调已失去
                # 归属，静默丢弃即可，不能让后台任务产生未处理异常。
                pass
            finally:
                self._auto_tasks.pop(task_id, None)

        old = self._auto_tasks.get(task_id)
        if old and not old.done():
            return self.snapshot(run_id)
        self._auto_tasks[task_id] = asyncio.create_task(later())
        return self.snapshot(run_id)


run_manager = RunManager()
