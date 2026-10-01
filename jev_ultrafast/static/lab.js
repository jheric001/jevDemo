const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const headers = { "Content-Type": "application/json", "X-Demo-Token": token };
const TYPE_NAME = { choice: "Choice", noul: "Noul", score: "Score" };
let items = [];
let edited = false;
let current = null;

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const fmt = (x) => Number(x).toFixed(2);
const confidenceText = (c) =>
  `信心 ${fmt(c)}（${c < 0.5 ? "低：建議交給人判斷" : c <= 0.9 ? "中：可以做，高風險要再確認" : "高"}）`;

function optionRow(name, desc, p, chosen, mid = false) {
  const row = el("div", "opt" + (chosen ? " chosen" : ""));
  const label = el("div", "opt-name", name);
  if (desc) label.append(el("small", "", desc));
  const track = el("div", "track" + (mid ? " mid" : ""));
  const fill = el("span");
  fill.style.width = `${Math.round(p * 100)}%`;
  track.append(fill);
  row.append(label, track, el("div", "opt-val", fmt(p)));
  return row;
}

function answerBlock(qid, question, answer) {
  const block = el("div", "q");
  const head = el("div", "q-head");
  head.append(el("span", "q-id", qid), el("span", "q-type", TYPE_NAME[question.type] || question.type));
  head.append(el("span", "q-ins", typeof question.instructions === "string" ? question.instructions : JSON.stringify(question.instructions)));
  block.append(head);
  if (!answer) {
    block.append(el("p", "error", "這題沒有回傳答案"));
    return block;
  }
  const sum = el("p", "q-sum");
  if (answer.type === "noul") {
    const v = answer.noul;
    sum.append(el("b", "", fmt(v)), ` · 以 0.5 為門檻判定為「${v >= 0.5 ? "是" : "否"}」`);
    if (Math.abs(v - 0.5) < 0.1) sum.append(el("span", "hint", "（接近 0.5，模型不確定）"));
    block.append(sum, optionRow("是的機率", "", v, v >= 0.5, true));
  } else if (answer.type === "choice") {
    sum.append("選 ", el("b", "", answer.choice), ` · ${confidenceText(answer.confidence)}`);
    block.append(sum);
    const criteria = question.criteria || {};
    // Jev returns options in varying order; follow the question's own order so runs line up side by side.
    const keys = Object.keys(criteria).filter((k) => k in answer.probabilities);
    for (const key of keys.length ? keys : Object.keys(answer.probabilities)) {
      const desc = typeof criteria[key] === "string" ? criteria[key] : "";
      block.append(optionRow(key, desc, answer.probabilities[key], key === answer.choice));
    }
  } else if (answer.type === "score") {
    const levels = Object.keys(answer.probabilities).sort((a, b) => a - b);
    const top = levels.reduce((a, b) => (answer.probabilities[b] > answer.probabilities[a] ? b : a));
    sum.append("分數 ", el("b", "", fmt(answer.score)), `（0 到 ${levels[levels.length - 1]}）· ${confidenceText(answer.confidence)}`);
    block.append(sum);
    for (const level of levels) {
      const desc = answer.legend?.[level];
      block.append(optionRow(`等級 ${level}`, typeof desc === "string" ? desc : "", answer.probabilities[level], level === top));
    }
  } else {
    block.append(el("pre", "hint", JSON.stringify(answer, null, 2)));
  }
  return block;
}

function renderPanel(node, run, tag) {
  node.replaceChildren();
  if (!run) {
    node.hidden = true;
    return;
  }
  const head = el("div", "head");
  head.append(el("h2", "", run.label), el("span", "tag", tag));
  const { data } = run;
  const meta = `${data.model || "?"} · ${(data.elapsed_ms / 1000).toFixed(2)} 秒 · ${data.usage?.input_tokens ?? "?"} tokens · 約 US$${data.cost_usd.toFixed(5)}`;
  node.append(head, el("p", "meta", meta), el("p", "state", run.state));
  for (const [qid, question] of Object.entries(run.questions)) node.append(answerBlock(qid, question, data.answers[qid]));
  node.hidden = false;
}

function showError(message) {
  $("error").textContent = message || "";
  $("error").hidden = !message;
}

function applyBank(value) {
  showError("");
  if (!value) {
    $("focus").hidden = true;
    return;
  }
  const [index, variant] = value.split(":").map(Number);
  const item = items[index];
  $("state").value = variant < 0 ? item.state : item.variants[variant].state;
  $("questions").value = JSON.stringify(item.questions, null, 2);
  $("focus").textContent = `${item.id} 示範重點：${item.focus}`;
  $("focus").hidden = false;
  edited = false;
}

async function run(event) {
  event.preventDefault();
  showError("");
  let questions;
  try {
    questions = JSON.parse($("questions").value);
  } catch (error) {
    return showError(`問題 JSON 格式錯誤：${error.message}`);
  }
  const state = $("state").value.trim();
  const bank = $("bank");
  const label = (bank.value ? bank.options[bank.selectedIndex].textContent : "自訂") + (edited ? "（已修改）" : "");
  $("run").disabled = true;
  $("run").textContent = "執行中…";
  try {
    const response = await fetch("/api/lab/ask", { method: "POST", headers, body: JSON.stringify({ state, questions }) });
    const data = await response.json().catch(() => ({ error: `本機伺服器回應錯誤（代碼 ${response.status}）` }));
    if (!response.ok) return showError(data.error);
    const previous = current;
    current = { label, state, questions, data };
    renderPanel($("current"), current, "這一次");
    renderPanel($("previous"), previous, "上一次");
  } catch {
    showError("無法連線到本機伺服器，請確認它還在執行。");
  } finally {
    $("run").disabled = false;
    $("run").textContent = "執行";
  }
}

async function init() {
  $("form").addEventListener("submit", run);
  $("bank").addEventListener("change", (e) => applyBank(e.target.value));
  for (const id of ["state", "questions"]) $(id).addEventListener("input", () => (edited = true));
  items = await (await fetch("/lab-items.json")).json();
  const groups = { A: el("optgroup"), B: el("optgroup") };
  groups.A.label = "A 正常題";
  groups.B.label = "B 陷阱題";
  items.forEach((item, i) => {
    const group = groups[item.id[0]] || groups.A;
    group.append(new Option(`${item.id} ${item.title}`, `${i}:-1`));
    item.variants.forEach((v, j) => group.append(new Option(`${item.id} 變化：${v.label}`, `${i}:${j}`)));
  });
  $("bank").append(groups.A, groups.B);
  $("bank").value = "0:-1";
  applyBank("0:-1");
}

init().catch(() => showError("題庫載入失敗，請重新整理頁面。"));
