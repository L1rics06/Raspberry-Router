/* PiAgent 前端逻辑 —— 无构建纯静态 SPA（分工 A）
 * 接口契约：docs/接口文档/api_frontend.md
 * 静态资源以相对路径引用，网关根路径与 nginx /piagent/ 前缀下均可运行。
 */
"use strict";

/* ================= 基础工具 ================= */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtTime = (ts) => ts
  ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
  : "";

const fmtAgo = (ts) => {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
};

/* REST 基址：页面在 / 或 /piagent/ 下都能正确解析为相对路径 */
const pathBase = location.pathname.replace(/[^/]*$/, "");
const wsUrl = () =>
  (location.protocol === "https:" ? "wss://" : "ws://") +
  location.host + pathBase + "ws/user";

/* ================= 全局状态 ================= */

const store = {
  token: localStorage.getItem("pia_token") || "",
  username: localStorage.getItem("pia_user") || "",
  devices: [],
  current: null,            // 当前选中 device_id
  chatLog: [],              // {role:'user'|'ai', text, actions, status, id}
  busy: false,              // 同一设备指令串行：前端发送锁
  ws: null, wsOk: false, backoff: 1, wsTimer: null, pingTimer: null,
  authMode: "login",        // login | register
  lastManual: {},           // device_id → {tool: 上次成功结果状态}（开关类记忆用）
};

const currentDevice = () => store.devices.find((d) => d.device_id === store.current) || null;

/* ================= REST ================= */

async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(pathBase + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(store.token ? { Authorization: `Bearer ${store.token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new Error("网络请求失败，请检查连接");
  }
  if (res.status === 401) {
    logout("登录已过期，请重新登录");
    throw new Error("未登录");
  }
  let data = {};
  try { data = await res.json(); } catch { /* 空响应 */ }
  if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
  return data;
}

/* ================= Toast / 弹窗 ================= */

function toast(text, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

let _modalResolve = null;
function confirmModal(text) {
  return new Promise((resolve) => {
    _modalResolve = resolve;
    $("#modal-text").textContent = text;
    $("#modal-mask").classList.remove("hidden");
  });
}
function _closeModal(v) {
  $("#modal-mask").classList.add("hidden");
  if (_modalResolve) { _modalResolve(v); _modalResolve = null; }
}

/* ================= 登录 / 注册 ================= */

function showAuth() {
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  $("#auth-error").textContent = "";
}

function setAuthMode(mode) {
  store.authMode = mode;
  $("#auth-submit").textContent = mode === "login" ? "登 录" : "注 册";
  $("#auth-mode-switch").textContent = mode === "login" ? "注册新账号" : "返回登录";
  $("#auth-error").textContent = "";
}

async function submitAuth(e) {
  e.preventDefault();
  const username = $("#auth-username").value.trim();
  const password = $("#auth-password").value;
  const err = $("#auth-error");
  err.textContent = "";
  $("#auth-submit").disabled = true;
  try {
    const path = store.authMode === "login" ? "api/auth/login" : "api/auth/register";
    const data = await api(path, { method: "POST", body: { username, password } });
    store.token = data.token;
    store.username = data.username;
    localStorage.setItem("pia_token", data.token);
    localStorage.setItem("pia_user", data.username);
    enterApp();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    $("#auth-submit").disabled = false;
  }
}

function logout(reason = "") {
  localStorage.removeItem("pia_token");
  localStorage.removeItem("pia_user");
  store.token = "";
  store.username = "";
  store.devices = [];
  store.current = null;
  store.chatLog = [];
  clearTimeout(store.wsTimer);
  try { store.ws && store.ws.close(1000); } catch { /* 忽略 */ }
  showAuth();
  if (reason) toast(reason, "err");
}

/* ================= 进入主界面 ================= */

function enterApp() {
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#user-name").textContent = store.username;
  $("#user-avatar").textContent = store.username.slice(0, 1).toUpperCase();
  wsConnect();
  refreshDevices();
}

async function refreshDevices() {
  try {
    store.devices = await api("api/devices");
    renderDevices();
  } catch (e) {
    if (e.message !== "未登录") toast(e.message, "err");
  }
}

/* ================= WebSocket ================= */

function setWsState(ok) {
  store.wsOk = ok;
  $("#ws-dot").className = `dot ${ok ? "on" : "off"}`;
  $("#ws-state").textContent = ok ? "已连接" : "连接中…";
}

function wsConnect() {
  clearTimeout(store.wsTimer);
  if (!store.token) return;
  let ws;
  try {
    ws = new WebSocket(`${wsUrl()}?token=${encodeURIComponent(store.token)}`);
  } catch {
    scheduleReconnect();
    return;
  }
  store.ws = ws;

  ws.onopen = () => {
    store.backoff = 1;
    setWsState(true);
    clearInterval(store.pingTimer);
    store.pingTimer = setInterval(() => {
      try { ws.send(JSON.stringify({ type: "ping" })); } catch { /* 忽略 */ }
    }, 30000);
  };
  ws.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    handleEvent(m);
  };
  ws.onclose = (e) => {
    clearInterval(store.pingTimer);
    setWsState(false);
    if (e.code === 4401) { logout("登录已过期，请重新登录"); return; }
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch { /* 忽略 */ } };
}

function scheduleReconnect() {
  clearTimeout(store.wsTimer);
  const delay = Math.min(store.backoff, 30);
  store.backoff = Math.min(store.backoff * 2 || 1, 30);
  store.wsTimer = setTimeout(wsConnect, delay * 1000);
}

function handleEvent(m) {
  switch (m.type) {
    case "hello":
      store.devices = m.devices || [];
      renderDevices();
      break;

    case "telemetry": {
      const d = store.devices.find((x) => x.device_id === m.device_id);
      if (d) { d.telemetry = m.data; }
      renderDevices();
      break;
    }

    case "device_status": {
      const d = store.devices.find((x) => x.device_id === m.device_id);
      if (d) d.online = m.online;
      toast(`设备 ${m.device_id} ${m.online ? "已上线" : "已离线"}`, m.online ? "ok" : "err");
      renderDevices();
      break;
    }

    case "cmd_ack": {
      // 聊天占位条目（发送时 id 为 null）绑定真实 cmd_id，进入"思考中"
      const ph = store.chatLog.find((x) => x.role === "ai" && x.id === null);
      if (ph) { ph.id = m.id; ph.status = "thinking"; }
      renderChat();
      break;
    }

    case "result": {
      const item = store.chatLog.find((x) => x.id === m.id);
      if (item) {
        item.status = "done";
        item.text = m.text;
        item.actions = m.actions || [];
        item.ok = m.ok;
      }
      store.busy = false;
      $("#btn-send").disabled = false;
      renderChat();
      break;
    }

    case "cmd_error": {
      const item = store.chatLog.find((x) => x.id === m.id);
      if (item) { item.status = "error"; item.text = m.message || "指令失败"; }
      store.busy = false;
      $("#btn-send").disabled = false;
      renderChat();
      break;
    }

    case "error":
      // chat 发送即失败（如设备离线）：清理占位并提示
      {
        const ph = store.chatLog.find((x) => x.role === "ai" && x.status === "pending");
        if (ph) ph.status = "removed";
        store.busy = false;
        $("#btn-send").disabled = false;
        renderChat();
        toast(`${m.code || "错误"}：${m.message || ""}`, "err");
      }
      break;

    case "pong":
      break;

    default:
      break;
  }
}

/* ================= 渲染：设备侧栏 / 顶栏 ================= */

function renderDevices() {
  const list = $("#device-list");
  if (!store.devices.length) {
    list.innerHTML = `<div class="empty-note" style="padding:16px">还没有绑定设备<br><span style="font-size:12px">点击下方按钮添加</span></div>`;
  } else {
    list.innerHTML = store.devices.map((d) => `
      <div class="device-card ${d.device_id === store.current ? "active" : ""}" data-id="${esc(d.device_id)}">
        <div class="row1"><span class="dot ${d.online ? "on" : "off"}"></span>${esc(d.device_id)}</div>
        <div class="row2">
          <span>${d.telemetry && d.telemetry.temp != null ? `🌡 ${d.telemetry.temp}°C` : "无遥测"}</span>
          <span>${fmtAgo(d.last_seen)}</span>
        </div>
      </div>`).join("");
    $$(".device-card", list).forEach((el) =>
      el.addEventListener("click", () => selectDevice(el.dataset.id)));
  }
  $("#device-count").textContent = store.devices.length || "";

  if (!store.devices.find((d) => d.device_id === store.current)) {
    const online = store.devices.find((d) => d.online);
    store.current = (online || store.devices[0] || {}).device_id || null;
  }
  renderTopbar();
  renderManual();
}

function renderTopbar() {
  const d = currentDevice();
  $("#dev-name").textContent = d ? d.device_id : "未选择设备";
  $("#dev-online").className = `pill ${d && d.online ? "on" : "off"}`;
  $("#dev-online").textContent = d && d.online ? "在线" : "离线";
  const t = d && d.telemetry;
  $("#dev-telemetry").textContent =
    t && t.temp != null ? `🌡 ${t.temp}°C  💧 ${t.hum}%` : "";
}

function selectDevice(id) {
  if (store.current === id) return;
  store.current = id;
  store.chatLog = [];
  renderDevices();
  renderChat();
  if (store.tab === "history") loadHistory();
}

/* ================= 渲染：对话 ================= */

function renderChat() {
  const box = $("#chat-box");
  const logs = store.chatLog.filter((x) => x.status !== "removed");
  if (!logs.length) {
    box.innerHTML = $("#chat-empty")
      ? `<div class="chat-empty" id="chat-empty">
           <div class="chat-empty-logo">🤖</div>
           <p>我是你的设备管家，试试对我说：</p>
           <div class="hint-row">
             <span class="chip clickable" data-fill="把风扇打开">把风扇打开</span>
             <span class="chip clickable" data-fill="舵机转到 90 度">舵机转到 90 度</span>
             <span class="chip clickable" data-fill="寝室现在多少度？">寝室现在多少度？</span>
           </div>
         </div>` : "";
  } else {
    box.innerHTML = logs.map((m) => {
      if (m.role === "user") {
        return `<div class="msg user"><div class="avatar">👤</div><div class="bubble">${esc(m.text)}</div></div>`;
      }
      let inner;
      if (m.status === "pending" || m.status === "thinking") {
        inner = `<span class="thinking"><i></i><i></i><i></i></span> Agent 正在思考…`;
      } else {
        inner = esc(m.text || (m.ok ? "完成" : "执行失败"));
      }
      const actions = (m.actions || []).map((a) =>
        `<span class="chip action" title="${esc(a)}">⚙ ${esc(shortAction(a))}</span>`).join("");
      return `<div class="msg ai">
        <div class="avatar ai">🤖</div>
        <div class="bubble ${m.status === "error" || m.ok === false ? "error" : ""}">
          ${inner}${actions ? `<div class="msg-actions">${actions}</div>` : ""}
        </div>
      </div>`;
    }).join("");
  }
  box.scrollTop = box.scrollHeight;
}

function shortAction(a) {
  // "fan.set({\"state\": true})" → "fan.set"
  const m = String(a).match(/^([\w.]+)\(/);
  return m ? m[1] : String(a).slice(0, 30);
}

function sendChat() {
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text || store.busy) return;
  if (!store.wsOk) { toast("消息通道未连接，稍候再试", "err"); return; }
  const online = store.devices.filter((d) => d.online);
  if (!online.length) { toast("设备不在线", "err"); return; }

  input.value = "";
  store.busy = true;
  $("#btn-send").disabled = true;
  store.chatLog.push({ role: "user", text });
  store.chatLog.push({ role: "ai", id: null, status: "pending", text: "" });
  renderChat();

  const msg = { type: "chat", text };
  if (store.current) msg.device_id = store.current;
  store.ws.send(JSON.stringify(msg));

  // 兜底：90s 服务端超时之外仍无回音，解除发送锁
  setTimeout(() => {
    const item = store.chatLog.find((x) => x.role === "ai" && (x.id === null || x.status === "pending" || x.status === "thinking"));
    if (item && (item.status === "pending" || item.status === "thinking")) {
      item.status = "error";
      item.text = "响应超时，请重试";
      store.busy = false;
      $("#btn-send").disabled = false;
      renderChat();
    }
  }, 95000);
}

/* ================= 渲染：手动控制 ================= */

const CONFIRM_HINT = /确认|高危|白名单/;

function renderManual() {
  const box = $("#manual-controls");
  const empty = $("#manual-empty");
  const d = currentDevice();
  if (!d || !d.online || !(d.caps || []).length) {
    box.innerHTML = "";
    empty.textContent = !d ? "未选择设备" : (!d.online ? "设备不在线，无法手动控制" : "设备未上报任何工具");
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  box.innerHTML = (d.caps || []).map(renderToolCard).join("");
  bindToolEvents();
}

function renderToolCard(cap) {
  const mem = (store.lastManual[store.current] || {})[cap];
  switch (cap) {
    case "led.set": {
      const on = !!mem;
      return toolCard("💡", cap, "LED 灯", `
        <div class="switch-row">
          <button class="btn ${on ? "ok" : "ghost"}" data-tool="led.set" data-args='{"on":true}'>点亮</button>
          <button class="btn ghost" data-tool="led.set" data-args='{"on":false}'>熄灭</button>
        </div>`);
    }
    case "fan.set": {
      const on = !!mem;
      return toolCard("🌀", cap, "风扇", `
        <div class="switch-row">
          <button class="btn ${on ? "ok" : "ghost"}" data-tool="fan.set" data-args='{"state":true}'>打开</button>
          <button class="btn ghost" data-tool="fan.set" data-args='{"state":false}'>关闭</button>
        </div>`);
    }
    case "relay.set":
      return toolCard("🔌", cap, "继电器（高危）", [0, 1, 2, 3].map((ch) => `
        <div class="switch-row" style="margin-bottom:8px">
          <span class="muted" style="width:44px">通道 ${ch}</span>
          <button class="btn small ghost" data-tool="relay.set" data-args='{"ch":${ch},"state":true}'>闭合</button>
          <button class="btn small ghost" data-tool="relay.set" data-args='{"ch":${ch},"state":false}'>断开</button>
        </div>`).join(""), true);
    case "servo.set":
      return toolCard("🦾", cap, "舵机（0-180°）", `
        <div class="slider-row">
          <input type="range" min="0" max="180" step="1" value="${mem ?? 0}" id="servo-angle">
          <span class="angle-val" id="servo-angle-val">${mem ?? 0}°</span>
        </div>
        <div class="preset-row">
          <button class="btn small ghost" data-tool="servo.set" data-args='{"angle":0}'>复位 0°</button>
          <button class="btn small primary" data-tool="servo.set" data-args='{"angle":90}'>打开 90°</button>
          <button class="btn small ghost" data-tool="servo.set" data-args='{"angle":180}'>180°</button>
          <button class="btn small ghost" data-tool="servo.set" data-dynamic="servo">转到滑条角度</button>
        </div>`);
    case "dht.read":
      return toolCard("🌡", cap, "温湿度传感器", `
        <button class="btn primary" data-tool="dht.read" data-args='{}'>读取温湿度</button>`);
    case "devices.status":
      return toolCard("📋", cap, "设备状态总览", `
        <button class="btn ghost" data-tool="devices.status" data-args='{}'>刷新状态</button>`);
    default:
      return toolCard("🔧", cap, cap, `
        <input class="generic-args" placeholder='参数 JSON，如 {"key": "value"}' data-tool="${esc(cap)}">
        <div style="margin-top:10px"><button class="btn primary small" data-generic="${esc(cap)}">执行</button></div>`);
  }
}

function toolCard(icon, cap, title, bodyHtml, danger = false) {
  return `<div class="tool-card">
    <h4>${icon} ${esc(title)} ${danger ? '<span class="danger-tag">需二次确认</span>' : ""}</h4>
    <div class="tool-id">${esc(cap)}</div>
    ${bodyHtml}
  </div>`;
}

function bindToolEvents() {
  const box = $("#manual-controls");
  const slider = $("#servo-angle", box);
  if (slider) {
    slider.addEventListener("input", () => {
      $("#servo-angle-val").textContent = `${slider.value}°`;
    });
  }
  $$("button[data-tool]", box).forEach((btn) =>
    btn.addEventListener("click", () => {
      const args = btn.dataset.dynamic === "servo"
        ? { angle: parseInt($("#servo-angle", box).value, 10) }
        : JSON.parse(btn.dataset.args);
      runTool(btn.dataset.tool, args, btn.textContent.includes("闭合"));  // 继电器闭合默认弹确认
    }));
  $$("button[data-generic]", box).forEach((btn) =>
    btn.addEventListener("click", async () => {
      const tool = btn.dataset.generic;
      const input = $(`.generic-args[data-tool="${CSS.escape(tool)}"]`, box);
      let args = {};
      if (input && input.value.trim()) {
        try { args = JSON.parse(input.value); } catch { toast("参数不是合法 JSON", "err"); return; }
      }
      await runTool(tool, args, false);
    }));
}

async function runTool(tool, args, askConfirm) {
  const d = currentDevice();
  if (!d || !d.online) { toast("设备不在线", "err"); return; }

  let confirmed = false;
  if (askConfirm) {
    confirmed = await confirmModal(
      `即将执行高危工具 ${tool}(${JSON.stringify(args)})，确认继续？`);
    if (!confirmed) return;
    confirmed = true;
  }

  const resultBox = $("#manual-result");
  resultBox.classList.remove("hidden", "ok", "bad");
  resultBox.textContent = `${tool} 执行中…`;

  try {
    const res = await api(`api/devices/${encodeURIComponent(d.device_id)}/command`, {
      method: "POST",
      body: { tool, args, confirmed, wait: true },
    });

    // 高危未确认（未走弹窗的路径，如聊天侧惯例）：补一次确认再执行
    if (res.status === "done" && res.ok === false && CONFIRM_HINT.test(res.text || "")) {
      const go = await confirmModal(`${tool}(${JSON.stringify(args)}) 属高危操作，二次确认后执行？`);
      if (!go) { resultBox.classList.add("bad"); resultBox.textContent = "已取消"; return; }
      return void await runToolConfirmed(tool, args);
    }

    if (res.status === "timeout") {
      resultBox.classList.add("bad");
      resultBox.textContent = `${tool} 设备响应超时`;
      return;
    }

    const ok = res.ok !== false;
    resultBox.classList.add(ok ? "ok" : "bad");
    resultBox.textContent = `${tool}(${JSON.stringify(args)}) → ${res.text || ""}`;

    // 记忆开关类状态，用于按钮高亮
    if (ok) {
      const mem = (store.lastManual[d.device_id] = store.lastManual[d.device_id] || {});
      if (tool === "led.set") mem[tool] = args.on;
      if (tool === "fan.set") mem[tool] = args.state;
      if (tool === "servo.set") mem[tool] = args.angle;
      renderManual();
    }
  } catch (e) {
    resultBox.classList.add("bad");
    resultBox.textContent = `${tool} 执行失败：${e.message}`;
  }
}

async function runToolConfirmed(tool, args) {
  const d = currentDevice();
  const resultBox = $("#manual-result");
  try {
    const res = await api(`api/devices/${encodeURIComponent(d.device_id)}/command`, {
      method: "POST",
      body: { tool, args, confirmed: true, wait: true },
    });
    const ok = res.status === "done" && res.ok !== false;
    resultBox.classList.remove("hidden");
    resultBox.classList.add(ok ? "ok" : "bad");
    resultBox.textContent = `${tool}(${JSON.stringify(args)}) → ${res.text || res.status}`;
  } catch (e) {
    resultBox.classList.add("bad");
    resultBox.textContent = `${tool} 执行失败：${e.message}`;
  }
}

/* ================= 历史 ================= */

async function loadHistory() {
  const list = $("#history-list");
  list.innerHTML = `<p class="muted">加载中…</p>`;
  try {
    const q = store.current ? `?device_id=${encodeURIComponent(store.current)}&limit=100`
                            : "?limit=100";
    const rows = await api(`api/history${q}`);
    $("#history-count").textContent = `共 ${rows.length} 条`;
    if (!rows.length) {
      list.innerHTML = `<div class="empty-note">暂无历史记录</div>`;
      return;
    }
    list.innerHTML = rows.map((r) => {
      const isChat = r.kind === "chat";
      const ask = isChat ? (r.payload && r.payload.text) || "" : JSON.stringify(r.payload || {});
      const dur = r.finished_at && r.created_at ? `耗时 ${(r.finished_at - r.created_at).toFixed(1)}s` : "";
      const acts = (r.actions || []).map((a) =>
        `<span class="chip action">${esc(shortAction(a))}</span>`).join(" ");
      return `<div class="history-item">
        <div class="row1">
          <span class="kind ${esc(r.kind)}">${isChat ? "对话" : "直控"}</span>
          <b>${esc(r.device_id)}</b>
          <span class="st ${r.ok ? "ok" : "bad"}">${r.ok ? "✓ 成功" : "✗ 失败"}</span>
          <span class="time">${fmtTime(r.created_at)}</span>
        </div>
        <div class="row2">${esc(String(ask).slice(0, 120))}</div>
        <div class="row3">
          <span>↳ ${esc((r.result_text || "").slice(0, 140))}</span>
          ${acts} ${dur ? `<span>${dur}</span>` : ""}
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    list.innerHTML = `<div class="empty-note">加载失败：${esc(e.message)}</div>`;
  }
}

/* ================= Tab 切换 ================= */

function switchTab(name) {
  store.tab = name;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  if (name === "manual") renderManual();
  if (name === "history") loadHistory();
}

/* ================= 绑定设备 ================= */

function openBind() {
  $("#bind-id").value = "";
  $("#bind-token").value = "";
  $("#bind-error").textContent = "";
  $("#bind-mask").classList.remove("hidden");
}

async function submitBind() {
  const device_id = $("#bind-id").value.trim();
  const device_token = $("#bind-token").value.trim();
  const err = $("#bind-error");
  err.textContent = "";
  if (!device_id || !device_token) { err.textContent = "请填写完整"; return; }
  try {
    await api("api/devices/bind", { method: "POST", body: { device_id, device_token } });
    $("#bind-mask").classList.add("hidden");
    toast(`设备 ${device_id} 绑定成功`, "ok");
    refreshDevices();
  } catch (e) {
    err.textContent = e.message;
  }
}

/* ================= 事件绑定 & 启动 ================= */

function bindEvents() {
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#auth-mode-switch").addEventListener("click", (e) => {
    e.preventDefault();
    setAuthMode(store.authMode === "login" ? "register" : "login");
  });
  $("#btn-logout").addEventListener("click", (e) => { e.preventDefault(); logout(); });

  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  $("#btn-send").addEventListener("click", sendChat);
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) sendChat();
  });
  $("#chat-box").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip.clickable");
    if (chip) { $("#chat-input").value = chip.dataset.fill; $("#chat-input").focus(); }
  });

  $("#btn-bind").addEventListener("click", openBind);
  $("#bind-cancel").addEventListener("click", () => $("#bind-mask").classList.add("hidden"));
  $("#bind-ok").addEventListener("click", submitBind);

  $("#modal-cancel").addEventListener("click", () => _closeModal(false));
  $("#modal-ok").addEventListener("click", () => _closeModal(true));

  $("#btn-history-refresh").addEventListener("click", loadHistory);

  // WS 断开时的 REST 兜底刷新
  setInterval(() => { if (store.token && !store.wsOk) refreshDevices(); }, 30000);
}

bindEvents();
setAuthMode("login");
if (store.token) enterApp();
else showAuth();
