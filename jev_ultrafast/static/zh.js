const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false,
  lastUsage = null,
  usageTimer = null;
const RATE_TWD = 32; // 粗略換算，僅供參考
const SITE_NAMES = {
  momo: "momo 購物網",
  pchome: "PChome 24h",
  coupang: "酷澎",
  google: "Google 搜尋",
  flights: "Google 航班",
  wikipedia: "維基百科",
  travel: "旅遊範例頁",
  research: "閱讀室範例頁",
  url: "自訂網址",
};
const SITE_GOALS = {
  momo: "在 momo 購物網搜尋「水凝尿布」，看到商品搜尋結果就停止。不要加入購物車或購買。",
  pchome: "在 PChome 24h 搜尋「水凝尿布」，看到商品搜尋結果就停止。不要加入購物車或購買。",
  coupang: "在酷澎搜尋「水凝尿布」，看到商品搜尋結果就停止。不要加入購物車或購買。",
  google: "在 Google 搜尋「水凝尿布 比價」，看到搜尋結果就停止。",
  flights: "搜尋 2026 年 12 月 10 日從台北到東京的單程經濟艙機票，一位成人。看到航班列表就停止，不要選擇或訂票。",
  wikipedia: "在維基百科找到並打開「哥德爾不完備定理」的條目。",
  travel: "找一間位於里斯本、風格為 Design、可免費取消的住宿，並打開 Casa Flora。",
  research: "打開那篇關於用有限選項控制瀏覽器代理的文章。",
  url: "",
};
const STATUS = {
  idle: "準備就緒",
  ready: "已觀察頁面 · 等待決策",
  predicted: "已選好下一步 · 可檢視或執行",
  done: "Jev 回報完成 · 請檢查頁面結果",
  blocked: "已停止 · 沒有可行的下一步",
};
const SHORT_STATUS = { idle: "—", ready: "進行", predicted: "進行", done: "完成", blocked: "停止" };
const OPS = {
  CLICK: "點擊",
  TYPE_TEXT: "輸入文字",
  SELECT: "選擇選項",
  SCROLL_UP: "向上捲動",
  SCROLL_DOWN: "向下捲動",
  WAIT: "等待",
  DONE: "完成",
  BLOCKED: "無法繼續",
};
const ROLES = {
  button: "按鈕",
  link: "連結",
  checkbox: "核取方塊",
  radio: "單選鈕",
  switch: "開關",
  tab: "分頁標籤",
  menuitem: "選單項目",
  menuitemradio: "選單單選",
  option: "選項",
  gridcell: "儲存格",
  combobox: "下拉輸入框",
  textbox: "文字欄位",
  searchbox: "搜尋框",
  spinbutton: "數字欄位",
  select: "下拉選單",
  textarea: "多行文字欄",
  input: "輸入欄位",
};
// 核心程式回傳的英文錯誤訊息，在介面上改成中文。
const ERRORS = [
  [/Start a demo first|請先開始一個任務/, "請先開始一個任務。"],
  [/Enter 1.2,000 characters/, "請輸入 1 至 2,000 字的任務。"],
  [/Unknown demo scenario/, "不支援的起始網站。"],
  [/A browser step is already running/, "上一個步驟還在執行中，請稍候再試。"],
  [/Local demo requests only/, "這個分頁的連線已失效，請重新整理頁面。"],
  [/This run has stopped/, "這個任務已經結束，請重新開始一個任務。"],
  [/model-call budget/, "已達到這次任務的決策次數上限。"],
  [/action demo budget/, "已達到這次任務的動作次數上限。"],
  [/Observe and choose before acting/, "請先觀察頁面並選好下一步，再執行。"],
  [/Page changed since the decision|Page changed before text generation/, "頁面在決策後已改變，請重新選擇下一步。"],
  [/Text helper returned no valid field value/, "文字模型沒有產生有效的欄位內容，這一步沒有輸入任何文字。"],
  [/TYPE_TEXT needs TEXT_MODEL_API_KEY/, "輸入文字需要文字模型金鑰，請在設定檔填入。"],
  [/Model provider returned HTTP (\d+)/, "模型服務回應錯誤（代碼 $1），這一步沒有執行。"],
  [/Model connection failed/, "無法連線到模型服務，這一步沒有執行。"],
  [/Model unavailable/, "模型服務暫時無法使用。"],
  [/Invalid TypeSafe response/, "Jev 回傳的內容格式不正確，這一步沒有執行。"],
  [/Local demo failed/, "本機伺服器發生錯誤，不會自動重試，請重新開始任務。"],
  [/Invalid request size/, "請求內容過大。"],
  [/Failed to fetch|NetworkError/, "無法連線到本機伺服器，請確認它還在執行。"],
  [/No target with given id/, "任務用的瀏覽器分頁已被關閉或失效，請重新按「開始任務」。"],
  [/'code': -32\d+|"code": -32\d+/, "瀏覽器連線錯誤，任務分頁可能已關閉或 Chrome 已重啟，請重新開始任務。"],
  [/daemon|remote debugging|CDP/i, "連不上 Chrome。請確認 Chrome 已開啟並允許遠端偵錯，必要時執行 uv run browser-harness --doctor。"],
];
const zhError = (message) => {
  let text = String(message || "發生未知錯誤");
  for (const [pattern, replacement] of ERRORS) {
    if (pattern.test(text)) {
      const zh = text.replace(pattern, replacement);
      const providerDetail = text.match(/Provider error: (.+)$/);
      return replacement.includes("$1") ? zh : replacement + (providerDetail ? `（供應商回應：${providerDetail[1]}）` : "");
    }
  }
  return text;
};
const op = (name) => OPS[name] || name;
const role = (name) => ROLES[name] || name;
const modelName = (id) =>
  String(id || "")
    .split("/")
    .pop()
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
const num = (value) => (value == null ? "—" : Number(value).toLocaleString("zh-Hant-TW"));
const usd = (value) =>
  value == null || Number.isNaN(Number(value)) ? "—" : `${Number(value).toFixed(Number(value) < 0.01 ? 5 : 3)} 美元`;
const twd = (value) => {
  if (value == null || Number.isNaN(Number(value))) return "";
  const amount = Number(value) * RATE_TWD;
  return `（約 ${amount.toFixed(amount < 1 ? 2 : 0)} 元台幣）`;
};
const headers = { "Content-Type": "application/json", "X-Demo-Token": token };

async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, { method: "POST", headers, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "請求失敗");
  state = data;
  render();
  scheduleUsage();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  for (const id of ["start", "site", "url", "goal-zh", "goal-en", "translate", "raw"]) $(id).disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* 保留原本的錯誤訊息 */
    }
    $("error").textContent = zhError(error.message);
    $("error").hidden = false;
    $("status").textContent = "已暫停 · 需要你處理";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("helper").textContent = `文字模型：${modelName(state.text_model)}`;
  $("plan").innerHTML = state.goal
    ? `<div class="plan-step current"><span>任務</span>${escape(state.goal_zh || state.goal)}</div>` +
      (state.goal_zh && state.goal_zh !== state.goal
        ? `<div class="plan-step"><span>指令</span>${escape(state.goal)}</div>`
        : "")
    : "";
  const page = state.page,
    d = state.decision || (state.status === "done" ? state.decisions?.at(-1) : null);
  $("status").textContent = STATUS[state.status] || state.status;
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url-bar").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} 個元素`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d ? chosen?.label || op(d.choice) : "選一個動作";
  $("latency").textContent = d ? `${d.latency_ms} 毫秒` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? op(d.operation) : "—";
  $("ranking-note").textContent = d ? "已由 Jev 排序" : "尚未排序";
  const ops = Object.entries(d?.operation_probabilities || {}).sort((a, b) => b[1] - a[1]);
  $("operation-choices").innerHTML = ops
    .map(([name, p]) => `<span class="operation-choice ${name === d.operation ? "best" : ""}">${escape(op(name))} <b>${percent(p)}</b></span>`)
    .join("");
  const probability = (e) =>
    d?.target_probabilities[e.index] ?? Math.max(-1, ...(e.options || []).map((o) => d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(":")[0];
  const elements = [...state.elements];
  if (d) elements.sort((a, b) => probability(b) - probability(a));
  $("choices").innerHTML = elements
    .map((e) => {
      const p = probability(e);
      const operations = e.operations.map(op).join(" / ");
      const checked = e.checked === undefined ? "" : e.checked === "true" || e.checked === true ? " · 已勾選" : " · 未勾選";
      return `<div class="choice ${selectedIndex === e.index ? "best" : ""}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(role(e.role))} · ${escape(operations)}${e.value ? " · " + escape(e.value) : ""}${checked}</small>${p >= 0 ? `<div class="bar" style="--probability:${p * 100}%"></div>` : ""}</div><span class="probability">${p >= 0 ? percent(p) : "—"}</span></div>`;
    })
    .join("");
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()]
    .map((a, i) => {
      const index = String(i + 1);
      return `<div class="target ${index === selectedIndex ? "selected" : ""}" data-action="${index}" style="left:${(100 * a.rect.x) / page.w}%;top:${(100 * a.rect.y) / page.h}%;width:${(100 * a.rect.w) / page.w}%;height:${(100 * a.rect.h) / page.h}%"><span>${index}</span></div>`;
    })
    .join("");
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(op(h.operation))} · ${escape(h.action)}${h.text ? ` <b>「${escape(h.text)}」</b><small>由文字模型產生</small>` : ""}</div><span class="time">${h.latency_ms} 毫秒 · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "頁面已改變" : "未觀察到變化"}</span></div>`,
        )
        .join("")
    : '<p class="muted">每個執行過的動作都會留下觀察到的結果。</p>';
  $("step-count").textContent = `${state.history.length} 個動作 · ${(state.elapsed_ms / 1000).toFixed(2)} 秒`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}

/* ---------- 用量與額度 ---------- */
function scheduleUsage(delay = 1500) {
  clearTimeout(usageTimer);
  usageTimer = setTimeout(() => refreshUsage(false), delay);
}
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
  const c = u.current,
    a = u.all_time || {},
    o = u.openrouter || {};
  $("u-task-jev").textContent = c ? `${num(c.jev_requests)} 次 · ${num(c.jev_input_tokens)} 詞元` : "—";
  $("u-task-jev-cost").textContent = c ? `${usd(c.jev_cost_usd)} ${twd(c.jev_cost_usd)}` : "—";
  $("u-task-helper").textContent = c ? `${num(c.helper_calls)} 次 · ${num(c.helper_input_tokens + c.helper_output_tokens)} 詞元` : "—";
  $("u-task-helper-cost").textContent = c ? `${usd(c.helper_cost_usd)} ${twd(c.helper_cost_usd)}` : "—";
  $("u-task-actions").textContent = c ? `${num(c.actions)} 個 · ${(c.elapsed_ms / 1000).toFixed(1)} 秒` : "—";
  $("u-jev-requests").textContent = `${num(a.jev_requests)} 次（${num(a.tasks)} 個任務）`;
  $("u-jev-tokens").textContent = `輸入 ${num(a.jev_input_tokens)} · 輸出 ${num(a.jev_output_tokens)}（輸出免費）`;
  $("u-jev-cost").textContent = `${usd(a.jev_cost_usd)} ${twd(a.jev_cost_usd)}`;
  if (!$("prepaid").value && (localStorage.getItem("jevPrepaidUsd") || u.jev.prepaid_usd)) {
    $("prepaid").value = localStorage.getItem("jevPrepaidUsd") || u.jev.prepaid_usd;
  }
  const prepaid = parseFloat($("prepaid").value);
  const remaining = prepaid - (a.jev_cost_usd || 0);
  $("u-jev-remaining").textContent = prepaid > 0 ? `${usd(remaining)} ${twd(remaining)}` : "請輸入預付金額";
  $("u-jev-price").textContent = `Jev 每百萬輸入詞元 ${usd(u.jev.usd_per_mtok)}，輸出免費`;
  if (o.error) {
    $("u-or-account").textContent = o.error;
    $("u-or-account-used").textContent = "—";
    $("u-or-key").textContent = "—";
    $("u-or-key-used").textContent = "—";
  } else {
    $("u-or-account").textContent = `剩餘 ${usd(o.account_remaining_usd)} / 總額 ${usd(o.account_total_usd)}`;
    $("u-or-account-used").textContent = `${usd(o.account_used_usd)} ${twd(o.account_used_usd)}`;
    $("u-or-key").textContent = o.key_limit_usd != null ? `剩餘 ${usd(o.key_remaining_usd)} / 上限 ${usd(o.key_limit_usd)}` : "此金鑰未設上限";
    $("u-or-key-used").textContent = `${usd(o.key_used_usd)} · 今日 ${usd(o.key_usage_daily_usd)} · 本月 ${usd(o.key_usage_monthly_usd)}`;
  }
  $("u-or-local").textContent = `本工具累計：文字模型 ${num(a.helper_calls)} 次 ${usd(a.helper_cost_usd)} · 改寫指令 ${num(a.translate_calls)} 次 ${usd(a.translate_cost_usd)}`;
  const at = o.fetched_at ? new Date(o.fetched_at * 1000).toLocaleTimeString("zh-Hant-TW") : new Date().toLocaleTimeString("zh-Hant-TW");
  $("usage-note").textContent = `餘額更新於 ${at} · 文字模型 ${modelName(u.models.text)} · 改寫用 ${modelName(u.models.translate)}`;
  $("recent").innerHTML = (u.recent || []).length
    ? u.recent
        .map(
          (t) =>
            `<div class="trace-row"><span class="number">${escape(SHORT_STATUS[t.status] || t.status || "")}</span><div>${escape(t.goal_zh || t.goal || "")}<small>${t.started_at_epoch ? new Date(t.started_at_epoch * 1000).toLocaleString("zh-Hant-TW") : ""} · ${escape(SITE_NAMES[t.scenario] || t.scenario || "")}</small></div><span class="time">Jev ${num(t.jev_requests)} 次 · ${num(t.jev_input_tokens)} 詞元</span><span class="effect">${usd((t.jev_cost_usd || 0) + (t.helper_cost_usd || 0))}</span></div>`,
        )
        .join("")
    : '<p class="muted">還沒有任務紀錄。</p>';
}

/* ---------- 改寫指令 ---------- */
async function translate() {
  const goal_zh = $("goal-zh").value.trim();
  if (!goal_zh) throw Error("請先輸入中文任務");
  $("translate-note").textContent = "正在改寫成給 Jev 的指令…";
  const response = await fetch("/api/translate", { method: "POST", headers, body: JSON.stringify({ goal_zh }) });
  const data = await response.json();
  if (!response.ok) {
    $("translate-note").textContent = "改寫失敗";
    throw Error(data.error || "改寫失敗");
  }
  $("goal-en").value = data.goal_en;
  $("translate-note").textContent = `已改寫 · ${modelName(data.model)} · ${data.latency_ms} 毫秒 · ${usd(data.cost_usd)}`;
  scheduleUsage(500);
  return data.goal_en;
}

/* ---------- 事件 ---------- */
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(async () => {
    const goal_zh = $("goal-zh").value.trim();
    const raw = $("raw").checked;
    let goal = raw ? goal_zh : $("goal-en").value.trim();
    if (!goal && !raw) goal = await translate();
    if (!goal) throw Error("請先輸入任務");
    await call("reset", { scenario: $("site").value, url: $("url").value.trim(), goal, goal_zh });
  }, "正在開啟新的瀏覽器分頁…");
});
$("translate").addEventListener("click", () => perform(translate, "改寫中…"));
$("site").addEventListener("change", () => {
  const site = $("site").value;
  $("url-row").hidden = site !== "url";
  $("goal-zh").value = SITE_GOALS[site] ?? "";
  $("goal-en").value = "";
  $("translate-note").textContent = "Jev 以英文訓練為主，改寫成英文指令後成功率較高；關鍵字會保留中文。";
});
$("goal-zh").addEventListener("input", () => {
  $("goal-en").value = "";
});
$("choose").addEventListener("click", () => perform(() => call("predict"), "Jev 正在比較可用的動作…"));
$("execute").addEventListener("click", () =>
  perform(() => call("act", { fingerprint: state.page.fingerprint }), "正在執行選擇…"),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    controls();
    for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
      $("status").textContent = "執行中…";
      if ($("pace").checked) {
        await call("predict");
        await new Promise((resolve) => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", { fingerprint: state.page.fingerprint });
      } else {
        await call("tick");
      }
      if (["done", "blocked"].includes(state.status)) break;
    }
    automatic = false;
  }, "瀏覽器執行中…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "完成目前這一步後暫停…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle("selected", t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(":")[0]),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) => t.classList.toggle("selected", t.dataset.action === state?.decision?.target?.split(":")[0])),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob([JSON.stringify({ ...rest, page: { ...page, screenshot: undefined } }, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "jev-任務紀錄.json";
  a.click();
  URL.revokeObjectURL(url);
});
$("usage-refresh").addEventListener("click", () => refreshUsage(true));
$("prepaid").addEventListener("input", () => {
  localStorage.setItem("jevPrepaidUsd", $("prepaid").value);
  if (lastUsage) renderUsage(lastUsage);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshUsage(false);
});
setInterval(() => {
  if (!document.hidden) refreshUsage(false);
}, 20000);

$("goal-zh").value = SITE_GOALS[$("site").value];
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "無法連線到本機伺服器";
  });
refreshUsage(false);
