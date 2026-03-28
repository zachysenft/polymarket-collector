import logging
import time
from datetime import datetime, timezone

import requests

from db import upsert_deribit_options, upsert_deribit_surface

log = logging.getLogger(__name__)

BASE_URL = "https://www.deribit.com/api/v2/public"
CURRENCIES = ["BTC", "ETH"]


def _parse_instrument_name(name):
    """Extract expiry_ts, strike, option_type from e.g. 'BTC-28MAR25-90000-C'."""
    try:
        parts = name.split("-")
        # parts: [currency, expiry_str, strike_str, type_char]
        expiry_str = parts[1]   # e.g. '28MAR25'
        strike = float(parts[2])
        option_type = "call" if parts[3].upper() == "C" else "put"
        expiry_ts = datetime.strptime(expiry_str, "%d%b%y").replace(
            hour=8, minute=0, second=0, tzinfo=timezone.utc
        )
        return expiry_ts, strike, option_type
    except Exception:
        return None, None, None


def _fetch_book_summary(currency):
    """Fetch all active options for a currency in one call."""
    r = requests.get(
        f"{BASE_URL}/get_book_summary_by_currency",
        params={"currency": currency, "kind": "option"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("result") is None:
        raise ValueError(f"Unexpected Deribit response: {data}")
    return data["result"]


def _compute_surface(options_rows, ts):
    """Aggregate per-expiry surface metrics from raw instrument rows."""
    from collections import defaultdict

    by_expiry = defaultdict(list)
    for row in options_rows:
        if row["expiry_ts"] is not None:
            by_expiry[row["expiry_ts"]].append(row)

    surface_rows = []
    now = datetime.now(timezone.utc)

    for expiry_ts, instruments in by_expiry.items():
        if not instruments:
            continue

        days_to_exp = (expiry_ts - now).total_seconds() / 86400
        if days_to_exp < 0:
            continue

        # Underlying price from first instrument (same for all same expiry)
        underlying = next(
            (i["underlying_price"] for i in instruments if i["underlying_price"]), None
        )

        # ATM IV: instrument with strike closest to spot
        atm_iv = None
        if underlying:
            nearest = min(
                instruments,
                key=lambda i: abs(i["strike"] - underlying) if i["strike"] else float("inf"),
            )
            atm_iv = nearest.get("mark_iv")

        # 25-delta skew: avg put IV - avg call IV for instruments near 25 delta
        puts_25d = [
            i["mark_iv"] for i in instruments
            if i["option_type"] == "put"
            and i.get("delta") is not None
            and 0.2 <= abs(i["delta"]) <= 0.3
            and i.get("mark_iv")
        ]
        calls_25d = [
            i["mark_iv"] for i in instruments
            if i["option_type"] == "call"
            and i.get("delta") is not None
            and 0.2 <= abs(i["delta"]) <= 0.3
            and i.get("mark_iv")
        ]
        skew_25d = None
        if puts_25d and calls_25d:
            skew_25d = round(
                sum(puts_25d) / len(puts_25d) - sum(calls_25d) / len(calls_25d), 4
            )

        # Put/call OI ratio
        put_oi = sum(i["open_interest"] or 0 for i in instruments if i["option_type"] == "put")
        call_oi = sum(i["open_interest"] or 0 for i in instruments if i["option_type"] == "call")
        pc_oi_ratio = round(put_oi / call_oi, 4) if call_oi > 0 else None

        total_oi = put_oi + call_oi
        total_vol = sum(i["volume"] or 0 for i in instruments)

        surface_rows.append({
            "ts": ts,
            "currency": instruments[0]["currency"],
            "expiry_ts": expiry_ts,
            "days_to_exp": round(days_to_exp, 2),
            "atm_iv": atm_iv,
            "skew_25d": skew_25d,
            "pc_oi_ratio": pc_oi_ratio,
            "total_oi": round(total_oi, 2),
            "total_volume": round(total_vol, 2),
        })

    return surface_rows


def collect_deribit_options():
    """Fetch BTC and ETH options chains, store raw + surface aggregates."""
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    total_instruments = 0
    total_surface = 0

    for currency in CURRENCIES:
        try:
            results = _fetch_book_summary(currency)
            options_rows = []
            for item in results:
                name = item.get("instrument_name", "")
                expiry_ts, strike, option_type = _parse_instrument_name(name)
                if expiry_ts is None:
                    continue
                options_rows.append({
                    "ts": ts,
                    "currency": currency,
                    "instrument_name": name,
                    "expiry_ts": expiry_ts,
                    "strike": strike,
                    "option_type": option_type,
                    "mark_iv": item.get("mark_iv"),
                    "bid_iv": item.get("bid_iv"),
                    "ask_iv": item.get("ask_iv"),
                    "delta": item.get("delta"),
                    "gamma": item.get("gamma"),
                    "vega": item.get("vega"),
                    "theta": item.get("theta"),
                    "open_interest": item.get("open_interest"),
                    "volume": item.get("volume"),
                    "mark_price": item.get("mark_price"),
                    "underlying_price": item.get("underlying_price"),
                })

            upsert_deribit_options(options_rows)
            total_instruments += len(options_rows)

            surface_rows = _compute_surface(options_rows, ts)
            upsert_deribit_surface(surface_rows)
            total_surface += len(surface_rows)

            log.info(
                f"Deribit {currency}: {len(options_rows)} instruments, "
                f"{len(surface_rows)} expiries stored"
            )
        except Exception as e:
            log.error(f"Deribit {currency} collection failed: {e}")

        time.sleep(0.5)

    log.info(
        f"Deribit collection complete — {total_instruments} instruments, "
        f"{total_surface} surface rows"
    )
