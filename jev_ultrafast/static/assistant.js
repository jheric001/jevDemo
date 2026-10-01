const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const headers = { "Content-Type": "application/json", "X-Demo-Token": token };
let state = { phase: "idle" },
  busy = false,
  pollTimer = null,
  lastUsage = null,
  siteList = [];
const RATE_TWD = 32;
const DEFAULT_SITES = ["momo", "pchome"];
const DEFAULT_TASK =
  "搜尋「水凝尿布M」，只要水凝尿布 M 尺寸的商品，不要其他尺寸、不要其他品牌。每個商品點進去之後收錄本頁。不要加入購物車或購買。最後整理成跨網站比較表，標出最便宜和每片單價最低的。";
const INTENTS = { product_search: "找商品／比價", sentiment: "輿情分析", patrol: "巡查貼文", general: "一般查詢" };
const COLLECTIONS = { product: "商品", post: "貼文", article: "文章／新聞", generic: "一般頁面" };
const STEP_BADGE = { search_collect: "搜尋", browse: "Jev 操作", navigate: "開網址", collect_candidates: "挑候選", foreach_candidate: "逐一處理", capture: "收錄", report: "報告" };
const PHASES = { idle: "尚未開始", planning: "分析任務中…", planned: "規劃完成，等待執行", running: "執行中…", done: "全部完成", failed: "失敗", stopped: "已停止" };
const FIELD_LABELS = {
  name: "名稱", brand: "品牌", size: "尺寸", price_twd: "價格（台幣）", pack_count: "總片數", unit_price_note: "單價", seller: "賣家", promotion: "促銷", specs: "規格", availability: "供貨", matches_criteria: "符合", reason_zh: "判斷原因", summary_zh: "摘要", url: "連結", title: "頁面標題", author: "作者", posted_at: "時間", content: "內容", likes: "讚數", comments: "留言", shares: "分享", sentiment: "態度", source: "來源", date: "日期", headline: "標題", key_points: "重點", list_title: "清單標題", list_price_twd: "清單價格", candidate_reason_zh: "入選原因", captured_at: "收錄時間", id: "#", site: "網站", keyword: "關鍵字", screenshot: "截圖",
};
const OPS = { CLICK: "點擊", TYPE_TEXT: "輸入文字", SELECT: "選擇選項", SCROLL_UP: "向上捲動", SCROLL_DOWN: "向下捲動", WAIT: "等待", DONE: "完成", BLOCKED: "無法繼續" };
const ERRORS = [
  [/No target with given id/, "任務用的瀏覽器分頁已被關閉或失效，請重新分析並執行。"],
  [/'code': -32\d+|"code": -32\d+/, "瀏覽器連線錯誤，分頁可能已關閉或 Chrome 已重啟。"],
  [/daemon|remote debugging/i, "連不上 Chrome，請確認 Chrome 已開啟並允許遠端偵錯。"],
  [/Model provider returned HTTP (\d+)[^]*$/, "模型服務回應錯誤（代碼 $1），這一步沒有執行。"],
  [/Model connection failed[^]*$/, "無法連線到模型服務（逾時或網路問題），這一步沒有執行，請再試一次。"],
  [/Failed to fetch|NetworkError/, "無法連線到本機伺服器，請確認它還在執行。"],
  [/Local demo requests only|只接受本機請求/, "這個分頁的連線已失效，請重新整理頁面。"],
];
const zhError = (m) => {
  const text = String(m || "發生未知錯誤");
  for (const [p, r] of ERRORS) if (p.test(text)) return text.replace(p, r).slice(0, 300);
  return text;
};
const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (v) => (v == null ? "—" : Number(v).toLocaleString("zh-Hant-TW"));
const usd = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : `${Number(v).toFixed(Number(v) < 0.01 ? 5 : 3)} 美元`);
const twd = (v) => (v == null || Number.isNaN(Number(v)) ? "" : `（約 ${(Number(v) * RATE_TWD).toFixed(Number(v) * RATE_TWD < 1 ? 2 : 0)} 元台幣）`);
const modelName = (id) => String(id || "").split("/").pop().replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const label = (f) => FIELD_LABELS[f] || f;
const selectedSites = () => [...document.querySelectorAll("#sites input:checked")].map((i) => i.value);

async function api(name, body) {
  const response = await fetch(`/api/assistant/${name}`, { method: "POST", headers, body: JSON.stringify(body || {}) });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "請求失敗");
  state = data;
  render();
  return data;
}
async function refreshState() {
  try {
    state = await fetch("/api/assistant/state").then((r) => r.json());
    render();
  } catch {
    $("run-status").textContent = "無法連線到本機伺服器";
  }
}
function setPolling(on) {
  clearInterval(pollTimer);
  pollTimer = on ? setInterval(refreshState, 1000) : null;
}
function lockInputs(locked) {
  for (const id of ["analyze", "url", "task", "max-items", "search-jev"]) $(id).disabled = locked;
  document.querySelectorAll("#sites input").forEach((i) => (i.disabled = locked));
}
async function perform(fn, note) {
  if (busy) return;
  busy = true;
  lockInputs(true);
  $("error").hidden = true;
  if (note) $("plan-note").textContent = note;
  try {
    await fn();
  } catch (error) {
    $("error").textContent = zhError(error.message);
    $("error").hidden = false;
    await refreshState();
  } finally {
    busy = false;
    lockInputs(state.phase === "running" || state.phase === "planning");
  }
}

/* ---------- 網站清單 ---------- */
async function loadSites() {
  try {
    siteList = await fetch("/api/sites").then((r) => r.json());
  } catch {
    siteList = [];
  }
  const saved = (() => { try { return JSON.parse(localStorage.getItem("assistantSites") || "null"); } catch { return null; } })();
  const chosen = new Set(saved || DEFAULT_SITES);
  $("sites").innerHTML = siteList
    .map((s) => `<label class="${chosen.has(s.key) ? "on" : ""}"><input type="checkbox" value="${escape(s.key)}" ${chosen.has(s.key) ? "checked" : ""}/> ${escape(s.name)}${s.search ? "" : " <span class='note'>（Jev 打字）</span>"}</label>`)
    .join("");
  $("sites").addEventListener("change", () => {
    document.querySelectorAll("#sites label").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked));
    localStorage.setItem("assistantSites", JSON.stringify(selectedSites()));
  });
}

/* ---------- 畫面 ---------- */
function render() {
  const phase = state.phase || "idle";
  const running = phase === "running" || phase === "planning";
  setPolling(running);
  $("run-status").textContent = PHASES[phase] || phase;
  $("plan-note").textContent = phase === "idle" ? "先按「分析任務」看規劃，確認後再執行。" : `狀態：${PHASES[phase] || phase}`;
  $("run").disabled = running || !state.plan;
  $("run").hidden = running;
  $("stop").hidden = !running;
  $("p-keywords").disabled = running;
  $("p-pages").disabled = running;
  if (!busy) lockInputs(running);
  if (state.error) {
    $("error").textContent = zhError(state.error);
    $("error").hidden = false;
  }
  const plan = state.plan;
  $("plan-card").hidden = !plan;
  if (plan) {
    $("p-intent").textContent = INTENTS[plan.intent] || plan.intent;
    $("p-sites").textContent = (state.site_names || []).join("、") + (plan.search_mode === "jev" ? "（Jev 打字搜尋）" : "");
    $("p-collection").textContent = `${COLLECTIONS[plan.collection.type] || plan.collection.type} · ${plan.collection.fields.map(label).join("、")}`;
    $("p-criteria").textContent = plan.criteria_zh || "—";
    $("p-model").textContent = state.plan_meta ? `${modelName(state.plan_meta.model)} · ${state.plan_meta.latency_ms} 毫秒 · ${usd(state.plan_meta.cost_usd)}` : "—";
    $("p-summary").textContent = plan.summary_zh || "";
    if (document.activeElement !== $("p-keywords")) $("p-keywords").value = (state.keywords || plan.keywords || []).join("\n");
    if (document.activeElement !== $("p-pages")) $("p-pages").value = plan.max_pages || 2;
    $("p-steps").innerHTML = (state.steps || [])
      .map((s) => `<li class="${s.status}"><span class="badge">${escape(STEP_BADGE[s.type] || s.type)}</span><span>${escape(s.text)}</span></li>`)
      .join("");
  }
  const live = phase !== "idle" && phase !== "planning" && (state.page || (state.log || []).length);
  $("live").hidden = !live;
  if (state.page?.screenshot) $("shot").src = `data:image/jpeg;base64,${state.page.screenshot}`;
  $("live-url").textContent = state.page?.url ? `· ${state.page.url.slice(0, 90)}` : "";
  const jev = state.jev;
  $("jev-line").textContent = state.current_item
    ? `逐一處理中：第 ${state.current_item.index}/${state.current_item.total} 筆 ${state.current_item.title}`
    : jev
      ? `Jev：${jev.goal_zh || jev.goal_en}　最近動作：${(jev.history || []).slice(-3).map((h) => `${OPS[h.operation] || h.operation} ${h.action || ""}${h.text ? `「${h.text}」` : ""}`).join(" → ") || "—"}`
      : "";
  const searches = state.searches || [];
  $("searches-wrap").hidden = !searches.length;
  if (searches.length) {
    $("searches").innerHTML =
      "<tr><th>網站</th><th>關鍵字</th><th>頁</th><th>連結數</th><th>新增候選</th></tr>" +
      searches.map((s) => `<tr><td>${escape(s.site)}</td><td>${escape(s.keyword)}</td><td class="num">${s.page}</td><td class="num">${num(s.links)}</td><td class="num">${num(s.new)}</td></tr>`).join("");
  }
  const log = $("log");
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.innerHTML = (state.log || []).map((e) => `<div class="${e.level}"><time>${e.t}</time><span>${escape(e.text)}</span></div>`).join("");
  if (stick) log.scrollTop = log.scrollHeight;
  renderRecords();
  $("report-card").hidden = !state.report_md;
  if (state.report_md) {
    $("report").innerHTML = markdown(state.report_md);
    $("report-file").textContent = state.report_file ? `已存檔：${state.reports_dir}/${state.report_file}` : "";
  }
  $("collection-path").textContent = state.collection_dir ? `收錄資料夾：${state.collection_dir}` : "";
  if (phase === "done" && state.report_file && !render.lastArchived) {
    render.lastArchived = state.report_file;
    loadHistory();
  }
}
function renderRecords() {
  const records = state.records || [];
  $("records-card").hidden = !records.length;
  if (!records.length) return;
  const fields = (state.plan?.collection?.fields || []).filter((f) => !["summary_zh", "specs", "content", "key_points"].includes(f));
  const columns = ["id", "site", ...fields, "url"];
  $("records-count").textContent = `${records.length} 筆`;
  $("records-dir").textContent = `已同步存成 records.json 與 records.csv，每筆附截圖：${state.collection_dir}`;
  const cell = (r, f) => {
    const v = r[f];
    if (f === "url") return `<td><a href="${escape(v)}" target="_blank" rel="noopener">開啟</a></td>`;
    if (f === "matches_criteria") return `<td class="${v === true ? "yes" : v === false ? "no" : ""}">${v === true ? "符合" : v === false ? "不符合" : "未知"}</td>`;
    if (typeof v === "number") return `<td class="num">${num(v)}</td>`;
    if (v && typeof v === "object") return `<td>${escape(JSON.stringify(v)).slice(0, 80)}</td>`;
    return `<td>${escape(String(v ?? "—")).slice(0, 90)}</td>`;
  };
  $("records").innerHTML =
    `<tr>${columns.map((c) => `<th>${escape(label(c))}</th>`).join("")}</tr>` +
    records.map((r) => `<tr>${columns.map((c) => cell(r, c)).join("")}</tr>`).join("");
}
function markdown(text) {
  const lines = String(text).split("\n");
  let html = "",
    table = [],
    list = false;
  const inline = (s) => escape(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\[(.+?)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r));
    html += "<table>" + rows.map((r, i) => `<tr>${r.replace(/^\||\|$/g, "").split("|").map((c) => `<${i ? "td" : "th"}>${inline(c.trim())}</${i ? "td" : "th"}>`).join("")}</tr>`).join("") + "</table>";
    table = [];
  };
  const flushList = () => { if (list) { html += "</ul>"; list = false; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\|/.test(line.trim())) { flushList(); table.push(line.trim()); continue; }
    flushTable();
    if (/^---\s*$/.test(line)) continue;
    const h = line.match(/^(#{1,3})\s+(.*)/);
    if (h) { flushList(); html += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`; continue; }
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    flushList();
    if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  flushTable();
  flushList();
  return html;
}

/* ---------- 歷史報告 ---------- */
async function loadHistory() {
  try {
    const data = await fetch("/api/reports").then((r) => r.json());
    $("history-dir").textContent = `· ${data.dir}`;
    const rows = data.reports || [];
    $("history").innerHTML = rows.length
      ? "<tr><th>日期</th><th>關鍵字</th><th>網站</th><th>收錄</th><th>符合</th><th>費用</th><th></th></tr>" +
        rows.map((r) => `<tr><td>${escape(r.date || "")}</td><td>${escape(r.title || "")}</td><td>${escape(r.sites || "")}</td><td class="num">${escape(r.records || "")}</td><td class="num">${escape(r.matched || "")}</td><td class="num">${r.cost_usd ? usd(Number(r.cost_usd)) : ""}</td><td><button type="button" data-report="${escape(r.name)}">開啟</button></td></tr>`).join("")
      : '<tr><td class="note">還沒有存檔的報告。完成一次任務後會自動出現在這裡，也可以直接打開資料夾裡的 index.md。</td></tr>';
  } catch {
    $("history").innerHTML = '<tr><td class="note">無法讀取歷史報告</td></tr>';
  }
}
$("history").addEventListener("click", async (event) => {
  const name = event.target.closest("[data-report]")?.dataset.report;
  if (!name) return;
  try {
    const data = await fetch(`/api/report?name=${encodeURIComponent(name)}`).then((r) => r.json());
    if (data.error) throw Error(data.error);
    $("report-card").hidden = false;
    $("report-file").textContent = `歷史報告：${name}`;
    $("report").innerHTML = markdown(data.markdown);
    $("report-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    $("error").textContent = zhError(error.message);
    $("error").hidden = false;
  }
});

/* ---------- 用量 ---------- */
async function refreshUsage(force) {
  try {
    const u = await fetch(`/api/usage${force ? "?refresh=1" : ""}`).then((r) => r.json());
    lastUsage = u;
    renderUsage(u);
  } catch {
    $("usage-note").textContent = "無法取得用量資料";
  }
}
function renderUsage(u) {
  const c = u.assistant, a = u.all_time || {}, o = u.openrouter || {};
  $("u-jev").textContent = c ? `${num(c.jev_requests)} 次 · ${num(c.jev_input_tokens)} 詞元 · ${num(c.actions)} 個動作` : "—";
  $("u-jev-cost").textContent = c ? `${usd(c.jev_cost_usd)} ${twd(c.jev_cost_usd)}` : "—";
  $("u-helper").textContent = c ? `${num(c.helper_calls)} 次 · ${usd(c.helper_cost_usd)}` : "—";
  $("u-planner").textContent = c ? `${num(c.planner_calls)} 次 · ${usd(c.planner_cost_usd)}` : "—";
  $("u-judge").textContent = c ? `${num(c.judge_calls)} 次 · ${usd(c.judge_cost_usd)}` : "—";
  const total = c ? c.jev_cost_usd + c.helper_cost_usd + c.llm_cost_usd : null;
  $("u-total").textContent = c ? `${usd(total)} ${twd(total)} · ${(c.elapsed_ms / 1000).toFixed(0)} 秒 · 收錄 ${num(c.records)} 筆` : "—";
  $("u-all-jev").textContent = `${num(a.jev_requests)} 次 · ${num(a.jev_input_tokens)} 詞元（${num(a.tasks)} 個任務）`;
  $("u-all-jev-cost").textContent = `${usd(a.jev_cost_usd)} ${twd(a.jev_cost_usd)}`;
  if (!$("prepaid").value && (localStorage.getItem("jevPrepaidUsd") || u.jev.prepaid_usd)) $("prepaid").value = localStorage.getItem("jevPrepaidUsd") || u.jev.prepaid_usd;
  const prepaid = parseFloat($("prepaid").value);
  $("u-all-remaining").textContent = prepaid > 0 ? `${usd(prepaid - (a.jev_cost_usd || 0))} ${twd(prepaid - (a.jev_cost_usd || 0))}` : "請輸入預付金額";
  $("u-all-llm").textContent = `規劃 ${usd(a.planner_cost_usd)} · 判斷 ${usd(a.judge_cost_usd)} · 文字 ${usd(a.helper_cost_usd)}`;
  if (o.error) {
    $("u-or-limit").textContent = o.error; $("u-or-remaining").textContent = "—"; $("u-or-key-used").textContent = "—";
  } else {
    $("u-or-limit").textContent = o.key_limit_usd != null ? `${usd(o.key_limit_usd)} ${twd(o.key_limit_usd)}` : "未設上限";
    $("u-or-remaining").textContent = o.key_limit_usd != null ? `${usd(o.key_remaining_usd)} ${twd(o.key_remaining_usd)}` : `帳戶剩餘 ${usd(o.account_remaining_usd)}`;
    $("u-or-key-used").textContent = `${usd(o.key_used_usd)} · 今日 ${usd(o.key_usage_daily_usd)} · 本月 ${usd(o.key_usage_monthly_usd)}`;
  }
  $("u-models").textContent = `規劃 ${modelName(u.models.planner)} · 判斷 ${modelName(u.models.judge)} · 文字 ${modelName(u.models.text)} · Jev ${u.jev.model}`;
  $("usage-note").textContent = `餘額更新於 ${new Date((o.fetched_at || Date.now() / 1000) * 1000).toLocaleTimeString("zh-Hant-TW")}`;
  const rows = (u.recent || []).map((t) => {
    const jev = t.jev_cost_usd || 0, or = (t.helper_cost_usd || 0) + (t.llm_cost_usd || 0);
    return `<tr><td>${t.started_at_epoch ? new Date(t.started_at_epoch * 1000).toLocaleString("zh-Hant-TW") : ""}</td><td>${escape((t.goal_zh || t.goal || "").slice(0, 40))}</td><td>${escape(t.kind === "assistant" ? "搜尋助手" : "單步任務台")}</td><td class="num">${t.records != null ? num(t.records) : "—"}</td><td class="num">${usd(jev)}</td><td class="num">${usd(or)}</td><td class="num"><b>${usd(jev + or)}</b> ${twd(jev + or)}</td></tr>`;
  });
  $("cost-history").innerHTML = rows.length
    ? "<tr><th>時間</th><th>任務</th><th>來源</th><th>收錄</th><th>Jev</th><th>OpenRouter</th><th>合計</th></tr>" + rows.join("")
    : '<tr><td class="note">還沒有任務紀錄。</td></tr>';
}

/* ---------- 匯出 ---------- */
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}
function toCsv(records) {
  const fields = [];
  for (const r of records) for (const k of Object.keys(r)) if (!fields.includes(k)) fields.push(k);
  const cell = (v) => `"${String(v == null ? "" : typeof v === "object" ? JSON.stringify(v) : v).replace(/"/g, '""')}"`;
  return "﻿" + [fields.map(label).map(cell).join(","), ...records.map((r) => fields.map((f) => cell(r[f])).join(","))].join("\n");
}

/* ---------- 事件 ---------- */
$("task-form").addEventListener("submit", (e) => {
  e.preventDefault();
  perform(
    () =>
      api("plan", {
        sites: selectedSites(),
        url: $("url").value.trim(),
        task_zh: $("task").value.trim(),
        max_items: Number($("max-items").value) || 20,
        search_mode: $("search-jev").checked ? "jev" : "url",
      }),
    "規劃模型分析中，約需 10 到 30 秒…",
  );
});
$("run").addEventListener("click", () =>
  perform(() =>
    api("run", {
      keywords: $("p-keywords").value.split("\n").map((s) => s.trim()).filter(Boolean),
      max_pages: Number($("p-pages").value) || 2,
    }),
  ),
);
$("stop").addEventListener("click", () => perform(() => api("stop", {})));
$("export-csv").addEventListener("click", () => download(`收錄清單-${state.task_id || "jev"}.csv`, toCsv(state.records || []), "text/csv;charset=utf-8"));
$("export-json").addEventListener("click", () => download(`收錄清單-${state.task_id || "jev"}.json`, JSON.stringify(state.records || [], null, 2), "application/json"));
$("usage-refresh").addEventListener("click", () => refreshUsage(true));
$("history-refresh").addEventListener("click", loadHistory);
$("prepaid").addEventListener("input", () => { localStorage.setItem("jevPrepaidUsd", $("prepaid").value); if (lastUsage) renderUsage(lastUsage); });
setInterval(() => { if (!document.hidden) refreshUsage(false); }, 15000);

$("task").value = DEFAULT_TASK;
loadSites().then(refreshState);
loadHistory();
refreshUsage(false);
