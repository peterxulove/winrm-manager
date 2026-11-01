/* ============================================================
   WinRM Manager — 前端逻辑
   ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const AVATAR_COLORS = [
  "#3d8bfd", "#a371f7", "#3fb950", "#d9a022",
  "#f85149", "#39c5cf", "#db6d28", "#6e7681",
];

const state = {
  connected: false,
  profile: {},
  system: {},
  processes: [],
  stats: {},
  selected: new Set(),
  activePid: null,
  sortKey: "cpuPct",
  sortDir: "desc",
  filter: "all",
  search: "",
  autoRefresh: true,
  intervalMs: 3000,
  timer: null,
  busy: false,
  lastElapsed: null,
  lastUpdated: null,
  latency: null,
  hoverTable: false,
  lastInteract: 0,
};

/* ---------------------------------------------------------- 工具函数 */

function esc(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(bytes);
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (sec == null) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}天${h}时`;
  if (h > 0) return `${h}时${m}分`;
  return `${m}分`;
}

function fmtStart(iso) {
  if (!iso) return "—";
  const t = new Date(iso);
  if (isNaN(t.getTime())) return "—";
  const now = new Date();
  const sameDay = t.toDateString() === now.toDateString();
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  const hh = String(t.getHours()).padStart(2, "0");
  const mm = String(t.getMinutes()).padStart(2, "0");
  if (sameDay) return `今天 ${hh}:${mm}`;
  if (t.toDateString() === yest.toDateString()) return `昨天 ${hh}:${mm}`;
  const mo = String(t.getMonth() + 1).padStart(2, "0");
  const dd = String(t.getDate()).padStart(2, "0");
  return `${mo}-${dd} ${hh}:${mm}`;
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes()
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

function avatarColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i += 1) h = (h * 31 + name.charCodeAt(i)) % 997;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function isSystemUser(user) {
  if (!user) return true;
  const u = user.toUpperCase();
  return (
    u.includes("NT AUTHORITY") ||
    u.includes("SYSTEM") ||
    u.startsWith("UMFD-") ||
    u.startsWith("DWM-") ||
    u.includes("LOCAL SERVICE") ||
    u.includes("NETWORK SERVICE")
  );
}

async function api(path, options = {}) {
  const opts = {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Auth-Token": getToken(),
      ...(options.headers || {}),
    },
  };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`服务返回异常（HTTP ${res.status}）`);
  }
  if (res.status === 401 && path !== "/api/login") {
    setToken("");
    showWebAuth("访问凭证已失效，请重新验证");
  }
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `请求失败（HTTP ${res.status}）`);
    err.detail = data.detail;
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------------------------------------------------------- 访问密码 */

function getToken() {
  try {
    return localStorage.getItem("winrm-manager-token") || "";
  } catch {
    return "";
  }
}

function setToken(token) {
  try {
    if (token) localStorage.setItem("winrm-manager-token", token);
    else localStorage.removeItem("winrm-manager-token");
  } catch {
    /* 隐私模式下忽略 */
  }
}

function showWebAuth(message) {
  const box = $("#web-auth");
  box.classList.remove("hidden");
  const errBox = $("#web-auth-error");
  if (message) {
    errBox.textContent = message;
    errBox.classList.add("show");
  }
  setTimeout(() => $("#web-password").focus(), 60);
}

$("#web-auth-btn").addEventListener("click", async () => {
  const pwd = $("#web-password").value;
  const errBox = $("#web-auth-error");
  errBox.classList.remove("show");
  try {
    const data = await api("/api/login", { method: "POST", body: { password: pwd } });
    setToken(data.token);
    $("#web-auth").classList.add("hidden");
    $("#web-password").value = "";
    bootAfterAuth();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.add("show");
  }
});

$("#web-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    $("#web-auth-btn").click();
  }
});

/* ---------------------------------------------------------- Toast */

function toast(message, type = "info", ms = 3600) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  $("#toast-stack").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s, transform .25s";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 260);
  }, ms);
}

/* ---------------------------------------------------------- 连接 */

async function doConnect(payload) {
  const btn = $("#btn-connect");
  const errBox = $("#login-error");
  errBox.classList.remove("show");
  btn.disabled = true;
  btn.textContent = "正在连接…";

  try {
    const data = await api("/api/connect", { method: "POST", body: payload });
    state.connected = true;
    state.profile = data.profile;
    state.system = data.system;
    state.latency = data.latency;

    // 只记住非敏感字段，密码永不落盘
    try {
      if (payload.remember) {
        localStorage.setItem(
          "winrm-manager-last",
          JSON.stringify({
            host: payload.host,
            username: payload.username,
            port: payload.port,
            transport: payload.transport,
          })
        );
      } else {
        localStorage.removeItem("winrm-manager-last");
      }
    } catch {
      /* 忽略 */
    }

    enterApp();
    toast(`已连接 ${data.system.host}（${data.latency}ms）`, "success");
  } catch (err) {
    errBox.textContent = err.message + (err.detail ? `\n${err.detail}` : "");
    errBox.classList.add("show");
  } finally {
    btn.disabled = false;
    btn.textContent = "连接";
  }
}

function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");

  const sys = state.system || {};
  $("#chip-host").textContent = sys.host || state.profile.host || "—";
  $("#chip-ip").textContent = state.profile.host || "";
  $("#latency").textContent = state.latency ? `${state.latency}ms` : "—";

  $("#side-host").textContent = sys.host || "—";
  $("#side-os").textContent = [sys.os, sys.arch].filter(Boolean).join(" · ") || "—";

  const cores = sys.cores || 1;
  $("#gauge-cpu-sub").textContent = `${cores} 核`;
  $("#stat-uptime").textContent = fmtDuration(sys.uptimeSec);
  $("#gauge-mem-sub").textContent =
    sys.totalMem != null
      ? `${fmtBytes(sys.totalMem - (sys.freeMem || 0))} / ${fmtBytes(sys.totalMem)}`
      : "—";

  startAutoRefresh();
  loadProcesses(true);
  loadLogs();
  refreshLatency();
}

async function refreshLatency() {
  try {
    const data = await api("/api/ping");
    state.latency = data.latency;
    $("#latency").textContent = `${data.latency}ms`;
  } catch {
    /* 忽略 */
  }
}

async function loadSystem() {
  try {
    const data = await api("/api/system");
    state.system = data.system;
    const sys = data.system;
    $("#gauge-cpu-sub").textContent = `${sys.cores || 1} 核`;
    $("#stat-uptime").textContent = fmtDuration(sys.uptimeSec);
    $("#gauge-mem-sub").textContent = `${fmtBytes(
      sys.totalMem - (sys.freeMem || 0)
    )} / ${fmtBytes(sys.totalMem)}`;
    renderGauges();
  } catch {
    /* 静默：系统信息非关键路径 */
  }
}

/* ---------------------------------------------------------- 进程加载 */

async function loadProcesses(showSkeleton = false) {
  if (state.busy || !state.connected) return;
  state.busy = true;

  const refreshBtn = $("#btn-refresh");
  refreshBtn.classList.add("spinning");
  if (showSkeleton) $("#skeleton").classList.remove("hidden");

  const scrollEl = $("#table-wrap");
  const scrollTop = scrollEl.scrollTop;

  try {
    const data = await api("/api/processes");
    state.processes = data.processes || [];
    state.stats = data.stats || {};
    state.lastElapsed = data.elapsedMs;
    state.lastUpdated = new Date();

    renderStats();
    renderGauges();
    // 鼠标悬停在表格上、或刚点过表格时，不重绘行，避免打断双击与文本选择
    const recentlyTouched = Date.now() - state.lastInteract < 1200;
    if (!state.hoverTable && !recentlyTouched) {
      renderTable();
    }
    renderStatusbar();

    scrollEl.scrollTop = scrollTop;
    $("#conn-dot").className = "dot";
  } catch (err) {
    $("#conn-dot").className = "dot err";
    toast(`刷新失败：${err.message}`, "error");
    if (err.status === 502) {
      state.autoRefresh = false;
      syncAutoSwitch();
    }
  } finally {
    state.busy = false;
    refreshBtn.classList.remove("spinning");
    $("#skeleton").classList.add("hidden");
  }
}

/* ---------------------------------------------------------- 渲染 */

function renderStats() {
  const stats = state.stats || {};
  $("#stat-count").textContent = stats.count ?? 0;
  $("#stat-threads").textContent = (stats.threads ?? 0).toLocaleString();
  $("#stat-handles").textContent = (stats.handles ?? 0).toLocaleString();

  const sys = state.system || {};
  const memPct =
    sys.totalMem > 0
      ? Math.round(((sys.totalMem - (sys.freeMem || 0)) / sys.totalMem) * 100)
      : 0;
  $("#gauge-mem").style.setProperty("--p", memPct);
  $("#gauge-mem-val").textContent = `${memPct}%`;
}

function renderGauges() {
  const cpu = Math.min(Math.round(state.stats.cpuPct || 0), 100);
  const gauge = $("#gauge-cpu");
  gauge.style.setProperty("--p", cpu);
  gauge.style.setProperty(
    "--gc",
    cpu > 75 ? "#f85149" : cpu > 45 ? "#d9a022" : "#3d8bfd"
  );
  $("#gauge-cpu-val").textContent = `${cpu}%`;

  const sys = state.system || {};
  if (sys.totalMem > 0) {
    const memPct = Math.round(
      ((sys.totalMem - (sys.freeMem || 0)) / sys.totalMem) * 100
    );
    const g = $("#gauge-mem");
    g.style.setProperty("--p", memPct);
    g.style.setProperty("--gc", memPct > 85 ? "#f85149" : "#a371f7");
    $("#gauge-mem-val").textContent = `${memPct}%`;
    $("#gauge-mem-sub").textContent = `${fmtBytes(
      sys.totalMem - (sys.freeMem || 0)
    )} / ${fmtBytes(sys.totalMem)}`;
  }
}

function filteredProcesses() {
  const kw = state.search.trim().toLowerCase();
  const sys = state.system || {};

  let list = state.processes.filter((p) => {
    if (state.filter === "user" && isSystemUser(p.user)) return false;
    if (state.filter === "system" && !isSystemUser(p.user)) return false;
    if (state.filter === "cpu" && (p.cpuPct == null || p.cpuPct < 5)) return false;
    if (state.filter === "mem" && (p.ws || 0) < 200 * 1024 * 1024) return false;

    if (!kw) return true;
    return (
      String(p.pid).includes(kw) ||
      (p.name || "").toLowerCase().includes(kw) ||
      (p.path || "").toLowerCase().includes(kw) ||
      (p.user || "").toLowerCase().includes(kw)
    );
  });

  const key = state.sortKey;
  const dir = state.sortDir === "asc" ? 1 : -1;
  list.sort((a, b) => {
    let va = a[key];
    let vb = b[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string") return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });
  return list;
}

const MOBILE_QUERY = "(max-width: 768px)";
const isMobileView = () => window.matchMedia(MOBILE_QUERY).matches;

function cpuColorOf(pct) {
  if (pct == null) return "#3a4658";
  if (pct > 50) return "#f85149";
  if (pct > 20) return "#d9a022";
  return "#3d8bfd";
}

function renderTable() {
  const list = filteredProcesses();

  // 筛选计数
  const all = state.processes;
  $("#cnt-all").textContent = all.length;
  $("#cnt-user").textContent = all.filter((p) => !isSystemUser(p.user)).length;
  $("#cnt-system").textContent = all.filter((p) => isSystemUser(p.user)).length;
  $("#cnt-cpu").textContent = all.filter(
    (p) => p.cpuPct != null && p.cpuPct >= 5
  ).length;
  $("#cnt-mem").textContent = all.filter((p) => (p.ws || 0) >= 200 * 1024 * 1024)
    .length;

  $("#empty-state").classList.toggle("hidden", list.length > 0);

  if (isMobileView()) {
    renderCards(list);
  } else {
    renderRows(list);
    updateSortHeader();
  }
}

/* 桌面端：表格行 */
function renderRows(list) {
  const html = list
    .map((p) => {
      const sel = state.selected.has(p.pid) ? " selected" : "";
      const prot = p.protected ? " protected" : "";
      const color = avatarColor(p.name || "?");
      const letter = (p.name || "?")[0].toUpperCase();

      const cpuPct = p.cpuPct;
      const cpuW = cpuPct == null ? 0 : Math.min(cpuPct, 100);
      const cpuColor = cpuColorOf(cpuPct);
      const memW = Math.min(((p.ws || 0) / (2 * 1024 * 1024 * 1024)) * 100, 100);

      return `<tr data-pid="${p.pid}" class="${sel}${prot}">
        <td class="num"><div class="dim">${p.pid}</div></td>
        <td>
          <div class="pcell">
            <div class="pavatar" style="background:${color}">${esc(letter)}</div>
            <span class="pname">${esc(p.name)}${
        p.path ? `<small>${esc(p.path.replace(/^.*\\/, ""))}</small>` : ""
      }</span>
          </div>
        </td>
        <td class="num">
          <div class="bar-cell">
            <div class="bar-track"><div class="bar-fill" style="width:${cpuW}%;background:${cpuColor}"></div></div>
            <span class="bar-val" style="color:${
              cpuPct == null ? "var(--text-faint)" : cpuColor
            }">${cpuPct == null ? "—" : cpuPct.toFixed(1) + "%"}</span>
          </div>
        </td>
        <td class="num">
          <div class="bar-cell">
            <div class="bar-track"><div class="bar-fill" style="width:${memW}%;background:#a371f7"></div></div>
            <span class="bar-val">${fmtBytes(p.ws)}</span>
          </div>
        </td>
        <td class="num"><div class="dim">${p.threads ?? "—"}</div></td>
        <td><div class="dim">${esc((p.user || "—").replace(/^.*\\/, ""))}</div></td>
        <td><div class="dim">${fmtStart(p.start)}</div></td>
      </tr>`;
    })
    .join("");

  $("#proc-body").innerHTML = html;
}

/* 移动端：卡片列表 */
function renderCards(list) {
  const html = list
    .map((p) => {
      const sel = state.selected.has(p.pid) ? " selected" : "";
      const prot = p.protected ? " protected" : "";
      const color = avatarColor(p.name || "?");
      const letter = (p.name || "?")[0].toUpperCase();

      const cpuPct = p.cpuPct;
      const cpuColor = cpuColorOf(cpuPct);
      const cpuText = cpuPct == null ? "—" : `${cpuPct.toFixed(1)}%`;
      // 占用极低时也给条一点宽度，否则整条看不见
      const barW =
        cpuPct == null || cpuPct <= 0
          ? 0
          : Math.max(Math.min(cpuPct, 100), 2);
      const user = (p.user || "").replace(/^.*\\/, "") || "—";

      return `<div class="pcard${sel}${prot}" data-pid="${p.pid}">
        <div class="pavatar" style="background:${color}">${esc(letter)}</div>
        <div class="pcard-main">
          <div class="pcard-top">
            <span class="pcard-name">${esc(p.name)}</span>
            <span class="pcard-cpu" style="color:${
              cpuPct == null ? "var(--text-faint)" : cpuColor
            }">${cpuText}</span>
          </div>
          <div class="pcard-meta">
            <span>PID ${p.pid}</span>
            <i class="dot-sep"></i>
            <span>${fmtBytes(p.ws)}</span>
            <i class="dot-sep"></i>
            <span class="puser">${esc(user)}</span>
          </div>
          <div class="pcard-bar"><i style="width:${barW}%;background:${cpuColor}"></i></div>
        </div>
      </div>`;
    })
    .join("");

  $("#proc-cards").innerHTML = html;
}

function updateSortHeader() {
  $$("thead th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sortKey);
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) arrow.textContent = state.sortDir === "asc" ? "▲" : "▼";
  });
}

function renderStatusbar() {
  $("#sb-count").textContent = `${filteredProcesses().length} / ${
    state.processes.length
  } 个进程`;
  $("#sb-elapsed").textContent =
    state.lastElapsed != null ? `采集 ${state.lastElapsed}ms` : "—";
  $("#sb-updated").textContent = state.lastUpdated
    ? `更新于 ${state.lastUpdated.toLocaleTimeString("zh-CN", { hour12: false })}`
    : "未刷新";

  const n = state.selected.size;
  $("#sb-sel").textContent = n === 0 ? "未选中" : `已选 ${n} 个`;

  const killBtn = $("#btn-kill");
  killBtn.disabled = n === 0;
  $("#kill-label").textContent = n > 1 ? `结束进程 (${n})` : "结束进程";

  renderBulkBar();
}

function renderBulkBar() {
  let bar = $("#bulk-bar");
  if (state.selected.size <= 1) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "bulk-bar";
    bar.className = "bulk-bar";
    $(".content").appendChild(bar);
  }
  bar.innerHTML = `<span>已选择 <b>${state.selected.size}</b> 个进程</span>
    <button class="btn btn-danger btn-sm" id="bulk-kill">批量结束</button>
    <button class="btn btn-ghost btn-sm" id="bulk-clear">取消</button>`;
  $("#bulk-kill").onclick = () => confirmKill([...state.selected]);
  $("#bulk-clear").onclick = () => {
    state.selected.clear();
    renderTable();
    renderStatusbar();
  };
}

/* ---------------------------------------------------------- 交互：表格 */

$("#proc-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-pid]");
  if (!tr) return;
  const pid = Number(tr.dataset.pid);

  if (e.metaKey || e.ctrlKey || e.shiftKey) {
    if (state.selected.has(pid)) state.selected.delete(pid);
    else state.selected.add(pid);
  } else {
    state.selected.clear();
    state.selected.add(pid);
  }

  // 只更新 class，不重建表格 —— 重建会让 DOM 节点失效，导致双击事件无法触发
  state.lastInteract = Date.now();
  syncSelection();
  renderStatusbar();
});

/* 同步选中态到表格行与移动端卡片 */
function syncSelection() {
  $$("#proc-body tr").forEach((row) => {
    row.classList.toggle("selected", state.selected.has(Number(row.dataset.pid)));
  });
  $$("#proc-cards .pcard").forEach((card) => {
    card.classList.toggle("selected", state.selected.has(Number(card.dataset.pid)));
  });
}

// 鼠标停留在表格上时暂停重绘，离开后立刻补一次
$("#table-wrap").addEventListener("mouseenter", () => {
  state.hoverTable = true;
});
$("#table-wrap").addEventListener("mouseleave", () => {
  state.hoverTable = false;
  renderTable();
  renderStatusbar();
});

$("#proc-body").addEventListener("dblclick", (e) => {
  const tr = e.target.closest("tr[data-pid]");
  if (!tr) return;
  state.lastInteract = Date.now();
  openDrawer(Number(tr.dataset.pid));
});

$$("thead th").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = key;
      state.sortDir = key === "name" || key === "user" ? "asc" : "desc";
    }
    renderTable();
  });
});

/* ---------------------------------------------------------- 搜索 / 筛选 */

$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  renderTable();
  renderStatusbar();
});

$("#filter-list").addEventListener("click", (e) => {
  const btn = e.target.closest(".filter-item");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  $$(".filter-item").forEach((b) => b.classList.toggle("active", b === btn));
  renderTable();
  renderStatusbar();
});

/* ---------------------------------------------------------- 自动刷新 */

function syncAutoSwitch() {
  $("#auto-switch").classList.toggle("on", state.autoRefresh);
}

function startAutoRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (state.autoRefresh) loadProcesses(false);
  }, state.intervalMs);
}

$("#auto-toggle").addEventListener("click", (e) => {
  if (e.target.tagName === "SELECT") return;
  state.autoRefresh = !state.autoRefresh;
  syncAutoSwitch();
  if (state.autoRefresh) loadProcesses(false);
});

$("#interval").addEventListener("change", (e) => {
  state.intervalMs = Number(e.target.value);
  startAutoRefresh();
});

$("#btn-refresh").addEventListener("click", () => {
  loadProcesses(true);
  loadSystem();
  refreshLatency();
});

/* ---------------------------------------------------------- 详情抽屉 */

async function openDrawer(pid) {
  state.activePid = pid;
  $("#drawer").classList.add("open");
  $("#drawer-backdrop").classList.add("open");
  $("#drawer-body").innerHTML =
    '<div class="skeleton" style="position:static;height:180px;background:transparent"><div class="spinner"></div><span>读取进程详情…</span></div>';

  const p = state.processes.find((x) => x.pid === pid) || {};
  $("#d-avatar").textContent = (p.name || "?")[0].toUpperCase();
  $("#d-avatar").style.background = avatarColor(p.name || "?");
  $("#d-name").textContent = p.name || `PID ${pid}`;
  $("#d-sub").textContent = `PID ${pid} · ${p.user || ""}`;

  try {
    // 详情 + 限制并行取；网速策略单独异步补，不阻塞首屏
    const [detail, lim] = await Promise.all([
      api(`/api/process/${pid}`),
      api(`/api/limits/${pid}`).catch(() => null),
    ]);
    renderDetail(detail.detail, lim?.limits ?? null, null);

    api("/api/netlimit")
      .then((n) => renderPolicies(n.policies || []))
      .catch(() => renderPolicies([]));
  } catch (err) {
    $("#drawer-body").innerHTML = `<div class="warn-box">读取失败：${esc(
      err.message
    )}</div>`;
  }
}

/* 网速策略列表单独渲染，便于异步更新 */
function policyHtml(policies) {
  if (!policies || !policies.length) {
    return '<div class="policy-empty">暂无网速限制</div>';
  }
  return `<div class="policy-list">${policies
    .map(
      (p) => `<div class="policy-item">
      <span class="policy-name">${esc(p.appPath || p.name)}</span>
      <span class="policy-rate">${
        p.throttle ? Math.round(p.throttle / 1024) + " KB/s" : "—"
      }</span>
      <button class="policy-del" data-name="${esc(p.name)}" title="移除">×</button>
    </div>`
    )
    .join("")}</div>`;
}

function renderPolicies(policies) {
  const el = $("#policy-container");
  if (el) el.innerHTML = policyHtml(policies);
}

function popcount(n) {
  let c = 0;
  let v = n >>> 0;
  while (v) {
    c += v & 1;
    v >>>= 1;
  }
  return c;
}

function renderDetail(d, limits, policies) {
  const conns = d.connections || [];
  const connHtml = conns.length
    ? `<div class="conn-list">${conns
        .map(
          (c) => `<div class="conn-row">
        <span class="st">${esc(c.state)}</span>
        <span>${esc(c.local)} → ${esc(c.remote)}</span>
      </div>`
        )
        .join("")}</div>`
    : '<div class="code-block">无 TCP 连接</div>';

  /* ---- 资源限制 ---- */
  const cores = limits?.cores || 1;
  const curAff = limits?.affinity ?? (1 << cores) - 1;
  const curCores = popcount(curAff);
  const curPriority = limits?.priority || "Normal";
  const memLimit = limits?.memLimitMb;

  const coreOptions = [
    `<option value="0"${curCores >= cores ? " selected" : ""}>全部（${cores} 核）</option>`,
    ...Array.from({ length: cores }, (_, i) => {
      const n = i + 1;
      const sel = curCores === n && curCores < cores ? " selected" : "";
      return `<option value="${n}"${sel}>${n} 核</option>`;
    }),
  ].join("");

  const priorityOptions = [
    ["Idle", "最低 (Idle)"],
    ["BelowNormal", "较低 (BelowNormal)"],
    ["Normal", "普通 (Normal)"],
    ["AboveNormal", "较高 (AboveNormal)"],
    ["High", "高 (High)"],
  ]
    .map(
      ([v, label]) =>
        `<option value="${v}"${curPriority === v ? " selected" : ""}>${label}</option>`
    )
    .join("");

  const exeName =
    (d.path ? d.path.split("\\").pop() : "") || `${d.name || ""}` || "";

  $("#drawer-body").innerHTML = `
    <div>
      <div class="section-label">基本信息</div>
      <div class="detail-grid">
        <div class="detail-cell"><div class="k">PID</div><div class="v">${d.pid}</div></div>
        <div class="detail-cell"><div class="k">会话 ID</div><div class="v">${d.sessionId ?? "—"}</div></div>
        <div class="detail-cell"><div class="k">父进程</div><div class="v">${esc(
          d.parentName || "—"
        )} (${d.parentPid ?? "—"})</div></div>
        <div class="detail-cell"><div class="k">所有者</div><div class="v">${esc(
          d.owner || "—"
        )}</div></div>
        <div class="detail-cell"><div class="k">线程 / 句柄</div><div class="v">${
          d.threads ?? "—"
        } / ${d.handles ?? "—"}</div></div>
        <div class="detail-cell"><div class="k">优先级</div><div class="v">${esc(
          curPriority || "—"
        )}</div></div>
        <div class="detail-cell"><div class="k">工作集</div><div class="v">${fmtBytes(
          d.ws
        )}</div></div>
        <div class="detail-cell"><div class="k">私有内存</div><div class="v">${fmtBytes(
          d.pm
        )}</div></div>
        <div class="detail-cell"><div class="k">虚拟内存</div><div class="v">${fmtBytes(
          d.vm
        )}</div></div>
        <div class="detail-cell"><div class="k">累计 CPU</div><div class="v">${
          d.cpu != null ? Number(d.cpu).toFixed(1) + " s" : "—"
        }</div></div>
        <div class="detail-cell" style="grid-column:1/-1"><div class="k">启动时间</div><div class="v">${esc(
          fmtStart(d.createdAt)
        )}</div></div>
      </div>
    </div>

    <div>
      <div class="section-label">资源限制</div>
      <div class="limit-box">
        <div class="limit-row">
          <label>CPU 核心</label>
          <select id="lim-cores" data-cores="${cores}">${coreOptions}</select>
        </div>
        <div class="limit-row">
          <label>优先级</label>
          <select id="lim-priority">${priorityOptions}</select>
        </div>
        <div class="limit-row">
          <label>内存上限</label>
          <input id="lim-mem" type="number" min="1" placeholder="不限制"
                 value="${memLimit ?? ""}">
          <span class="limit-unit">MB</span>
        </div>
        ${
          memLimit
            ? `<div class="limit-warn">该进程已设 ${memLimit} MB 硬上限。Windows 无法远程解除，
               需结束并重启该进程才能恢复。</div>`
            : `<div class="limit-hint">内存上限为硬限制：超出时分配失败，进程可能直接崩溃退出，且设下后无法远程解除。</div>`
        }
        <button class="btn btn-ghost btn-sm" id="lim-apply" style="width:100%">
          应用 CPU / 内存限制
        </button>
      </div>
    </div>

    <div>
      <div class="section-label">网速限制（按程序名）</div>
      <div class="limit-box">
        <div class="limit-row">
          <label>程序</label>
          <input id="net-exe" value="${esc(exeName)}" placeholder="chrome.exe">
        </div>
        <div class="limit-row">
          <label>限速</label>
          <input id="net-kbps" type="number" min="1" placeholder="如 512">
          <span class="limit-unit">KB/s</span>
        </div>
        <div class="limit-hint">Windows QoS 只能按可执行文件名匹配，会同时限住该程序的所有实例，且仅限制出站流量。</div>
        <button class="btn btn-ghost btn-sm" id="net-apply" style="width:100%">
          设置网速限制
        </button>
        <div id="policy-container">${policyHtml(policies)}</div>
      </div>
    </div>

    <div>
      <div class="section-label">可执行文件路径</div>
      <div class="code-block">${esc(d.path || "无访问权限或系统进程")}</div>
    </div>

    <div>
      <div class="section-label">命令行</div>
      <div class="code-block">${esc(d.commandLine || "—")}</div>
    </div>

    <div>
      <div class="section-label">网络连接 (${conns.length})</div>
      ${connHtml}
    </div>
  `;
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer-backdrop").classList.remove("open");
  state.activePid = null;
}

$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);

$("#d-refresh").addEventListener("click", () => {
  if (state.activePid) openDrawer(state.activePid);
});

$("#d-kill").addEventListener("click", () => {
  if (state.activePid) confirmKill([state.activePid]);
});

/* ---------------------------------------------------------- 结束进程 */

function modal(html) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop">${html}</div>`;
  root.querySelector(".modal-backdrop").addEventListener("click", (e) => {
    if (e.target.classList.contains("modal-backdrop")) closeModal();
  });
  return root;
}

function closeModal() {
  $("#modal-root").innerHTML = "";
}

function confirmKill(pids, tree = false) {
  if (!pids.length) return;
  const procs = pids.map((pid) => state.processes.find((p) => p.pid === pid) || { pid });
  const hasProtected = procs.some((p) => p.protected);

  const list = procs
    .slice(0, 40)
    .map((p) => `<span class="pid-chip">${esc(p.name || "?")} · ${p.pid}</span>`)
    .join("");

  const root = modal(`
    <div class="modal">
      <div class="modal-head">
        <h3>${tree ? "结束进程树" : "结束进程"}（${pids.length} 个）</h3>
        <p>${
          tree
            ? "将连同该进程的所有子进程一起强制终止。"
            : "该操作会强制终止进程，未保存的数据将丢失。"
        }</p>
      </div>
      <div class="modal-body">
        <div class="pid-chips">${list}</div>
        <label class="checkbox" style="margin:2px 0 0">
          <input type="checkbox" id="m-tree" ${tree ? "checked" : ""}>
          同时结束所有子进程（进程树）
        </label>
        ${
          hasProtected
            ? '<div class="warn-box">⚠️ 所选包含系统关键进程，强制结束可能导致系统蓝屏、注销或网络中断。</div>'
            : ""
        }
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" id="m-cancel">取消</button>
        <button class="btn btn-danger" id="m-ok">确认结束</button>
      </div>
    </div>
  `);

  root.querySelector("#m-cancel").onclick = closeModal;
  root.querySelector("#m-ok").onclick = async () => {
    const withTree = root.querySelector("#m-tree").checked;
    closeModal();
    try {
      const data = await api("/api/kill", {
        method: "POST",
        body: { pids, force: true, tree: withTree, allowProtected: hasProtected },
      });
      if (data.killed === data.total) {
        toast(`已结束 ${data.killed} 个进程${withTree ? "（含子进程）" : ""}`, "success");
      } else {
        const fails = data.results
          .filter((r) => !r.ok)
          .map((r) => `PID ${r.pid}: ${r.error}`)
          .join("；");
        toast(`成功 ${data.killed} / ${data.total}。${fails}`, "error", 6000);
      }
      state.selected.clear();
      closeDrawer();
      loadProcesses(false);
      loadLogs();
    } catch (err) {
      toast(err.message, "error", 6000);
    }
  };
}

/* ---------------------------------------------------------- 右键菜单 */

async function copyText(text, label) {
  if (!text) {
    toast(`没有可复制的${label}`, "error", 2200);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast(`已复制${label}`, "success", 1600);
  } catch {
    toast("复制失败：浏览器拒绝了剪贴板访问", "error");
  }
}

function hideContextMenu() {
  $("#ctx-menu").classList.add("hidden");
}

function showContextMenu(x, y, pid) {
  const menu = $("#ctx-menu");
  const proc = state.processes.find((p) => p.pid === pid) || {};
  const n = state.selected.size;
  const multi = n > 1;

  menu.innerHTML = `
    <div class="ctx-head">${esc(proc.name || "进程")} · PID ${pid}</div>
    <div class="ctx-item" data-act="detail">查看详情<span class="hint">双击</span></div>
    <div class="ctx-sep"></div>
    <div class="ctx-item danger" data-act="kill">${
      multi ? `结束选中的 ${n} 个进程` : "结束进程"
    }<span class="hint">Del</span></div>
    <div class="ctx-item danger" data-act="kill-tree">结束进程树（含子进程）</div>
    <div class="ctx-sep"></div>
    <div class="ctx-item" data-act="copy-pid">复制 PID</div>
    <div class="ctx-item" data-act="copy-path">复制文件路径</div>
    <div class="ctx-item" data-act="copy-cmd">复制命令行</div>
  `;

  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 10)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 10)}px`;

  menu.onclick = async (e) => {
    const item = e.target.closest(".ctx-item");
    if (!item) return;
    hideContextMenu();
    const act = item.dataset.act;
    if (act === "detail") {
      openDrawer(pid);
    } else if (act === "kill") {
      confirmKill([...state.selected]);
    } else if (act === "kill-tree") {
      confirmKill([...state.selected], true);
    } else if (act === "copy-pid") {
      copyText(String(pid), "PID");
    } else if (act === "copy-path") {
      copyText(proc.path || "", "文件路径");
    } else if (act === "copy-cmd") {
      try {
        const d = await api(`/api/process/${pid}`);
        copyText(d.detail.commandLine || "", "命令行");
      } catch (err) {
        toast(err.message, "error");
      }
    }
  };
}

$("#proc-body").addEventListener("contextmenu", (e) => {
  const tr = e.target.closest("tr[data-pid]");
  if (!tr) return;
  e.preventDefault();
  const pid = Number(tr.dataset.pid);

  // 右键的行若不在选中集内，则改为只选中它
  if (!state.selected.has(pid)) {
    state.selected.clear();
    state.selected.add(pid);
    syncSelection();
    renderStatusbar();
  }
  state.lastInteract = Date.now();
  showContextMenu(e.clientX, e.clientY, pid);
});

document.addEventListener("click", hideContextMenu);
document.addEventListener("scroll", hideContextMenu, true);
window.addEventListener("resize", hideContextMenu);

$("#btn-kill").addEventListener("click", () => {
  confirmKill([...state.selected]);
});

/* ---------------------------------------------------------- 启动进程 */

$("#btn-launch").addEventListener("click", () => {
  const root = modal(`
    <div class="modal">
      <div class="modal-head">
        <h3>启动新进程</h3>
        <p>进程由系统计划任务拉起，以 SYSTEM 权限运行在 Session 0，不显示图形界面。</p>
      </div>
      <div class="modal-body">
        <label class="field">
          <span>可执行文件</span>
          <input id="l-exe" placeholder="notepad.exe 或 C:\\Tools\\app.exe">
        </label>
        <label class="field">
          <span>命令行参数（可选）</span>
          <input id="l-args" placeholder="-flag value">
        </label>
        <label class="field">
          <span>工作目录（可选）</span>
          <input id="l-wd" placeholder="C:\\Windows\\System32">
        </label>
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" id="l-cancel">取消</button>
        <button class="btn btn-primary" style="width:auto;padding:9px 18px" id="l-ok">启动</button>
      </div>
    </div>
  `);

  root.querySelector("#l-cancel").onclick = closeModal;
  root.querySelector("#l-ok").onclick = async () => {
    const exe = root.querySelector("#l-exe").value.trim();
    if (!exe) {
      toast("请填写可执行文件路径", "error");
      return;
    }
    try {
      const data = await api("/api/start", {
        method: "POST",
        body: {
          exe,
          args: root.querySelector("#l-args").value,
          workdir: root.querySelector("#l-wd").value,
        },
      });
      closeModal();
      toast(`已启动 ${data.process.name}（PID ${data.process.pid}）`, "success");
      loadProcesses(false);
      loadLogs();
    } catch (err) {
      toast(err.message, "error", 6000);
    }
  };
});

/* ---------------------------------------------------------- 日志 */

async function loadLogs() {
  try {
    const data = await api("/api/logs");
    const logs = (data.logs || []).slice(-14).reverse();
    const box = $("#log-list");
    if (!logs.length) {
      box.innerHTML = '<div class="empty-log">暂无操作</div>';
      return;
    }
    box.innerHTML = logs
      .map(
        (l) => `<div class="log-item ${l.level}">
      <span class="t">${fmtTime(l.ts)}</span>
      <span class="m">${esc(l.action)}${
          l.detail ? ` · ${esc(l.detail)}` : ""
        }</span>
    </div>`
      )
      .join("");
  } catch {
    /* 忽略 */
  }
}

/* ---------------------------------------------------------- 断开 */

$("#btn-disconnect").addEventListener("click", async () => {
  try {
    await api("/api/disconnect", { method: "POST" });
  } catch {
    /* 忽略 */
  }
  state.connected = false;
  state.processes = [];
  state.selected.clear();
  if (state.timer) clearInterval(state.timer);
  closeDrawer();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-form").querySelector("[name=password]").value = "";
});

/* ---------------------------------------------------------- 快捷键 */

document.addEventListener("keydown", (e) => {
  const inInput = ["INPUT", "SELECT", "TEXTAREA"].includes(
    document.activeElement.tagName
  );

  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
    e.preventDefault();
    $("#search").focus();
    $("#search").select();
    return;
  }
  if (e.key === "Escape") {
    closeModal();
    closeDrawer();
    hideContextMenu();
    return;
  }
  if (!inInput && e.key.toLowerCase() === "r" && state.connected) {
    e.preventDefault();
    loadProcesses(true);
    return;
  }
  if (
    !inInput &&
    (e.key === "Delete" || e.key === "Backspace") &&
    state.selected.size > 0
  ) {
    e.preventDefault();
    confirmKill([...state.selected]);
  }
});

/* ---------------------------------------------------------- 登录表单 */

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  doConnect({
    host: f.host.value.trim(),
    username: f.username.value.trim(),
    password: f.password.value,
    port: Number(f.port.value) || 5985,
    transport: f.transport.value,
    use_ssl: f.use_ssl.value === "1",
    remember: f.remember.checked,
  });
});

/* ---------------------------------------------------------- 启动自检 */

async function bootAfterAuth() {
  try {
    const data = await api("/api/session");
    if (data.connected) {
      state.connected = true;
      state.profile = data.profile || {};
      state.system = data.system || {};
      enterApp();
      return;
    }
  } catch {
    /* 继续走登录流程 */
  }

  // 回填上次使用的连接参数（不含密码）
  const form = $("#login-form");
  try {
    const saved = JSON.parse(localStorage.getItem("winrm-manager-last") || "{}");
    if (saved.host) form.host.value = saved.host;
    if (saved.username) form.username.value = saved.username;
    if (saved.port) form.port.value = saved.port;
    if (saved.transport) form.transport.value = saved.transport;
  } catch {
    /* 忽略 */
  }
  (form.host.value ? form.password : form.host).focus();
}

(async function boot() {
  try {
    const health = await api("/api/health");
    if (health.authRequired && !getToken()) {
      showWebAuth();
      return;
    }
  } catch {
    /* 服务异常时仍显示登录页 */
  }
  bootAfterAuth();
})();

/* ============================================================
   移动端交互
   ============================================================ */

function toggleSidebar(force) {
  const sidebar = $("#sidebar");
  const backdrop = $("#sidebar-backdrop");
  const open =
    force === undefined ? !sidebar.classList.contains("open") : Boolean(force);
  sidebar.classList.toggle("open", open);
  backdrop.classList.toggle("open", open);
  document.body.style.overflow = open ? "hidden" : "";
}

$("#btn-sidebar").addEventListener("click", () => toggleSidebar());
$("#sidebar-backdrop").addEventListener("click", () => toggleSidebar(false));
$("#sidebar-close").addEventListener("click", () => toggleSidebar(false));

// 移动端筛选 chips（与侧边栏筛选联动）
$("#mobile-filters").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  $$(".chip").forEach((c) => c.classList.toggle("active", c === chip));
  $$(".filter-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === state.filter)
  );
  renderTable();
  renderStatusbar();
});

// 移动端排序
$("#mobile-sort").addEventListener("change", (e) => {
  state.sortKey = e.target.value;
  state.sortDir = e.target.value === "name" ? "asc" : "desc";
  renderTable();
  renderStatusbar();
});

// 侧边栏筛选后自动收起
$("#filter-list").addEventListener("click", () => {
  if (isMobileView()) toggleSidebar(false);
});

// 卡片：单击打开详情
let pressTimer = null;
let longPressFired = false;

$("#proc-cards").addEventListener("click", (e) => {
  const card = e.target.closest(".pcard");
  if (!card) return;
  if (longPressFired) {
    longPressFired = false;
    return;
  }
  const pid = Number(card.dataset.pid);
  state.selected.clear();
  state.selected.add(pid);
  syncSelection();
  renderStatusbar();
  openDrawer(pid);
});

// 卡片：长按弹出操作菜单（替代桌面右键）
$("#proc-cards").addEventListener(
  "touchstart",
  (e) => {
    const card = e.target.closest(".pcard");
    if (!card) return;
    longPressFired = false;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(() => {
      longPressFired = true;
      const pid = Number(card.dataset.pid);
      state.selected.clear();
      state.selected.add(pid);
      syncSelection();
      renderStatusbar();
      const r = card.getBoundingClientRect();
      showContextMenu(
        Math.min(r.left + 16, window.innerWidth - 236),
        Math.max(r.top + 20, 12),
        pid
      );
      if (navigator.vibrate) navigator.vibrate(12);
    }, 480);
  },
  { passive: true }
);

["touchend", "touchmove", "touchcancel"].forEach((evt) => {
  $("#proc-cards").addEventListener(
    evt,
    () => {
      clearTimeout(pressTimer);
      pressTimer = null;
    },
    { passive: true }
  );
});

// 横竖屏切换 / 窗口尺寸变化时切换渲染模式
let lastMobileMode = isMobileView();
window.addEventListener("resize", () => {
  const now = isMobileView();
  if (now !== lastMobileMode) {
    lastMobileMode = now;
    if (!now) toggleSidebar(false);
    renderTable();
  }
});

/* ============================================================
   资源限制（CPU / 内存 / 网速）
   ============================================================ */

function confirmMemoryLimit(mb) {
  return new Promise((resolve) => {
    const root = modal(`
      <div class="modal">
        <div class="modal-head">
          <h3>设置 ${mb} MB 内存上限</h3>
          <p>这是硬限制，不是"用慢一点"。</p>
        </div>
        <div class="modal-body">
          <div class="warn-box">
            进程占用超过 ${mb} MB 时，内存分配会直接失败，进程通常当场崩溃退出，
            未保存的数据会丢失。<br><br>
            另外，Windows 无法远程解除该限制 —— 设下之后，只有结束并重新启动这个进程才能恢复。
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn btn-ghost" id="mm-cancel">取消</button>
          <button class="btn btn-danger" id="mm-ok">仍然设置</button>
        </div>
      </div>
    `);
    root.querySelector("#mm-cancel").onclick = () => {
      closeModal();
      resolve(false);
    };
    root.querySelector("#mm-ok").onclick = () => {
      closeModal();
      resolve(true);
    };
  });
}

$("#drawer-body").addEventListener("click", async (e) => {
  const pid = state.activePid;

  /* ---- 应用 CPU / 内存限制 ---- */
  if (e.target.closest("#lim-apply")) {
    if (!pid) return;
    const sel = $("#lim-cores");
    const totalCores = Number(sel.dataset.cores || 1);
    const pick = Number(sel.value);
    // 0 = 全部核心；n = 前 n 个核心
    const affinity = pick === 0 ? (1 << totalCores) - 1 : (1 << pick) - 1;
    const priority = $("#lim-priority").value;
    const memRaw = $("#lim-mem").value.trim();

    const payload = { affinity, priority };
    if (memRaw) {
      const mb = Number(memRaw);
      if (!Number.isFinite(mb) || mb <= 0) {
        toast("内存上限需为正整数", "error");
        return;
      }
      payload.memoryLimitMb = Math.round(mb);
    }

    if (payload.memoryLimitMb) {
      const go = await confirmMemoryLimit(payload.memoryLimitMb);
      if (!go) return;
    }

    const btn = e.target.closest("#lim-apply");
    btn.disabled = true;
    btn.textContent = "应用中…";
    try {
      await api(`/api/limits/${pid}`, { method: "POST", body: payload });
      toast("限制已应用", "success");
      openDrawer(pid);
      loadLogs();
      loadProcesses(false);
    } catch (err) {
      toast(err.message + (err.detail ? `：${err.detail}` : ""), "error", 7000);
      btn.disabled = false;
      btn.textContent = "应用 CPU / 内存限制";
    }
    return;
  }

  /* ---- 设置网速限制 ---- */
  if (e.target.closest("#net-apply")) {
    const exe = $("#net-exe").value.trim();
    const kbps = Number($("#net-kbps").value);
    if (!exe) {
      toast("请填写程序名，如 chrome.exe", "error");
      return;
    }
    if (!Number.isFinite(kbps) || kbps <= 0) {
      toast("请填写有效的限速值（KB/s）", "error");
      return;
    }
    const btn = e.target.closest("#net-apply");
    btn.disabled = true;
    btn.textContent = "设置中…";
    try {
      await api("/api/netlimit", { method: "POST", body: { exe, kbps } });
      toast(`已限制 ${exe} 到 ${kbps} KB/s`, "success");
      if (pid) openDrawer(pid);
      loadLogs();
    } catch (err) {
      toast(err.message + (err.detail ? `：${err.detail}` : ""), "error", 7000);
      btn.disabled = false;
      btn.textContent = "设置网速限制";
    }
    return;
  }

  /* ---- 移除某条网速策略 ---- */
  const del = e.target.closest(".policy-del");
  if (del) {
    const name = del.dataset.name;
    try {
      await api("/api/netlimit/remove", { method: "POST", body: { name } });
      toast("已移除该限速策略", "success");
      if (pid) openDrawer(pid);
      loadLogs();
    } catch (err) {
      toast(err.message, "error", 6000);
    }
  }
});
