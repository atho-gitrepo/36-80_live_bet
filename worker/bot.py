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
            if not credentials_json_string:
                raise ValueError("FIREBASE_CREDENTIALS_JSON is empty. Please set the environment variable.")
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
        # Add this new function to your FirebaseManager class.
        # It replaces the old resolve_bet function.
    def move_to_resolved(self, match_id, bet_info, outcome):
            print(f"[DEBUG] Moving resolved bet for match ID: {match_id} to resolved_bets collection...")
            resolved_bet_ref = self.db.collection('resolved_bets').document(str(match_id))
            try:
                resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().isoformat()
                } 
                resolved_bet_ref.set(resolved_data)
                self.db.collection('unresolved_bets').document(str(match_id)).delete()
                print(f"[DEBUG] Successfully moved bet for match ID: {match_id}")
            except Exception as e:
                print(f"❌ Firestore Error during move_to_resolved: {e}")


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

def get_daily_matches():
    print(f"[DEBUG] Making single API call to get all daily matches...")
    today_date = datetime.now().strftime('%Y-%m-%d')
    url = f"{BASE_URL}/fixtures?date={today_date}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        print(f"[DEBUG] API call status code: {res.status_code}")
        #print(f"[DEBUG] Raw API response: {res.text}")
        if res.status_code != 200:
            print(f"❌ API ERROR: {res.status_code} - {res.text}")
            return []
        try:
            response_data = res.json()['response']
            print(f"[DEBUG] Found {len(response_data)} matches for today.")
            return response_data
        except Exception as e:
            print(f"❌ JSON Error: {e}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error: {e}")
        return []

def process_match(match):
    fixture_id = match['fixture']['id']
    match_name = f"{match['teams']['home']['name']} vs {match['teams']['away']['name']}"
    league = match['league']['name']
    league_id = match['league']['id']
    minute = match['fixture']['status']['elapsed']
    status = match['fixture']['status']['short']
    score = f"{match['goals']['home']}-{match['goals']['away']}"
    home_goals = match['goals']['home']
    away_goals = match['goals']['away']
     # Add debug logs for basic match info
    print(f"[DEBUG] Match: {match_name} | Status: {status} | Minute: {minute} | Score: {score}")
    print(f"[DEBUG] Score type: {type(score)}, Value: '{score}'")
    
    # We only process matches that are currently live
    if status != 'Live' and status != 'HT':
        print(f"[DEBUG] Skipping match - not live or HT (status: {status})")
        return
    
    print(f"[DEBUG] Processing match {match_name} (ID: {fixture_id}) at minute {minute} with score {score}")
    
    state = firebase_manager.get_tracked_match(fixture_id)
    print(f"[DEBUG] Current state from Firebase: {state}")
    if not state:
        print(f"[DEBUG] No existing state found for {match_name}, creating new entry.")
        state = {
            '36_bet_placed': False,
            '36_result_checked': False,
            '80_bet_placed': False,
            '36_bet_type': None,
            'skip_80': False
        }
        firebase_manager.update_tracked_match(fixture_id, state)

    # ✅ Place 36' Bet
    if 35 <= minute <= 37:
        print(f"[DEBUG] In 36' window (minute: {minute})")
        print(f"[DEBUG] 36_bet_placed: {state.get('36_bet_placed')}")
        if not state.get('36_bet_placed'):
        print(f"[DEBUG] Checking 36' bet condition for {match_name}. Current score: {score}")
        state['score_36'] = score
        
        unresolved_data_base = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'league': league,
            'league_id': league_id,
        }
        
        if score in ['1-0', '0-1']:
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'over_2.5'
            firebase_manager.update_tracked_match(fixture_id, state)
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score}\n🎯 Bet: Over 2.5 Goals FT")
            unresolved_data = {**unresolved_data_base, 'bet_type': 'over_2.5', 'home_goals_36': home_goals, 'away_goals_36': away_goals}
            firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        elif score in ['1-1', '2-2', '3-3']:
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'regular'
            firebase_manager.update_tracked_match(fixture_id, state)
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score}\n🎯 First Bet Placed")
            unresolved_data = {**unresolved_data_base, 'bet_type': 'regular'}
            firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        elif score == '0-0':
            state['36_bet_placed'] = True
            state['36_bet_type'] = 'no_draw'
            firebase_manager.update_tracked_match(fixture_id, state)
            send_telegram(f"⏱️ 36' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score}\n🎯 Bet: No Draw FT")
            unresolved_data = {**unresolved_data_base, 'bet_type': 'no_draw', 'home_goals_36': home_goals, 'away_goals_36': away_goals}
            firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        else:
            print(f"⛔ Skipping 36' bet for {match_name} — score {score} not in allowed range")

    # ✅ Check HT result and move regular bet to resolved
    if status == 'HT' and state.get('36_bet_placed') and not state.get('36_result_checked') and state.get('36_bet_type') == 'regular':
        current_score = score
        # Fetch the unresolved bet data to pass to the resolved collection
        unresolved_bet_data = firebase_manager.get_unresolved_bets('regular').get(str(fixture_id))
        
        if current_score == state['score_36']:
            send_telegram(f"✅ HT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {current_score}\n🎉 36' Bet WON")
            state['skip_80'] = True
            if unresolved_bet_data:
                firebase_manager.move_to_resolved(fixture_id, unresolved_bet_data, 'win')
        else:
            send_telegram(f"❌ HT Result: {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {current_score}\n🔁 36' Bet LOST — chasing at 80'")
            state['skip_80'] = False
            if unresolved_bet_data:
                firebase_manager.move_to_resolved(fixture_id, unresolved_bet_data, 'loss')
        
        state['36_result_checked'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

    # ✅ Place 80' Chase Bet only if 36' bet failed and not skipped
    if 79 <= minute <= 81 and state.get('36_result_checked') and not state.get('skip_80') and not state.get('80_bet_placed'):
        score_80 = score
        state['score_80'] = score_80
        state['80_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)
        send_telegram(f"⏱️ 80' - {match_name}\n🏆 {league}\n🏷️ League ID: {league_id}\n🔢 Score: {score_80}\n🎯 Chase Bet Placed")
        unresolved_data = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'score_80': score_80,
            'league': league,
            'league_id': league_id,
            'bet_type': '80'
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)

def check_unresolved_bets(daily_matches):
    print(f"[DEBUG] Checking for unresolved bets...")
    
    # Get all unresolved bets from Firestore
    all_unresolved = {
        'over_2.5': firebase_manager.get_unresolved_bets('over_2.5'),
        'no_draw': firebase_manager.get_unresolved_bets('no_draw'),
        '80': firebase_manager.get_unresolved_bets('80')
    }
    
    # Create a lookup table for today's matches
    daily_match_lookup = {str(match['fixture']['id']): match for match in daily_matches}

    for bet_type, unresolved_bets in all_unresolved.items():
        for match_id, info in unresolved_bets.items():
            
            # Check if match exists in today's data and has a final status
            if match_id in daily_match_lookup and daily_match_lookup[match_id]['fixture']['status']['short'] == 'FT':
                
                match_data = daily_match_lookup[match_id]
                final_score = f"{match_data['goals']['home']}-{match_data['goals']['away']}"
                home_goals_ft = match_data['goals']['home']
                away_goals_ft = match_data['goals']['away']
                
                outcome = None
                
                # --- Resolution Logic for different bet types ---
                if bet_type == 'over_2.5':
                    if (home_goals_ft + away_goals_ft) > 1:
                        outcome = 'win'
                        send_telegram(f"✅ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n🎉 Over 2.5 Goals Bet WON")
                    else:
                        outcome = 'loss'
                        send_telegram(f"❌ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n📉 Over 2.5 Goals Bet LOST")
                elif bet_type == 'no_draw':
                    if home_goals_ft != away_goals_ft:
                        outcome = 'win'
                        send_telegram(f"✅ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n🎉 No Draw Bet WON")
                    else:
                        outcome = 'loss'
                        send_telegram(f"❌ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n📉 No Draw Bet LOST")
                elif bet_type == '80':
                    if final_score != info['score_80']:
                        outcome = 'win'
                        send_telegram(f"✅ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n🎉 80' Chase Bet WON")
                    else:
                        outcome = 'loss'
                        send_telegram(f"❌ FT Result: {info['match_name']}\n🏆 {info['league']}\n🔢 Score: {final_score}\n📉 80' Chase Bet LOST")
                
                if outcome:
                    firebase_manager.move_to_resolved(match_id, info, outcome)

def run_bot_once():
    print(f"[{datetime.now()}] 🔍 Checking all daily matches...")
    
    # ONE API CALL HERE
    daily_matches = get_daily_matches()

    for match in daily_matches:
        process_match(match)

    check_unresolved_bets(daily_matches)
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
