"""数据中台学习项目的 FastAPI 后端。

接口只依赖本进程内的 LangGraph MemorySaver 和 MockPlatform，因此启动后即可在
浏览器中学习完整流程。真实项目可将 RunManager 的检查点和平台适配器替换为
数据库、Redis 或真实任务引擎，而无需改动前端契约。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .domain import CompleteTaskRequest, CreateRunRequest, ReviewRequest, RunSnapshot
from .manager import run_manager


BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="LangGraph 数据中台治理学习项目", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """返回无需构建工具的单页前端。"""

    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """健康检查，方便学习者确认服务已经启动。"""

    return {"status": "ok", "service": "data-platform-learning"}


@app.post("/api/runs", response_model=RunSnapshot)
async def create_run(payload: CreateRunRequest) -> dict[str, Any]:
    """创建运行并暂停在计划人工审核节点。"""

    return run_manager.create_run(payload.request)


@app.get("/api/runs/{run_id}", response_model=RunSnapshot)
async def get_run(run_id: str) -> dict[str, Any]:
    """读取当前检查点，前端可轮询观察异步任务恢复结果。"""

    try:
        return run_manager.snapshot(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/review", response_model=RunSnapshot)
async def review_run(run_id: str, payload: ReviewRequest) -> dict[str, Any]:
    """提交人工审核决定并恢复图线程。"""

    try:
        return run_manager.resume(run_id, {"decision": payload.decision, "comment": payload.comment})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/tasks/{task_id}/complete", response_model=RunSnapshot)
async def complete_task(run_id: str, task_id: str, payload: CompleteTaskRequest) -> dict[str, Any]:
    """立即模拟一次外部回调，可选择成功或失败以观察分支。"""

    try:
        return run_manager.complete_task(run_id, task_id, success=payload.success, message=payload.message)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/tasks/{task_id}/auto-complete", response_model=RunSnapshot)
async def auto_complete_task(run_id: str, task_id: str, payload: CompleteTaskRequest) -> dict[str, Any]:
    """启动后台延时回调，演示异步等待期间 Web 服务仍可响应。"""

    try:
        return await run_manager.auto_complete(run_id, task_id, success=payload.success, delay=2.0, message=payload.message)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/cancel", response_model=RunSnapshot)
async def cancel_run(run_id: str) -> dict[str, Any]:
    """通过恢复挂起节点结束运行，展示可取消的人机协作边界。"""

    try:
        return run_manager.resume(run_id, {"task_id": "__cancel__"})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
