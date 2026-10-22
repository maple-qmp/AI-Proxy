/* AI-Proxy console — vanilla JS, no build step.
   All dynamic text is inserted via textContent or DOM nodes; log bodies are
   untrusted input and never reach innerHTML. */
"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const KEY_STORAGE = "aiProxyKey";
const THEME_STORAGE = "ai-proxy.theme";
const SIDEBAR_STORAGE = "ai-proxy.sidebar";

const VIEW = $("#view");
const state = {
  route: "logs",
  logs: {
    window: "24h",
    status: "",
    provider: "",
    route: "",
    client: "",
    model: "",
    q: "",
    limit: 25,
    offset: 0,
    live: false,
    data: null,
    loading: false,
    error: null,
  },
  models: { data: null, loading: false, error: null, filter: "", provider: "" },
  accounts: { data: null, loading: false, error: null, refreshing: false },
  status: null,
  timer: null,
};

let gateResolver = null;

/* ------------------------------------------------------------------ helpers */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value; // only for trusted static SVG
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(value)) node.dataset[k] = v;
    } else if (key in node && key !== "title") {
      try { node[key] = value; } catch { node.setAttribute(key, value); }
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number"
      ? document.createTextNode(String(child)) : child);
  }
  return node;
}

const ICONS = {
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 5H6a1 1 0 0 0-1 1v9"/></svg>',
  close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M8 11l4 4 4-4M5 20h14"/></svg>',
};
const icon = (name) => { const span = el("span", { class: "icon-wrap" }); span.innerHTML = ICONS[name] || ""; return span.firstElementChild; };
const svgIcon = (name) => { const span = el("span"); span.innerHTML = ICONS[name] || ""; return span.firstElementChild; };

function copyText(text, message = "已复制") {
  const done = () => toast(message);
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback());
  } else fallback();
  function fallback() {
    const area = el("textarea", { value: text });
    area.style.position = "fixed"; area.style.opacity = "0";
    document.body.appendChild(area); area.select();
    try { document.execCommand("copy"); done(); } catch { toast("复制失败", true); }
    area.remove();
  }
}

function copyButton(text, label = "复制") {
  const button = el("button", { class: "copy-btn", type: "button", title: label, "aria-label": label });
  button.appendChild(svgIcon("copy"));
  button.addEventListener("click", (event) => { event.stopPropagation(); copyText(text); });
  return button;
}

function toast(message, isError = false) {
  const root = $("#toast-root");
  const node = el("div", { class: "toast" + (isError ? " error" : ""), text: message });
  root.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

const fmt = {
  int: (value) => (value === null || value === undefined ? "–" : Number(value).toLocaleString("zh-CN")),
  compact(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "–";
    const abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(1) + "B";
    if (abs >= 1e6) return (value / 1e6).toFixed(1) + "M";
    if (abs >= 1e4) return (value / 1e3).toFixed(1) + "K";
    return String(value);
  },
  ms(value) {
    if (value === null || value === undefined) return "–";
    if (value >= 1000) return (value / 1000).toFixed(2) + "s";
    return Math.round(value) + "ms";
  },
  bytes(value) {
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let index = 0, size = value;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return size.toFixed(index ? 1 : 0) + " " + units[index];
  },
  time(ms) {
    const date = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },
  clock(ms) {
    const date = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },
  ago(ms) {
    const delta = Math.max(0, Date.now() - ms);
    const minutes = Math.floor(delta / 60000);
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.floor(hours / 24)} 天前`;
  },
  duration(seconds) {
    if (seconds === null || seconds === undefined) return "–";
    const negative = seconds < 0;
    let rest = Math.abs(Math.trunc(seconds));
    const days = Math.floor(rest / 86400); rest -= days * 86400;
    const hours = Math.floor(rest / 3600); rest -= hours * 3600;
    const minutes = Math.floor(rest / 60);
    let text;
    if (days) text = `${days} 天 ${hours} 小时`;
    else if (hours) text = `${hours} 小时 ${minutes} 分`;
    else if (minutes) text = `${minutes} 分钟`;
    else text = `${rest} 秒`;
    return negative ? `已过期 ${text}` : text;
  },
  dateTime(ms) {
    if (!ms) return "–";
    return new Date(ms).toLocaleString("zh-CN", { hour12: false });
  },
  pct(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "–";
    return (value * 100).toFixed(value >= 0.999 ? 0 : 1) + "%";
  },
};

const ROUTE_LABELS = {
  "chat/completions": "Chat",
  "responses": "Responses",
  "messages": "Messages",
};
const ROUTE_CLASS = {
  "chat/completions": "badge-route-chat",
  "responses": "badge-route-responses",
  "messages": "badge-route-messages",
};
const PROVIDER_LABELS = { openai: "ChatGPT", codebuddy: "CodeBuddy" };

/* --------------------------------------------------------------------- api */

function getKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}

function setKey(value) {
  try {
    if (value) sessionStorage.setItem(KEY_STORAGE, value);
    else sessionStorage.removeItem(KEY_STORAGE);
  } catch { /* private mode: the key simply is not remembered */ }
}

async function api(path, options = {}) {
  const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
  const key = getKey();
  if (key) headers.Authorization = "Bearer " + key;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    await promptForKey();
    return api(path, options);
  }
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload?.error?.message || `请求失败 (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function promptForKey() {
  return new Promise((resolve) => {
    const root = $("#gate-root");
    root.replaceChildren();
    const input = el("input", { type: "password", placeholder: "粘贴 GATEWAY_API_KEY", autocomplete: "off" });
    const message = el("div", { class: "gate-error" });
    const submit = async () => {
      const value = input.value.trim();
      if (!value) { message.textContent = "请输入密钥"; return; }
      setKey(value);
      try {
        const check = await fetch("/dashboard/api/status", {
          headers: { Authorization: "Bearer " + value },
        });
        if (check.status === 401) {
          setKey("");
          message.textContent = "密钥不正确";
          return;
        }
      } catch {
        setKey("");
        message.textContent = "无法连接网关";
        return;
      }
      root.replaceChildren();
      document.removeEventListener("keydown", onKey);
      if (gateResolver) { const done = gateResolver; gateResolver = null; done(); }
      resolve();
    };
    const onKey = (event) => {
      if (event.key === "Enter") submit();
      if (event.key === "Escape") { root.replaceChildren(); document.removeEventListener("keydown", onKey); resolve(); }
    };
    document.addEventListener("keydown", onKey);
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") submit(); });
    const card = el("div", { class: "gate-card" }, [
      el("h2", { text: "需要网关密钥" }),
      el("p", { text: "网关设置了 GATEWAY_API_KEY。密钥只保存在当前标签页的内存中，不会写入磁盘。" }),
      input,
      message,
      el("div", { class: "row" }, [
        el("button", { class: "btn", type: "button", text: "取消", onclick: () => {
          root.replaceChildren(); document.removeEventListener("keydown", onKey); resolve();
        } }),
        el("button", { class: "btn btn-primary", type: "button", text: "连接", onclick: submit }),
      ]),
    ]);
    gateResolver = resolve;
    root.appendChild(el("div", { class: "gate-backdrop" }, [card]));
    setTimeout(() => input.focus(), 30);
  });
}

/* -------------------------------------------------------------------- shell */

const ROUTES = {
  logs: { title: "LLM 日志", desc: "网关请求记录" },
  models: { title: "可用模型", desc: "按账号实时发现" },
  accounts: { title: "登录状态", desc: "ChatGPT 与 WorkBuddy / CodeBuddy" },
};

function currentRoute() {
  const hash = (location.hash || "").replace(/^#\/?/, "");
  return ROUTES[hash] ? hash : "logs";
}

function setRoute(route, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", "#/" + route);
  else location.hash = "#/" + route;
}

function applyRoute() {
  const route = currentRoute();
  state.route = route;
  const meta = ROUTES[route];
  $("#page-title").textContent = meta.title;
  $("#page-desc").textContent = meta.desc;
  document.title = meta.title + " · AI-Proxy";
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.route === "#/" + route);
  }
  document.body.classList.remove("nav-open");
  $("#scrim").hidden = true;
  stopPolling();
  if (route === "logs") renderLogs();
  else if (route === "models") renderModels();
  else renderAccounts();
}

function startPolling() {
  stopPolling();
  state.timer = setInterval(() => {
    if (state.route === "logs" && state.logs.live && document.visibilityState === "visible") {
      loadLogs({ silent: true });
    }
  }, 5000);
}

function stopPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

/* --------------------------------------------------------------------- logs */

const WINDOW_OPTIONS = [
  { value: "1h", label: "最近 1 小时" },
  { value: "24h", label: "最近 24 小时" },
  { value: "7d", label: "最近 7 天" },
  { value: "all", label: "全部" },
];

function logsQuery() {
  const { window, status, provider, route, client, model, q, limit, offset } = state.logs;
  const params = new URLSearchParams({ window, limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  if (provider) params.set("provider", provider);
  if (route) params.set("route", route);
  if (client) params.set("client", client);
  if (model) params.set("model", model);
  if (q) params.set("q", q);
  return params.toString();
}

async function loadLogs({ silent = false } = {}) {
  if (state.logs.loading) return;
  state.logs.loading = true;
  if (!silent) { state.logs.error = null; renderLogsBody(); }
  try {
    state.logs.data = await api("/dashboard/api/logs?" + logsQuery());
    state.logs.error = null;
  } catch (error) {
    state.logs.error = error.message;
  } finally {
    state.logs.loading = false;
    if (state.route === "logs") renderLogsBody();
  }
}

function renderLogs() {
  VIEW.replaceChildren(
    el("div", { class: "logs-layout" }, [
      renderFilters(),
      el("div", { class: "logs-main", id: "logs-main" }),
    ]),
  );
  renderLogsBody();
  loadLogs();
}

function filterSection(title, options, selected, onSelect, { includeAll = true } = {}) {
  const section = el("details", { class: "filter-section", open: true });
  const summary = el("summary", {}, [svgIcon("chevron"), el("span", { text: title })]);
  const list = el("div", { class: "filter-options" });
  if (includeAll) list.appendChild(filterOption("全部", "", selected === "", () => onSelect("")));
  for (const option of options) {
    list.appendChild(filterOption(option.label, option.value, selected === option.value, () => onSelect(option.value)));
  }
  section.append(summary, list);
  return section;
}

function filterOption(label, value, checked, onSelect) {
  const input = el("input", { type: "radio", name: "f-" + value + label, checked });
  const row = el("label", { class: "filter-option" }, [input, el("span", { text: label })]);
  row.addEventListener("click", (event) => {
    event.preventDefault();
    input.checked = true;
    onSelect(value);
  });
  return row;
}

function renderFilters() {
  const data = state.logs.data;
  const facets = data?.facets || { providers: [], routes: [], clients: [], models: [] };
  const body = el("div", { class: "filters-body" });

  body.appendChild(filterSection("时间范围", WINDOW_OPTIONS, state.logs.window, (value) => {
    state.logs.window = value || "24h";
    state.logs.offset = 0;
    renderLogs();
  }, { includeAll: false }));
  body.appendChild(filterSection("状态", [
    { value: "success", label: "成功" },
    { value: "error", label: "失败" },
  ], state.logs.status, (value) => {
    state.logs.status = value; state.logs.offset = 0; renderLogs();
  }));
  body.appendChild(filterSection("Provider",
    (facets.providers || []).map((p) => ({ value: p, label: PROVIDER_LABELS[p] || p })),
    state.logs.provider, (value) => {
      state.logs.provider = value; state.logs.offset = 0; renderLogs();
    }, { includeAll: false }));
  body.appendChild(filterSection("端点",
    (facets.routes || []).map((r) => ({ value: r, label: ROUTE_LABELS[r] || r })),
    state.logs.route, (value) => {
      state.logs.route = value; state.logs.offset = 0; renderLogs();
    }, { includeAll: false }));
  body.appendChild(filterSection("客户端",
    (facets.clients || []).map((c) => ({ value: c, label: c })),
    state.logs.client, (value) => {
      state.logs.client = value; state.logs.offset = 0; renderLogs();
    }, { includeAll: false }));
  body.appendChild(filterSection("模型",
    (facets.models || []).slice(0, 20).map((m) => ({ value: m, label: m })),
    state.logs.model, (value) => {
      state.logs.model = value; state.logs.offset = 0; renderLogs();
    }, { includeAll: false }));

  return el("aside", { class: "filters" }, [
    el("div", { class: "filters-head" }, [
      el("strong", { text: "筛选" }),
      el("button", {
        class: "btn btn-ghost", type: "button", text: "重置",
        onclick: () => {
          Object.assign(state.logs, { window: "24h", status: "", provider: "", route: "",
                                      client: "", model: "", q: "", offset: 0 });
          renderLogs();
        },
      }),
    ]),
    body,
  ]);
}

function renderLogsBody() {
  const main = $("#logs-main");
  if (!main) return;
  const data = state.logs.data;
  const summary = data?.summary;

  const toolbar = el("div", { class: "toolbar" }, [
    toolbarButton("refresh", "刷新", state.logs.loading, () => loadLogs()),
    toolbarButton("live", "实时", state.logs.live, () => {
      state.logs.live = !state.logs.live;
      if (state.logs.live) startPolling(); else stopPolling();
      renderLogsBody();
    }, state.logs.live),
    el("div", { class: "search-box" }, [
      svgIcon("search"),
      (() => {
        const input = el("input", { type: "search", placeholder: "搜索模型、错误或正文", value: state.logs.q });
        let timer = null;
        input.addEventListener("input", () => {
          clearTimeout(timer);
          timer = setTimeout(() => {
            state.logs.q = input.value.trim();
            state.logs.offset = 0;
            loadLogs();
          }, 350);
        });
        return input;
      })(),
    ]),
    el("div", { class: "spacer" }),
    el("button", {
      class: "btn btn-danger", type: "button",
      onclick: async () => {
        if (!confirm("确定清空全部请求日志？此操作不可撤销。")) return;
        try {
          const result = await api("/dashboard/api/logs", { method: "DELETE" });
          toast(`已删除 ${result.deleted} 条日志`);
          state.logs.offset = 0;
          await loadLogs();
        } catch (error) { toast(error.message, true); }
      },
      title: "清空日志",
    }, [svgIcon("trash"), el("span", { text: "清空" })]),
  ]);

  const nodes = [toolbar];
  if (state.logs.error) {
    nodes.push(el("div", { class: "alert" }, [
      svgIcon("alert"),
      el("span", { text: state.logs.error }),
      el("button", { class: "btn", type: "button", text: "重试", onclick: () => loadLogs() }),
    ]));
  }
  nodes.push(renderMetricStrip(summary, data?.histogram));
  nodes.push(renderVolumeChart(data?.histogram || []));
  if (!data && state.logs.loading) {
    nodes.push(el("div", { class: "table-wrap" }, [el("div", { class: "empty" }, [el("p", { text: "加载中…" })])]));
  } else if (data && data.items.length === 0) {
    nodes.push(el("div", { class: "table-wrap" }, [renderEmptyLogs()]));
  } else if (data) {
    nodes.push(renderLogsTable(data.items));
  }
  nodes.push(renderPagination(data));
  main.replaceChildren(...nodes);
}

function toolbarButton(kind, label, busy, onClick, active = false) {
  const classes = ["btn"];
  if (active) classes.push("btn-toggle", "on");
  if (kind === "refresh" && busy) classes.push("is-spinning");
  const button = el("button", { class: classes.join(" "), type: "button", onclick: onClick, title: label });
  if (kind === "refresh") button.appendChild(svgIcon("refresh"));
  else if (kind === "live") button.appendChild(el("span", { class: "live-dot" }));
  button.appendChild(el("span", { text: label }));
  return button;
}

function renderMetricStrip(summary, histogram) {
  const strip = el("div", { class: "metric-strip" });
  const total = summary?.total ?? 0;
  const success = summary?.success ?? 0;
  const errors = summary?.errors ?? 0;
  const successRate = total ? success / total : null;
  const tokens = summary?.tokens || { prompt: 0, completion: 0, total: 0 };
  const tokenSum = (tokens.prompt || 0) + (tokens.completion || 0);
  const inputShare = tokenSum ? (tokens.prompt / tokenSum) * 100 : 0;

  strip.appendChild(metricSegment("请求总数", fmt.int(total), [
    sparkline(histogram, (bucket) => bucket.total),
    el("span", { class: "delta", text: `${errors} 失败` }),
  ]));
  strip.appendChild(metricSegment("成功率", successRate === null ? "–" : fmt.pct(successRate), [
    ratioBar(successRate),
    el("span", { class: "delta", text: `${fmt.int(success)} 成功` }),
  ]));
  strip.appendChild(metricSegment("平均延迟", fmt.ms(summary?.latency_ms?.avg), [
    el("span", { class: "delta", text: "全窗口平均" }),
  ]));
  strip.appendChild(metricSegment("P95 延迟", fmt.ms(summary?.latency_ms?.p95), [
    el("span", { class: "delta", text: `P50 ${fmt.ms(summary?.latency_ms?.p50)}` }),
  ]));
  strip.appendChild(metricSegment("Tokens", fmt.compact(tokens.total), [
    (() => {
      const bar = el("div", { class: "split-bar" });
      bar.append(el("i", { class: "split-in" }), el("i", { class: "split-out" }));
      bar.firstChild.style.width = inputShare + "%";
      bar.lastChild.style.width = (100 - inputShare) + "%";
      return bar;
    })(),
    el("span", { class: "delta", text: `入 ${fmt.compact(tokens.prompt)} / 出 ${fmt.compact(tokens.completion)}` }),
  ]));
  return strip;
}

function metricSegment(label, value, foot) {
  return el("div", { class: "metric" }, [
    el("div", { class: "metric-label", text: label }),
    el("div", { class: "metric-value", text: value }),
    el("div", { class: "metric-foot" }, foot),
  ]);
}

function ratioBar(ratio) {
  const bar = el("div", { class: "ratio-bar" });
  const fill = el("i");
  fill.style.width = Math.max(0, Math.min(1, ratio ?? 0)) * 100 + "%";
  if (ratio !== null && ratio !== undefined && ratio < 0.95) fill.style.background = "var(--chart-warning)";
  if (ratio !== null && ratio !== undefined && ratio < 0.8) fill.style.background = "var(--chart-error)";
  bar.appendChild(fill);
  return bar;
}

function sparkline(histogram, pick) {
  const width = 56, height = 16;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (!histogram || histogram.length === 0) return svg;
  const values = histogram.map(pick);
  const max = Math.max(...values, 1);
  const step = histogram.length > 1 ? width / (histogram.length - 1) : width;
  const points = values.map((value, index) => {
    const x = histogram.length > 1 ? index * step : width / 2;
    const y = height - 2 - (value / max) * (height - 4);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--chart-success)");
  line.setAttribute("stroke-width", "1.2");
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("stroke-linecap", "round");
  svg.appendChild(line);
  return svg;
}

function renderVolumeChart(histogram) {
  const card = el("div", { class: "chart-card" });
  const head = el("div", { class: "chart-head" }, [
    el("span", { class: "title", text: "请求量" }),
    el("div", { class: "legend" }, [
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--chart-success)" }), "成功"]),
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--chart-error)" }), "失败"]),
    ]),
  ]);
  const body = el("div", { class: "chart-body" });
  const buckets = histogram.filter(Boolean);
  if (buckets.length === 0) {
    body.appendChild(el("div", { class: "empty", style: "padding:32px 0" }, [
      el("p", { text: "当前窗口没有请求" }),
    ]));
  } else {
    body.appendChild(buildBars(buckets));
  }
  card.append(head, body);
  return card;
}

function buildBars(buckets) {
  const width = 1000, height = 120, padBottom = 16;
  const plotHeight = height - padBottom;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "请求量柱状图");

  const max = Math.max(...buckets.map((b) => b.total), 1);
  const slot = width / buckets.length;
  const barWidth = Math.max(1, Math.min(slot * 0.72, 26));

  buckets.forEach((bucket, index) => {
    const x = index * slot + (slot - barWidth) / 2;
    const success = Math.max(0, bucket.total - bucket.errors);
    const totalHeight = (bucket.total / max) * plotHeight;
    const errorHeight = bucket.total ? (bucket.errors / bucket.total) * totalHeight : 0;
    const successHeight = totalHeight - errorHeight;

    if (successHeight > 0.1) {
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", x.toFixed(2));
      rect.setAttribute("y", (plotHeight - totalHeight).toFixed(2));
      rect.setAttribute("width", barWidth.toFixed(2));
      rect.setAttribute("height", successHeight.toFixed(2));
      rect.setAttribute("fill", "var(--chart-success)");
      rect.setAttribute("fill-opacity", "0.7");
      svg.appendChild(rect);
    }
    if (errorHeight > 0.1) {
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", x.toFixed(2));
      rect.setAttribute("y", (plotHeight - errorHeight).toFixed(2));
      rect.setAttribute("width", barWidth.toFixed(2));
      rect.setAttribute("height", errorHeight.toFixed(2));
      rect.setAttribute("fill", "var(--chart-error)");
      rect.setAttribute("fill-opacity", "0.7");
      svg.appendChild(rect);
    }
    const title = document.createElementNS(ns, "title");
    title.textContent = `${fmt.time(bucket.ts)} · ${bucket.total} 请求 · ${bucket.errors} 失败`;
    svg.appendChild(title);
  });

  const label = (x, text, anchor = "start") => {
    const node = document.createElementNS(ns, "text");
    node.setAttribute("x", x);
    node.setAttribute("y", height - 3);
    node.setAttribute("fill", "var(--muted-foreground)");
    node.setAttribute("font-size", "11");
    node.setAttribute("text-anchor", anchor);
    node.textContent = text;
    svg.appendChild(node);
  };
  label(0, fmt.clock(buckets[0].ts));
  if (buckets.length > 1) label(width, fmt.clock(buckets[buckets.length - 1].ts), "end");
  return svg;
}

function renderEmptyLogs() {
  const hasFilters = state.logs.status || state.logs.provider || state.logs.route
    || state.logs.client || state.logs.model || state.logs.q;
  return el("div", { class: "empty" }, [
    el("h2", { text: hasFilters ? "没有匹配的请求" : "还没有请求日志" }),
    el("p", { text: hasFilters
      ? "试试放宽筛选条件或扩大时间范围。"
      : "通过网关发起一次请求后，这里就会出现记录。" }),
  ]);
}

function routeBadge(route) {
  return el("span", { class: "badge mono " + (ROUTE_CLASS[route] || ""), text: ROUTE_LABELS[route] || route || "–" });
}

function renderLogsTable(items) {
  const table = el("table", { class: "logs" });
  const head = el("thead", {}, [el("tr", {}, [
    el("th", { text: "" }), el("th", { text: "时间" }), el("th", { text: "端点" }),
    el("th", { text: "消息" }), el("th", { text: "模型" }), el("th", { text: "延迟" }),
    el("th", { text: "Tokens" }), el("th", { text: "状态" }), el("th", { text: "客户端" }),
  ])]);
  const body = el("tbody");
  for (const item of items) {
    const row = el("tr", { tabindex: "0", onclick: () => openLogSheet(item.id) });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openLogSheet(item.id); }
    });
    const ok = item.status < 400;
    row.append(
      el("td", { class: "cell-status" }, [
        el("span", { class: "status-bar " + (item.completed ? (ok ? "ok" : "error") : "incomplete") }),
      ]),
      el("td", { class: "cell-time" }, [
        el("span", { class: "mono", text: fmt.time(item.ts) }),
        el("span", { class: "ago", text: fmt.ago(item.ts) }),
      ]),
      el("td", {}, [routeBadge(item.route)]),
      el("td", { class: "cell-msg" }, [
        el("span", { class: "preview", text: item.request_preview || item.error?.text || "—",
                     title: item.request_preview || "" }),
      ]),
      el("td", {}, [modelCell(item)]),
      el("td", {}, [latencyCell(item.duration_ms)]),
      el("td", {}, [tokensCell(item.tokens)]),
      el("td", { class: "cell-code " + (ok ? "ok" : "error"), text: String(item.status) }),
      el("td", { class: "muted", text: item.client || "–" }),
    );
    body.appendChild(row);
  }
  table.append(head, body);
  return el("div", { class: "table-wrap" }, [table]);
}

function modelCell(item) {
  const provider = item.provider || "unknown";
  return el("div", { class: "model-cell" }, [
    el("span", { class: "provider-dot " + provider }),
    el("div", { style: "min-width:0" }, [
      el("div", { class: "name", text: item.model_id || item.requested_model || "–" }),
      el("div", { class: "prov", text: PROVIDER_LABELS[provider] || provider }),
    ]),
  ]);
}

function latencyCell(value) {
  const ratio = Math.max(0, Math.min(1, (value || 0) / 5000));
  const tone = value >= 5000 ? "bad" : value >= 2000 ? "warn" : "";
  const bar = el("span", { class: "bar" });
  const fill = el("i", { class: tone });
  fill.style.width = Math.max(3, ratio * 100) + "%";
  bar.appendChild(fill);
  return el("div", { class: "latency" }, [el("span", { class: "mono", text: fmt.ms(value) }), bar]);
}

function tokensCell(tokens) {
  const prompt = tokens?.prompt ?? 0;
  const completion = tokens?.completion ?? 0;
  const total = tokens?.total;
  const sum = prompt + completion;
  const wrap = el("div", { class: "tokens" });
  wrap.appendChild(el("span", { class: "num", text: total === null || total === undefined ? "–" : fmt.compact(total) }));
  if (sum > 0) {
    const bar = el("div", { class: "split-bar" });
    const inBar = el("i", { class: "split-in" });
    const outBar = el("i", { class: "split-out" });
    inBar.style.width = (prompt / sum) * 100 + "%";
    outBar.style.width = (completion / sum) * 100 + "%";
    bar.append(inBar, outBar);
    wrap.appendChild(bar);
  } else {
    // Reserve the bar's row so token-less streaming rows keep the same height
    // and the value stays aligned with the other rows. (Class name is distinct
    // from the page-level `.empty` placeholder, which has its own padding.)
    wrap.appendChild(el("div", { class: "split-bar bar-placeholder" }));
  }
  return wrap;
}

function renderPagination(data) {
  const total = data?.total ?? 0;
  const limit = state.logs.limit;
  const offset = state.logs.offset;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const container = el("div", { class: "pagination" }, [
    el("span", { text: `${from}–${to} / 共 ${fmt.int(total)} 条` }),
    el("div", { class: "spacer" }),
    (() => {
      const select = el("select", { "aria-label": "每页条数" });
      for (const size of [25, 50, 100]) {
        select.appendChild(el("option", { value: String(size), text: `${size} 条/页`, selected: size === limit }));
      }
      select.addEventListener("change", () => {
        state.logs.limit = Number(select.value);
        state.logs.offset = 0;
        loadLogs();
        renderLogsBody();
      });
      return select;
    })(),
    el("button", {
      class: "btn", type: "button", text: "上一页",
      disabled: offset <= 0,
      onclick: () => { state.logs.offset = Math.max(0, offset - limit); loadLogs(); },
    }),
    el("button", {
      class: "btn", type: "button", text: "下一页",
      disabled: offset + limit >= total,
      onclick: () => { state.logs.offset = offset + limit; loadLogs(); },
    }),
  ]);
  return container;
}

/* --------------------------------------------------------------- log sheet */

async function openLogSheet(id) {
  const root = $("#sheet-root");
  root.replaceChildren();

  const dialog = document.createElement("dialog");
  dialog.className = "sheet";
  dialog.setAttribute("aria-label", "请求详情");
  root.appendChild(dialog);
  dialog.showModal();
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => root.replaceChildren());

  const sheet = dialog;
  sheet.appendChild(el("div", { class: "sheet-body" }, [el("div", { class: "empty" }, [
    el("p", { text: "加载中…" }),
  ])]));

  let detail;
  try {
    const payload = await api(`/dashboard/api/logs/${id}`);
    detail = payload.log;
  } catch (error) {
    sheet.replaceChildren(el("div", { class: "sheet-body" }, [
      el("div", { class: "alert" }, [svgIcon("alert"), el("span", { text: error.message })]),
    ]));
    return;
  }

  const close = () => dialog.close();
  const ok = detail.status < 400;
  const head = el("div", { class: "sheet-head" }, [
    routeBadge(detail.route),
    el("span", { class: "badge " + (detail.completed ? (ok ? "badge-success" : "badge-error") : "badge-warning"),
                 text: detail.completed ? (ok ? "成功" : "失败") : "未完成" }),
    el("span", { class: "badge mono", text: String(detail.status) }),
    (() => {
      // Copy the whole exchange as one JSON blob, which is what gets pasted
      // into a bug report or another tool.
      const button = el("button", { class: "btn btn-ghost", type: "button", title: "复制完整请求与响应" }, [
        svgIcon("copy"), el("span", { text: "复制" }),
      ]);
      button.addEventListener("click", () => copyText(JSON.stringify({
        request: safeParse(detail.bodies?.request),
        response: safeParse(detail.bodies?.response),
        meta: {
          id: detail.id, route: detail.route, status: detail.status,
          provider: detail.provider, model: detail.model_id,
          duration_ms: detail.duration_ms, tokens: detail.tokens,
        },
      }, null, 2)));
      return button;
    })(),
    el("div", { class: "grow" }, [
      el("div", { class: "reqid" }, [
        el("span", { text: "请求 #" + detail.id + (detail.upstream_request_id ? " · " + detail.upstream_request_id : "") }),
      ]),
    ]),
  ]);
  const provider = el("span", { class: "badge" }, [
    el("span", { class: "provider-dot " + (detail.provider || "unknown") }),
    el("span", { text: PROVIDER_LABELS[detail.provider] || detail.provider || "未知" }),
  ]);
  head.appendChild(provider);
  const closeBtn = el("button", { class: "icon-btn", type: "button", "aria-label": "关闭", onclick: close });
  closeBtn.appendChild(svgIcon("close"));
  head.appendChild(closeBtn);

  const hero = el("div", { class: "hero-stats" }, [
    heroStat("延迟", fmt.ms(detail.duration_ms),
      detail.ttfb_ms === null ? fmt.clock(detail.ts) : `首字节 ${fmt.ms(detail.ttfb_ms)}`),
    heroStat("模型", detail.model_id || detail.requested_model || "–", PROVIDER_LABELS[detail.provider] || "–"),
    heroStat("Tokens", fmt.compact(detail.tokens?.total),
      `入 ${fmt.compact(detail.tokens?.prompt)} / 出 ${fmt.compact(detail.tokens?.completion)}`),
    heroStat("客户端", detail.client || "–", detail.client_detail || ""),
    heroStat("时间", fmt.clock(detail.ts), fmt.time(detail.ts)),
  ]);

  const conversation = buildConversation(detail);
  const tabs = el("div", { class: "tabs" });
  const body = el("div", { class: "sheet-body" });
  const paneState = { roles: new Set(ROLE_ORDER) };
  const panes = {
    conversation: () => renderConversationPane(conversation, paneState),
    request: () => renderBodyPane(detail.bodies?.request, "请求正文", detail.bodies?.request_truncated),
    response: () => renderBodyPane(detail.bodies?.response, "响应正文", detail.bodies?.response_truncated),
    overview: () => renderOverviewPane(detail),
    raw: () => renderRawPane(detail),
  };
  const labels = [["conversation", "对话"], ["request", "请求"], ["response", "响应"],
                  ["overview", "概览"], ["raw", "原始"]];
  let active = "conversation";
  const show = (name) => {
    active = name;
    for (const button of tabs.children) button.classList.toggle("active", button.dataset.tab === name);
    body.replaceChildren(panes[name]());
  };
  for (const [name, label] of labels) {
    const tab = el("button", { class: "tab", type: "button", "data-tab": name, onclick: () => show(name) });
    // Build the label as a child node: setting a `text` property on a button
    // replaces its children, which would drop the count badge.
    tab.appendChild(document.createTextNode(label));
    if (name === "conversation" && conversation.messages.length) {
      tab.appendChild(el("span", { class: "tab-count", text: String(conversation.messages.length) }));
    }
    tabs.appendChild(tab);
  }

  sheet.replaceChildren(head, hero, tabs, body);
  show(active);
}

function heroStat(label, value, sub) {
  return el("div", { class: "hero-stat" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value, title: value }),
    sub ? el("div", { class: "sub truncate", text: sub, title: sub }) : null,
  ]);
}

function renderOverviewPane(detail) {
  const rows = [
    ["端点", ROUTE_LABELS[detail.route] || detail.route],
    ["请求模型", detail.requested_model || "–"],
    ["上游模型", detail.model_id || "–"],
    ["Provider", PROVIDER_LABELS[detail.provider] || detail.provider || "–"],
    ["状态码", String(detail.status)],
    ["流式", detail.stream ? "是" : "否"],
    ["完成", detail.completed ? "是" : "否（客户端中断或流未走完）"],
    ["耗时", fmt.ms(detail.duration_ms)],
    ["首字节", detail.ttfb_ms === null ? "–" : fmt.ms(detail.ttfb_ms)],
    ["输入 / 输出 Tokens", `${fmt.int(detail.tokens?.prompt)} / ${fmt.int(detail.tokens?.completion)}`],
    ["请求体积", fmt.bytes(detail.bodies?.request_bytes)],
    ["响应体积", fmt.bytes(detail.bodies?.response_bytes)],
    ["上游请求 ID", detail.upstream_request_id || "–"],
    ["上游错误码", detail.upstream_code === null || detail.upstream_code === undefined ? "–" : String(detail.upstream_code)],
    ["错误类别", detail.error?.category || "–"],
  ];
  const dl = el("dl", { class: "kv" });
  for (const [key, value] of rows) {
    dl.append(el("dt", { text: key }), el("dd", {}, [el("span", { class: "mono", text: value })]));
  }
  const nodes = [dl];
  if (detail.error?.text) {
    nodes.unshift(el("div", { class: "alert", style: "margin-bottom:12px" }, [
      svgIcon("alert"), el("span", { text: detail.error.text }),
    ]));
  }
  return el("div", {}, nodes);
}

function parseJson(text) {
  if (!text) return { value: null, parsed: false };
  try { return { value: JSON.parse(text), parsed: true }; }
  catch { return { value: null, parsed: false }; }
}

function renderBodyPane(text, label, truncated) {
  const wrap = el("div");
  const header = el("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:8px" }, [
    el("strong", { style: "font-size:12.5px", text: label }),
    truncated ? el("span", { class: "badge badge-warning", text: "已截断" }) : null,
  ]);
  if (text) {
    const copy = el("button", { class: "btn btn-ghost", type: "button", title: "复制正文" }, [
      svgIcon("copy"), el("span", { text: "复制" }),
    ]);
    copy.addEventListener("click", () => copyText(text));
    header.appendChild(copy);
  }
  wrap.appendChild(header);
  if (!text) {
    wrap.appendChild(el("div", { class: "empty", style: "padding:28px 0" }, [el("p", { text: "没有正文" })]));
    return wrap;
  }

  const { value, parsed } = parseJson(text);
  const messages = parsed ? extractMessages(value) : null;
  if (messages && messages.length) {
    const list = el("div", { class: "timeline" });
    messages.forEach((message, index) => list.appendChild(renderTimelineRow(message, index === messages.length - 1)));
    wrap.appendChild(list);
    wrap.appendChild(el("details", { style: "margin-top:12px" }, [
      el("summary", { class: "muted", style: "cursor:pointer;font-size:12px", text: "查看原始 JSON" }),
      el("pre", { class: "raw", style: "margin-top:8px", text: pretty(text) }),
    ]));
  } else {
    const assembled = !parsed ? assembleStreamText(text) : null;
    if (assembled) {
      const list = el("div", { class: "timeline" });
      list.appendChild(renderTimelineRow(
        { role: "assistant", text: assembled.text, reasoning: assembled.reasoning, toolCallId: null }, true));
      wrap.appendChild(list);
    }
    wrap.appendChild(el("pre", { class: "raw", text: parsed ? pretty(text) : text }));
  }
  return wrap;
}

function pretty(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}

function extractMessages(value, toolNames = {}) {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value.messages)) {
    return value.messages.filter((m) => m && typeof m === "object").map((m) => normalizeMessage(m, toolNames));
  }
  if (Array.isArray(value.input)) {
    return value.input.filter((m) => m && typeof m === "object").map((m) => normalizeMessage(m, toolNames));
  }
  if (Array.isArray(value.choices) && value.choices[0]?.message) {
    return [normalizeMessage(value.choices[0].message)];
  }
  if (Array.isArray(value.content)) {
    return [normalizeMessage({ role: value.role || "assistant", content: value.content })];
  }
  if (Array.isArray(value.output)) {
    return normalizeResponseItems(value.output);
  }
  return null;
}

/* Merge a role's content blocks into readable text, keeping non-text blocks
   visible as markers instead of silently dropping them. */
function flattenContent(content) {
  if (content === null || content === undefined) return "";
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return JSON.stringify(content, null, 2);
  return content.map((part) => {
    if (typeof part === "string") return part;
    if (!part || typeof part !== "object") return "";
    if (typeof part.text === "string") return part.text;
    switch (part.type) {
      case "thinking":
      case "reasoning":
        return part.thinking || part.text || "";
      case "image":
        return "[图片]";
      case "image_url":
      case "input_image":
        return "[图片]";
      case "tool_use":
        return `[调用 ${part.name || "工具"}]`;
      case "tool_result":
        return typeof part.content === "string" ? part.content : "[工具结果]";
      case "file":
        return `[文件 ${part.file?.filename || ""}]`.replace(" ", "");
      case "input_audio":
        return "[音频]";
      default:
        return part.type ? `[${part.type}]` : "";
    }
  }).filter(Boolean).join("\n");
}

function formatToolCalls(calls) {
  return calls.map((call) => {
    const fn = call?.function || call || {};
    const args = typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments || {}, null, 2);
    return `${fn.name || "tool"}(${args})`;
  }).join("\n\n");
}

function normalizeMessage(message, toolNames = {}) {
  const role = typeof message.role === "string" ? message.role.toLowerCase() : "unknown";
  const reasoning = flattenContent(message.reasoning_content || message.reasoning || "");
  let toolCallId = message.tool_call_id || null;
  let isToolResult = false;
  let text = "";
  if (Array.isArray(message.content)) {
    const parts = [];
    for (const part of message.content) {
      if (typeof part === "string") { parts.push(part); continue; }
      if (!part || typeof part !== "object") continue;
      if (part.type === "tool_result") {
        // A tool result arrives in a user turn; it reads as its own step.
        isToolResult = true;
        toolCallId = part.tool_use_id || toolCallId;
        parts.push(typeof part.content === "string" ? part.content
          : Array.isArray(part.content) ? flattenContent(part.content)
          : JSON.stringify(part.content, null, 2));
        continue;
      }
      if (typeof part.text === "string") { parts.push(part.text); continue; }
      switch (part.type) {
        case "thinking":
        case "reasoning":
          parts.push(part.thinking || part.text || "");
          break;
        case "image":
        case "image_url":
        case "input_image":
          parts.push("[图片]");
          break;
        case "tool_use":
          parts.push(`[调用 ${part.name || "工具"}]`);
          break;
        case "file":
          parts.push(`[文件 ${part.file?.filename || ""}]`);
          break;
        case "input_audio":
          parts.push("[音频]");
          break;
        default:
          parts.push(part.type ? `[${part.type}]` : "");
      }
    }
    text = parts.filter(Boolean).join("\n");
  } else {
    text = flattenContent(message.content);
  }
  if (message.tool_calls?.length) {
    const calls = formatToolCalls(message.tool_calls);
    text = text ? `${text}\n\n${calls}` : calls;
  }
  return {
    role: isToolResult ? "tool" : role,
    text,
    reasoning,
    toolCallId,
    toolName: isToolResult ? (toolNames[toolCallId] || null) : null,
  };
}

/* Responses-API items: messages, tool calls and their outputs each become a
   row, in the order the model produced them. */
function normalizeResponseItems(items) {
  const names = {};
  for (const item of items) {
    if (item?.type === "function_call" && item.call_id) names[item.call_id] = item.name;
  }
  return items.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    if (item.type === "message" || item.role) {
      return [{
        role: (item.role || "assistant").toLowerCase(),
        text: flattenContent(item.content),
        reasoning: "",
        toolCallId: null,
      }];
    }
    if (item.type === "function_call") {
      return [{
        role: "assistant",
        text: `${item.name || "tool"}(${item.arguments || ""})`,
        reasoning: "",
        toolCallId: item.call_id || null,
        call: true,
      }];
    }
    if (item.type === "function_call_output") {
      return [{
        role: "tool",
        text: typeof item.output === "string" ? item.output : JSON.stringify(item.output, null, 2),
        reasoning: "",
        toolCallId: item.call_id || null,
        toolName: names[item.call_id],
      }];
    }
    if (item.type === "reasoning") {
      const text = (item.summary || []).map((s) => s.text).filter(Boolean).join("\n");
      return text ? [{ role: "assistant", text: "", reasoning: text, toolCallId: null }] : [];
    }
    return [];
  });
}

/* Build the whole exchange: the request's messages plus the assistant's reply
   (or the error), so one view answers "what went in and what came out". */
function buildConversation(detail) {
  const request = parseJson(detail.bodies?.request);
  const response = parseJson(detail.bodies?.response);
  const messages = [];
  const seen = new Set();

  const push = (message) => {
    if (!message.text && !message.reasoning) return;
    const key = `${message.role}\u0000${message.text}\u0000${message.reasoning}`;
    if (message.text && seen.has(key)) return;
    if (message.text) seen.add(key);
    messages.push(message);
  };

  const requestValue = request.parsed ? request.value : null;
  let toolNames = {};
  if (requestValue && typeof requestValue === "object") {
    // Map tool_use ids to names so a tool result can be labelled with the tool
    // that produced it, mirroring how a transcript reads in the client.
    for (const message of requestValue.messages || []) {
      if (!Array.isArray(message?.content)) continue;
      for (const part of message.content) {
        if (part?.type === "tool_use" && part.id) toolNames[part.id] = part.name;
      }
    }
    const system = requestValue.system ?? requestValue.instructions;
    if (system) push({ role: "system", text: flattenContent(system), reasoning: "", toolCallId: null });
    for (const message of extractMessages(requestValue, toolNames) || []) push(message);
  }

  if (response.parsed && typeof response.value === "object") {
    for (const message of extractMessages(response.value) || []) push(message);
  } else if (response.parsed === false && detail.bodies?.response) {
    // A streamed transcript: reassemble the assistant's deltas into one turn.
    const assembled = assembleStreamText(detail.bodies.response);
    if (assembled) push({ role: "assistant", text: assembled.text, reasoning: assembled.reasoning, toolCallId: null });
  }

  if (detail.error?.text && detail.status >= 400) {
    messages.push({ role: "error", text: detail.error.text, reasoning: "", toolCallId: null });
  }
  return { messages, system: null };
}

/* Rebuild the assistant's answer from an SSE transcript (chat chunks or
   Responses events) so streamed requests read like buffered ones. */
function assembleStreamText(raw) {
  let text = "";
  let reasoning = "";
  let calls = [];
  for (const line of String(raw).split("\n")) {
    if (!line.startsWith("data: ")) continue;
    const payload = line.slice(6).trim();
    if (!payload || payload === "[DONE]") continue;
    let event;
    try { event = JSON.parse(payload); } catch { continue; }
    if (!event || typeof event !== "object") continue;
    if (Array.isArray(event.choices)) {
      for (const choice of event.choices) {
        const delta = choice?.delta || {};
        if (typeof delta.content === "string") text += delta.content;
        if (typeof delta.reasoning_content === "string") reasoning += delta.reasoning_content;
        for (const call of delta.tool_calls || []) {
          const index = call.index ?? calls.length;
          const current = calls[index] || (calls[index] = { name: "", arguments: "" });
          current.name += call.function?.name || "";
          current.arguments += call.function?.arguments || "";
        }
      }
    } else if (event.type === "response.output_text.delta" && typeof event.delta === "string") {
      text += event.delta;
    } else if (event.type === "response.output_item.done" && event.item?.type === "function_call") {
      calls.push({ name: event.item.name || "", arguments: event.item.arguments || "" });
    } else if (event.type === "content_block_delta" && event.delta?.type === "text_delta") {
      text += event.delta.text || "";
    } else if (event.type === "content_block_delta" && event.delta?.type === "thinking_delta") {
      reasoning += event.delta.thinking || "";
    }
  }
  if (calls.length) {
    const rendered = calls.filter(Boolean).map((c) => `${c.name || "tool"}(${c.arguments})`).join("\n\n");
    text = text ? `${text}\n\n${rendered}` : rendered;
  }
  return text || reasoning
    ? { text: text || reasoning, reasoning: text ? reasoning : "" }
    : null;
}

function safeParse(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

/* ------------------------------------------------------------------- chat */

const ROLE_ORDER = ["system", "user", "assistant", "tool", "error"];
const ROLE_META = {
  system: { label: "System", dot: "system" },
  user: { label: "User", dot: "user" },
  assistant: { label: "Assistant", dot: "assistant" },
  tool: { label: "Tool", dot: "tool" },
  error: { label: "Error", dot: "error" },
};

function renderConversationPane(conversation, paneState) {
  const { messages } = conversation;
  const wrap = el("div");
  if (!messages.length) {
    wrap.appendChild(el("div", { class: "empty" }, [
      el("h2", { text: "没有可解析的对话" }),
      el("p", { text: "请求或响应正文无法按对话结构解析，可在「请求」「响应」或「原始」页签查看原文。" }),
    ]));
    return wrap;
  }

  const present = ROLE_ORDER.filter((role) => messages.some((m) => m.role === role));
  const filterState = { roles: new Set(paneState?.roles || present) };
  const list = el("div", { class: "timeline" });

  const draw = () => {
    const visible = messages.filter((m) => filterState.roles.has(m.role));
    list.replaceChildren(...visible.map((message, index) => renderTimelineRow(message, index === visible.length - 1)));
  };

  if (present.length > 1) {
    const bar = el("div", { class: "role-filter" });
    bar.appendChild(el("span", { class: "role-filter-label", text: "显示" }));
    for (const role of present) {
      const count = messages.filter((m) => m.role === role).length;
      const chip = el("button", {
        class: "role-chip" + (filterState.roles.has(role) ? " on" : ""),
        type: "button",
        onclick: () => {
          if (filterState.roles.has(role)) filterState.roles.delete(role);
          else filterState.roles.add(role);
          chip.classList.toggle("on", filterState.roles.has(role));
          draw();
        },
      }, [
        el("span", { class: "role-dot " + (ROLE_META[role]?.dot || "system") }),
        el("span", { text: ROLE_META[role]?.label || role }),
        el("span", { class: "role-count", text: String(count) }),
      ]);
      bar.appendChild(chip);
    }
    const all = el("button", {
      class: "role-chip ghost", type: "button", text: "全部",
      onclick: () => {
        filterState.roles = new Set(present);
        for (const chip of bar.querySelectorAll(".role-chip:not(.ghost)")) chip.classList.add("on");
        draw();
      },
    });
    bar.appendChild(all);
    wrap.appendChild(bar);
  }

  draw();
  wrap.appendChild(list);
  return wrap;
}

function renderTimelineRow(message, last) {
  const meta = ROLE_META[message.role] || { label: message.role, dot: "system" };
  const lines = (message.text || "").replace(/\s+$/, "").split("\n");
  const lineCount = message.text ? lines.length : 0;
  const tokens = message.text ? Math.max(1, Math.round(message.text.length / 4)) : 0;

  const body = el("div", { class: "msg-body" });
  if (message.reasoning && !message.text) {
    body.appendChild(collapsibleText(message.reasoning, { mono: false, tone: "reasoning" }));
  } else if (message.text) {
    // Content that is itself JSON (tool arguments, structured payloads) reads
    // far better pretty-printed than as one long line.
    const parsed = tryJson(message.text);
    const mono = message.role === "system" || message.role === "tool" || message.role === "error";
    if (parsed && typeof parsed === "object") {
      body.appendChild(collapsiblePre(JSON.stringify(parsed, null, 2), true));
    } else {
      // Every body goes through the collapsible so a long reply clamps instead
      // of pushing the rest of the transcript off screen.
      body.appendChild(collapsibleText(message.text, { mono }));
    }
  }

  const headRow = el("div", { class: "msg-head" }, [
    el("span", { class: "msg-role", text: meta.label }),
    message.toolName ? el("span", { class: "msg-meta mono", text: message.toolName }) : null,
    message.toolCallId ? el("span", { class: "msg-meta mono", text: truncateMiddle(message.toolCallId, 24) }) : null,
    lineCount
      ? el("span", { class: "msg-meta", text: `${lineCount} 行 · ~${fmt.compact(tokens)} tokens` })
      : null,
    message.text || message.reasoning
      ? (() => {
          const copy = el("button", { class: "msg-copy", type: "button", title: "复制此条" });
          copy.appendChild(svgIcon("copy"));
          copy.addEventListener("click", () => copyText(message.text || message.reasoning));
          return copy;
        })()
      : null,
  ]);

  return el("div", { class: "timeline-row" + (last ? " last" : "") }, [
    el("div", { class: "timeline-rail" }, [
      el("span", { class: "role-dot " + meta.dot }),
      last ? null : el("span", { class: "timeline-line" }),
    ]),
    el("div", { class: "timeline-content" }, [headRow, body]),
  ]);
}

function tryJson(text) {
  const trimmed = (text || "").trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    const value = JSON.parse(trimmed);
    return typeof value === "object" ? value : null;
  } catch { return null; }
}

/* Long text opens clamped with an explicit control; short text is shown whole. */
function collapsibleText(text, { mono = true, tone = "" } = {}) {
  const box = el("div", { class: "collapsible" + (tone ? " tone-" + tone : "") });
  const content = el("div", {
    class: (mono ? "mono " : "") + "collapsible-inner",
    text,
  });
  const wrap = el("div", { class: "collapsible-clip" }, [content]);
  box.appendChild(wrap);
  attachExpander(box, wrap, content, text.split("\n").length);
  return box;
}

function collapsiblePre(text, isJson) {
  const content = el("pre", { class: "raw compact", text });
  const wrap = el("div", { class: "collapsible-clip" }, [content]);
  const box = el("div", { class: "collapsible" }, [wrap]);
  attachExpander(box, wrap, content, text.split("\n").length);
  return box;
}

function attachExpander(box, wrap, content, lineCount) {
  const COLLAPSED = 116; // ~7 lines
  // Measure after layout. setTimeout(0) runs once styles are resolvable; rAF is
  // skipped in background/headless contexts, which would silently drop the
  // expander there.
  setTimeout(() => {
    if (content.scrollHeight <= COLLAPSED + 8) return;
    box.classList.add("clamped");
    const toggle = el("button", { class: "collapsible-toggle", type: "button" }, [
      el("span", { text: `展开全部 ${lineCount} 行` }),
      (() => { const s = svgIcon("chevron"); s.classList.add("chev-down"); return s; })(),
    ]);
    const set = (open) => {
      box.classList.toggle("clamped", !open);
      toggle.querySelector("span").textContent = open ? "收起" : `展开全部 ${lineCount} 行`;
    };
    toggle.addEventListener("click", () => set(box.classList.contains("clamped")));
    box.appendChild(toggle);
  }, 0);
}

function truncateMiddle(value, max) {
  if (!value || value.length <= max) return value || "";
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

function renderRawPane(detail) {
  const blocks = [];
  for (const [label, text] of [["请求", detail.bodies?.request], ["响应", detail.bodies?.response]]) {
    if (!text) continue;
    blocks.push(el("div", { style: "margin-bottom:14px" }, [
      el("div", { style: "display:flex;align-items:center;gap:8px;margin-bottom:6px" }, [
        el("strong", { style: "font-size:12.5px", text: label }),
        (() => {
          const button = el("button", { class: "btn btn-ghost", type: "button" }, [
            svgIcon("copy"), el("span", { text: "复制" }),
          ]);
          button.addEventListener("click", () => copyText(text));
          return button;
        })(),
      ]),
      el("pre", { class: "raw", text: pretty(text) }),
    ]));
  }
  if (blocks.length === 0) {
    return el("div", { class: "empty" }, [el("p", { text: "没有正文" })]);
  }
  return el("div", {}, blocks);
}

/* ------------------------------------------------------------------- models */

async function loadModels({ refresh = false } = {}) {
  state.models.loading = true;
  state.models.error = null;
  renderModels();
  try {
    state.models.data = await api("/dashboard/api/models" + (refresh ? "?refresh=1" : ""));
    if (refresh) toast("模型目录已刷新");
  } catch (error) {
    state.models.error = error.message;
  } finally {
    state.models.loading = false;
    if (state.route === "models") renderModels();
  }
}

function renderModels() {
  if (!state.models.data && !state.models.loading) { loadModels(); return; }
  const data = state.models.data;
  const groups = data?.groups || [];
  const chatgpt = groups.find((g) => g.provider === "openai");
  const codebuddy = groups.find((g) => g.provider === "codebuddy");

  const head = el("div", { class: "page-head" }, [
    el("p", { text: "按当前登录账号实时发现；同名模型默认走 ChatGPT，CodeBuddy 需要用 codebuddy/ 前缀。" }),
    el("div", { class: "actions" }, [
      (() => {
        const button = el("button", { class: "btn", type: "button" }, [
          svgIcon("refresh"), el("span", { text: "刷新目录" }),
        ]);
        if (state.models.loading) { button.disabled = true; button.classList.add("is-spinning"); }
        button.addEventListener("click", () => loadModels({ refresh: true }));
        return button;
      })(),
    ]),
  ]);

  const summary = el("div", { class: "summary-cards" }, [
    summaryCard("ChatGPT 模型", (chatgpt?.models.length ?? 0).toString()),
    summaryCard("CodeBuddy 模型", (codebuddy?.models.length ?? 0).toString()),
    summaryCard("合计", (data?.total ?? 0).toString()),
    summaryCard("上游状态", statusText(groups), statusTone(groups)),
  ]);

  const nodes = [head, summary];
  if (state.models.error) {
    nodes.push(el("div", { class: "alert", style: "margin-bottom:12px" }, [
      svgIcon("alert"), el("span", { text: state.models.error }),
      el("button", { class: "btn", type: "button", text: "重试", onclick: () => loadModels() }),
    ]));
  }
  if (!data && state.models.loading) {
    nodes.push(el("div", { class: "empty" }, [el("p", { text: "加载中…" })]));
  } else if (data) {
    for (const group of groups) nodes.push(renderModelGroup(group));
    if (data.total === 0) {
      nodes.push(el("div", { class: "empty" }, [
        el("h2", { text: "暂无可用模型" }),
        el("p", { text: "请先在「登录状态」页完成登录。" }),
      ]));
    }
    nodes.push(el("div", { class: "notice", style: "margin-top:6px" }, [
      svgIcon("info"),
      el("span", { text: `数据获取于 ${fmt.dateTime(data.generated_at)}，模型目录缓存 5 分钟。` }),
    ]));
  }
  VIEW.replaceChildren(...nodes);
}

function summaryCard(label, value, tone) {
  return el("div", { class: "summary-card" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value, style: tone ? `color:${tone}` : null }),
  ]);
}

function statusText(groups) {
  if (!groups.length) return "–";
  const ok = groups.filter((g) => g.status?.ok).length;
  return `${ok} / ${groups.length}`;
}

function statusTone(groups) {
  if (!groups.length) return null;
  const ok = groups.filter((g) => g.status?.ok).length;
  if (ok === groups.length) return "var(--chart-success-ink)";
  return ok === 0 ? "var(--chart-error-ink)" : "var(--chart-warning-ink)";
}

function renderModelGroup(group) {
  const block = el("div", { class: "provider-block" });
  const ok = group.status?.ok;
  const sourceLabel = { live: "实时获取", cache: "缓存", stale: "过期缓存", none: "不可用" }[group.status?.source] || "–";
  const blockHead = el("div", { class: "provider-head" }, [
    el("span", { class: "provider-dot " + group.provider, style: "width:8px;height:8px" }),
    el("h3", { text: group.label }),
    el("span", { class: "meta", text: `${group.models.length} 个模型 · ${sourceLabel}` }),
    el("div", { class: "actions" }, [
      el("span", { class: "badge " + (ok ? "badge-success" : "badge-error"),
                   text: ok ? "正常" : "不可用" }),
    ]),
  ]);
  block.appendChild(blockHead);

  if (!ok || group.status?.error) {
    block.appendChild(el("div", { class: "alert", style: "margin-bottom:8px" }, [
      svgIcon("alert"),
      el("span", { text: group.status?.error || "模型目录不可用" }),
      el("button", { class: "btn", type: "button", text: "重试",
                     onclick: () => loadModels({ refresh: true }) }),
    ]));
  }
  if (group.models.length === 0) {
    block.appendChild(el("div", { class: "empty", style: "padding:22px 0" }, [
      el("p", { text: ok ? "该上游没有返回模型。" : "无法获取模型列表。" }),
    ]));
    return block;
  }

  const table = el("table", { class: "models" });
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "模型" }), el("th", { text: "调用名" }),
    el("th", { text: "上下文" }), el("th", { text: "能力" }), el("th", { text: "备注" }),
  ])]));
  const tbody = el("tbody");
  for (const model of group.models) {
    const caps = el("div", { class: "caps" });
    if (model.images) caps.appendChild(el("span", { class: "badge badge-secondary", text: "图片" }));
    if (model.tools) caps.appendChild(el("span", { class: "badge badge-secondary", text: "工具" }));
    if (!caps.children.length) caps.appendChild(el("span", { class: "muted", text: "—" }));

    const routeName = el("span", { class: "copyable" }, [
      el("span", { class: "mono", text: model.route_name }),
      copyButton(model.route_name, "复制调用名"),
    ]);

    tbody.appendChild(el("tr", {}, [
      el("td", { class: "mono", text: model.id }),
      el("td", {}, [routeName]),
      el("td", { class: "mono", text: fmt.compact(model.context_window) }),
      el("td", {}, [caps]),
      el("td", { class: "muted", text: model.shadowed ? "与 ChatGPT 同名，默认走 ChatGPT" : "" }),
    ]));
  }
  table.appendChild(tbody);
  block.appendChild(el("div", { class: "table-wrap", style: "max-height:none" }, [table]));
  return block;
}

/* ----------------------------------------------------------------- accounts */

const STATE_LABELS = {
  ok: { text: "已登录", cls: "badge-success" },
  expiring: { text: "即将过期", cls: "badge-warning" },
  expired: { text: "已过期", cls: "badge-error" },
  missing: { text: "未登录", cls: "badge-secondary" },
  invalid: { text: "凭据异常", cls: "badge-error" },
};

function stateBadge(value) {
  const meta = STATE_LABELS[value] || { text: value || "未知", cls: "badge-secondary" };
  return el("span", { class: "badge " + meta.cls, text: meta.text });
}

async function loadAccounts({ refresh = false } = {}) {
  if (refresh) state.accounts.refreshing = true;
  else state.accounts.loading = true;
  state.accounts.error = null;
  renderAccounts();
  try {
    const payload = refresh
      ? await api("/dashboard/api/accounts/refresh", { method: "POST" })
      : await api("/dashboard/api/accounts");
    state.accounts.data = payload;
    if (refresh) {
      const failed = Object.entries(payload.results || {}).filter(([, r]) => !r.ok);
      if (failed.length) toast(failed.map(([, r]) => r.error).join("；"), true);
      else toast("Token 已刷新");
    }
  } catch (error) {
    state.accounts.error = error.message;
    if (refresh) toast(error.message, true);
  } finally {
    state.accounts.loading = false;
    state.accounts.refreshing = false;
    if (state.route === "accounts") renderAccounts();
  }
}

function renderAccounts() {
  if (!state.accounts.data && !state.accounts.loading) { loadAccounts(); return; }
  const data = state.accounts.data;
  const accounts = data?.accounts || {};
  const node = el("div");

  node.appendChild(el("div", { class: "page-head" }, [
    el("p", { text: "凭据只从本机文件读取；页面不会显示任何 token 内容。" }),
    el("div", { class: "actions" }, [
      (() => {
        const button = el("button", { class: "btn", type: "button" }, [
          svgIcon("refresh"), el("span", { text: "重新检测" }),
        ]);
        if (state.accounts.loading) { button.disabled = true; button.classList.add("is-spinning"); }
        button.addEventListener("click", () => loadAccounts());
        return button;
      })(),
    ]),
  ]));

  if (state.accounts.error) {
    node.appendChild(el("div", { class: "alert", style: "margin-bottom:12px" }, [
      svgIcon("alert"), el("span", { text: state.accounts.error }),
      el("button", { class: "btn", type: "button", text: "重试", onclick: () => loadAccounts() }),
    ]));
  }
  if (!data && state.accounts.loading) {
    node.appendChild(el("div", { class: "empty" }, [el("p", { text: "加载中…" })]));
    VIEW.replaceChildren(node);
    return;
  }

  const grid = el("div", { class: "accounts-grid" }, [
    chatgptCard(accounts.openai),
    codebuddyCard(accounts.codebuddy),
  ]);
  node.appendChild(grid);
  if (data?.checked_at) {
    node.appendChild(el("div", { class: "notice", style: "margin-top:12px" }, [
      svgIcon("info"),
      el("span", { text: `状态读取于 ${fmt.dateTime(data.checked_at)}。「刷新 Token」会调用上游接口，仅当 token 临近过期时才真正发起网络请求。` }),
    ]));
  }
  VIEW.replaceChildren(node);
}

function refreshButton(label = "刷新 Token") {
  const button = el("button", { class: "btn btn-primary", type: "button" }, [
    svgIcon("refresh"), el("span", { text: label }),
  ]);
  if (state.accounts.refreshing) { button.disabled = true; button.classList.add("is-spinning"); }
  button.addEventListener("click", () => loadAccounts({ refresh: true }));
  return button;
}

function chatgptCard(account) {
  const card = el("div", { class: "account-card" });
  const ready = account && (account.state === "ok" || account.state === "expiring");
  card.appendChild(el("div", { class: "account-head" }, [
    el("span", { class: "provider-dot openai", style: "width:8px;height:8px" }),
    el("h3", { text: "ChatGPT" }),
    stateBadge(account?.state),
    el("div", { class: "actions" }, [refreshButton()]),
  ]));

  const body = el("div", { class: "account-body" });
  if (!account) {
    body.appendChild(el("div", { class: "empty", style: "padding:20px 0" }, [el("p", { text: "没有数据" })]));
    card.appendChild(body);
    return card;
  }

  if (account.error) {
    body.appendChild(el("div", { class: "alert" }, [svgIcon("alert"), el("span", { text: account.error })]));
  }

  const kv = el("dl", { class: "kv" });
  const add = (key, value, mono = true) => {
    kv.append(el("dt", { text: key }), el("dd", {}, [
      mono ? el("span", { class: "mono", text: value }) : el("span", { text: value }),
    ]));
  };
  add("凭据类型", account.kind === "oauth" ? "订阅 OAuth" : account.kind === "api_key" ? "API Key" : "–", false);
  add("账号标识", account.account_id || "–");
  add("Base URL", account.base_url || "–");
  add("凭据文件", account.file || "–");
  add("文件更新", fmt.dateTime(account.file_mtime));
  kv.append(el("dt", { text: "到期时间" }), el("dd", {}, [
    el("div", { class: "mono", text: account.expires_at ? fmt.dateTime(account.expires_at) : "–" }),
    el("div", { class: "muted", style: "font-size:11.5px", text: fmt.duration(account.expires_in_seconds) }),
  ]));
  body.appendChild(kv);

  if (account.expires_at && account.expires_in_seconds !== null) {
    // The bar tracks the remaining piece of a nominal one-hour token, so a
    // freshly minted credential reads full and a stale one reads empty.
    const total = 3600;
    const ratio = Math.max(0, Math.min(1, account.expires_in_seconds / total));
    const track = el("div", { class: "expiry-track" });
    const fill = el("i", { class: account.expires_in_seconds <= 60 ? "bad" : account.expires_in_seconds <= 600 ? "warn" : "" });
    fill.style.width = Math.max(ratio * 100, 2) + "%";
    track.appendChild(fill);
    body.appendChild(track);
    if (account.state === "expired") {
      body.appendChild(el("p", { class: "muted", style: "font-size:11.5px;margin:0",
        text: "Token 已过期；下一次请求会尝试自动刷新，失败时请重新登录。" }));
    } else if (account.state === "expiring") {
      body.appendChild(el("p", { class: "muted", style: "font-size:11.5px;margin:0",
        text: "Token 即将过期，网关会在下一次请求时自动刷新。" }));
    }
  }

  if (account.scopes?.length) {
    const scopes = el("div", { class: "scopes" });
    for (const scope of account.scopes) scopes.appendChild(el("span", { class: "badge mono", text: scope }));
    body.appendChild(el("div", {}, [el("div", { class: "muted", style: "font-size:11.5px;margin-bottom:5px", text: "授权范围" }), scopes]));
  }

  body.appendChild(el("details", { class: "help" }, [
    el("summary", { text: "如何登录 ChatGPT" }),
    el("div", { class: "code-line" }, [
      el("span", { style: "flex:1", text: ".venv/bin/python -m ai_proxy login" }),
      copyButton(".venv/bin/python -m ai_proxy login"),
    ]),
    el("p", { class: "muted", style: "font-size:11.5px;margin:8px 0 0", text: ready
      ? "已登录。登录命令只用于重新授权或切换账号。"
      : "运行上面的命令，浏览器会自动打开授权页；无法访问回调时可在终端粘贴完整回调 URL。" }),
  ]));

  card.appendChild(body);
  return card;
}

function codebuddyCard(account) {
  const card = el("div", { class: "account-card" });
  card.appendChild(el("div", { class: "account-head" }, [
    el("span", { class: "provider-dot codebuddy", style: "width:8px;height:8px" }),
    el("h3", { text: "CodeBuddy / WorkBuddy" }),
    stateBadge(account?.state),
    el("div", { class: "actions" }, [refreshButton()]),
  ]));

  const body = el("div", { class: "account-body" });
  if (!account) {
    body.appendChild(el("div", { class: "empty", style: "padding:20px 0" }, [el("p", { text: "没有数据" })]));
    card.appendChild(body);
    return card;
  }

  if (account.error) {
    body.appendChild(el("div", { class: "alert" }, [svgIcon("alert"), el("span", { text: account.error })]));
  }

  const kv = el("dl", { class: "kv" });
  kv.append(el("dt", { text: "凭据目录" }), el("dd", {}, [
    el("span", { class: "mono", text: account.directory || "–" }),
  ]));
  kv.append(el("dt", { text: "凭据数量" }), el("dd", {}, [
    el("span", { class: "mono", text: String(account.credentials?.length ?? 0) }),
  ]));
  body.appendChild(kv);

  if (account.credentials?.length) {
    const list = el("div", { class: "cred-list" });
    for (const credential of account.credentials) {
      const who = el("div", { class: "who" }, [
        el("div", { class: "name" }, [
          el("span", { class: "badge badge-secondary", text: labelApp(credential.app) }),
          credential.nickname ? el("span", { style: "margin-left:6px", text: credential.nickname }) : null,
        ]),
        el("div", { class: "sub", text: `到期 ${credential.expires_at ? fmt.dateTime(credential.expires_at) : "–"} · 更新 ${fmt.dateTime(credential.file_mtime)}` }),
      ]);
      const right = el("div", { class: "right" }, [
        el("span", { class: "muted", style: "font-size:11.5px", text: fmt.duration(credential.expires_in_seconds) }),
        stateBadge(credential.state),
        credential.selected ? el("span", { class: "badge badge-info", text: "当前使用" }) : null,
      ]);
      if (credential.detail) {
        who.lastChild.textContent = `${credential.detail} · 更新 ${fmt.dateTime(credential.file_mtime)}`;
      }
      list.appendChild(el("div", { class: "cred-row" }, [who, right]));
    }
    body.appendChild(list);
  } else {
    body.appendChild(el("div", { class: "empty", style: "padding:18px 0" }, [
      el("p", { text: "没有发现凭据文件。" }),
    ]));
  }

  body.appendChild(el("div", { class: "notice" }, [
    svgIcon("info"),
    el("span", { text: "网关按文件修改时间选择最新的有效凭据，因此新登录的 WorkBuddy 会自动接管。" }),
  ]));

  body.appendChild(el("details", { class: "help" }, [
    el("summary", { text: "如何登录 CodeBuddy / WorkBuddy" }),
    el("p", { class: "muted", style: "font-size:11.5px;margin:6px 0 0",
              text: "在 CodeBuddy 或 WorkBuddy 桌面端完成登录，然后回到这里点击「重新检测」。" }),
  ]));

  card.appendChild(body);
  return card;
}

function labelApp(app) {
  return app === "workbuddy" ? "WorkBuddy" : app === "codebuddy" ? "CodeBuddy" : "未知";
}

/* -------------------------------------------------------------------- boot */

async function refreshGatewayStatus() {
  const dot = $("#gw-dot");
  const label = $("#gw-label");
  try {
    state.status = await api("/dashboard/api/status");
    dot.className = "dot ok";
    label.textContent = state.status.features.logs ? "网关运行中" : "网关运行中（日志已关闭）";
  } catch (error) {
    dot.className = "dot bad";
    label.textContent = error.status === 401 ? "需要密钥" : "无法连接网关";
  }
}

function wireShell() {
  // Restore the rail state before anything reads it, so the collapse button's
  // label reflects the state the user is actually in on first paint.
  try {
    if (localStorage.getItem(SIDEBAR_STORAGE) === "collapsed") document.body.classList.add("collapsed");
  } catch {}

  const applyTheme = (theme) => {
    const dark = theme === "dark";
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem(THEME_STORAGE, theme); } catch {}
  };

  $("#theme").addEventListener("click", () => {
    applyTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
  });

  $("#gate-btn").addEventListener("click", () => {
    const existing = getKey();
    if (existing) {
      setKey("");
      toast("已清除当前标签页的密钥");
      refreshGatewayStatus();
      applyRoute();
    } else {
      promptForKey().then(() => { refreshGatewayStatus(); applyRoute(); });
    }
  });

  const collapse = $("#collapse");
  const syncCollapseLabel = () => {
    const collapsed = document.body.classList.contains("collapsed");
    const label = collapsed ? "展开侧栏" : "收起侧栏";
    collapse.title = label;
    collapse.setAttribute("aria-label", label);
    collapse.setAttribute("aria-expanded", String(!collapsed));
  };
  collapse.addEventListener("click", () => {
    document.body.classList.toggle("collapsed");
    syncCollapseLabel();
    try {
      localStorage.setItem(SIDEBAR_STORAGE, document.body.classList.contains("collapsed") ? "collapsed" : "expanded");
    } catch {}
  });
  syncCollapseLabel();

  $("#menu").addEventListener("click", () => {
    document.body.classList.add("nav-open");
    $("#scrim").hidden = false;
  });
  $("#scrim").addEventListener("click", () => {
    document.body.classList.remove("nav-open");
    $("#scrim").hidden = true;
  });

  window.addEventListener("hashchange", applyRoute);
}

wireShell();
refreshGatewayStatus();
if (!location.hash) setRoute("logs", { replace: true });
applyRoute();
setInterval(refreshGatewayStatus, 30000);
