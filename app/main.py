from pathlib import Path
import os
from typing import Optional

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
            {
                "ok": False,
                "message": "Frontend file static/index.html nebyl nalezen."
            },
            status_code=500
        )
    return FileResponse(index_file)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/search-teams")
async def search_teams(
    name: str = Query(..., min_length=2),
    league: Optional[int] = None,
    season: int = 2024,
):
    api_key = os.getenv("API_SPORTS_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Chybí API_SPORTS_KEY")

    headers = {"x-apisports-key": api_key}
    url = "https://v3.football.api-sports.io/teams"

    def normalize(text: str) -> str:
        return (
            text.lower()
            .replace("ac ", "")
            .replace("sk ", "")
            .replace("fk ", "")
            .replace("fc ", "")
            .replace(" praha", "")
            .replace(" prague", "")
            .strip()
        )

    search_variants = []
    original = name.strip()
    base = normalize(original)

    candidates = [original, base]
    if base:
        first_word = base.split()[0]
        candidates.append(first_word)

    for v in candidates:
        if v and v not in search_variants:
            search_variants.append(v)

    async with httpx.AsyncClient(timeout=20) as client:
        found_items = []

        # 1) zkus hledání s ligou
        if league:
            for variant in search_variants:
                params = {"search": variant, "league": league, "season": season}
                r = await client.get(url, params=params, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    resp = data.get("response", [])
                    if resp:
                        found_items = resp
                        break

        # 2) fallback bez ligy
        if not found_items:
            for variant in search_variants:
                params = {"search": variant}
                r = await client.get(url, params=params, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    resp = data.get("response", [])
                    if resp:
                        found_items = resp
                        break

    if not found_items:
        return {"ok": False, "results": [], "detail": f"Tým nenalezen: {name}"}

    results = []
    for item in found_items:
        team = item.get("team", {})
        venue = item.get("venue", {})
        results.append(
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "country": team.get("country"),
                "founded": team.get("founded"),
                "logo": team.get("logo"),
                "stadium": venue.get("name"),
            }
        )

    return {"ok": True, "results": results[:10]}


@app.get("/api/analyze")
async def analyze_match(
    home_team_id: int,
    away_team_id: int,
    league: int,
    season: int = 2024,
):
    api_key = os.getenv("API_SPORTS_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Chybí API_SPORTS_KEY")

    headers = {"x-apisports-key": api_key}

    async with httpx.AsyncClient(timeout=25) as client:
        home_resp = await client.get(
            "https://v3.football.api-sports.io/teams/statistics",
            params={"league": league, "season": season, "team": home_team_id},
            headers=headers,
        )
        away_resp = await client.get(
            "https://v3.football.api-sports.io/teams/statistics",
            params={"league": league, "season": season, "team": away_team_id},
            headers=headers,
        )

    if home_resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Home API chyba: {home_resp.text}")
    if away_resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Away API chyba: {away_resp.text}")

    home_data = home_resp.json().get("response", {})
    away_data = away_resp.json().get("response", {})

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

        score = (
            points_per_game * 3.2
            + gf_per_game * 1.8
            - ga_per_game * 1.3
        )
        return round(score, 2)

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
        winner_lean = "Těsné / No clear edge"
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
        reasons.append(f"{home_name} vychází lépe podle sezónní síly týmu.")
    elif away_strength > home_strength:
        reasons.append(f"{away_name} vychází lépe podle sezónní síly týmu.")
    else:
        reasons.append("Oba týmy vycházejí velmi podobně.")

    reasons.append(f"Síla domácích: {home_strength}")
    reasons.append(f"Síla hostů: {away_strength}")
    reasons.append(f"Odhad gólového trhu: {goals_pick}")

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
