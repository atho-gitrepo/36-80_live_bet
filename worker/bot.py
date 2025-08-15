import requests
import os
import json
import time
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter  # Add this import
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
            
            # Use keyword argument for filter instead of positional
            if bet_type:
                query = col_ref.where(filter=FieldFilter('bet_type', '==', bet_type))
            else:
                query = col_ref
                
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
        def resolve_transaction(transaction, unresolved_ref, resolved_ref, tracked_ref):
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
            
            # Clean up tracked match if conditions met
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
            
            # Create references
            unresolved_ref = self.db.collection('unresolved_bets').document(str(match_id))
            resolved_ref = self.db.collection('resolved_bets').document(str(match_id))
            tracked_ref = self.db.collection('tracked_matches').document(str(match_id))
            
            # Execute transaction
            transaction = self.db.transaction()
            resolve_transaction(transaction, unresolved_ref, resolved_ref, tracked_ref)
            
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
            # Use keyword argument for filter
            query = (self.db.collection('tracked_matches')
                    .where(filter=FieldFilter('last_update', '<', cutoff_date.isoformat())))
            
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

def make_api_request(url, headers, timeout=15, max_retries=3):
    """Make API request with retry logic and rate limit handling"""
    for attempt in range(max_retries):
        try:
            logger.debug(f"Making API request to {url} (attempt {attempt + 1})")
            response = requests.get(url, headers=headers, timeout=timeout)
            
            # Handle rate limiting
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Rate limited. Sleeping for {retry_after} seconds")
                time.sleep(retry_after)
                continue
                
            response.raise_for_status()
            return response
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                sleep_time = (attempt + 1) * 5
                logger.info(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
            else:
                logger.error(f"Failed after {max_retries} attempts")
                raise

def get_live_matches():
    """Fetch live matches using the tracked API call"""
    url = f"{BASE_URL}/fixtures?live=all"
    try:
        response = make_api_request(url, HEADERS)
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        logger.error(f"Error getting live matches: {str(e)}")
        return []

def get_fixtures_by_ids(match_ids):
    """Fetch fixtures using the tracked API call"""
    if not match_ids:
        return {}
        
    chunk_size = 20
    fixtures = {}
    
    for i in range(0, len(match_ids), chunk_size):
        chunk = match_ids[i:i + chunk_size]
        ids_param = '-'.join(str(mid) for mid in chunk)
        url = f"{BASE_URL}/fixtures?ids={ids_param}&status=FT"
        
        try:
            response = make_api_request(url, HEADERS, timeout=25)
            data = response.json()
            for f in data.get('response', []):
                fixtures[str(f['fixture']['id'])] = f
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
