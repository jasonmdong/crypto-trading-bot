# USDU random-amount buyer (Crypto.com Exchange)

Each run:

1. Generates `round(20 * random(), 2)` USD (using `secrets.SystemRandom`).
2. Validates it against live exchange rules, your Exchange USD balance, and a daily spending limit.
3. In **dry-run** (the default), prints the exact `private/create-order` request and logs a preview row.
4. In **live** mode, asks you to type `BUY`, submits a **MARKET BUY on `USDU_USD` by USD notional**, polls until the order reaches a final status, prints the result, and appends it to `trades.csv`.

The bot only trades `USDU_USD`. That pair is hard-coded, and if USDU is unavailable the bot stops instead of buying anything else.

---

## Which API this uses, and why

| Surface | Official API? | USDU? | Used here |
|---|---|---|---|
| **Crypto.com Exchange** (`api.crypto.com/exchange/v1`) | Yes, with full REST reference: <https://exchange-developer.crypto.com/exchange/v1/docs/api/rest-introduction> | **Yes**: `USDU_USD` spot pair, `tradable: true` | **Yes** |
| **Crypto.com App** (where your Cash Account USD sits) | Only through the "Agent Key" feature, which Crypto.com ships as Node.js scripts in [crypto-com/crypto-agent-trading](https://github.com/crypto-com/crypto-agent-trading). There's no published REST reference. | Not verifiable without an App key | No |

These facts were checked against production on 2026-11-11:

- `GET public/get-instruments` → `USDU_USD`: base `USDU`, quote `USD`, `qty_tick_size` `0.1`, `price_tick_size` `0.00001`, `tradable: true`. This is the **only** USDU pair; there's no USDU/USDT or USDU/USDC.
- `GET public/get-risk-parameters` → `USDU`: `min_order_notional_usd` **1.0**, `max_order_notional_usd` 1000.0, `daily_notional_limit` 100000.0.
- `GET public/get-book` showed asks around 0.9985 USD and thousands of USDU of depth, which is plenty for a $20 order.
- `private/create-order` documents `notional` as "Amount to spend; for MARKET BUY only (alternative to quantity)". That makes a buy by USD amount a documented feature.

The bot re-reads all of these on every run instead of trusting the values above.

### Exchange vs. App: where your USD has to be

Your USD in the **App Cash Account is not visible to the Exchange API**. The bot spends only the **USD balance in your Exchange account** (`private/user-balance`).

- Crypto.com's "Transfer Crypto from Crypto.com App to Exchange" feature (Help Center) covers **crypto** assets. I found no documented way to move **App Cash USD** onto the Exchange. Crypto.com documents funding the Exchange with USD by bank transfer (SWIFT / Fedwire articles in the Help Center). Check the Exchange **Deposit** screen for the options available to your account.
- The App Agent Key API (`wapi.crypto.com`, quote → confirm flow, keys expire after 30 days, weekly budget slider) is official, but it's documented only through Crypto.com's TypeScript scripts, and their own skill file says "NEVER call the Crypto.com App API directly". Building a Python client for it would mean copying an undocumented contract from source code, so this project doesn't. If you'd rather buy straight from App Cash, use those official scripts (`trade.ts quote purchase '{"from_currency":"USD","to_currency":"USDU","from_amount":"12.34"}'` and then `confirm`). That route needs an App Agent Key, and I couldn't check whether USDU is purchasable in the App.

---

## Install (macOS)

Requires Python 3.11 or newer (tested on 3.11 and 3.14).

```bash
cd ~/Desktop/crypto-trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

In VS Code, pick `.venv/bin/python` with **Python: Select Interpreter**.

## Create Exchange API credentials

1. Log in at <https://crypto.com/exchange> (this must be the **Exchange**, not the App).
2. Go to Profile → **Settings / Manage Account → API Management** → **Create a new API key**, add a label, and enter your 2FA code.
3. Copy the **Secret Key** right away. It's shown only once.
4. New keys are **Can Read** only. Enable **Trading**. Crypto.com requires an **IP whitelist** for trading keys, so add your current public IP (`curl ifconfig.me`).
5. Do **not** enable withdrawals.
6. Put the key and secret in `.env`:

```dotenv
CRYPTO_COM_API_KEY=your-key
CRYPTO_COM_API_SECRET=your-secret
DRY_RUN=true
DAILY_LIMIT_USD=50
```

`.env` is git-ignored. The bot never prints the key or the secret. The dry-run preview shows `<redacted>`, and error messages leave out the exchange's `original` echo field, which would otherwise contain your key.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CRYPTO_COM_API_KEY` / `CRYPTO_COM_API_SECRET` | empty | Exchange API key pair. Required for live mode. Optional for dry-run. |
| `DRY_RUN` | `true` | Live trading only when this is `false`/`0`/`no`/`off`. Any other unrecognised value is a configuration error. |
| `DAILY_LIMIT_USD` | `50` | Maximum USD per local calendar day on `USDU_USD` buys. |
| `CRYPTO_COM_BASE_URL` | `https://api.crypto.com/exchange/v1` | Must be `https://`. The UAT sandbox is invite-only for institutions. |
| `TRADES_CSV` | `trades.csv` | Relative paths resolve to the project folder. |
| `REQUEST_TIMEOUT_SECONDS` | `10` | HTTP timeout. |
| `ORDER_STATUS_TIMEOUT_SECONDS` | `30` | How long to poll for a final order status. |

## Dry run (default)

```bash
python main.py
```

You don't need credentials for this. With credentials set, the dry run also checks your Exchange USD balance and today's order history, using read-only calls. Here's real output from 2026-11-11 without credentials:

```text
Mode: DRY RUN | Exchange: https://api.crypto.com/exchange/v1 | Daily limit: $50
Random purchase amount: $10.94
Market USDU_USD: best ask 0.99853, qty tick 0.1, order notional min $1.0 / max $1000.0
No API credentials: skipping balance and exchange order-history checks.
Spent today: $0.00 | Remaining daily limit: $50.00
Estimated fill: ~10.9 USDU at ~0.99853 (market order; actual may differ)
Order request:
POST https://api.crypto.com/exchange/v1/private/create-order
{
  "id": "<request id>",
  "method": "private/create-order",
  "api_key": "<redacted>",
  "nonce": "<current time in ms>",
  "sig": "<HMAC-SHA256 computed at submit>",
  "params": {
    "instrument_name": "USDU_USD",
    "side": "BUY",
    "type": "MARKET",
    "notional": "10.94",
    "client_oid": "9111b182-4cb9-40b9-a175-2c85794b9383",
    "spot_margin": "SPOT"
  }
}

DRY RUN: no order submitted. Preview logged to .../trades.csv
```

About 5% of draws land under the exchange's **$1.00 minimum**. Those runs print `NOT PLACED: $0.85 is below the exchange minimum order of $1.0 for USDU` and exit with code 3. The bot doesn't re-draw, so the amount always comes straight from the specified formula.

## Live trading

1. Make sure your **Exchange** account holds USD (see "Exchange vs. App" above).
2. Set `DRY_RUN=false` in `.env`.
3. Run `python main.py` and type `BUY` when it shows `*** LIVE ORDER: market BUY $X of USDU ... ***`. Anything else cancels.

On success it prints:

```text
=== Order result ===
  Status:            FILLED
  Purchase amount:   $<requested> requested, $<cumulative_value> executed
  Execution price:   <avg_price> USD per USDU (average)
  Quantity received: <cumulative_quantity> USDU (gross)
  Fees:              <cumulative_fee> <fee_instrument_name>
  Net of fees:       <qty - |fee|> USDU        # only when the fee is charged in USDU
  Order ID:          <order_id>
  Client order ID:   <client_oid>
```

Every value comes from `private/get-order-detail` once the order reaches `FILLED`, `CANCELED`, `REJECTED` or `EXPIRED`. This README contains no real live-order output because none has been placed.

### Safety mechanisms

- **Dry-run default.** Live mode needs both `DRY_RUN=false` and typing `BUY` (EOF or empty input cancels).
- **No fallback asset.** If `USDU_USD` is missing, not tradable, has no ask, or reports the wrong currencies, the bot stops (exit 3).
- **Daily limit.** "Spent today" is the larger of: LIVE rows in `trades.csv` for today (unverified rows count their full requested amount), and today's `USDU_USD` BUY `cumulative_value` from `private/get-order-history`. Your manual USDU_USD buys count toward the limit too.
- **Spot only.** `spot_margin: "SPOT"`. The balance check uses free USD (quantity − reserved, capped at the withdrawable amount).
- **Final status verification.** Acceptance by `create-order` is not treated as a purchase. The bot polls `get-order-detail` and exits 0 only if USDU was actually received.
- **Duplicate prevention.**
  - Each order has a UUID `client_oid`, written to `.pending_order.json` *before* submission.
  - `create-order` is never blindly retried. After a timeout, 5xx or non-JSON response, the bot looks the order up by `client_oid` and only resubmits, with the **same** `client_oid`, if the exchange reports `NOT_FOUND`.
  - `DUPLICATE_CLORDID` resolves to the existing order.
  - If anything stays ambiguous, the journal is kept and the next run reconciles it before placing anything new.
- **Single run.** `.bot.lock` (`flock`) stops two overlapping runs.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Dry-run preview OK, or live order received USDU |
| 1 | Order rejected, not executed, unverified, or an API/network error |
| 2 | Configuration error |
| 3 | Not placed: below minimum, over daily limit or balance, or market unavailable |
| 4 | You declined the confirmation |

## trades.csv

The file is created on the first run. Columns: `timestamp_utc, mode, instrument, side, order_type, requested_usd, status, order_id, client_oid, avg_price, quantity, executed_value_usd, fee, fee_currency, reason`. `DRY_RUN` rows hold the preview estimate and never count toward the daily limit. Fee values are stored exactly as the exchange returns them.

## Tests

```bash
python -m unittest -v
```

48 tests cover:

- **Signing:** the parameter string and HMAC match the documented algorithm and examples, and the secret never appears in the request.
- **Amounts:** generation and validation (minimum, maximum, daily-limit and balance boundaries, cent precision, quantity tick).
- **Error handling:** code → exception mapping, nonce and rate-limit retries on reads, `create-order` is never auto-retried, and idempotent resubmission via `client_oid`.
- **Run-level safety:** dry-run never submits, confirmation is required, the daily limit blocks, a pending order blocks, and there's no fallback asset.

## Scheduling later

Live mode is deliberately interactive. `main.run(settings, client, confirm=...)` takes the confirmation as a callable, so a future scheduler can supply its own confirmation policy without touching the order logic. The lock file and pending-order journal already make overlapping or interrupted scheduled runs safe.

## Troubleshooting

| Message | Cause / fix |
|---|---|
| `40101 UNAUTHORIZED` | Wrong key/secret, or the key belongs to the App rather than the Exchange. Recreate it on the Exchange. |
| `40103 IP_ILLEGAL` | Your public IP isn't on the key's whitelist (`curl ifconfig.me`). |
| `40104 USER_TIER_INVALID` | Your account tier or jurisdiction isn't allowed this call. Contact Crypto.com support. |
| `40102 INVALID_NONCE` | Your Mac clock is more than 60 s off. Run `sudo sntp -sS time.apple.com`. Reads retry automatically. |
| `306 INSUFFICIENT_AVAILABLE_BALANCE` / "exceeds the available Exchange USD balance" | Fund the **Exchange** USD wallet. App Cash doesn't count. |
| `415 BELOW_MIN_ORDER_SIZE` / `213 INVALID_ORDERQTY` | Amount is under the $1 minimum or breaks quantity rules. The bot pre-checks, but the exchange decides. |
| `208 INSTRUMENT_NOT_TRADABLE` / `309 MARKET_IS_NOT_OPEN` | USDU trading is halted. The bot won't substitute another asset. |
| `42901 TOO_MANY_REQUESTS` | Rate limited (create-order allows 15 per 100 ms; get-order-history 1/s). Wait and rerun. |
| `ORDER STATE UNKNOWN` / `UNVERIFIED` | Check Exchange → Orders. Rerun `python main.py` to reconcile before anything new is bought. |
| `Another run is in progress` | Another `main.py` holds `.bot.lock`. |
| `Configuration error: DRY_RUN must be true or false` | Fix the typo. Unknown values never enable live mode. |

## Known limitations / unresolved items

- **App Cash USD can't be used.** The Exchange API spends only Exchange-held USD, and I found no documented transfer for fiat USD from App Cash to the Exchange.
- **Exchange availability depends on your jurisdiction.** The API answers from anywhere, but `40104` or account errors mean your account can't trade there.
- **`notional` precision isn't specified in the docs.** The bot sends whole cents (`"12.34"`); 2-decimal USD is assumed to be valid.
- **Duplicate detection relies on `DUPLICATE_CLORDID`.** The docs list the code but don't say whether `client_oid` must be unique across closed orders, so the bot always looks up by `client_oid` before any resubmit rather than relying on that error.
- **Partial fills.** A market order could end `CANCELED` after a partial fill (for example, a remainder smaller than the 0.1 USDU tick). The bot reports the actual executed quantity and value, and warns if the status isn't `FILLED`.
- **Fee currency.** Fees come from `cumulative_fee` / `fee_instrument_name` on the order. The docs don't say which currency spot buys pay fees in.
- **No live order has been placed** in testing. The live path was exercised against the real API only up to authentication (`40101` with an invalid key) and with test doubles for fills.
