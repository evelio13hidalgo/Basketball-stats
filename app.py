"""
NBA Stats Hub - Flask backend.

DATA SOURCE NOTE (important for this machine):
    stats.nba.com / cdn.nba.com are BLOCKED here, so the `nba_api` library
    cannot work. Instead we use ESPN's free public JSON API, which needs no
    API key. All data comes from a few documented ESPN URLs (see ESPN_* below).

WHAT THIS FILE DOES:
    - Serves the single HTML page at "/".
    - Exposes a few small JSON endpoints under /api/... that the frontend
      (static/script.js) calls with fetch().
    - Talks to ESPN with the `requests` library, then NORMALIZES ESPN's deeply
      nested JSON into a simple, flat shape the frontend can render easily.
    - Caches responses in memory for a short time so we don't hammer ESPN on
      every page refresh.

The big idea: the browser never talks to ESPN directly. The browser talks to
*our* Flask routes, and Flask talks to ESPN. That's the "backend as a
middleman / proxy" pattern. It keeps the frontend simple and lets us reshape
the data before it ever reaches the page.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import os
import sqlite3
import time
import unicodedata

import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow the browser to call our /api routes without CORS errors

# The current NBA season label, used for headings/text. Update each year.
SEASON = "2025-26"

# ESPN endpoints we rely on. Keeping them as named constants makes the code
# read like English below and means there's exactly ONE place to change a URL.
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
ESPN_STANDINGS = (
    "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
)
ESPN_TEAMS = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
)
# {team_id} is filled in per request, e.g. .../teams/14/roster for the Heat.
ESPN_ROSTER = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
)
# ESPN's lower-level "core" API. Unlike the roster feed above, this one knows
# each player's draft history. One request per athlete id.
ESPN_ATHLETE = (
    "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{athlete_id}"
)

# Local SQLite database of historical player data (every player/season since
# 1947). It is built by the separate `load_data.py` script, NOT by ESPN. We
# resolve the path relative to THIS file so it works no matter the working dir.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nba.db")


# ---------------------------------------------------------------------------
# Tiny in-memory cache
# ---------------------------------------------------------------------------
# Why cache? Every time someone loads the page or hits Refresh, we'd otherwise
# make a fresh network call to ESPN. That's slow and unkind to ESPN. Instead we
# remember the last answer for a short "time to live" (TTL) in seconds. Live
# scores get a SHORT ttl (they change often); standings get a LONGER ttl.
#
# This cache lives in a plain dict in memory, so it resets every time the
# server restarts. That's perfectly fine for a learning project. A real
# production app might use something like Redis instead.
_cache: dict = {}


def get_cached(key, fetch_fn, ttl):
    """Return cached data for `key` if it's still fresh; otherwise fetch + store.

    `fetch_fn` is a function we only call on a cache MISS. Passing a function
    (instead of the data) means the expensive network call only happens when we
    actually need it. This is sometimes called "lazy evaluation".
    """
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached["ts"] < ttl:
        return cached["data"]  # cache HIT - still fresh, reuse it
    data = fetch_fn()          # cache MISS - go get fresh data
    _cache[key] = {"data": data, "ts": now}
    return data


# ---------------------------------------------------------------------------
# SQLite access (historical player data)
# ---------------------------------------------------------------------------
# Unlike the ESPN endpoints above, the Players feature reads from a LOCAL
# database file (nba.db). We open a brand-new connection per query and close it
# right after. That's a touch less efficient than a long-lived connection, but
# it's the simplest pattern that's safe across Flask's worker threads (a single
# sqlite3 connection can't be shared between threads). Our queries are tiny and
# read-only, so the overhead is negligible.
def query_db(sql, params=(), one=False):
    """Run a read-only SQL query and return rows as plain dicts.

    `one=True` returns a single dict (or None) instead of a list - handy when a
    query is expected to match exactly one row (e.g. one player by id).
    """
    conn = sqlite3.connect(DB_PATH)
    # row_factory = sqlite3.Row lets us access columns by name and convert each
    # row to a dict, so the JSON we send out has friendly keys.
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# ESPN -> our-shape normalization
# ---------------------------------------------------------------------------
def _normalize_game(event):
    """Turn one ESPN "event" (a game) into a flat dict our frontend can render.

    ESPN nests everything: event -> competitions[0] -> competitors[] -> team.
    We dig out only the fields we care about and return a clean, predictable
    object. If the frontend ever breaks, it's almost always because the shape
    returned here changed - so this is the single source of truth for "what a
    game looks like" in our app.
    """
    competition = event["competitions"][0]
    status_type = competition["status"]["type"]

    # ESPN's status.state is one of: "pre" (scheduled), "in" (live),
    # "post" (finished). We translate that into simple booleans + a label so
    # the frontend doesn't have to know ESPN's vocabulary.
    state = status_type.get("state")
    is_live = state == "in"
    is_final = state == "post"

    # Pull out the home and away competitors. ESPN lists both in one array and
    # tags each with homeAway, so we find each by that tag rather than assuming
    # an order.
    competitors = competition["competitors"]
    home = next(c for c in competitors if c["homeAway"] == "home")
    away = next(c for c in competitors if c["homeAway"] == "away")

    def team_shape(competitor):
        team = competitor["team"]
        # records[] holds overall/home/road records; the first is "overall".
        records = competitor.get("records") or []
        overall_record = records[0]["summary"] if records else ""
        return {
            "name": team.get("displayName", ""),
            "abbreviation": team.get("abbreviation", ""),
            "logo": team.get("logo", ""),
            # score arrives as a string from ESPN; keep it as a string and let
            # the frontend decide whether to show it (no score before tip-off).
            "score": competitor.get("score", ""),
            "record": overall_record,
        }

    return {
        "id": event.get("id"),
        "name": event.get("name", ""),          # e.g. "Rockets at Nets"
        "startTime": event.get("date", ""),      # ISO UTC, e.g. 2026-01-01T23:00Z
        "status": status_type.get("description", ""),   # "Scheduled"/"Final"
        "statusDetail": status_type.get("shortDetail", ""),  # "8:00 PM"/"Q3 4:12"
        "isLive": is_live,
        "isFinal": is_final,
        "isScheduled": state == "pre",
        "home": team_shape(home),
        "away": team_shape(away),
    }


def _fetch_games_for_date(yyyymmdd):
    """Call ESPN for one date and return a list of normalized games.

    `yyyymmdd` is a string like "20260101" (the format ESPN's `dates` query
    param expects). Returns [] when ESPN has no games for that day - that's a
    normal, non-error situation (e.g. the offseason).
    """
    response = requests.get(
        ESPN_SCOREBOARD,
        params={"dates": yyyymmdd},
        timeout=15,
    )
    response.raise_for_status()  # turn HTTP errors (4xx/5xx) into exceptions
    data = response.json()
    events = data.get("events", [])
    return [_normalize_game(ev) for ev in events]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the single-page app shell. All data loads later via /api calls."""
    return render_template("index.html", season=SEASON)


@app.route("/api/scores")
def get_scores():
    """Games for a given date (defaults to today).

    Query param: ?date=YYYY-MM-DD (with dashes - friendlier for humans/URLs).
    We convert it to ESPN's YYYYMMDD format internally.

    Response shape:
        { "date": "2026-06-22", "games": [ <normalized game>, ... ] }
    An empty `games` list is a valid answer (no games that day), NOT an error.
    """
    # date=today by default. We accept the dashed form and strip the dashes.
    date_param = request.args.get("date")
    if date_param:
        yyyymmdd = date_param.replace("-", "")
        iso_date = date_param
    else:
        today = date.today()
        yyyymmdd = today.strftime("%Y%m%d")
        iso_date = today.strftime("%Y-%m-%d")

    try:
        # Short TTL (20s): live scores change fast, so we don't want stale data.
        games = get_cached(
            f"scores_{yyyymmdd}",
            lambda: _fetch_games_for_date(yyyymmdd),
            ttl=20,
        )
        return jsonify({"date": iso_date, "games": games})
    except requests.RequestException as e:
        # Network/HTTP problem talking to ESPN. Report it cleanly with a 502
        # ("bad gateway") since the failure is upstream, not in our code.
        return jsonify({"error": f"Could not reach ESPN: {e}"}), 502


@app.route("/api/upcoming")
def get_upcoming():
    """Find the next days that actually have games, looking ~10 days ahead.

    Why this exists: in the offseason (or any quiet stretch) "today" may have
    zero games. Rather than show an empty screen, the frontend can ask "what's
    next?" and we scan forward day by day until we find scheduled games.

    Response shape:
        {
          "found": true/false,
          "days": [ { "date": "YYYY-MM-DD", "games": [...] }, ... ]
        }
    We stop after we collect a few days that have games, so we don't make 10
    network calls when the next game is tomorrow.
    """
    days_with_games = []
    start = date.today()
    LOOKAHEAD_DAYS = 10      # how far forward we're willing to scan
    MAX_DAYS_TO_RETURN = 3   # stop once we've found this many game-days

    try:
        for offset in range(0, LOOKAHEAD_DAYS + 1):
            day = start + timedelta(days=offset)
            yyyymmdd = day.strftime("%Y%m%d")
            iso_date = day.strftime("%Y-%m-%d")

            # Reuse the same cached fetch as /api/scores so repeated scans are
            # cheap. Slightly longer TTL here (60s) since "what's upcoming"
            # doesn't change second to second.
            games = get_cached(
                f"scores_{yyyymmdd}",
                lambda d=yyyymmdd: _fetch_games_for_date(d),
                ttl=60,
            )
            if games:
                days_with_games.append({"date": iso_date, "games": games})
            if len(days_with_games) >= MAX_DAYS_TO_RETURN:
                break

        return jsonify({
            "found": len(days_with_games) > 0,
            "days": days_with_games,
        })
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach ESPN: {e}"}), 502


@app.route("/api/standings")
def get_standings():
    """League standings split into Eastern and Western conferences.

    ESPN returns two "children" (one per conference), each with an ordered list
    of team "entries". We flatten each entry into a small dict of just the
    columns we want to show.

    Response shape:
        { "east": [ <team row>, ... ], "west": [ ... ] }
    """
    def fetch():
        response = requests.get(ESPN_STANDINGS, timeout=15)
        response.raise_for_status()
        data = response.json()

        def rows_for_conference(child):
            entries = child["standings"]["entries"]
            rows = []
            for entry in entries:
                # Each entry has a stats[] list; turn it into a name->value map
                # so we can look up the few stats we care about by name.
                stats = {s["name"]: s.get("displayValue", "") for s in entry["stats"]}
                team = entry["team"]
                # The standings endpoint nests logos differently from the
                # scoreboard endpoint: here it's team.logos[0].href, not
                # team.logo. APIs are inconsistent like this - always inspect
                # the actual JSON rather than assuming fields match.
                logos = team.get("logos") or []
                logo_url = logos[0]["href"] if logos else ""
                rows.append({
                    "name": team.get("displayName", ""),
                    "abbreviation": team.get("abbreviation", ""),
                    "logo": logo_url,
                    "wins": stats.get("wins", ""),
                    "losses": stats.get("losses", ""),
                    "winPercent": stats.get("winPercent", ""),
                    "gamesBehind": stats.get("gamesBehind", ""),
                    "streak": stats.get("streak", ""),
                })
            return rows

        east, west = [], []
        for child in data.get("children", []):
            name = child.get("name", "")
            if "East" in name:
                east = rows_for_conference(child)
            elif "West" in name:
                west = rows_for_conference(child)
        return {"east": east, "west": west}

    try:
        # Standings barely change during a day, so a 10-minute TTL is plenty.
        result = get_cached("standings", fetch, ttl=600)
        return jsonify(result)
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach ESPN: {e}"}), 502


# ---------------------------------------------------------------------------
# Teams (current rosters from ESPN + franchise history from the local DB)
# ---------------------------------------------------------------------------
# The historical database uses Basketball-Reference team codes, and franchises
# change codes when they move cities (Seattle SuperSonics "SEA" became Oklahoma
# City "OKC") or when the code style simply differs from ESPN's ("GS" vs
# "GSW"). This map connects each CURRENT team (keyed by ESPN's abbreviation)
# to EVERY code that franchise has used since 1947, so "team history" means
# the whole franchise, not just the current city.
FRANCHISE_CODES = {
    "ATL": ["ATL", "STL", "MLH", "TRI"],          # via St. Louis, Milwaukee, Tri-Cities
    "BKN": ["BRK", "NJN", "NYN", "NJA", "NYA"],   # via New Jersey + ABA New York
    "BOS": ["BOS"],
    "CHA": ["CHO", "CHA", "CHH"],                 # incl. the Bobcats years
    "CHI": ["CHI"],
    "CLE": ["CLE"],
    "DAL": ["DAL"],
    "DEN": ["DEN", "DNA", "DNR"],                 # incl. ABA Denver Rockets
    "DET": ["DET", "FTW"],                        # via Fort Wayne
    "GS":  ["GSW", "SFW", "PHW"],                 # via San Francisco + Philadelphia
    "HOU": ["HOU", "SDR"],                        # via San Diego
    "IND": ["IND", "INA"],                        # incl. ABA Pacers
    "LAC": ["LAC", "SDC", "BUF"],                 # via San Diego + Buffalo Braves
    "LAL": ["LAL", "MNL"],                        # via Minneapolis
    "MEM": ["MEM", "VAN"],                        # via Vancouver
    "MIA": ["MIA"],
    "MIL": ["MIL"],
    "MIN": ["MIN"],
    "NO":  ["NOP", "NOH", "NOK"],                 # incl. Hornets + post-Katrina OKC years
    "NY":  ["NYK"],
    "OKC": ["OKC", "SEA"],                        # via Seattle SuperSonics
    "ORL": ["ORL"],
    "PHI": ["PHI", "SYR"],                        # via Syracuse Nationals
    "PHX": ["PHO"],
    "POR": ["POR"],
    "SAC": ["SAC", "KCK", "KCO", "CIN", "ROC"],   # all the way back to Rochester Royals
    "SA":  ["SAS", "SAA", "TEX", "DLC"],          # incl. ABA Chaparrals years
    "TOR": ["TOR"],
    "UTAH": ["UTA", "NOJ"],                       # via New Orleans Jazz
    "WSH": ["WAS", "WSB", "CAP", "BAL", "CHP", "CHZ"],  # Bullets lineage to Chicago Packers
}


def _franchise_history(espn_abbr):
    """Summarize a franchise's whole past from the local player_season table.

    Returns None (rather than raising) when we can't produce history - unknown
    abbreviation or missing database - so the roster still renders without it.
    """
    codes = FRANCHISE_CODES.get(espn_abbr)
    if not codes:
        return None

    # SQL "IN" needs one ? placeholder per code: IN (?,?,?). We build exactly
    # that many - the values themselves still travel as bound parameters.
    placeholders = ",".join("?" for _ in codes)

    try:
        # Headline facts: when the franchise first appears, how many seasons
        # it has played, and how many players have ever suited up for it.
        span = query_db(
            f"""
            SELECT MIN(season) AS first_season,
                   MAX(season) AS last_season,
                   COUNT(DISTINCT season) AS seasons,
                   COUNT(DISTINCT player_id) AS players
            FROM player_season
            WHERE team IN ({placeholders})
            """,
            codes,
            one=True,
        )
        if not span or span["first_season"] is None:
            return None

        # Franchise scoring leaders: same "per-game average x games" totalling
        # trick as the all-time leaderboards, but restricted to this franchise.
        legends = query_db(
            f"""
            SELECT p.player_id, p.name, p.hof,
                   CAST(SUM(COALESCE(ps.points, 0) * COALESCE(ps.games, 0)) AS INTEGER)
                       AS total_points,
                   MIN(ps.season) AS from_year,
                   MAX(ps.season) AS to_year
            FROM player_season ps
            JOIN player p ON p.player_id = ps.player_id
            WHERE ps.team IN ({placeholders})
            GROUP BY ps.player_id
            HAVING total_points > 0
            ORDER BY total_points DESC
            LIMIT 8
            """,
            codes,
        )

        hof = query_db(
            f"""
            SELECT COUNT(DISTINCT ps.player_id) AS n
            FROM player_season ps
            JOIN player p ON p.player_id = ps.player_id
            WHERE p.hof = 1 AND ps.team IN ({placeholders})
            """,
            codes,
            one=True,
        )

        return {
            "codes": codes,
            "first_season": span["first_season"],
            "last_season": span["last_season"],
            "seasons": span["seasons"],
            "players": span["players"],
            "hof_count": hof["n"] if hof else 0,
            "legends": legends,
        }
    except sqlite3.OperationalError:
        # nba.db missing - the roster half of the page still works without us.
        return None


# ---------------------------------------------------------------------------
# Championships (curated data - no API offers this cleanly)
# ---------------------------------------------------------------------------
# Every NBA (and 1947-49 BAA) Finals result. "champion" is the team's name AT
# THE TIME ("Seattle SuperSonics"); "franchise" is the ESPN abbreviation of
# the CURRENT team that owns the title (SEA's ring belongs to OKC), matching
# the franchise-lineage idea used in FRANCHISE_CODES. franchise=None means the
# champion folded entirely (the original Baltimore Bullets).
# Finals MVP only exists from 1969 on, so earlier years use None.
# MAINTENANCE: add a new entry at the TOP each June.
CHAMPIONSHIPS = [
    {"year": 2026, "champion": "New York Knicks", "franchise": "NY", "runner_up": "San Antonio Spurs", "result": "4-1", "mvp": None},
    {"year": 2025, "champion": "Oklahoma City Thunder", "franchise": "OKC", "runner_up": "Indiana Pacers", "result": "4-3", "mvp": "Shai Gilgeous-Alexander"},
    {"year": 2024, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Dallas Mavericks", "result": "4-1", "mvp": "Jaylen Brown"},
    {"year": 2023, "champion": "Denver Nuggets", "franchise": "DEN", "runner_up": "Miami Heat", "result": "4-1", "mvp": "Nikola Jokic"},
    {"year": 2022, "champion": "Golden State Warriors", "franchise": "GS", "runner_up": "Boston Celtics", "result": "4-2", "mvp": "Stephen Curry"},
    {"year": 2021, "champion": "Milwaukee Bucks", "franchise": "MIL", "runner_up": "Phoenix Suns", "result": "4-2", "mvp": "Giannis Antetokounmpo"},
    {"year": 2020, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Miami Heat", "result": "4-2", "mvp": "LeBron James"},
    {"year": 2019, "champion": "Toronto Raptors", "franchise": "TOR", "runner_up": "Golden State Warriors", "result": "4-2", "mvp": "Kawhi Leonard"},
    {"year": 2018, "champion": "Golden State Warriors", "franchise": "GS", "runner_up": "Cleveland Cavaliers", "result": "4-0", "mvp": "Kevin Durant"},
    {"year": 2017, "champion": "Golden State Warriors", "franchise": "GS", "runner_up": "Cleveland Cavaliers", "result": "4-1", "mvp": "Kevin Durant"},
    {"year": 2016, "champion": "Cleveland Cavaliers", "franchise": "CLE", "runner_up": "Golden State Warriors", "result": "4-3", "mvp": "LeBron James"},
    {"year": 2015, "champion": "Golden State Warriors", "franchise": "GS", "runner_up": "Cleveland Cavaliers", "result": "4-2", "mvp": "Andre Iguodala"},
    {"year": 2014, "champion": "San Antonio Spurs", "franchise": "SA", "runner_up": "Miami Heat", "result": "4-1", "mvp": "Kawhi Leonard"},
    {"year": 2013, "champion": "Miami Heat", "franchise": "MIA", "runner_up": "San Antonio Spurs", "result": "4-3", "mvp": "LeBron James"},
    {"year": 2012, "champion": "Miami Heat", "franchise": "MIA", "runner_up": "Oklahoma City Thunder", "result": "4-1", "mvp": "LeBron James"},
    {"year": 2011, "champion": "Dallas Mavericks", "franchise": "DAL", "runner_up": "Miami Heat", "result": "4-2", "mvp": "Dirk Nowitzki"},
    {"year": 2010, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Boston Celtics", "result": "4-3", "mvp": "Kobe Bryant"},
    {"year": 2009, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Orlando Magic", "result": "4-1", "mvp": "Kobe Bryant"},
    {"year": 2008, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-2", "mvp": "Paul Pierce"},
    {"year": 2007, "champion": "San Antonio Spurs", "franchise": "SA", "runner_up": "Cleveland Cavaliers", "result": "4-0", "mvp": "Tony Parker"},
    {"year": 2006, "champion": "Miami Heat", "franchise": "MIA", "runner_up": "Dallas Mavericks", "result": "4-2", "mvp": "Dwyane Wade"},
    {"year": 2005, "champion": "San Antonio Spurs", "franchise": "SA", "runner_up": "Detroit Pistons", "result": "4-3", "mvp": "Tim Duncan"},
    {"year": 2004, "champion": "Detroit Pistons", "franchise": "DET", "runner_up": "Los Angeles Lakers", "result": "4-1", "mvp": "Chauncey Billups"},
    {"year": 2003, "champion": "San Antonio Spurs", "franchise": "SA", "runner_up": "New Jersey Nets", "result": "4-2", "mvp": "Tim Duncan"},
    {"year": 2002, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "New Jersey Nets", "result": "4-0", "mvp": "Shaquille O'Neal"},
    {"year": 2001, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Philadelphia 76ers", "result": "4-1", "mvp": "Shaquille O'Neal"},
    {"year": 2000, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Indiana Pacers", "result": "4-2", "mvp": "Shaquille O'Neal"},
    {"year": 1999, "champion": "San Antonio Spurs", "franchise": "SA", "runner_up": "New York Knicks", "result": "4-1", "mvp": "Tim Duncan"},
    {"year": 1998, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Utah Jazz", "result": "4-2", "mvp": "Michael Jordan"},
    {"year": 1997, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Utah Jazz", "result": "4-2", "mvp": "Michael Jordan"},
    {"year": 1996, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Seattle SuperSonics", "result": "4-2", "mvp": "Michael Jordan"},
    {"year": 1995, "champion": "Houston Rockets", "franchise": "HOU", "runner_up": "Orlando Magic", "result": "4-0", "mvp": "Hakeem Olajuwon"},
    {"year": 1994, "champion": "Houston Rockets", "franchise": "HOU", "runner_up": "New York Knicks", "result": "4-3", "mvp": "Hakeem Olajuwon"},
    {"year": 1993, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Phoenix Suns", "result": "4-2", "mvp": "Michael Jordan"},
    {"year": 1992, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Portland Trail Blazers", "result": "4-2", "mvp": "Michael Jordan"},
    {"year": 1991, "champion": "Chicago Bulls", "franchise": "CHI", "runner_up": "Los Angeles Lakers", "result": "4-1", "mvp": "Michael Jordan"},
    {"year": 1990, "champion": "Detroit Pistons", "franchise": "DET", "runner_up": "Portland Trail Blazers", "result": "4-1", "mvp": "Isiah Thomas"},
    {"year": 1989, "champion": "Detroit Pistons", "franchise": "DET", "runner_up": "Los Angeles Lakers", "result": "4-0", "mvp": "Joe Dumars"},
    {"year": 1988, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Detroit Pistons", "result": "4-3", "mvp": "James Worthy"},
    {"year": 1987, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Boston Celtics", "result": "4-2", "mvp": "Magic Johnson"},
    {"year": 1986, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Houston Rockets", "result": "4-2", "mvp": "Larry Bird"},
    {"year": 1985, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Boston Celtics", "result": "4-2", "mvp": "Kareem Abdul-Jabbar"},
    {"year": 1984, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-3", "mvp": "Larry Bird"},
    {"year": 1983, "champion": "Philadelphia 76ers", "franchise": "PHI", "runner_up": "Los Angeles Lakers", "result": "4-0", "mvp": "Moses Malone"},
    {"year": 1982, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Philadelphia 76ers", "result": "4-2", "mvp": "Magic Johnson"},
    {"year": 1981, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Houston Rockets", "result": "4-2", "mvp": "Cedric Maxwell"},
    {"year": 1980, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "Philadelphia 76ers", "result": "4-2", "mvp": "Magic Johnson"},
    {"year": 1979, "champion": "Seattle SuperSonics", "franchise": "OKC", "runner_up": "Washington Bullets", "result": "4-1", "mvp": "Dennis Johnson"},
    {"year": 1978, "champion": "Washington Bullets", "franchise": "WSH", "runner_up": "Seattle SuperSonics", "result": "4-3", "mvp": "Wes Unseld"},
    {"year": 1977, "champion": "Portland Trail Blazers", "franchise": "POR", "runner_up": "Philadelphia 76ers", "result": "4-2", "mvp": "Bill Walton"},
    {"year": 1976, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Phoenix Suns", "result": "4-2", "mvp": "Jo Jo White"},
    {"year": 1975, "champion": "Golden State Warriors", "franchise": "GS", "runner_up": "Washington Bullets", "result": "4-0", "mvp": "Rick Barry"},
    {"year": 1974, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Milwaukee Bucks", "result": "4-3", "mvp": "John Havlicek"},
    {"year": 1973, "champion": "New York Knicks", "franchise": "NY", "runner_up": "Los Angeles Lakers", "result": "4-1", "mvp": "Willis Reed"},
    {"year": 1972, "champion": "Los Angeles Lakers", "franchise": "LAL", "runner_up": "New York Knicks", "result": "4-1", "mvp": "Wilt Chamberlain"},
    {"year": 1971, "champion": "Milwaukee Bucks", "franchise": "MIL", "runner_up": "Baltimore Bullets", "result": "4-0", "mvp": "Kareem Abdul-Jabbar"},
    {"year": 1970, "champion": "New York Knicks", "franchise": "NY", "runner_up": "Los Angeles Lakers", "result": "4-3", "mvp": "Willis Reed"},
    {"year": 1969, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-3", "mvp": "Jerry West"},
    {"year": 1968, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-2", "mvp": None},
    {"year": 1967, "champion": "Philadelphia 76ers", "franchise": "PHI", "runner_up": "San Francisco Warriors", "result": "4-2", "mvp": None},
    {"year": 1966, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-3", "mvp": None},
    {"year": 1965, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-1", "mvp": None},
    {"year": 1964, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "San Francisco Warriors", "result": "4-1", "mvp": None},
    {"year": 1963, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-2", "mvp": None},
    {"year": 1962, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Los Angeles Lakers", "result": "4-3", "mvp": None},
    {"year": 1961, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "St. Louis Hawks", "result": "4-1", "mvp": None},
    {"year": 1960, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "St. Louis Hawks", "result": "4-3", "mvp": None},
    {"year": 1959, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "Minneapolis Lakers", "result": "4-0", "mvp": None},
    {"year": 1958, "champion": "St. Louis Hawks", "franchise": "ATL", "runner_up": "Boston Celtics", "result": "4-2", "mvp": None},
    {"year": 1957, "champion": "Boston Celtics", "franchise": "BOS", "runner_up": "St. Louis Hawks", "result": "4-3", "mvp": None},
    {"year": 1956, "champion": "Philadelphia Warriors", "franchise": "GS", "runner_up": "Fort Wayne Pistons", "result": "4-1", "mvp": None},
    {"year": 1955, "champion": "Syracuse Nationals", "franchise": "PHI", "runner_up": "Fort Wayne Pistons", "result": "4-3", "mvp": None},
    {"year": 1954, "champion": "Minneapolis Lakers", "franchise": "LAL", "runner_up": "Syracuse Nationals", "result": "4-3", "mvp": None},
    {"year": 1953, "champion": "Minneapolis Lakers", "franchise": "LAL", "runner_up": "New York Knicks", "result": "4-1", "mvp": None},
    {"year": 1952, "champion": "Minneapolis Lakers", "franchise": "LAL", "runner_up": "New York Knicks", "result": "4-3", "mvp": None},
    {"year": 1951, "champion": "Rochester Royals", "franchise": "SAC", "runner_up": "New York Knicks", "result": "4-3", "mvp": None},
    {"year": 1950, "champion": "Minneapolis Lakers", "franchise": "LAL", "runner_up": "Syracuse Nationals", "result": "4-2", "mvp": None},
    {"year": 1949, "champion": "Minneapolis Lakers", "franchise": "LAL", "runner_up": "Washington Capitols", "result": "4-2", "mvp": None},
    {"year": 1948, "champion": "Baltimore Bullets", "franchise": None, "runner_up": "Philadelphia Warriors", "result": "4-2", "mvp": None},
    {"year": 1947, "champion": "Philadelphia Warriors", "franchise": "GS", "runner_up": "Chicago Stags", "result": "4-1", "mvp": None},
]


@app.route("/api/championships")
def get_championships():
    """Every Finals result, newest first. Pure local data - no network, no DB."""
    return jsonify({"championships": CHAMPIONSHIPS})


# The "stat of the day" rotates between these categories by day of year, so
# Monday might feature a scoring season and Tuesday a rebounding one.
STAT_OF_DAY_CATEGORIES = [
    ("points", "PPG"),
    ("rebounds", "RPG"),
    ("assists", "APG"),
]


@app.route("/api/dashboard")
def get_dashboard():
    """Everything the home dashboard needs from LOCAL data, in one call:

        {
          "champion":    <newest CHAMPIONSHIPS entry>,
          "birthdays":   [ players born on today's month+day, best first ],
          "stat_of_day": { name, player_id, season, team, value, label }
        }

    Scores/standings arrive via their existing endpoints; this one only adds
    the pieces no other route provides. Both extras are DETERMINISTIC per
    calendar day (not random), so the dashboard looks the same all day and
    changes overnight - like a real "today in basketball" page.
    """
    today = date.today()
    payload = {"champion": CHAMPIONSHIPS[0]}

    try:
        # strftime('%m-%d', ...) works because birth_date is stored as ISO
        # text ("1963-02-17"); matching month+day finds birthdays in any year.
        payload["birthdays"] = query_db(
            """
            SELECT player_id, name, birth_date, hof, from_year, to_year
            FROM player
            WHERE strftime('%m-%d', birth_date) = ?
            ORDER BY hof DESC, career_points DESC
            LIMIT 8
            """,
            (today.strftime("%m-%d"),),
        )

        # Pick today's category, grab the 100 best qualifying seasons ever for
        # it, then use the day-of-year to select one. games >= 40 filters out
        # tiny-sample seasons that would make the "record" misleading.
        day_number = today.timetuple().tm_yday
        column, label = STAT_OF_DAY_CATEGORIES[
            day_number % len(STAT_OF_DAY_CATEGORIES)
        ]
        rows = query_db(
            f"""
            SELECT player_id, name, season, team, {column} AS value
            FROM player_season
            WHERE games >= 40 AND {column} IS NOT NULL
            ORDER BY {column} DESC
            LIMIT 100
            """
        )
        payload["stat_of_day"] = (
            {**rows[day_number % len(rows)], "label": label} if rows else None
        )
    except sqlite3.OperationalError:
        # No nba.db yet - the dashboard still renders its other panels.
        payload["birthdays"] = []
        payload["stat_of_day"] = None

    return jsonify(payload)


def _normalize_name(name):
    """Make a player name comparable across our two data sources.

    ESPN spells names plain-ASCII ("Luka Doncic", "Kristaps Porzingis") while
    Basketball-Reference keeps the accents ("Luka Dončić"). Unicode NFKD
    decomposition splits an accented letter into base letter + accent mark, and
    dropping the "combining" marks leaves just the base letters. We also drop
    periods (P.J. vs PJ) and lowercase everything.
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ascii_only.lower().replace(".", "").strip()


def _specific_positions_by_name():
    """Map normalized player name -> specific position (PG/SG/SF/PF/C).

    ESPN's roster endpoint only labels players Guard/Forward/Center, which
    isn't much of an answer to "who's the point guard?". Our local database
    records the SPECIFIC position for every season, so we read the last couple
    of seasons and keep each player's most recent entry (the rows are ordered
    oldest-first, so newer seasons overwrite older ones as we build the dict).
    """
    rows = query_db(
        """
        SELECT name, position
        FROM player_season
        WHERE season >= 2024 AND position != 'NA'
        ORDER BY season
        """
    )
    return {_normalize_name(r["name"]): r["position"] for r in rows}


@app.route("/api/teams")
def get_teams():
    """All 30 current NBA teams, for the team directory grid.

    Response shape:
        { "teams": [ { id, abbreviation, name, location, nickname,
                       color, logo }, ... ] }
    """
    def fetch():
        response = requests.get(ESPN_TEAMS, timeout=15)
        response.raise_for_status()
        data = response.json()
        raw = data["sports"][0]["leagues"][0]["teams"]

        teams = []
        for wrapper in raw:
            team = wrapper["team"]
            logos = team.get("logos") or []
            teams.append({
                "id": team.get("id"),
                "abbreviation": team.get("abbreviation", ""),
                "name": team.get("displayName", ""),
                "location": team.get("location", ""),   # "Miami"
                "nickname": team.get("name", ""),        # "Heat"
                # ESPN sends colors as bare hex like "98002e"; the frontend
                # prepends the "#". Fall back to NBA blue if a team lacks one.
                "color": team.get("color", "1d428a"),
                "logo": logos[0]["href"] if logos else "",
            })
        teams.sort(key=lambda t: t["name"])
        return teams

    try:
        # The list of NBA teams changes roughly never; cache for a day.
        teams = get_cached("teams", fetch, ttl=86400)
        return jsonify({"teams": teams})
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach ESPN: {e}"}), 502


def _fetch_draft(athlete_id):
    """Draft year/round/pick for one player, from ESPN's core API.

    Returns None for undrafted players AND on any network hiccup - a missing
    draft line should never take down the whole roster page.
    """
    try:
        response = requests.get(
            ESPN_ATHLETE.format(athlete_id=athlete_id), timeout=10
        )
        response.raise_for_status()
        draft = response.json().get("draft")
        if not draft:
            return None  # undrafted
        return {
            "year": draft.get("year"),
            "round": draft.get("round"),
            "pick": draft.get("selection"),
        }
    except requests.RequestException:
        return None


@app.route("/api/teams/<team_id>")
def team_detail(team_id):
    """One team's current roster (from ESPN) + franchise history (local DB).

    Response shape:
        {
          "abbreviation": "MIA",
          "name": "Miami Heat",
          "coach": "Erik Spoelstra",
          "roster": [ { id, name, jersey, position, age, height, weight,
                        college, headshot, experience }, ... ],
          "history": { first_season, last_season, seasons, players,
                       hof_count, legends: [...] }   # or null
        }
    """
    def fetch():
        response = requests.get(ESPN_ROSTER.format(team_id=team_id), timeout=15)
        response.raise_for_status()
        data = response.json()

        players = []
        for athlete in data.get("athletes", []):
            # Most nested fields can be absent (rookies without photos, etc.),
            # so we grab each sub-object defensively with `or {}`.
            position = athlete.get("position") or {}
            college = athlete.get("college") or {}
            headshot = athlete.get("headshot") or {}
            experience = athlete.get("experience") or {}
            # contracts[] and injuries[] are often empty lists; `or [{}]`
            # swaps an empty list for one empty dict so .get() stays safe.
            contract = (athlete.get("contracts") or [{}])[0]
            injury = (athlete.get("injuries") or [{}])[0]
            players.append({
                "id": athlete.get("id"),
                "name": athlete.get("fullName", ""),
                "jersey": athlete.get("jersey", ""),
                "position": position.get("abbreviation", ""),
                "age": athlete.get("age"),
                "height": athlete.get("displayHeight", ""),
                "weight": athlete.get("displayWeight", ""),
                "college": college.get("shortName") or college.get("name", ""),
                "headshot": headshot.get("href", ""),
                "experience": experience.get("years", 0),
                "salary": contract.get("salary"),
                "injury": injury.get("status", ""),   # "Day-To-Day"/"Out"/""
            })
        players.sort(key=lambda p: p["name"])

        # Look up each player's draft pick. That's one extra ESPN request per
        # player, so two tricks keep it fast: (1) a thread pool fires ~8
        # requests at once instead of one after another, and (2) each result
        # is cached for a week (a player's draft history never changes), so
        # only the FIRST view of a team pays the cost.
        def draft_for(player):
            return get_cached(
                f"draft_{player['id']}",
                lambda: _fetch_draft(player["id"]),
                ttl=604800,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            drafts = list(pool.map(draft_for, players))
        for player, draft in zip(players, drafts):
            player["draft"] = draft

        # Upgrade ESPN's generic G/F labels to real positions (PG/SG/SF/PF)
        # from the local database. Players we can't match - mostly rookies who
        # haven't logged an NBA season yet - keep the generic label.
        try:
            specific = get_cached(
                "positions_by_name", _specific_positions_by_name, ttl=86400
            )
        except sqlite3.OperationalError:
            specific = {}  # no nba.db - generic labels are still fine
        for p in players:
            if p["position"] in ("G", "F", ""):
                better = specific.get(_normalize_name(p["name"]))
                if better:
                    p["position"] = better

        # ESPN has sent the coach as both a dict and a list-of-dicts over
        # time, so accept either shape.
        coaches = data.get("coach") or []
        if isinstance(coaches, dict):
            coaches = [coaches]
        coach = ""
        if coaches:
            first = coaches[0]
            coach = f"{first.get('firstName', '')} {first.get('lastName', '')}".strip()

        team = data.get("team") or {}
        return {
            "abbreviation": team.get("abbreviation", ""),
            "name": team.get("displayName", ""),
            "coach": coach,
            "roster": players,
        }

    try:
        # Rosters change on trades/signings, not minute to minute; 1h TTL.
        detail = get_cached(f"roster_{team_id}", fetch, ttl=3600)
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach ESPN: {e}"}), 502

    history = _franchise_history(detail["abbreviation"])
    # This franchise's championship years (newest first), for the trophy line.
    titles = [
        c["year"] for c in CHAMPIONSHIPS
        if c["franchise"] == detail["abbreviation"]
    ]
    return jsonify({**detail, "history": history, "titles": titles})


@app.route("/api/players")
def search_players():
    """Search the historical player database by name.

    Query param: ?q=<text> (at least 2 characters). We do a case-insensitive
    substring match, but rank players whose name STARTS with the query first,
    then most-recent players first - so typing "james" surfaces LeBron James
    and recent guys before someone from 1952.

    Response shape:
        { "players": [ { player_id, name, position, from_year, to_year, hof }, ...] }
    """
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        # Too short to be useful - avoid returning half the league.
        return jsonify({"players": []})

    try:
        # Ranking has two parts:
        #   1. Match quality: a name where SOME word starts with the query
        #      (first OR last name) beats a mid-word substring match. This makes
        #      "jordan" treat "Jordan Clarkson" and "Michael Jordan" equally.
        #   2. Notability: within the same match tier, sort by career points so
        #      legends surface above journeymen who happen to share a name.
        players = query_db(
            """
            SELECT player_id, name, position, from_year, to_year, hof
            FROM player
            WHERE name LIKE ?
            ORDER BY
                CASE WHEN name LIKE ? OR name LIKE ? THEN 0 ELSE 1 END,
                career_points DESC,
                to_year DESC,
                name
            LIMIT 25
            """,
            (f"%{q}%", f"{q}%", f"% {q}%"),
        )
        return jsonify({"players": players})
    except sqlite3.OperationalError:
        # Almost always means nba.db is missing or empty - i.e. load_data.py
        # hasn't been run yet. Tell the user how to fix it.
        return jsonify({
            "error": "Player database not found. Run `python3 load_data.py` first."
        }), 503


@app.route("/api/players/<player_id>")
def player_detail(player_id):
    """One player's bio plus their full season-by-season career.

    Response shape:
        {
          "player":  { player_id, name, position, height_in, ... , hof },
          "seasons": [ { season, team, points, rebounds, assists, ... }, ... ]
        }
    Seasons are ordered oldest -> newest so the table reads top-to-bottom like a
    career timeline.
    """
    try:
        bio = query_db(
            "SELECT * FROM player WHERE player_id = ?", (player_id,), one=True
        )
        if bio is None:
            return jsonify({"error": "Player not found"}), 404

        seasons = query_db(
            """
            SELECT season, league, team, age, position, games, games_started,
                   minutes, points, rebounds, assists, steals, blocks,
                   turnovers, fg_pct, fg3_pct, ft_pct
            FROM player_season
            WHERE player_id = ?
            ORDER BY season
            """,
            (player_id,),
        )
        return jsonify({"player": bio, "seasons": seasons})
    except sqlite3.OperationalError:
        return jsonify({
            "error": "Player database not found. Run `python3 load_data.py` first."
        }), 503


# Which per-game stat column to total up for each all-time leaderboard. Keys are
# what the frontend sends (?stat=points); values are REAL column names. Using a
# whitelist like this means we can safely drop the chosen column into the SQL
# string below without risking SQL injection from user input.
ALLTIME_STATS = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
}


@app.route("/api/leaders/all-time")
def all_time_leaders():
    """All-time career leaders for one counting stat (points, rebounds, ...).

    We approximate a career TOTAL as the sum over every season of
    (per-game average x games played). Steals/blocks only exist from 1973-74
    on, so those boards naturally only include the modern era.

    Query param: ?stat=points|rebounds|assists|steals|blocks  (default points).
    Response: { "stat": "points", "leaders": [ { player_id, name, hof, total,
                from_year, to_year }, ... ] }
    """
    stat = (request.args.get("stat") or "points").lower()
    column = ALLTIME_STATS.get(stat)
    if column is None:
        return jsonify({"error": f"Unknown stat '{stat}'"}), 400

    try:
        leaders = query_db(
            # `column` is safe here: it's a value from ALLTIME_STATS, never raw
            # user text. The user-supplied parts stay as bound parameters.
            f"""
            SELECT p.player_id, p.name, p.hof,
                   SUM(COALESCE(ps.{column}, 0) * COALESCE(ps.games, 0)) AS total,
                   MIN(ps.season) AS from_year,
                   MAX(ps.season) AS to_year
            FROM player_season ps
            JOIN player p ON p.player_id = ps.player_id
            GROUP BY ps.player_id
            HAVING total > 0
            ORDER BY total DESC
            LIMIT 15
            """
        )
        return jsonify({"stat": stat, "leaders": leaders})
    except sqlite3.OperationalError:
        return jsonify({
            "error": "Player database not found. Run `python3 load_data.py` first."
        }), 503


@app.route("/api/legends/random")
def random_legend():
    """A random Hall-of-Famer with a quick career summary, for the home spotlight.

    We only pick HOF players with a real playing career (career_points filter)
    so we don't surface coaches/contributors with thin stat lines. Returns the
    bio plus weighted career averages and the player's best scoring season.
    """
    try:
        legend = query_db(
            """
            SELECT player_id, name, position, height_in, weight, birth_date,
                   colleges, from_year, to_year, career_points
            FROM player
            WHERE hof = 1 AND career_points > 8000
            ORDER BY RANDOM()
            LIMIT 1
            """,
            one=True,
        )
        if legend is None:
            return jsonify({"error": "No legends found"}), 404

        seasons = query_db(
            """
            SELECT season, team, games, points, rebounds, assists
            FROM player_season
            WHERE player_id = ?
            ORDER BY season
            """,
            (legend["player_id"],),
        )

        # Career per-game averages, weighted by games played each season (so a
        # 10-game cup-of-coffee year doesn't count as much as an 82-game one).
        total_games = sum((s["games"] or 0) for s in seasons) or 1

        def weighted(stat):
            return round(
                sum((s[stat] or 0) * (s["games"] or 0) for s in seasons) / total_games,
                1,
            )

        legend["seasons_count"] = len(seasons)
        legend["career_ppg"] = weighted("points")
        legend["career_rpg"] = weighted("rebounds")
        legend["career_apg"] = weighted("assists")

        # Best scoring season, for a fun "peak" highlight on the card.
        if seasons:
            best = max(seasons, key=lambda s: (s["points"] or 0))
            legend["best_season"] = {
                "season": best["season"],
                "team": best["team"],
                "points": best["points"],
            }
        return jsonify({"legend": legend})
    except sqlite3.OperationalError:
        return jsonify({
            "error": "Player database not found. Run `python3 load_data.py` first."
        }), 503


if __name__ == "__main__":
    # debug=True gives auto-reload on code changes + helpful error pages.
    # Turn it OFF if you ever deploy this for real.
    #
    # host="0.0.0.0" means "listen on every network interface", so OTHER
    # devices on your home network (your laptop/phone) can reach the Pi.
    # Without it, Flask only accepts connections from the Pi itself.
    # Then open http://<your-pi-ip>:5000 from another device on the same Wi-Fi.
    # Security note: this exposes the site to everyone on your local network.
    # That's fine at home; never run debug=True on a network you don't trust.
    #
    # debug defaults ON for manual runs (`python3 app.py`), but the systemd
    # boot service sets FLASK_DEBUG=0 to turn it OFF. The Werkzeug debugger
    # allows remote code execution, so we never leave it on for an always-on
    # service that's reachable from the whole network.
    debug = os.environ.get("FLASK_DEBUG", "1") != "0"
    app.run(host="0.0.0.0", debug=debug, port=5000)
