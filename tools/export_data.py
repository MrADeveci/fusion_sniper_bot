import MetaTrader5 as mt5
import json, sys, os, time
import pandas as pd

CONFIG = r"C:\fusion_sniper_bot\config.json"
SYMBOL = "XAUUSD"
OUT_DIR = r"C:\fusion_sniper_bot\data"

with open(CONFIG) as f:
    cfg = json.load(f)
b = cfg["BROKER"]

ok = mt5.initialize(path=b["mt5_path"], login=int(b["account"]),
                    password=b["password"], server=b["server"],
                    portable=b.get("portable", True))
if not ok:
    print("initialize failed:", mt5.last_error()); sys.exit(1)

ti = mt5.terminal_info()
print("connected:", ti.connected, "| build:", ti.build, "| data_path:", ti.data_path)

os.makedirs(OUT_DIR, exist_ok=True)
mt5.symbol_select(SYMBOL, True)
time.sleep(1)

# Request a very large count; on this build the terminal returns all it has
# (capped by what the broker provides) rather than -2, now that history is cached.
BIG = 10_000_000
TARGETS = [("M1", mt5.TIMEFRAME_M1), ("M15", mt5.TIMEFRAME_M15)]

for tf_name, tf in TARGETS:
    rates = None
    for attempt in range(5):
        rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, BIG)
        if rates is not None and len(rates) > 0:
            break
        print(f"{tf_name} attempt {attempt+1}: {mt5.last_error()} - retrying")
        time.sleep(3)
    if rates is None or len(rates) == 0:
        print(tf_name, "FAILED:", mt5.last_error()); continue
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    path = os.path.join(OUT_DIR, f"{SYMBOL}_{tf_name}.csv")
    df.to_csv(path, index=False)
    print(tf_name, len(df), "bars ->", path,
          "| range", df["time"].min(), "to", df["time"].max())

mt5.shutdown()
