import requests
import json
import os
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


API_KEY = os.environ.get("ROTOWIRE_KEY")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_CONFIRMED")
ROTOWIRE_LINEUPS_URL = "https://www.rotowire.com/soccer/lineups.php?league=EPL"
STATE_FILE = "posted_lineups.json"


WINDOW_BEFORE_KICKOFF = 180  # minutes


TZ_ET = ZoneInfo("America/New_York")
TZ_UK = ZoneInfo("Europe/London")


POSITION_ORDER = {
    "GK": 0,
    "DR": 1, "DC": 2, "DL": 3, "D": 4,
    "DMR": 5, "DMC": 6, "DML": 7,
    "MR": 8, "MC": 9, "ML": 10, "M": 11,
    "AMR": 12, "AMC": 13, "AML": 14,
    "FWR": 15, "FW": 16, "FWL": 17,
}



def load_posted():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}



def save_posted(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)



def fetch_lineups():
    try:
        url = f"https://api.rotowire.com/Soccer/EPL/Lineups.php?key={API_KEY}"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return ET.fromstring(response.text)
    except Exception as e:
        print(f"[ERROR] Failed to fetch lineups: {e}")
        return None



def parse_kickoff(date_str):
    """Parse ISO date string and convert to UTC properly."""
    try:
        dt_str = date_str[:19]
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        # Parse the offset e.g. -04:00 or -05:00
        offset_str = date_str[19:].strip()
        if offset_str:
            sign = 1 if offset_str[0] == '+' else -1
            parts = offset_str[1:].split(':')
            offset_hours = int(parts[0])
            offset_mins  = int(parts[1]) if len(parts) > 1 else 0
            offset = timedelta(hours=offset_hours, minutes=offset_mins) * sign
            # Convert to UTC
            dt_utc = dt - offset
        else:
            dt_utc = dt
        return dt_utc.replace(tzinfo=timezone.utc)
    except Exception:
        return None



def has_games_today(root):
    """Check if there are any EPL games today (ET date, since API times are ET)."""
    games_el = root.find("Games")
    if games_el is None:
        return False
    today_utc = datetime.now(timezone.utc).date()
    for game_el in games_el.findall("Game"):
        kickoff = parse_kickoff(game_el.get("Date", ""))
        if kickoff and kickoff.date() == today_utc:
            return True
    return False



def hash_players(players_xml):
    player_list = []
    for p in players_xml:
        player_list.append({
            "id": p.get("Id", ""),
            "firstname": p.findtext("Firstname", ""),
            "lastname": p.findtext("Lastname", ""),
            "position": p.findtext("Position", ""),
        })
    player_list.sort(key=lambda x: x["id"])
    return hashlib.md5(json.dumps(player_list, sort_keys=True).encode()).hexdigest()



def sort_players(players):
    def key(p):
        pos = p.findtext("GamePosition", p.findtext("Position", "")).strip().upper()
        return POSITION_ORDER.get(pos, 99)
    return sorted(players, key=key)



def clean_cdata(text):
    if text:
        return text.replace("<![CDATA[", "").replace("]]>", "").strip()
    return ""



def format_kickoff(dt):
    et = dt.astimezone(TZ_ET).strftime("%a %b %-d, %-I:%M %p ET")
    uk = dt.astimezone(TZ_UK).strftime("%H:%M UK")
    return f"{et}  |  {uk}"



def fmt_player(p):
    firstname = p.findtext("Firstname", "").strip()
    lastname  = p.findtext("Lastname", "").strip()
    nickname  = clean_cdata(p.findtext("Nickname", "")).strip()
    game_pos  = p.findtext("GamePosition", "").strip()
    position  = p.findtext("Position", "").strip()


    name    = nickname if nickname else f"{firstname} {lastname}".strip()
    pos     = game_pos or position
    pos_str = f" `{pos}`" if pos else ""
    return f"• {name}{pos_str}"



def build_discord_message(team_el, home_name, away_name, kickoff):
    team_name  = clean_cdata(team_el.findtext("Name", "Unknown Team"))
    players_el = team_el.find("Players")


    lines = []
    lines.append(f"**{away_name}  ✈️  vs  🏠  {home_name}**")
    if kickoff:
        lines.append(f"🗓️  {format_kickoff(kickoff)}")
    lines.append("")


    lines.append(f"✅  **{team_name}** — *Confirmed*")
    lines.append("")


    if players_el is not None:
        all_players = players_el.findall("Player")
        starters    = sort_players(all_players[:11])
        bench       = all_players[11:]


        if starters:
            lines.append("**⚽  Starting XI**")
            for p in starters:
                lines.append(f"  {fmt_player(p)}")
            lines.append("")


        if bench:
            lines.append("**🪑  Bench**")
            for p in bench:
                lines.append(f"  {fmt_player(p)}")
            lines.append("")


    lines.append(f"🔗  [Full EPL Lineups]({ROTOWIRE_LINEUPS_URL})")


    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1897] + "..."
    return message



def post_to_discord(message):
    payload = {"content": message}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}")



def main():
    if not API_KEY:
        print("[ERROR] ROTOWIRE_KEY secret is not set.")
        return
    if not DISCORD_WEBHOOK:
        print("[ERROR] DISCORD_WEBHOOK_CONFIRMED secret is not set.")
        return


    now_utc = datetime.now(timezone.utc)
    window  = timedelta(minutes=WINDOW_BEFORE_KICKOFF)


    root = fetch_lineups()
    if root is None:
        return


    # Exit early if no EPL games today (international break etc.)
    if not has_games_today(root):
        print("No EPL games today — exiting.")
        return


    games_el = root.find("Games")


    # Check if any game falls within our kickoff window (all in UTC)
    games_in_window = []
    for game_el in games_el.findall("Game"):
        kickoff = parse_kickoff(game_el.get("Date", ""))
        if kickoff and (kickoff - window) <= now_utc <= kickoff:
            games_in_window.append(game_el)


    if not games_in_window:
        print("No games within 3 hour window — exiting.")
        return


    print(f"{len(games_in_window)} game(s) in window — checking confirmed lineups...")


    posted        = load_posted()
    updated_count = 0


    for game_el in games_in_window:
        game_id  = game_el.get("Id", "").strip()
        kickoff  = parse_kickoff(game_el.get("Date", ""))
        teams_el = game_el.find("Teams")


        if not game_id or teams_el is None:
            continue


        home_name = "Home"
        away_name = "Away"
        for team_el in teams_el.findall("Team"):
            name = clean_cdata(team_el.findtext("Name", ""))
            if team_el.get("IsHome", "0") == "1":
                home_name = name
            else:
                away_name = name


        for team_el in teams_el.findall("Team"):
            team_id       = team_el.get("Id", "").strip()
            players_el    = team_el.find("Players")
            lineup_status = team_el.findtext("LineupStatus", "").strip()


            if lineup_status != "C":
                print(f"[SKIP] {clean_cdata(team_el.findtext('Name', team_id))} — status '{lineup_status}'")
                continue


            if not team_id or players_el is None:
                continue


            key          = f"{game_id}_{team_id}"
            current_hash = hash_players(players_el.findall("Player"))


            if posted.get(key) != current_hash:
                message = build_discord_message(team_el, home_name, away_name, kickoff)
                post_to_discord(message)
                posted[key] = current_hash
                updated_count += 1
                team_name = clean_cdata(team_el.findtext("Name", team_id))
                print(f"[POSTED] {team_name} confirmed lineup (game {game_id})")


    save_posted(posted)


    if updated_count == 0:
        print("No new confirmed lineups.")
    else:
        print(f"Done. Posted/updated {updated_count} confirmed lineup(s).")



if __name__ == "__main__":
    main()
