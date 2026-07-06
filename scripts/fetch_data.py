#!/usr/bin/env python3
"""
Commodities Price Monitor — Standalone Data Fetcher for GitHub Actions

Fetches global commodity futures prices (via yfinance) and Chinese domestic
futures prices (via Eastmoney API). Appends daily to history, builds snapshot.

Produces:
  data/snapshot.json — latest values + changes + alerts
  data/history.json  — full time series (appended daily)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("commodities-monitor")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
HISTORY_PATH = DATA_DIR / "history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 20
USER_AGENT = "CommoditiesMonitor/1.0 (GitHub Actions)"

# ── Global commodity futures (yfinance tickers) ──────────────────
# Format: { internal_id: (yfinance_ticker, name_cn, name_en, unit, category) }
GLOBAL_COMMODITIES = {
    "gold":        ("GC=F",  "黄金",   "Gold",       "USD/oz",   "precious"),
    "silver":      ("SI=F",  "白银",   "Silver",     "USD/oz",   "precious"),
    "platinum":    ("PL=F",  "铂金",   "Platinum",   "USD/oz",   "precious"),
    "palladium":   ("PA=F",  "钯金",   "Palladium",  "USD/oz",   "precious"),
    "copper":      ("HG=F",  "铜",     "Copper",     "USD/lb",   "industrial"),
    "aluminum":    ("ALI=F", "铝",     "Aluminum",   "USD/tonne","industrial"),
    # zinc/nickel/lead/tin: no reliable free global ticker on yfinance
    # Domestic prices still available via Sina API
    "wti":         ("CL=F",  "WTI原油","WTI Crude",  "USD/bbl",  "energy"),
    "brent":       ("BZ=F",  "布伦特",  "Brent Crude","USD/bbl",  "energy"),
    "natgas":      ("NG=F",  "天然气",  "Natural Gas","USD/MMBtu","energy"),
}

# ── Chinese domestic futures (Sina Finance API) ──────────────────
# Format: { internal_id: (sina_code, name_cn, unit, category, global_pair_id) }
# Sina API: https://hq.sinajs.cn/list=nf_{code}
# Field mapping: 0=name, 1=contract_id, 2=latest_price, 3=high, 4=low, 5=open, 6=prev_close
DOMESTIC_COMMODITIES = {
    "au":    ("AU0",   "沪金",   "元/克",   "precious",    "gold"),
    "ag":    ("AG0",   "沪银",   "元/千克", "precious",    "silver"),
    "cu":    ("CU0",   "沪铜",   "元/吨",  "industrial",  "copper"),
    "al":    ("AL0",   "沪铝",   "元/吨",  "industrial",  "aluminum"),
    "zn":    ("ZN0",   "沪锌",   "元/吨",  "industrial",  "zinc"),
    "ni":    ("NI0",   "沪镍",   "元/吨",  "industrial",  "nickel"),
    "pb":    ("PB0",   "沪铅",   "元/吨",  "industrial",  "lead"),
    "sn":    ("SN0",   "沪锡",   "元/吨",  "industrial",  "tin"),
    "rb":    ("RB0",   "螺纹钢", "元/吨",  "ferrous",     None),
    "hc":    ("HC0",   "热卷",   "元/吨",  "ferrous",     None),
    "i":     ("I0",    "铁矿石", "元/吨",  "ferrous",     None),
    "lc":    ("LC0",   "碳酸锂", "元/吨",  "new_energy",  None),
    "jm":    ("JM0",   "焦煤",   "元/吨",  "coal",        None),
    "j":     ("J0",    "焦炭",   "元/吨",  "coal",        None),
}

# ── Exchange rate for CNY conversion ─────────────────────────────
USDCNY_FALLBACK = 7.25


# ══════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════

def fetch_global_prices() -> dict:
    """Fetch global commodity futures prices via yfinance."""
    log.info("Fetching global commodity prices via yfinance…")
    results = {}

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — skipping global prices")
        return results

    ticker_ids = [info[0] for info in GLOBAL_COMMODITIES.values()]
    tickers = yf.Tickers(" ".join(ticker_ids))

    for cid, (ticker, name_cn, name_en, unit, cat) in GLOBAL_COMMODITIES.items():
        try:
            t = tickers.tickers.get(ticker)
            if t is None:
                log.warning(f"  ⚠ {ticker} ({name_cn}) ticker not found")
                continue
            hist = t.history(period="2d")
            if hist.empty:
                log.warning(f"  ⚠ {ticker} ({name_cn}) no data")
                continue
            price = float(hist["Close"].iloc[-1])
            results[cid] = round(price, 2)
            log.info(f"  ✓ {name_cn} ({ticker}): {price}")
        except Exception as e:
            log.warning(f"  ⚠ {ticker} ({name_cn}): {e}")

    log.info(f"  ✓ Global: {len(results)} commodities fetched")
    return results


def fetch_fx_rate() -> float:
    """Fetch USD/CNY exchange rate for domestic-to-USD conversion."""
    log.info("Fetching USD/CNY rate…")
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "CNY"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        rate = resp.json()["rates"]["CNY"]
        log.info(f"  ✓ USD/CNY: {rate}")
        return rate
    except Exception as e:
        log.warning(f"  ⚠ USD/CNY failed: {e}, using fallback {USDCNY_FALLBACK}")
        return USDCNY_FALLBACK


def fetch_domestic_prices() -> dict:
    """Fetch Chinese domestic futures prices via Sina Finance API."""
    log.info("Fetching Chinese domestic prices via Sina Finance API…")
    results = {}

    # Batch fetch all symbols in one request for efficiency
    sina_codes = [info[0] for info in DOMESTIC_COMMODITIES.values()]
    batch_url = "https://hq.sinajs.cn/list=" + ",".join(f"nf_{c}" for c in sina_codes)

    try:
        resp = requests.get(batch_url, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://finance.sina.com.cn/",
        })
        resp.encoding = "gbk"
        raw_text = resp.text
    except Exception as e:
        log.error(f"  ✗ Sina API batch request failed: {e}")
        return results

    # Parse each line: var hq_str_nf_XX0="name,code,price,high,low,open,prev_close,..."
    for cid, (sina_code, name_cn, unit, cat, pair_id) in DOMESTIC_COMMODITIES.items():
        try:
            prefix = f'var hq_str_nf_{sina_code}="'
            start = raw_text.find(prefix)
            if start == -1:
                log.warning(f"  ⚠ {name_cn} ({sina_code}) not found in response")
                continue
            start += len(prefix)
            end = raw_text.find('"', start)
            if end == -1:
                continue
            fields = raw_text[start:end].split(",")
            if len(fields) < 3:
                log.warning(f"  ⚠ {name_cn} ({sina_code}) insufficient fields")
                continue
            # field[2] = latest price
            price = float(fields[2])
            results[cid] = round(price, 2)
            log.info(f"  ✓ {name_cn} ({sina_code}): {price}")
        except Exception as e:
            log.warning(f"  ⚠ {name_cn} ({sina_code}): {e}")

    log.info(f"  ✓ Domestic: {len(results)} commodities fetched")
    return results


def fetch_dxy() -> Optional[float]:
    """Fetch US Dollar Index (DXY) via yfinance."""
    log.info("Fetching DXY…")
    try:
        import yfinance as yf
        t = yf.Ticker("DX-Y.NYB")
        hist = t.history(period="2d")
        if hist.empty:
            return None
        price = float(hist["Close"].iloc[-1])
        log.info(f"  ✓ DXY: {price}")
        return round(price, 2)
    except Exception as e:
        log.warning(f"  ⚠ DXY: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# HISTORY MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def load_history() -> dict:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "commodities": {},
        "indicators": {},
        "meta": {"created": datetime.now().isoformat()},
    }


def save_history(history: dict):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def save_snapshot(snapshot: dict):
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)


def compute_changes(history: dict, today: str) -> dict:
    """Compute 1d, 7d, 1m changes from history."""
    dates = sorted(history.get("commodities", {}).keys())
    changes = {"change_1d": {}, "change_7d": {}, "change_1m": {}}

    if today not in history.get("commodities", {}):
        return changes

    today_data = history["commodities"][today]

    def pct_change(today_val, prev_val):
        if today_val is None or prev_val is None or prev_val == 0:
            return None
        return round((today_val - prev_val) / prev_val * 100, 2)

    def abs_change(today_val, prev_val):
        if today_val is None or prev_val is None:
            return None
        return round(today_val - prev_val, 2)

    def compute_period(period_days, change_key):
        if len(dates) < 2:
            return
        idx = dates.index(today)
        target_idx = None
        target_date = None
        for d in reversed(dates[:idx]):
            if d <= (date.fromisoformat(today) - timedelta(days=period_days)).isoformat():
                target_date = d
                break
        if target_date:
            target_date = target_date
        else:
            # fallback: use earliest available date within range
            cutoff = date.fromisoformat(today) - timedelta(days=period_days)
            for d in reversed(dates[:idx]):
                if d >= cutoff.isoformat():
                    target_date = d
                    break
        if not target_date:
            return
        prev_data = history["commodities"][target_date]
        # compute for global and domestic
        for source in ["global", "domestic"]:
            changes[change_key][source] = {}
            today_src = today_data.get(source, {})
            prev_src = prev_data.get(source, {})
            for cid in today_src:
                if cid in prev_src:
                    changes[change_key][source][cid] = pct_change(
                        today_src[cid], prev_src[cid]
                    )

    compute_period(1, "change_1d")
    compute_period(7, "change_7d")
    compute_period(30, "change_1m")

    return changes


def build_snapshot(history: dict, fx_rate: float) -> dict:
    """Build snapshot from history."""
    commodities = history.get("commodities", {})
    dates = sorted(commodities.keys())

    snapshot = {
        "commodities": {
            "date": dates[-1] if dates else date.today().isoformat(),
            "global": {},
            "domestic": {},
            "spread": {},
            "change_1d": {},
            "change_7d": {},
            "change_1m": {},
        },
        "indicators": {},
        "alerts": [],
        "meta": {
            "updated": datetime.now().isoformat(),
            "source": "Commodities Monitor (GH Actions)",
        },
    }

    if not dates:
        return snapshot

    today = dates[-1]
    today_data = commodities[today]

    # Latest values
    snapshot["commodities"]["global"] = today_data.get("global", {})
    snapshot["commodities"]["domestic"] = today_data.get("domestic", {})

    # Changes
    changes = compute_changes(history, today)
    snapshot["commodities"]["change_1d"] = changes.get("change_1d", {})
    snapshot["commodities"]["change_7d"] = changes.get("change_7d", {})
    snapshot["commodities"]["change_1m"] = changes.get("change_1m", {})

    # Compute spreads (domestic vs global equivalent)
    # For metals with both global and domestic: convert global to CNY equivalent
    spreads = {}
    for cid, (sina_code, name_cn, unit, cat, pair_id) in DOMESTIC_COMMODITIES.items():
        if pair_id and pair_id in today_data.get("global", {}) and cid in today_data.get("domestic", {}):
            global_price = today_data["global"][pair_id]
            domestic_price = today_data["domestic"][cid]

            # Convert global to CNY-equivalent per domestic unit
            # Gold: global is USD/oz, domestic is CNY/g. 1 oz = 31.1035g
            if pair_id == "gold":
                global_cny_equiv = global_price * fx_rate / 31.1035
                spread_pct = round((domestic_price - global_cny_equiv) / global_cny_equiv * 100, 2)
                spreads[cid] = {
                    "global_cny_equiv": round(global_cny_equiv, 2),
                    "domestic": domestic_price,
                    "spread_pct": spread_pct,
                    "unit": "CNY/g",
                }
            # Silver: global is USD/oz, domestic is CNY/kg. 1 oz = 0.0311035 kg
            elif pair_id == "silver":
                global_cny_equiv = global_price * fx_rate / 0.0311035
                spread_pct = round((domestic_price - global_cny_equiv) / global_cny_equiv * 100, 2)
                spreads[cid] = {
                    "global_cny_equiv": round(global_cny_equiv, 2),
                    "domestic": domestic_price,
                    "spread_pct": spread_pct,
                    "unit": "CNY/kg",
                }
            # Industrial metals: global USD/tonne, domestic CNY/tonne
            # But copper global is USD/lb — need conversion. 1 lb = 0.000453592 tonne
            elif pair_id == "copper":
                global_cny_equiv = global_price * fx_rate / 0.000453592
                spread_pct = round((domestic_price - global_cny_equiv) / global_cny_equiv * 100, 2)
                spreads[cid] = {
                    "global_cny_equiv": round(global_cny_equiv, 0),
                    "domestic": domestic_price,
                    "spread_pct": spread_pct,
                    "unit": "CNY/tonne",
                }
            else:
                global_cny_equiv = global_price * fx_rate
                spread_pct = round((domestic_price - global_cny_equiv) / global_cny_equiv * 100, 2)
                spreads[cid] = {
                    "global_cny_equiv": round(global_cny_equiv, 2),
                    "domestic": domestic_price,
                    "spread_pct": spread_pct,
                    "unit": "CNY/tonne",
                }

    snapshot["commodities"]["spread"] = spreads

    # Indicators
    indicators = history.get("indicators", {})
    if today in indicators:
        snapshot["indicators"] = indicators[today]
    # Compute derived indicators
    if "gold" in today_data.get("global", {}) and "silver" in today_data.get("global", {}):
        gold = today_data["global"]["gold"]
        silver = today_data["global"]["silver"]
        snapshot["indicators"]["gold_silver_ratio"] = round(gold / silver, 2)
    if "gold" in today_data.get("global", {}) and "copper" in today_data.get("global", {}):
        gold = today_data["global"]["gold"]
        copper = today_data["global"]["copper"]
        snapshot["indicators"]["gold_copper_ratio"] = round(gold / copper, 1)

    # Add fx_rate to indicators
    snapshot["indicators"]["usdcny"] = fx_rate

    # Alerts
    alerts = []
    ch1d = snapshot["commodities"]["change_1d"]
    for source in ["global", "domestic"]:
        for cid, change in ch1d.get(source, {}).items():
            if change is not None:
                if abs(change) >= 3.0:
                    all_info = {**GLOBAL_COMMODITIES, **DOMESTIC_COMMODITIES}
                    name = all_info.get(cid, (None, cid))[1] if cid in all_info else cid
                    location = "国际" if source == "global" else "国内"
                    alerts.append({
                        "type": "warning",
                        "message": f"{name}({location}) 单日变动 {change:+.2f}%",
                    })
                elif abs(change) >= 1.5:
                    all_info = {**GLOBAL_COMMODITIES, **DOMESTIC_COMMODITIES}
                    name = all_info.get(cid, (None, cid))[1] if cid in all_info else cid
                    location = "国际" if source == "global" else "国内"
                    alerts.append({
                        "type": "info",
                        "message": f"{name}({location}) 单日变动 {change:+.2f}%",
                    })
    snapshot["alerts"] = alerts

    return snapshot


def daily_update():
    log.info("=" * 60)
    log.info("COMMODITIES MONITOR — Daily Update")
    log.info("=" * 60)

    history = load_history()
    today_str = date.today().isoformat()

    # Skip if already fetched today (idempotent)
    # Comment out for GH Actions — we want fresh price even on same day
    # if today_str in history.get("commodities", {}):
    #     log.info(f"  Data already exists for {today_str}, skipping fetch")
    #     return

    # Fetch global prices
    global_prices = {}
    try:
        global_prices = fetch_global_prices()
    except Exception as e:
        log.error(f"  ✗ Global prices failed: {e}")

    # Fetch FX rate
    fx_rate = USDCNY_FALLBACK
    try:
        fx_rate = fetch_fx_rate()
    except Exception:
        pass

    # Fetch domestic prices
    domestic_prices = {}
    try:
        domestic_prices = fetch_domestic_prices()
    except Exception as e:
        log.error(f"  ✗ Domestic prices failed: {e}")

    # Fetch DXY
    dxy = None
    try:
        dxy = fetch_dxy()
    except Exception:
        pass

    # Append/merge to history
    commodities = history.setdefault("commodities", {})
    today_entry = commodities.setdefault(today_str, {})
    today_entry.setdefault("global", {}).update(global_prices)
    today_entry.setdefault("domestic", {}).update(domestic_prices)

    # Indicators
    indicators = history.setdefault("indicators", {})
    indicators[today_str] = {}
    if dxy is not None:
        indicators[today_str]["dxy"] = dxy

    save_history(history)
    snapshot = build_snapshot(history, fx_rate)
    save_snapshot(snapshot)

    # Summary
    global_count = len(global_prices)
    domestic_count = len(domestic_prices)
    alert_count = len(snapshot.get("alerts", []))
    history_days = len(commodities)
    log.info(
        f"SUMMARY — Global: {global_count}, Domestic: {domestic_count}, "
        f"History: {history_days}d, Alerts: {alert_count}"
    )
    return snapshot


if __name__ == "__main__":
    if "--init" in sys.argv:
        # Backfill 12 months of history
        log.info("=" * 60)
        log.info("INIT — Backfilling 12 months of history")
        log.info("=" * 60)

        import yfinance as yf
        from datetime import datetime as dt

        history = {"commodities": {}, "indicators": {}, "meta": {"created": datetime.now().isoformat()}}
        commodities = history["commodities"]

        # ── Global: yfinance 1y history ──
        log.info("Fetching 12-month global history via yfinance…")
        for cid, (ticker_str, name_cn, name_en, unit, cat) in GLOBAL_COMMODITIES.items():
            try:
                t = yf.Ticker(ticker_str)
                hist = t.history(period="1y")
                if hist.empty:
                    log.warning(f"  ⚠ {name_cn} ({ticker_str}): no history")
                    continue
                count = 0
                for idx, row in hist.iterrows():
                    d = idx.strftime("%Y-%m-%d") if hasattr(idx, 'strftime') else str(idx)[:10]
                    price = float(row["Close"])
                    commodities.setdefault(d, {}).setdefault("global", {})[cid] = round(price, 2)
                    count += 1
                log.info(f"  ✓ {name_cn}: {count} days")
            except Exception as e:
                log.warning(f"  ⚠ {name_cn} ({ticker_str}): {e}")

        # ── Domestic: akshare 1y history ──
        log.info("Fetching 12-month domestic history via akshare…")
        try:
            import akshare as ak
            start_d = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
            for cid, (sina_code, name_cn, unit, cat, pair_id) in DOMESTIC_COMMODITIES.items():
                try:
                    df = ak.futures_main_sina(symbol=sina_code, start_date=start_d)
                    if df is None or df.empty:
                        log.warning(f"  ⚠ {name_cn} ({sina_code}): no history from akshare")
                        continue
                    count = 0
                    for _, row in df.iterrows():
                        d = str(row["日期"])[:10]
                        # Use 收盘价 as the price
                        price = float(row["收盘价"])
                        commodities.setdefault(d, {}).setdefault("domestic", {})[cid] = round(price, 2)
                        count += 1
                    log.info(f"  ✓ {name_cn}: {count} days")
                except Exception as e:
                    log.warning(f"  ⚠ {name_cn} ({sina_code}): {e}")
        except ImportError:
            log.warning("akshare not installed — skipping domestic history")

        # ── DXY history ──
        log.info("Fetching DXY history…")
        try:
            t = yf.Ticker("DX-Y.NYB")
            hist = t.history(period="1y")
            indicators = history.setdefault("indicators", {})
            for idx, row in hist.iterrows():
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, 'strftime') else str(idx)[:10]
                indicators.setdefault(d, {})["dxy"] = round(float(row["Close"]), 2)
            log.info(f"  ✓ DXY: {len(hist)} days")
        except Exception as e:
            log.warning(f"  ⚠ DXY history: {e}")

        # Sort dates
        sorted_dates = sorted(commodities.keys())
        history["commodities"] = {d: commodities[d] for d in sorted_dates}

        save_history(history)
        fx_rate = USDCNY_FALLBACK
        try:
            fx_rate = fetch_fx_rate()
        except Exception:
            pass
        snapshot = build_snapshot(history, fx_rate)
        save_snapshot(snapshot)

        total_days = len(sorted_dates)
        log.info(f"INIT COMPLETE — {total_days} days of history")
        log.info("Running daily update to fill today's data…")
        daily_update()
    else:
        daily_update()
