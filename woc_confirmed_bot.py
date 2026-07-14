"""
woc_confirmed_bot.py
Posts World Cup CONFIRMED lineups to Discord.
- C (Confirmed) lineups only → DISCORD_WEBHOOK_WOC_CONFIRMED
- Detects changes via MD5 hash and re-posts on change
- State tracked in posted_woc_lineups.json
- Runs in public repo via poller (unlimited GitHub Actions minutes)
"""
import os
import json
import hashlib
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY           = os.environ.get("ROTOWIRE_KEY_WOC", "")
WEBHOOK_CONFIRMED = os.environ.get("DISCORD_WEBHOOK_WOC_CONFIRMED", "")

# ── Rotowire ──────────────────────────────────────────────────────────────────
LINEUPS_URL   = f"https://api.rotowire.com/Soccer/WOC/Lineups.php?key={API_KEY}"
ROTOWIRE_PAGE = "https://www.rotowire.com/soccer/lineups.php?league=WOC"

# ── State file ────────────────────────────────────────────────────────────────
STATE_FILE = "posted_woc_lineups.json"

# ── Position sort order ───────────────────────────────────────────────────────
POSITION_ORDER = [
    "GK",
    "DR", "DRC", "DC", "DLC", "DL",
    "DMR", "DMC", "DML",
    "MR",  "MC",  "ML",
    "AMR", "AMC", "AML",
    "FWR", "FW",  "FWL",
    "SS",
]

# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def lineup_hash(players: list) -> str:
    # Sort players into a canonical order so a pure reordering by the feed
    # (e.g. two players sharing the same position swapping places) does not
    # change the hash and trigger a duplicate re-post.
    canonical = sorted(
        players,
        key=lambda p: (p.get("name", ""), p.get("pos", ""), p.get("gpos", "")),
    )
    key = json.dumps(canonical, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()

# ── Fetch lineups ─────────────────────────────────────────────────────────────
def fetch_lineups() -> list:
    try:
        r = requests.get(LINEUPS_URL, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.startswith("{"):
            print(f"[SKIP] Non-XML response: {text[:120]}")
            return []
        return parse_xml(text)
    except Exception as e:
        print(f"[ERROR] Fetch failed: {e}")
        return []

def parse_xml(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    games_el = root.find("Games")
    if games_el is None:
        return []
    games = []
    for game in games_el:
        game_id   = game.get("Id", "")
        game_date = game.get("Date", "")
        teams_el  = game.find("Teams")
        if teams_el is None:
            continue
        teams = []
        for team in teams_el:
            team_id    = team.get("Id", "")
            is_home    = team.get("IsHome", "0") == "1"
            name       = (team.findtext("Name") or "").strip()
            status     = (team.findtext("LineupStatus") or "").strip().upper()
            players_el = team.find("Players")
            players = []
            if players_el is not None:
                for p in players_el:
                    nickname = (p.findtext("Nickname") or "").strip()
                    first    = (p.findtext("Firstname") or "").strip()
                    last     = (p.findtext("Lastname")  or "").strip()
                    players.append({
                        "name": nickname if nickname else f"{first} {last}".strip(),
                        "pos":  (p.findtext("Position")     or "").strip(),
                        "gpos": (p.findtext("GamePosition") or "").strip(),
                    })
            teams.append({
                "id":      team_id,
                "name":    name,
                "is_home": is_home,
                "status":  status,
                "players": players,
            })
        games.append({
            "id":    game_id,
            "date":  game_date,
            "teams": teams,
        })
    return games

# ── Formatting ────────────────────────────────────────────────────────────────
def format_kickoff(iso_date: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_date)
        et_str   = dt.strftime("%-I:%M %p ET")
        uk_dt    = dt + timedelta(hours=5)
        uk_str   = uk_dt.strftime("%-I:%M %p UK")
        date_str = dt.strftime("%a %b %-d")
        return f"{date_str} · {et_str} / {uk_str}"
    except Exception:
        return iso_date

def sort_players(players: list) -> list:
    def sort_key(p):
        try:
            return POSITION_ORDER.index(p.get("gpos", ""))
        except ValueError:
            return len(POSITION_ORDER)
    return sorted(players, key=sort_key)

def build_message(game: dict, team: dict) -> str:
    team_names = " vs ".join(t["name"] for t in game["teams"])
    kickoff    = format_kickoff(game["date"])
    lines = [
        f"⚽ **World Cup Lineup**",
        f"🏟️  **{team_names}**",
        f"🗓️  {kickoff}",
        "",
        f"✅ **{team['name']}** — *Confirmed XI*",
        "",
        "**Starting XI:**",
    ]
    for p in sort_players(team["players"]):
        gpos    = p.get("gpos", "")
        pos_str = f" `{gpos}`" if gpos else ""
        lines.append(f"  • {p['name']}{pos_str}")
    lines.append("")
    lines.append(f"[Full World Cup Lineups](<{ROTOWIRE_PAGE}>)")
    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1897] + "..."
    return message

# ── Discord ───────────────────────────────────────────────────────────────────
def post_to_discord(message: str):
    try:
        r = requests.post(WEBHOOK_CONFIRMED, json={"content": message}, timeout=10)
        r.raise_for_status()
        print("[OK] Posted to Discord.")
        time.sleep(2)
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not API_KEY:
        print("[ERROR] ROTOWIRE_KEY_WOC is not set.")
        return
    if not WEBHOOK_CONFIRMED:
        print("[ERROR] DISCORD_WEBHOOK_WOC_CONFIRMED is not set.")
        return

    state = load_state()
    games = fetch_lineups()
    if not games:
        print("No lineup data available.")
        return

    posted_count = 0
    for game in games:
        game_id = game["id"]
        for team in game["teams"]:
            team_id = team["id"]
            status  = team["status"]
            players = team["players"]

            if status != "C":
                continue
            if not players:
                print(f"[SKIP] {team['name']} — no players.")
                continue

            state_key = f"{game_id}_{team_id}"
            h         = lineup_hash(players)
            prev_hash = state.get(state_key, {}).get("hash", "")

            if h == prev_hash:
                print(f"[SKIP] {team['name']} — no change.")
                continue

            reason = "new" if not prev_hash else "change"
            print(f"[POST] {team['name']} — {reason} → confirmed channel.")
            post_to_discord(build_message(game, team))
            state[state_key] = {"hash": h, "status": "C"}
            posted_count += 1

    save_state(state)
    print(f"Done. Posted/updated {posted_count} confirmed lineup(s).")

if __name__ == "__main__":
    main()
