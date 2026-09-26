"""Logs Coinbase perp funding rates once per run (scheduled hourly). Builds the funding
history Coinbase's public API doesn't provide, for a future cash-and-carry backtest."""
import os, time, csv
from datetime import datetime, timezone
import paper_bot as pb

PERPS = ["BIP-20DEC30-CDE", "ETP-20DEC30-CDE", "SLP-20DEC30-CDE", "XPP-20DEC30-CDE", "DOP-20DEC30-CDE",
         "ADP-20DEC30-CDE", "LCP-20DEC30-CDE", "LNP-20DEC30-CDE", "AVP-20DEC30-CDE", "SUP-20DEC30-CDE",
         "ZEC-20DEC30-CDE", "HYP-20DEC30-CDE"]
OUT = os.path.join(pb.HERE, "funding_log.csv")

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for pid in PERPS:
        try:
            j = pb.get_json(pb.API + pid)
            f = j["future_product_details"]
            pb.append_csv(OUT, [now, pid, f.get("funding_rate"), f.get("funding_time"), j.get("price"),
                                f.get("open_interest"), f.get("index_price")],
                          ["utc_time", "product", "funding_rate_hourly", "funding_time", "price", "open_interest", "index_price"])
        except Exception as e:
            print(pid, "ERROR", e)
        time.sleep(0.3)

if __name__ == "__main__":
    main()
