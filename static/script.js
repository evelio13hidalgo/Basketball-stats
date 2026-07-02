/*
  NBA Stats Hub - frontend logic.

  This file is loaded at the bottom of index.html via
    <script src="/static/script.js"></script>
  By the time it runs, all the HTML elements already exist, so we can safely
  find them with document.getElementById / querySelector.

  The core pattern you'll see repeated is the "fetch / render" loop:
    1. fetch() data from one of OUR /api routes (which talks to ESPN for us),
    2. turn the JSON response into HTML strings,
    3. drop that HTML into a container element on the page.

  We never touch ESPN directly from here - only our own backend. That keeps
  this file simple and means all the messy data-reshaping lives in app.py.
*/

"use strict";

/* ---------------------------------------------------------------------------
   Tab switching
   --------------------------------------------------------------------------- */
// Each tab button has data-tab="scores" (etc.) matching a <section id="scores">.
// Clicking a button hides every section and shows the matching one.
const tabButtons = document.querySelectorAll(".tab-btn");
const tabSections = document.querySelectorAll(".tab-content");

// Track which tabs we've already loaded so we don't refetch every click.
const loadedTabs = new Set();

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;

    // Move the "active" highlight to the clicked button.
    tabButtons.forEach((b) => b.classList.toggle("active", b === btn));
    // Show only the matching section.
    tabSections.forEach((s) => s.classList.toggle("active", s.id === target));

    // Lazy-load each tab's data the first time it's opened.
    if (!loadedTabs.has(target)) {
      loadedTabs.add(target);
      if (target === "standings") loadStandings();
      if (target === "teams") loadTeamsGrid();
      if (target === "titles") loadTitles();
      if (target === "leaders") initLeaders();
      if (target === "players") initPlayerSearch();
      if (target === "compare") initCompare();
    }
    // The favorites strip can change while you're elsewhere (e.g. you starred a
    // player), so refresh it every time the Players tab is opened.
    if (target === "players") renderFavStrip();
  });
});

/* ---------------------------------------------------------------------------
   Small helpers
   --------------------------------------------------------------------------- */

// Format an ISO start time (e.g. "2026-06-24T23:00Z") as a local time like
// "7:00 PM". The browser converts UTC -> the user's timezone automatically.
function formatStartTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// Format an ISO date string ("2026-06-24") as a friendly "Wed, Jun 24".
function formatDayLabel(isoDate) {
  // Append T12:00 so the date isn't shifted by timezone when parsed.
  const d = new Date(isoDate + "T12:00:00");
  return d.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

// Escape user/API text before inserting it as HTML, to avoid breaking the
// markup (and as a good security habit). Team names are safe, but escaping is
// the right default whenever you build HTML from strings.
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/* ---------------------------------------------------------------------------
   Liveliness helpers: animation + charts (shared by several tabs)
   --------------------------------------------------------------------------- */

// Animate a number from 0 up to `target` over a short duration. We use
// requestAnimationFrame (the browser's "call me before the next repaint" hook)
// and an ease-out curve so it decelerates naturally. `opts.format` controls how
// the value is displayed (e.g. add thousands separators).
function countUp(el, target, opts = {}) {
  const duration = opts.duration ?? 900;
  const format = opts.format ?? ((v) => String(Math.round(v)));
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
    el.textContent = format(target * eased);
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = format(target);
  }
  requestAnimationFrame(tick);
}

// A shimmering placeholder block to show while data loads, instead of plain
// "Loading..." text. `lines` controls how many bars to draw.
function skeletonLines(lines = 3) {
  let html = "";
  for (let i = 0; i < lines; i++) {
    const width = 60 + ((i * 17) % 35); // vary widths so it looks organic
    html += `<div class="skeleton skeleton-row" style="width:${width}%"></div>`;
  }
  return html;
}

// Build a bar chart as an inline SVG string. No charting library - just a bit of
// geometry. `items` is [{ value, label, title }]; the SVG scales to its
// container width via the viewBox. Bars animate upward (see .chart-bar in CSS),
// staggered by an increasing animation-delay.
function barChartSVG(items, opts = {}) {
  const color = opts.color || "var(--accent)";
  const H = opts.height || 170;
  const padTop = 12;
  const padBottom = 22;
  const slot = 26; // horizontal space per bar (viewBox units)
  const barW = 15;
  const chartH = H - padTop - padBottom;
  const W = Math.max(items.length * slot, slot);
  const max = Math.max(...items.map((d) => d.value || 0), 1);

  // Show roughly 10 x-axis labels max, evenly spaced, so they don't overlap.
  const labelStep = Math.ceil(items.length / 10);

  let bars = "";
  let labels = "";
  items.forEach((d, i) => {
    const barH = ((d.value || 0) / max) * chartH;
    const x = i * slot + (slot - barW) / 2;
    const y = padTop + (chartH - barH);
    bars += `<rect class="chart-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}"
      width="${barW}" height="${Math.max(barH, 0).toFixed(1)}" rx="2"
      style="fill:${color};animation-delay:${i * 30}ms">
      <title>${esc(d.title || "")}</title></rect>`;
    if (i % labelStep === 0) {
      labels += `<text class="chart-xlabel" x="${(x + barW / 2).toFixed(1)}"
        y="${H - 7}" text-anchor="middle">${esc(d.label ?? "")}</text>`;
    }
  });

  const baselineY = padTop + chartH;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">
    <line class="chart-baseline" x1="0" y1="${baselineY}" x2="${W}" y2="${baselineY}"/>
    ${bars}${labels}</svg>`;
}

// Build a multi-series line chart (used to overlay two players when comparing).
// `series` is [{ name, color, values:[number,...] }] where index 0 = career
// year 1. Lines draw themselves in via the .spark-line CSS animation.
function lineChartSVG(series, opts = {}) {
  const W = 520;
  const H = 240;
  const padL = 34;
  const padR = 14;
  const padT = 14;
  const padB = 26;
  const maxLen = Math.max(...series.map((s) => s.values.length), 1);
  const maxY = Math.max(
    ...series.flatMap((s) => s.values),
    1
  );

  // Map a (career-year index, value) pair to pixel coordinates in the viewBox.
  const xAt = (i) =>
    padL + (maxLen <= 1 ? 0 : (i / (maxLen - 1)) * (W - padL - padR));
  const yAt = (v) => padT + (1 - v / maxY) * (H - padT - padB);

  // Horizontal gridlines + y labels at 0 and the max.
  let grid = "";
  [0, maxY].forEach((v) => {
    const y = yAt(v);
    grid += `<line class="chart-baseline" x1="${padL}" y1="${y.toFixed(1)}"
      x2="${W - padR}" y2="${y.toFixed(1)}"/>
      <text class="chart-ylabel" x="${padL - 6}" y="${(y + 3).toFixed(1)}"
        text-anchor="end">${Math.round(v)}</text>`;
  });

  let lines = "";
  series.forEach((s) => {
    const pts = s.values
      .map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`)
      .join(" ");
    lines += `<polyline class="spark-line" pathLength="1" points="${pts}"
      style="stroke:${s.color}"/>`;
    // End-of-line dot.
    const li = s.values.length - 1;
    if (li >= 0) {
      lines += `<circle class="spark-dot" cx="${xAt(li).toFixed(1)}"
        cy="${yAt(s.values[li]).toFixed(1)}" r="4" style="fill:${s.color}"/>`;
    }
  });

  const xlabel = `<text class="chart-xlabel" x="${(W / 2).toFixed(
    1
  )}" y="${H - 4}" text-anchor="middle">Season of career &rarr;</text>`;

  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${grid}${lines}${xlabel}</svg>`;
}

/* ---------------------------------------------------------------------------
   Favorites (saved in the browser via localStorage)
   --------------------------------------------------------------------------- */
// localStorage persists across page reloads on this device/browser. We store a
// small array of { id, name } so the favorites strip can show names without an
// extra fetch.
const FAV_KEY = "nba_favorites";

function getFavorites() {
  try {
    return JSON.parse(localStorage.getItem(FAV_KEY)) || [];
  } catch {
    return [];
  }
}

function isFavorite(id) {
  return getFavorites().some((f) => f.id === id);
}

function toggleFavorite(id, name, btn) {
  let list = getFavorites();
  list = isFavorite(id)
    ? list.filter((f) => f.id !== id)
    : [...list, { id, name }];
  localStorage.setItem(FAV_KEY, JSON.stringify(list));
  // Reflect the new state on the star that was clicked...
  if (btn) {
    const now = isFavorite(id);
    btn.classList.toggle("active", now);
    btn.textContent = now ? "★" : "☆"; // ★ / ☆
  }
  renderFavStrip(); // ...and refresh the quick-access chips.
}

function renderFavStrip() {
  const strip = document.getElementById("fav-strip");
  if (!strip) return;
  const favs = getFavorites();
  if (!favs.length) {
    strip.classList.add("hidden");
    strip.innerHTML = "";
    return;
  }
  strip.classList.remove("hidden");
  strip.innerHTML =
    `<span class="fav-strip-label">★ Favorites:</span>` +
    favs
      .map(
        (f) =>
          `<button class="fav-chip" onclick="openPlayer('${esc(
            f.id
          )}')">${esc(f.name)}</button>`
      )
      .join("");
}

/* ---------------------------------------------------------------------------
   Career math + cross-tab navigation
   --------------------------------------------------------------------------- */
// Reduce a list of season rows to career figures: weighted per-game averages
// (weighted by games so short seasons count less), career point total, peak
// scoring season, and season count.
function careerSummary(seasons) {
  const games = seasons.reduce((a, s) => a + (s.games || 0), 0) || 1;
  const weighted = (key) =>
    seasons.reduce((a, s) => a + (s[key] || 0) * (s.games || 0), 0) / games;
  return {
    seasons: seasons.length,
    games,
    ppg: weighted("points"),
    rpg: weighted("rebounds"),
    apg: weighted("assists"),
    totalPts: seasons.reduce((a, s) => a + (s.points || 0) * (s.games || 0), 0),
    peak: seasons.reduce((m, s) => Math.max(m, s.points || 0), 0),
  };
}

// Jump to the Players tab and open a specific player. Used by the leaders
// boards, the legend spotlight, and the favorites chips.
function openPlayer(id) {
  document.querySelector('.tab-btn[data-tab="players"]').click();
  loadPlayer(id);
}

/* ---------------------------------------------------------------------------
   Scores tab
   --------------------------------------------------------------------------- */
const scoresGrid = document.getElementById("scores-grid");
const scoresTitle = document.getElementById("scores-title");

// Handle for the auto-refresh timer so we can cancel/restart it cleanly.
let liveRefreshTimer = null;

// Build the HTML for a single team row inside a game card.
function teamRowHTML(team, isLoser, showScore) {
  // Show the score only once the game has started; before tip-off show "-".
  const scoreText = showScore ? esc(team.score || "0") : "-";
  return `
    <div class="team-row ${isLoser ? "loser" : ""}">
      <img class="team-logo" src="${esc(team.logo)}" alt="${esc(team.name)} logo"
           onerror="this.style.visibility='hidden'">
      <div class="team-meta">
        <div class="team-name">${esc(team.name)}</div>
        <div class="team-record">${esc(team.record)}</div>
      </div>
      <div class="team-score">${scoreText}</div>
    </div>
  `;
}

// Build the colored status badge (LIVE / FINAL / start time) for a game.
function statusBadgeHTML(game) {
  if (game.isLive) {
    return `<span class="status-badge live"><span class="dot"></span>${esc(
      game.statusDetail || "Live"
    )}</span>`;
  }
  if (game.isFinal) {
    return `<span class="status-badge final">Final</span>`;
  }
  // Scheduled: show local tip-off time.
  return `<span class="status-badge scheduled">${esc(
    formatStartTime(game.startTime)
  )}</span>`;
}

// Build the full HTML for one game card.
function gameCardHTML(game) {
  const showScore = game.isLive || game.isFinal;

  // On a finished game, mark the team with the lower score as the loser so we
  // can dim it. parseInt turns the string scores into numbers for comparison.
  let homeLoser = false;
  let awayLoser = false;
  if (game.isFinal) {
    const homeScore = parseInt(game.home.score, 10) || 0;
    const awayScore = parseInt(game.away.score, 10) || 0;
    homeLoser = homeScore < awayScore;
    awayLoser = awayScore < homeScore;
  }

  return `
    <div class="game-card fade-in-up ${game.isLive ? "is-live" : ""}">
      <div class="game-card-top">
        ${statusBadgeHTML(game)}
      </div>
      ${teamRowHTML(game.away, awayLoser, showScore)}
      ${teamRowHTML(game.home, homeLoser, showScore)}
      <div class="game-card-footer">${esc(game.status)}</div>
    </div>
  `;
}

// Render a list of games into the grid (or an empty message if none).
function renderGames(games) {
  scoresGrid.innerHTML = games.map(gameCardHTML).join("");
}

// MAIN entry point for the Scores tab. Called on page load, by the Refresh
// button (see index.html onclick="loadScores()"), and by the auto-refresh.
async function loadScores() {
  // Stop any existing auto-refresh; we'll restart it only if there are live
  // games after this load.
  stopLiveRefresh();

  try {
    // 1. Fetch today's games from OUR backend.
    const res = await fetch("/api/scores");
    const data = await res.json();

    if (data.error) {
      showScoresError(data.error);
      return;
    }

    if (data.games.length > 0) {
      // We have games today - render them.
      scoresTitle.textContent = "Today's Games";
      renderGames(data.games);

      // If any game is live, poll for fresh scores every 30 seconds.
      if (data.games.some((g) => g.isLive)) {
        startLiveRefresh();
      }
    } else {
      // 2. No games today (totally normal in the offseason!). Instead of a
      //    blank page, show a lively "home": a featured legend + all-time
      //    leaders, followed by the next scheduled games if there are any.
      scoresTitle.textContent = "NBA Stats Hub";
      await renderHome();
    }
  } catch (err) {
    // Network error reaching our own server, bad JSON, etc.
    showScoresError(err.message);
  }
}

/* The offseason "home" --------------------------------------------------- */
// Shown on the Scores tab whenever there are no games today. Combines a
// featured-legend spotlight + all-time scoring leaders + any upcoming games, so
// the landing page is never blank even in July.
async function renderHome() {
  // Show a skeleton immediately so the page feels responsive while we fetch.
  scoresGrid.innerHTML = `
    <div class="spotlight">
      <div class="spotlight-grid">
        <div class="spotlight-panel">${skeletonLines(4)}</div>
        <div class="spotlight-panel">${skeletonLines(5)}</div>
      </div>
    </div>`;

  // Fetch the three pieces in parallel - they don't depend on each other.
  const [legend, leaders, upcoming] = await Promise.all([
    fetch("/api/legends/random").then((r) => r.json()).catch(() => null),
    fetch("/api/leaders/all-time?stat=points").then((r) => r.json()).catch(() => null),
    fetch("/api/upcoming").then((r) => r.json()).catch(() => null),
  ]);

  let html = `
    <div class="spotlight">
      <div class="spotlight-grid">
        <div class="spotlight-panel" id="legend-panel">
          ${legend && legend.legend ? legendCardHTML(legend.legend) : "<div class='loading'>No legend.</div>"}
        </div>
        <div class="spotlight-panel">
          <div class="spotlight-head">
            <span class="spotlight-eyebrow">All-Time Scoring</span>
          </div>
          <div id="home-leaders" class="leaders-grid"></div>
        </div>
      </div>
    </div>`;

  // Append upcoming games (if the lookahead found any).
  if (upcoming && upcoming.found) {
    html += `<div class="day-heading">Upcoming Games</div>`;
    upcoming.days.forEach((day) => {
      html += `<div class="day-heading">${esc(formatDayLabel(day.date))}</div>`;
      html += day.games.map(gameCardHTML).join("");
    });
  }

  scoresGrid.innerHTML = html;

  // Fill the leaders board (top 5 for the compact home version) with animation.
  if (leaders && leaders.leaders) {
    renderLeaderBoard(
      document.getElementById("home-leaders"),
      leaders.leaders.slice(0, 5),
      "PTS"
    );
  }
}

// Basketball-Reference hosts a headshot photo for nearly every player who
// ever played - even 1950s guys - at a predictable URL built from the SAME
// player_id our database uses (it's Basketball-Reference data, after all).
// The visitor's browser loads the image straight from their server.
function headshotURL(playerId) {
  return `https://www.basketball-reference.com/req/202106291/images/headshots/${playerId}.jpg`;
}

// An <img> for a player photo that degrades gracefully: if the photo doesn't
// exist (or their server is down), onerror swaps in a basketball placeholder
// of the same size so the layout never jumps.
function playerPhotoHTML(playerId, cls) {
  return `<img class="${cls}" src="${esc(headshotURL(playerId))}" alt="" loading="lazy"
    onerror="this.outerHTML='<span class=\\'${cls} player-photo-blank\\'>&#127936;</span>'">`;
}

// One featured Hall-of-Famer card (name, career line, peak season, links).
function legendCardHTML(l) {
  const best = l.best_season;
  return `
    <div class="spotlight-head">
      <span class="spotlight-eyebrow">Featured Legend</span>
      <button class="shuffle-btn" onclick="shuffleLegend(this)">&#127922; Shuffle</button>
    </div>
    <div class="legend-id-row">
      ${playerPhotoHTML(l.player_id, "player-photo")}
      <div>
        <div class="legend-name">${esc(l.name)} <span class="hof-badge">HOF</span></div>
        <div class="legend-years">${esc(l.from_year)}&ndash;${esc(l.to_year)} &middot; ${esc(
    l.seasons_count
  )} seasons</div>
      </div>
    </div>
    <div class="legend-statline">
      <div class="ls"><div class="ls-num">${esc(l.career_ppg)}</div><div class="ls-lab">PPG</div></div>
      <div class="ls"><div class="ls-num">${esc(l.career_rpg)}</div><div class="ls-lab">RPG</div></div>
      <div class="ls"><div class="ls-num">${esc(l.career_apg)}</div><div class="ls-lab">APG</div></div>
    </div>
    ${
      best
        ? `<div class="legend-peak">Peak: <strong>${esc(best.points)} PPG</strong> in ${esc(
            seasonLabel(best.season)
          )} (${esc(best.team)})</div>`
        : ""
    }
    <span class="legend-link" onclick="openPlayer('${esc(
      l.player_id
    )}')">View full career &rarr;</span>`;
}

// Reroll the featured legend (just the one panel, not the whole page).
async function shuffleLegend(btn) {
  btn.classList.add("spin");
  try {
    const data = await fetch("/api/legends/random").then((r) => r.json());
    if (data && data.legend) {
      document.getElementById("legend-panel").innerHTML = legendCardHTML(
        data.legend
      );
    }
  } catch {
    /* ignore - keep the current legend on a hiccup */
  }
}

function showScoresError(message) {
  scoresGrid.innerHTML = `
    <div class="error-state">
      <strong>Couldn't load games.</strong>
      <div>${esc(message)}</div>
    </div>`;
}

/* Auto-refresh for live games -------------------------------------------- */
// setInterval calls loadScores() every 30s. We keep the timer id so we can
// clearInterval() it when we leave a live state or reload.
function startLiveRefresh() {
  liveRefreshTimer = setInterval(loadScores, 30000);
}

function stopLiveRefresh() {
  if (liveRefreshTimer) {
    clearInterval(liveRefreshTimer);
    liveRefreshTimer = null;
  }
}

/* ---------------------------------------------------------------------------
   Standings tab
   --------------------------------------------------------------------------- */
function standingsTableHTML(rows) {
  if (!rows || rows.length === 0) {
    return `<div class="loading">No standings available.</div>`;
  }

  const body = rows
    .map(
      (t, i) => `
      <tr class="fade-in-up" style="animation-delay:${i * 25}ms">
        <td class="num">${i + 1}</td>
        <td>
          <div class="team-cell">
            <img class="mini-logo" src="${esc(t.logo)}" alt=""
                 onerror="this.style.visibility='hidden'">
            ${esc(t.name)}
          </div>
        </td>
        <td class="num">${esc(t.wins)}</td>
        <td class="num">${esc(t.losses)}</td>
        <td class="num">
          ${esc(t.winPercent)}
          <div class="leader-bar-track" style="width:64px;margin:5px auto 0">
            <div class="leader-bar-fill"
                 data-width="${(parseFloat(t.winPercent) || 0) * 100}"
                 style="width:0"></div>
          </div>
        </td>
        <td class="num">${esc(t.streak)}</td>
      </tr>`
    )
    .join("");

  return `
    <table class="standings-table">
      <thead>
        <tr>
          <th>#</th><th>Team</th><th>W</th><th>L</th><th>PCT</th><th>STRK</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

// After a table is inserted, grow its win%/leader bars from 0 to their real
// width. We set the width on the NEXT animation frame so the browser registers
// the change as a transition (rather than just rendering the final width).
function animateBars(container) {
  if (!container) return;
  requestAnimationFrame(() => {
    container.querySelectorAll(".leader-bar-fill").forEach((el) => {
      el.style.width = `${el.dataset.width || 0}%`;
    });
  });
}

async function loadStandings() {
  const east = document.getElementById("east-standings");
  const west = document.getElementById("west-standings");
  east.innerHTML = west.innerHTML = skeletonLines(6);
  try {
    const res = await fetch("/api/standings");
    const data = await res.json();
    if (data.error) {
      east.innerHTML = west.innerHTML = `<div class="error-state">${esc(
        data.error
      )}</div>`;
      return;
    }
    east.innerHTML = standingsTableHTML(data.east);
    west.innerHTML = standingsTableHTML(data.west);
    animateBars(east);
    animateBars(west);
  } catch (err) {
    east.innerHTML = west.innerHTML = `<div class="error-state">${esc(
      err.message
    )}</div>`;
  }
}

/* ---------------------------------------------------------------------------
   Teams tab - team directory, then per-team roster + franchise history
   --------------------------------------------------------------------------- */
const teamsView = document.getElementById("teams-view");

// The 30-team list changes roughly never, so fetch it once per page load and
// keep it around. It also doubles as our lookup for logos/colors when we
// render a single team's page.
let teamsCache = null;

async function loadTeamsGrid() {
  teamsView.innerHTML = `<div class="loading">Loading teams...</div>`;
  try {
    if (!teamsCache) {
      const res = await fetch("/api/teams");
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      teamsCache = data.teams;
    }
    // One tile per team. Each tile carries its own team color as a CSS custom
    // property (--team-color) so the stylesheet can use it for accents without
    // us writing 30 color rules.
    teamsView.innerHTML =
      `<div class="teams-grid">` +
      teamsCache
        .map(
          (t, i) => `
        <button class="team-tile fade-in-up"
          style="--team-color:#${esc(t.color)}; animation-delay:${i * 20}ms"
          onclick="openTeam('${esc(t.id)}')">
          <img class="team-tile-logo" src="${esc(t.logo)}" alt="" loading="lazy">
          <span class="team-tile-loc">${esc(t.location)}</span>
          <span class="team-tile-name">${esc(t.nickname)}</span>
        </button>`
        )
        .join("") +
      `</div>`;
  } catch (err) {
    teamsView.innerHTML = `<div class="error-state">${esc(err.message)}</div>`;
  }
}

async function openTeam(teamId) {
  // Logo and color come from the grid data we already fetched; the roster
  // endpoint doesn't repeat them.
  const meta =
    (teamsCache || []).find((t) => String(t.id) === String(teamId)) || {};
  teamsView.innerHTML = skeletonLines(8);
  try {
    const res = await fetch(`/api/teams/${teamId}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    teamsView.innerHTML = teamPageHTML(data, meta);
    animateBars(teamsView); // grow the franchise-leader bars
  } catch (err) {
    teamsView.innerHTML = `<div class="error-state">${esc(err.message)}</div>`;
  }
}

// ESPN abbreviates positions; spell them out so the roster reads like a
// broadcast graphic ("SF - Small Forward"). Some players are listed with
// just the generic "G" or "F", so those are mapped too. If ESPN ever sends
// a code we don't know, we fall back to showing the raw code by itself.
const POSITION_NAMES = {
  PG: "Point Guard",
  SG: "Shooting Guard",
  SF: "Small Forward",
  PF: "Power Forward",
  C: "Center",
  G: "Guard",
  F: "Forward",
};

function positionLabel(code) {
  const full = POSITION_NAMES[code];
  return full ? `${code} - ${full}` : code || "-";
}

// "2017 R1 #14" - draft year, round, and overall pick.
function draftLabel(d) {
  if (!d || !d.year) return "Undrafted";
  return `${d.year} R${d.round} #${d.pick}`;
}

// Salaries arrive as raw dollars (49500000); show them the way sports media
// does: "$49.5M", or "$850K" for the rare sub-million deal.
function salaryLabel(v) {
  if (!v) return "-";
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${Math.round(v / 1e3)}K`;
}

// Years in the league; 0 years = rookie.
function expLabel(years) {
  return years ? `${years} yr` : "R";
}

// Compact injury tag: ESPN's "Day-To-Day" becomes the familiar "DTD".
function injuryShort(status) {
  if (!status) return "";
  return status === "Day-To-Day" ? "DTD" : status.toUpperCase();
}

// One roster row: headshot photo, name, and the bio columns. ESPN hosts a
// real photo for nearly every current player; if one is missing we show a
// little basketball instead (onerror catches broken image URLs too).
function rosterRowHTML(p) {
  const photo = p.headshot
    ? `<img class="roster-headshot" src="${esc(p.headshot)}" alt=""
         loading="lazy" onerror="this.outerHTML='<span class=\\'roster-headshot roster-headshot-blank\\'>&#127936;</span>'">`
    : `<span class="roster-headshot roster-headshot-blank">&#127936;</span>`;
  const injury = p.injury
    ? ` <span class="injury-badge" title="${esc(p.injury)}">${esc(
        injuryShort(p.injury)
      )}</span>`
    : "";
  return `
    <tr>
      <td class="num">${esc(p.jersey || "-")}</td>
      <td><div class="team-cell">${photo}<span>${esc(p.name)}${injury}</span></div></td>
      <td class="pos">${esc(positionLabel(p.position))}</td>
      <td class="num">${esc(fmt(p.age))}</td>
      <td class="num">${esc(p.height || "-")}</td>
      <td class="num">${esc(p.weight || "-")}</td>
      <td>${esc(p.college || "-")}</td>
      <td class="pos">${esc(draftLabel(p.draft))}</td>
      <td class="num">${esc(salaryLabel(p.salary))}</td>
      <td class="num">${esc(expLabel(p.experience))}</td>
    </tr>`;
}

// The franchise-history side panel: headline facts + all-time scoring
// leaders. The leaders reuse the leader-row look from the Leaders tab, and
// clicking one jumps to that player's full profile (they're in our local DB).
function franchiseHistoryHTML(h) {
  if (!h) return "";
  const legends = h.legends || [];
  const max = Math.max(...legends.map((l) => l.total_points), 1);

  const legendRows = legends
    .map((l, i) => {
      const pct = (l.total_points / max) * 100;
      const rankClass = i < 3 ? `rank-${i + 1}` : "";
      return `
      <div class="leader-row ${rankClass} fade-in-up" style="animation-delay:${
        i * 40
      }ms" onclick="openPlayer('${esc(l.player_id)}')">
        <div class="leader-rank">${i + 1}</div>
        <div class="leader-main">
          <div class="leader-name">${esc(l.name)}${
        l.hof ? ' <span class="hof-badge">HOF</span>' : ""
      }</div>
          <div class="leader-sub">${esc(seasonLabel(l.from_year))} to ${esc(
        seasonLabel(l.to_year)
      )}</div>
          <div class="leader-bar-track">
            <div class="leader-bar-fill" data-width="${pct}" style="width:0"></div>
          </div>
        </div>
        <div class="leader-value">${l.total_points.toLocaleString()}</div>
      </div>`;
    })
    .join("");

  return `
    <div class="history-panel">
      <div class="spotlight-eyebrow">Franchise history</div>
      <div class="legend-statline history-facts">
        <div class="ls"><div class="ls-num">${esc(
          seasonLabel(h.first_season)
        )}</div><div class="ls-lab">First season</div></div>
        <div class="ls"><div class="ls-num">${esc(h.seasons)}</div><div class="ls-lab">Seasons</div></div>
        <div class="ls"><div class="ls-num">${esc(h.players)}</div><div class="ls-lab">Players</div></div>
        <div class="ls"><div class="ls-num">${esc(h.hof_count)}</div><div class="ls-lab">Hall of Famers</div></div>
      </div>
      <div class="spotlight-eyebrow">All-time scoring leaders</div>
      <div class="leaders-grid">${legendRows}</div>
    </div>`;
}

function teamPageHTML(data, meta) {
  const color = meta.color ? `#${meta.color}` : "var(--accent)";
  const subParts = [];
  if (data.coach) subParts.push(`Head coach: ${esc(data.coach)}`);
  subParts.push(`${data.roster.length} players`);
  // Trophy case: "3x champions (2013, 2012, 2006)". Long dynasties get their
  // year list trimmed with a "+N more" so the header stays one line.
  if (data.titles && data.titles.length) {
    const years = data.titles.slice(0, 6).join(", ");
    const extra =
      data.titles.length > 6 ? ` +${data.titles.length - 6} more` : "";
    subParts.push(
      `&#127942; ${data.titles.length}x champions (${esc(years)}${extra})`
    );
  }

  return `
    <button class="refresh-btn back-btn" onclick="loadTeamsGrid()">&larr; All teams</button>
    <div class="team-page-head" style="--team-color:${esc(color)}">
      <img class="team-page-logo" src="${esc(meta.logo || "")}" alt="">
      <div>
        <div class="player-name">${esc(data.name)}</div>
        <div class="player-bio">${subParts.join(" &middot; ")}</div>
      </div>
    </div>
    <div class="team-page-grid">
      <div>
        <h3 class="conference-title" style="border-color:${esc(color)}">Current Roster</h3>
        <div class="standings-table-wrap">
          <table class="standings-table roster-table">
            <thead>
              <tr>
                <th class="num">#</th><th>Player</th><th>Position</th>
                <th class="num">Age</th><th class="num">Ht</th>
                <th class="num">Wt</th><th>College</th>
                <th>Draft</th><th class="num">Salary</th><th class="num">Exp</th>
              </tr>
            </thead>
            <tbody>${data.roster.map(rosterRowHTML).join("")}</tbody>
          </table>
        </div>
      </div>
      <div>${franchiseHistoryHTML(data.history)}</div>
    </div>`;
}

/* ---------------------------------------------------------------------------
   Titles tab - every championship since 1947, plus a count-by-franchise board
   --------------------------------------------------------------------------- */
const titlesView = document.getElementById("titles-view");

// Make sure the 30-team list is loaded (the Teams tab may not have been
// opened yet); we use it to put logos next to franchises.
async function ensureTeams() {
  if (!teamsCache) {
    const res = await fetch("/api/teams");
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    teamsCache = data.teams;
  }
  return teamsCache;
}

function teamByAbbr(abbr) {
  return (teamsCache || []).find((t) => t.abbreviation === abbr);
}

async function loadTitles() {
  titlesView.innerHTML = skeletonLines(8);
  try {
    const [, res] = await Promise.all([
      ensureTeams(),
      fetch("/api/championships"),
    ]);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    titlesView.innerHTML = titlesHTML(data.championships);
    animateBars(titlesView);
  } catch (err) {
    titlesView.innerHTML = `<div class="error-state">${esc(err.message)}</div>`;
  }
}

// Count titles per CURRENT franchise. A plain object keyed by the ESPN
// abbreviation does the tallying; defunct champions (franchise=null) are
// skipped here but still appear in the year-by-year list.
function titleCounts(championships) {
  const counts = {};
  championships.forEach((c) => {
    if (c.franchise) counts[c.franchise] = (counts[c.franchise] || 0) + 1;
  });
  return Object.entries(counts)
    .map(([abbr, count]) => ({ abbr, count }))
    .sort((a, b) => b.count - a.count);
}

function titlesHTML(championships) {
  const counts = titleCounts(championships);
  const max = counts.length ? counts[0].count : 1;

  // Left panel: banner counts, styled like the leaderboards.
  const countRows = counts
    .map((c, i) => {
      const team = teamByAbbr(c.abbr) || {};
      const rankClass = i < 3 ? `rank-${i + 1}` : "";
      return `
      <div class="leader-row ${rankClass} fade-in-up" style="animation-delay:${
        i * 30
      }ms" onclick="openTeam('${esc(team.id || "")}')">
        <img class="mini-logo" src="${esc(team.logo || "")}" alt="">
        <div class="leader-main">
          <div class="leader-name">${esc(team.name || c.abbr)}</div>
          <div class="leader-bar-track">
            <div class="leader-bar-fill" data-width="${(c.count / max) * 100}"
              style="width:0"></div>
          </div>
        </div>
        <div class="leader-value">${c.count} &#127942;</div>
      </div>`;
    })
    .join("");

  // Right panel: the full Finals timeline, newest first.
  const timelineRows = championships
    .map((c) => {
      const team = c.franchise ? teamByAbbr(c.franchise) : null;
      const logo = team
        ? `<img class="mini-logo" src="${esc(team.logo)}" alt="">`
        : "";
      return `
      <tr>
        <td class="num"><strong>${esc(c.year)}</strong></td>
        <td><div class="team-cell">${logo}<span>${esc(c.champion)}</span></div></td>
        <td class="num">${esc(c.result)}</td>
        <td>${esc(c.runner_up)}</td>
        <td>${esc(c.mvp || "-")}</td>
      </tr>`;
    })
    .join("");

  return `
    <div class="titles-grid">
      <div>
        <h3 class="conference-title">Titles by Franchise</h3>
        <div class="leaders-grid">${countRows}</div>
      </div>
      <div>
        <h3 class="conference-title">Every Finals Since 1947</h3>
        <div class="standings-table-wrap">
          <table class="standings-table">
            <thead>
              <tr>
                <th class="num">Year</th><th>Champion</th><th class="num">Series</th>
                <th>Runner-up</th><th>Finals MVP</th>
              </tr>
            </thead>
            <tbody>${timelineRows}</tbody>
          </table>
        </div>
      </div>
    </div>`;
}

/* ---------------------------------------------------------------------------
   Leaders tab - all-time career leaderboards
   --------------------------------------------------------------------------- */
// The buttons in the HTML use short labels (PTS/AST/...); the API wants long
// names (points/assists/...). These maps bridge the two.
const STAT_API = {
  PTS: "points",
  AST: "assists",
  REB: "rebounds",
  STL: "steals",
  BLK: "blocks",
};

// Render a ranked leaderboard with animated bars + counting-up totals. Shared
// by the Leaders tab and the compact version on the home spotlight.
function renderLeaderBoard(container, leaders, statLabel) {
  if (!container) return;
  if (!leaders || !leaders.length) {
    container.innerHTML = `<div class="loading">No data available.</div>`;
    return;
  }
  const max = Math.max(...leaders.map((l) => l.total), 1);
  container.innerHTML = leaders
    .map((l, i) => {
      const pct = (l.total / max) * 100;
      const rankClass = i < 3 ? `rank-${i + 1}` : "";
      return `
      <div class="leader-row ${rankClass} fade-in-up" style="animation-delay:${
        i * 40
      }ms" onclick="openPlayer('${esc(l.player_id)}')">
        <div class="leader-rank">${i + 1}</div>
        <div class="leader-main">
          <div class="leader-name">${esc(l.name)}${
        l.hof ? ' <span class="hof-badge">HOF</span>' : ""
      }</div>
          <div class="leader-sub">${esc(l.from_year)}&ndash;${esc(
        l.to_year
      )} &middot; ${esc(statLabel)}</div>
          <div class="leader-bar-track">
            <div class="leader-bar-fill" data-width="${pct}" style="width:0"></div>
          </div>
        </div>
        <div class="leader-value" data-target="${l.total}">0</div>
      </div>`;
    })
    .join("");

  // Kick off the bar-grow + number count-up animations.
  animateBars(container);
  container.querySelectorAll(".leader-value").forEach((el) => {
    countUp(el, parseFloat(el.dataset.target) || 0, {
      format: (v) => Math.round(v).toLocaleString(),
    });
  });
}

function initLeaders() {
  // Wire the stat selector buttons (once), then load the default board.
  document.querySelectorAll("#leaders .stat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll("#leaders .stat-btn")
        .forEach((b) => b.classList.toggle("active", b === btn));
      loadLeaders(btn.dataset.stat);
    });
  });
  loadLeaders("PTS");
}

async function loadLeaders(statLabel) {
  const list = document.getElementById("leaders-list");
  list.innerHTML = skeletonLines(8);
  const apiStat = STAT_API[statLabel] || "points";
  try {
    const data = await fetch(`/api/leaders/all-time?stat=${apiStat}`).then((r) =>
      r.json()
    );
    if (data.error) {
      list.innerHTML = `<div class="error-state">${esc(data.error)}</div>`;
      return;
    }
    renderLeaderBoard(list, data.leaders, statLabel);
  } catch (err) {
    list.innerHTML = `<div class="error-state">${esc(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------------
   Players tab - search + season-by-season career
   --------------------------------------------------------------------------- */
// Unlike the other tabs, this one reads from our LOCAL database (nba.db, built
// by load_data.py) rather than ESPN. Flow: type a name -> /api/players?q= gives
// a short list -> click one -> /api/players/<id> gives bio + every season.
const playerSearchInput = document.getElementById("player-search");
const searchResults = document.getElementById("search-results");
const playerProfile = document.getElementById("player-profile");

// A REUSABLE search box. Given an <input>, a results <div>, and an onPick
// callback, it debounces typing, queries /api/players, renders the dropdown,
// and calls onPick(player) when a result is clicked. The Players tab and BOTH
// Compare slots all use this same function.
function attachSearch(input, results, onPick) {
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      results.classList.add("hidden");
      results.innerHTML = "";
      return;
    }
    // Wait 250ms after the last keystroke before hitting the server.
    timer = setTimeout(async () => {
      try {
        const data = await fetch(
          `/api/players?q=${encodeURIComponent(q)}`
        ).then((r) => r.json());
        results.classList.remove("hidden");
        if (data.error) {
          results.innerHTML = `<div class="error-state">${esc(
            data.error
          )}</div>`;
          return;
        }
        if (!data.players.length) {
          results.innerHTML = `<div class="search-empty">No players found.</div>`;
          return;
        }
        results.innerHTML = data.players.map(searchItemHTML).join("");
        // Wire each result to onPick. We attach listeners (rather than inline
        // onclick) so the callback receives the full player object.
        results.querySelectorAll(".search-item").forEach((el, i) => {
          el.addEventListener("click", () => {
            results.classList.add("hidden");
            input.value = data.players[i].name;
            onPick(data.players[i]);
          });
        });
      } catch (err) {
        results.classList.remove("hidden");
        results.innerHTML = `<div class="error-state">${esc(
          err.message
        )}</div>`;
      }
    }, 250);
  });
}

function searchItemHTML(p) {
  return `
    <button class="search-item">
      <span class="search-item-name">${esc(p.name)}${
    p.hof ? ' <span class="hof-badge">HOF</span>' : ""
  }</span>
      <span class="search-item-meta">${esc(p.position || "")} &middot; ${esc(
    p.from_year
  )}&ndash;${esc(p.to_year)}</span>
    </button>`;
}

// Wire up the Players-tab search exactly once (guarded by loadedTabs).
function initPlayerSearch() {
  attachSearch(playerSearchInput, searchResults, (p) => loadPlayer(p.player_id));
  renderFavStrip();
}

async function loadPlayer(playerId) {
  // Collapse the dropdown and show a skeleton in the profile area.
  searchResults.classList.add("hidden");
  playerProfile.classList.remove("hidden");
  playerProfile.innerHTML = `<div class="skeleton skeleton-card" style="margin-bottom:16px"></div>${skeletonLines(
    6
  )}`;

  try {
    const res = await fetch(`/api/players/${encodeURIComponent(playerId)}`);
    const data = await res.json();
    if (data.error) {
      playerProfile.innerHTML = `<div class="error-state">${esc(data.error)}</div>`;
      return;
    }
    renderPlayerProfile(data.player, data.seasons);
  } catch (err) {
    playerProfile.innerHTML = `<div class="error-state">${esc(err.message)}</div>`;
  }
}

/* Formatting helpers for the profile ------------------------------------- */
// Height arrives as total inches (e.g. 81). Show it the basketball way: 6'9".
function formatHeight(inches) {
  if (!inches) return "";
  return `${Math.floor(inches / 12)}'${inches % 12}"`;
}

// A plain stat value: show "-" for missing data instead of blank/"null".
function fmt(v) {
  return v === null || v === undefined ? "-" : v;
}

// A shooting percentage stored as 0.417 -> show ".417" (the convention on
// stat sheets, which drop the leading zero).
function fmtPct(v) {
  if (v === null || v === undefined) return "-";
  return v.toFixed(3).replace(/^0/, "");
}

// The DB stores a season by its END year (2026). Show the conventional label
// spanning both years: "2025-26".
function seasonLabel(year) {
  return `${year - 1}–${String(year).slice(-2)}`;
}

// The currently-displayed player + their seasons, kept so the career chart can
// re-render instantly on stat toggle and the favorite star can read the name
// without us having to inline it into an onclick (which breaks on apostrophes).
let currentCareerSeasons = [];
let currentPlayer = null;

// Toggle the displayed player's favorite status (called by the profile star).
function toggleFavoriteCurrent(btn) {
  if (currentPlayer) {
    toggleFavorite(currentPlayer.player_id, currentPlayer.name, btn);
  }
}

function renderPlayerProfile(player, seasons) {
  currentCareerSeasons = seasons;
  currentPlayer = player;

  // Assemble the bio line from whichever fields we actually have.
  const bioParts = [];
  if (player.position) bioParts.push(esc(player.position));
  const ht = formatHeight(player.height_in);
  if (ht) bioParts.push(ht);
  if (player.weight) bioParts.push(`${esc(player.weight)} lb`);
  if (player.birth_date) bioParts.push(`b. ${esc(player.birth_date)}`);
  if (player.colleges) bioParts.push(esc(player.colleges));

  const fav = isFavorite(player.player_id);

  const seasonRows = seasons
    .map(
      (s) => `
      <tr>
        <td class="num">${esc(seasonLabel(s.season))}</td>
        <td>${esc(s.team || "")}</td>
        <td class="num">${esc(fmt(s.age))}</td>
        <td class="num">${esc(fmt(s.games))}</td>
        <td class="num strong">${esc(fmt(s.points))}</td>
        <td class="num">${esc(fmt(s.rebounds))}</td>
        <td class="num">${esc(fmt(s.assists))}</td>
        <td class="num">${esc(fmt(s.steals))}</td>
        <td class="num">${esc(fmt(s.blocks))}</td>
        <td class="num">${esc(fmtPct(s.fg_pct))}</td>
        <td class="num">${esc(fmtPct(s.fg3_pct))}</td>
        <td class="num">${esc(fmtPct(s.ft_pct))}</td>
      </tr>`
    )
    .join("");

  playerProfile.innerHTML = `
    <div class="player-header">
      ${playerPhotoHTML(player.player_id, "player-photo player-photo-lg")}
      <div class="player-header-main">
        <div class="player-name-row">
          <h3 class="player-name">${esc(player.name)}${
    player.hof ? ' <span class="hof-badge">HOF</span>' : ""
  }</h3>
          <button class="fav-star ${fav ? "active" : ""}" title="Save to favorites"
            onclick="toggleFavoriteCurrent(this)">${fav ? "★" : "☆"}</button>
        </div>
        <div class="player-bio">${bioParts.join(" &middot; ")}</div>
        <div class="player-span">${esc(player.from_year)}&ndash;${esc(
    player.to_year
  )} &middot; ${seasons.length} season${seasons.length === 1 ? "" : "s"}</div>
      </div>
    </div>

    <div class="chart-card">
      <div class="chart-toggle">
        <button class="chart-toggle-btn active" onclick="setCareerStat('points', this)">Points</button>
        <button class="chart-toggle-btn" onclick="setCareerStat('rebounds', this)">Rebounds</button>
        <button class="chart-toggle-btn" onclick="setCareerStat('assists', this)">Assists</button>
      </div>
      <div id="career-chart"></div>
    </div>

    <div class="season-table-wrap">
      <table class="season-table">
        <thead>
          <tr>
            <th>Season</th><th>Tm</th><th>Age</th><th>G</th>
            <th>PPG</th><th>RPG</th><th>APG</th><th>SPG</th><th>BPG</th>
            <th>FG%</th><th>3P%</th><th>FT%</th>
          </tr>
        </thead>
        <tbody>${seasonRows}</tbody>
      </table>
    </div>`;

  renderCareerChart("points"); // default chart view
}

// Per-game stat -> the label/units shown on the career chart tooltips.
const CAREER_STAT_LABEL = { points: "PPG", rebounds: "RPG", assists: "APG" };

function setCareerStat(stat, btn) {
  document
    .querySelectorAll(".chart-toggle-btn")
    .forEach((b) => b.classList.toggle("active", b === btn));
  renderCareerChart(stat);
}

function renderCareerChart(stat) {
  const unit = CAREER_STAT_LABEL[stat] || "";
  const items = currentCareerSeasons.map((s) => ({
    value: s[stat] || 0,
    label: `'${String(s.season).slice(-2)}`, // e.g. '10
    title: `${seasonLabel(s.season)} (${s.team || ""}): ${fmt(s[stat])} ${unit}`,
  }));
  const el = document.getElementById("career-chart");
  if (el) el.innerHTML = barChartSVG(items, { color: "var(--accent)" });
}

/* ---------------------------------------------------------------------------
   Compare tab - two players side by side
   --------------------------------------------------------------------------- */
// Each slot holds the chosen player's bio + seasons. When BOTH are filled we
// draw an overlaid PPG-by-career-year chart and a head-to-head stat table.
const compareState = { a: null, b: null };
const COMPARE_COLORS = { a: "#4aa3ff", b: "#ff6b6b" };

function initCompare() {
  attachSearch(
    document.getElementById("compare-search-a"),
    document.getElementById("compare-results-a"),
    (p) => selectCompare("a", p)
  );
  attachSearch(
    document.getElementById("compare-search-b"),
    document.getElementById("compare-results-b"),
    (p) => selectCompare("b", p)
  );
  renderCompare(); // show the initial "pick two players" prompt
}

async function selectCompare(slot, player) {
  const chosenEl = document.getElementById(`compare-chosen-${slot}`);
  chosenEl.textContent = "Loading...";
  try {
    const data = await fetch(
      `/api/players/${encodeURIComponent(player.player_id)}`
    ).then((r) => r.json());
    if (data.error) {
      chosenEl.innerHTML = `<span class="error-state">${esc(data.error)}</span>`;
      return;
    }
    compareState[slot] = { player: data.player, seasons: data.seasons };
    chosenEl.innerHTML = `<span class="compare-${slot}">${esc(
      data.player.name
    )}</span>`;
    renderCompare();
  } catch (err) {
    chosenEl.innerHTML = `<span class="error-state">${esc(err.message)}</span>`;
  }
}

function renderCompare() {
  const out = document.getElementById("compare-output");
  const a = compareState.a;
  const b = compareState.b;

  if (!a || !b) {
    out.innerHTML = `
      <div class="coming-soon">
        <div class="big">&#9878;&#65039;</div>
        <h3>Pick two players to compare</h3>
        <p>Search for a player in each box above to see their careers head-to-head.</p>
      </div>`;
    return;
  }

  // Overlay each player's points-per-game across the seasons of their career.
  const series = [
    { color: COMPARE_COLORS.a, values: a.seasons.map((s) => s.points || 0) },
    { color: COMPARE_COLORS.b, values: b.seasons.map((s) => s.points || 0) },
  ];

  const sa = careerSummary(a.seasons);
  const sb = careerSummary(b.seasons);

  // [label, valueA, valueB, decimals] - higher value wins (highlighted green).
  const rows = [
    ["Seasons", sa.seasons, sb.seasons, 0],
    ["Career PPG", sa.ppg, sb.ppg, 1],
    ["Career RPG", sa.rpg, sb.rpg, 1],
    ["Career APG", sa.apg, sb.apg, 1],
    ["Peak PPG", sa.peak, sb.peak, 1],
    ["Career points", sa.totalPts, sb.totalPts, 0],
  ];
  const tableRows = rows
    .map(([label, va, vb, dec]) => {
      const f = (v) =>
        dec === 0 ? Math.round(v).toLocaleString() : v.toFixed(dec);
      return `<tr>
        <td>${esc(label)}</td>
        <td class="cval ${va > vb ? "cwin" : ""}">${f(va)}</td>
        <td class="cval ${vb > va ? "cwin" : ""}">${f(vb)}</td>
      </tr>`;
    })
    .join("");

  out.innerHTML = `
    <div class="chart-card">
      <div class="chart-legend">
        <span class="legend-item"><span class="legend-dot" style="background:${
          COMPARE_COLORS.a
        }"></span>${esc(a.player.name)}</span>
        <span class="legend-item"><span class="legend-dot" style="background:${
          COMPARE_COLORS.b
        }"></span>${esc(b.player.name)}</span>
      </div>
      ${lineChartSVG(series)}
      <div class="leader-sub" style="text-align:center;margin-top:6px">
        Points per game across each season of their career
      </div>
    </div>
    <table class="compare-table">
      <thead>
        <tr>
          <th></th>
          <th class="compare-a">${esc(a.player.name)}</th>
          <th class="compare-b">${esc(b.player.name)}</th>
        </tr>
      </thead>
      <tbody>${tableRows}</tbody>
    </table>`;
}

/* ---------------------------------------------------------------------------
   Kick everything off
   --------------------------------------------------------------------------- */
// Load the Scores tab as soon as the page is ready.
loadScores();
