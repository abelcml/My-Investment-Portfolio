r"""Fetch all price history needed by compute_twr.py / compute_tw.py from Yahoo.

Writes Data\prices_tw.json (TW tickers, adjusted closes + split events),
Data\prices_us.json (FT-era US tickers), Data\ndx.json, Data\fx.json (JPY/HKD).
Run before the compute scripts whenever new trades/dates were added.

TW fallback when Yahoo has no data (e.g. Yahoo drops delisted tickers entirely):
TWSE official daily closes over the ticker's trading months (unadjusted prices;
compute_tw.py's trade-price/close ratio keeps share counts consistent), then the
previous prices_tw.json. A ticker missing from all three is reported as failed.
"""
import csv
import json
import os
import ssl
import time
import urllib.request

DATA = r"C:\Users\Tr7\Trades\Data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# TWSE's certificate lacks a Subject Key Identifier, which Python 3.13+ rejects under
# VERIFY_X509_STRICT; chain and hostname verification stay on.
TWSE_SSL = ssl.create_default_context()
TWSE_SSL.verify_flags &= ~ssl.VERIFY_X509_STRICT
P_START = 1546300800  # 2019-01-01
NOW = int(time.time()) + 86400


def fetch(sym, p1=P_START):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?period1={p1}&period2={NOW}&interval=1d&events=splits")
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
    res = d["chart"]["result"][0]
    closes = {}
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            closes[time.strftime("%Y-%m-%d", time.localtime(ts))] = round(c, 4)
    splits = {}
    for ev in (res.get("events", {}).get("splits", {}) or {}).values():
        splits[time.strftime("%Y-%m-%d", time.localtime(ev["date"]))] = ev["numerator"] / ev["denominator"]
    return {"close": closes, "splits": splits}


def twse_closes(ticker, first_month, last_month):
    # months as (year, month); TWSE dates are ROC years, e.g. "110/05/13"
    closes = {}
    y, m = first_month
    while (y, m) <= last_month:
        url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json"
               f"&date={y}{m:02d}01&stockNo={ticker}")
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                                 timeout=30, context=TWSE_SSL))
        except Exception as e:
            print(f"  TWSE {ticker} {y}{m:02d}: {type(e).__name__}")
            d = {}
        for row in d.get("data", []):
            ry, rm, rd = row[0].split("/")
            try:
                closes[f"{int(ry) + 1911}-{rm}-{rd}"] = float(row[6].replace(",", ""))
            except ValueError:
                pass   # "--" on days without a trade
        time.sleep(1.0)   # TWSE rate-limits aggressive clients
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return closes


def main():
    # --- TW tickers from the canonical trade file, with their first/last trading month ---
    tw = set()
    months = {}
    for r in csv.DictReader(open(rf"{DATA}\TW.csv", encoding="utf-8-sig")):
        t = r["Ticker"].strip()
        if t.isdigit() and len(t) < 4:
            t = t.zfill(4)
        tw.add(t)
        ym = (int(r["TradeDate"][:4]), int(r["TradeDate"][4:6]))
        lo, hi = months.get(t, (ym, ym))
        months[t] = (min(lo, ym), max(hi, ym))
    prev_path = rf"{DATA}\prices_tw.json"
    prev_tw = json.load(open(prev_path)) if os.path.exists(prev_path) else {}
    out, failed, from_twse, kept_from_cache = {}, [], [], []
    for t in sorted(tw):
        got = None
        for suffix in (".TW", ".TWO"):
            try:
                g = fetch(t + suffix)
                if g["close"]:
                    got = g
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if got is None:
            c = twse_closes(t, *months[t])
            if c:
                got = {"close": c, "splits": {}}
                from_twse.append(t)
        if got is None and t in prev_tw:
            got = prev_tw[t]
            kept_from_cache.append(t)
        if got is None:
            failed.append(t)
        else:
            out[t] = got
        time.sleep(0.25)
    json.dump(out, open(rf"{DATA}\prices_tw.json", "w"))
    print(f"TW: {len(out)} tickers ready | from TWSE: {from_twse} | "
          f"WARNING kept from old cache: {kept_from_cache} | failed: {failed}")

    # --- FT-era US tickers (fixed list; UST/option are cost-carried in compute) ---
    us = ["TMF", "TLT", "TTT", "SQQQ", "SDOW", "QID", "SPXS", "IWO", "TQQQ",
          "WMT", "NVDA", "AMD", "MU", "VIXY", "UVIX"]
    outu = {}
    for t in us:
        outu[t] = fetch(t, p1=1651000000)  # 2022-04-26
        time.sleep(0.25)
    json.dump(outu, open(rf"{DATA}\prices_us.json", "w"))
    print(f"US: {len(outu)} tickers fetched")

    # --- NDX + FX ---
    json.dump(fetch("%5ENDX", p1=1650844800), open(rf"{DATA}\ndx.json", "w"))
    fx = {"jpy": fetch("JPY%3DX", p1=1656000000), "hkd": fetch("HKD%3DX", p1=1656000000)}
    json.dump(fx, open(rf"{DATA}\fx.json", "w"))
    print("NDX + FX fetched")


if __name__ == "__main__":
    main()
