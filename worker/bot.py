import requests
import os
import json
import time
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FIREBASE_CREDENTIALS_JSON_STRING = os.getenv("FIREBASE_CREDENTIALS_JSON")

# Validate critical environment variables
if not all([API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, FIREBASE_CREDENTIALS_JSON_STRING]):
    logger.error("Missing one or more required environment variables")
    sys.exit(1)

HEADERS = {'x-apisports-key': API_KEY}
BASE_URL = 'https://v3.football.api-sports.io'

class FirebaseManager:
    """Enhanced Firebase Firestore manager with fixed transaction handling"""
    
    def __init__(self, credentials_json_string):
        try:
            logger.info("Initializing Firebase connection")
            if not credentials_json_string:
                raise ValueError("Firebase credentials JSON string is empty")
                
            cred_dict = json.loads(credentials_json_string)
            cred = credentials.Certificate(cred_dict)
            
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
                
            self.db = firestore.client()
            logger.info("Firebase initialized successfully")
            
        except json.JSONDecodeError:
            logger.error("Invalid JSON in Firebase credentials")
            raise
        except Exception as e:
            logger.error(f"Firebase initialization failed: {str(e)}")
            raise

    def get_tracked_match(self, match_id):
        """Retrieve tracked match data with error handling"""
        try:
            doc_ref = self.db.collection('tracked_matches').document(str(match_id))
            doc = doc_ref.get()
            
            if not doc.exists:
                logger.debug(f"No tracked match found for ID: {match_id}")
                return None
                
            logger.debug(f"Retrieved tracked match data for ID: {match_id}")
            return doc.to_dict()
            
        except Exception as e:
            logger.error(f"Error getting tracked match {match_id}: {str(e)}")
            return None

    def update_tracked_match(self, match_id, data):
        """Update tracked match with automatic cleanup of None values"""
        try:
            # Remove None values to save space
            clean_data = {k: v for k, v in data.items() if v is not None}
            logger.debug(f"Updating tracked match {match_id} with clean data")
            
            doc_ref = self.db.collection('tracked_matches').document(str(match_id))
            doc_ref.set(clean_data, merge=True)
            logger.info(f"Successfully updated tracked match {match_id}")
            
        except Exception as e:
            logger.error(f"Error updating tracked match {match_id}: {str(e)}")
            raise

    def get_unresolved_bets(self, bet_type=None):
        """Retrieve unresolved bets with optional filtering"""
        try:
            col_ref = self.db.collection('unresolved_bets')
            query = col_ref.where('bet_type', '==', bet_type) if bet_type else col_ref
            
            bets = query.stream()
            result = {doc.id: doc.to_dict() for doc in bets}
            
            logger.info(f"Retrieved {len(result)} unresolved bets" + 
                       f" of type {bet_type}" if bet_type else "")
            return result
            
        except Exception as e:
            logger.error(f"Error getting unresolved bets: {str(e)}")
            return {}

    def add_unresolved_bet(self, match_id, data):
        """Fixed version: Add unresolved bet with proper transaction handling"""
        @firestore.transactional
        def add_bet_transaction(transaction):
            # Get document references
            match_ref = self.db.collection('tracked_matches').document(str(match_id))
            unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
            
            # Properly get the document snapshot within the transaction
            match_snapshot = transaction.get(match_ref)
            
            if not match_snapshot.exists:
                raise ValueError(f"Match {match_id} not found in tracked_matches")
                
            # Add to unresolved bets
            transaction.set(unresolved_ref, data)
            
            # Update tracked match state if needed
            if 'tracked_updates' in data:
                transaction.set(match_ref, data['tracked_updates'], merge=True)
        
        try:
            logger.info(f"Adding unresolved bet for match {match_id}")
            transaction = self.db.transaction()
            add_bet_transaction(transaction)
            logger.info(f"Successfully added unresolved bet for match {match_id}")
            
        except Exception as e:
            logger.error(f"Failed to add unresolved bet for match {match_id}: {str(e)}")
            raise

    def move_to_resolved(self, match_id, bet_info, outcome):
        """Atomic operation to move bet to resolved and clean up"""
        @firestore.transactional
        def resolve_transaction(transaction):
            # Get document references
            unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
            resolved_ref = self.db.collection('resolved_bets').document(str(match_id))
            tracked_ref = self.db.collection('tracked_matches').document(str(match_id))
            
            # Verify the bet is still unresolved
            unresolved_snap = transaction.get(unresolved_ref)
            if not unresolved_snap.exists:
                raise ValueError(f"Bet {match_id} not found in unresolved_bets")
            
            # Create resolved bet record
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().isoformat(),
                'resolution_data': bet_info.get('match_data', {})
            }
            transaction.set(resolved_ref, resolved_data)
            
            # Remove from unresolved bets
            transaction.delete(unresolved_ref)
            
            # Check if we can remove from tracked_matches
            tracked_snap = transaction.get(tracked_ref)
            if tracked_snap.exists:
                tracked_data = tracked_snap.to_dict()
                if self._can_remove_tracked_match(tracked_data, bet_info.get('bet_type')):
                    transaction.delete(tracked_ref)
                    logger.info(f"Removed tracked match {match_id} after resolution")
                else:
                    # Update with resolution info
                    updates = {
                        f"{bet_info.get('bet_type', 'unknown')}_resolved": True,
                        'last_update': datetime.utcnow().isoformat()
                    }
                    transaction.set(tracked_ref, updates, merge=True)
        
        try:
            logger.info(f"Resolving bet {match_id} with outcome: {outcome}")
            transaction = self.db.transaction()
            resolve_transaction(transaction)
            logger.info(f"Successfully resolved bet {match_id}")
            
        except Exception as e:
            logger.error(f"Failed to resolve bet {match_id}: {str(e)}")
            raise

    def _can_remove_tracked_match(self, tracked_data, bet_type):
        """Determine if a tracked match can be safely removed"""
        if bet_type == 'chase':
            return True
        if bet_type == 'regular' and tracked_data.get('36_bet_won') is False:
            return False
        return True

    def cleanup_old_matches(self, days_threshold=7):
        """Periodically clean up old tracked matches"""
        try:
            logger.info(f"Cleaning up matches older than {days_threshold} days")
            cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)
            
            query = (self.db.collection('tracked_matches')
                    .where('last_update', '<', cutoff_date.isoformat()))
            
            deleted_count = 0
            for doc in query.stream():
                try:
                    unresolved_ref = self.db.collection('unresolved_bets').document(doc.id)
                    if not unresolved_ref.get().exists:
                        doc.reference.delete()
                        deleted_count += 1
                except Exception as e:
                    logger.warning(f"Could not delete match {doc.id}: {str(e)}")
            
            logger.info(f"Deleted {deleted_count} old tracked matches")
            return deleted_count
            
        except Exception as e:
            logger.error(f"Error during match cleanup: {str(e)}")
            return 0

# Initialize Firebase
try:
    logger.info("Initializing Firebase manager")
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception as e:
    logger.critical(f"Failed to initialize Firebase: {str(e)}")
    sys.exit(1)

def send_telegram(message):
    """Send Telegram notification with retry logic"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                logger.error(f"Failed to send Telegram message: {str(e)}")
                return False

def make_api_request(url, headers, timeout=15, max_retries=3):
    """Make API request with retry logic"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                raise

def get_live_matches():
    """Fetch live matches from API"""
    url = f"{BASE_URL}/fixtures?live=all"
    try:
        response = make_api_request(url, HEADERS)
        return response.json().get('response', [])
    except Exception as e:
        logger.error(f"Error getting live matches: {str(e)}")
        return []

def get_fixtures_by_ids(match_ids):
    """Fetch specific fixtures by IDs"""
    if not match_ids:
        return {}
        
    fixtures = {}
    chunk_size = 20
    
    for i in range(0, len(match_ids), chunk_size):
        chunk = match_ids[i:i + chunk_size]
        url = f"{BASE_URL}/fixtures?ids={'-'.join(str(mid) for mid in chunk)}&status=FT"
        
        try:
            response = make_api_request(url, HEADERS, timeout=25)
            for f in response.json().get('response', []):
                fixtures[str(f['fixture']['id'])] = f
        except Exception as e:
            logger.error(f"Error fetching fixtures: {str(e)}")
            continue
            
    return fixtures

def should_process_match(status, elapsed):
    """Check if match should be processed"""
    return status.upper() in ['LIVE', 'HT', '1H', '2H'] and elapsed is not None

def initialize_match_state(match_id):
    """Initialize default match state"""
    state = {
        '36_bet_placed': False,
        '36_result_checked': False,
        '36_bet_won': None,
        '80_bet_placed': False,
        '80_bet_resolved': False,
        '36_score': None,
        'ht_score': None,
        '80_score': None,
        'last_update': datetime.utcnow().isoformat()
    }
    firebase_manager.update_tracked_match(match_id, state)
    return state

def process_match(match):
    """Process a single match"""
    try:
        fixture = match['fixture']
        fixture_id = fixture['id']
        status = fixture['status']['short']
        elapsed = fixture['status']['elapsed']
        
        if not should_process_match(status, elapsed):
            return
            
        teams = match['teams']
        league = match['league']
        goals = match['goals']
        
        match_name = f"{teams['home']['name']} vs {teams['away']['name']}"
        league_name = league['name']
        country = league.get('country', 'N/A')
        current_score = f"{goals['home'] or 0}-{goals['away'] or 0}"
        
        state = firebase_manager.get_tracked_match(fixture_id) or initialize_match_state(fixture_id)
        
        # 36' Bet Logic
        if (status.upper() == '1H' and 35 <= elapsed <= 37 and not state['36_bet_placed']):
            process_36min_bet(fixture_id, match_name, league_name, country, current_score, state, match)
        
        # HT Check
        if (status.upper() == 'HT' and state['36_bet_placed'] and not state['36_result_checked']):
            process_ht_check(fixture_id, match_name, league_name, country, current_score, state)
        
        # 80' Chase Bet
        if (status.upper() == '2H' and 79 <= elapsed <= 81 and not state['80_bet_placed'] and state.get('36_bet_won') is False):
            process_80min_chase(fixture_id, match_name, league_name, country, current_score, state, match)
            
    except Exception as e:
        logger.error(f"Error processing match: {str(e)}\n{traceback.format_exc()}")

def process_36min_bet(fixture_id, match_name, league_name, country, current_score, state, match_data):
    """Handle 36' bet logic"""
    bet_data = {
        'match_name': match_name,
        'placed_at': datetime.utcnow().isoformat(),
        'league': league_name,
        'country': country,
        'league_id': match_data['league']['id'],
        'initial_score': current_score,
        'bet_type': 'regular',
        'match_data': {
            'fixture_id': fixture_id,
            'teams': match_data['teams'],
            'league': match_data['league']
        },
        'tracked_updates': {
            '36_bet_placed': True,
            '36_score': current_score,
            'last_update': datetime.utcnow().isoformat()
        }
    }
    
    if current_score in ['0-0', '1-1', '2-2', '3-3']:
        firebase_manager.add_unresolved_bet(fixture_id, bet_data)
        send_telegram(
            f"⏱️ 36' - {match_name}\n"
            f"🏆 {league_name} ({country})\n"
            f"🔢 Score: {current_score}\n"
            f"🎯 Correct Score Bet Placed"
        )
    else:
        firebase_manager.update_tracked_match(fixture_id, bet_data['tracked_updates'])

def process_ht_check(fixture_id, match_name, league_name, country, current_score, state):
    """Handle HT result check"""
    firebase_manager.update_tracked_match(fixture_id, {
        'ht_score': current_score,
        'last_update': datetime.utcnow().isoformat()
    })
    
    unresolved_bet = firebase_manager.get_unresolved_bets('regular').get(str(fixture_id))
    if not unresolved_bet:
        logger.warning(f"No unresolved bet found for {match_name} at HT")
        firebase_manager.update_tracked_match(fixture_id, {
            '36_result_checked': True,
            'last_update': datetime.utcnow().isoformat()
        })
        return
        
    outcome = 'win' if current_score == state['36_score'] else 'loss'
    
    if outcome == 'win':
        message = (f"✅ HT Result: {match_name}\n"
                  f"🏆 {league_name} ({country})\n"
                  f"🔢 Score: {current_score}\n"
                  f"🎉 36' Bet WON")
    else:
        message = (f"❌ HT Result: {match_name}\n"
                  f"🏆 {league_name} ({country})\n"
                  f"🔢 Score: {current_score}\n"
                  f"🔁 36' Bet LOST — eligible for chase")
    
    send_telegram(message)
    firebase_manager.move_to_resolved(fixture_id, unresolved_bet, outcome)
    
    firebase_manager.update_tracked_match(fixture_id, {
        '36_result_checked': True,
        '36_bet_won': outcome == 'win',
        'last_update': datetime.utcnow().isoformat()
    })

def process_80min_chase(fixture_id, match_name, league_name, country, current_score, state, match_data):
    """Handle 80' chase bet logic"""
    bet_data = {
        'match_name': match_name,
        'placed_at': datetime.utcnow().isoformat(),
        'league': league_name,
        'country': country,
        'league_id': match_data['league']['id'],
        'bet_type': 'chase',
        '36_score': state['36_score'],
        'ht_score': state['ht_score'],
        '80_score': current_score,
        'initial_score': current_score,
        'match_data': {
            'fixture_id': fixture_id,
            'teams': match_data['teams'],
            'league': match_data['league']
        },
        'tracked_updates': {
            '80_bet_placed': True,
            '80_score': current_score,
            'last_update': datetime.utcnow().isoformat()
        }
    }
    
    firebase_manager.add_unresolved_bet(fixture_id, bet_data)
    send_telegram(
        f"⏱️ 80' CHASE BET: {match_name}\n"
        f"🏆 {league_name} ({country})\n"
        f"🔢 Score: {current_score}\n"
        f"🎯 Betting for Correct Score\n"
        f"💡 Covering lost 36' bet ({state['36_score']} -> {state['ht_score']})"
    )

def check_unresolved_bets():
    """Check and resolve all outstanding bets"""
    unresolved_bets = firebase_manager.get_unresolved_bets()
    if not unresolved_bets:
        return
        
    fixtures = get_fixtures_by_ids(list(unresolved_bets.keys()))
    
    for match_id, bet_info in unresolved_bets.items():
        try:
            if match_id not in fixtures:
                continue
                
            match_data = fixtures[match_id]
            if match_data['fixture']['status']['short'] != 'FT':
                continue
                
            home_goals = match_data['goals']['home'] or 0
            away_goals = match_data['goals']['away'] or 0
            final_score = f"{home_goals}-{away_goals}"
            
            if bet_info['bet_type'] == 'chase':
                resolve_chase_bet(match_id, bet_info, final_score)
            else:
                resolve_regular_bet(match_id, bet_info, final_score)
                
        except Exception as e:
            logger.error(f"Error resolving bet {match_id}: {str(e)}\n{traceback.format_exc()}")

def resolve_regular_bet(match_id, bet_info, final_score):
    """Resolve regular bet (should have been resolved at HT)"""
    message = (f"⚠️ FT Result: {bet_info['match_name']}\n"
              f"🏆 {bet_info['league']} ({bet_info['country']})\n"
              f"🔢 Score: {final_score}\n"
              f"❓ Regular bet was not resolved at HT")
    send_telegram(message)
    firebase_manager.move_to_resolved(match_id, bet_info, 'error')

def resolve_chase_bet(match_id, bet_info, final_score):
    """Resolve chase bet"""
    chase_score = bet_info.get('80_score', '')
    match_name = bet_info['match_name']
    league_name = bet_info['league']
    country = bet_info['country']
    
    if final_score == chase_score:
        outcome = 'win'
        message = (f"✅ CHASE BET WON: {match_name}\n"
                  f"🏆 {league_name} ({country})\n"
                  f"🔢 Final Score: {final_score}\n"
                  f"🎉 Same as 80' score\n"
                  f"💡 Covered 36' loss ({bet_info['36_score']} -> {bet_info['ht_score']})")
    else:
        outcome = 'loss'
        message = (f"❌ CHASE BET LOST: {match_name}\n"
                  f"🏆 {league_name} ({country})\n"
                  f"🔢 Final Score: {final_score} (was {chase_score} at 80')\n"
                  f"📉 Score changed after 80'")
    
    send_telegram(message)
    firebase_manager.move_to_resolved(match_id, bet_info, outcome)

def run_bot_cycle():
    """Execute one complete bot cycle"""
    try:
        # Process live matches
        for match in get_live_matches():
            process_match(match)
        
        # Check unresolved bets
        check_unresolved_bets()
        
        # Periodic cleanup
        if datetime.now().hour == 3 and datetime.now().minute < 5:
            firebase_manager.cleanup_old_matches()
            
    except Exception as e:
        logger.error(f"Error during bot cycle: {str(e)}\n{traceback.format_exc()}")
        return False
    return True

# Backward compatibility alias
run_bot_once = run_bot_cycle

def main():
    """Main execution loop"""
    logger.info("🚀 Starting Football Betting Bot")
    consecutive_errors = 0
    
    while True:
        try:
            if run_bot_cycle():
                consecutive_errors = 0
                sleep_time = 90
            else:
                consecutive_errors += 1
                sleep_time = min(300, 30 * consecutive_errors)
                
                if consecutive_errors >= 5:
                    send_telegram(
                        "🚨 CRITICAL ALERT\n"
                        f"Bot has {consecutive_errors} consecutive errors\n"
                        "Manual intervention may be required"
                    )
                    consecutive_errors = 0
            
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.critical(f"Fatal error: {str(e)}\n{traceback.format_exc()}")
            send_telegram(f"🔥 CRITICAL ERROR: {str(e)[:300]}")
            time.sleep(60)

if __name__ == "__main__":
    main()