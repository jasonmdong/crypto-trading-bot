# USDU random buyer (Crypto.com Exchange)

Buys a random amount of USDU on the Crypto.com Exchange each time you run it, sized by price: the cheaper USDU is below $1, the more it buys. At or below $0.995 the amount is random in $0–20; between $0.995 and $1.00 the top of that range shrinks linearly (e.g. $0–5.92 at $0.99852). Any amount under $2 is rerolled, so every buy is at least $2 (safely above the exchange's $1 minimum); once the top would fall below $2 (price above $0.9995) it buys nothing. Tune `MIN_AMOUNT_USD`, `MAX_AMOUNT_USD`, `FULL_BUY_PRICE` and `NO_BUY_PRICE` in `config.py`. Runs in dry-run mode by default; with `DRY_RUN=false` it places the order immediately, without asking, and prints one line with the result. Spending is capped by `DAILY_LIMIT_USD`. Trades are logged to `trades.csv`.

## Installation

1. Create an API key on the Crypto.com **Exchange** (API Management) with **Trading** enabled and your IP whitelisted.
2. Set up the project:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate # .venv\Scripts\Activate.ps1 for Windows
   pip install -r requirements.txt
   cp .env.example .env
   ```

3. Add your API keys and secrets to `.env`, then set up the symbol and side to spend.
4.  You're good to go! Set `DRY_RUN=false` in `.env` to trade for real. Each run then buys immediately, with no confirmation.

## Run once a day at a random time

```bash
python run.py
```

Runs `scripts/main.py`(buys) at a random time in the rest of today, then once a day at a random time, printing each result. Stop it with Ctrl-C. It only runs while the window (or SSH session) stays open.
