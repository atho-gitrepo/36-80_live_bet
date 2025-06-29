# 36-80_live_bet_Bot ⚽💰

A Python bot that tracks live football matches and places virtual bets based on specific score conditions at the 36th and 80th minutes.

## Features ✨

- Real-time monitoring of live football matches via API-Football
- Automated betting strategy focused on specific scorelines:
  - Places initial bet at 36' only if score is in: `0-0`, `1-0`, `0-1`, or `1-1`
  - Optional chase bet at 80' if initial bet loses
- Telegram notifications for all betting actions and results
- Persistent tracking of match states between runs
- Comprehensive logging for debugging and analysis

## Prerequisites 📋

- Python 3.8+
- API-Football subscription (from [API-Sports](https://api-sports.io/documentation/football))
- Telegram bot token and chat ID (for notifications)
- `.env` file with required credentials (see `.env.example` below)

## Installation 🛠️

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/football-betting-bot.git
   cd football-betting-bot
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create your `.env` file:
   ```ini
   # .env.example
   API_KEY=your_api-football_key_here
   TELEGRAM_TOKEN=your_telegram_bot_token
   TELEGRAM_CHAT_ID=your_telegram_chat_id
   ```

## Configuration ⚙️

The bot can be configured by modifying these variables in the code:

```python
VALID_36_SCORES = {'0-0', '1-0', '0-1', '1-1'}  # Scores to bet on at 36'
POLLING_INTERVAL = 60  # Seconds between checks
POLLING_DURATION = 120  # Minutes to run in continuous mode
```

## Usage 🚀

### Run once (for cron jobs):
```bash
python bot.py
```

### Run continuously (for testing):
```bash
python bot.py --continuous
```

### Command line options:
```
--continuous  Run in continuous polling mode (default: False)
--interval    Polling interval in seconds (default: 60)
--duration    Duration to run in minutes (default: 120)
```

## Telegram Notifications 📱
The bot sends notifications for:
- New bets placed (36' and 80')
- Half-time and full-time results
- Bet outcomes (won/lost)

Example notification:
```
⏱️ 36' - Liverpool vs Manchester City
🏆 Premier League (England)
🔢 Score: 1-1
🎯 First Bet Placed
```

## License 📄
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Disclaimer ⚠️
This is an educational project demonstrating API integration and automation. The betting strategy is for demonstration purposes only. Always gamble responsibly and within legal boundaries in your jurisdiction.