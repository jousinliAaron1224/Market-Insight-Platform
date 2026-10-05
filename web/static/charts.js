// 小型 SVG 圖表（原型用，不依賴外部套件、離線可用）
// 規則：細長條（≤24px）、4px 圓角資料端、2px 表面間隙、髮絲格線；顏色依序取 --s1…--s5，不循環；
// 每張圖都有滑鼠／鍵盤提示（值在前、名稱在後），也都在 <details> 裡附表格，提示只是加強、不是唯一管道。

const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"];
const DEEMPH = "var(--deemph)";
const INK_ON = ["#fff", "#0b0b0b", "#0b0b0b", "#0b0b0b", "#0b0b0b"];   // 色塊內文字：藍底白字，其他黑字

// ---------------------------------------------------------------- 提示框
const Tip = (() => {
  const store = new Map();
  let el = null;
  const box = () => {
    if (!el) {
      el = document.createElement("div");
      el.className = "tip";
      el.setAttribute("role", "tooltip");
      document.body.appendChild(el);
    }
    return el;
  };
  function show(id, x, y) {
    const t = store.get(id);
    if (!t) return hide();
    const e = box();
    e.replaceChildren();
    const h = document.createElement("div");
    h.className = "tip-h";
    h.textContent = t.head;
    e.appendChild(h);
    for (const r of t.rows) {
      const row = document.createElement("div");
      row.className = "tip-r";
      const k = document.createElement("span");
      k.className = "tip-k";
      if (r.color) k.style.background = r.color;
      const v = document.createElement("b");
      v.textContent = r.value;
      const l = document.createElement("span");
      l.className = "tip-l";
      l.textContent = r.label;
      row.append(k, v, l);
      e.appendChild(row);
    }
    e.style.display = "block";
    const w = e.offsetWidth, hh = e.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top + hh > window.innerHeight - 8) top = y - hh - 14;
    e.style.left = Math.max(8, left) + "px";
    e.style.top = Math.max(8, top) + "px";
  }
  function hide() { if (el) el.style.display = "none"; }
  document.addEventListener("pointermove", ev => {
    const m = ev.target.closest && ev.target.closest("[data-tip]");
    if (m) show(m.dataset.tip, ev.clientX, ev.clientY); else hide();
  });
  document.addEventListener("focusin", ev => {
    const m = ev.target.closest && ev.target.closest("[data-tip]");
    if (!m) return hide();
    const r = m.getBoundingClientRect();
    show(m.dataset.tip, r.right, r.top);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
  return { set: (id, t) => { store.set(id, t); return id; } };
})();

// ---------------------------------------------------------------- 共用
const Charts = [];
function chart(box, draw) {           // 依容器寬度畫，視窗變寬窄時重畫
  const run = () => { box.innerHTML = draw(Math.max(280, box.clientWidth)); };
  Charts.push(run);
  run();
}
let _rz;
window.addEventListener("resize", () => { clearTimeout(_rz); _rz = setTimeout(() => Charts.forEach(f => f()), 150); });

function niceTicks(max, n = 4) {
  if (max <= 0) return [0, 1];
  const raw = max / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw);
  const out = [];
  for (let v = 0; v <= max + step * 0.001; v += step) out.push(+v.toFixed(6));
  if (out[out.length - 1] < max) out.push(+(out[out.length - 1] + step).toFixed(6));
  return out;
}

// 上方圓角（資料端）、底部方角的長條
function colPath(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}
// 右側圓角的橫條（負值時左側圓角）
function barPath(x, y, w, h, r = 4, left = false) {
  if (w <= 0) return "";
  r = Math.min(r, h / 2, w);
  return left
    ? `M${x + w},${y}H${x + r}Q${x},${y} ${x},${y + r}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w}Z`
    : `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
}

function legend(items, shape = "rect") {
  return `<div class="legend">${items.map(([name, color, s]) => `<span class="lg"><span class="lg-k ${s || shape}" style="background:${color}"></span>${esc(name)}</span>`).join("")}</div>`;
}

// ---------------------------------------------------------------- 直條（可堆疊）
// rows: [{...}]；series: 欄位名稱；opts: {id, key, colors, fmt, xfmt, height, totals:'all'|'last'|'none', tip(row)}
function columns(box, rows, series, opts) {
  const colors = opts.colors || SERIES, fmt = opts.fmt || (v => v), xfmt = opts.xfmt || (v => v);
  chart(box, W => {
    const H = opts.height || 240, pad = { l: opts.padLeft || 44, r: 8, t: 18, b: 26 };
    const totals = rows.map(r => series.reduce((s, k) => s + (r[k] || 0), 0));
    const ticks = niceTicks(Math.max(...totals, 1));
    const max = ticks[ticks.length - 1];
    const ph = H - pad.t - pad.b, band = (W - pad.l - pad.r) / rows.length;
    const bw = Math.min(24, band * 0.7);
    const y = v => pad.t + ph * (1 - v / max);
    let g = "";
    const labels = [];
    for (const t of ticks) {
      g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}" class="${t ? "grid" : "base"}"/>`
        + `<text x="${pad.l - 6}" y="${y(t) + 4}" class="tick" text-anchor="end">${esc(fmt(t))}</text>`;
    }
    rows.forEach((r, i) => {
      const cx = pad.l + band * i + band / 2, x = cx - bw / 2;
      let acc = 0;
      const segs = series.map((k, j) => ({ v: r[k] || 0, j })).filter(s => s.v > 0);
      segs.forEach((s, n) => {
        const y0 = y(acc), y1 = y(acc + s.v);
        const top = n === segs.length - 1;
        const h = Math.max(0, y0 - y1 - (n > 0 ? 2 : 0));   // 2px 表面間隙
        g += top ? `<path d="${colPath(x, y1, bw, h)}" fill="${colors[s.j]}"/>`
                 : `<rect x="${x}" y="${y1}" width="${bw}" height="${h}" fill="${colors[s.j]}"/>`;
        acc += s.v;
      });
      const showTotal = opts.totals === "all" || (opts.totals === "last" && i === rows.length - 1);
      if (showTotal && totals[i]) g += `<text x="${cx}" y="${y(totals[i]) - 5}" class="val" text-anchor="middle">${esc(fmt(totals[i]))}</text>`;
      const lab = xfmt(r[opts.key], i);
      if (lab) labels.push({ cx, lab });
      const tip = Tip.set(`${opts.id}:${i}`, opts.tip ? opts.tip(r, totals[i]) : {
        head: String(r[opts.key]),
        rows: [{ label: "合計", value: fmt(totals[i]) }, ...series.map((k, j) => ({ label: k, value: fmt(r[k] || 0), color: colors[j] })).reverse()],
      });
      g += `<rect class="hit" x="${pad.l + band * i}" y="${pad.t}" width="${band}" height="${ph}" data-tip="${tip}" tabindex="0"/>`;
    });
    // x 軸標籤：從最後一個往前放，彼此不重疊（窄螢幕自動跳著標）
    let lastX = Infinity;
    for (const l of labels.reverse()) {
      const w = l.lab.length * 6.5 + 10;
      if (lastX - l.cx >= w) { g += `<text x="${l.cx}" y="${H - 8}" class="tick" text-anchor="middle">${esc(l.lab)}</text>`; lastX = l.cx; }
    }
    return `<svg width="${W}" height="${H}" role="img">${g}</svg>`;
  });
}

// ---------------------------------------------------------------- 橫條（單一數列，可強調一筆）
// items: [{label, value, color?, text?, tip?}]；opts: {id, fmt, labelW, rowH, signed}
function hbars(box, items, opts) {
  const fmt = opts.fmt || (v => v);
  chart(box, W => {
    const rowH = opts.rowH || 30, lw = Math.min(opts.labelW || 112, W < 520 ? 92 : 999), vw = opts.valueW || 96;
    const H = items.length * rowH + 6;
    const min = Math.min(0, ...items.map(i => i.value)), max = Math.max(0, ...items.map(i => i.value));
    const span = (max - min) || 1, plotW = W - lw - vw;
    const x = v => lw + plotW * (v - min) / span;
    let g = `<line x1="${x(0)}" x2="${x(0)}" y1="0" y2="${H}" class="base"/>`;
    items.forEach((it, i) => {
      const y0 = i * rowH + 3, bh = Math.min(18, rowH - 10), by = y0 + (rowH - bh) / 2;
      const a = x(Math.min(0, it.value)), b = x(Math.max(0, it.value));
      g += `<text x="${lw - 8}" y="${by + bh / 2 + 4}" class="lab${it.strong ? " strong" : ""}" text-anchor="end">${esc(it.label)}</text>`
        + `<path d="${barPath(a, by, b - a, bh, 4, it.value < 0)}" fill="${it.color || SERIES[0]}"/>`
        + `<text x="${(it.value < 0 ? x(0) : b) + 6}" y="${by + bh / 2 + 4}" class="val${it.strong ? " strong" : ""}">${esc(it.text ?? fmt(it.value))}</text>`;
      const tip = Tip.set(`${opts.id}:${i}`, it.tip || { head: it.label, rows: [{ label: opts.unit || "", value: fmt(it.value), color: it.color || SERIES[0] }] });
      g += `<rect class="hit" x="0" y="${y0}" width="${W}" height="${rowH}" data-tip="${tip}" tabindex="0"/>`;
    });
    return `<svg width="${W}" height="${H}" role="img">${g}</svg>`;
  });
}

// ---------------------------------------------------------------- 100% 堆疊橫條（組成）
// items: [{label, parts:{k:v}, note}]；series：組成順序
function stack100(box, items, series, opts) {
  const colors = opts.colors || SERIES;
  chart(box, W => {
    const rowH = 32, lw = Math.min(opts.labelW || 112, W < 520 ? 92 : 999), nw = 64, plotW = W - lw - nw, H = items.length * rowH + 4;
    let g = "";
    items.forEach((it, i) => {
      const tot = series.reduce((s, k) => s + (it.parts[k] || 0), 0) || 1;
      const by = i * rowH + 7, bh = 18;
      g += `<text x="${lw - 8}" y="${by + 13}" class="lab${it.strong ? " strong" : ""}" text-anchor="end">${esc(it.label)}</text>`;
      let acc = 0;
      series.forEach((k, j) => {
        const v = it.parts[k] || 0;
        if (!v) return;
        const x0 = lw + plotW * acc / tot, w = plotW * v / tot - 2;
        g += `<rect x="${x0}" y="${by}" width="${Math.max(0, w)}" height="${bh}" fill="${colors[j]}"/>`;
        const pctTxt = Math.round(v * 100 / tot) + "%";
        if (w > pctTxt.length * 7 + 10) g += `<text x="${x0 + w / 2}" y="${by + 13}" class="in" fill="${(opts.ink || INK_ON)[j]}" text-anchor="middle">${pctTxt}</text>`;
        acc += v;
      });
      g += `<text x="${lw + plotW + 8}" y="${by + 13}" class="val">${esc(it.note || "")}</text>`;
      const tip = Tip.set(`${opts.id}:${i}`, { head: it.label, rows: series.filter(k => it.parts[k]).map(k => ({
        label: k, value: `${it.parts[k]}（${Math.round(it.parts[k] * 100 / tot)}%）`, color: colors[series.indexOf(k)] })) });
      g += `<rect class="hit" x="0" y="${i * rowH}" width="${W}" height="${rowH}" data-tip="${tip}" tabindex="0"/>`;
    });
    return `<svg width="${W}" height="${H}" role="img">${g}</svg>`;
  });
}

// ---------------------------------------------------------------- 啞鈴圖（前→後）
// items: [{label, a, b, strong}]；opts: {id, fmt, aName, bName, max}
function dumbbell(box, items, opts) {
  const fmt = opts.fmt || (v => v);
  chart(box, W => {
    const narrow = W < 520;
    const rowH = 30, lw = Math.min(opts.labelW || 112, narrow ? 84 : 999), nw = narrow ? 96 : 116, top = 22, plotW = W - lw - nw - 10;
    const H = items.length * rowH + top + 4;
    const ticks = niceTicks(opts.max || Math.max(...items.flatMap(i => [i.a, i.b])), narrow ? 2 : 4);
    const max = ticks[ticks.length - 1];
    const x = v => lw + plotW * v / max;
    let g = "";
    for (const t of ticks) g += `<line x1="${x(t)}" x2="${x(t)}" y1="${top - 4}" y2="${H}" class="${t ? "grid" : "base"}"/>`
      + `<text x="${x(t)}" y="${top - 8}" class="tick" text-anchor="middle">${esc((opts.tickFmt || fmt)(t))}</text>`;
    const col = opts.color || SERIES[0];
    items.forEach((it, i) => {
      const cy = top + i * rowH + rowH / 2;
      g += `<text x="${lw - 8}" y="${cy + 4}" class="lab${it.strong ? " strong" : ""}" text-anchor="end">${esc(it.label)}</text>`
        + `<line x1="${x(it.a)}" x2="${x(it.b)}" y1="${cy}" y2="${cy}" class="conn"/>`
        + `<circle cx="${x(it.a)}" cy="${cy}" r="5" class="dot-a"/>`
        + `<circle cx="${x(it.b)}" cy="${cy}" r="5.5" fill="${it.color || col}" class="dot-b"/>`
        + `<text x="${lw + plotW + 14}" y="${cy + 4}" class="val${it.strong ? " strong" : ""}">${esc(fmt(it.a))} → ${esc(fmt(it.b))}</text>`;
      const tip = Tip.set(`${opts.id}:${i}`, { head: it.label, rows: [
        { label: opts.bName, value: fmt(it.b), color: it.color || col }, { label: opts.aName, value: fmt(it.a), color: "var(--deemph)" }] });
      g += `<rect class="hit" x="0" y="${cy - rowH / 2}" width="${W}" height="${rowH}" data-tip="${tip}" tabindex="0"/>`;
    });
    return `<svg width="${W}" height="${H}" role="img">${g}</svg>`
      + legend([[opts.aName, "transparent", "ring"], [opts.bName, col, "dot"]]);
  });
}

// ---------------------------------------------------------------- 折線（單一數列＋參考線＋一個特別點）
// pts: [{x, y}]；opts: {id, fmt, ref:{y,label}, extra:{x,y,label}, xEvery}
function line(box, pts, opts) {
  const fmt = opts.fmt || (v => v);
  chart(box, W => {
    const H = opts.height || 220, pad = { l: 44, r: 70, t: 16, b: 26 };
    const all = [...pts, ...(opts.extra ? [opts.extra] : [])];
    const ticks = niceTicks(Math.max(...all.map(p => p.y), opts.ref ? opts.ref.y : 0));
    const max = ticks[ticks.length - 1], n = all.length;
    const ph = H - pad.t - pad.b, step = (W - pad.l - pad.r) / (n - 1 || 1);
    const X = i => pad.l + step * i, Y = v => pad.t + ph * (1 - v / max);
    let g = "";
    for (const t of ticks) g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${Y(t)}" y2="${Y(t)}" class="${t ? "grid" : "base"}"/>`
      + `<text x="${pad.l - 6}" y="${Y(t) + 4}" class="tick" text-anchor="end">${esc(fmt(t))}</text>`;
    if (opts.ref) {
      const x0 = X(opts.ref.from || 0);
      g += `<line x1="${x0}" x2="${X(pts.length - 1)}" y1="${Y(opts.ref.y)}" y2="${Y(opts.ref.y)}" class="ref"/>`
        + `<text x="${x0}" y="${Y(opts.ref.y) - 6}" class="tick">${esc(opts.ref.label)}</text>`;
    }
    const col = opts.color || SERIES[1];
    g += `<path d="${pts.map((p, i) => `${i ? "L" : "M"}${X(i)},${Y(p.y)}`).join("")}" class="ln" stroke="${col}"/>`;
    const every = opts.xEvery || 1;
    all.forEach((p, i) => {
      if (i < pts.length && (i % every === 0 || i === pts.length - 1))
        g += `<text x="${X(i)}" y="${H - 8}" class="tick" text-anchor="middle">${esc(p.x)}</text>`;
    });
    const last = pts[pts.length - 1];
    g += `<circle cx="${X(pts.length - 1)}" cy="${Y(last.y)}" r="4.5" fill="${col}" class="ring"/>`;
    if (opts.extra) {
      const e = opts.extra;
      g += `<circle cx="${X(n - 1)}" cy="${Y(e.y)}" r="5.5" fill="var(--surface)" stroke="${col}" stroke-width="2.5"/>`
        + `<text x="${X(n - 1) + 9}" y="${Y(e.y) - 8}" class="val strong">${esc(fmt(e.y))}</text>`
        + `<text x="${X(n - 1) + 9}" y="${Y(e.y) + 7}" class="tick">${esc(e.label)}</text>`;
    }
    all.forEach((p, i) => {
      const tip = Tip.set(`${opts.id}:${i}`, { head: p.label || p.x, rows: [{ label: opts.name || "", value: fmt(p.y), color: col }] });
      g += `<g class="xh-g"><line x1="${X(i)}" x2="${X(i)}" y1="${pad.t}" y2="${pad.t + ph}" class="xh"/>`
        + `<rect class="hit" x="${X(i) - step / 2}" y="${pad.t}" width="${step}" height="${ph}" data-tip="${tip}" tabindex="0"/></g>`;
    });
    return `<svg width="${W}" height="${H}" role="img">${g}</svg>`;
  });
}

// ---------------------------------------------------------------- 數字卡
function statTile({ label, value, unit, delta, deltaGood, sub, src }) {
  const d = delta ? `<div class="st-delta ${deltaGood === false ? "bad" : deltaGood ? "good" : ""}">${esc(delta)}</div>` : "";
  return `<div class="st"><div class="st-label">${esc(label)}</div>
    <div class="st-value">${esc(value)}<span class="st-unit">${esc(unit || "")}</span></div>${d}
    <div class="st-sub">${esc(sub || "")}</div>${src ? `<div class="st-src">${esc(src)}</div>` : ""}</div>`;
}

function tableView(head, rows, note) {
  return `<details class="tv"><summary>看表格</summary>${note ? `<p class="small muted">${esc(note)}</p>` : ""}
    <div class="tv-wrap"><table><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
}
