"""数据中台学习项目的进程内实时通信组件。

本模块刻意不接入真实 MQTT Broker。``MockMqttBroker`` 保留发布/订阅与主题匹配
这两个学习重点，再由 FastAPI WebSocket 将通知桥接给浏览器；``RunEventStore`` 则
保存较完整的运行事件，解决 WebSocket 在前端建立连接前错过首条消息的问题。
"""

from __future__ import annotations

import asyncio
import copy
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


MAX_EVENTS_PER_RUN = 200
MAX_QUEUE_SIZE = 100
MQTT_TOPIC_PREFIX = "governance/runs"


def _utc_now() -> str:
    """生成统一 UTC 时间字符串，使消息日志便于排序和跨时区阅读。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def topic_matches(topic_filter: str, topic: str) -> bool:
    """判断简化 MQTT 订阅过滤器能否匹配主题。

    教学 Mock 只实现精确匹配和末尾 ``/#`` 通配，已经足以表现“订阅某运行全部
    通知”的用途；不实现真实 MQTT 的 ``+``、QoS 与保留消息，避免混入无关细节。
    """

    if topic_filter == topic:
        return True
    if topic_filter.endswith("/#"):
        prefix = topic_filter[:-2]
        return topic == prefix or topic.startswith(f"{prefix}/")
    return False


def _put_bounded(queue: asyncio.Queue[dict[str, Any]], value: dict[str, Any]) -> None:
    """向订阅队列写入消息；慢客户端只丢弃最旧实时项，状态可由 REST 补齐。"""

    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            # 队列可能刚被消费；此时继续写入即可，不应让后台发布线程失败。
            pass
    queue.put_nowait(copy.deepcopy(value))


@dataclass(frozen=True)
class QueueSubscription:
    """一个 WebSocket 会话对应的异步队列订阅句柄。"""

    subscription_id: str
    queue: asyncio.Queue[dict[str, Any]]


class MockMqttBroker:
    """线程安全的内存 MQTT 发布/订阅模拟器。

    运行管理器会在 FastAPI 的线程池中发布事件，WebSocket 则运行在 asyncio 主循环。
    因此发布时使用 ``loop.call_soon_threadsafe`` 跨线程写入队列，避免直接操作其他
    线程所属的 asyncio 对象。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[str, tuple[str, asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, topic_filter: str) -> QueueSubscription:
        """在当前事件循环创建订阅队列，并返回用于取消订阅的句柄。"""

        if not topic_filter.strip():
            raise ValueError("订阅主题不能为空")
        loop = asyncio.get_running_loop()
        subscription_id = uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        with self._lock:
            self._subscriptions[subscription_id] = (topic_filter, loop, queue)
        return QueueSubscription(subscription_id=subscription_id, queue=queue)

    def unsubscribe(self, subscription_id: str) -> None:
        """移除断开 WebSocket 的订阅，防止进程内订阅表持续增长。"""

        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """向匹配主题的所有订阅者异步投递轻量 MQTT 通知。"""

        with self._lock:
            targets = [
                (loop, queue)
                for topic_filter, loop, queue in self._subscriptions.values()
                if topic_matches(topic_filter, topic)
            ]
        for loop, queue in targets:
            # 事件循环停止时无需抛出异常影响业务状态；这通常意味着应用正在关闭。
            if not loop.is_closed():
                loop.call_soon_threadsafe(_put_bounded, queue, payload)

    def clear(self) -> None:
        """清空全部订阅；服务停止后不保留任何模拟 Broker 会话。"""

        with self._lock:
            self._subscriptions.clear()


class RunEventStore:
    """按运行隔离、可按序号回放的进程内详细事件日志。"""

    def __init__(self, *, max_events_per_run: int = MAX_EVENTS_PER_RUN) -> None:
        self._lock = threading.RLock()
        self._max_events_per_run = max_events_per_run
        self._next_sequence = 0
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self._max_events_per_run)
        )
        self._subscriptions: dict[str, tuple[str, asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = {}

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        message: str,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条事件并把副本发送给已订阅该运行的 WebSocket。"""

        with self._lock:
            self._next_sequence += 1
            event = {
                "sequence": self._next_sequence,
                "event_type": event_type,
                "run_id": run_id,
                "task_id": task_id,
                "message": message,
                "data": copy.deepcopy(data or {}),
                "occurred_at": _utc_now(),
            }
            self._events[run_id].append(event)
            targets = [
                (loop, queue)
                for subscribed_run_id, loop, queue in self._subscriptions.values()
                if subscribed_run_id == run_id
            ]
        for loop, queue in targets:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_put_bounded, queue, event)
        return copy.deepcopy(event)

    def subscribe(self, run_id: str, *, after: int = 0) -> tuple[QueueSubscription, list[dict[str, Any]]]:
        """先登记实时订阅再读取回放，避免读历史与订阅之间出现消息空窗。"""

        loop = asyncio.get_running_loop()
        subscription_id = uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        with self._lock:
            self._subscriptions[subscription_id] = (run_id, loop, queue)
            replay = [copy.deepcopy(event) for event in self._events.get(run_id, ()) if event["sequence"] > after]
        return QueueSubscription(subscription_id=subscription_id, queue=queue), replay

    def unsubscribe(self, subscription_id: str) -> None:
        """取消一个运行事件订阅。"""

        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    def events_after(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        """读取指定序号之后的事件，供测试和非 WebSocket 调试使用。"""

        with self._lock:
            return [copy.deepcopy(event) for event in self._events.get(run_id, ()) if event["sequence"] > after]

    def clear(self) -> None:
        """清空事件和订阅，明确表达实时记录不跨服务进程持久化。"""

        with self._lock:
            self._events.clear()
            self._subscriptions.clear()
            self._next_sequence = 0


class RealtimeService:
    """协调详细事件流与轻量 Mock MQTT 通知的门面。"""

    def __init__(self) -> None:
        self.broker = MockMqttBroker()
        self.events = RunEventStore()

    @staticmethod
    def notification_topic(run_id: str) -> str:
        """返回每个运行固定的通知主题，前端可用 ``/#`` 订阅其全部事件。"""

        return f"{MQTT_TOPIC_PREFIX}/{run_id}/notifications"

    def publish_run_event(
        self,
        *,
        run_id: str,
        event_type: str,
        message: str,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
        notify_mqtt: bool = True,
    ) -> dict[str, Any]:
        """写入详细事件，并在需要时发出不含业务详情的 MQTT 变化通知。"""

        event = self.events.append(
            run_id=run_id,
            event_type=event_type,
            message=message,
            task_id=task_id,
            data=data,
        )
        if notify_mqtt:
            topic = self.notification_topic(run_id)
            self.broker.publish(
                topic,
                {
                    "type": "mqtt_notification",
                    "topic": topic,
                    "sequence": event["sequence"],
                    "event_type": event_type,
                    "run_id": run_id,
                    "task_id": task_id,
                    "published_at": event["occurred_at"],
                },
            )
        return event

    def clear(self) -> None:
        """应用关闭时释放进程内实时资源，不影响 MongoDB 中的图检查点。"""

        self.broker.clear()
        self.events.clear()


realtime_service = RealtimeService()
