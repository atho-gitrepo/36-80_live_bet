import requests
import os
import json
import time
from datetime import datetime, timedelta

import firebase_admin
from firebase_admin import credentials, firestore

# Load .env config for local development only
# if os.getenv("RAILWAY_ENVIRONMENT") is None:
#     from dotenv import load_dotenv
#     load_dotenv()

API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FIREBASE_CREDENTIALS_JSON_STRING = os.getenv("FIREBASE_CREDENTIALS_JSON")

HEADERS = {'x-apisports-key': API_KEY}
BASE_URL = 'https://v3.football.api-sports.io'

class FirebaseManager:
    """Manages all interactions with the Firebase Firestore database."""
    def __init__(self, credentials_json_string):
        try:
            print("[DEBUG] Initializing Firebase...")
            
            print("[DEBUG] Attempting to parse credentials JSON string...")
            cred_dict = json.loads(credentials_json_string)
            print("✅ Successfully parsed credentials JSON.")
            
            cred = credentials.Certificate(cred_dict)
            print("✅ Successfully created credentials object.")
            
            firebase_admin.initialize_app(cred)
            print("✅ Successfully initialized Firebase app.")
            
            self.db = firestore.client()
            print("✅ Successfully created Firestore client.")
            
            print("✅ Firebase initialized successfully.")
        except Exception as e:
            print(f"❌ Failed to initialize Firebase: {e}")
            raise


    def get_tracked_match(self, match_id):
        print(f"[DEBUG] Fetching tracked match state for ID: {match_id}")
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        try:
            doc = doc_ref.get()
            state = doc.to_dict() if doc.exists else None
            print(f"[DEBUG] Found state: {state}")
            return state
        except Exception as e:
            print(f"❌ Firestore Error during get_tracked_match: {e}")
            return None

    def update_tracked_match(self, match_id, data):
        print(f"[DEBUG] Updating tracked match state for ID: {match_id} with data: {data}")
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        try:
            doc_ref.set(data, merge=True)
            print(f"[DEBUG] Successfully updated tracked match state for ID: {match_id}")
        except Exception as e:
            print(f"❌ Firestore Error during update_tracked_match: {e}")

    def get_unresolved_bets(self, bet_type):
        print(f"[DEBUG] Fetching unresolved bets for bet type: {bet_type}")
        try:
            bets = self.db.collection('unresolved_bets').where(filter=firestore.FieldFilter('bet_type', '==', bet_type)).stream()
            unresolved_bets_dict = {doc.id: doc.to_dict() for doc in bets}
            print(f"[DEBUG] Found {len(unresolved_bets_dict)} unresolved bets.")
            return unresolved_bets_dict
        except Exception as e:
            print(f"❌ Firestore Error during get_unresolved_bets: {e}")
            return {}
    
    def add_unresolved_bet(self, match_id, data):
        print(f"[DEBUG] Adding unresolved bet for match ID: {match_id} with data: {data}")
        try:
            self.db.collection('unresolved_bets').document(str(match_id)).set(data)
            print(f"[DEBUG] Successfully added unresolved bet for match ID: {match_id}")
        except Exception as e:
            print(f"❌ Firestore Error during add_unresolved_bet: {e}")
        
    def resolve_bet(self, match_id):
        print(f"[DEBUG] Resolving bet for match ID: {match_id}")
        try:
            self.db.collection('unresolved_bets').document(str(match_id)).delete()
            print(f"[DEBUG] Successfully resolved bet for match ID: {match_id}")
        except Exception as e:
            print(f"❌ Firestore Error during resolve_bet: {e}")

# Initialize Firebase
try:
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception:
    exit()

def send_telegram(msg):
    print(f"[DEBUG] Preparing to send Telegram message...")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg}
    print(f"📤 Sending:\n{msg}\n")
    try:
        response = requests.post(url, data=data, timeout=10)
        print("✅ Sent" if response.status_code == 200 else f"❌ Telegram error: {response.text}")
        return response
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error sending Telegram message: {e}")
        return None

def get_live_matches():
    print(f"[DEBUG] Making API call to get live matches...")
    try:
        res = requests.get(f"{BASE_URL}/fixtures?live=all", headers=HEADERS, timeout=10)
        print(f"[DEBUG] API call status code: {res.status_code}")
        if res.status_code != 200:
            print(f"❌ API ERROR: {res.status_code} - {res.text}")
            return []
        try:
            response_data = res.json()['response']
            print(f"[DEBUG] Found {len(response_data)} live matches.")
            return response_data
        except Exception as e:
            print(f"❌ JSON Error: {e}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error: {e}")
        return []

def fetch_match_result(match_id):
    print(f"[DEBUG] Fetching final result for match ID: {match_id}")
    try:
        res = requests.get(f"{BASE_URL}/fixtures?id={match_id}", headers=HEADERS, timeout=10)
        print(f"[DEBUG] Result fetch status code: {res.status_code}")
        if res.status_code != 200:
            print(f"❌ Error fetching match {match_id}")
            return None
        data = res.json().get('response', [])
        return data[0] if data else None
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error fetching match {match_id}: {e}")
        return None

def process_match(match):
    fixture_id = match['fixture']['id']
    match_name = f"{match['teams']['home']['name']} vs {match['teams']['away']['name']}"
    minute = match['fixture']['status']['elapsed']
    score = f"{match['goals']['home']}-{match['goals']['away']}"
    print(f"[DEBUG] Processing match {match_name} (ID: {fixture_id}) at minute {minute} with score {score}")

    state = firebase_manager.get_tracked_match(fixture_id)
    if not state:
        print(f"[DEBUG] No existing state found for {match_name}, creating new entry.")
        state = {'36_bet_placed': False, '80_bet_placed': False}
        firebase_manager.update_tracked_match(fixture_id, state)

    if 35 <= minute <= 37 and not state.get('36_bet_placed'):
        print(f"[DEBUG] Checking 36' bet condition for {match_name}. Current score: {score}")
        if score in ['1-1', '2-2', '3-3']:
            state['score_36'] = score
            state['36_bet_placed'] = True
            firebase_manager.update_tracked_match(fixture_id, state)
            send_telegram(f"⏱️ 36' - {match_name}\n🔢 Score: {score}\n🎯 Bet: Goals after 36'")
        else:
            print(f"⛔ Skipping 36' bet for {match_name} — score {score} is not a tie (1-1, 2-2, or 3-3)")
    
    if 79 <= minute <= 81 and state.get('36_bet_placed') and not state.get('80_bet_placed'):
        print(f"[DEBUG] Checking 80' chase bet condition for {match_name}. Current score: {score}")
        score_80 = score
        state['score_80'] = score_80
        state['80_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)
        send_telegram(f"⏱️ 80' - {match_name}\n🔢 Score: {score_80}\n🎯 Chase Bet Placed")
        unresolved_data = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'score_80': score_80,
            'league': match['league']['name'],
            'league_id': match['league']['id'],
            'bet_type': '80'
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        
def check_unresolved_80_bets():
    print("[DEBUG] Checking for unresolved 80' bets...")
    unresolved_bets = firebase_manager.get_unresolved_bets('80')

    for match_id, info in unresolved_bets.items():
        placed_time = datetime.fromisoformat(info['placed_at'])
        if datetime.utcnow() - placed_time < timedelta(minutes=15):
            print(f"[DEBUG] Skipping resolution for match {match_id} (not enough time passed).")
            continue

        match_data = fetch_match_result(match_id)
        if not match_data:
            print(f"⚠️ Match {match_id} not found in API. Skipping resolution.")
            continue

        status = match_data['fixture']['status']['short']
        final_score = f"{match_data['goals']['home']}-{match_data['goals']['away']}"
        print(f"[DEBUG] Final status for {info['match_name']} is {status} with score {final_score}")

        if status == 'FT':
            if final_score != info['score_80']:
                send_telegram(f"✅ FT Result: {info['match_name']}\n🔢 Score: {final_score}\n🎉 80' Chase Bet WON")
            else:
                send_telegram(f"❌ FT Result: {info['match_name']}\n🔢 Score: {final_score}\n📉 80' Chase Bet LOST")
            firebase_manager.resolve_bet(match_id)

def run_bot_once():
    print(f"[{datetime.now()}] 🔍 Checking live matches...")
    live_matches = get_live_matches()

    for match in live_matches:
        process_match(match)

    check_unresolved_80_bets()
    print(f"[{datetime.now()}] ✅ Cycle complete.")

if __name__ == "__main__":
    print("🚀 Bot worker started")
    while True:
        try:
            run_bot_once()
        except Exception as e:
            print(f"❌ Unexpected error in main loop: {e}")
        finally:
            print(f"[{datetime.now()}] 💤 Sleeping for 90 seconds...")
            time.sleep(90)
