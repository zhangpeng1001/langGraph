"""数据中台实时学习流程测试。

测试使用 MemorySaver 替代 MongoDB，既验证真实 FastAPI WebSocket 契约，也避免把
单元测试绑定到开发者本地的数据库连接串。
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from data_platform import api
from data_platform.graph import build_governance_graph
from data_platform.manager import RunManager, run_manager
from data_platform.mock_platform import mock_platform
from data_platform.realtime import MockMqttBroker, RealtimeService, RunEventStore, topic_matches


class _MemoryRuntime:
    """模拟 API lifespan 所需的检查点运行时，避免测试阶段创建 MongoClient。"""

    def open(self) -> MemorySaver:
        """返回每次测试独立的内存 checkpointer。"""

        return MemorySaver()

    def close(self) -> None:
        """MemorySaver 没有外部连接，关闭方法仅保持与生产运行时接口一致。"""


class RealtimePrimitiveTests(unittest.IsolatedAsyncioTestCase):
    """验证无需 LangGraph 的主题匹配、队列投递和事件回放基础能力。"""

    def test_topic_matches_exact_and_tail_wildcard(self) -> None:
        """Mock 只应支持精确主题和末尾 #，避免伪装成完整 MQTT 协议实现。"""

        self.assertTrue(topic_matches("governance/runs/run_a/#", "governance/runs/run_a/notifications"))
        self.assertTrue(topic_matches("governance/runs/run_a/notifications", "governance/runs/run_a/notifications"))
        self.assertFalse(topic_matches("governance/runs/run_a/#", "governance/runs/run_b/notifications"))
        self.assertFalse(topic_matches("governance/runs/run_a/+", "governance/runs/run_a/notifications"))

    async def test_broker_cross_thread_delivery_and_event_replay(self) -> None:
        """后台线程发布通知时，WebSocket 所在事件循环应能收到副本并按序回放。"""

        broker = MockMqttBroker()
        subscription = broker.subscribe("governance/runs/run_test/#")
        worker = threading.Thread(
            target=broker.publish,
            args=("governance/runs/run_test/notifications", {"type": "mqtt_notification", "sequence": 1}),
        )
        worker.start()
        worker.join()
        self.assertEqual((await asyncio.wait_for(subscription.queue.get(), timeout=1))["sequence"], 1)
        broker.unsubscribe(subscription.subscription_id)

        store = RunEventStore(max_events_per_run=2)
        store.append(run_id="run_test", event_type="run.created", message="已创建")
        live_subscription, replay = store.subscribe("run_test", after=0)
        self.assertEqual([event["sequence"] for event in replay], [1])
        store.append(run_id="run_test", event_type="run.waiting_review", message="等待审核")
        self.assertEqual((await asyncio.wait_for(live_subscription.queue.get(), timeout=1))["event_type"], "run.waiting_review")
        self.assertEqual(len(store.events_after("run_test", after=0)), 2)
        store.unsubscribe(live_subscription.subscription_id)
        store.clear()
        self.assertEqual(store.events_after("run_test"), [])


class RunManagerRealtimeTests(unittest.IsolatedAsyncioTestCase):
    """验证自动模拟、手动回调互斥和事件发布都围绕同一张 LangGraph 图工作。"""

    async def asyncSetUp(self) -> None:
        """每个测试都清空全局 Mock 任务，防止幂等键跨用例复用。"""

        mock_platform.reset()
        self.realtime = RealtimeService()
        self.manager = RunManager(build_governance_graph(MemorySaver()), realtime=self.realtime)
        self.manager.configure(self.manager._graph, event_loop=asyncio.get_running_loop())

    async def asyncTearDown(self) -> None:
        """取消测试创建的后台协程，避免 IsolatedAsyncioTestCase 关闭循环时报警。"""

        self.manager.reset()
        await asyncio.sleep(0)

    async def test_auto_progress_advances_once_and_starts_next_task(self) -> None:
        """短延迟自动模拟应产生分段进度，并且只把首个任务恢复一次。"""

        created = self.manager.create_run("治理 demo.csv，完成全流程")
        waiting = self.manager.resume(created["run_id"], {"decision": "approve", "comment": "测试批准"})
        first_task = waiting["current_task"]
        self.assertIsNotNone(first_task)
        # 默认自动模拟约两秒；测试显式重设为短延迟，仅验证调度和恢复而非等待时间。
        self.manager._schedule_auto_task(created["run_id"], first_task, delay=0.2, replace=True)
        await asyncio.sleep(0.35)

        after = self.manager.snapshot(created["run_id"])
        event_types = [event["event_type"] for event in self.realtime.events.events_after(created["run_id"])]
        self.assertIn("task.progress", event_types)
        self.assertIn("task.succeeded", event_types)
        self.assertEqual(after["current_index"], 1)
        self.assertNotEqual(after["current_task"]["task_id"], first_task["task_id"])

    async def test_manual_failure_wins_over_auto_callback(self) -> None:
        """手动失败在自动协程完成前到达时，运行只能进入一次 FAILED 终态。"""

        created = self.manager.create_run("治理 demo.csv，完成全流程")
        waiting = self.manager.resume(created["run_id"], {"decision": "approve"})
        task_id = waiting["current_task"]["task_id"]
        self.manager._schedule_auto_task(created["run_id"], waiting["current_task"], delay=0.2, replace=True)
        await asyncio.sleep(0.03)
        failed = self.manager.complete_task(created["run_id"], task_id, success=False, message="手动失败")
        await asyncio.sleep(0.3)

        event_types = [event["event_type"] for event in self.realtime.events.events_after(created["run_id"])]
        self.assertEqual(failed["phase"], "FAILED")
        self.assertEqual(event_types.count("task.failed"), 1)
        self.assertEqual(event_types.count("run.failed"), 1)


class ApiRealtimeTests(unittest.TestCase):
    """验证 Mock MQTT 桥、运行 WebSocket 回放和聊天输入的对外契约。"""

    def setUp(self) -> None:
        """进入 TestClient 前清理跨用例的全局运行管理器和 Mock 任务。"""

        run_manager.reset()
        mock_platform.reset()

    def tearDown(self) -> None:
        """TestClient 生命周期结束后再次清理，防止测试顺序影响下一条用例。"""

        run_manager.reset()
        mock_platform.reset()

    def test_mqtt_snapshot_run_stream_and_chat(self) -> None:
        """通知应触发快照可查，运行流应回放事件并返回两条聊天事件。"""

        with patch.object(api, "MongoCheckpointRuntime", _MemoryRuntime):
            with TestClient(api.app) as client:
                with client.websocket_connect("/ws/mock-mqtt") as mqtt_ws:
                    # 先建立一个运行，拿到确定 run_id 后再订阅其约定主题。
                    created = client.post("/api/runs", json={"request": "治理 demo.csv，完成全流程"})
                    self.assertEqual(created.status_code, 200)
                    run_id = created.json()["run_id"]
                    mqtt_ws.send_json({"action": "subscribe", "topic": f"governance/runs/{run_id}/#"})
                    self.assertEqual(mqtt_ws.receive_json()["type"], "mqtt_subscribed")

                    approved = client.post(f"/api/runs/{run_id}/review", json={"decision": "approve"})
                    self.assertEqual(approved.status_code, 200)
                    notification = mqtt_ws.receive_json()
                    self.assertEqual(notification["type"], "mqtt_notification")
                    self.assertEqual(notification["run_id"], run_id)
                    snapshot = client.get(f"/api/runs/{run_id}")
                    self.assertEqual(snapshot.status_code, 200)
                    self.assertIn(snapshot.json()["phase"], {"WAITING_EXTERNAL", "RUNNING"})

                    with client.websocket_connect(f"/ws/runs/{run_id}?after=0") as run_ws:
                        self.assertEqual(run_ws.receive_json()["type"], "snapshot")
                        # 创建和批准事件在 WebSocket 建立前发生，必须由事件日志回放。
                        replayed = run_ws.receive_json()
                        self.assertEqual(replayed["type"], "event")
                        self.assertGreater(replayed["event"]["sequence"], 0)
                        run_ws.send_json({"type": "chat.send", "content": "请说明当前任务状态"})
                        # 聊天消息排在已经回放的运行事件之后，因此持续读取到两条聊天
                        # 事件，而不是错误假设它们一定是队列中的下一条消息。
                        received_types: set[str] = set()
                        for _ in range(12):
                            message = run_ws.receive_json()
                            if message.get("type") == "event" and message["event"]["event_type"].startswith("chat."):
                                received_types.add(message["event"]["event_type"])
                            if received_types == {"chat.user", "chat.assistant"}:
                                break
                        self.assertEqual(received_types, {"chat.user", "chat.assistant"})

    def test_unknown_run_websocket_returns_error(self) -> None:
        """不存在的运行不能建立伪造事件流，服务端应在握手后明确返回错误消息。"""

        with patch.object(api, "MongoCheckpointRuntime", _MemoryRuntime):
            with TestClient(api.app) as client:
                with client.websocket_connect("/ws/runs/run_missing?after=0") as websocket:
                    message = websocket.receive_json()
                    self.assertEqual(message["type"], "error")
                    self.assertIn("不存在的运行", message["message"])


if __name__ == "__main__":
    unittest.main()
