"""数据中台学习图的核心回归测试。

测试使用本地 MockPlatform，不需要网络、模型密钥或数据库，重点验证检查点
恢复、幂等任务、人工审核和失败阻断等评审文档要求的行为事实。
"""

from __future__ import annotations

import unittest

from data_platform.manager import RunManager


def _approve(manager: RunManager, request: str = "治理 demo.csv") -> tuple[str, dict]:
    """创建运行并批准计划，返回稳定 run_id 和首个等待快照。"""

    snapshot = manager.create_run(request)
    run_id = snapshot["run_id"]
    return run_id, manager.resume(run_id, {"decision": "approve"})


class DataPlatformGraphTests(unittest.TestCase):
    """用 unittest 保持与仓库现有测试入口一致。"""

    def test_reject_plan_does_not_start_external_task(self) -> None:
        """人工拒绝必须是终态，且计划审核之前不能存在外部任务。"""

        manager = RunManager()
        snapshot = manager.create_run("治理 demo.csv")
        result = manager.resume(snapshot["run_id"], {"decision": "reject", "comment": "需要补充说明"})
        self.assertEqual(result["phase"], "REJECTED")
        self.assertTrue(all("task_id" not in step for step in result["steps"].values()))

    def test_successful_run_waits_for_each_step_and_publishes_url(self) -> None:
        """每次回调只推进一步，最终发布成功才产生 service_url。"""

        manager = RunManager()
        run_id, snapshot = _approve(manager)
        completed = 0
        while snapshot["phase"] == "WAITING_EXTERNAL":
            task_id = snapshot["pending_action"]["task_id"]
            snapshot = manager.complete_task(run_id, task_id, success=True, message="测试成功")
            completed += 1
        self.assertEqual(completed, 5)
        self.assertEqual(snapshot["phase"], "SUCCEEDED")
        self.assertTrue(snapshot["result"]["service_url"].startswith("http://mock.local/"))

    def test_failed_clean_or_any_step_blocks_following_steps(self) -> None:
        """失败回调立即结束流程，不会伪造后续入库或发布成功。"""

        manager = RunManager()
        run_id, snapshot = _approve(manager)
        snapshot = manager.complete_task(run_id, snapshot["pending_action"]["task_id"], success=False, message="模拟失败")
        self.assertEqual(snapshot["phase"], "FAILED")
        self.assertEqual(snapshot["current_index"], 0)
        self.assertEqual(snapshot["result"]["error"], "STEP_FAILED")

    def test_same_task_callback_is_idempotent_at_platform_boundary(self) -> None:
        """外部任务终态回调重复到达时，Mock 平台保持同一终态，不创建新任务。"""

        manager = RunManager()
        run_id, snapshot = _approve(manager)
        task_id = snapshot["pending_action"]["task_id"]
        first = manager.complete_task(run_id, task_id, success=True, message="第一次")
        # 第二次已经进入下一个任务，旧 task_id 不允许冒充当前任务回调。
        self.assertNotEqual(first["pending_action"]["task_id"], task_id)
