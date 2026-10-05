// 共用小工具（原型，不用任何框架）
const IMPACT_ZH = { high: "高", medium: "中", low: "低" };
const FIELD_ZH = {
  coverage: "給付項目", exclusions: "除外責任", payment_modes: "繳費方式", currency: "幣別",
  issue_age: "投保年齡", rate_terms: "宣告／預定利率", riders: "附約",
};

const CUR_ZH = { TWD: "新臺幣", FX: "外幣", USD: "美元", AUD: "澳幣", CNY: "人民幣", EUR: "歐元", JPY: "日圓" };
const curLabel = c => (c || "").split("/").map(x => CUR_ZH[x] || x).join("、");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null && v !== false) p.set(k, v);
  return p.toString();
}

function option(value, text, selected) {
  return `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(text)}</option>`;
}

// 標出關鍵字（先 escape 再加 <mark>）
function highlight(text, kw) {
  const t = esc(text);
  if (!kw) return t;
  return t.split(esc(kw)).join(`<mark>${esc(kw)}</mark>`);
}

async function renderNav(here) {
  const nav = document.getElementById("nav");
  nav.innerHTML = `<a href="/" class="${here === "wall" ? "here" : ""}">情報牆</a>`
    + `<a href="/compare.html" class="${here === "compare" ? "here" : ""}">競品比較</a>`
    + `<span class="db" id="dbinfo"></span>`;
  const m = await getJSON("/api/meta");
  document.getElementById("dbinfo").textContent =
    `資料庫：${m.db}｜事件 ${m.events}（未處理 ${m.pending}）`;
  if (!m.parsed) {
    nav.insertAdjacentHTML("afterend",
      `<div class="banner">這個資料庫還沒解析。先在終端機跑 <code>python -m scheduler.run --parse</code>，再重新整理。</div>`);
  }
  return m;
}
