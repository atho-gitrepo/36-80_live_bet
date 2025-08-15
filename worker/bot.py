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
    """Enhanced Firebase Firestore manager with atomic operations and storage optimization"""
    
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
    """Add unresolved bet with transaction to maintain consistency"""
    	@firestore.transactional
    	def add_bet_transaction(transaction, match_ref, unresolved_ref):
        # Get the document snapshot properly
        	tracked_snap = transaction.get(match_ref)
        
        	if not tracked_snap.exists:
            	raise ValueError(f"Match {match_id} not found in tracked_matches")
            
        # Add to unresolved bets
        	transaction.set(unresolved_ref, data)
        
        # Update tracked match state if updates are provided
        	if 'tracked_updates' in data:
            	transaction.set(match_ref, data['tracked_updates'], merge=True)
    
    	try:
        	logger.info(f"Adding unresolved bet for match {match_id}")
        
        # Create references first
        	match_ref = self.db.collection('tracked_matches').document(str(match_id))
        	unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
        
        # Execute transaction with references
        	transaction = self.db.transaction()
        	add_bet_transaction(transaction, match_ref, unresolved_ref)
        
        	logger.info(f"Successfully added unresolved bet for match {match_id}")
        
    	except Exception as e:
        	logger.error(f"Failed to add unresolved bet for match {match_id}: {str(e)}")
        	raise

    def move_to_resolved(self, match_id, bet_info, outcome):
        """Atomic operation to move bet to resolved and clean up"""
        @firestore.transactional
        def resolve_transaction(transaction):
            # 1. Verify the bet is still unresolved
            unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
            unresolved_snap = transaction.get(unresolved_ref)
            
            if not unresolved_snap.exists:
                raise ValueError(f"Bet {match_id} not found in unresolved_bets")
            
            # 2. Create resolved bet record
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().isoformat(),
                'resolution_data': bet_info.get('match_data', {})
            }
            resolved_ref = self.db.collection('resolved_bets').document(str(match_id))
            transaction.set(resolved_ref, resolved_data)
            
            # 3. Remove from unresolved bets
            transaction.delete(unresolved_ref)
            
            # 4. Clean up tracked match if conditions met
            tracked_ref = self.db.collection('tracked_matches').document(str(match_id))
            tracked_snap = transaction.get(tracked_ref)
            
            if tracked_snap.exists:
                tracked_data = tracked_snap.to_dict()
                if self._can_remove_tracked_match(tracked_data, bet_info.get('bet_type')):
                    transaction.delete(tracked_ref)
                    logger.info(f"Removed tracked match {match_id} after resolution")
                else:
                    # Update with resolution info but keep tracking
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
        # If this was a chase bet, we can always remove as it's the final bet
        if bet_type == 'chase':
            return True
            
        # For regular bets, only remove if no chase bet is expected
        if bet_type == 'regular':
            # Don't remove if we might place an 80' chase bet later
            if tracked_data.get('36_bet_won') is False:
                return False
                
        # Matches with all bets resolved can be removed
        return True

    def cleanup_old_matches(self, days_threshold=7):
        """Periodically clean up old tracked matches"""
        try:
            logger.info(f"Cleaning up matches older than {days_threshold} days")
            cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)
            
            # Query for old matches that are no longer active
            query = (self.db.collection('tracked_matches')
                    .where('last_update', '<', cutoff_date.isoformat()))
            
            # Batch delete in chunks
            deleted_count = 0
            for doc in query.stream():
                try:
                    # Check if this match has any unresolved bets
                    unresolved_ref = self.db.collection('unresolved_bets').document(doc.id)
                    if not unresolved_ref.get().exists:
                        doc.reference.delete()
                        deleted_count += 1
                        if deleted_count % 100 == 0:
                            logger.info(f"Deleted {deleted_count} old matches so far")
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
            logger.debug(f"Sending Telegram message (attempt {attempt + 1}): {message[:50]}...")
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram message sent successfully")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Telegram send failed (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                logger.error(f"Failed to send Telegram message after {max_retries} attempts")
                return False

def handle_api_rate_limit(response):
    """Handle API rate limiting with exponential backoff"""
    if response.status_code == 429:
        retry_after = int(response.headers.get('Retry-After', 60))
        logger.warning(f"Rate limited. Sleeping for {retry_after} seconds")
        time.sleep(retry_after)
        return True
    return False

def get_live_matches():
    """Fetch live matches with retry logic"""
    url = f"{BASE_URL}/fixtures?live=all"
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Fetching live matches (attempt {attempt + 1})")
            response = requests.get(url, headers=HEADERS, timeout=15)
            
            if handle_api_rate_limit(response):
                continue  # Will retry after sleep
                
            response.raise_for_status()
            
            data = response.json()
            matches = data.get('response', [])
            
            logger.info(f"Found {len(matches)} live matches")
            return matches
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                sleep_time = (attempt + 1) * 5
                logger.info(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
            else:
                logger.error("Failed to fetch live matches after retries")
                return []

def get_fixtures_by_ids(match_ids):
    """Batch fetch finished fixtures by IDs with chunking"""
    if not match_ids:
        logger.info("No match IDs provided for fixture lookup")
        return {}
        
    logger.info(f"Fetching {len(match_ids)} unresolved matches")
    
    chunk_size = 20  # API limit
    fixtures = {}
    
    for i in range(0, len(match_ids), chunk_size):
        chunk = match_ids[i:i + chunk_size]
        ids_param = '-'.join(str(mid) for mid in chunk)
        url = f"{BASE_URL}/fixtures?ids={ids_param}&status=FT"
        
        try:
            logger.debug(f"Fetching chunk {i//chunk_size + 1} with {len(chunk)} matches")
            response = requests.get(url, headers=HEADERS, timeout=25)
            
            if handle_api_rate_limit(response):
                continue  # Will retry after sleep
                
            response.raise_for_status()
            
            data = response.json()
            response_fixtures = data.get('response', [])
            
            for f in response_fixtures:
                fixture_id = str(f['fixture']['id'])
                fixtures[fixture_id] = f
                
            logger.info(f"Retrieved {len(response_fixtures)} finished fixtures in chunk {i//chunk_size + 1}")
            
        except Exception as e:
            logger.error(f"Error fetching fixture chunk: {str(e)}")
            continue
            
    return fixtures

def should_process_match(fixture_status, elapsed_time):
    """Simplified match processing condition check"""
    status = fixture_status.upper()
    return status in ['LIVE', 'HT', '1H', '2H'] and elapsed_time is not None

def initialize_match_state(match_id):
    """Initialize default state for new matches"""
    default_state = {
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
    firebase_manager.update_tracked_match(match_id, default_state)
    return default_state

def process_match(match):
    """Main match processing logic with simplified conditions"""
    try:
        fixture = match['fixture']
        fixture_id = fixture['id']
        status = fixture['status']['short']
        elapsed = fixture['status']['elapsed']
        teams = match['teams']
        league = match['league']
        goals = match['goals']
        
        match_name = f"{teams['home']['name']} vs {teams['away']['name']}"
        league_name = league['name']
        country = league.get('country', 'N/A')
        home_goals = goals['home'] or 0
        away_goals = goals['away'] or 0
        current_score = f"{home_goals}-{away_goals}"
        
        logger.info(f"Processing match: {match_name} ({current_score}) at {elapsed}'")
        
        # Skip non-processable matches
        if not should_process_match(status, elapsed):
            logger.debug(f"Skipping match {match_name} - status: {status}, elapsed: {elapsed}")
            return
            
        # Get or initialize match state
        state = firebase_manager.get_tracked_match(fixture_id) or initialize_match_state(fixture_id)
        
        # Ensure all state fields exist
        for key in ['36_bet_placed', '36_result_checked', '36_bet_won', 
                   '80_bet_placed', '80_bet_resolved', '36_score', 
                   'ht_score', '80_score', 'last_update']:
            state.setdefault(key, None)
        
        # 36' Bet Logic (35-37 minute window)
        if (status.upper() == '1H' and 35 <= elapsed <= 37 
            and not state['36_bet_placed']):
            
            logger.info(f"Evaluating 36' bet for {match_name}")
            
            bet_data = {
                'match_name': match_name,
                'placed_at': datetime.utcnow().isoformat(),
                'league': league_name,
                'country': country,
                'league_id': league['id'],
                'initial_score': current_score,
                'bet_type': 'regular',
                'match_data': {
                    'fixture_id': fixture_id,
                    'teams': teams,
                    'league': league
                },
                'tracked_updates': {
                    '36_bet_placed': True,
                    '36_score': current_score,
                    'last_update': datetime.utcnow().isoformat()
                }
            }
            
            if current_score in ['0-0', '1-1', '2-2', '3-3']:
                logger.info(f"Placing 36' bet for {match_name}")
                firebase_manager.add_unresolved_bet(fixture_id, bet_data)
                
                message = (f"⏱️ 36' - {match_name}\n"
                          f"🏆 {league_name} ({country})\n"
                          f"🔢 Score: {current_score}\n"
                          f"🎯 Correct Score Bet Placed")
                send_telegram(message)
            else:
                logger.info(f"No 36' bet for {match_name} - score {current_score}")
                firebase_manager.update_tracked_match(fixture_id, {
                    '36_bet_placed': True,
                    '36_score': current_score,
                    'last_update': datetime.utcnow().isoformat()
                })

        # HT Result Check
        if (status.upper() == 'HT' 
            and state['36_bet_placed'] 
            and not state['36_result_checked']):
            
            logger.info(f"Checking HT result for {match_name}")
            
            # Update state with HT score
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
                
            if current_score == state['36_score']:
                message = (f"✅ HT Result: {match_name}\n"
                          f"🏆 {league_name} ({country})\n"
                          f"🔢 Score: {current_score}\n"
                          f"🎉 36' Bet WON")
                outcome = 'win'
            else:
                message = (f"❌ HT Result: {match_name}\n"
                          f"🏆 {league_name} ({country})\n"
                          f"🔢 Score: {current_score}\n"
                          f"🔁 36' Bet LOST — eligible for chase")
                outcome = 'loss'
                
            send_telegram(message)
            firebase_manager.move_to_resolved(fixture_id, unresolved_bet, outcome)
            
            # Update tracked match state
            firebase_manager.update_tracked_match(fixture_id, {
                '36_result_checked': True,
                '36_bet_won': outcome == 'win',
                'last_update': datetime.utcnow().isoformat()
            })
        
        # 80' Chase Bet Logic (79-81 minute window)
        if (status.upper() == '2H' 
            and 79 <= elapsed <= 81 
            and not state['80_bet_placed']
            and state.get('36_bet_won') is False):
            
            logger.info(f"Evaluating 80' chase bet for {match_name}")
            
            bet_data = {
                'match_name': match_name,
                'placed_at': datetime.utcnow().isoformat(),
                'league': league_name,
                'country': country,
                'league_id': league['id'],
                'bet_type': 'chase',
                '36_score': state['36_score'],
                'ht_score': current_score,
                '80_score': current_score,
                'initial_score': current_score,
                'match_data': {
                    'fixture_id': fixture_id,
                    'teams': teams,
                    'league': league
                },
                'tracked_updates': {
                    '80_bet_placed': True,
                    '80_score': current_score,
                    'last_update': datetime.utcnow().isoformat()
                }
            }
            
            firebase_manager.add_unresolved_bet(fixture_id, bet_data)
            
            message = (f"⏱️ 80' CHASE BET: {match_name}\n"
                      f"🏆 {league_name} ({country})\n"
                      f"🔢 Score: {current_score}\n"
                      f"🎯 Betting for Correct Score\n"
                      f"💡 Covering lost 36' bet ({state['36_score']} -> {state['ht_score']})")
            send_telegram(message)
            
    except Exception as e:
        logger.error(f"Error processing match: {str(e)}\n{traceback.format_exc()}")

def check_unresolved_bets():
    """Check and resolve all outstanding bets"""
    logger.info("Checking unresolved bets")
    
    unresolved_bets = firebase_manager.get_unresolved_bets()
    if not unresolved_bets:
        logger.info("No unresolved bets found")
        return
        
    logger.info(f"Found {len(unresolved_bets)} unresolved bets")
    match_ids = list(unresolved_bets.keys())
    fixtures = get_fixtures_by_ids(match_ids)
    
    for match_id, bet_info in unresolved_bets.items():
        try:
            if match_id not in fixtures:
                logger.warning(f"Fixture {match_id} not found in finished matches")
                continue
                
            match_data = fixtures[match_id]
            fixture_status = match_data['fixture']['status']['short']
            
            if fixture_status != 'FT':
                logger.debug(f"Match {match_id} not finished (status: {fixture_status})")
                continue
                
            home_goals = match_data['goals']['home'] or 0
            away_goals = match_data['goals']['away'] or 0
            final_score = f"{home_goals}-{away_goals}"
            match_name = bet_info.get('match_name', f"Match {match_id}")
            league_name = bet_info.get('league', 'Unknown League')
            country = bet_info.get('country', 'N/A')
            bet_type = bet_info['bet_type']
            
            logger.info(f"Resolving {bet_type} bet for {match_name}")
            
            # Determine outcome based on bet type
            if bet_type == 'regular':
                outcome = 'error'
                message = (f"⚠️ FT Result: {match_name}\n"
                          f"🏆 {league_name} ({country})\n"
                          f"🔢 Score: {final_score}\n"
                          f"❓ Regular bet was not resolved at HT")
                
            elif bet_type == 'chase':
                chase_score = bet_info.get('80_score', '')
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
            else:
                outcome = 'error'
                message = (f"⚠️ FT Result: {match_name}\n"
                          f"🏆 {league_name} ({country})\n"
                          f"🔢 Score: {final_score}\n"
                          f"❓ Unknown bet type: {bet_type}")
            
            send_telegram(message)
            firebase_manager.move_to_resolved(match_id, bet_info, outcome)
            
        except Exception as e:
            logger.error(f"Error resolving bet {match_id}: {str(e)}\n{traceback.format_exc()}")

def get_uptime():
    """Calculate bot uptime"""
    if not hasattr(get_uptime, 'start_time'):
        get_uptime.start_time = datetime.now()
    uptime = datetime.now() - get_uptime.start_time
    return str(uptime).split('.')[0]  # Remove microseconds

def get_memory_usage():
    """Get current memory usage in MB"""
    import psutil
    process = psutil.Process(os.getpid())
    return round(process.memory_info().rss / 1024 / 1024, 2)

def health_check():
    """Periodic system health check"""
    now = datetime.now()
    if now.minute % 30 == 0:  # Every 30 minutes
        message = (f"🤖 Bot Status Update\n"
                  f"⏰ Last cycle: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"🔄 Uptime: {get_uptime()}\n"
                  f"💾 Memory: {get_memory_usage()} MB")
        send_telegram(message)

def run_bot_cycle():
    """Execute one complete bot cycle"""
    cycle_start = datetime.now()
    logger.info(f"Starting bot cycle at {cycle_start}")
    
    try:
        # Process live matches
        live_matches = get_live_matches()
        logger.info(f"Processing {len(live_matches)} live matches")
        for match in live_matches:
            process_match(match)
        
        # Check unresolved bets
        check_unresolved_bets()
        
        # Periodic cleanup (once per day at 3 AM)
        if datetime.now().hour == 3 and datetime.now().minute < 5:
            cleaned = firebase_manager.cleanup_old_matches(days_threshold=7)
            if cleaned > 0:
                send_telegram(f"🧹 Cleaned up {cleaned} old tracked matches")
        
        # Calculate cycle duration
        cycle_duration = (datetime.now() - cycle_start).total_seconds()
        logger.info(f"Cycle completed in {cycle_duration:.2f} seconds")
        
        return True
        
    except Exception as e:
        logger.error(f"Error during bot cycle: {str(e)}\n{traceback.format_exc()}")
        return False

def main():
    """Main bot execution loop"""
    logger.info("🚀 Starting Football Betting Bot")
    consecutive_errors = 0
    max_errors = 5
    
    while True:
        try:
            success = run_bot_cycle()
            
            if success:
                consecutive_errors = 0
                sleep_time = 90  # Normal sleep time (1.5 minutes)
            else:
                consecutive_errors += 1
                sleep_time = min(300, 30 * consecutive_errors)  # Exponential backoff
                logger.warning(f"Consecutive errors: {consecutive_errors}, sleeping for {sleep_time}s")
                
                if consecutive_errors >= max_errors:
                    alert = ("🚨 CRITICAL ALERT\n"
                            f"Bot has encountered {consecutive_errors} consecutive errors\n"
                            "Manual intervention may be required")
                    send_telegram(alert)
                    consecutive_errors = 0  # Reset after alert
            
            health_check()
            logger.info(f"Sleeping for {sleep_time} seconds...")
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.critical(f"Fatal error in main loop: {str(e)}\n{traceback.format_exc()}")
            send_telegram(f"🔥 CRITICAL ERROR: {str(e)[:300]}")
            time.sleep(60)  # Prevent tight error loop

if __name__ == "__main__":
    main()