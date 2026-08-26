"""数据中台治理图的 MongoDB 检查点运行时。

本模块只负责创建、复用和关闭 LangGraph 的 MongoDB checkpointer。它不会保存
业务运行模型；LangGraph 会按 ``thread_id`` 将图状态、interrupt 暂停位置和中间
写入保存到 MongoDB。连接信息始终来自环境变量，避免把数据库密码提交到仓库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.mongodb import MongoDBSaver
from pymongo import MongoClient
from pymongo.errors import PyMongoError


class MongoCheckpointConfigurationError(RuntimeError):
    """MongoDB 检查点的环境变量缺失或格式无效时抛出。"""


class MongoCheckpointConnectionError(RuntimeError):
    """MongoDB 无法连接、鉴权失败或索引初始化失败时抛出。"""


@dataclass(frozen=True)
class MongoCheckpointSettings:
    """创建 MongoDBSaver 所需的、非敏感配置项。

    URI 会包含用户名和密码，因此只从 ``MONGODB_URI`` 读取且绝不写入日志或
    API 响应。数据库名单独配置，避免依赖 URI 的默认数据库解析行为。
    """

    uri: str
    database: str
    checkpoint_collection: str
    writes_collection: str
    server_selection_timeout_ms: int

    @classmethod
    def from_environment(cls) -> "MongoCheckpointSettings":
        """读取 ``.env`` 和进程环境，并校验 MongoDB 检查点配置。

        ``load_dotenv`` 不会覆盖部署环境已有变量，因此生产环境可由 Secret、
        Kubernetes 或 CI 安全注入 URI。本项目不会为 URI 提供带凭据的默认值。
        """

        load_dotenv()
        uri = os.getenv("MONGODB_URI", "").strip()
        database = os.getenv("MONGODB_DATABASE", "potato_data_platform").strip()
        checkpoint_collection = os.getenv(
            "MONGODB_CHECKPOINT_COLLECTION", "langgraph_checkpoints"
        ).strip()
        writes_collection = os.getenv(
            "MONGODB_CHECKPOINT_WRITES_COLLECTION", "langgraph_checkpoint_writes"
        ).strip()
        raw_timeout = os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "5000").strip()

        if not uri:
            raise MongoCheckpointConfigurationError(
                "缺少 MONGODB_URI。请在 .env 或部署环境中配置 MongoDB 连接串。"
            )
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            raise MongoCheckpointConfigurationError(
                "MONGODB_URI 必须以 mongodb:// 或 mongodb+srv:// 开头。"
            )
        if not database or not checkpoint_collection or not writes_collection:
            raise MongoCheckpointConfigurationError(
                "MONGODB_DATABASE、MONGODB_CHECKPOINT_COLLECTION 和 "
                "MONGODB_CHECKPOINT_WRITES_COLLECTION 均不能为空。"
            )
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise MongoCheckpointConfigurationError(
                "MONGODB_SERVER_SELECTION_TIMEOUT_MS 必须是正整数。"
            ) from exc
        if timeout <= 0:
            raise MongoCheckpointConfigurationError(
                "MONGODB_SERVER_SELECTION_TIMEOUT_MS 必须大于 0。"
            )
        return cls(
            uri=uri,
            database=database,
            checkpoint_collection=checkpoint_collection,
            writes_collection=writes_collection,
            server_selection_timeout_ms=timeout,
        )


class MongoCheckpointRuntime:
    """管理单个 FastAPI 进程内 MongoClient 和 MongoDBSaver 的生命周期。

    MongoDBSaver 0.1.x 在构造时会创建必要的复合索引。运行时显式 ``ping``，能在
    服务启动阶段尽早发现网络、DNS 或鉴权问题，而不是在首个审核请求时才失败。
    """

    def __init__(self, settings: MongoCheckpointSettings | None = None) -> None:
        self._settings = settings
        self._client: MongoClient[Any] | None = None
        self._checkpointer: MongoDBSaver | None = None

    def open(self) -> MongoDBSaver:
        """建立连接并返回可传给 ``StateGraph.compile`` 的持久化 saver。

        多次调用只复用同一个 saver，防止 FastAPI 的多个请求重复创建连接池和
        索引；关闭后的实例不可再次复用，应在新进程生命周期中重新创建。
        """

        if self._checkpointer is not None:
            return self._checkpointer
        settings = self._settings or MongoCheckpointSettings.from_environment()
        try:
            client = MongoClient(
                settings.uri,
                # 设置较短超时，避免数据库不可用时卡住 Web 服务启动。
                serverSelectionTimeoutMS=settings.server_selection_timeout_ms,
                connectTimeoutMS=settings.server_selection_timeout_ms,
                # 由 MongoClient 管理连接池，所有请求共享而非逐请求创建连接。
                appname="langgraph-data-platform-learning",
            )
            client.admin.command("ping")
            checkpointer = MongoDBSaver(
                client,
                db_name=settings.database,
                checkpoint_collection_name=settings.checkpoint_collection,
                writes_collection_name=settings.writes_collection,
            )
        except PyMongoError as exc:
            if "client" in locals():
                client.close()
            raise MongoCheckpointConnectionError(
                "无法连接 MongoDB 或初始化 LangGraph 检查点索引；请检查网络、"
                "MONGODB_URI、数据库权限和目标库是否可访问。"
            ) from exc
        self._client = client
        self._checkpointer = checkpointer
        return checkpointer

    def close(self) -> None:
        """在服务退出时关闭 MongoClient，释放连接池和网络资源。"""

        if self._client is not None:
            self._client.close()
        self._client = None
        self._checkpointer = None
