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

# Validate environment variables
if not all([API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, FIREBASE_CREDENTIALS_JSON_STRING]):
    logger.error("Missing required environment variables")
    sys.exit(1)

HEADERS = {'x-apisports-key': API_KEY}
BASE_URL = 'https://v3.football.api-sports.io'

class FirebaseManager:
    """Fixed Firebase Firestore manager with proper transaction handling"""
    
    def __init__(self, credentials_json_string):
        try:
            logger.info("Initializing Firebase connection")
            cred_dict = json.loads(credentials_json_string)
            cred = credentials.Certificate(cred_dict)
            
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
                
            self.db = firestore.client()
            logger.info("Firebase initialized successfully")
            
        except Exception as e:
            logger.critical(f"Firebase initialization failed: {str(e)}")
            raise

    def get_tracked_match(self, match_id):
        try:
            doc = self.db.collection('tracked_matches').document(str(match_id)).get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            logger.error(f"Error getting tracked match: {str(e)}")
            return None

    def update_tracked_match(self, match_id, data):
        try:
            self.db.collection('tracked_matches').document(str(match_id)).set(data, merge=True)
        except Exception as e:
            logger.error(f"Error updating tracked match: {str(e)}")
            raise

    def get_unresolved_bets(self, bet_type=None):
        try:
            col_ref = self.db.collection('unresolved_bets')
            query = col_ref.where('bet_type', '==', bet_type) if bet_type else col_ref
            return {doc.id: doc.to_dict() for doc in query.stream()}
        except Exception as e:
            logger.error(f"Error getting unresolved bets: {str(e)}")
            return {}

    def add_unresolved_bet(self, match_id, data):
        """Fixed transaction implementation"""
        @firestore.transactional
        def transaction_handler(transaction):
            # Get document references
            match_ref = self.db.collection('tracked_matches').document(str(match_id))
            bet_ref = self.db.collection('unresolved_bets').document(str(match_id))
            
            # Verify match exists
            match_snap = transaction.get(match_ref)
            if not match_snap.exists:
                raise ValueError("Match not found in tracked_matches")
            
            # Create/update documents
            transaction.set(bet_ref, data)
            if 'tracked_updates' in data:
                transaction.set(match_ref, data['tracked_updates'], merge=True)
        
        try:
            transaction = self.db.transaction()
            transaction_handler(transaction)
        except Exception as e:
            logger.error(f"Failed to add unresolved bet: {str(e)}")
            raise

    def move_to_resolved(self, match_id, bet_info, outcome):
        """Fixed transaction implementation"""
        @firestore.transactional
        def transaction_handler(transaction):
            unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
            resolved_ref = self.db.collection('resolved_bets').document(str(match_id))
            tracked_ref = self.db.collection('tracked_matches').document(str(match_id))
            
            # Verify bet exists
            if not transaction.get(unresolved_ref).exists:
                raise ValueError("Unresolved bet not found")
            
            # Create resolved bet
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().isoformat()
            }
            transaction.set(resolved_ref, resolved_data)
            transaction.delete(unresolved_ref)
            
            # Update tracked match
            updates = {
                f"{bet_info.get('bet_type', 'unknown')}_resolved": True,
                'last_update': datetime.utcnow().isoformat()
            }
            transaction.set(tracked_ref, updates, merge=True)
        
        try:
            transaction = self.db.transaction()
            transaction_handler(transaction)
        except Exception as e:
            logger.error(f"Failed to resolve bet: {str(e)}")
            raise

# Initialize Firebase
try:
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception as e:
    logger.critical(f"Failed to initialize Firebase: {str(e)}")
    sys.exit(1)

def send_telegram(message):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': message},
            timeout=10
        )
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {str(e)}")

def get_live_matches():
    """Fetch live matches with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.get(
                f"{BASE_URL}/fixtures?live=all",
                headers=HEADERS,
                timeout=15
            )
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Rate limited. Sleeping for {retry_after} seconds")
                time.sleep(retry_after)
                continue
                
            response.raise_for_status()
            return response.json().get('response', [])
            
        except Exception as e:
            logger.warning(f"API request failed (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)
    
    logger.error("Failed to fetch live matches after retries")
    return []

def process_match(match):
    try:
        fixture = match['fixture']
        match_id = fixture['id']
        status = fixture['status']['short']
        elapsed = fixture['status']['elapsed']
        
        # Skip if not in a processable state
        if status.upper() not in ['LIVE', 'HT', '1H', '2H'] or elapsed is None:
            return
            
        teams = match['teams']
        league = match['league']
        goals = match['goals']
        score = f"{goals['home'] or 0}-{goals['away'] or 0}"
        match_name = f"{teams['home']['name']} vs {teams['away']['name']}"
        
        # Get or initialize match state
        state = firebase_manager.get_tracked_match(match_id) or {
            '36_bet_placed': False,
            '36_result_checked': False,
            '36_bet_won': None,
            '80_bet_placed': False,
            'last_update': datetime.utcnow().isoformat()
        }
        
        # 36' Bet Logic
        if (status.upper() == '1H' and 35 <= elapsed <= 37 and not state['36_bet_placed']):
            handle_36min_bet(match_id, match_name, league, score, state)
        
        # HT Check
        elif (status.upper() == 'HT' and state['36_bet_placed'] and not state['36_result_checked']):
            handle_ht_check(match_id, match_name, league, score, state)
        
        # 80' Chase Bet
        elif (status.upper() == '2H' and 79 <= elapsed <= 81 and not state['80_bet_placed'] 
              and state.get('36_bet_won') is False):
            handle_80min_chase(match_id, match_name, league, score, state)
            
    except Exception as e:
        logger.error(f"Error processing match: {str(e)}\n{traceback.format_exc()}")

def handle_36min_bet(match_id, match_name, league, score, state):
    bet_data = {
        'match_name': match_name,
        'league': league['name'],
        'country': league.get('country', 'N/A'),
        'initial_score': score,
        'bet_type': 'regular',
        'placed_at': datetime.utcnow().isoformat()
    }
    
    state_updates = {
        '36_bet_placed': True,
        '36_score': score,
        'last_update': datetime.utcnow().isoformat()
    }
    
    if score in ['0-0', '1-1', '2-2', '3-3']:
        firebase_manager.add_unresolved_bet(match_id, {
            **bet_data,
            'tracked_updates': state_updates
        })
        send_telegram(
            f"⏱️ 36' - {match_name}\n"
            f"🏆 {league['name']} ({league.get('country', 'N/A')})\n"
            f"🔢 Score: {score}\n"
            f"🎯 Correct Score Bet Placed"
        )
    else:
        firebase_manager.update_tracked_match(match_id, state_updates)

def handle_ht_check(match_id, match_name, league, score, state):
    unresolved_bet = firebase_manager.get_unresolved_bets('regular').get(str(match_id))
    if not unresolved_bet:
        logger.warning(f"No unresolved bet found for match {match_id}")
        return
        
    outcome = 'win' if score == state['36_score'] else 'loss'
    message = (
        f"✅ HT Result: {match_name}\n"
        f"🏆 {league['name']} ({league.get('country', 'N/A')})\n"
        f"🔢 Score: {score}\n"
        f"{'🎉 36\' Bet WON' if outcome == 'win' else '❌ 36\' Bet LOST'}"
    )
    
    send_telegram(message)
    firebase_manager.move_to_resolved(match_id, unresolved_bet, outcome)
    
    firebase_manager.update_tracked_match(match_id, {
        '36_result_checked': True,
        '36_bet_won': outcome == 'win',
        'ht_score': score,
        'last_update': datetime.utcnow().isoformat()
    })

def handle_80min_chase(match_id, match_name, league, score, state):
    bet_data = {
        'match_name': match_name,
        'league': league['name'],
        'country': league.get('country', 'N/A'),
        'bet_type': 'chase',
        '36_score': state['36_score'],
        'ht_score': state['ht_score'],
        '80_score': score,
        'placed_at': datetime.utcnow().isoformat(),
        'tracked_updates': {
            '80_bet_placed': True,
            'last_update': datetime.utcnow().isoformat()
        }
    }
    
    firebase_manager.add_unresolved_bet(match_id, bet_data)
    send_telegram(
        f"⏱️ 80' CHASE BET: {match_name}\n"
        f"🏆 {league['name']} ({league.get('country', 'N/A')})\n"
        f"🔢 Score: {score}\n"
        f"💡 Covering lost 36' bet"
    )

def check_unresolved_bets():
    try:
        unresolved_bets = firebase_manager.get_unresolved_bets()
        if not unresolved_bets:
            return
            
        for match_id, bet_info in unresolved_bets.items():
            # Add your resolution logic here
            pass
            
    except Exception as e:
        logger.error(f"Error checking unresolved bets: {str(e)}")

def run_bot_cycle():
    """Main bot cycle with proper error handling"""
    try:
        logger.info("Starting new bot cycle")
        
        # Process live matches
        matches = get_live_matches()
        logger.info(f"Processing {len(matches)} live matches")
        for match in matches:
            process_match(match)
        
        # Check unresolved bets
        check_unresolved_bets()
        
        logger.info("Cycle completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error in bot cycle: {str(e)}\n{traceback.format_exc()}")
        return False

def main():
    """Main execution loop with proper initialization"""
    logger.info("🚀 Starting Football Betting Bot")
    
    consecutive_errors = 0
    while True:
        try:
            if not run_bot_cycle():
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    send_telegram("⚠️ Bot experiencing repeated errors - check logs")
                    consecutive_errors = 0
            else:
                consecutive_errors = 0
            
            # Sleep with logging
            sleep_time = 90
            logger.info(f"Sleeping for {sleep_time} seconds...")
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.critical(f"Fatal error in main loop: {str(e)}")
            send_telegram(f"🔥 CRITICAL ERROR: {str(e)[:200]}")
            time.sleep(60)

if __name__ == "__main__":
    main()