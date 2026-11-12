# USDU random buyer (Crypto.com Exchange)

Buys a random amount of USDU ($0–20) on the Crypto.com Exchange each time you run it. Runs in dry-run mode by default; live orders require `DRY_RUN=false` and typing `BUY` to confirm. Trades are logged to `trades.csv`.

## Installation

1. Create an API key on the Crypto.com **Exchange** (API Management) with **Trading** enabled and your IP whitelisted.
2. Set up the project:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. Add your API key and secret to `.env`.
4. Run:

   ```bash
   python main.py
   ```

   Set `DRY_RUN=false` in `.env` to trade for real.
