"""数据中台学习项目的 FastAPI 后端。

接口在 FastAPI 生命周期启动 MongoDBSaver；状态由 MongoDB 持久化，MockPlatform
只模拟外部数据任务。这样审核中断可以在服务重启后按原 thread_id 恢复。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .checkpoint import MongoCheckpointRuntime
from .domain import CompleteTaskRequest, CreateRunRequest, ReviewRequest, RunSnapshot
from .graph import build_governance_graph
from .manager import run_manager


BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    """在 Web 服务启动/关闭时管理 MongoDB 连接池与治理图实例。"""

    runtime = MongoCheckpointRuntime()
    try:
        # 连接、ping 和索引创建都在启动期完成；失败时服务拒绝启动，避免运行到
        # 人工审核后才发现无法持久化 checkpoint。
        checkpointer = await asyncio.to_thread(runtime.open)
        # 管理器需要保存当前主循环，才能让线程池中的 LangGraph 回调安全调度自动
        # 进度协程；不能在工作线程中直接 asyncio.create_task。
        run_manager.configure(build_governance_graph(checkpointer), event_loop=asyncio.get_running_loop())
        yield
    finally:
        run_manager.reset()
        await asyncio.to_thread(runtime.close)


app = FastAPI(
    title="LangGraph 数据中台治理学习项目",
    version="1.2.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """返回无需构建工具的单页前端。"""

    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """健康检查，方便学习者确认服务已经启动。"""

    return {"status": "ok", "service": "data-platform-learning"}


async def _next_socket_signals(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, Any]],
) -> list[tuple[str, dict[str, Any] | None]]:
    """同时等待客户端输入和后台队列，避免只发消息时无法及时释放断开订阅。

    WebSocket 的 receive 与 asyncio.Queue 的 get 都会阻塞。每轮创建两个短生命周期
    Task。未完成的等待项会取消；若恰好同时收到事件和客户端消息，则两个信号都会
    返回，避免在高频进度推送期间丢失用户聊天输入。
    """

    queue_task = asyncio.create_task(queue.get())
    receive_task = asyncio.create_task(websocket.receive())
    done, pending = await asyncio.wait({queue_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    signals: list[tuple[str, dict[str, Any] | None]] = []
    if queue_task in done:
        signals.append(("event", queue_task.result()))
    if receive_task in done:
        message = receive_task.result()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(code=message.get("code", 1000))
        raw_text = message.get("text")
        if raw_text is None:
            signals.append(("client", None))
        else:
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                signals.append(("client_invalid", None))
            else:
                signals.append(("client", payload if isinstance(payload, dict) else None))
    return signals


def _valid_mock_mqtt_topic(topic: str) -> bool:
    """限制浏览器只订阅本教学项目约定的运行通知主题，避免演示接口变成任意通道。"""

    prefix = "governance/runs/"
    return topic.startswith(prefix) and (topic.endswith("/#") or topic.endswith("/notifications"))


@app.websocket("/ws/mock-mqtt")
async def mock_mqtt_websocket(websocket: WebSocket) -> None:
    """将进程内 Mock MQTT 订阅桥接为浏览器可直接使用的 WebSocket。

    浏览器先发送一次 ``subscribe`` 指令；随后每条轻量通知只表示“状态可能变化”，
    前端必须继续调用 REST 快照接口，不能把通知本身当成最终业务事实。
    """

    await websocket.accept()
    subscription_id: str | None = None
    try:
        initial = await websocket.receive_json()
        topic = str(initial.get("topic", "")) if isinstance(initial, dict) else ""
        if not isinstance(initial, dict) or initial.get("action") != "subscribe" or not _valid_mock_mqtt_topic(topic):
            await websocket.send_json({"type": "error", "message": "请使用约定的 Mock MQTT 主题订阅格式。"})
            await websocket.close(code=1008)
            return
        subscription = run_manager.realtime.broker.subscribe(topic)
        subscription_id = subscription.subscription_id
        await websocket.send_json({"type": "mqtt_subscribed", "topic": topic})
        while True:
            for signal_type, payload in await _next_socket_signals(websocket, subscription.queue):
                if signal_type == "event" and payload is not None:
                    await websocket.send_json(payload)
                elif signal_type == "client_invalid":
                    await websocket.send_json({"type": "error", "message": "WebSocket 消息必须是 JSON 对象。"})
                elif signal_type == "client":
                    # 已订阅连接不支持动态换主题，保持一次连接只观察一个运行，便于学习
                    # 生命周期和资源释放；前端切换任务时应重新建立 MQTT WebSocket。
                    await websocket.send_json({"type": "error", "message": "已订阅主题不可修改，请重新连接。"})
    except WebSocketDisconnect:
        pass
    finally:
        if subscription_id is not None:
            run_manager.realtime.broker.unsubscribe(subscription_id)


@app.websocket("/ws/runs/{run_id}")
async def run_event_websocket(
    websocket: WebSocket,
    run_id: str,
    after: int = Query(default=0, ge=0),
) -> None:
    """提供快照、可重放事件和双向本地交流消息的运行级 WebSocket。"""

    await websocket.accept()
    subscription_id: str | None = None
    try:
        try:
            snapshot = await asyncio.to_thread(run_manager.snapshot, run_id)
        except (KeyError, RuntimeError) as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close(code=4404)
            return

        # 先登记订阅再读取事件回放，确保此刻开始发生的事件进入队列，不会落在
        # “已读取历史、尚未订阅”这个经典实时通信空窗中。
        subscription, replay = run_manager.realtime.events.subscribe(run_id, after=after)
        subscription_id = subscription.subscription_id
        await websocket.send_json({"type": "snapshot", "snapshot": snapshot})
        for event in replay:
            await websocket.send_json({"type": "event", "event": event})

        while True:
            for signal_type, payload in await _next_socket_signals(websocket, subscription.queue):
                if signal_type == "event" and payload is not None:
                    await websocket.send_json({"type": "event", "event": payload})
                    continue
                if signal_type == "client_invalid" or payload is None:
                    await websocket.send_json({"type": "error", "message": "消息必须是 JSON 对象。"})
                    continue
                if payload.get("type") != "chat.send":
                    await websocket.send_json({"type": "error", "message": "仅支持 chat.send 类型的交流消息。"})
                    continue
                try:
                    await asyncio.to_thread(run_manager.send_chat, run_id, str(payload.get("content", "")))
                except (KeyError, RuntimeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        if subscription_id is not None:
            run_manager.realtime.events.unsubscribe(subscription_id)


@app.post("/api/runs", response_model=RunSnapshot)
async def create_run(payload: CreateRunRequest) -> dict[str, Any]:
    """创建运行并暂停在计划人工审核节点。"""

    return await asyncio.to_thread(run_manager.create_run, payload.request)


@app.get("/api/runs/{run_id}", response_model=RunSnapshot)
async def get_run(run_id: str) -> dict[str, Any]:
    """读取当前检查点与 Mock 任务实时进度，供 MQTT 通知后的前端快照刷新。"""

    try:
        return await asyncio.to_thread(run_manager.snapshot, run_id)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/review", response_model=RunSnapshot)
async def review_run(run_id: str, payload: ReviewRequest) -> dict[str, Any]:
    """提交人工审核决定并恢复图线程。"""

    try:
        return await asyncio.to_thread(
            run_manager.resume,
            run_id,
            {"decision": payload.decision, "comment": payload.comment},
        )
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/tasks/{task_id}/complete", response_model=RunSnapshot)
async def complete_task(run_id: str, task_id: str, payload: CompleteTaskRequest) -> dict[str, Any]:
    """立即模拟一次外部回调，可选择成功或失败以观察分支。"""

    try:
        return await asyncio.to_thread(
            run_manager.complete_task,
            run_id,
            task_id,
            success=payload.success,
            message=payload.message,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/tasks/{task_id}/auto-complete", response_model=RunSnapshot)
async def auto_complete_task(run_id: str, task_id: str, payload: CompleteTaskRequest) -> dict[str, Any]:
    """兼容旧自动回调接口，可重设已经自动运行的 Mock 任务终态场景。"""

    try:
        return await run_manager.auto_complete(run_id, task_id, success=payload.success, delay=2.0, message=payload.message)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/cancel", response_model=RunSnapshot)
async def cancel_run(run_id: str) -> dict[str, Any]:
    """通过恢复挂起节点结束运行，展示可取消的人机协作边界。"""

    try:
        return await asyncio.to_thread(run_manager.resume, run_id, {"task_id": "__cancel__"})
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
