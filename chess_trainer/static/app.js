"use strict";
// Шахматный тренер — одностраничный интерфейс без сборки и внешних библиотек.

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
const MODE_RU = { rapid: "Рапид", blitz: "Блиц", bullet: "Пуля", daily: "Заочные" };
const RES_RU = { win: "Победа", loss: "Поражение", draw: "Ничья" };
const CLS_COLOR = (c) => `var(--c-${c})`;

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function date(ts) { return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "2-digit" }); }
async function busy(btn, fn) {
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${old}`;
  try { return await fn(); } catch (e) { toast(e.message, 6000); } finally { btn.disabled = false; btn.innerHTML = old; }
}

// ---------------- роутер ----------------
let cleanup = null;
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const h = location.hash.slice(1) || "/";
  const [, page, arg] = h.split("/");
  app.innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    if (!page) await home();
    else if (page === "games") await gamesList(arg);
    else if (page === "game") await gameView(decodeURIComponent(arg));
    else if (page === "openings") await openingsView();
    else app.innerHTML = `<div class="empty">Страница не найдена</div>`;
  } catch (e) { app.innerHTML = `<div class="empty">Ошибка: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
route();

// ---------------- главная ----------------
function sparkline(vals) {
  if (vals.length < 2) return "";
  const min = Math.min(...vals), max = Math.max(...vals), span = Math.max(1, max - min);
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * 100},${38 - ((v - min) / span) * 34}`).join(" ");
  return `<svg viewBox="0 0 100 40" preserveAspectRatio="none"><polyline points="${pts}" fill="none"
    stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
}
async function home() {
  const d = await api("/api/overview");
  const cards = d.modes.map((m) => `
    <div class="panel mode" data-tc="${m.time_class}">
      <div class="name">${MODE_RU[m.time_class] || m.time_class}</div>
      <div class="rating">${m.rating ?? "—"}</div>
      <div class="muted small">${m.best ? `лучший ${m.best} · ` : ""}${m.games} партий, разобрано ${m.analyzed}</div>
      ${sparkline(m.history)}
      <div class="small">30 дней: <b class="res win">+${m.last30.win}</b> <b class="res loss">−${m.last30.loss}</b> <b class="res draw">=${m.last30.draw}</b></div>
    </div>`).join("");
  app.innerHTML = `
    <div class="row"><h1>${esc(d.username || "Игрок")}</h1><span class="spacer"></span>
      <button id="sync">Загрузить новые партии</button></div>
    ${d.live ? "" : `<p class="muted small">Рейтинг из базы — Chess.com сейчас недоступен.</p>`}
    <div class="modes">${cards || `<div class="empty">Партий пока нет — нажмите «Загрузить новые партии».</div>`}</div>
    ${d.profile ? `<div class="panel"><h2>Профиль слабых мест</h2><div class="profile">${esc(d.profile)}</div></div>` : ""}`;
  app.querySelectorAll(".mode").forEach((el) => el.onclick = () => location.hash = `#/games/${el.dataset.tc}`);
  $("#sync").onclick = (e) => busy(e.target, async () => {
    const r = await api("/api/sync", { method: "POST" }); toast(`Новых партий: ${r.new}`); home();
  });
}

// ---------------- список партий ----------------
async function gamesList(tc = "rapid") {
  let filter = "", offset = 0, rows = [];
  const render = () => {
    app.innerHTML = `
      <div class="row"><h1>${MODE_RU[tc] || tc}</h1><span class="spacer"></span>
        ${Object.keys(MODE_RU).map((k) => `<a class="chip ${k === tc ? "on" : ""}" href="#/games/${k}">${MODE_RU[k]}</a>`).join("")}</div>
      <div class="row filters">
        ${[["", "Все"], ["win", "Победы"], ["loss", "Поражения"], ["draw", "Ничьи"]].map(([k, n]) =>
          `<span class="chip ${k === filter ? "on" : ""}" data-f="${k}">${n}</span>`).join("")}</div>
      <div class="panel"><table class="games"><thead><tr><th>Дата</th><th>Соперник</th><th>Результат</th>
        <th>Дебют</th><th></th></tr></thead><tbody>
        ${rows.map((g) => `<tr class="g" data-id="${esc(g.id)}"><td>${date(g.end_time)}</td>
          <td><span class="dot ${g.color}"></span>${esc(g.opponent)} <span class="muted">(${g.opp_rating ?? "?"})</span></td>
          <td class="res ${g.result}">${RES_RU[g.result]} <span class="muted small">${esc(g.result_detail)}</span></td>
          <td class="small">${esc(g.opening)}</td><td class="small muted">${g.analyzed ? "разобрана" : ""}</td></tr>`).join("")
          || `<tr><td colspan="5" class="empty">Нет партий</td></tr>`}
      </tbody></table>
      ${rows.length && rows.length % 50 === 0 ? `<div class="row" style="justify-content:center;margin-top:12px"><button id="more">Ещё</button></div>` : ""}</div>`;
    app.querySelectorAll("[data-f]").forEach((el) => el.onclick = () => { filter = el.dataset.f; offset = 0; rows = []; load(); });
    app.querySelectorAll("tr.g").forEach((el) => el.onclick = () => location.hash = `#/game/${encodeURIComponent(el.dataset.id)}`);
    const more = $("#more"); if (more) more.onclick = () => { offset += 50; load(); };
  };
  const load = async () => {
    rows = rows.concat(await api(`/api/games?time_class=${tc}&offset=${offset}${filter ? `&result=${filter}` : ""}`));
    render();
  };
  await load();
}

// ---------------- партия ----------------
async function gameView(id) {
  let g = await api(`/api/game/${encodeURIComponent(id)}`);
  if (!g.analyzed || g.outdated) {
    app.innerHTML = `<div class="panel" style="max-width:520px;margin:40px auto;text-align:center">
      <h2>${g.analyzed ? "Партия разобрана старой версией" : "Партия ещё не разобрана"}</h2>
      <p class="muted">${g.analyzed ? "Чтобы появились оценки «Блестящий» и «Отличный», её нужно проанализировать заново." :
        "Stockfish проверит каждый ход. Обычно это 10–40 секунд."}</p>
      <div class="row" style="justify-content:center"><button class="primary" id="an">Анализировать</button>
      ${g.analyzed ? `<button id="skip">Открыть как есть</button>` : ""}</div></div>`;
    const go = () => new Promise((res) => { $("#skip") && ($("#skip").onclick = () => res()); });
    await Promise.race([
      new Promise((res) => $("#an").onclick = (e) => busy(e.target, async () => {
        await api(`/api/game/${encodeURIComponent(id)}/analyze`, { method: "POST" }); res();
      })),
      go(),
    ]);
    g = await api(`/api/game/${encodeURIComponent(id)}`);
  }

  const st = {
    idx: g.moves.length ? 0 : 0,        // 0 = начальная позиция, i = после i-го полухода
    flip: g.color === "black",
    sel: null,
    vari: null,                          // {base, moves:[{san,uci,fen}], i}
    lines: [], linesFen: null, showArrows: true,
  };
  const me = g.color, opp = me === "white" ? "black" : "white";

  app.innerHTML = `
    <div class="row"><a class="btn" href="#/games/${g.time_class}">← ${MODE_RU[g.time_class] || g.time_class}</a>
      <h1 style="margin:0">${esc(g.white.name)} — ${esc(g.black.name)}</h1>
      <span class="res ${g.result}">${RES_RU[g.result]}</span><span class="muted small">${esc(g.opening)}</span>
      <span class="spacer"></span>
      <button class="primary" id="reportBtn">Отчёт о партии</button>
      ${g.url ? `<a class="btn" href="${esc(g.url)}" target="_blank" rel="noopener">Chess.com ↗</a>` : ""}</div>
    <div class="game" style="margin-top:14px">
      <div>
        <div class="player" id="pTop"></div>
        <div class="boardwrap">
          <div class="evalbar" id="evalbar"><div class="w" id="evalw"></div><div class="t" id="evalt"></div></div>
          <div class="board" id="board"><img id="boardImg" alt="доска"></div>
        </div>
        <div class="player" id="pBot"></div>
        <div class="controls">
          <button data-nav="first" title="Начало (Home)">⏮</button><button data-nav="prev" title="Назад (←)">◀</button>
          <button data-nav="next" title="Вперёд (→)">▶</button><button data-nav="last" title="Конец (End)">⏭</button>
          <button data-nav="flip" title="Перевернуть (F)">⇅</button>
        </div>
        <div class="graph panel" id="graph"></div>
      </div>
      <div class="side">
        <div class="panel movecard" id="movecard"></div>
        <div class="panel"><div class="row"><h2 style="margin:0">Варианты Stockfish</h2><span class="spacer"></span>
          <label class="small muted"><input type="checkbox" id="arrows" checked> стрелки</label></div>
          <div class="lines" id="lines"></div></div>
        <div class="panel"><div class="movelist" id="movelist"></div></div>
      </div>
    </div>`;

  const name = (side) => `${esc(g[side].name)} <span class="muted">(${g[side].rating ?? "?"})</span>`;
  const curFen = () => st.vari ? (st.vari.i ? st.vari.moves[st.vari.i - 1].fen : fenAt(st.vari.base)) : fenAt(st.idx);
  const fenAt = (i) => i === 0 ? g.start_fen : g.moves[i - 1].fen;
  const lastUci = () => st.vari ? (st.vari.i ? st.vari.moves[st.vari.i - 1].uci : (st.vari.base ? g.moves[st.vari.base - 1].uci : "")) :
    (st.idx ? g.moves[st.idx - 1].uci : "");

  function clockAt(side) {
    const i = st.vari ? st.vari.base : st.idx;
    for (let k = i; k >= 1; k--) {
      const mv = g.moves[k - 1];
      if ((k % 2 === 1 ? "white" : "black") === side && mv.clock != null) {
        const s = Math.round(mv.clock); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      }
    }
    return "";
  }

  function drawBoard() {
    const arrows = [];
    if (st.showArrows && st.lines.length && st.linesFen === curFen()) arrows.push(st.lines[0].uci[0]);
    const q = new URLSearchParams({ fen: curFen(), flip: st.flip ? "1" : "0", last: lastUci(), arrows: arrows.join(",") });
    if (st.sel) q.set("sel", st.sel);
    $("#boardImg").src = `/api/board.svg?${q}`;
    const top = st.flip ? "white" : "black", bot = st.flip ? "black" : "white";
    $("#pTop").innerHTML = `<span>${name(top)}</span><span>${clockAt(top)}</span>`;
    $("#pBot").innerHTML = `<span>${name(bot)}</span><span>${clockAt(bot)}</span>`;
    // значок оценки хода на поле, куда пошла фигура
    $("#board").querySelectorAll(".badge").forEach((b) => b.remove());
    const mv = !st.vari && st.idx ? g.moves[st.idx - 1] : null;
    if (mv && mv.cls) {
      const to = mv.uci.slice(2, 4); let f = to.charCodeAt(0) - 97, r = +to[1] - 1;
      if (st.flip) { f = 7 - f; r = 7 - r; }
      const b = document.createElement("div"); b.className = "badge";
      b.style.background = CLS_COLOR(mv.cls); b.textContent = mv.cls_icon;
      b.style.left = `${((15 + (f + 1) * 45) / 390) * 100}%`; b.style.top = `${((15 + (7 - r) * 45) / 390) * 100}%`;
      $("#board").appendChild(b);
    }
  }

  function evalBar(cpWhite, text) {
    const wp = cpWhite == null ? 0.5 : 1 / (1 + Math.exp(-0.00368208 * cpWhite));
    const pct = st.flip ? (1 - wp) * 100 : wp * 100;
    $("#evalw").style.height = `${pct}%`;
    $("#evalw").style.top = st.flip ? "0" : ""; $("#evalw").style.bottom = st.flip ? "" : "0";
    const t = $("#evalt"); t.textContent = text ?? ""; t.style.color = wp >= 0.5 ? "#333" : "#eee";
    const whiteOnTop = st.flip; t.style[(wp >= 0.5) === !whiteOnTop ? "bottom" : "top"] = "3px";
    t.style[(wp >= 0.5) === !whiteOnTop ? "top" : "bottom"] = "";
  }

  function moveCard() {
    const el = $("#movecard");
    if (st.vari) {
      el.innerHTML = `<div class="head">Свой вариант</div>
        <div class="variation" style="margin-top:8px">${st.vari.moves.map((m, k) =>
          `<span data-v="${k + 1}" class="${st.vari.i === k + 1 ? "cur" : ""}">${esc(m.san)}</span>`).join("") || "<span class='muted'>сделайте ход на доске</span>"}</div>
        <div class="row" style="margin-top:10px"><button id="back">← Вернуться к партии</button></div>`;
      el.querySelectorAll("[data-v]").forEach((s) => s.onclick = () => { st.vari.i = +s.dataset.v; update(); });
      $("#back").onclick = () => { st.idx = st.vari.base; st.vari = null; update(); };
      return;
    }
    if (st.idx === 0) {
      el.innerHTML = `<div class="head">Начальная позиция</div><p class="muted">Листайте ходы стрелками ← →, кликайте по ходам справа
        или делайте свои ходы прямо на доске, чтобы проверить идею.</p>`;
      return;
    }
    const m = g.moves[st.idx - 1];
    const who = (st.idx % 2 === 1) === (me === "white") ? "Ваш ход" : "Ход соперника";
    const n = Math.ceil(st.idx / 2) + (st.idx % 2 ? "." : "...");
    el.innerHTML = `
      <div class="head">${m.cls ? `<span class="icon" style="background:${CLS_COLOR(m.cls)}">${m.cls_icon}</span>` : ""}
        ${n} ${esc(m.san)} <span class="muted small" style="font-weight:400">${m.cls_name ? "— " + m.cls_name : ""}</span></div>
      <div class="muted small">${who}${m.time_spent != null ? ` · ${m.time_spent} с на ход` : ""}${m.phase ? ` · ${{ opening: "дебют", middlegame: "миттельшпиль", endgame: "эндшпиль" }[m.phase]}` : ""}</div>
      <p>${esc(m.explain || "")}</p>
      <div class="row">
        ${m.best_san && m.best_san !== m.san ? `<button id="showBest">Показать лучше: ${esc(m.best_san)}</button>` : ""}
        ${m.reply_line && m.cls && ["inaccuracy", "mistake", "miss", "blunder"].includes(m.cls) ? `<button id="showReply">Как наказать</button>` : ""}
        <button id="explain">Объяснить подробнее</button>
      </div>
      <div id="llm"></div>`;
    const sb = $("#showBest");
    if (sb) sb.onclick = () => playLine(st.idx - 1, [m.best_uci], true);
    const sr = $("#showReply");
    if (sr) sr.onclick = async () => {
      const prev = fenAt(st.idx);
      const ls = await api("/api/lines", { method: "POST", body: { fen: prev, multipv: 1 } });
      if (ls[0]) playLine(st.idx, ls[0].uci, false);
    };
    $("#explain").onclick = (e) => busy(e.target, async () => {
      const r = await api(`/api/game/${encodeURIComponent(id)}/explain/${st.idx}`, { method: "POST" });
      $("#llm").innerHTML = `<div class="llm">${esc(r.text)}</div>`;
    });
  }

  async function playLine(base, uciList, fromBefore) {
    // base — индекс позиции, из которой играем вариант
    const fen = fenAt(base);
    let moves = await api("/api/play", { method: "POST", body: { fen, uci: uciList } });
    if (fromBefore && moves.length === 1) {
      // докрутим вариант движком после лучшего хода
      const ls = await api("/api/lines", { method: "POST", body: { fen: moves[0].fen, multipv: 1 } });
      if (ls[0]) moves = moves.concat(await api("/api/play", { method: "POST", body: { fen: moves[0].fen, uci: ls[0].uci.slice(0, 7) } }));
    }
    st.vari = { base, moves, i: 1 }; st.sel = null; update();
  }

  function moveList() {
    const el = $("#movelist"); let html = "";
    for (let i = 0; i < g.moves.length; i += 2) {
      html += `<div class="n">${i / 2 + 1}.</div>`;
      for (const k of [i, i + 1]) {
        const m = g.moves[k];
        if (!m) { html += "<div></div>"; continue; }
        const bad = m.cls && !["best", "excellent", "good", "book"].includes(m.cls);
        html += `<div class="m ${!st.vari && st.idx === k + 1 ? "cur" : ""}" data-i="${k + 1}">${esc(m.san)}
          ${m.cls && (bad || ["brilliant", "great"].includes(m.cls)) ? `<span class="i" style="color:${CLS_COLOR(m.cls)}">${m.cls_icon}</span>` : ""}</div>`;
      }
    }
    el.innerHTML = html;
    el.querySelectorAll(".m").forEach((d) => d.onclick = () => { st.vari = null; st.idx = +d.dataset.i; update(); });
    const cur = el.querySelector(".cur"); if (cur) cur.scrollIntoView({ block: "nearest" });
  }

  function graph() {
    const pts = [[0, 0.5]].concat(g.moves.map((m, i) => [i + 1, m.wp_white ?? 0.5]));
    const n = Math.max(1, g.moves.length), W = 600, H = 90;
    const xy = pts.map(([i, w]) => `${(i / n) * W},${H - w * H}`).join(" ");
    const marks = g.moves.map((m, i) => ["blunder", "mistake", "miss", "brilliant", "great"].includes(m.cls) ?
      `<circle cx="${((i + 1) / n) * W}" cy="${H - (m.wp_white ?? .5) * H}" r="3.5" fill="${CLS_COLOR(m.cls)}"/>` : "").join("");
    const cx = ((st.vari ? st.vari.base : st.idx) / n) * W;
    $("#graph").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <rect width="${W}" height="${H}" fill="#403d39"/>
      <polygon points="0,${H} ${xy} ${W},${H}" fill="#f2f2f2"/>
      <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="#888" stroke-dasharray="3 3"/>
      ${marks}<line x1="${cx}" x2="${cx}" y1="0" y2="${H}" stroke="var(--accent)" stroke-width="2"/></svg>`;
  }
  $("#graph").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    st.vari = null; st.idx = Math.round(((e.clientX - r.left) / r.width) * g.moves.length); update();
  };

  let linesTimer = null, linesReq = 0;
  function lines() {
    const fen = curFen();
    const box = $("#lines");
    if (st.linesFen === fen) return renderLines();
    box.innerHTML = `<div class="muted small" style="padding:8px 4px"><span class="spin"></span> считаю…</div>`;
    clearTimeout(linesTimer);
    const req = ++linesReq;
    linesTimer = setTimeout(async () => {
      try {
        const ls = await api("/api/lines", { method: "POST", body: { fen, multipv: 3 } });
        if (req !== linesReq) return;
        st.lines = ls; st.linesFen = fen; renderLines(); drawBoard();
      } catch (e) { box.innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
    }, 250);
  }
  function renderLines() {
    const box = $("#lines");
    if (!st.lines.length) { box.innerHTML = `<div class="muted small" style="padding:8px 4px">Партия окончена</div>`; evalBar(null, ""); return; }
    box.innerHTML = st.lines.map((l, k) => `<div class="line" data-k="${k}"><span class="ev">${l.eval}</span>
      <span class="pv">${esc(l.san)}</span></div>`).join("");
    box.querySelectorAll(".line").forEach((d) => d.onclick = () => {
      const base = st.vari ? null : st.idx;
      const l = st.lines[+d.dataset.k];
      if (base === null) {
        // продолжаем свой вариант
        api("/api/play", { method: "POST", body: { fen: curFen(), uci: l.uci.slice(0, 8) } }).then((mv) => {
          st.vari.moves = st.vari.moves.slice(0, st.vari.i).concat(mv); st.vari.i += 1; update();
        });
      } else playLine(base, l.uci.slice(0, 8), false);
    });
    evalBar(st.lines[0].cp_white, st.lines[0].eval.replace("+", ""));
  }

  function update() { drawBoard(); moveCard(); moveList(); graph(); lines(); }

  // ходы мышью по доске
  $("#boardImg").onclick = async (e) => {
    const r = e.target.getBoundingClientRect();
    let f = Math.floor((((e.clientX - r.left) / r.width) * 390 - 15) / 45);
    let rk = 7 - Math.floor((((e.clientY - r.top) / r.height) * 390 - 15) / 45);
    if (f < 0 || f > 7 || rk < 0 || rk > 7) return;
    if (st.flip) { f = 7 - f; rk = 7 - rk; }
    const sq = String.fromCharCode(97 + f) + (rk + 1);
    if (!st.sel) { st.sel = sq; drawBoard(); return; }
    if (st.sel === sq) { st.sel = null; drawBoard(); return; }
    const res = await api("/api/move", { method: "POST", body: { fen: curFen(), from: st.sel, to: sq } });
    if (!res.legal) { st.sel = sq; drawBoard(); return; }
    st.sel = null;
    if (!st.vari) {
      // совпало с ходом партии — просто шагаем вперёд
      const nxt = g.moves[st.idx];
      if (nxt && nxt.uci === res.uci) { st.idx += 1; update(); return; }
      st.vari = { base: st.idx, moves: [], i: 0 };
    }
    st.vari.moves = st.vari.moves.slice(0, st.vari.i).concat([res]); st.vari.i += 1; update();
  };

  function nav(cmd) {
    if (cmd === "flip") { st.flip = !st.flip; update(); return; }
    if (st.vari) {
      const max = st.vari.moves.length;
      st.vari.i = { first: 0, prev: Math.max(0, st.vari.i - 1), next: Math.min(max, st.vari.i + 1), last: max }[cmd];
    } else {
      const max = g.moves.length;
      st.idx = { first: 0, prev: Math.max(0, st.idx - 1), next: Math.min(max, st.idx + 1), last: max }[cmd];
    }
    st.sel = null; update();
  }
  app.querySelectorAll("[data-nav]").forEach((b) => b.onclick = () => nav(b.dataset.nav));
  const onKey = (e) => {
    if (e.target.tagName === "INPUT") return;
    const k = { ArrowLeft: "prev", ArrowRight: "next", Home: "first", End: "last", f: "flip" }[e.key];
    if (k) { e.preventDefault(); nav(k); }
    if (e.key === "Escape" && st.vari) { st.idx = st.vari.base; st.vari = null; update(); }
  };
  document.addEventListener("keydown", onKey);
  cleanup = () => document.removeEventListener("keydown", onKey);
  $("#arrows").onchange = (e) => { st.showArrows = e.target.checked; drawBoard(); };
  $("#reportBtn").onclick = () => showReport(id, g, (ply) => { st.vari = null; st.idx = ply; update(); });

  update();
}

// ---------------- отчёт о партии ----------------
async function showReport(id, g, jump) {
  const ov = document.createElement("div"); ov.className = "overlay";
  ov.innerHTML = `<div class="panel"><div class="loading">Собираю отчёт…</div></div>`;
  document.body.appendChild(ov);
  ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  try {
    const r = await api(`/api/game/${encodeURIComponent(id)}/report`);
    const order = ["brilliant", "great", "best", "excellent", "good", "book", "inaccuracy", "mistake", "miss", "blunder"];
    ov.firstChild.innerHTML = `
      <div class="row"><h2 style="margin:0">Отчёт о партии</h2><span class="spacer"></span><button id="close">✕</button></div>
      <div class="acc"><div><span class="muted small">Ваша точность</span><b>${r.accuracy.me ?? "—"}</b></div>
        <div><span class="muted small">Соперник</span><b>${r.accuracy.opponent ?? "—"}</b></div>
        ${Object.entries(r.phases).map(([k, v]) => `<div><span class="muted small">${k}</span><b style="font-size:20px">${v}</b></div>`).join("")}</div>
      <table class="counts"><tr><td></td><td class="num muted small">Вы</td><td class="num muted small">Соперник</td></tr>
        ${order.map((k) => `<tr><td><span class="icon" style="background:${CLS_COLOR(k)}">${g.classes[k].icon}</span> ${g.classes[k].name}</td>
          <td class="num" style="color:${CLS_COLOR(k)}">${r.counts.me[k]}</td><td class="num muted">${r.counts.opponent[k]}</td></tr>`).join("")}</table>
      <p class="muted small">Время: в среднем ${r.time.avg_spent ?? "—"} с на ход${r.time.fast_errors ? `, ошибок сделано быстрее 3 секунд: ${r.time.fast_errors}` : ""}.</p>
      <h2>Ключевые моменты</h2>
      ${r.key_moments.map((k) => `<div class="km" data-ply="${k.ply}"><span class="icon" style="background:${CLS_COLOR(k.cls)}">${k.cls_icon}</span>
        <b>${esc(k.move)}</b> — ${esc(k.cls_name)}<div class="small muted">${esc(k.explain)}</div></div>`).join("") || `<p class="muted">Серьёзных ошибок нет.</p>`}
      <h2 style="margin-top:16px">Разбор тренера</h2>
      <div id="narr">${r.narrative ? `<div class="llm">${esc(r.narrative)}</div>` : ""}</div>
      <button class="primary" id="narrBtn" style="margin-top:10px">${r.narrative ? "Переписать разбор" : "Написать разбор (Claude)"}</button>`;
    $("#close", ov).onclick = () => ov.remove();
    ov.querySelectorAll(".km").forEach((d) => d.onclick = () => { ov.remove(); jump(+d.dataset.ply); });
    $("#narrBtn", ov).onclick = (e) => busy(e.target, async () => {
      const n = await api(`/api/game/${encodeURIComponent(id)}/report/narrative`, { method: "POST" });
      $("#narr", ov).innerHTML = `<div class="llm">${esc(n.text)}</div>`;
    });
  } catch (e) { ov.firstChild.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

// ---------------- дебюты ----------------
async function openingsView() {
  const list = await api("/api/openings");
  const card = (o) => `
    <div class="panel" data-id="${o.id}">
      <div class="row"><h2 style="margin:0"><span class="dot ${o.color}"></span>${esc(o.name)}</h2><span class="spacer"></span>
        <button class="del small" title="Удалить">✕</button></div>
      <p class="small muted">${o.games ? `Ваших партий: ${o.games} — <b class="res win">+${o.win}</b> <b class="res loss">−${o.loss}</b> <b class="res draw">=${o.draw}</b>` :
        "В ваших партиях пока не встречался"}${o.match ? ` · ищу по «${esc(o.match)}»` : ""}</p>
      ${o.recent_games.length ? `<div class="small">${o.recent_games.slice(0, 5).map((gm) =>
        `<a href="#/game/${encodeURIComponent(gm.id)}" class="res ${gm.result}">${date(gm.end_time)}</a>`).join(" · ")}</div>` : ""}
      <div style="margin-top:10px">${o.resources.map((r) => `<div class="res-item">
        ${{ video: "🎬", article: "📄", course: "🎓" }[r.kind] || "•"} <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
        <span class="muted small">${esc(r.lang || "")}</span> <a href="#" class="small muted rdel" data-r="${r.id}">убрать</a>
        <div class="small muted">${esc(r.why || "")}</div></div>`).join("") || `<p class="muted small">Материалов пока нет.</p>`}</div>
      <div class="row" style="margin-top:10px"><button class="primary find">Найти материалы</button>
        <button class="send" ${o.resources.length ? "" : "disabled"}>Отправить в Telegram</button></div>
    </div>`;
  app.innerHTML = `
    <h1>Мои дебюты</h1>
    <form class="add" id="add">
      <input name="name" placeholder="Название, например «Каро-Канн»" required size="28">
      <select name="color"><option value="white">белыми</option><option value="black">чёрными</option></select>
      <input name="match" placeholder="как в Chess.com: Caro Kann (необяз.)" size="26">
      <button class="primary">Добавить</button>
    </form>
    <p class="muted small">«Найти материалы» ищет видео и статьи через Claude с веб-поиском, проверяет, что ссылки живые,
      и сохраняет их здесь. Поиск занимает 1–3 минуты.</p>
    <div class="openings">${list.map(card).join("") || `<div class="empty">Добавьте свои 6 дебютов — по 3 за каждый цвет.</div>`}</div>`;
  $("#add").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { await api("/api/openings", { method: "POST", body: Object.fromEntries(f) }); openingsView(); }
    catch (err) { toast(err.message); }
  };
  app.querySelectorAll("[data-id]").forEach((el) => {
    const oid = el.dataset.id;
    $(".del", el).onclick = async () => { if (confirm("Удалить дебют?")) { await api(`/api/openings/${oid}`, { method: "DELETE" }); openingsView(); } };
    $(".find", el).onclick = (e) => busy(e.target, async () => {
      const r = await api(`/api/openings/${oid}/find`, { method: "POST" });
      toast(`Добавлено: ${r.saved}${r.dropped.length ? `, отброшено нерабочих ссылок: ${r.dropped.length}` : ""}`);
      openingsView();
    });
    $(".send", el).onclick = (e) => busy(e.target, async () => {
      const r = await api(`/api/openings/${oid}/send`, { method: "POST" }); toast(`Отправлено в Telegram: ${r.sent}`);
    });
    el.querySelectorAll(".rdel").forEach((a) => a.onclick = async (ev) => {
      ev.preventDefault(); await api(`/api/resources/${a.dataset.r}`, { method: "DELETE" }); openingsView();
    });
  });
}
