const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const headers = { "Content-Type": "application/json", "X-Demo-Token": token };
const MODES = [["llm", "純 LLM"], ["hybrid", "LLM + Jev"], ["jev", "純 Jev"]];
const STEPS = [["understand", "理解任務"], ["search", "網站搜尋"], ["cards", "讀取結果"], ["pick", "挑選商品"], ["compare", "比價整理"]];
const ACTORS = {
  llm: { understand: "LLM", search: "LLM", cards: "程式", pick: "LLM", compare: "LLM" },
  hybrid: { understand: "LLM", search: "Jev", cards: "程式", pick: "Jev", compare: "LLM" },
  jev: { understand: "程式", search: "Jev", cards: "程式", pick: "Jev", compare: "程式" },
};
const ACTOR_CLASS = { LLM: "a-llm", Jev: "a-jev", 程式: "a-code" };
const STATUS = { pending: "等待中", running: "執行中…", done: "完成", failed: "失敗", stopped: "已停止" };
const PHASE = { idle: "", ready: "· 準備中", connecting: "· 連線 Chrome 中（若跳出允許遠端偵錯，請按允許）", running: "· 執行中…", done: "· 已結束", stopped: "· 已停止" };
const ACTIVE = new Set(["ready", "connecting", "running"]);
const ERRORS = [
  [/^[^]*(?:permission-blocked|before approval)[^]*$/i, "Chrome 在等你按「允許遠端偵錯」，沒按就逾時了。請切到 Chrome 按「允許」，再按一次開始比較。"],
  [/^[^]*(?:daemon|remote debugging|DevToolsActivePort)[^]*$/i, "連不上 Chrome。請在 Chrome 打開 chrome://inspect/#remote-debugging，勾選允許遠端偵錯後再試一次。"],
  [/^[^]*No target with given id[^]*$/, "比價用的瀏覽器分頁被關掉了，請重新開始。"],
  [/Model provider returned HTTP (\d+)[^]*$/, "模型服務回應錯誤（代碼 $1），這一步沒有執行。"],
  [/Model connection failed[^]*$/, "無法連線到模型服務，請再試一次。"],
  [/Failed to fetch|NetworkError/, "無法連線到本機伺服器，請確認它還在執行。"],
  [/只接受本機請求/, "這個分頁的連線已失效，請重新整理頁面。"],
];
const zhError = (m) => {
  const text = String(m || "發生未知錯誤");
  for (const [p, r] of ERRORS) if (p.test(text)) return text.replace(p, r).slice(0, 300);
  return text;
};
const DEFAULT_TASK = "在 momo 找「水凝尿布M」，只要 Parasol 水凝尿布 M 號，不要其他尺寸、不要其他品牌，比較價格找出最便宜的。";
let state = { phase: "idle", runs: [] };
let timer = null;

const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const sec = (ms) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)} 秒`);
const usd = (v) => (v == null ? "—" : `$${Number(v).toFixed(Number(v) < 0.01 ? 5 : 4)}`);
const actor = (a) => `<span class="actor ${ACTOR_CLASS[a] || "a-code"}">${escape(a)}</span>`;
const other = (r) => Math.max(0, (r.total_ms || 0) - (r.jev_ms || 0) - (r.llm_ms || 0));

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

function renderMatrix() {
  $("matrix").innerHTML =
    `<tr><th>步驟</th>${MODES.map(([, n]) => `<th>${n}</th>`).join("")}</tr>` +
    STEPS.map(([k, n]) => `<tr><td>${n}</td>${MODES.map(([m]) => `<td>${actor(ACTORS[m][k])}</td>`).join("")}</tr>`).join("");
}

function renderSummary(runs) {
  const max = Math.max(1, ...runs.map((r) => r.total_ms || 0));
  $("bars").innerHTML = runs
    .map((r) => {
      if (r.total_ms == null) return `<div class="bar-row"><b>${escape(r.name)}</b><div class="bar"></div><span class="val note">${STATUS[r.status] || ""}</span></div>`;
      const w = (ms) => `${((ms / max) * 100).toFixed(2)}%`;
      return `<div class="bar-row"><b>${escape(r.name)}</b><div class="bar"><span class="seg-jev" style="width:${w(r.jev_ms)}"></span><span class="seg-llm" style="width:${w(r.llm_ms)}"></span><span class="seg-other" style="width:${w(other(r))}"></span></div><span class="val">總 ${sec(r.total_ms)}｜Jev ${sec(r.jev_ms)}｜LLM ${sec(r.llm_ms)}</span></div>`;
    })
    .join("");
  $("summary").innerHTML =
    '<tr><th>模式</th><th>狀態</th><th class="num">總時間</th><th class="num">Jev 自己</th><th class="num">LLM 自己</th><th class="num">其他</th><th class="num">Jev 呼叫</th><th class="num">LLM 呼叫</th><th class="num">費用</th><th class="num">挑到</th><th>最便宜</th></tr>' +
    runs
      .map((r) => {
        const cheapest = (r.items || []).filter((i) => i.price_twd != null).sort((a, b) => a.price_twd - b.price_twd)[0];
        return `<tr><td><b>${escape(r.name)}</b></td><td>${STATUS[r.status] || r.status}</td><td class="num">${sec(r.total_ms)}</td><td class="num">${sec(r.jev_ms)}</td><td class="num">${sec(r.llm_ms)}</td><td class="num">${r.total_ms == null ? "—" : sec(other(r))}</td><td class="num">${r.jev_calls}</td><td class="num">${r.llm_calls}</td><td class="num">${usd(r.cost_usd)}</td><td class="num">${(r.items || []).length}</td><td>${cheapest ? `${Number(cheapest.price_twd).toLocaleString("zh-Hant-TW")} 元` : "—"}</td></tr>`;
      })
      .join("");
}

function renderRun(r) {
  const steps = r.steps
    .map(
      (s) =>
        `<tr class="${s.status}"><td>${escape(s.name)}<div class="detail">${escape(s.detail)}</div></td><td>${actor(s.actor)}</td><td class="num">${s.ms == null ? (s.status === "running" ? "…" : "—") : sec(s.ms)}</td><td class="num">${s.jev_ms ? sec(s.jev_ms) : "—"}</td><td class="num">${s.llm_ms ? sec(s.llm_ms) : "—"}</td><td class="num">${s.cost_usd ? usd(s.cost_usd) : "—"}</td></tr>`,
    )
    .join("");
  const items = (r.items || []).length
    ? `<table class="grid" style="margin-top:10px"><tr><th>挑到的商品</th><th class="num">價格</th></tr>${r.items
        .map((i) => `<tr><td><a href="${escape(i.url)}" target="_blank" rel="noopener">${escape(i.title)}</a><div class="detail">${escape(i.why || "")}</div></td><td class="num">${i.price_twd == null ? "—" : Number(i.price_twd).toLocaleString("zh-Hant-TW")}</td></tr>`)
        .join("")}</table>`
    : "";
  const decs = r.decisions || [];
  const who = r.mode === "llm" ? "LLM" : "Jev";
  const decisions = decs.length
    ? `<details data-key="${escape(r.mode)}-decisions" style="margin-top:10px"><summary class="note">${who} 決策紀錄（${decs.length} 次）</summary><table class="grid"><tr><th>#</th><th>選擇</th><th>對象</th><th class="num">信心</th><th class="num">元素</th><th>看到搜尋框</th></tr>${decs
        .map((d) => `<tr><td>${d.n}</td><td>${escape(d.operation || "—")}</td><td>${escape(d.target || "—")}</td><td class="num">${d.confidence == null ? "—" : Number(d.confidence).toFixed(2)}</td><td class="num">${d.elements}</td><td>${d.search_box ? "有" : "沒有"}</td></tr>`)
        .join("")}</table>${r.mode === "llm" ? '<p class="note">LLM 只回一個答案，沒有機率與信心。</p>' : ""}${r.log_file ? `<p class="note">完整紀錄：${escape(r.log_file)}</p>` : ""}</details>`
    : "";
  const cands = r.candidates || [];
  const judge = r.mode === "llm" ? "LLM 判斷" : "Jev 機率";
  const candidates = cands.length
    ? `<details data-key="${escape(r.mode)}-candidates" style="margin-top:10px"><summary class="note">全部 ${cands.length} 張卡片與判斷</summary><table class="grid"><tr><th>商品</th><th class="num">價格</th><th class="num">${judge}</th></tr>${cands
        .map((c) => `<tr><td>${c.picked ? "✓ " : ""}${escape(c.title)}</td><td class="num">${c.price_twd == null ? "—" : Number(c.price_twd).toLocaleString("zh-Hant-TW")}</td><td class="num">${c.score == null ? (c.picked ? "挑中" : "—") : Number(c.score).toFixed(2)}</td></tr>`)
        .join("")}</table></details>`
    : "";
  return `<article class="card run">
    <div class="head"><h2>${escape(r.name)}</h2><span class="status ${r.status}">${STATUS[r.status] || r.status}</span></div>
    <p class="meta">${escape(r.note)}${r.keyword ? `<br>關鍵字：「${escape(r.keyword)}」` : ""}${r.total_ms != null ? `<br>總時間 ${sec(r.total_ms)}，Jev ${sec(r.jev_ms)}，LLM ${sec(r.llm_ms)}，費用 ${usd(r.cost_usd)}` : ""}</p>
    ${r.error ? `<p class="error">${escape(zhError(r.error))}</p>` : ""}
    <table class="grid"><tr><th>步驟</th><th>誰做</th><th class="num">耗時</th><th class="num">Jev</th><th class="num">LLM</th><th class="num">費用</th></tr>${steps}</table>
    ${r.screenshot ? `<img class="shot" alt="${escape(r.name)} 的搜尋結果頁" src="data:image/jpeg;base64,${r.screenshot}">` : ""}
    ${items}
    ${decisions}
    ${candidates}
    ${r.report_md ? `<div class="report">${markdown(r.report_md)}</div>` : ""}
  </article>`;
}

function render() {
  const running = ACTIVE.has(state.phase);
  $("start").disabled = running;
  $("stop").hidden = !running;
  $("phase").textContent = PHASE[state.phase] || "";
  const runs = state.runs || [];
  $("summary-card").hidden = !runs.length;
  if (runs.length) renderSummary(runs);
  const open = new Set([...document.querySelectorAll("details[data-key]")].filter((d) => d.open).map((d) => d.dataset.key));
  $("runs").innerHTML = runs.map(renderRun).join("");
  document.querySelectorAll("details[data-key]").forEach((d) => (d.open = open.has(d.dataset.key)));
}

async function refresh() {
  try {
    state = await fetch("/api/compare/state").then((r) => r.json());
    render();
    if (!ACTIVE.has(state.phase)) setPolling(false);
  } catch {
    showError("無法連線到本機伺服器，請確認它還在執行。");
  }
}
function setPolling(on) {
  clearInterval(timer);
  timer = on ? setInterval(refresh, 1000) : null;
}
function showError(text) {
  $("error").textContent = text;
  $("error").hidden = !text;
}
async function post(name, body) {
  const response = await fetch(`/api/compare/${name}`, { method: "POST", headers, body: JSON.stringify(body || {}) });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "請求失敗");
  state = data;
  render();
}

$("modes").innerHTML = MODES.map(([k, n]) => `<label><input type="checkbox" value="${k}" checked> ${n}</label>`).join("");
$("task").value = DEFAULT_TASK;
renderMatrix();
$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const modes = [...document.querySelectorAll("#modes input:checked")].map((i) => i.value);
  try {
    await post("start", { task_zh: $("task").value, site: $("site").value, max_items: Number($("max-items").value), modes });
    setPolling(true);
  } catch (error) {
    showError(zhError(error.message));
  }
});
$("stop").addEventListener("click", async () => {
  try {
    await post("stop");
  } catch (error) {
    showError(error.message);
  }
});
refresh().then(() => {
  if (ACTIVE.has(state.phase)) setPolling(true);
});
