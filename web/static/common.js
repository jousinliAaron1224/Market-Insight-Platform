// 共用小工具（原型，不用任何框架）
const IMPACT_ZH = { high: "高", medium: "中", low: "低" };
const FIELD_ZH = {
  coverage: "給付項目", exclusions: "除外責任", payment_modes: "繳費方式", currency: "幣別",
  issue_age: "投保年齡", rate_terms: "宣告／預定利率", riders: "附約",
};

const CUR_ZH = { TWD: "新臺幣", FX: "外幣", USD: "美元", AUD: "澳幣", CNY: "人民幣", EUR: "歐元", JPY: "日圓",
                 GBP: "英鎊", CAD: "加幣", NZD: "紐幣", HKD: "港幣", ZAR: "南非幣" };
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
    + `<a href="/market.html" class="${here === "market" ? "here" : ""}">市場數據</a>`
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

// 簡單的堆疊長條圖（SVG，不靠外部套件，離線可用）
const PALETTE = ["#1f5fa8", "#d1495b", "#edae49", "#00798c", "#66a182", "#8d6a9f", "#999999"];
function stackedBars(rows, keyField, series, opts = {}) {
  const W = opts.width || 900, H = opts.height || 260, pad = { l: opts.padLeft || 36, r: 10, t: 10, b: 40 };
  const fmt = opts.fmt || (v => v), xfmt = opts.xfmt || (v => v);
  const totals = rows.map(r => series.reduce((s, k) => s + (r[k] || 0), 0));
  const max = Math.max(1, ...totals);
  const bw = (W - pad.l - pad.r) / rows.length;
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  let g = "";
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = Math.round(max * i / ticks);
    g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" stroke="#ddd"/>`
      + `<text x="${pad.l - 4}" y="${y(v) + 4}" font-size="11" text-anchor="end" fill="#555">${fmt(v)}</text>`;
  }
  rows.forEach((r, i) => {
    let acc = 0;
    const x = pad.l + i * bw + bw * 0.15, w = bw * 0.7;
    series.forEach((k, j) => {
      const v = r[k] || 0;
      if (!v) return;
      g += `<rect x="${x}" y="${y(acc + v)}" width="${w}" height="${y(acc) - y(acc + v)}" fill="${PALETTE[j % PALETTE.length]}"><title>${esc(r[keyField])} ${esc(k)}：${fmt(v)}</title></rect>`;
      acc += v;
    });
    if (totals[i] && opts.totals !== false)
      g += `<text x="${x + w / 2}" y="${y(totals[i]) - 3}" font-size="11" text-anchor="middle">${fmt(totals[i])}</text>`;
    g += `<text x="${x + w / 2}" y="${H - pad.b + 16}" font-size="11" text-anchor="middle">${esc(xfmt(r[keyField], i))}</text>`;
  });
  const legend = series.map((k, j) => `<span class="small" style="margin-right:12px"><span style="display:inline-block;width:10px;height:10px;background:${PALETTE[j % PALETTE.length]}"></span> ${esc(k)}</span>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">${g}</svg><div>${legend}</div>`;
}

const num = (v, d = 0) => v == null ? "—" : Number(v).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });
const signed = v => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%";
