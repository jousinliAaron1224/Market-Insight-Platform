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

// ---------------------------------------------------------------- 圖示（自繪線條圖示，24×24，跟著文字顏色）
const ICONS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  building: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  coins: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>',
  scale: '<path d="M12 3v18M5 21h14M6 7h12"/><path d="m6 7-3 7a3 3 0 0 0 6 0Z"/><path d="m18 7-3 7a3 3 0 0 0 6 0Z"/>',
  news: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  trend: '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/><path d="m9 12 2 2 4-4"/>',
  alert: '<path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  pin: '<path d="M12 21s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/>',
  bookmark: '<path d="M6 3h12v18l-6-4-6 4Z"/>',
  plus: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
  check: '<path d="m5 12 4 4 10-10"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  tag: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9Z"/><circle cx="8" cy="8" r="1.5"/>',
  hash: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  print: '<path d="M6 9V3h12v6M6 18H4v-7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v7h-2"/><rect x="6" y="14" width="12" height="7"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
};
function icon(name, cls = "") {
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

// ---------------------------------------------------------------- 上方導覽列
const NAV = [["lab", "/", "商品工作台"], ["wall", "/wall.html", "情報牆"], ["compare", "/compare.html", "競品比較"], ["market", "/market.html", "市場數據"]];
async function renderNav(here) {
  const nav = document.getElementById("nav");
  nav.className = "topbar";
  nav.innerHTML = `<div class="topbar-in">
      <a class="brand" href="/"><svg class="logo" viewBox="0 0 28 28" aria-hidden="true"><rect x="2" y="2" width="24" height="24" rx="7" fill="#2fb3a3"/><path d="M8 18.5 12.5 14l3 3L20 10" fill="none" stroke="#0f1f38" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg><span>商品與市場情報平台</span></a>
      <div class="topnav">${NAV.map(([k, href, zh], i) => (i === 1 ? '<span class="topnav-sep">資料來源</span>' : "")
        + `<a href="${href}" class="${here === k ? "here" : ""}">${zh}</a>`).join("")}</div>
      <div class="topright"><span class="dbchip" id="dbinfo" title="">${icon("db")}<span>資料庫</span></span></div>
    </div>`;
  document.querySelectorAll("[data-ic]").forEach(el => el.insertAdjacentHTML("afterbegin", icon(el.dataset.ic)));
  const m = await getJSON("/api/meta");
  const db = document.getElementById("dbinfo");
  db.title = m.db;
  db.querySelector("span").textContent = `事件 ${m.events.toLocaleString()}｜未處理 ${m.pending}`;
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
