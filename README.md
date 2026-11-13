# USDU random buyer (Crypto.com Exchange)

Buys a random amount of USDU on the Crypto.com Exchange each time you run it, sized by price: the cheaper USDU is below $1, the more it buys. At or below $0.995 the amount is random in $0–20; between $0.995 and $1.00 that range shrinks linearly to $0 (e.g. $0–5.92 at $0.99852); at $1.00 or above it buys nothing. Tune `MAX_AMOUNT_USD`, `FULL_BUY_PRICE` and `NO_BUY_PRICE` in `config.py`. Runs in dry-run mode by default; with `DRY_RUN=false` it places the order immediately, without asking, and prints one line with the result. Spending is capped by `DAILY_LIMIT_USD`. Trades are logged to `trades.csv`.

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

   Set `DRY_RUN=false` in `.env` to trade for real. Each run then buys immediately, with no confirmation.
