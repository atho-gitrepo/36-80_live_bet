import requests
import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

# Load .env config for local development
load_dotenv()

API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Use a new variable for the JSON content itself
FIREBASE_CREDENTIALS_JSON_STRING = os.getenv("FIREBASE_CREDENTIALS_JSON")

HEADERS = {'x-apisports-key': API_KEY}
BASE_URL = 'https://v3.football.api-sports.io'

class FirebaseManager:
    """Manages all interactions with the Firebase Firestore database."""
    def __init__(self, credentials_json_string):
        try:
            # Parse the JSON string into a dictionary
            cred_dict = json.loads(credentials_json_string)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            print("✅ Firebase initialized successfully.")
        except Exception as e:
            print(f"❌ Failed to initialize Firebase: {e}")
            raise

    def get_tracked_match(self, match_id):
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else None

    def update_tracked_match(self, match_id, data):
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        doc_ref.set(data, merge=True)

    def get_unresolved_bets(self, bet_type):
        bets = self.db.collection('unresolved_bets').where('bet_type', '==', bet_type).stream()
        return {doc.id: doc.to_dict() for doc in bets}
    
    def add_unresolved_bet(self, match_id, data):
        self.db.collection('unresolved_bets').document(str(match_id)).set(data)
        
    def resolve_bet(self, match_id):
        self.db.collection('unresolved_bets').document(str(match_id)).delete()

# Initialize Firebase
try:
    # Use the JSON string from the environment variable
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception:
    exit()

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

    # Fetch match state from Firebase
    state = firebase_manager.get_tracked_match(fixture_id)
    if not state:
        state = {'36_bet_placed': False, '80_bet_placed': False}
        firebase_manager.update_tracked_match(fixture_id, state)

    # ✅ Place 36' Bet only if score is 1-1, 2-2, or 3-3
    if 35 <= minute <= 37 and not state.get('36_bet_placed'):
        score_36 = f"{score['home']}-{score['away']}"
        if score_36 in ['1-1', '2-2', '3-3']:
            state['score_36'] = score_36
            state['36_bet_placed'] = True
            firebase_manager.update_tracked_match(fixture_id, state)
            
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_36}\n🎯 Bet: Goals after 36'")
        else:
            print(f"⛔ Skipping 36' bet for {match_name} — score {score_36} is not a tie (1-1, 2-2, or 3-3)")
    
    # ✅ Place 80' Chase Bet
    if 79 <= minute <= 81 and state.get('36_bet_placed') and not state.get('80_bet_placed'):
        score_80 = f"{score['home']}-{score['away']}"
        state['score_80'] = score_80
        state['80_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

        send_telegram(f"⏱️ 80' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_80}\n🎯 Chase Bet Placed")

        # Add to unresolved bets in Firebase
        unresolved_data = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'score_80': score_80,
            'league': league,
            'league_id': league_id,
            'bet_type': '80'
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        
def check_unresolved_80_bets():
    unresolved_bets = firebase_manager.get_unresolved_bets('80')

    for match_id, info in unresolved_bets.items():
        placed_time = datetime.fromisoformat(info['placed_at'])
        # Wait at least 15 minutes to check for final score
        if datetime.utcnow() - placed_time < timedelta(minutes=15):
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
            if final_score != info['score_80']:
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
            firebase_manager.resolve_bet(match_id)

def run_bot_once():
    print(f"[{datetime.now()}] 🔍 Checking live matches...")
    live_matches = get_live_matches()

    for match in live_matches:
        process_match(match)

    check_unresolved_80_bets()
    print(f"[{datetime.now()}] ✅ Cycle complete.")

