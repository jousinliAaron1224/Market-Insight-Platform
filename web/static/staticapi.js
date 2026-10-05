// 靜態快照版的 API（只在 scripts/export_site.py 匯出的版本載入，D31）。
// 把頁面呼叫的 /api/... 轉成讀取同目錄 data/*.json；需要篩選的在瀏覽器端做。
// 商品工作台的草案存在 claude.ai 頁面的共用資料庫（db capability）；拿不到時改存在這個瀏覽器（localStorage）。
(() => {
  const realFetch = window.fetch.bind(window);
  const cache = {};
  const load = name => (cache[name] = cache[name] || realFetch("data/" + name).then(r => {
    if (!r.ok) throw new Error(`${name} → ${r.status}`);
    return r.json();
  }));
  const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  const LINES = ["", "變額年金保險", "變額壽險", "變額萬能壽險"];
  const IMPACT_RANK = { high: 0, medium: 1, low: 2 };
  const GROUPS = { law: ["tii_law_rss", "fsc_press", "fsc_penalty"], news: ["news_rss"] };

  // ---------------------------------------------------------------- 草案儲存
  let dbP = null;
  const getDb = () => (dbP = dbP || (window.claude && window.claude.use ? window.claude.use("db").catch(() => null) : Promise.resolve(null)));
  const LS = "lab-drafts";
  const lsAll = () => { try { return JSON.parse(localStorage.getItem(LS) || "{}"); } catch { return {}; } };
  const lsSave = all => { try { localStorage.setItem(LS, JSON.stringify(all)); } catch { /* 無痕視窗等情況：只留在記憶體 */ } };
  let mem = null;
  // 資料來源頁在主頁的框架裡開：草案一律交給主頁處理（主頁才拿得到共用資料庫）
  const parentDrafts = (() => { try { return window.parent !== window && window.parent.__staticDrafts; } catch { return null; } })();
  const Drafts = parentDrafts || {
    async list() {
      const db = await getDb();
      if (db) {
        const snap = await db.collection("drafts").get();
        return snap.docs.map(d => ({ id: d.id, ...d.data() }));
      }
      mem = mem || lsAll();
      return Object.entries(mem).map(([id, d]) => ({ id, ...d }));
    },
    async get(id) {
      const db = await getDb();
      if (db) {
        const s = await db.doc("drafts/" + id).get();
        return s.exists ? { id: s.id, ...s.data() } : null;
      }
      mem = mem || lsAll();
      return mem[id] ? { id, ...mem[id] } : null;
    },
    async put(id, body) {
      const db = await getDb();
      if (db) return db.doc("drafts/" + id).set(body);
      mem = mem || lsAll(); mem[id] = body; lsSave(mem);
    },
    async del(id) {
      const db = await getDb();
      if (db) return db.doc("drafts/" + id).delete();
      mem = mem || lsAll(); delete mem[id]; lsSave(mem);
    },
  };
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const now = () => new Date().toISOString().slice(0, 19);

  function cleanData(d) {
    const ok = v => ["string", "number", "boolean"].includes(typeof v) || v === null || (Array.isArray(v) && v.every(x => typeof x === "string"));
    const obj = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
    const fields = {}, notes = {}, refs = {};
    for (const [k, v] of Object.entries(obj(d.fields))) if (ok(v)) fields[k.slice(0, 64)] = v;
    for (const [k, v] of Object.entries(obj(d.notes))) notes[k.slice(0, 64)] = String(v).slice(0, 5000);
    for (const [k, v] of Object.entries(obj(d.refs))) if (Array.isArray(v)) refs[k.slice(0, 64)] = v.filter(r => r && typeof r === "object").slice(0, 50);
    return { fields, notes, refs };
  }

  // ---------------------------------------------------------------- 檢查提示（與 web/product_lab.py 的 checks 相同）
  const empty = v => v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
  const fv = (d, k, dflt = null) => { const v = (d.fields || {})[k]; return empty(v) ? dflt : v; };
  const numv = v => (v === null || v === "" || isNaN(Number(v))) ? null : Number(v);
  async function checks(d) {
    const modules = await load("lab_modules.json"), feats = await load("lab_features.json");
    const out = [], add = (module, level, msg, ref = null) => out.push({ module, level, msg, ref });
    for (const m of modules) for (const f of m.fields) if (f.required && fv(d, f.id) === null) add(m.id, "missing", `「${f.label}」還沒填`);
    const line = fv(d, "line");
    const same = feats.filter(x => !line || x.line === line);
    const ageMax = numv(fv(d, "age_max"));
    if ((ageMax !== null && ageMax >= 65) || fv(d, "senior") === true)
      add("sales", "warn", "開放 65 歲以上投保：要規劃高齡客戶保護（適合度、關懷提問；2026 年起錄音錄影可申請以其他措施取代）",
        "保險業招攬及核保理賠辦法（第 6、7 條，以現行條文為準）");
    if (line === "變額壽險" || line === "變額萬能壽險") {
      add("benefits", "warn", "變額壽險類：身故給付對保單帳戶價值要符合依年齡訂定的最低比率，投保與每次繳費時都要檢核",
        "投資型人壽保險商品死亡給付對保單帳戶價值之最低比率規範");
      if (fv(d, "death_type") === "返還保單帳戶價值（年金型）") add("benefits", "warn", "險種是變額壽險，但身故給付型態選了年金型的「返還保單帳戶價值」");
    }
    if (line === "變額年金保險" && fv(d, "annuity_guarantee") === null) add("benefits", "info", "變額年金：年金保證期間還沒定（同險種競品多半提供 10／15／20 年）");
    if ((fv(d, "currency", [])).some(c => c !== "新臺幣")) add("positioning", "info", "外幣計價：要揭露匯率風險，並確認外幣收付作業", "投資型保險商品銷售應注意事項（匯率風險揭露）");
    if (fv(d, "distribution") === true) add("investment", "warn", "有資產撥回：撥回可能來自本金，銷售文件與條款要明確揭露撥回來源與可能減損本金",
      "投資型保險投資管理辦法；投資型保險商品銷售應注意事項");
    if (fv(d, "discretionary") === true || (fv(d, "asset_types", [])).includes("全權委託帳戶"))
      add("investment", "info", "全權委託帳戶：要符合委託投資的資格、費用揭露與專設帳簿規定", "投資型保險投資管理辦法");
    const ch = fv(d, "channel", []);
    if (ch.includes("網路投保")) add("sales", "info", "網路投保：確認可網路銷售的商品範圍與身分驗證", "保險業辦理電子商務應注意事項");
    if (ch.includes("銀行保險")) add("sales", "info", "銀行通路：確認銀行保險業務的招攬、說明與適合度規定", "銀行、保險公司、保險代理人或保險經紀人辦理銀行保險業務應注意事項");
    for (const [fid, feat, label] of [["premium_charge", "premium_charge_pct", "保費費用率"], ["surrender_max", "surrender_max_pct", "第一年解約費用率"]]) {
      const v = numv(fv(d, fid));
      const comp = same.map(x => x.features[feat]).filter(x => x !== null && x !== undefined);
      if (v !== null && comp.length) {
        const mx = Math.max(...comp), mn = Math.min(...comp);
        if (v > mx) add("fees", "info", `${label} ${v}% 高於同險種競品最高 ${mx}%（${comp.length} 個商品）`);
        else if (v < mn) add("fees", "info", `${label} ${v}% 低於同險種競品最低 ${mn}%（${comp.length} 個商品）`);
      }
    }
    if ([null, "待確認"].includes(fv(d, "filing_type"))) add("filing", "info", "送審方式（核准／備查）還沒確定", "保險商品銷售前程序作業準則");
    return out;
  }

  function fromProduct(item) {
    const f = item.features;
    const fields = {
      name: `（參考 ${item.name}）`, line: f.line, currency: f.currency, payment: f.payment_modes,
      death_type: (f.death_type || [null])[0], min_death: f.min_death_guarantee, disability: f.disability,
      maturity: f.maturity_benefit, annuity_guarantee: f.annuity_guarantee_years, accumulation_min: f.accumulation_min_years,
      discretionary: f.discretionary, distribution: f.distribution, stop_profit: f.stop_profit,
      premium_charge: f.premium_charge_pct, surrender_max: f.surrender_max_pct, bonus: f.bonus,
      partial_withdrawal: f.partial_withdrawal, basic_amount_change: f.basic_amount_change, riders: f.riders,
    };
    for (const k of Object.keys(fields)) if (fields[k] === null || fields[k] === undefined || (Array.isArray(fields[k]) && !fields[k].length)) delete fields[k];
    return { fields, notes: {}, refs: { positioning: [{ kind: "product", title: `${item.company}｜${item.name}`, url: item.pdf, note: "複製來源" }] } };
  }

  async function withChecks(d) { return d ? { ...d, checks: await checks(d) } : null; }

  // ---------------------------------------------------------------- 條文
  async function articles(rid) {
    const idx = await load("articles_index.json");
    const file = idx[String(rid)];
    if (!file) return [];
    return (await load(file))[String(rid)] || [];
  }
  function pickArticles(module, arts) {
    const kws = module.articles || [], akws = module.appendix || [], out = [];
    for (const r of arts) {
      if (r.kind === "article" && kws.some(k => (r.title || "").includes(k))) {
        out.push({ kind: "article", title: `第 ${r.article_no} 條 ${r.title}`, text: r.text.slice(0, 2000), page: r.page_start });
      } else if (r.kind === "appendix" && akws.length) {
        for (const k of akws) {
          const i = r.text.indexOf(k);
          if (i >= 0) {
            const end = Math.max(0, i - 200);
            const a = end > 0 ? r.text.lastIndexOf("\n", end - 1) + 1 : 0;
            out.push({ kind: "appendix", title: `附表：${k}`, text: r.text.slice(a, Math.min(r.text.length, i + 1400)), page: r.page_start });
            break;
          }
        }
      }
    }
    return out;
  }

  // ---------------------------------------------------------------- 路由
  async function route(path, q, method, body) {
    if (method === "GET") {
      if (path === "/api/meta") return load("meta.json");
      if (path === "/api/week") return load(`week_${[7, 14, 30].includes(+q.days) ? +q.days : 7}.json`);
      if (path === "/api/labels") {
        let items = await load("labels.json");
        const imps = (q.impact || "").split(",").filter(x => x in IMPACT_RANK);
        items = items.filter(i => (!imps.length || imps.includes(i.impact))
          && (!q.category || (i.categories || []).includes(q.category))
          && (!q.source || i.source_id === q.source)
          && (!GROUPS[q.group] || GROUPS[q.group].includes(i.source_id))
          && (!q.q || (i.title || "").includes(q.q))
          && (!q.from || (i.date || "") >= q.from) && (!q.to || (i.date || "") <= q.to)
          && ((q.hidden || "0") === "all" || (+!!i.hidden) === +(q.hidden || 0)));
        items = [...items].sort((a, b) => q.sort === "date" ? (b.date || "").localeCompare(a.date || "")
          : (IMPACT_RANK[a.impact] - IMPACT_RANK[b.impact]) || (b.date || "").localeCompare(a.date || ""));
        return { total: items.length, items };
      }
      if (path === "/api/impact") return (await load("impact.json"))[q.raw_doc_id] || null;
      if (path === "/api/products") {
        const items = (await load("products.json")).filter(p => (!q.company || p.company === q.company)
          && (!q.line || p.line === q.line) && (!q.status || p.status === q.status)
          && (q.currency !== "TWD" || p.currency === "TWD") && (q.currency !== "FX" || p.currency !== "TWD")
          && (!q.q || p.name.includes(q.q)) && (q.has_clause !== "1" || p.has_clause));
        return { total: items.length, items };
      }
      if (path === "/api/product") return (await load("products_detail.json"))[`${q.company}|${q.name}`] || null;
      if (path === "/api/articles") return articles(q.raw_doc_id);
      if (path === "/api/market/supply") return load("market_supply.json");
      if (path === "/api/market/demand") return load("market_demand.json");
      if (path === "/api/market/companies") return load("market_companies.json");
      if (path === "/api/lab/modules") return load("lab_modules.json");
      if (path === "/api/lab/products") {
        const meta = await load("meta.json");
        return (await load("lab_features.json")).filter(x => !q.line || x.line === q.line)
          .sort((a, b) => ((a.company !== meta.self_company) - (b.company !== meta.self_company)) || a.company.localeCompare(b.company) || a.name.localeCompare(b.name));
      }
      if (path === "/api/lab/context") {
        const modules = await load("lab_modules.json");
        const module = modules.find(m => m.id === q.module);
        if (!module) return null;
        const d = q.id ? await Drafts.get(q.id) : null;
        const li = Math.max(0, LINES.indexOf(d ? fv(d, "line", "") : ""));
        const ctx = JSON.parse(JSON.stringify(await load(`ctx_${module.id}_${li}.json`)));
        const c = ctx.competitors;
        if (q.product) {
          const [co, nm] = [q.product.split("|")[0], q.product.split("|").slice(1).join("|")];
          const p = c.products.find(x => x.company === co && x.name === nm);
          if (p) c.selected = { company: p.company, name: p.name, pdf: p.pdf, first_date: p.first_date };
        }
        c.articles = [];
        if (c.selected) {
          const f = (await load("lab_features.json")).find(x => x.company === c.selected.company && x.name === c.selected.name);
          if (f) c.articles = pickArticles(module, await articles(f.raw_doc_id));
        }
        return ctx;
      }
      if (path === "/api/lab/spec") {
        const d = await Drafts.get(q.id);
        if (!d) return null;
        const modules = await load("lab_modules.json");
        const li = Math.max(0, LINES.indexOf(fv(d, "line", "")));
        const mods = [];
        let n = 0;
        for (const m of modules) {
          const c = (await load(`ctx_${m.id}_${li}.json`)).competitors;
          n = c.n;
          mods.push({ id: m.id, name: m.name, regs: m.regs || [], note: (d.notes || {})[m.id] || "", refs: (d.refs || {})[m.id] || [],
            fields: m.fields.map(f => ({ id: f.id, label: f.label, value: (d.fields || {})[f.id] ?? null, competitors: f.feature ? c.fields[f.id] : null })) });
        }
        return { draft: { id: d.id, name: d.name, base: d.base, created_at: d.created_at, updated_at: d.updated_at },
          line: fv(d, "line"), n_competitors: n, modules: mods, checks: await checks(d) };
      }
      if (path === "/api/drafts") {
        return (await Drafts.list()).map(d => ({ id: d.id, name: d.name, base: d.base || null, line: (d.fields || {}).line || null,
          created_at: d.created_at, updated_at: d.updated_at })).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
      }
      if (path === "/api/draft") return withChecks(await Drafts.get(q.id));
    }
    if (method === "POST" && path === "/api/drafts") {
      let data = {}, base = null;
      if (body.from) {
        const [co, nm] = [body.from.split("|")[0], body.from.split("|").slice(1).join("|")];
        const item = (await load("lab_features.json")).find(x => x.company === co && x.name === nm);
        if (!item) return [{ error: `找不到競品：${body.from}` }, 400];
        data = fromProduct(item); base = body.from;
      }
      const id = newId(), t = now();
      const doc = { name: String(body.name || "").trim() || "未命名商品", base, ...cleanData(data), created_at: t, updated_at: t };
      await Drafts.put(id, doc);
      return [await withChecks({ id, ...doc }), 201];
    }
    if (method === "PUT" && path === "/api/draft") {
      const old = await Drafts.get(q.id);
      if (!old) return null;
      const doc = { name: (body.name || "").trim() || old.name, base: old.base || null, ...cleanData(body), created_at: old.created_at, updated_at: now() };
      await Drafts.put(q.id, doc);
      return withChecks({ id: q.id, ...doc });
    }
    if (method === "DELETE" && path === "/api/draft") { await Drafts.del(q.id); return { deleted: true }; }
    return null;
  }

  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return realFetch(input, init);
    const u = new URL(url, location.href);
    const q = Object.fromEntries(u.searchParams.entries());
    const method = (init.method || "GET").toUpperCase();
    try {
      const body = init.body ? JSON.parse(init.body) : {};
      let res = await route(u.pathname, q, method, body);
      let status = 200;
      if (Array.isArray(res) && res.length === 2 && typeof res[1] === "number") [res, status] = res;
      if (res === null || res === undefined) return reply({ error: "not found" }, 404);
      return reply(res, status);
    } catch (e) {
      const code = e && e.code;
      const msg = code === "invalid_argument" ? "你目前的分享權限只能看、不能修改草案" : String((e && e.message) || e);
      return reply({ error: msg }, 400);
    }
  };
  window.STATIC_SNAPSHOT = true;
  window.__staticDrafts = Drafts;

  // ---------------------------------------------------------------- 頁面切換：主頁（工作台）＋資料來源頁放在框架裡
  const inFrame = (() => { try { return window.parent !== window && !!window.parent.__showPage; } catch { return false; } })();
  const PAGE_RE = /(?:^|\/)(wall|compare|market|spec)\.html([?#].*)?$/;
  const LAB_RE = /(?:^|\/)index\.html(#.*)?$/;
  if (inFrame) {
    document.documentElement.classList.add("in-frame");
    document.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a) return;
      const href = a.getAttribute("href");
      const m = LAB_RE.exec(href);
      if (m) { e.preventDefault(); window.parent.__showLab(m[1] || ""); return; }
      const p = PAGE_RE.exec(href);
      if (p) { a.removeAttribute("target"); window.parent.__setHere(p[1]); }
    }, true);
  } else {
    window.__showPage = (href) => {
      const f = document.getElementById("srcFrame"), main = document.querySelector("main");
      if (!f) { location.href = href; return; }
      const tb = document.querySelector(".topbar");
      if (tb) { f.style.top = tb.offsetHeight + "px"; f.style.height = `calc(100% - ${tb.offsetHeight}px)`; }
      f.src = href; f.hidden = false; if (main) main.hidden = true;
      window.__setHere((PAGE_RE.exec(href) || [])[1]);
      window.scrollTo(0, 0);
    };
    window.__showLab = (hash) => {
      const f = document.getElementById("srcFrame"), main = document.querySelector("main");
      if (f) { f.hidden = true; f.removeAttribute("src"); }
      if (main) main.hidden = false;
      window.__setHere("lab");
      if (hash && hash !== location.hash) { history.replaceState(null, "", hash); window.dispatchEvent(new Event("hashchange")); }
    };
    window.__setHere = (k) => {
      document.querySelectorAll(".topnav a").forEach(a => {
        const hit = k === "lab" ? LAB_RE.test(a.getAttribute("href")) : (PAGE_RE.exec(a.getAttribute("href")) || [])[1] === k;
        a.classList.toggle("here", !!hit);
      });
    };
    document.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a) return;
      const href = a.getAttribute("href");
      if (PAGE_RE.test(href)) { e.preventDefault(); window.__showPage(href); }
      else if (LAB_RE.test(href)) { e.preventDefault(); window.__showLab((LAB_RE.exec(href) || [])[1] || ""); }
    }, true);
  }
})();
