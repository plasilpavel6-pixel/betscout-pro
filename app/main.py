from pathlib import Path
import os
from difflib import SequenceMatcher

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="BetScout Pro")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(
            {"ok": False, "message": "Frontend file static/index.html nebyl nalezen."},
            status_code=500,
        )
    return FileResponse(index_file)


@app.get("/health")
async def health():
    return {"ok": True}


def get_api_key() -> str:
    api_key = os.getenv("API_SPORTS_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Chybi API_SPORTS_KEY")
    return api_key


def normalize_team_name(text: str) -> str:
    text = (text or "").lower().strip()

    replacements = [
        ("ac ", ""),
        ("sk ", ""),
        ("fk ", ""),
        ("fc ", ""),
        ("afc ", ""),
        (" cf", ""),
        (" praha", ""),
        (" prague", ""),
        (" 1905", ""),
        (".", " "),
        ("-", " "),
        ("_", " "),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    return " ".join(text.split())


def score_team_match(query: str, candidate_name: str, candidate_country: str) -> float:
    q = normalize_team_name(query)
    c = normalize_team_name(candidate_name)
    country = (candidate_country or "").lower()

    if not q or not c:
        return 0.0

    score = 0.0

    if q == c:
        score += 100

    if q in c:
        score += 45

    if c in q:
        score += 20

    q_words = set(q.split())
    c_words = set(c.split())
    common = q_words.intersection(c_words)
    score += len(common) * 12

    ratio = SequenceMatcher(None, q, c).ratio()
    score += ratio * 40

    if "czech" in country or "czech republic" in country or "czech-republic" in country:
        if "sparta" in q or "slavia" in q or "plzen" in q or "banik" in q:
            score += 6

    return round(score, 2)


async def get_league_teams(api_key: str, league: int, season: int) -> list[dict]:
    headers = {"x-apisports-key": api_key}
    url = "https://v3.football.api-sports.io/teams"
    params = {"league": league, "season": season}

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, params=params, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"API chyba teams: {r.text}")

    data = r.json()
    return data.get("response", [])


async def find_best_team_in_league(
    api_key: str, team_name: str, league: int, season: int
) -> dict | None:
    teams = await get_league_teams(api_key, league, season)
    if not teams:
        return None

    scored = []
    for item in teams:
        team = item.get("team", {})
        name = team.get("name", "")
        country = team.get("country", "")
        score = score_team_match(team_name, name, country)
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best_item = scored[0]
    if best_score < 20:
        return None

    team = best_item.get("team", {})
    venue = best_item.get("venue", {})

    return {
        "id": team.get("id"),
        "name": team.get("name"),
        "country": team.get("country"),
        "founded": team.get("founded"),
        "logo": team.get("logo"),
        "stadium": venue.get("name"),
        "match_score": best_score,
    }


def team_strength(stats: dict) -> float:
    fixtures = stats.get("fixtures", {})
    wins = fixtures.get("wins", {}).get("total", 0) or 0
    draws = fixtures.get("draws", {}).get("total", 0) or 0
    played = fixtures.get("played", {}).get("total", 1) or 1

    goals_for = (
        stats.get("goals", {})
        .get("for", {})
        .get("total", {})
        .get("total", 0)
        or 0
    )
    goals_against = (
        stats.get("goals", {})
        .get("against", {})
        .get("total", {})
        .get("total", 0)
        or 0
    )

    points_per_game = ((wins * 3) + draws) / played
    gf_per_game = goals_for / played
    ga_per_game = goals_against / played

    score = (points_per_game * 3.2) + (gf_per_game * 1.8) - (ga_per_game * 1.3)
    return round(score, 2)


async def get_team_statistics(api_key: str, team_id: int, league: int, season: int) -> dict:
    headers = {"x-apisports-key": api_key}
    url = "https://v3.football.api-sports.io/teams/statistics"
    params = {"league": league, "season": season, "team": team_id}

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, params=params, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"API chyba statistics: {r.text}")

    return r.json().get("response", {})


def build_analysis_from_stats(home_data: dict, away_data: dict) -> dict:
    home_strength = team_strength(home_data)
    away_strength = team_strength(away_data)
    diff = home_strength - away_strength

    home_name = home_data.get("team", {}).get("name", "Home")
    away_name = away_data.get("team", {}).get("name", "Away")

    if diff > 0.9:
        winner_lean = home_name
        confidence = 72
        safer_pick = f"{home_name} DNB"
    elif diff > 0.25:
        winner_lean = home_name
        confidence = 63
        safer_pick = f"{home_name} or Draw"
    elif diff < -0.9:
        winner_lean = away_name
        confidence = 72
        safer_pick = f"{away_name} DNB"
    elif diff < -0.25:
        winner_lean = away_name
        confidence = 63
        safer_pick = f"{away_name} or Draw"
    else:
        winner_lean = "Tesne / No clear edge"
        confidence = 54
        safer_pick = "No bet / Draw lean"

    home_played = home_data.get("fixtures", {}).get("played", {}).get("total", 1) or 1
    away_played = away_data.get("fixtures", {}).get("played", {}).get("total", 1) or 1

    home_gf = (
        home_data.get("goals", {}).get("for", {}).get("total", {}).get("total", 0) or 0
    ) / home_played
    away_gf = (
        away_data.get("goals", {}).get("for", {}).get("total", {}).get("total", 0) or 0
    ) / away_played

    combined_goal_rate = home_gf + away_gf

    if combined_goal_rate >= 3.0:
        goals_pick = "Over 2.5"
    elif combined_goal_rate >= 2.1:
        goals_pick = "Over 1.5"
    else:
        goals_pick = "Under 3.5"

    reasons = []
    if home_strength > away_strength:
        reasons.append(f"{home_name} vychazi lepe podle sezonni sily tymu.")
    elif away_strength > home_strength:
        reasons.append(f"{away_name} vychazi lepe podle sezonni sily tymu.")
    else:
        reasons.append("Oba tymy vychazeji velmi podobne.")

    reasons.append(f"Sila domacich: {home_strength}")
    reasons.append(f"Sila hostu: {away_strength}")
    reasons.append(f"Odhad goloveho trhu: {goals_pick}")

    return {
        "ok": True,
        "matchup": f"{home_name} vs {away_name}",
        "winner_lean": winner_lean,
        "safer_pick": safer_pick,
        "goals_pick": goals_pick,
        "confidence": confidence,
        "reasons": reasons,
        "home_strength": home_strength,
        "away_strength": away_strength,
    }


@app.get("/api/search-teams")
async def search_teams(
    name: str = Query(..., min_length=2),
    league: int = Query(...),
    season: int = Query(2024),
):
    api_key = get_api_key()
    team = await find_best_team_in_league(api_key, name, league, season)

    if not team:
        return {"ok": False, "results": [], "detail": f"Tym nenalezen: {name}"}

    return {"ok": True, "results": [team]}


@app.get("/api/analyze")
async def analyze_match(
    home_team_id: int,
    away_team_id: int,
    league: int,
    season: int = 2024,
):
    api_key = get_api_key()

    home_data = await get_team_statistics(api_key, home_team_id, league, season)
    away_data = await get_team_statistics(api_key, away_team_id, league, season)

    return build_analysis_from_stats(home_data, away_data)


@app.get("/api/analyze-by-name")
async def analyze_by_name(
    home_name: str,
    away_name: str,
    league: int,
    season: int = 2024,
):
    api_key = get_api_key()

    home_team = await find_best_team_in_league(api_key, home_name, league, season)
    if not home_team:
        return {"ok": False, "detail": f"Tym nenalezen: {home_name}"}

    away_team = await find_best_team_in_league(api_key, away_name, league, season)
    if not away_team:
        return {"ok": False, "detail": f"Tym nenalezen: {away_name}"}

    home_data = await get_team_statistics(api_key, home_team["id"], league, season)
    away_data = await get_team_statistics(api_key, away_team["id"], league, season)

    result = build_analysis_from_stats(home_data, away_data)
    result["home_team_found"] = home_team["name"]
    result["away_team_found"] = away_team["name"]
    return result


@app.get("/api/test-status")
async def test_status():
    api_key = get_api_key()
    headers = {"x-apisports-key": api_key}
    url = "https://v3.football.api-sports.io/status"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers)

    return {
        "status_code": r.status_code,
        "text": r.text
    }
