/*
 * 前端只负责展示快照和提交明确动作。
 * 所有状态推进都由后端 LangGraph 检查点完成，浏览器刷新后仍可用 run_id 查询。
 */
const $ = (id) => document.getElementById(id);
let current = null;

const labels = { COLLECT: "采集", QUALITY_CHECK: "质检", CLEAN: "清洗", STORE: "入库", PUBLISH: "发布服务" };
const phases = { WAITING_REVIEW: "等待人工审核", WAITING_EXTERNAL: "等待异步任务", RUNNING: "执行中", SUCCEEDED: "已完成", FAILED: "失败", REJECTED: "已拒绝", CANCELLED: "已取消" };

async function api(url, options = {}) {
  // 统一封装 JSON 请求，错误信息直接回显到控制台消息区域，便于学习 API 契约。
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json()).detail || `请求失败：${response.status}`);
  return response.json();
}

function render(snapshot) {
  // 每次后端恢复检查点后完整刷新视图，不在浏览器侧推断业务状态。
  current = snapshot;
  $("run-panel").classList.remove("hidden");
  $("run-id").textContent = snapshot.run_id;
  $("file-name").textContent = snapshot.file_name || "—";
  $("phase").textContent = phases[snapshot.phase] || snapshot.phase;
  $("message").textContent = snapshot.ui_message || "—";
  const review = snapshot.pending_action?.type === "human_review";
  const task = snapshot.pending_action?.type === "external_task";
  $("review-box").classList.toggle("hidden", !review);
  $("task-box").classList.toggle("hidden", !task);
  if (review) {
    const planned = (snapshot.plan_steps || []).map((s) => `<span class="plan-item">${labels[s] || s}</span>`).join("");
    const skipped = (snapshot.skipped_steps || []).map((s) => `<span class="plan-item skip">跳过 ${labels[s] || s}</span>`).join("");
    $("plan").innerHTML = planned + skipped;
  }
  if (task) {
    $("task-id").textContent = snapshot.pending_action.task_id || "—";
    $("task-message").textContent = `${labels[snapshot.pending_action.step] || snapshot.pending_action.step}：${snapshot.pending_action.message || "等待回调"}`;
  }
  const statuses = snapshot.steps || {};
  $("steps").innerHTML = Object.entries(statuses).map(([key, value]) =>
    `<div class="step"><strong>${labels[key] || key}</strong><span class="status status-${value.status}">${value.status}</span>${value.task_id ? `<small>任务：${value.task_id}</small>` : ""}</div>`
  ).join("") || `<div class="hint">当前计划没有需要执行的步骤。</div>`;
  const terminal = ["SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"].includes(snapshot.phase);
  $("result").classList.toggle("hidden", !terminal);
  if (terminal) $("result").textContent = JSON.stringify(snapshot.result || {}, null, 2);
  $("audit").textContent = JSON.stringify(snapshot.audit || [], null, 2);
}

async function refresh() {
  // 自动回调后重新读取检查点，验证后台协程确实推进了 LangGraph。
  if (!current) return;
  try { render(await api(`/api/runs/${current.run_id}`)); } catch (error) { showError(error); }
}

function showError(error) {
  // 将网络或契约错误集中呈现，避免按钮点击后页面无反馈。
  $("message").textContent = error.message || String(error);
}

$("create").addEventListener("click", async () => {
  const button = $("create"); button.disabled = true;
  try { render(await api("/api/runs", { method: "POST", body: JSON.stringify({ request: $("request").value }) })); }
  catch (error) { showError(error); } finally { button.disabled = false; }
});
document.querySelectorAll(".example").forEach((button) => button.addEventListener("click", () => { $("request").value = button.dataset.value; }));
$("approve").addEventListener("click", async () => { try { render(await api(`/api/runs/${current.run_id}/review`, { method: "POST", body: JSON.stringify({ decision: "approve", comment: $("review-comment").value }) })); } catch (error) { showError(error); } });
$("reject").addEventListener("click", async () => { try { render(await api(`/api/runs/${current.run_id}/review`, { method: "POST", body: JSON.stringify({ decision: "reject", comment: $("review-comment").value }) })); } catch (error) { showError(error); } });
async function complete(success) {
  // 成功和失败都走同一个回调接口，方便观察 resolve_external 的两条分支。
  try {
    const id = current.pending_action.task_id;
    render(await api(`/api/runs/${current.run_id}/tasks/${id}/complete`, { method: "POST", body: JSON.stringify({ success, message: success ? "前端按钮模拟成功回调" : "前端按钮模拟失败回调" }) }));
  } catch (error) { showError(error); }
}
$("complete-success").addEventListener("click", () => complete(true));
$("complete-failure").addEventListener("click", () => complete(false));
$("auto-complete").addEventListener("click", async () => { try { const id = current.pending_action.task_id; render(await api(`/api/runs/${current.run_id}/tasks/${id}/auto-complete`, { method: "POST", body: JSON.stringify({ success: true, message: "后台延时回调已完成" }) })); setTimeout(refresh, 2400); } catch (error) { showError(error); } });
api("/api/health").then(() => { $("health").textContent = "本地服务已连接"; }).catch(() => { $("health").textContent = "服务未连接"; });
