"""READ-ONLY diagnostic: replay modules/momentum_strategy.py over the last ~10 days of
real MT5 XAUUSD data and report what the strategy WOULD have signalled per in-session
M15 bar from Monday onward. No orders are placed (copy_rates only)."""
import json
from datetime import timedelta

import pandas as pd
import MetaTrader5 as mt5

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.momentum_strategy import MomentumBreakoutStrategy

cfg = json.load(open(r"C:\fusion_sniper_bot\config.json"))
B = cfg["BROKER"]
mom_cfg = cfg["STRATEGY"]["momentum"]
strat = MomentumBreakoutStrategy(mom_cfg)
LOOKBACK = strat.lookback
BROKER_OFFSET = 3   # empirically verified: ICMarkets server is UTC+3 in summer (DST)
UK_OFFSET = 1                                              # UK BST = UTC+1 (June)
SYM = B["symbol"]

assert mt5.initialize(path=B["mt5_path"], login=B["account"],
                      password=B["password"], server=B["server"],
                      portable=B.get("portable", False)), f"init failed: {mt5.last_error()}"

# ~10 trading days of M15 = ~960 bars; grab extra. H4 needs >= ema50 history.
m15 = pd.DataFrame(mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_M15, 0, 1200))
h4 = pd.DataFrame(mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_H4, 0, 400))
mt5.shutdown()

for df in (m15, h4):
    df["broker_t"] = pd.to_datetime(df["time"], unit="s")          # broker wall clock
    df["uk_open"] = df["broker_t"] - timedelta(hours=BROKER_OFFSET - UK_OFFSET)

m15["uk_close"] = m15["uk_open"] + timedelta(minutes=15)
h4["uk_close"] = h4["uk_open"] + timedelta(hours=4)

MON = pd.Timestamp("2026-06-08")   # Monday this week (UK date)

print(f"M15 bars: {len(m15)}  range {m15['uk_open'].iloc[0]} .. {m15['uk_open'].iloc[-1]} (UK)")
print(f"H4  bars: {len(h4)}  range {h4['uk_open'].iloc[0]} .. {h4['uk_open'].iloc[-1]} (UK)")
print(f"Lookback={LOOKBACK} session={strat.sess_start}-{strat.sess_end} UK  h4_ema={strat.h4_ema}\n")
print(f"{'UK close':<17} {'hr':>2} {'H4dir':>5} {'h4close':>9} {'h4ema50':>9} "
      f"{'prHigh':>9} {'prLow':>9} {'close':>9} {'signal':>6}")

signals = []
for i in range(LOOKBACK, len(m15)):
    bar = m15.iloc[i]
    uk_close = bar["uk_close"]
    if uk_close < MON:
        continue
    uk_hour = uk_close.hour
    if not strat.in_session(uk_hour):
        continue
    # H4 bars closed at/before this M15 bar closed (what the live fetch would have)
    h4_known = h4[h4["uk_close"] <= uk_close]
    if len(h4_known) < strat.h4_ema:
        continue
    ema = strat.ema_series(h4_known["close"])
    h4_close = float(h4_known["close"].iloc[-1])
    h4_ema = float(ema.iloc[-1])
    trend = strat.trend_sign(h4_close, h4_ema)
    # m15 window: prior LOOKBACK bars (excl. signal bar) + the signal bar = module's slice
    window = m15.iloc[i - LOOKBACK:i + 1]
    sig = strat.signal(window, h4_known)
    prior = window.iloc[:-1]
    prh, prl = float(prior["high"].max()), float(prior["low"].min())
    close = float(bar["close"])
    direction = sig["type"] if sig else "None"
    if sig:
        signals.append((uk_close, direction, close, prh, prl))
    tdir = {1: "UP", -1: "DOWN", 0: "FLAT"}[trend]
    print(f"{str(uk_close):<17} {uk_hour:>2d} {tdir:>5} {h4_close:>9.2f} {h4_ema:>9.2f} "
          f"{prh:>9.2f} {prl:>9.2f} {close:>9.2f} {direction:>6}")

print(f"\n=== {len(signals)} entry signal(s) this week ===")
for s in signals:
    print(f"  {s[0]}  {s[1]}  close={s[2]:.2f}  brokeHigh={s[3]:.2f} / Low={s[4]:.2f}")

# H4 trend context across the week
print("\n--- H4 close vs EMA50 (most recent 14 H4 bars) ---")
ema_full = strat.ema_series(h4["close"])
for j in range(max(0, len(h4) - 14), len(h4)):
    c = float(h4["close"].iloc[j]); e = float(ema_full.iloc[j])
    print(f"  {str(h4['uk_close'].iloc[j]):<17} close={c:>9.2f} ema50={e:>9.2f} "
          f"diff={c - e:>+7.2f} {'UP' if c > e else 'DOWN'}")
