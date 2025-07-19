import requests
import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load .env config
load_dotenv()

API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {'x-apisports-key': API_KEY}
BASE_URL = 'https://v3.football.api-sports.io'
tracked_matches = {}

BASE_DIR = os.path.dirname(__file__)
STATUS_FILE = os.path.join(BASE_DIR, "..", "bot_status.json")
UNRESOLVED_80_FILE = os.path.join(BASE_DIR, "..", "unresolved_80bets.json")
UNRESOLVED_SPECIAL_FT_FILE = os.path.join(BASE_DIR, "..", "unresolved_special_ft.json")

# Bot status
bot_status = {"last_check": "Not yet run", "active_matches": []}

# Load status and unresolved bets
if os.path.exists(STATUS_FILE):
    try:
        with open(STATUS_FILE, "r") as f:
            bot_status = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load bot status: {e}")

def load_unresolved_80bets():
    if os.path.exists(UNRESOLVED_80_FILE):
        with open(UNRESOLVED_80_FILE, "r") as f:
            return json.load(f)
    return {}

def save_unresolved_80bets(data):
    with open(UNRESOLVED_80_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_unresolved_special_ft():
    if os.path.exists(UNRESOLVED_SPECIAL_FT_FILE):
        with open(UNRESOLVED_SPECIAL_FT_FILE, "r") as f:
            return json.load(f)
    return {}

def save_unresolved_special_ft(data):
    with open(UNRESOLVED_SPECIAL_FT_FILE, "w") as f:
        json.dump(data, f, indent=2)

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg}
    print(f"📤 Sending:\n{msg}\n")
    response = requests.post(url, data=data)
    print("✅ Sent" if response.status_code == 200 else f"❌ Telegram error: {response.text}")
    return response

def get_live_matches():
    res = requests.get(f"{BASE_URL}/fixtures?live=all", headers=HEADERS)
    if res.status_code != 200:
        print(f"❌ API ERROR: {res.status_code} - {res.text}")
        return []
    try:
        return res.json()['response']
    except Exception as e:
        print(f"❌ JSON Error: {e}")
        return []

def fetch_match_result(match_id):
    res = requests.get(f"{BASE_URL}/fixtures?id={match_id}", headers=HEADERS)
    if res.status_code != 200:
        print(f"❌ Error fetching match {match_id}")
        return None
    data = res.json().get('response', [])
    return data[0] if data else None

def process_match(match):
    fixture_id = match['fixture']['id']
    match_name = f"{match['teams']['home']['name']} vs {match['teams']['away']['name']}"
    league = f"{match['league']['name']} ({match['league']['country']})"
    league_id = match['league']['id']
    score = match['goals']
    minute = match['fixture']['status']['elapsed']
    status = match['fixture']['status']['short']

    if fixture_id not in tracked_matches:
        tracked_matches[fixture_id] = {
            '36_bet_placed': False,
            '36_result_checked': False,
            '80_bet_placed': False,
            'match_name': match_name,
            '36_bet_type': None  # New field to track bet type
        }

    state = tracked_matches[fixture_id]

    # ✅ Place 36' Bet only if score is in allowed patterns
    if 35 <= minute <= 37 and not state['36_bet_placed']:
        score_36 = f"{score['home']}-{score['away']}"
        
        # New conditions for 36" results
        if score_36 == '0-0':
            state['score_36'] = score_36
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'over_1.5'
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_36}\n🎯 Bet: Over 1.5 Goals FT")
        elif score_36 in ['1-1', '2-2']:
            state['score_36'] = score_36
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'regular'
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_36}\n🎯 First Bet Placed")
        elif score_36 in ['1-0', '0-1']:
            state['score_36'] = score_36
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'no_draw'
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_36}\n🎯 Bet: No Draw FT")
        else:
            print(f"⛔ Skipping 36' bet for {match_name} — score {score_36} not in allowed range")

    # ✅ Check HT result
    if status == 'HT' and state['36_bet_placed'] and state['36_bet_type'] == 'regular' and not state['36_result_checked']:
        current_score = f"{score['home']}-{score['away']}"
        if current_score == state['score_36']:
            send_telegram(f"✅ HT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {current_score}\n🎉 36' Bet WON")
            state['skip_80'] = True
        else:
            send_telegram(f"❌ HT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {current_score}\n🔁 36' Bet LOST — chasing at 80'")
        state['36_result_checked'] = True

    # ✅ Check FT result for special bet types
    if status == 'FT' and state['36_bet_placed'] and not state.get('ft_result_checked'):
        try:
            final_score = f"{score['home']}-{score['away']}"
            total_goals = score['home'] + score['away']
            
            if state['36_bet_type'] == 'over_1.5':
                if total_goals > 1.5:
                    send_telegram(f"✅ FT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {final_score}\n🎉 Over 1.5 Goals Bet WON")
                else:
                    send_telegram(f"❌ FT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {final_score}\n📉 Over 1.5 Goals Bet LOST")
            
            elif state['36_bet_type'] == 'no_draw':
                if final_score not in ['0-0', '1-1', '2-2', '3-3', '4-4']:  # Assuming these are draw scores
                    send_telegram(f"✅ FT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {final_score}\n🎉 No Draw Bet WON")
                else:
                    send_telegram(f"❌ FT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {final_score}\n📉 No Draw Bet LOST")
            
            state['ft_result_checked'] = True
        except Exception as e:
            print(f"❌ Error processing FT result for {match_name}: {e}")
            # Save to unresolved special FT bets
            unresolved = load_unresolved_special_ft()
            unresolved[str(fixture_id)] = {
                'match_name': match_name,
                'league': league,
                'league_id': league_id,
                'bet_type': state['36_bet_type'],
                'placed_at': datetime.utcnow().isoformat()
            }
            save_unresolved_special_ft(unresolved)
            state['ft_result_checked'] = True  # Mark as checked to avoid duplicate processing

    # ✅ Place 80' Chase Bet only if 36' bet failed and not skipped
    if 70 <= minute <= 81 and state['36_result_checked'] and not state.get('skip_80') and not state['80_bet_placed']:
        score_80 = f"{score['home']}-{score['away']}"
        state['score_80'] = score_80
        state['80_bet_placed'] = True
        send_telegram(f"⏱️ 80' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_80}\n🎯 Chase Bet Placed")

        unresolved = load_unresolved_80bets()
        unresolved[str(fixture_id)] = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'score_80': score_80,
            'league': league,
            'league_id': league_id
        }
        save_unresolved_80bets(unresolved)

def check_unresolved_80_bets():
    unresolved = load_unresolved_80bets()
    updated = unresolved.copy()

    for match_id, info in unresolved.items():
        placed_time = datetime.fromisoformat(info['placed_at'])
        if datetime.utcnow() - placed_time < timedelta(minutes=20):
            continue

        match_data = fetch_match_result(match_id)
        if not match_data:
            print(f"⚠️ Match {match_id} not found.")
            continue

        status = match_data['fixture']['status']['short']
        final_score = f"{match_data['goals']['home']}-{match_data['goals']['away']}"
        league = info.get('league', 'Unknown League')
        league_id = info.get('league_id', 'N/A')

        if status == 'FT':
            if final_score == info['score_80']:
                send_telegram(
                    f"✅ FT Result: {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"🎉 80' Chase Bet WON"
                )
            else:
                send_telegram(
                    f"❌ FT Result: {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"📉 80' Chase Bet LOST"
                )
            updated.pop(match_id)

    save_unresolved_80bets(updated)

def check_unresolved_special_ft():
    unresolved = load_unresolved_special_ft()
    updated = unresolved.copy()

    for match_id, info in unresolved.items():
        match_data = fetch_match_result(match_id)
        if not match_data:
            print(f"⚠️ Match {match_id} not found.")
            continue

        status = match_data['fixture']['status']['short']
        if status != 'FT':
            continue

        final_score = f"{match_data['goals']['home']}-{match_data['goals']['away']}"
        total_goals = match_data['goals']['home'] + match_data['goals']['away']
        league = info.get('league', 'Unknown League')
        league_id = info.get('league_id', 'N/A')
        bet_type = info.get('bet_type', 'unknown')

        if bet_type == 'over_1.5':
            if total_goals > 1.5:
                send_telegram(
                    f"✅ FT Result (Late Update): {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"🎉 Over 1.5 Goals Bet WON"
                )
            else:
                send_telegram(
                    f"❌ FT Result (Late Update): {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"📉 Over 1.5 Goals Bet LOST"
                )
        elif bet_type == 'no_draw':
            if final_score not in ['0-0', '1-1', '2-2', '3-3']:
                send_telegram(
                    f"✅ FT Result (Late Update): {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"🎉 No Draw Bet WON"
                )
            else:
                send_telegram(
                    f"❌ FT Result (Late Update): {info['match_name']}\n"
                    f"🏆 {league}\n"
                    f"🏷️ League ID: {league_id}\n"
                    f"🔢 Score: {final_score}\n"
                    f"📉 No Draw Bet LOST"
                )
        
        updated.pop(match_id)

    save_unresolved_special_ft(updated)

def save_bot_status(last_check, matches):
    global bot_status
    bot_status = {
        "last_check": last_check,
        "active_matches": matches
    }
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(bot_status, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save status: {e}")

def run_bot_once():
    print(f"[{datetime.now()}] 🔍 Checking live matches...")
    live_matches = get_live_matches()

    matches_list = [
        f"{m['teams']['home']['name']} vs {m['teams']['away']['name']} ({m['fixture']['status']['elapsed']}')"
        for m in live_matches
    ]
    save_bot_status(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), matches_list)

    for match in live_matches:
        process_match(match)

    check_unresolved_80_bets()
    check_unresolved_special_ft()
    return matches_list
