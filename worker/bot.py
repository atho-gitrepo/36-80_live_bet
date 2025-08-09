import requests
import os
import json
import time
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

# Load environment variables
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
            cred_dict = json.loads(credentials_json_string)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            print("✅ Firebase initialized successfully")
        except Exception as e:
            print(f"❌ Failed to initialize Firebase: {e}")
            raise

    def get_tracked_match(self, match_id):
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        try:
            doc = doc_ref.get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            print(f"❌ Firestore Error during get_tracked_match: {e}")
            return None

    def update_tracked_match(self, match_id, data):
        doc_ref = self.db.collection('tracked_matches').document(str(match_id))
        try:
            doc_ref.set(data, merge=True)
        except Exception as e:
            print(f"❌ Firestore Error during update_tracked_match: {e}")

    def get_unresolved_bets(self, bet_type=None):
        try:
            col_ref = self.db.collection('unresolved_bets')
            if bet_type:
                query = col_ref.where('bet_type', '==', bet_type)
            else:
                query = col_ref
            bets = query.stream()
            return {doc.id: doc.to_dict() for doc in bets}
        except Exception as e:
            print(f"❌ Firestore Error during get_unresolved_bets: {e}")
            return {}
    
    def add_unresolved_bet(self, match_id, data):
        try:
            self.db.collection('unresolved_bets').document(str(match_id)).set(data)
        except Exception as e:
            print(f"❌ Firestore Error during add_unresolved_bet: {e}")

    def move_to_resolved(self, match_id, bet_info, outcome):
        resolved_bet_ref = self.db.collection('resolved_bets').document(str(match_id))
        try:
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().isoformat()
            } 
            resolved_bet_ref.set(resolved_data)
            self.db.collection('unresolved_bets').document(str(match_id)).delete()
        except Exception as e:
            print(f"❌ Firestore Error during move_to_resolved: {e}")

# Initialize Firebase
try:
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception as e:
    print(f"❌ Critical Firebase initialization error: {e}")
    exit(1)

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg}
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code != 200:
            print(f"❌ Telegram error: {response.text}")
        return response
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error sending Telegram message: {e}")
        return None

def handle_api_rate_limit(response):
    """Handle API rate limiting by adjusting sleep time"""
    if response.status_code == 429:
        retry_after = int(response.headers.get('Retry-After', 60))
        print(f"⏳ Rate limited. Sleeping for {retry_after} seconds")
        time.sleep(retry_after)
        return True
    return False

def get_live_matches():
    """Fetch ONLY live matches from API"""
    print("🔍 Fetching live matches...")
    url = f"{BASE_URL}/fixtures?live=all"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        # Handle rate limiting
        if handle_api_rate_limit(response):
            return get_live_matches()  # Retry after sleep
        
        if response.status_code != 200:
            print(f"❌ API ERROR: {response.status_code} - {response.text}")
            return []
            
        data = response.json()
        matches = data.get('response', [])
        print(f"✅ Found {len(matches)} live matches")
        return matches
    except Exception as e:
        print(f"❌ API Error: {e}")
        return []

def get_fixtures_by_ids(match_ids):
    """Fetch specific fixtures by their IDs"""
    if not match_ids:
        return {}
    
    print(f"🔍 Fetching {len(match_ids)} unresolved matches")
    ids_param = '-'.join(str(mid) for mid in match_ids)
    url = f"{BASE_URL}/fixtures?ids={ids_param}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        
        # Handle rate limiting
        if handle_api_rate_limit(response):
            return get_fixtures_by_ids(match_ids)  # Retry after sleep
            
        if response.status_code != 200:
            print(f"❌ API ERROR: {response.status_code} - {response.text}")
            return {}
            
        data = response.json()
        fixtures = data.get('response', [])
        return {str(f['fixture']['id']): f for f in fixtures}
    except Exception as e:
        print(f"❌ Fixture Lookup Error: {e}")
        return {}

def process_match(match):
    fixture = match['fixture']
    teams = match['teams']
    league = match['league']
    goals = match['goals']
    
    fixture_id = fixture['id']
    match_name = f"{teams['home']['name']} vs {teams['away']['name']}"
    league_name = league['name']
    league_id = league['id']
    country = league.get('country', 'N/A')
    minute = fixture['status']['elapsed']
    status = fixture['status']['short'] 
    
    # Handle possible None scores and minutes
    home_goals = goals['home'] if goals['home'] is not None else 0
    away_goals = goals['away'] if goals['away'] is not None else 0
    score = f"{home_goals}-{away_goals}"
    
    # Skip non-live matches (case-insensitive check)
    if status.upper() not in ['LIVE', 'HT', '1H', '2H']:
        return
        
    # Skip matches without minute data
    if minute is None:
        print(f"⚠️ Skipping {match_name} - no minute data (status: {status})")
        return
    
    print(f"⚽ Processing: {match_name} ({minute}' {score}) [ID: {fixture_id}]")
    
    # Get or create match state
    state = firebase_manager.get_tracked_match(fixture_id)
    if not state:
        state = {
            '36_bet_placed': False,
            '36_result_checked': False,
            '80_bet_placed': False,
            'skip_80': False
        }
        firebase_manager.update_tracked_match(fixture_id, state)
    else:
        # Ensure all state keys exist
        state.setdefault('36_bet_placed', False)
        state.setdefault('36_result_checked', False)
        state.setdefault('80_bet_placed', False)
        state.setdefault('skip_80', False)

    # ✅ Place 36' Bet (Widened window to 35-42 minutes)
    if status.upper() == '1H' and 35 <= minute <= 42 and not state.get('36_bet_placed'):
        print(f"🔍 Checking 36' bet for {match_name} at {minute}'")
        state['score_36'] = score
        unresolved_data_base = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'league': league_name,
            'country': country,
            'league_id': league_id,
        }
        
        # Only place bets for 1-1, 2-2, or 3-3 scores
        if score in ['1-1', '2-2', '3-3']:
            print("✅ Placing Regular bet")
            state['36_bet_placed'] = True
            firebase_manager.update_tracked_match(fixture_id, state)
            send_telegram(f"⏱️ 36' - {match_name}\n🏆{league_name} ({country})\n🔢 Score: {score}\n🎯 First Bet Placed")
            unresolved_data = {**unresolved_data_base, 'bet_type': 'regular'}
            firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        else:
            print(f"⛔ No 36' bet for {match_name} - score {score} not in strategy")
            # Mark as placed to avoid retrying
            state['36_bet_placed'] = True
            firebase_manager.update_tracked_match(fixture_id, state)

    # ✅ Check HT result for regular bets
    if status.upper() == 'HT' and state.get('36_bet_placed') and not state.get('36_result_checked'):
        current_score = score
        unresolved_bet_data = firebase_manager.get_unresolved_bets('regular').get(str(fixture_id))
        
        if not unresolved_bet_data:
            print(f"⚠️ No unresolved bet found for {match_name} at HT")
            state['36_result_checked'] = True
            firebase_manager.update_tracked_match(fixture_id, state)
            return
            
        if current_score == state.get('score_36', ''):
            send_telegram(f"✅ HT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {current_score}\n🎉 36' Bet WON")
            state['skip_80'] = True
            firebase_manager.move_to_resolved(fixture_id, unresolved_bet_data, 'win')
        else:
            send_telegram(f"❌ HT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {current_score}\n🔁 36' Bet LOST — chasing at 80'")
            state['skip_80'] = False
            
        state['36_result_checked'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

    # ✅ Place 80' Chase Bet (Widened window to 79-85 minutes)
    if status.upper() == '2H' and 79 <= minute <= 85 and state.get('36_result_checked') and not state.get('skip_80') and not state.get('80_bet_placed'):
        print(f"🔍 Placing 80' chase bet for {match_name} at {minute}'")
        state['score_80'] = score
        state['80_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)
        send_telegram(f"⏱️ 80' - {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {score}\n🎯 Chase Bet: Betting this will be the final result")
        unresolved_data = {
            'match_name': match_name,
            'placed_at': datetime.utcnow().isoformat(),
            'score_80': score,
            'league': league_name,
            'league_id': league_id,
            'country': country,
            'bet_type': '80'
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)

def check_unresolved_bets():
    """Check ALL unresolved bets regardless of match date"""
    print("🔍 Checking unresolved bets...")
    
    # Get all unresolved bets
    unresolved_bets = firebase_manager.get_unresolved_bets()
    if not unresolved_bets:
        print("✅ No unresolved bets found")
        return
        
    match_ids = list(unresolved_bets.keys())
    fixtures = get_fixtures_by_ids(match_ids)
    
    for match_id, bet_info in unresolved_bets.items():
        if match_id not in fixtures:
            continue
            
        match_data = fixtures[match_id]
        fixture = match_data['fixture']
        status = fixture['status']['short']
        
        # Only process finished matches
        if status != 'FT':
            continue
            
        home_goals_ft = match_data['goals']['home'] or 0
        away_goals_ft = match_data['goals']['away'] or 0
        final_score = f"{home_goals_ft}-{away_goals_ft}"
        match_name = bet_info.get('match_name', f"Match {match_id}")
        league_name = bet_info.get('league', 'Unknown League')
        bet_type = bet_info['bet_type']
        country = bet_info.get('country', 'N/A')
        
        outcome = None
        message = ""
        
        # --- Resolution Logic ---
        if bet_type == 'regular':
            # This should have been resolved at HT
            outcome = 'error'
            message = f"⚠️ FT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {final_score}\n❓ Regular bet was not resolved at HT. Marked as error."
            
        elif bet_type == '80':
            # Get score at 80' from bet info
            score_80 = bet_info.get('score_80', '')
            # Win if final score matches 80' score
            if final_score == score_80:
                outcome = 'win'
                message = f"✅ FT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {final_score}\n🎉 80' Chase Bet WON (same as 80')"
            else:
                outcome = 'loss'
                message = f"❌ FT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {final_score}\n📉 80' Chase Bet LOST (was {score_80} at 80')"
                
        else:
            # Handle unknown bet types
            outcome = 'error'
            message = f"⚠️ FT Result: {match_name}\n🏆 {league_name} ({country})\n🔢 Score: {final_score}\n❓ Unknown bet type: {bet_type}"
        
        if outcome:
            send_telegram(message)
            firebase_manager.move_to_resolved(match_id, bet_info, outcome)

def run_bot_once():
    """Run one complete cycle of the bot"""
    print(f"\n⏰ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting new cycle")
    
    # Process live matches
    live_matches = get_live_matches()
    for match in live_matches:
        process_match(match)
    
    # Check unresolved bets
    check_unresolved_bets()
    
    print(f"✅ Cycle completed at {datetime.now().strftime('%H:%M:%S')}")

def health_check():
    """Periodic health check notification"""
    if datetime.now().minute % 30 == 0:  # Every 30 minutes
        send_telegram(f"🤖 Bot is active | Last cycle: {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
    print("🚀 Starting Football Betting Bot")
    cycle_count = 0
    
    while True:
        try:
            cycle_count += 1
            run_bot_once()
            health_check()
        except Exception as e:
            error_msg = f"🔥 CRITICAL ERROR: {str(e)[:300]}"
            print(error_msg)
            send_telegram(error_msg)
            # Exponential backoff on errors
            time.sleep(min(300, 5 * 2 ** cycle_count))
        finally:
            sleep_time = 90  # 1.5 minutes
            print(f"💤 Sleeping for {sleep_time} seconds...")
            time.sleep(sleep_time)