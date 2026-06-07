from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from nba_api.stats.endpoints import (
    leaguestandings,
    leagueleaders,
    scoreboardv2,
    playercareerstats,
    commonplayerinfo,
)
from nba_api.stats.static import players as nba_players
import time

app = Flask(__name__)
CORS(app)

SEASON = "2024-25"

_cache: dict = {}


def get_cached(key, fetch_fn, ttl=300):
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < ttl:
        return _cache[key]["data"]
    data = fetch_fn()
    _cache[key] = {"data": data, "ts": now}
    return data


def parse_result_set(result_set, limit=None):
    headers = result_set["headers"]
    rows = result_set["rowSet"]
    if limit:
        rows = rows[:limit]
    return [dict(zip(headers, row)) for row in rows]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/standings")
def get_standings():
    try:
        def fetch():
            s = leaguestandings.LeagueStandings(season=SEASON, timeout=30)
            data = s.get_dict()
            return parse_result_set(data["resultSets"][0])

        teams = get_cached("standings", fetch, ttl=600)

        # Detect conference field name dynamically
        conf_field = next(
            (f for f in ["Conference", "TeamConference"] if teams and f in teams[0]),
            "Conference",
        )
        east = [t for t in teams if t.get(conf_field) == "East"]
        west = [t for t in teams if t.get(conf_field) == "West"]
        return jsonify({"east": east, "west": west})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/leaders")
def get_leaders():
    try:
        stat = request.args.get("stat", "PTS")
        cache_key = f"leaders_{stat}"

        def fetch():
            l = leagueleaders.LeagueLeaders(
                season=SEASON,
                stat_category_abbreviation=stat,
                timeout=30,
            )
            data = l.get_dict()
            return parse_result_set(data["resultSets"][0], limit=25)

        result = get_cached(cache_key, fetch, ttl=600)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scores")
def get_scores():
    try:
        def fetch():
            sb = scoreboardv2.ScoreboardV2(timeout=30)
            data = sb.get_dict()
            games = parse_result_set(data["resultSets"][0])
            line_scores = parse_result_set(data["resultSets"][1])
            return {"games": games, "lineScores": line_scores}

        result = get_cached("scores", fetch, ttl=60)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/players/search")
def search_players():
    try:
        query = request.args.get("q", "").lower()
        if len(query) < 2:
            return jsonify([])
        all_players = nba_players.get_active_players()
        filtered = [p for p in all_players if query in p["full_name"].lower()][:15]
        return jsonify(filtered)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/player/<int:player_id>")
def get_player(player_id):
    try:
        cache_key = f"player_{player_id}"

        def fetch():
            career = playercareerstats.PlayerCareerStats(
                player_id=player_id, timeout=30
            )
            career_data = career.get_dict()
            seasons = parse_result_set(career_data["resultSets"][0])

            time.sleep(0.6)  # respect NBA API rate limit between calls

            info = commonplayerinfo.CommonPlayerInfo(
                player_id=player_id, timeout=30
            )
            info_data = info.get_dict()
            player_info = parse_result_set(info_data["resultSets"][0])

            return {
                "info": player_info[0] if player_info else {},
                "seasons": seasons,
            }

        result = get_cached(cache_key, fetch, ttl=3600)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
