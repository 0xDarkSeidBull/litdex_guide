#!/usr/bin/env python3
"""
PAPER MODE ONLY. No private key, no orders sent anywhere. Pure simulation.

Folder layout created on first run:
    paper_bot_data/
        ticks/ticks_<window_ts>.csv       <- every-second price log, T300->T000, every window
        trades_ptb_ge15.csv                <- full trade lifecycle, windows where PTB>=15 at entry
        trades_ptb_lt15.csv                <- full trade lifecycle, windows where PTB<15 at entry

Logic per 5-min window:
  T300 -> T001 : log up/down bid+ask + BTC price + live PTB, every second, ALWAYS (whether or not a trade happens)
  T060 -> T015 : entry watch. First side (Up/Down) whose ASK falls into [0.88, 0.90] gets bought (paper).
                 Only one entry per window. PTB at entry decides which output file this trade lands in
                 (>=15 or <15) -- PTB does NOT block the trade, it's just a classification tag.
  After entry  : manage position every second until closed:
                   - bid >= 0.95            -> TP, close
                   - bid <= 0.70            -> hard floor SL, close (never allowed to go lower)
                   - 0.70 < bid <= 0.84 and drop-rate >= 0.03/sec -> "genuine" SL, close
                   - 0.70 < bid <= 0.84 and drop-rate <  0.03/sec -> "noise", hold, keep watching
  T015         : if still open (no TP/SL fired) -> force-exit at current bid, reason=FORCE_EXIT_T15
  Window close : compare final BTC price to window-open BTC price -> actual outcome (Up/Down).
                 If a trade was taken, classify:
                    TP hit                          -> status = TP_HIT
                    SL hit & outcome == buy dir      -> status = SL_PREMATURE   (SL fired, market went your way anyway)
                    SL hit & outcome != buy dir      -> status = SL_SAVED       (SL correctly protected you)
                    Force-exit & outcome == buy dir  -> status = FORCEEXIT_WOULD_HAVE_WON
                    Force-exit & outcome != buy dir  -> status = FORCEEXIT_WOULD_HAVE_LOST
                 If no entry ever triggered this window -> still logged as NO_ENTRY row (nothing lost/unaccounted).

Run:
    python3 paper_bot.py
Stop anytime with Ctrl+C, safe to restart (appends to CSVs, only loses the in-progress window).
"""
import time
import csv
import os
import requests

# ---------- config ----------
WINDOW_SECONDS   = 300
ENTRY_WATCH_START = 60     # T60
ENTRY_WATCH_END   = 15     # don't open new entries once inside final 15s
FORCE_EXIT_AT     = 15     # T15 -> dump whatever is open
BUY_LOW, BUY_HIGH = 0.88, 0.90
TP_PRICE           = 0.95
HARD_FLOOR          = 0.70
SOFT_SL_CEILING     = 0.84   # start caring about SL logic once bid <= this
VELOCITY_THRESHOLD  = 0.03   # 3c/sec => "genuine" move
STAKE_SHARES        = 5.0    # paper stake size, matches bot's usual $5 buys

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
CLOB_BOOK_URL   = "https://clob.polymarket.com/book"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

DATA_DIR   = "paper_bot_data"
TICKS_DIR  = os.path.join(DATA_DIR, "ticks")
FILE_GE15  = os.path.join(DATA_DIR, "trades_ptb_ge15.csv")
FILE_LT15  = os.path.join(DATA_DIR, "trades_ptb_lt15.csv")

TRADE_HEADER = [
    "window_ts", "slug", "direction", "ptb_at_entry",
    "entry_label", "entry_ts", "entry_price",
    "exit_label", "exit_ts", "exit_price", "exit_reason",
    "btc_open_price", "btc_close_price", "actual_outcome",
    "status", "pnl_usdc",
]

session = requests.Session()


# ---------- helpers ----------
def get_window_ts(now=None):
    now = now or time.time()
    return int(now - (now % WINDOW_SECONDS))


def fetch_token_ids(window_ts, retries=5):
    slug = f"btc-updown-5m-{window_ts}"
    for attempt in range(retries):
        try:
            resp = session.get(GAMMA_EVENT_URL.format(slug=slug), timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data:
                market = data[0]["markets"][0]
                outcomes = eval(market["outcomes"])
                token_ids = eval(market["clobTokenIds"])
                up_idx = outcomes.index("Up")
                down_idx = outcomes.index("Down")
                return token_ids[up_idx], token_ids[down_idx], slug
        except Exception as e:
            print(f"  [warn] fetch_token_ids attempt {attempt+1}: {e}")
        time.sleep(1)
    return None, None, slug


def best_bid_ask(token_id):
    if not token_id:
        return None, None
    try:
        resp = session.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=3)
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        return best_bid, best_ask
    except Exception:
        return None, None


def fetch_btc_price():
    try:
        resp = session.get(BINANCE_PRICE_URL, timeout=3)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


def ensure_dirs_and_headers():
    os.makedirs(TICKS_DIR, exist_ok=True)
    for path in (FILE_GE15, FILE_LT15):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(TRADE_HEADER)


def tick_file_for(window_ts):
    path = os.path.join(TICKS_DIR, f"ticks_{window_ts}.csv")
    new_file = not os.path.exists(path)
    if new_file:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["label", "unix_ts", "up_bid", "up_ask", "down_bid", "down_ask",
                 "btc_price", "ptb"]
            )
    return path


def write_trade_row(ptb_at_entry, row):
    path = FILE_GE15 if (ptb_at_entry is not None and ptb_at_entry >= 15) else FILE_LT15
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)


# ---------- per-window state ----------
class WindowState:
    def __init__(self, window_ts, up_id, down_id, slug, btc_open):
        self.window_ts = window_ts
        self.up_id = up_id
        self.down_id = down_id
        self.slug = slug
        self.btc_open = btc_open
        self.entered = False
        self.direction = None       # "Up" / "Down"
        self.entry_price = None
        self.entry_label = None
        self.entry_ts = None
        self.ptb_at_entry = None
        self.closed = False
        self.exit_price = None
        self.exit_label = None
        self.exit_ts = None
        self.exit_reason = None
        self.prev_bid = None        # for velocity calc, only the bought token's bid
        self.prev_bid_ts = None
        self.last_btc_price = None
        self.finalized = False      # trade row written or no-entry row written


def classify_and_write(state, sec_label_at_close):
    """Called once when window fully closes (T000)."""
    if state.finalized:
        return
    state.finalized = True

    btc_close = state.last_btc_price
    if btc_close is not None and state.btc_open is not None:
        actual_outcome = "Up" if btc_close >= state.btc_open else "Down"
    else:
        actual_outcome = "UNKNOWN"

    if not state.entered:
        # no trade this window at all -- still record it so nothing is unaccounted
        row = [
            state.window_ts, state.slug, "NONE", None,
            None, None, None,
            None, None, None, "NO_ENTRY",
            state.btc_open, btc_close, actual_outcome,
            "NO_ENTRY", 0.0,
        ]
        # no PTB known -> dump into lt15 file by default (nothing traded anyway)
        write_trade_row(0, row)
        print(f"  [{state.slug}] no entry triggered this window. actual={actual_outcome}")
        return

    if state.exit_reason == "TP":
        status = "TP_HIT"
    elif state.exit_reason in ("SL_GENUINE", "SL_HARD_FLOOR"):
        status = "SL_PREMATURE" if actual_outcome == state.direction else "SL_SAVED"
    elif state.exit_reason == "FORCE_EXIT_T15":
        status = "FORCEEXIT_WOULD_HAVE_WON" if actual_outcome == state.direction else "FORCEEXIT_WOULD_HAVE_LOST"
    else:
        status = f"UNKNOWN_REASON({state.exit_reason})"

    buy_cost = STAKE_SHARES * state.entry_price
    sell_proceeds = STAKE_SHARES * (state.exit_price if state.exit_price is not None else 0.0)
    pnl = sell_proceeds - buy_cost

    row = [
        state.window_ts, state.slug, state.direction, state.ptb_at_entry,
        state.entry_label, state.entry_ts, state.entry_price,
        state.exit_label, state.exit_ts, state.exit_price, state.exit_reason,
        state.btc_open, btc_close, actual_outcome,
        status, round(pnl, 4),
    ]
    write_trade_row(state.ptb_at_entry, row)
    print(f"  [{state.slug}] {state.direction} entry={state.entry_price:.2f} "
          f"exit={state.exit_price:.2f} ({state.exit_reason}) actual={actual_outcome} "
          f"-> {status} pnl={pnl:.3f}")


def maybe_enter(state, sec_remaining, up_bid, up_ask, down_bid, down_ask, ptb, label, now):
    if state.entered:
        return
    if not (ENTRY_WATCH_END < sec_remaining <= ENTRY_WATCH_START):
        return
    up_hit = up_ask is not None and BUY_LOW <= up_ask <= BUY_HIGH
    down_hit = down_ask is not None and BUY_LOW <= down_ask <= BUY_HIGH
    # if both hit same tick, just prefer whichever is closer to BUY_LOW (cheaper/earlier-looking)
    if up_hit and (not down_hit or up_ask <= down_ask):
        state.entered = True
        state.direction = "Up"
        state.entry_price = up_ask
        state.entry_label = label
        state.entry_ts = int(now)
        state.ptb_at_entry = ptb
        state.prev_bid = up_bid
        state.prev_bid_ts = now
    elif down_hit:
        state.entered = True
        state.direction = "Down"
        state.entry_price = down_ask
        state.entry_label = label
        state.entry_ts = int(now)
        state.ptb_at_entry = ptb
        state.prev_bid = down_bid
        state.prev_bid_ts = now


def manage_position(state, sec_remaining, up_bid, down_bid, label, now):
    if not state.entered or state.closed:
        return
    current_bid = up_bid if state.direction == "Up" else down_bid
    if current_bid is None:
        return

    # force exit window, always wins regardless of anything else
    if sec_remaining <= FORCE_EXIT_AT:
        state.closed = True
        state.exit_price = current_bid
        state.exit_label = label
        state.exit_ts = int(now)
        state.exit_reason = "FORCE_EXIT_T15"
        return

    if current_bid >= TP_PRICE:
        state.closed = True
        state.exit_price = current_bid
        state.exit_label = label
        state.exit_ts = int(now)
        state.exit_reason = "TP"
        return

    if current_bid <= HARD_FLOOR:
        state.closed = True
        state.exit_price = current_bid
        state.exit_label = label
        state.exit_ts = int(now)
        state.exit_reason = "SL_HARD_FLOOR"
        return

    if current_bid <= SOFT_SL_CEILING:
        # velocity check vs previous tick's bid for this same token
        if state.prev_bid is not None and state.prev_bid_ts is not None:
            dt = max(now - state.prev_bid_ts, 0.001)
            drop_rate = (state.prev_bid - current_bid) / dt
            if drop_rate >= VELOCITY_THRESHOLD:
                state.closed = True
                state.exit_price = current_bid
                state.exit_label = label
                state.exit_ts = int(now)
                state.exit_reason = "SL_GENUINE"
                return
        # else: noise, hold, fall through

    state.prev_bid = current_bid
    state.prev_bid_ts = now


# ---------- main loop ----------
def main():
    ensure_dirs_and_headers()
    print("PAPER MODE — no orders, no private key, pure simulation.\n")

    current_window_ts = None
    state = None

    while True:
        now = time.time()
        window_ts = get_window_ts(now)
        elapsed = now - window_ts
        sec_remaining = WINDOW_SECONDS - int(elapsed) - 1
        if sec_remaining < 0:
            sec_remaining = 0

        if window_ts != current_window_ts:
            # finalize previous window if any
            if state is not None:
                classify_and_write(state, "T000")
            up_id, down_id, slug = fetch_token_ids(window_ts)
            btc_open = fetch_btc_price()
            print(f"\n=== New window {window_ts} ({slug}) | BTC open={btc_open} ===")
            state = WindowState(window_ts, up_id, down_id, slug, btc_open)
            current_window_ts = window_ts

        label = f"T{sec_remaining:03d}"

        up_bid = up_ask = down_bid = down_ask = None
        if state.up_id and state.down_id:
            up_bid, up_ask = best_bid_ask(state.up_id)
            down_bid, down_ask = best_bid_ask(state.down_id)

        btc_price = fetch_btc_price()
        if btc_price is not None:
            state.last_btc_price = btc_price
        ptb = abs(btc_price - state.btc_open) if (btc_price is not None and state.btc_open is not None) else None

        # log tick, always
        tpath = tick_file_for(window_ts)
        with open(tpath, "a", newline="") as f:
            csv.writer(f).writerow([label, int(now), up_bid, up_ask, down_bid, down_ask, btc_price, ptb])

        # trading logic
        maybe_enter(state, sec_remaining, up_bid, up_ask, down_bid, down_ask, ptb, label, now)
        manage_position(state, sec_remaining, up_bid, down_bid, label, now)

        status_bit = ""
        if state.entered:
            status_bit = f" | pos={state.direction}@{state.entry_price:.2f}" + \
                         (f" CLOSED({state.exit_reason}@{state.exit_price:.2f})" if state.closed else " open")
        print(f"{label} ptb={ptb} up_ask={up_ask} down_ask={down_ask}{status_bit}")

        time.sleep(max(0, 1 - (time.time() - now)))


if __name__ == "__main__":
    main()
