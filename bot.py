import curses, time, logging, csv, os
from datetime import datetime, timezone
import chainlink_feed
from markets import find_current_market
from clob import get_best_ask, get_best_bid, place_buy, get_order
try:
    from clob import place_limit_sell
except ImportError:
    place_limit_sell = None
try:
    from clob import cancel_order
except ImportError:
    cancel_order = None
from config import PAPER_TRADE

logging.basicConfig(filename="/root/btc5m_v2/bot.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

POLL         = 0.15
BUY_MIN      = 0.88
BUY_MAX      = 0.90
AUTO_REM_SEC = 60
AUTO_MIN_REM_SEC = 30   # no buy once less than this many sec remain
PTB_GATE     = 15.0   # only auto-buy if |BTC-PTB| >= this many dollars
TIME_OFFSET_SEC = 2.0   # +ve = bot ko rem thoda zyada dikhega (jaldi trigger), -ve = der se trigger
TP_PRICE     = 0.94
SL_PRICE     = 0.84   # (with FORCE_EXIT_REM_SEC below)
FORCE_EXIT_REM_SEC = 10   # if TP/SL not hit and this few sec remain, close at market regardless

# ---------- data logging (pure logging, zero effect on trading logic) ----------
DATA_DIR   = "/root/btc5m_v2/bot_data"
TICKS_DIR  = os.path.join(DATA_DIR, "ticks")
FILE_GE15  = os.path.join(DATA_DIR, "trades_ptb_ge15.csv")
FILE_LT15  = os.path.join(DATA_DIR, "trades_ptb_lt15.csv")
TRADE_LOG_HEADER = [
    "window_ts", "slug", "direction", "ptb_at_entry", "btc_at_entry",
    "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason",
    "shares", "cost", "proceeds", "pnl_usd", "pnl_pct", "result",
]

def _ensure_data_dirs():
    os.makedirs(TICKS_DIR, exist_ok=True)
    for path in (FILE_GE15, FILE_LT15):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(TRADE_LOG_HEADER)

def _tick_file_for(slug):
    safe = (slug or "unknown").replace("/", "_")
    path = os.path.join(TICKS_DIR, f"ticks_{safe}.csv")
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["unix_ts", "rem_sec", "label", "btc", "ptb", "ptb_diff",
                 "up_ask", "up_bid", "dn_ask", "dn_bid"]
            )
    return path

def log_tick(s):
    """Append one row of current window state. Called every poll cycle. Never raises."""
    if not s.market:
        return
    try:
        rem = (s.market["end_time"] - datetime.now(timezone.utc)).total_seconds()
        rem_i = max(0, int(rem))
        label = f"T{rem_i:03d}"
        ptb_diff = (s.btc - s.ptb) if (s.btc is not None and s.ptb is not None) else None
        path = _tick_file_for(s.market.get("slug"))
        with open(path, "a", newline="") as f:
            csv.writer(f).writerow([
                int(time.time()), rem_i, label, s.btc, s.ptb, ptb_diff,
                s.up_ask, s.up_bid, s.dn_ask, s.dn_bid,
            ])
    except Exception as e:
        logger.error(f"[DATALOG] tick write failed: {e}")

def log_no_entry(s, slug):
    """Called when a window closes with no trade taken -- keeps the record complete."""
    try:
        row = [
            slug, slug, "NONE", None, s.btc,
            None, None, None, None, "NO_ENTRY",
            None, None, None, None, None, "NO_ENTRY",
        ]
        with open(FILE_LT15, "a", newline="") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        logger.error(f"[DATALOG] no-entry write failed: {e}")

def log_trade_result(s, pos, avg_px, qty, proceeds, pnl, pnl_pct, result, exit_reason):
    """Called from _finalize -- classifies into ge15/lt15 file by PTB at entry time. Never raises."""
    try:
        ptb_entry = pos.get('ptb_at_entry')
        btc_entry = pos.get('btc_at_entry')
        slug      = pos.get('slug', '')
        row = [
            pos.get('window_ts'), slug, pos['dir'], ptb_entry, btc_entry,
            pos['ts'], pos['entry'], datetime.now().strftime("%H:%M:%S"), avg_px, exit_reason,
            pos['shares'], pos['cost'], proceeds, round(pnl, 4), pnl_pct, result,
        ]
        target = FILE_GE15 if (ptb_entry is not None and ptb_entry >= PTB_GATE) else FILE_LT15
        with open(target, "a", newline="") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        logger.error(f"[DATALOG] trade write failed: {e}")

_C = {}

def init_colors():
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,   -1)
    curses.init_pair(2, curses.COLOR_GREEN,  -1)
    curses.init_pair(3, curses.COLOR_RED,    -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(6, curses.COLOR_WHITE,  -1)
    _C['hdr'] = curses.color_pair(1) | curses.A_BOLD
    _C['sep'] = curses.color_pair(1)
    _C['grn'] = curses.color_pair(2) | curses.A_BOLD
    _C['red'] = curses.color_pair(3) | curses.A_BOLD
    _C['ylw'] = curses.color_pair(4)
    _C['ylb'] = curses.color_pair(4) | curses.A_BOLD
    _C['wht'] = curses.color_pair(6)
    _C['dim'] = curses.color_pair(6) | curses.A_DIM

class State:
    def __init__(self):
        self.market          = None
        self.up_tok          = self.dn_tok = None
        self.up_ask          = self.dn_ask = None
        self.up_bid          = self.dn_bid = None
        self.btc             = None
        self.ptb             = None
        self.ptb_auto        = False
        self.pos             = None
        self.msg             = "Ready"
        self.history         = []
        self.total_pnl       = 0.0
        self.wins            = self.losses = 0
        self.last_poll       = 0
        self.window_auto_done = False
        self.window_auto_done_traded = False

def find_window():
    return find_current_market("btc", 5)

def do_refresh(s):
    p = chainlink_feed.get_price("btc/usd")
    if p: s.btc = p
    if s.up_tok:
        a = get_best_ask(s.up_tok)
        if a: s.up_ask = a
        b = get_best_bid(s.up_tok)
        if b: s.up_bid = b
    if s.dn_tok:
        a = get_best_ask(s.dn_tok)
        if a: s.dn_ask = a
        b = get_best_bid(s.dn_tok)
        if b: s.dn_bid = b
    if s.pos:
        b = get_best_bid(s.pos['tok'])
        if b is not None: s.pos['bid'] = b
    s.last_poll = time.time()

def do_buy(s, direction):
    if s.pos:
        s.msg = "!! Already in position"; return
    if not s.market:
        s.msg = "!! No market loaded"; return
    tok   = s.up_tok if direction == "UP" else s.dn_tok
    price = s.up_ask if direction == "UP" else s.dn_ask
    if not price:
        s.msg = f"!! No ask price for {direction}"; return
    price = round(price, 2)
    if not (BUY_MIN <= price <= BUY_MAX):
        s.msg = f"!! BLOCKED: {direction} ask={price*100:.0f}c range [{BUY_MIN*100:.0f}-{BUY_MAX*100:.0f}c] ke bahar -- buy cancel"
        logger.warning(f"[BUY-BLOCKED] {direction} @ {price:.2f} outside [{BUY_MIN:.2f},{BUY_MAX:.2f}]")
        return
    logger.info(f"[BUY] {direction} @ {price:.2f}")
    resp = place_buy(tok, price)
    # Accept order if we got ANY response with orderID or success
    oid = (resp or {}).get("orderID") or (resp or {}).get("id")
    ok  = (resp or {}).get("success") or bool(oid)
    if ok:
        shares = (resp or {}).get("shares", 5.0)
        cost   = round(shares * price, 4)
        s.pos  = dict(dir=direction, tok=tok, entry=price, shares=shares,
                      cost=cost, ts=datetime.now().strftime("%H:%M:%S"),
                      bid=None, filled=0.0, proceeds=0.0, sell_oid=None,
                      sell_placed_at=0,
                      ptb_at_entry=s.ptb, btc_at_entry=s.btc,
                      window_ts=(s.market.get("slug") if s.market else None),
                      slug=(s.market.get("slug") if s.market else None))
        tag = "[PAPER]" if PAPER_TRADE else "[LIVE]"
        s.msg = f"BOUGHT {tag} {direction} @ {price*100:.0f}c  {shares:.2f}sh  cost=${cost:.2f}"
        logger.info(f"[BUY] OK {direction} @ {price:.2f} shares={shares} cost={cost}")
        s.window_auto_done_traded = True
    else:
        s.msg = f"!! BUY FAILED -- {resp}"
        logger.error(f"[BUY] FAILED {direction} @ {price:.2f} resp={resp}")

def do_auto_check(s):
    if s.pos or s.window_auto_done or not s.market:
        return
    rem = (s.market["end_time"] - datetime.now(timezone.utc)).total_seconds() + TIME_OFFSET_SEC
    if rem > AUTO_REM_SEC or rem < AUTO_MIN_REM_SEC:
        return
    if s.ptb is None or s.btc is None:
        return
    if abs(s.btc - s.ptb) < PTB_GATE:
        return
    direction = None
    if s.up_ask and BUY_MIN <= round(s.up_ask, 2) <= BUY_MAX:
        direction = "UP"
    elif s.dn_ask and BUY_MIN <= round(s.dn_ask, 2) <= BUY_MAX:
        direction = "DOWN"
    else:
        return
    s.window_auto_done = True
    logger.info(f"[AUTO] rem={rem:.0f}s BUY {direction}")
    do_buy(s, direction)

def do_sell(s):
    if not s.pos:
        s.msg = "!! No position to sell"; return
    if s.pos.get('sell_oid'):
        s.msg = "!! Sell already placed"; return
    bid = s.pos.get('bid') or get_best_bid(s.pos['tok'])
    if not bid:
        s.msg = "!! No bid price available"; return
    bid       = round(bid, 2)
    remaining = round(s.pos['shares'] - s.pos.get('filled', 0.0), 6)
    if remaining <= 1e-6:
        _finalize(s); return
    if not place_limit_sell:
        s.msg = "!! place_limit_sell missing from clob.py"; return

    # Track first sell attempt time
    if not s.pos.get('first_sell_at'):
        s.pos['first_sell_at'] = time.time()

    # Force-clear if sell has been failing for >8 seconds
    # (order likely went through on Polymarket despite None response)
    elif time.time() - s.pos['first_sell_at'] > 8:
        logger.warning(f"[SELL] 8s timeout -- assuming sell went through, force-clearing position")
        s.msg = "Sell assumed filled (8s timeout) -- check Polymarket"
        s.pos['filled']   = s.pos['shares']
        s.pos['proceeds'] = s.pos['shares'] * bid
        _finalize(s)
        return

    logger.info(f"[SELL] placing limit sell {remaining:.2f}sh @ {bid:.2f}")
    resp = place_limit_sell(s.pos['tok'], bid, remaining)
    logger.info(f"[SELL] raw resp={resp}")
    oid = (resp or {}).get("orderID") or (resp or {}).get("id")
    ok  = (resp or {}).get("success") or bool(oid)
    if ok:
        s.pos['sell_oid']       = oid or "UNKNOWN"
        s.pos['sell_price']     = bid
        s.pos['sell_placed_at'] = time.time()
        s.msg = f"SELL placed @ {bid*100:.0f}c  {remaining:.2f}sh  oid={oid}"
        logger.info(f"[SELL] placed @ {bid:.2f} remaining={remaining} oid={oid}")
    else:
        logger.error(f"[SELL] FAILED resp={resp} -- retrying")
        s.msg = f"Sell retrying... ({int(time.time() - s.pos.get('first_sell_at', time.time()))}s)"


def check_tp_sl(s):
    if not s.pos or s.pos.get('sell_oid'):
        return
    bid = s.pos.get('bid')
    if bid is None:
        return
    if bid >= TP_PRICE:
        logger.info(f"[TP] bid={bid:.2f} >= {TP_PRICE:.2f}")
        s.msg = f"TP hit @ {bid*100:.0f}c -- selling"
        s.pos['exit_reason'] = "TP"
        do_sell(s)
    elif bid <= SL_PRICE:
        logger.info(f"[SL] bid={bid:.2f} <= {SL_PRICE:.2f}")
        s.msg = f"SL hit @ {bid*100:.0f}c -- selling"
        s.pos['exit_reason'] = "SL"
        do_sell(s)
    elif s.market:
        rem = (s.market["end_time"] - datetime.now(timezone.utc)).total_seconds() + TIME_OFFSET_SEC
        if 0 < rem <= FORCE_EXIT_REM_SEC:
            logger.info(f"[FORCE-EXIT] rem={rem:.0f}s bid={bid:.2f} -- neither TP nor SL hit, closing now")
            s.msg = f"FORCE-EXIT @ {bid*100:.0f}c (rem={rem:.0f}s) -- selling"
            s.pos['exit_reason'] = "FORCE_EXIT"
            do_sell(s)

def _poll_sell(s):
    pos = s.pos
    if not pos or not pos.get('sell_oid'):
        return
    oid = pos['sell_oid']
    if oid == "UNKNOWN":
        # No orderID — can't track, assume filled and finalize
        logger.warning("[SELL] oid=UNKNOWN, assuming filled and finalizing")
        pos['filled']   = pos['shares']
        pos['proceeds'] = pos['shares'] * pos.get('sell_price', pos['entry'])
        _finalize(s)
        return
    order = get_order(oid)
    if not order:
        # API returned nothing — don't assume, just wait
        return
    logger.info(f"[SELL] poll oid={oid} order={order}")
    status  = (order.get("status") or "").upper()
    matched = float(order.get("size_matched",
                    order.get("filled_size",
                    order.get("filled",
                    order.get("amount_filled", 0)))) or 0)
    avg_px  = float(order.get("avg_price",
                    order.get("price",
                    pos.get('sell_price', pos['entry']))) or pos.get('sell_price', pos['entry']))
    prev     = pos.get('filled', 0.0)
    new_fill = round(matched - prev, 6)
    if new_fill > 1e-6:
        pos['filled']   = matched
        pos['proceeds'] = pos.get('proceeds', 0.0) + new_fill * avg_px
        logger.info(f"[SELL] +{new_fill:.2f}sh @ {avg_px*100:.0f}c (cum {matched:.2f})")
    remaining = round(pos['shares'] - pos.get('filled', 0.0), 6)
    # Fully filled
    if remaining <= 1e-6 or status in ("MATCHED", "FILLED", "MINED"):
        if pos.get('filled', 0) <= 1e-6 and matched <= 1e-6:
            # status says filled but no size data — use shares at sell_price
            pos['filled']   = pos['shares']
            pos['proceeds'] = pos['shares'] * pos.get('sell_price', pos['entry'])
        _finalize(s)
        return
    # Cancelled/rejected — retry
    if status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
        pos['sell_oid'] = None
        s.msg = "!! Sell cancelled -- retrying"
        return
    # Stalled > 3s with no fill — cancel and retry
    placed_at = pos.get('sell_placed_at', time.time())
    if new_fill <= 1e-6 and (time.time() - placed_at) >= 3.0:
        if cancel_order:
            try:
                cancel_order(oid)
                logger.info(f"[SELL] cancelled stalled order {oid}")
            except Exception as e:
                logger.error(f"[SELL] cancel error: {e}")
        pos['sell_oid'] = None
        s.msg = "Sell stalled -- retrying"
        return
    s.msg = f"Selling @ {pos.get('sell_price',0)*100:.0f}c -- {remaining:.2f}sh resting"

def _finalize(s):
    pos      = s.pos
    qty      = pos.get('filled', 0.0)
    proceeds = pos.get('proceeds', 0.0)
    avg_px   = proceeds / qty if qty else 0.0
    pnl      = proceeds - pos['cost']
    pnl_pct  = round(pnl / pos['cost'] * 100, 2) if pos['cost'] else 0
    result   = "WIN" if pnl > 0 else "LOSS"
    if pnl > 0: s.wins += 1
    else:       s.losses += 1
    s.total_pnl += pnl
    s.history.append(dict(time=pos['ts'], dir=pos['dir'], entry=pos['entry'],
                           pnl_usd=pnl, pnl_pct=pnl_pct, result=result,
                           info=f"avg {avg_px*100:.0f}c"))
    s.msg = f"{result}: {pos['dir']} avg={avg_px*100:.0f}c -> ${pnl:+.4f} ({pnl_pct:+.1f}%)"
    logger.info(f"[FINALIZE] {result} avg={avg_px:.4f} qty={qty} pnl={pnl:.4f}")
    log_trade_result(s, pos, avg_px, qty, proceeds, pnl, pnl_pct, result,
                      pos.get('exit_reason', 'UNKNOWN'))
    s.pos = None

def ask_ptb(stdscr):
    h, w = stdscr.getmaxyx()
    stdscr.nodelay(False); curses.echo(); curses.curs_set(1)
    row = min(h - 4, 20)
    prompt = " Enter BTC PTB price (Enter=skip): "
    try:
        stdscr.addstr(row,     0, " " * (w-1))
        stdscr.addstr(row + 1, 0, " " * (w-1))
        stdscr.addstr(row, 0, prompt[:w-1], _C.get("ylb", 0))
        stdscr.refresh()
        val = stdscr.getstr(row + 1, 0, 30).decode().strip()
        ptb = float(val.replace(",", "").replace("$", "")) if val else None
    except Exception:
        ptb = None
    curses.noecho(); curses.curs_set(0); stdscr.nodelay(True)
    return ptb

def load_next_window(s, current_slug=""):
    market = find_window()
    if market and market.get("slug") != current_slug:
        if current_slug and not s.window_auto_done_traded:
            log_no_entry(s, current_slug)
        s.market = market
        s.up_tok = market["tokens"][0]
        s.dn_tok = market["tokens"][1]
        s.up_ask = s.dn_ask = s.up_bid = s.dn_bid = None
        s.window_auto_done = False
        s.window_auto_done_traded = False
        if s.btc:
            s.ptb = s.btc; s.ptb_auto = True
        logger.info(f"[WINDOW] switched to {market.get('slug')}")
        if not s.pos:
            s.msg = "New window loaded -- watching"
        return True
    return False

def render(stdscr, s):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    r = [0]
    def ln(txt="", c="wht", bold=False):
        if r[0] >= h - 1: return
        attr = _C.get(c, 0) | (curses.A_BOLD if bold else 0)
        try: stdscr.addnstr(r[0], 0, str(txt)[:w-1], w-1, attr)
        except curses.error: pass
        r[0] += 1
    def sep(): ln("─"*(w-1), "sep")

    tag = "[PAPER]" if PAPER_TRADE else "[LIVE]"
    ln(f" == BTC 5m AUTO BOT {tag} ==", "hdr")
    sep()
    ok = chainlink_feed.is_connected()
    ln(f" Feed : {'OK' if ok else 'RECONNECTING...'}", "grn" if ok else "red")
    ln(f" BTC  : {'${:,.2f}'.format(s.btc) if s.btc else '-'}", "wht")
    if s.ptb and s.btc:
        diff    = s.btc - s.ptb
        ptb_tag = "(auto)" if s.ptb_auto else "(manual)"
        ln(f" PTB  : ${s.ptb:,.2f} {ptb_tag}  diff={diff:+.2f}", "ylw")
    else:
        ln(" PTB  : NOT SET  [p] to set", "dim")
    if s.market:
        rem = (s.market["end_time"] - datetime.now(timezone.utc)).total_seconds()
        mm, ss = divmod(max(0, int(rem)), 60)
        tc = "red" if AUTO_MIN_REM_SEC <= rem <= AUTO_REM_SEC else ("ylw" if rem < 90 else "grn")
        ptb_ok = s.ptb is not None and s.btc is not None and abs(s.btc - s.ptb) >= PTB_GATE
        armed = AUTO_MIN_REM_SEC <= rem <= AUTO_REM_SEC and not s.window_auto_done and not s.pos and ptb_ok
        ln(f" Win  : {s.market.get('slug','')[-30:]}", "wht")
        ln(f" Time : {mm:02d}:{ss:02d}   AutoArm: {'YES' if armed else 'no'}  PTBgate: {'OK' if ptb_ok else 'no'}", tc, bold=True)
    sep()
    ln(f" UP   ask={f'{s.up_ask*100:.0f}c' if s.up_ask else '-':5} bid={f'{s.up_bid*100:.0f}c' if s.up_bid else '-'}", "grn", bold=True)
    ln(f" DOWN ask={f'{s.dn_ask*100:.0f}c' if s.dn_ask else '-':5} bid={f'{s.dn_bid*100:.0f}c' if s.dn_bid else '-'}", "red", bold=True)
    ln(f" AutoBuy range: [{BUY_MIN*100:.0f}c-{BUY_MAX*100:.0f}c]  window {AUTO_MIN_REM_SEC}-{AUTO_REM_SEC}s  TP={TP_PRICE*100:.0f}c SL={SL_PRICE*100:.0f}c", "dim")
    sep()
    if s.pos:
        pc    = "grn" if s.pos["dir"] == "UP" else "red"
        bid_s = f"bid={s.pos['bid']*100:.0f}c" if s.pos.get('bid') is not None else "bid=-"
        filled = s.pos.get('filled', 0.0)
        ln(f" OPEN: {s.pos['dir']}  {s.pos['shares']:.2f}sh @ {s.pos['entry']*100:.0f}c  cost=${s.pos['cost']:.2f}  {bid_s}", pc, bold=True)
        if s.pos.get('sell_oid'):
            rem2  = round(s.pos['shares'] - filled, 2)
            blink = "ylb" if int(time.time()*3) % 2 == 0 else "wht"
            ln(f"   Selling @ {s.pos.get('sell_price',0)*100:.0f}c -- {filled:.2f}/{s.pos['shares']:.2f}sh  ({rem2:.2f}sh resting)", blink, bold=True)
        else:
            ln("   Watching for TP/SL...", "ylw")
    else:
        ln(" No position -- watching for auto trigger", "dim")
    sep()
    mc = ("grn" if any(k in s.msg for k in ("BOUGHT", "WIN", "SELL placed"))
          else "red" if any(k in s.msg for k in ("FAIL", "LOSS", "!!"))
          else "ylw")
    ln(f" > {s.msg[:w-4]}", mc)
    ln(" [1] UP   [2] DOWN   [3] SELL   [p] PTB   [q] quit", "dim")
    sep()
    total = s.wins + s.losses
    wr    = f"{s.wins/total*100:.0f}%" if total else "-"
    pc    = "grn" if s.total_pnl >= 0 else "red"
    ln(f" {total} trades  W:{s.wins} L:{s.losses}  WR:{wr}  PnL:${s.total_pnl:+.4f}", pc, bold=True)
    if s.history:
        sep()
        for t in reversed(s.history[-10:]):
            c = "grn" if t["pnl_usd"] >= 0 else "red"
            ln(f"  {t['time']} {t['dir']:4} @ {t['entry']*100:.0f}c  {t['pnl_pct']:+.1f}%  {t['result']} ({t['info']})", c)
    stdscr.refresh()

def main(stdscr):
    init_colors()
    curses.curs_set(0); stdscr.nodelay(True)
    s = State()
    chainlink_feed.start()
    s.msg = "Loading window..."
    render(stdscr, s)
    market = None
    while market is None:
        if stdscr.getch() == ord('q'): return
        market = find_window()
        render(stdscr, s); time.sleep(1)
    s.market = market
    s.up_tok = market["tokens"][0]
    s.dn_tok = market["tokens"][1]
    s.window_auto_done = False
    s.window_auto_done_traded = False
    _ensure_data_dirs()
    logger.info(f"[WINDOW] {market.get('slug')}")
    s.msg = "Ready  [1] UP  [2] DOWN  [3] SELL  [p] PTB  [q] quit"

    last_sell_poll = 0

    while True:
        ch = stdscr.getch()
        if   ch == ord('q'): break
        elif ch == ord('1'): do_buy(s, "UP")
        elif ch == ord('2'): do_buy(s, "DOWN")
        elif ch == ord('3'):
            if s.pos:
                s.pos.setdefault('exit_reason', 'MANUAL_SELL')
            do_sell(s)
        elif ch == ord('p'):
            ptb = ask_ptb(stdscr)
            if ptb:
                s.ptb = ptb; s.ptb_auto = False
                s.msg = f"PTB set: ${ptb:,.2f}"

        now = time.time()
        if now - s.last_poll >= POLL:
            do_refresh(s)
            if s.btc and s.ptb is None:
                s.ptb = s.btc; s.ptb_auto = True
            do_auto_check(s)
            log_tick(s)

        check_tp_sl(s)

        if s.pos and s.pos.get('sell_oid') and now - last_sell_poll >= 0.20:
            _poll_sell(s)
            last_sell_poll = now

        if s.market:
            rem = (s.market["end_time"] - datetime.now(timezone.utc)).total_seconds()
            if rem <= 0:
                current_slug = s.market.get("slug", "")
                # Position still open at window close — resolve as LOSS after 10s grace
                # (rather than silently discarding: this keeps stats accurate.
                # proceeds=0 is an ASSUMPTION since we never confirmed the real
                # redemption value -- msg flags it clearly for manual check)
                if s.pos:
                    elapsed_since_close = abs(rem)
                    if elapsed_since_close > 10:
                        logger.warning("[WINDOW] position still open 10s after close -- "
                                        "resolving as LOSS (unconfirmed, proceeds assumed $0)")
                        s.pos['filled']   = s.pos['shares']
                        s.pos['proceeds'] = 0.0
                        s.pos.setdefault('exit_reason', 'WINDOW_CLOSE_UNCONFIRMED')
                        _finalize(s)
                        s.msg = "!! ESTIMATED LOSS -- window closed with open position, verify on Polymarket!"
                load_next_window(s, current_slug)

        render(stdscr, s)
        time.sleep(0.05)

    stdscr.erase()
    try:
        total = s.wins + s.losses
        wr    = f"{s.wins/total*100:.0f}%" if total else "-"
        stdscr.addstr(0, 0, " === SUMMARY ===", _C.get("hdr", 0))
        stdscr.addstr(2, 0, f" Trades:{total}  W:{s.wins} L:{s.losses}  WR:{wr}", _C.get("ylb", 0))
        pc = "grn" if s.total_pnl >= 0 else "red"
        stdscr.addstr(3, 0, f" PnL: ${s.total_pnl:+.4f}", _C.get(pc, 0) | curses.A_BOLD)
        if s.pos:
            stdscr.addstr(5, 0, " !! Open position at quit -- check Polymarket!", _C.get("red", 0) | curses.A_BOLD)
        stdscr.addstr(7, 0, " Press any key...", _C.get("dim", 0))
    except curses.error: pass
    stdscr.nodelay(False); stdscr.refresh(); stdscr.getch()

if __name__ == "__main__":
    while True:
        try:
            curses.wrapper(main)
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"[CRASH] {type(e).__name__}: {e} -- restarting in 3s")
            time.sleep(3)
