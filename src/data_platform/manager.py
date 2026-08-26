"""LangGraph 运行管理器与本地异步回调桥。

管理器从 HTTP 的 run_id 确定性推导稳定的 LangGraph thread_id，并将所有恢复操作
集中到这里。这样 API 层不需要理解 interrupt 的内部结构，前端也只需点击按钮
模拟审核或平台回调即可；服务重启后无需依赖进程内映射表。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langgraph.types import Command

from .mock_platform import mock_platform


class RunManager:
    """管理可恢复治理运行，并通过确定性 thread_id 支持服务重启后恢复。"""

    def __init__(self, graph: Any | None = None) -> None:
        # 图在 FastAPI lifespan 中由 MongoDBSaver 编译后注入；测试可传入使用
        # MemorySaver 编译的图，确保测试不依赖外部 MongoDB。
        self._graph = graph
        self._auto_tasks: dict[str, asyncio.Task[Any]] = {}

    def configure(self, graph: Any) -> None:
        """绑定已编译的 MongoDB 持久化图，服务启动完成后调用一次。"""

        self._graph = graph

    def reset(self) -> None:
        """服务关闭时解绑图，防止关闭后的 MongoClient 被后续请求误用。"""

        self._graph = None

    def _require_graph(self) -> Any:
        """返回已配置图；未启动完成时给出明确的服务状态错误。"""

        if self._graph is None:
            raise RuntimeError("治理服务尚未完成 MongoDB 检查点初始化。")
        return self._graph

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        """生成 LangGraph 要求的稳定 configurable.thread_id。"""

        return {"configurable": {"thread_id": thread_id}}

    @classmethod
    def _config_for_run(cls, run_id: str) -> dict[str, Any]:
        """从 run_id 推导稳定 thread_id，避免仅依赖进程内映射表。

        初始创建和后续审核/回调使用同一规则，因此应用重启后仍能从 MongoDB
        checkpoint 中精确恢复此前暂停的线程。
        """

        return cls._config(f"thread_{run_id}")

    def create_run(self, request: str) -> dict[str, Any]:
        """创建运行并执行到首次人工审核中断。"""

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        thread_id = f"thread_{run_id}"
        graph = self._require_graph()
        config = self._config_for_run(run_id)
        graph.invoke(
            {"run_id": run_id, "thread_id": thread_id, "request": request},
            config=config,
        )
        return self.snapshot(run_id)

    def resume(self, run_id: str, value: Any) -> dict[str, Any]:
        """用 Command(resume=...) 恢复检查点，并返回新的状态快照。"""

        graph = self._require_graph()
        config = self._config_for_run(run_id)
        # 先确认检查点存在；避免对不存在的 thread_id 发送 resume 后得到误导结果。
        if not graph.get_state(config).values:
            raise KeyError(f"不存在的运行：{run_id}")
        graph.invoke(Command(resume=value), config=config)
        return self.snapshot(run_id)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """读取 LangGraph 当前检查点，并整理成可 JSON 序列化的字典。"""

        graph = self._require_graph()
        config = self._config_for_run(run_id)
        values = dict(graph.get_state(config).values)
        # run_id 是本图的首个输入字段，以它核对 thread_id 推导结果，防止调用方
        # 使用格式相似但实际指向其他运行的 ID。
        if values.get("run_id") != run_id:
            raise KeyError(f"不存在的运行：{run_id}")
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
                # MongoDBSaver 是同步实现，在线程池中执行恢复操作，避免占用
                # FastAPI 的 asyncio 事件循环。
                await asyncio.to_thread(
                    self.complete_task,
                    run_id,
                    task_id,
                    success=success,
                    message=message,
                )
            except (KeyError, ValueError, RuntimeError):
                # 用户可能在延时期间取消运行或手动完成任务；此时外部回调已失去
                # 归属，静默丢弃即可，不能让后台任务产生未处理异常。
                pass
            finally:
                self._auto_tasks.pop(task_id, None)

        old = self._auto_tasks.get(task_id)
        if old and not old.done():
            return await asyncio.to_thread(self.snapshot, run_id)
        self._auto_tasks[task_id] = asyncio.create_task(later())
        return await asyncio.to_thread(self.snapshot, run_id)


run_manager = RunManager()
