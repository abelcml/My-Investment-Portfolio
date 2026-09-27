r"""Append IB connector data (daily NAV + recent trades) to Data\NAV.csv and Data\IB.csv.

Usage: python ingest_ib_connector.py <connector json archived under .Archive\Inbox-processed>

The connector has no stock/cash split and no cash-transaction detail, so:
- Stock column is rebuilt as sum(shares from IB.csv x Yahoo close);
- any day where NAV does not follow IB's own TWR (an implied external flow) aborts,
  because a deposit/withdrawal can't be booked without the Flex cash section.
Nothing is written unless all three checks pass:
  1. implied flow < $0.01 on every day (NAV vs cps consistency),
  2. connector NAV == NAV.csv Total on overlapping days (< $0.01),
  3. rebuilt Stock == NAV.csv Stock on overlapping days (< $1).
A later official Flex export replaces the whole year block, overwriting these rows.
"""
import csv
import io
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

DATA = r"C:\Users\Tr7\Trades\Data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FX_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")


def parse_d(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def yahoo_closes(sym, d0, d1):
    p1 = int(time.mktime((d0 - timedelta(days=10)).timetuple()))
    p2 = int(time.mktime((d1 + timedelta(days=2)).timetuple()))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"
    res = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))["chart"]["result"][0]
    return {time.strftime("%Y-%m-%d", time.localtime(ts)): c
            for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None}


def close_asof(closes, d):
    for k in range(10):
        v = closes.get((d - timedelta(days=k)).isoformat())
        if v:
            return v
    raise SystemExit(f"no close near {d}")


conn = json.load(open(sys.argv[1], encoding="utf-8"))
ytd = conn["ytd"]
days = [parse_d(s) for s in ytd["dates"]]
keep = [i for i, d in enumerate(days) if d.weekday() < 5]   # weekend marks are not trading-day NAVs
days = [days[i] for i in keep]
cnav = [ytd["nav"][i] for i in keep]
cps = [ytd["cps"][i] for i in keep]

# check 1: every day's NAV change is fully explained by IB's own TWR (no external flow)
for i in range(1, len(days)):
    implied_flow = cnav[i] - cnav[i - 1] * (1 + cps[i]) / (1 + cps[i - 1])
    if abs(implied_flow) > 0.01:
        raise SystemExit(f"check 1 FAILED on {days[i]}: implied flow {implied_flow:+.2f} USD")

# existing NAV.csv daily rows
lines = open(rf"{DATA}\NAV.csv", encoding="utf-8-sig").read().splitlines()
nav_total, nav_stock = {}, {}
header = None
for l in lines:
    r = next(csv.reader([l]))
    if r[0] == "Total":
        header = r
        continue
    if r[0] in ("TWR", "Type"):
        header = None
        continue
    if header and len(r) == len(header):
        m = dict(zip(header, r))
        nav_total[parse_d(m["ReportDate"])] = float(m["Total"])
        nav_stock[parse_d(m["ReportDate"])] = float(m["Stock"])

# check 2: overlap agrees with the official Flex NAV
overlap = [d for d in days if d in nav_total]
for d, v in zip(days, cnav):
    if d in nav_total and abs(v - nav_total[d]) > 0.01:
        raise SystemExit(f"check 2 FAILED on {d}: connector {v:.2f} vs NAV.csv {nav_total[d]:.2f}")

# IB.csv trades plus connector trades not yet in it
ib_rows = list(csv.DictReader(open(rf"{DATA}\IB.csv", encoding="utf-8-sig")))
have = {(r["TradeDate"], r["Symbol"], r["Buy/Sell"], float(r["TradePrice"]), abs(float(r["Quantity"])))
        for r in ib_rows if r["Account"] == "IB"}
new_trades = []
for t in conn["trades"]:
    ds = t["trade_time"][:10].replace("-", "")
    key = (ds, t["symbol"], t["side"], float(t["price"]), float(t["size"]))
    if key in have:
        continue
    sign = 1 if t["side"] == "BUY" else -1
    new_trades.append({"TradeDate": ds, "Symbol": t["symbol"], "Buy/Sell": t["side"],
                       "TradePrice": f'{t["price"]:g}', "Quantity": f'{sign * t["size"]:g}',
                       "TradeMoney": f'{sign * t["net_amount"]:g}', "Account": "IB"})

trades = [(parse_d(r["TradeDate"]), r["Symbol"], float(r["Quantity"]))
          for r in ib_rows + new_trades
          if r["Account"] == "IB" and not FX_RE.match(r["Symbol"])
          and not r["Symbol"].endswith(".T") and r["Symbol"] != "1810"]
trades.sort(key=lambda x: x[0])

# shares held at each day's close
shares_by_day = {}
pos = defaultdict(float)
ti = 0
for d in days:
    while ti < len(trades) and trades[ti][0] <= d:
        pos[trades[ti][1]] += trades[ti][2]
        ti += 1
    shares_by_day[d] = {s: q for s, q in pos.items() if q > 0.001}

held = sorted({s for h in shares_by_day.values() for s in h})
closes = {}
for s in held:
    closes[s] = yahoo_closes(s, days[0], days[-1])
    time.sleep(0.25)
stock_mv_rebuilt = {d: sum(q * close_asof(closes[s], d) for s, q in shares_by_day[d].items()) for d in days}

# check 3: rebuilt stock value matches the Flex Stock column where both exist
for d in overlap:
    if abs(stock_mv_rebuilt[d] - nav_stock[d]) > 1:
        raise SystemExit(f"check 3 FAILED on {d}: rebuilt {stock_mv_rebuilt[d]:.2f} vs NAV.csv {nav_stock[d]:.2f}")

new_days = [d for d in days if d > max(nav_total)]
print(f"checks passed: {len(days)} days consistent, {len(overlap)} overlap days match NAV.csv (Total and Stock)")
print("new trades:", [(t["TradeDate"], t["Symbol"], t["Buy/Sell"], t["Quantity"]) for t in new_trades])
print(f"new NAV rows: {new_days[0]} .. {new_days[-1]} ({len(new_days)} days)")

# ---- write IB.csv ----
fieldnames = ["TradeDate", "Symbol", "Buy/Sell", "TradePrice", "Quantity", "TradeMoney", "Account"]
allrows = sorted(ib_rows + new_trades, key=lambda r: (r["TradeDate"], r["Account"]))
with open(rf"{DATA}\IB.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(allrows)

# ---- write NAV.csv: rows go at the end of the last year's NAV section, then refresh its TWR row ----
def quoted(values):
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="").writerow(values)
    return buf.getvalue()

blk = max(i for i, l in enumerate(lines) if next(csv.reader([l]))[0] == "Total")
nav_header = next(csv.reader([lines[blk]]))
twr_i = next(i for i in range(blk, len(lines)) if next(csv.reader([lines[i]]))[0] == "TWR")
new_lines = []
for d in new_days:
    v = cnav[days.index(d)]
    row = dict.fromkeys(nav_header, "")
    row.update({"Total": repr(v), "Stock": f"{stock_mv_rebuilt[d]:.2f}",
                "Cash": repr(v - stock_mv_rebuilt[d]), "ReportDate": d.strftime("%Y%m%d")})
    new_lines.append(quoted([row[h] for h in nav_header]))

twr_header = next(csv.reader([lines[twr_i]]))
twr_row = dict(zip(twr_header, next(csv.reader([lines[twr_i + 1]]))))
if twr_row["FromDate"][:4] != str(new_days[-1].year):
    raise SystemExit("last TWR row is not the current year; aborting")
last = new_days[-1]
twr_row.update({"TWR": repr(cps[days.index(last)] * 100), "EndingValue": repr(cnav[days.index(last)]),
                "ToDate": last.strftime("%Y%m%d")})
lines[twr_i + 1] = quoted([twr_row[h] for h in twr_header])
lines = lines[:twr_i] + new_lines + lines[twr_i:]
open(rf"{DATA}\NAV.csv", "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"written: IB.csv +{len(new_trades)} trades, NAV.csv +{len(new_lines)} rows, "
      f"{last.year} TWR -> {cps[days.index(last)] * 100:.4f}%")
