"""
One-time historical data loader for NBA Stats Hub.

WHAT THIS DOES:
    Downloads season-by-season NBA player data (every player since the league's
    first season in 1946-47) and loads it into a local SQLite database, `nba.db`.
    The Flask app (app.py) then reads from that database to power the Players tab.

WHY A SEPARATE SCRIPT (not part of app.py):
    Building the database is a SLOW, ONE-TIME job (download + parse + insert).
    We don't want to do that on every web request. So we do it ONCE here, save
    the result to nba.db, and the web app just runs fast read-only queries.
    Re-run this script whenever you want to refresh the data (e.g. once a season).

WHY THIS DATA SOURCE:
    stats.nba.com is BLOCKED on this machine, and ESPN's API isn't built for
    deep history. Instead we use a pre-compiled, public dataset on GitHub:
        github.com/sumitrodatta/bball-reference-datasets
    It's derived from Basketball-Reference, covers 1947 -> present at SEASON
    granularity, needs no API key, and downloads directly (no Kaggle login).

RUN IT:
    python3 load_data.py
"""

import csv
import io
import sqlite3
import sys

import requests

# Where to write the database. Same folder as this script / app.py.
DB_PATH = "nba.db"

# Raw-file base URL for the dataset's `Data` folder (note: branch is "master").
RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "sumitrodatta/bball-reference-datasets/master/Data"
)

# The two files we need for season-by-season stats + player bios. The %20 is a
# URL-encoded space (the filenames contain real spaces).
PER_GAME_URL = f"{RAW_BASE}/Player%20Per%20Game.csv"
CAREER_INFO_URL = f"{RAW_BASE}/Player%20Career%20Info.csv"


# ---------------------------------------------------------------------------
# Small type-coercion helpers
# ---------------------------------------------------------------------------
# CSV values are ALL strings, and missing cells are empty strings "". We want
# real numbers in the database (so the app can sort/compare), and NULL for
# blanks. These helpers convert safely, returning None when a value is missing
# or unparseable instead of crashing.
def to_int(value):
    try:
        return int(float(value))  # float() first handles "73.0"-style strings
    except (TypeError, ValueError):
        return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value):
    """Basketball-Reference writes booleans as 'TRUE'/'FALSE' strings."""
    return 1 if str(value).strip().lower() in ("true", "1", "yes") else 0


def download_csv(url):
    """Download a CSV and return a list of dict rows (keyed by column name)."""
    print(f"  downloading {url.rsplit('/', 1)[-1].replace('%20', ' ')} ...")
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    # csv.DictReader needs a file-like object; wrap the downloaded text.
    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Two tables, joined by `player_id` (a stable Basketball-Reference id like
# "jamesle01" for LeBron James):
#   player          -> one row per player: bio + career span + HOF flag
#   player_season   -> one row per player PER SEASON: that year's per-game stats
SCHEMA = """
DROP TABLE IF EXISTS player;
DROP TABLE IF EXISTS player_season;

CREATE TABLE player (
    player_id   TEXT PRIMARY KEY,
    name        TEXT,
    position    TEXT,
    height_in   INTEGER,   -- height in inches
    weight      INTEGER,   -- weight in pounds
    birth_date  TEXT,
    colleges    TEXT,
    from_year   INTEGER,   -- first season played
    to_year     INTEGER,   -- last season played
    debut       TEXT,
    hof         INTEGER,   -- 1 if in the Hall of Fame, else 0
    career_points REAL     -- approx career total points (notability score for search)
);

CREATE TABLE player_season (
    player_id     TEXT,
    season        INTEGER,   -- e.g. 2026 means the 2025-26 season
    league        TEXT,      -- NBA / ABA / BAA
    name          TEXT,
    age           INTEGER,
    team          TEXT,      -- 3-letter abbreviation, e.g. LAL
    position      TEXT,
    games         INTEGER,
    games_started INTEGER,
    minutes       REAL,      -- per game
    points        REAL,      -- per game
    rebounds      REAL,      -- total rebounds per game
    assists       REAL,      -- per game
    steals        REAL,      -- per game
    blocks        REAL,      -- per game
    turnovers     REAL,      -- per game
    fg_pct        REAL,      -- field-goal %
    fg3_pct       REAL,      -- 3-point %
    ft_pct        REAL       -- free-throw %
);

-- Indexes make the app's lookups fast: search by name, and pull one player's
-- whole career (all their seasons) by player_id.
CREATE INDEX idx_player_name ON player (name);
CREATE INDEX idx_season_player ON player_season (player_id);
CREATE INDEX idx_season_year ON player_season (season);
"""


def build():
    print("Downloading source CSVs from GitHub (Basketball-Reference data)...")
    per_game = download_csv(PER_GAME_URL)
    career = download_csv(CAREER_INFO_URL)
    print(f"  -> {len(per_game):,} season rows, {len(career):,} players")

    print(f"Building {DB_PATH} ...")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)

        # --- player table (bios) ---------------------------------------
        player_rows = [
            (
                r["player_id"],
                r["player"],
                r["pos"],
                to_int(r["ht_in_in"]),
                to_int(r["wt"]),
                r["birth_date"] or None,
                r["colleges"] or None,
                to_int(r["from"]),
                to_int(r["to"]),
                r["debut"] or None,
                to_bool(r["hof"]),
            )
            for r in career
        ]
        conn.executemany(
            """INSERT INTO player
               (player_id, name, position, height_in, weight, birth_date,
                colleges, from_year, to_year, debut, hof)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            player_rows,
        )

        # --- player_season table (per-game stats) ----------------------
        # IMPORTANT: when a player is traded mid-season, Basketball-Reference
        # writes a combined total row (team = "2TM"/"3TM"/...) AND one row per
        # team they played for. If we kept all of them, summing would
        # double-count those seasons (inflating career totals) and the career
        # chart would draw multiple bars for one year. So for any (player,
        # season) that has a combined row, we keep ONLY that combined row, and
        # relabel it "TOT" for display.
        MULTI_TEAM = {"2TM", "3TM", "4TM", "5TM"}
        combined_seasons = {
            (r["player_id"], r["season"])
            for r in per_game
            if r["team"] in MULTI_TEAM
        }

        def keep_row(r):
            if (r["player_id"], r["season"]) in combined_seasons:
                return r["team"] in MULTI_TEAM  # keep only the combined total
            return True

        season_rows = [
            (
                r["player_id"],
                to_int(r["season"]),
                r["lg"],
                r["player"],
                to_int(r["age"]),
                "TOT" if r["team"] in MULTI_TEAM else r["team"],
                r["pos"],
                to_int(r["g"]),
                to_int(r["gs"]),
                to_float(r["mp_per_game"]),
                to_float(r["pts_per_game"]),
                to_float(r["trb_per_game"]),
                to_float(r["ast_per_game"]),
                to_float(r["stl_per_game"]),
                to_float(r["blk_per_game"]),
                to_float(r["tov_per_game"]),
                to_float(r["fg_percent"]),
                to_float(r["x3p_percent"]),
                to_float(r["ft_percent"]),
            )
            for r in per_game
            if keep_row(r)
        ]
        conn.executemany(
            "INSERT INTO player_season VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            season_rows,
        )

        # Compute an approximate career-points total for each player
        # (points-per-game x games, summed over every season). It's used purely
        # as a "notability" score so that searching e.g. "jordan" ranks Michael
        # Jordan above the couple-dozen role players whose FIRST name is Jordan.
        conn.execute(
            """
            UPDATE player
            SET career_points = (
                SELECT COALESCE(SUM(points * games), 0)
                FROM player_season
                WHERE player_season.player_id = player.player_id
            )
            """
        )

        conn.commit()

        # Quick sanity check so a successful run prints something meaningful.
        players = conn.execute("SELECT COUNT(*) FROM player").fetchone()[0]
        seasons = conn.execute("SELECT COUNT(*) FROM player_season").fetchone()[0]
        span = conn.execute(
            "SELECT MIN(season), MAX(season) FROM player_season"
        ).fetchone()
        print(
            f"Done. {players:,} players, {seasons:,} player-seasons, "
            f"covering {span[0]}-{span[1]}."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        build()
    except requests.RequestException as e:
        # Network problem reaching GitHub. Print a clear message and exit non-zero
        # so it's obvious the build failed (rather than silently making a bad db).
        print(f"ERROR: could not download data: {e}", file=sys.stderr)
        sys.exit(1)
