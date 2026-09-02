/*
 * 数据中台实时学习前端。
 *
 * Mock MQTT 只通知“某运行有变化”，收到它以后必须先查询 REST 快照；运行级 WebSocket
 * 才负责传输详细、可回放的事件。把两条连接分开可以直观看到消息通知与状态事实的区别。
 */
const $ = (id) => document.getElementById(id);

let current = null;
let activeRunId = null;
let mqttSocket = null;
let runSocket = null;
let lastSequence = 0;
let reconnectTimer = null;
const eventLog = [];

const labels = { COLLECT: "采集", QUALITY_CHECK: "质检", CLEAN: "清洗", STORE: "入库", PUBLISH: "发布服务" };
const phases = { WAITING_REVIEW: "等待人工审核", WAITING_EXTERNAL: "等待异步任务", RUNNING: "执行中", SUCCEEDED: "已完成", FAILED: "失败", REJECTED: "已拒绝", CANCELLED: "已取消" };
const terminalPhases = new Set(["SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"]);

async function api(url, options = {}) {
  // 所有业务动作仍由 REST 提交到后端，浏览器从快照读取事实，绝不自行推断图状态。
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败：${response.status}`);
  }
  return response.json();
}

function websocketUrl(path) {
  // 页面可能经 HTTP 或 HTTPS 提供，WebSocket 协议必须随之切换，否则浏览器会拦截混合内容。
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}${path}`;
}

// 统一转为安全文本，避免状态字段为 null 或数字时干扰 DOM 渲染。
function safeText(value) {
  return String(value ?? "");
}

// 按后端 steps 快照创建步骤卡片；使用 textContent 防止任务消息被当作 HTML 执行。
function renderSteps(snapshot) {
  const container = $("steps");
  container.replaceChildren();
  const statuses = snapshot.steps || {};
  const entries = Object.entries(statuses);
  if (!entries.length) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "当前计划没有需要执行的步骤。";
    container.append(hint);
    return;
  }
  entries.forEach(([key, value]) => {
    const item = document.createElement("div");
    item.className = "step";
    const title = document.createElement("strong");
    title.textContent = labels[key] || key;
    const status = document.createElement("span");
    status.className = `status status-${safeText(value.status)}`;
    status.textContent = safeText(value.status);
    item.append(title, status);
    if (value.task_id) {
      const task = document.createElement("small");
      task.textContent = `任务：${value.task_id}`;
      item.append(task);
    }
    container.append(item);
  });
}

// 根据唯一可信的运行快照切换审核、外部任务、终态任务等不同业务视图。
function render(snapshot) {
  // 只接受当前激活运行的快照，避免旧连接的延迟响应覆盖新任务的页面状态。
  if (!snapshot || (activeRunId && snapshot.run_id !== activeRunId)) return;
  current = snapshot;
  activeRunId = snapshot.run_id;
  localStorage.setItem("data-platform-last-run", activeRunId);
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
    const plan = $("plan");
    plan.replaceChildren();
    (snapshot.plan_steps || []).forEach((step) => {
      const item = document.createElement("span");
      item.className = "plan-item";
      item.textContent = labels[step] || step;
      plan.append(item);
    });
    (snapshot.skipped_steps || []).forEach((step) => {
      const item = document.createElement("span");
      item.className = "plan-item skip";
      item.textContent = `跳过 ${labels[step] || step}`;
      plan.append(item);
    });
  }
  if (task) {
    const currentTask = snapshot.current_task || snapshot.pending_action;
    const progress = Math.max(0, Math.min(Number(currentTask.progress || 0), 100));
    $("task-id").textContent = currentTask.task_id || "—";
    $("task-message").textContent = `${labels[currentTask.step] || currentTask.step || "外部任务"}：${currentTask.message || snapshot.pending_action.message || "等待回调"}`;
    $("task-progress-bar").style.width = `${progress}%`;
    $("task-progress").textContent = `当前进度：${progress}%（${currentTask.status || "PENDING"}）`;
  }
  renderSteps(snapshot);
  const terminal = terminalPhases.has(snapshot.phase);
  $("result").classList.toggle("hidden", !terminal);
  $("terminal-task").classList.toggle("hidden", !terminal);
  if (terminal) $("result").textContent = JSON.stringify(snapshot.result || {}, null, 2);
  $("audit").textContent = JSON.stringify(snapshot.audit || [], null, 2);
  renderRoute();
}

function addLogLine(text, kind = "event", sequence = null) {
  // 事件序号是重连去重依据；Mock MQTT 通知没有详细内容，因此允许其使用空序号单独展示。
  if (sequence !== null && eventLog.some((item) => item.sequence === sequence)) return;
  eventLog.push({ text, kind, sequence, at: new Date().toLocaleTimeString() });
  if (eventLog.length > 120) eventLog.shift();
  const container = $("realtime-log");
  container.replaceChildren();
  eventLog.forEach((item) => {
    const row = document.createElement("li");
    row.className = `realtime-${item.kind}`;
    row.textContent = `${item.at} ${item.sequence !== null ? `#${item.sequence} ` : ""}${item.text}`;
    container.append(row);
  });
  container.scrollTop = container.scrollHeight;
}

// 将内存事件历史筛选为聊天内容，供 hash 交流页复用而不是维护第二套消息状态。
function renderChatMessages() {
  const container = $("chat-messages");
  container.replaceChildren();
  eventLog.filter((item) => item.kind === "chat").forEach((item) => {
    const bubble = document.createElement("div");
    bubble.className = "chat-message";
    bubble.textContent = item.text;
    container.append(bubble);
  });
  container.scrollTop = container.scrollHeight;
}

// 处理详细运行事件：更新回放序号、打印日志，并即时显示进度事件携带的任务数据。
function handleRunEvent(event) {
  const sequence = Number(event.sequence || 0);
  // 历史回放与重连队列可能交叠；只处理严格递增的新序号以保证打印不会重复。
  if (!sequence || sequence <= lastSequence) return;
  lastSequence = sequence;
  const isChat = event.event_type === "chat.user" || event.event_type === "chat.assistant";
  addLogLine(`[${event.event_type}] ${event.message}`, isChat ? "chat" : "event", sequence);
  console.log("运行 WebSocket 事件", event);

  // 进度事件携带最新脱敏任务数据，即使 MQTT 后的 REST 请求还在网络中，也能立即更新 UI。
  const eventTask = event.data?.task;
  if (current && eventTask && current.current_task?.task_id === eventTask.task_id) {
    current = { ...current, current_task: eventTask };
    render(current);
  }
  if (isConversationRoute()) renderChatMessages();
}

// MQTT 通知抵达后重新读取后端快照，保证 UI 不依赖可能重复的通知内容。
async function refreshSnapshot() {
  if (!activeRunId) return;
  try {
    render(await api(`/api/runs/${activeRunId}`));
  } catch (error) {
    showError(error);
  }
}

// 关闭旧任务关联的连接，确保切换运行时不会继续收到历史任务的推送。
function closeSocket(socket) {
  // CONNECTING 状态的旧连接同样必须关闭；否则它稍后成功握手会向新运行页面写入旧消息。
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) socket.close(1000, "切换运行");
}

// 建立浏览器到 Mock MQTT WebSocket 桥的订阅，并把每条通知转换为一次快照刷新。
function connectMockMqtt(runId) {
  if (mqttSocket && mqttSocket.datasetRunId === runId && [WebSocket.CONNECTING, WebSocket.OPEN].includes(mqttSocket.readyState)) return;
  closeSocket(mqttSocket);
  const socket = new WebSocket(websocketUrl("/ws/mock-mqtt"));
  socket.datasetRunId = runId;
  mqttSocket = socket;
  socket.onopen = () => {
    socket.send(JSON.stringify({ action: "subscribe", topic: `governance/runs/${runId}/#` }));
  };
  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "mqtt_subscribed") {
      addLogLine(`Mock MQTT 已订阅：${payload.topic}`, "mqtt");
      return;
    }
    if (payload.type === "mqtt_notification") {
      addLogLine(`Mock MQTT 通知：${payload.event_type}，正在查询最新快照。`, "mqtt", null);
      console.log("Mock MQTT 通知", payload);
      refreshSnapshot();
      connectRunStream(runId);
      return;
    }
    if (payload.type === "error") showError(new Error(payload.message));
  };
  socket.onclose = () => {
    if (activeRunId === runId) window.setTimeout(() => connectMockMqtt(runId), 1200);
  };
}

// 建立运行级详细事件流；断线后携带最后序号重连，以获得服务器保存的缺失事件。
function connectRunStream(runId) {
  if (runSocket && runSocket.datasetRunId === runId && [WebSocket.CONNECTING, WebSocket.OPEN].includes(runSocket.readyState)) return;
  closeSocket(runSocket);
  $("realtime-state").textContent = "正在连接运行 WebSocket…";
  const socket = new WebSocket(websocketUrl(`/ws/runs/${encodeURIComponent(runId)}?after=${lastSequence}`));
  socket.datasetRunId = runId;
  runSocket = socket;
  socket.onopen = () => {
    $("realtime-state").textContent = "运行 WebSocket 已连接";
    $("chat-state").textContent = "已连接，可发送本地演示消息。";
  };
  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "snapshot") {
      render(payload.snapshot);
    } else if (payload.type === "event") {
      handleRunEvent(payload.event);
    } else if (payload.type === "error") {
      showError(new Error(payload.message));
    }
  };
  socket.onclose = () => {
    $("realtime-state").textContent = "运行 WebSocket 已断开，准备重连…";
    $("chat-state").textContent = "连接暂时断开，正在自动重连。";
    if (activeRunId === runId) {
      clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(() => connectRunStream(runId), 1000);
    }
  };
  socket.onerror = () => {
    // 浏览器不会给出可安全展示的错误详情，统一等待 onclose 的重连分支即可。
    $("realtime-state").textContent = "运行 WebSocket 连接异常";
  };
}

// 激活一个运行并完成首次事件回放、Mock MQTT 订阅与运行 WebSocket 连接。
function activateRun(snapshot) {
  const changed = activeRunId && activeRunId !== snapshot.run_id;
  activeRunId = snapshot.run_id;
  if (changed) {
    eventLog.length = 0;
    lastSequence = 0;
  }
  render(snapshot);
  // 创建接口返回 run_id 时首批事件已经产生，因此立即连接运行流并以 after=0 回放；
  // 后续则严格遵循 MQTT 通知 -> REST 快照 -> 维持 WebSocket 流的教学链路。
  connectMockMqtt(activeRunId);
  connectRunStream(activeRunId);
}

// 判断当前 hash 是否指向已经激活的运行交流页，防止无效 hash 错误隐藏控制台。
function isConversationRoute() {
  return location.hash === `#conversation/${activeRunId}`;
}

// 依据 hash 在运行控制台与交流视图之间切换，两个视图共享同一个 WebSocket 连接。
function renderRoute() {
  const conversation = isConversationRoute();
  $("run-panel").classList.toggle("hidden", conversation);
  $("conversation-panel").classList.toggle("hidden", !conversation);
  if (conversation) renderChatMessages();
}

async function restoreRoute() {
  const match = location.hash.match(/^#conversation\/([^/]+)$/);
  const runId = match?.[1] || localStorage.getItem("data-platform-last-run");
  if (!runId) return;
  try {
    const snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`);
    // 不要在查询前覆盖 activeRunId；activateRun 需要比较旧 ID，才能清空旧任务的
    // 序号、日志和 WebSocket，防止直接修改 hash 时两个运行的记录混在一起。
    activateRun(snapshot);
  } catch (error) {
    // MongoDB 中可能没有这条很早的运行；清理失效的浏览器记录，以免每次刷新都报错。
    localStorage.removeItem("data-platform-last-run");
    if (match) showError(error);
  }
}

function showError(error) {
  const message = error.message || String(error);
  $("message").textContent = message;
  $("chat-state").textContent = message;
  console.error(error);
}

$("create").addEventListener("click", async () => {
  const button = $("create");
  button.disabled = true;
  try {
    const snapshot = await api("/api/runs", { method: "POST", body: JSON.stringify({ request: $("request").value }) });
    activateRun(snapshot);
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll(".example").forEach((button) => button.addEventListener("click", () => {
  $("request").value = button.dataset.value;
}));

$("approve").addEventListener("click", async () => {
  try {
    render(await api(`/api/runs/${activeRunId}/review`, { method: "POST", body: JSON.stringify({ decision: "approve", comment: $("review-comment").value }) }));
  } catch (error) { showError(error); }
});

$("reject").addEventListener("click", async () => {
  try {
    render(await api(`/api/runs/${activeRunId}/review`, { method: "POST", body: JSON.stringify({ decision: "reject", comment: $("review-comment").value }) }));
  } catch (error) { showError(error); }
});

async function complete(success) {
  // 自动模拟与人工回调会争夺同一个运行锁；后端保证后到的一方不会重复恢复图。
  try {
    const taskId = current?.current_task?.task_id || current?.pending_action?.task_id;
    render(await api(`/api/runs/${activeRunId}/tasks/${taskId}/complete`, {
      method: "POST",
      body: JSON.stringify({ success, message: success ? "前端按钮模拟成功回调" : "前端按钮模拟失败回调" }),
    }));
  } catch (error) { showError(error); }
}

$("complete-success").addEventListener("click", () => complete(true));
$("complete-failure").addEventListener("click", () => complete(false));
$("open-conversation").addEventListener("click", () => {
  if (activeRunId) location.hash = `conversation/${activeRunId}`;
});
$("back-to-run").addEventListener("click", () => { location.hash = ""; });

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const content = $("chat-input").value.trim();
  if (!content) return;
  if (!runSocket || runSocket.readyState !== WebSocket.OPEN) {
    showError(new Error("运行 WebSocket 尚未连接，消息未发送。"));
    return;
  }
  runSocket.send(JSON.stringify({ type: "chat.send", content }));
  $("chat-input").value = "";
});

window.addEventListener("hashchange", () => {
  if (location.hash.startsWith("#conversation/")) restoreRoute();
  else renderRoute();
});

api("/api/health").then(() => {
  $("health").textContent = "本地服务已连接";
  restoreRoute();
}).catch(() => { $("health").textContent = "服务未连接"; });
