from bot import run_bot_once
import time
from datetime import datetime, timedelta

CHECK_INTERVAL = 90  # in seconds
RUN_DURATION = 2  # in hours

def main():
    print("🚀 Bot worker started")

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=RUN_DURATION)

    while datetime.now() < end_time:
        try:
            print(f"\n[{datetime.now()}] ⏳ Running bot cycle...")
            run_bot_once()
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Unexpected error in main loop: {e}")
        finally:
            print(f"[{datetime.now()}] 💤 Sleeping for {CHECK_INTERVAL} seconds...\n")
            time.sleep(CHECK_INTERVAL)

    print(f"[{datetime.now()}] 🛑 Bot stopped after {RUN_DURATION} hours")

if __name__ == "__main__":
    main()