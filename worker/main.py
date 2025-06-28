from bot import run_bot_once
import time
from datetime import datetime

CHECK_INTERVAL = 60 # in seconds 

def main():
    print("🚀 Bot worker started")
    while True:
        try:
            print(f"\n[{datetime.now()}] ⏳ Running bot cycle...")
            run_bot_once()
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Unexpected error in main loop: {e}")
        finally:
            print(f"[{datetime.now()}] 💤 Sleeping for {CHECK_INTERVAL} seconds...\n")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
