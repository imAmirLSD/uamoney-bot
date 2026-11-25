/* UA. Mini App — MVP: главная, задания (демо), вывод, профиль. */
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { tg.expand(); tg.ready(); }

const API = "/miniapi";
const DEMO = !tg || !tg.initData;
const STAR_USD = 0.017;

let profile = null;
let cases = [];
let roulette = null;
let starfield = null;
let daily = null;
let selectedStars = 50;
let withdrawMethod = "stars";

function $(id) { return document.getElementById(id); }
function setText(id, value) { const el = $(id); if (el) el.textContent = value; }
function usd(v) { return Number(v || 0).toFixed(3) + " $"; }
function stars(v) { return Number(v || 0).toFixed(2) + " ⭐"; }
function toStars(v) { return Number(v || 0) / STAR_USD; }

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (tg && tg.initData) headers["X-Telegram-Init-Data"] = tg.initData;
  if (options.body) headers["Content-Type"] = "application/json";
  const url = API + path + (DEMO ? (path.includes("?") ? "&demo=1" : "?demo=1") : "");
  const response = await fetch(url, Object.assign({}, options, { headers }));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 403) throw new Error("Mini App пока доступен только администратору");
    throw new Error(data.error || ("HTTP " + response.status));
  }
  return data;
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(el => el.classList.toggle("active", el.id === "tab-" + name));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.tab === name));
  if (window.scrollTo) window.scrollTo(0, 0);
}

function renderProfile(data) {
  profile = data;
  const b = data.balance || {};
  const u = data.user || {};

  setText("home-balance", usd(b.usd));
  setText("home-balance-stars", "≈ " + stars(b.stars));
  setText("home-frozen", usd(b.frozen_usd));
  setText("home-frozen-stars", "≈ " + stars(b.frozen_stars));
  setText("home-tasks", String(u.completed || 0));
  setText("header-level", "Уровень " + (u.level || 0));
  setText("home-level", String(u.level || 0));

  const need = Math.max(0, (u.exp_next || 3) - (u.exp || 0));
  const pct = Math.min(100, Math.round(((u.exp || 0) / Math.max(1, u.exp_next || 3)) * 100));
  $("home-progress").style.width = pct + "%";
  setText("home-progress-note", need ? "До следующего уровня: " + need + " заданий" : "Следующий уровень уже близко");

  setText("withdraw-balance", usd(b.usd));
  setText("withdraw-balance-stars", "≈ " + stars(b.stars));
  setText("cases-balance", stars(b.stars));
  setText("sheet-balance", stars(b.stars));
  setText("roulette-balance", stars(b.stars));
  setText("starfield-balance", stars(b.stars));
  setText("tasks-progress", "Выполнено за всё время: " + (u.completed || 0));

  setText("profile-name", u.username || "Пользователь");
  setText("profile-id", "ID: " + (u.id || "—"));
  setText("profile-earned", usd(b.lifetime_usd));
  setText("profile-earned-stars", "≈ " + stars(b.lifetime_stars));
  setText("profile-completed", String(u.completed || 0));
  $("ref-link").value = u.referral_link || "https://t.me/UAMONEY_BOT";
}

function taskNode(task) {
  const item = document.createElement("div");
  item.className = "task";
  const icon = document.createElement("div");
  icon.className = "task-icon";
  icon.textContent = task.icon || "📢";
  const body = document.createElement("div");
  body.className = "task-body";
  const title = document.createElement("div");
  title.className = "task-title";
  title.textContent = task.title;
  const meta = document.createElement("div");
  meta.className = "task-meta";
  meta.textContent = (task.type || "Задание") + " · награда через 12 ч";
  body.appendChild(title);
  body.appendChild(meta);
  const reward = document.createElement("div");
  reward.className = "task-reward";
  reward.textContent = "+" + Number(task.reward_usd || 0).toFixed(3) + " $";
  const small = document.createElement("div");
  small.className = "sub";
  small.textContent = "≈ +" + Number(task.reward_stars || 0).toFixed(2) + " ⭐";
  reward.appendChild(small);
  const btn = document.createElement("button");
  btn.className = "task-btn";
  btn.textContent = task.button_text || "Скоро";
  btn.disabled = Boolean(task.demo);
  btn.onclick = () => {
    if (task.demo) { toast(task.title + ": задания пока в разработке"); return; }
    const url = task.url;
    if (!url) { toast("Ссылка для задания не найдена"); return; }
    if (tg && url.startsWith("https://t.me/")) tg.openTelegramLink(url);
    else if (tg) tg.openLink(url);
    else window.open(url, "_blank");
  };
  item.appendChild(icon);
  item.appendChild(body);
  item.appendChild(reward);
  item.appendChild(btn);
  return item;
}

function renderTasks(data) {
  const list = $("tasks-list");
  list.innerHTML = "";
  const groups = data.groups || [];
  if (groups.length) {
    groups.forEach(group => {
      const heading = document.createElement("div");
      heading.className = "section-title";
      heading.textContent = group.label + " · " + (group.tasks || []).length;
      list.appendChild(heading);
      if (!(group.tasks || []).length) {
        const empty = document.createElement("div");
        empty.className = "sub";
        empty.textContent = "Нет доступных заданий";
        list.appendChild(empty);
      }
      (group.tasks || []).forEach(task => list.appendChild(taskNode(task)));
    });
  } else {
    (data.tasks || []).forEach(task => list.appendChild(taskNode(task)));
  }

  const total = (data.tasks || []).length;
  setText("tasks-count", total ? String(total) : "0");

  const homeList = $("home-tasks-list");
  if (homeList) {
    homeList.innerHTML = "";
    (data.tasks || []).slice(0, 5).forEach(task => homeList.appendChild(taskNode(task)));
  }
  setText("home-tasks-count", total ? String(total) : "0");
  const completed = (profile && profile.user && profile.user.completed) || 0;
  setText("home-tasks-progress", "Сделано " + completed + " из 15 · новые через 3 ч");

  if ((data.warnings || []).length) {
    setText("tasks-warning", "Некоторые сервисы временно недоступны: " + data.warnings.join("; "));
  }
}

function renderHistory(data) {
  const list = $("history-list");
  if (!list) return;
  list.innerHTML = "";
  const items = data.items || [];
  setText("history-count", String(data.count || items.length));
  setText("history-summary", items.length ? "Последние операции" : (data.message || "Операций пока нет"));

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "sub";
    empty.textContent = data.message || "Операций пока нет";
    list.appendChild(empty);
    return;
  }

  items.forEach(item => {
    const row = document.createElement("div");
    row.className = "history-item";

    const body = document.createElement("div");
    body.className = "history-body";
    const title = document.createElement("div");
    title.className = "history-title";
    title.textContent = item.title;
    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.textContent = (item.date || "") + (item.status ? " · " + item.status : "");
    body.appendChild(title);
    body.appendChild(meta);

    const amount = document.createElement("div");
    amount.className = "history-amount";
    const isZero = Math.abs(Number(item.amount_usd || 0)) < 1e-9;
    amount.textContent = isZero
      ? (item.status || "—")
      : item.sign + " " + Math.abs(Number(item.amount_usd || 0)).toFixed(3) + " $";
    const small = document.createElement("div");
    small.className = "sub";
    small.textContent = isZero ? "" : item.sign + " " + Number(item.amount_stars || 0).toFixed(2) + " ⭐";
    amount.appendChild(small);

    row.appendChild(body);
    row.appendChild(amount);
    list.appendChild(row);
  });
}

function renderDaily(data) {
  daily = data;
  setText("daily-streak", "серия " + Number(data.streak || 0));
  setText(
    "daily-progress",
    Number(data.completed_today || 0) + " из " + Number(data.goal || 3) + " заданий сегодня"
  );
  const strip = $("daily-days");
  if (strip) {
    strip.innerHTML = "";
    (data.days || []).forEach(day => {
      const chip = document.createElement("div");
      chip.className = "day-chip" + (day.claimed ? " done" : "");
      chip.textContent = Number(day.day) + " день";
      strip.appendChild(chip);
    });
  }
  const reward = data.today_reward || {};
  setText(
    "daily-reward",
    "Что внутри сегодня: " + Number(reward.usd || 0).toFixed(2) + " $ · " +
      usd(reward.usd) + " ≈ " + Number(reward.stars || 0).toFixed(2) + " ⭐"
  );
  const btn = $("daily-claim");
  if (btn) {
    btn.disabled = !data.unlocked || Boolean(data.claimed_today);
    btn.textContent = data.claimed_today
      ? "Уже открыт сегодня"
      : data.unlocked
        ? "Открыть сундук"
        : "Нужно " + Number(data.goal || 3) + " задания";
    btn.onclick = claimDaily;
  }
}

async function claimDaily() {
  const btn = $("daily-claim");
  if (btn) btn.disabled = true;
  try {
    const result = await api("/daily/claim", { method: "POST", body: "{}" });
    toast(result.message || "Сундук открыт");
    api("/daily").then(renderDaily).catch(() => {});
    api("/profile").then(renderProfile).catch(() => {});
    api("/history").then(renderHistory).catch(() => {});
  } catch (error) {
    toast(error.message);
    if (btn) btn.disabled = false;
  }
}

function makeCaseVisual(caseId, size) {
  const wrap = document.createElement("div");
  wrap.className = "case-visual case-" + caseId + " " + (size || "cv-small");

  const glow = document.createElement("div");
  glow.className = "cv-glow";
  const rays = document.createElement("div");
  rays.className = "cv-rays";
  const body = document.createElement("div");
  body.className = "cv-body";
  const lid = document.createElement("div");
  lid.className = "cv-lid";
  const lock = document.createElement("div");
  lock.className = "cv-lock";

  wrap.appendChild(glow);
  wrap.appendChild(rays);
  wrap.appendChild(body);
  wrap.appendChild(lid);
  wrap.appendChild(lock);

  for (let i = 0; i < 12; i++) {
    const spark = document.createElement("div");
    spark.className = "cv-spark";
    const angle = (Math.PI * 2 * i) / 12;
    const distance = 55 + Math.random() * 45;
    spark.style.setProperty("--sx", Math.cos(angle) * distance + "px");
    spark.style.setProperty("--sy", Math.sin(angle) * distance + "px");
    spark.style.animationDelay = (Math.random() * 0.25).toFixed(2) + "s";
    wrap.appendChild(spark);
  }
  return wrap;
}

function renderCases(data) {
  cases = data.cases || [];
  const list = $("case-list");
  if (list) {
    list.innerHTML = "";
    cases.forEach(item => {
      const row = document.createElement("div");
      row.className = "case-row case-home-row";
      row.appendChild(makeCaseVisual(item.id, "cv-small"));
      const max = document.createElement("div");
      max.className = "case-max";
      max.textContent = "до " + Number(item.max_stars || 0).toFixed(0) + " ⭐";
      const title = document.createElement("div");
      title.className = "case-title";
      title.textContent = item.title;
      const btn = document.createElement("button");
      btn.className = "case-btn";
      btn.textContent = Number(item.cost_stars || 0).toFixed(1) + " ⭐";
      btn.onclick = () => openCaseDetail(item.id);
      row.appendChild(max);
      row.appendChild(title);
      row.appendChild(btn);
      list.appendChild(row);
    });
  }
  renderCasesGrid();
  renderWinsTicker(data);
}

function renderCasesGrid() {
  const grid = $("cases-grid");
  if (!grid) return;
  grid.innerHTML = "";
  cases.forEach(item => {
    const card = document.createElement("div");
    card.className = "case-card";
    card.appendChild(makeCaseVisual(item.id, "cv-medium"));

    const top = document.createElement("div");
    top.className = "case-card-top";
    top.textContent = item.title;
    const badge = document.createElement("span");
    badge.className = "chip";
    badge.textContent = Number(item.cost_stars || 0).toFixed(1) + " ⭐";
    top.appendChild(badge);

    const sub1 = document.createElement("div");
    sub1.className = "case-card-sub";
    sub1.textContent =
      (item.prizes || []).length + " призов · возврат " +
      Number(item.return_percent || 0).toFixed(0) + "%";

    const sub2 = document.createElement("div");
    sub2.className = "case-card-sub";
    sub2.textContent = "главный — " + Number(item.max_stars || 0).toFixed(0) + " ⭐";

    const btn = document.createElement("button");
    btn.className = "wide-btn case-card-btn";
    btn.textContent = "для игры";
    btn.onclick = () => openCaseDetail(item.id);

    card.appendChild(top);
    card.appendChild(sub1);
    card.appendChild(sub2);
    card.appendChild(btn);
    grid.appendChild(card);
  });
}

function renderWinsTicker(data) {
  const box = $("wins-ticker");
  if (!box) return;
  const wins = data.recent_wins || [];
  const total = Number(data.wins_total_stars || 0).toFixed(0);
  const count = Number(data.wins_count || 0);
  let text = "Сейчас выигрывают за сутки: вывели " + total + " ⭐ и " + count + " призов";
  if (wins.length) {
    text += " · " + wins.slice(0, 3).map(item =>
      item.case + " +" + Number(item.stars || 0).toFixed(1) + " ⭐"
    ).join(" · ");
  }
  box.textContent = text;
}

function openCasesScreen() {
  const screen = $("cases-overlay");
  const detail = $("case-detail-overlay");
  if (detail) detail.classList.add("hidden");
  if (screen) screen.classList.remove("hidden");
}

function closeCasesScreen() {
  const screen = $("cases-overlay");
  const detail = $("case-detail-overlay");
  if (screen) screen.classList.add("hidden");
  if (detail) detail.classList.add("hidden");
}

function openCaseDetail(caseId) {
  const item = (cases || []).find(entry => entry.id === caseId);
  if (!item) return;
  const screen = $("cases-overlay");
  const detail = $("case-detail-overlay");
  const body = $("detail-body");
  if (!detail || !body) return;
  if (screen) screen.classList.add("hidden");
  detail.classList.remove("hidden");
  setText("detail-title", "Кейс «" + item.title + "»");
  setText("detail-cost", Number(item.cost_stars || 0).toFixed(1) + " ⭐");
  body.innerHTML = "";
  const detailVisual = makeCaseVisual(item.id, "cv-large");
  detailVisual.style.margin = "0 auto 10px";
  body.appendChild(detailVisual);

  const stat = document.createElement("div");
  stat.className = "case-big-stat";
  const prizes = item.prizes || [];
  const featured = prizes.length
    ? prizes[Math.floor(prizes.length / 2)].stars
    : item.expected_stars;
  const statValue = document.createElement("div");
  statValue.className = "case-big-value";
  statValue.textContent = Number(featured || 0).toFixed(2) + " ⭐";
  const statMeta = document.createElement("div");
  statMeta.className = "sub";
  statMeta.textContent =
    prizes.length + " призов · возврат " + Number(item.return_percent || 0).toFixed(0) + "% · 0/0";
  stat.appendChild(statValue);
  stat.appendChild(statMeta);
  body.appendChild(stat);

  const summary = document.createElement("div");
  summary.className = "case-summary";
  summary.textContent =
    (item.prizes || []).length + " призов · возврат " +
    Number(item.return_percent || 0).toFixed(0) + "% · главный — " +
    Number(item.max_stars || 0).toFixed(0) + " ⭐";
  body.appendChild(summary);

  const hint = document.createElement("div");
  const balanceStars = (profile && profile.balance && profile.balance.stars) || 0;
  const need = Number(item.cost_stars || 0) - Number(balanceStars || 0);
  hint.className = "sub case-hint";
  hint.textContent = need > 0
    ? "Добрать " + Number(need).toFixed(2) + " ⭐ на заданиях"
    : "Выигрыш сразу на балансе · код раунда сохраняется";
  body.appendChild(hint);

  const tableTitle = document.createElement("div");
  tableTitle.className = "section-title";
  tableTitle.textContent = "Внутри " + prizes.length + " призов шанс у каждого";
  body.appendChild(tableTitle);

  const table = document.createElement("div");
  table.className = "prize-table";
  (item.prizes || []).forEach(prize => {
    const row = document.createElement("div");
    row.className = "prize-row";
    const label = document.createElement("div");
    label.className = "prize-label";
    label.textContent = Number(prize.stars || 0).toFixed(2) + " ⭐";
    const chance = document.createElement("div");
    chance.className = "prize-chance";
    chance.textContent = (Number(prize.chance || 0) * 100).toFixed(2) + "%";
    const bar = document.createElement("div");
    bar.className = "prize-bar";
    const fill = document.createElement("div");
    fill.className = "prize-bar-fill";
    fill.style.width = Math.min(100, Math.sqrt(Number(prize.chance || 0)) * 100) + "%";
    bar.appendChild(fill);
    row.appendChild(label);
    row.appendChild(bar);
    row.appendChild(chance);
    table.appendChild(row);
  });
  body.appendChild(table);

  const fair = document.createElement("div");
  fair.className = "fineprint";
  fair.textContent = "Шансы честные — ровно те, что написаны.";
  body.appendChild(fair);

  const play = document.createElement("button");
  play.className = "wide-btn primary";
  if (need > 0) {
    play.textContent = "для игры";
    play.onclick = () => { closeCasesScreen(); switchTab("tasks"); };
  } else {
    play.textContent = "Открыть за " + Number(item.cost_stars || 0).toFixed(1) + " ⭐";
    play.onclick = () => openCase(item.id, play, "detail-result");
  }
  body.appendChild(play);

  const result = document.createElement("div");
  result.id = "detail-result";
  result.className = "case-result hidden";
  body.appendChild(result);
}

async function openCase(caseId, button, resultId) {
  const overlay = $("case-open-overlay");
  const visualBox = $("opening-visual");
  const prizeBox = $("opening-prize");
  const codeBox = $("opening-code");
  const closeBtn = $("opening-close");
  if (!overlay || !visualBox || !prizeBox || !codeBox) return;

  button.disabled = true;
  prizeBox.textContent = "";
  prizeBox.classList.add("hidden");
  codeBox.textContent = "Открываем…";
  visualBox.innerHTML = "";
  const visual = makeCaseVisual(caseId, "cv-xl");
  visualBox.appendChild(visual);
  overlay.classList.remove("hidden");
  setTimeout(() => visual.classList.add("case-opening"), 40);

  const showResult = result => {
    visual.classList.remove("case-opening");
    prizeBox.textContent = "+" + Number(result.prize_stars || 0).toFixed(2) + " ⭐";
    prizeBox.classList.remove("hidden");
    codeBox.textContent = usd(result.prize_usd) + " · код раунда " + result.code;
    const resultBox = $(resultId || "case-result");
    if (resultBox) {
      resultBox.classList.remove("hidden");
      resultBox.innerHTML = "";
      const title = document.createElement("div");
      title.className = "case-win";
      title.textContent = "+" + Number(result.prize_stars || 0).toFixed(2) + " ⭐";
      const detail = document.createElement("div");
      detail.className = "sub";
      detail.textContent = usd(result.prize_usd) + " · код раунда " + result.code;
      resultBox.appendChild(title);
      resultBox.appendChild(detail);
    }
    closeBtn.onclick = () => overlay.classList.add("hidden");
  };

  try {
    const [result] = await Promise.all([
      api("/cases/open", {
        method: "POST",
        body: JSON.stringify({ case_id: caseId }),
      }),
      new Promise(resolve => setTimeout(resolve, 1100)),
    ]);
    showResult(result);
    toast(result.message || "Кейс открыт");
    api("/profile").then(renderProfile).catch(() => {});
    api("/history").then(renderHistory).catch(() => {});
    api("/cases").then(renderCases).catch(() => {});
  } catch (error) {
    overlay.classList.add("hidden");
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderRoulette(data) {
  roulette = data;
  const list = $("roulette-list");
  if (!list) return;
  list.innerHTML = "";
  setText(
    "roulette-info",
    data.sectors + " секторов · возврат " + data.return_percent + "% · максимум x" + data.max_multiplier
  );

  (data.bets || []).forEach(item => {
    const row = document.createElement("div");
    row.className = "case-row";
    const info = document.createElement("div");
    info.className = "case-info";
    const title = document.createElement("div");
    title.className = "case-title";
    title.textContent = "Ставка " + Number(item.stars || 0).toFixed(2) + " ⭐";
    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.textContent = "до " + Number(item.max_win_stars || 0).toFixed(2) + " ⭐ · " + usd(item.usd);
    info.appendChild(title);
    info.appendChild(meta);

    const btn = document.createElement("button");
    btn.className = "case-btn spin-btn";
    btn.dataset.bet = String(item.stars);
    btn.textContent = "Крутить";
    btn.onclick = () => spinRoulette(item.stars, btn);

    row.appendChild(info);
    row.appendChild(btn);
    list.appendChild(row);
  });
}

async function spinRoulette(betStars, button) {
  const resultBox = $("roulette-result");
  if (!resultBox) return;
  button.disabled = true;
  try {
    const result = await api("/roulette/spin", {
      method: "POST",
      body: JSON.stringify({ bet_stars: betStars }),
    });
    resultBox.classList.remove("hidden");
    resultBox.innerHTML = "";
    const title = document.createElement("div");
    title.className = "case-win";
    title.textContent = "x" + Number(result.multiplier || 0).toFixed(2);
    const detail = document.createElement("div");
    detail.className = "sub";
    detail.textContent =
      (result.payout_stars ? "+" + Number(result.payout_stars).toFixed(2) + " ⭐" : "ставка сгорела") +
      " · код " + result.code;
    resultBox.appendChild(title);
    resultBox.appendChild(detail);
    toast(result.message || "Спин завершён");
    api("/profile").then(renderProfile).catch(() => {});
    api("/history").then(renderHistory).catch(() => {});
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderStarfield(data) {
  starfield = data;
  setText(
    "starfield-info",
    data.cells + " клеток · " + data.mines + " чёрных дыр · возврат " +
      data.return_percent + "% · максимум x" + data.max_multiplier
  );
  if (data.active) {
    renderStarfieldGame(data.active);
    return;
  }
  const betsBox = $("starfield-bets");
  const gameBox = $("starfield-game");
  if (!betsBox || !gameBox) return;
  gameBox.classList.add("hidden");
  betsBox.innerHTML = "";
  (data.bets || []).forEach(item => {
    const row = document.createElement("div");
    row.className = "case-row";
    const info = document.createElement("div");
    info.className = "case-info";
    const title = document.createElement("div");
    title.className = "case-title";
    title.textContent = "Ставка " + Number(item.stars || 0).toFixed(2) + " ⭐";
    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.textContent = "до " + Number(item.max_win_stars || 0).toFixed(2) + " ⭐ · " + usd(item.usd);
    info.appendChild(title);
    info.appendChild(meta);

    const btn = document.createElement("button");
    btn.className = "case-btn spin-btn";
    btn.textContent = "Играть";
    btn.onclick = () => startStarfield(item.stars, btn);

    row.appendChild(info);
    row.appendChild(btn);
    betsBox.appendChild(row);
  });
}

function renderStarfieldGame(state) {
  const betsBox = $("starfield-bets");
  const gameBox = $("starfield-game");
  const grid = $("starfield-grid");
  if (!betsBox || !gameBox || !grid) return;
  betsBox.innerHTML = "";
  gameBox.classList.remove("hidden");
  grid.innerHTML = "";
  const opened = state.opened || [];
  const active = state.status === "active";

  setText(
    "starfield-status",
    "Ставка " + Number(state.bet_stars || 0).toFixed(2) + " ⭐ · x" +
      Number(state.multiplier || 1).toFixed(2) + " · открыто " + opened.length +
      " · код " + (state.code || "—")
  );

  const cashout = $("starfield-cashout");
  if (cashout) {
    cashout.disabled = !active;
    cashout.textContent = "Забрать " +
      (Number(state.bet_stars || 0) * Number(state.multiplier || 1)).toFixed(2) + " ⭐";
    cashout.onclick = () => cashoutStarfield(state.round_id);
  }

  for (let i = 0; i < Number(state.cells || 25); i++) {
    const cell = document.createElement("button");
    cell.className = "star-cell";
    cell.dataset.index = String(i);
    if (opened.includes(i)) {
      cell.classList.add("revealed");
      cell.textContent = "★";
    } else {
      cell.textContent = "·";
      cell.disabled = !active;
      cell.onclick = () => openStarfieldCell(state.round_id, i, cell);
    }
    grid.appendChild(cell);
  }
}

async function startStarfield(betStars, button) {
  button.disabled = true;
  try {
    const state = await api("/starfield/start", {
      method: "POST",
      body: JSON.stringify({ bet_stars: betStars }),
    });
    renderStarfieldGame(state);
    toast(state.message || "Ставка принята");
    api("/profile").then(renderProfile).catch(() => {});
    api("/history").then(renderHistory).catch(() => {});
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function openStarfieldCell(roundId, index, cell) {
  cell.disabled = true;
  try {
    const result = await api("/starfield/open", {
      method: "POST",
      body: JSON.stringify({ round_id: roundId, index: index }),
    });
    cell.classList.add("revealed");
    cell.textContent = result.hit === "mine" ? "💥" : "★";
    if (result.hit === "mine") {
      cell.classList.add("mine");
      Array.from(document.querySelectorAll(".star-cell")).forEach(el => {
        el.disabled = true;
      });
      const cashout = $("starfield-cashout");
      if (cashout) cashout.disabled = true;
    }
    const state = {
      round_id: result.round_id,
      bet_stars: (starfield && starfield.active && starfield.active.bet_stars) || 0,
      cells: (starfield && starfield.active && starfield.active.cells) || 25,
      opened: result.opened,
      status: result.game_over ? "lost" : "active",
      multiplier: result.multiplier,
      code: result.code,
    };
    starfield.active = state;
    setText("starfield-status", result.message);
    const cashout = $("starfield-cashout");
    if (cashout) {
      cashout.disabled = !result.can_cashout;
      cashout.textContent = "Забрать " +
        (Number(state.bet_stars) * Number(result.multiplier)).toFixed(2) + " ⭐";
    }
    api("/history").then(renderHistory).catch(() => {});
  } catch (error) {
    toast(error.message);
    cell.disabled = false;
  }
}

async function cashoutStarfield(roundId) {
  const btn = $("starfield-cashout");
  if (btn) btn.disabled = true;
  try {
    const result = await api("/starfield/cashout", {
      method: "POST",
      body: JSON.stringify({ round_id: roundId }),
    });
    const box = $("starfield-result");
    if (box) {
      box.classList.remove("hidden");
      box.innerHTML = "";
      const title = document.createElement("div");
      title.className = "case-win";
      title.textContent = "+" + Number(result.payout_stars || 0).toFixed(2) + " ⭐";
      const detail = document.createElement("div");
      detail.className = "sub";
      detail.textContent = usd(result.payout_usd) + " · код " + result.code;
      box.appendChild(title);
      box.appendChild(detail);
    }
    starfield.active = null;
    renderStarfield({ ...starfield, active: null });
    toast(result.message || "Выигрыш получен");
    api("/profile").then(renderProfile).catch(() => {});
    api("/history").then(renderHistory).catch(() => {});
  } catch (error) {
    toast(error.message);
    if (btn) btn.disabled = false;
  }
}

function updateStarButtons() {
  document.querySelectorAll(".preset").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.stars) === selectedStars);
  });
}

function sendToBot(payload) {
  if (!tg || !tg.sendData) { toast("Открой мини-апп через бот"); return; }
  tg.sendData(JSON.stringify(payload));
  toast("Продолжаем в чате с ботом…");
  setTimeout(() => { try { tg.close(); } catch (e) {} }, 250);
}

document.querySelectorAll(".nav-item").forEach(el => {
  el.addEventListener("click", () => switchTab(el.dataset.tab));
});
document.querySelectorAll("[data-go]").forEach(el => {
  el.addEventListener("click", () => switchTab(el.dataset.go));
});

$("check-tasks").addEventListener("click", () => {
  sendToBot({ action: "check_tasks" });
});

const casesAll = $("cases-all");
if (casesAll) casesAll.addEventListener("click", openCasesScreen);
const casesBack = $("cases-back");
if (casesBack) casesBack.addEventListener("click", closeCasesScreen);
const detailBack = $("detail-back");
if (detailBack) detailBack.addEventListener("click", openCasesScreen);
const homeTasksMore = $("home-tasks-more");
if (homeTasksMore) homeTasksMore.addEventListener("click", () => switchTab("tasks"));
const homeFriends = $("home-friends-invite");
if (homeFriends) homeFriends.addEventListener("click", () => switchTab("profile"));

document.querySelectorAll(".seg").forEach(el => {
  el.addEventListener("click", () => {
    withdrawMethod = el.dataset.method;
    document.querySelectorAll(".seg").forEach(x => x.classList.toggle("active", x === el));
    $("stars-block").classList.toggle("hidden", withdrawMethod !== "stars");
    $("usdt-block").classList.toggle("hidden", withdrawMethod !== "usdt");
  });
});

document.querySelectorAll(".preset").forEach(el => {
  el.addEventListener("click", () => { selectedStars = Number(el.dataset.stars); updateStarButtons(); });
});

$("custom-star-btn").addEventListener("click", () => {
  const value = Number($("custom-stars").value);
  if (!Number.isFinite(value) || value < 15) { toast("Минимум 15 ⭐"); return; }
  selectedStars = Math.floor(value);
  updateStarButtons();
  toast("Выбрано " + selectedStars + " ⭐");
});

$("do-withdraw").addEventListener("click", () => {
  if (withdrawMethod === "usdt") { sendToBot({ action: "withdraw_usdt" }); return; }
  const balanceUsd = (profile && profile.balance && profile.balance.usd) || 0;
  const cost = selectedStars * STAR_USD;
  if (cost > balanceUsd + 1e-9) { toast("Недостаточно средств: нужно " + usd(cost)); return; }
  sendToBot({ action: "withdraw_stars", stars: selectedStars });
});

$("copy-ref").addEventListener("click", async () => {
  const value = $("ref-link").value;
  try {
    await navigator.clipboard.writeText(value);
    toast("Ссылка скопирована");
  } catch (e) {
    $("ref-link").select();
    toast("Выделите и скопируйте ссылку");
  }
});

updateStarButtons();
api("/profile")
  .then(renderProfile)
  .catch(error => toast("Не удалось загрузить профиль: " + error.message));
api("/tasks")
  .then(renderTasks)
  .catch(error => toast("Не удалось загрузить задания: " + error.message));
api("/history")
  .then(renderHistory)
  .catch(error => toast("Не удалось загрузить историю: " + error.message));
api("/cases")
  .then(renderCases)
  .catch(error => toast("Не удалось загрузить кейсы: " + error.message));
api("/daily")
  .then(renderDaily)
  .catch(error => toast("Не удалось загрузить сундук: " + error.message));
api("/roulette")
  .then(renderRoulette)
  .catch(error => toast("Не удалось загрузить рулетку: " + error.message));
api("/starfield")
  .then(renderStarfield)
  .catch(error => toast("Не удалось загрузить звёздное поле: " + error.message));
