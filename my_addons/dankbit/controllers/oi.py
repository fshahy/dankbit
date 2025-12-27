# -*- coding: utf-8 -*-
import requests
import logging
import time

_logger = logging.getLogger(__name__)

DERIBIT_URL = "https://www.deribit.com/api/v2"

# ------------------------------------------------------------
# persistent session (Deribit-friendly)
# ------------------------------------------------------------
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Dankbit/0.2 (research; non-commercial)",
    "Accept": "application/json",
})


# ------------------------------------------------------------
# simple in-process cache (OI is slow-moving)
# ------------------------------------------------------------
_OI_CACHE = {}
_OI_CACHE_TS = {}
_OI_CACHE_TTL = 300  # seconds


# ------------------------------------------------------------
# low-level helper
# ------------------------------------------------------------
def _deribit_get(path, params=None, timeout=5):
    r = _SESSION.get(
        DERIBIT_URL + path,
        params=params or {},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


# ------------------------------------------------------------
# public API (authoritative OI snapshot)
# ------------------------------------------------------------
def get_oi_snapshot(instrument):
    """
    Returns authoritative Deribit open interest snapshot.

    Output:
    [
        {
            "instrument_name": str,
            "strike": float,
            "option_type": "call" | "put",
            "open_interest": float,
        }
    ]
    """

    now = time.time()

    # cache key per currency
    currency = "BTC" if instrument.startswith("BTC") else "ETH"

    # --- cache hit ---
    ts = _OI_CACHE_TS.get(currency, 0)
    if now - ts < _OI_CACHE_TTL:
        return _OI_CACHE.get(currency, [])

    # --------------------------------------------------------
    # SINGLE Deribit request (safe + fast)
    # --------------------------------------------------------
    summaries = _deribit_get(
        "/public/get_book_summary_by_currency",
        {
            "currency": currency,
            "kind": "option",
        },
    )

    results = []

    for s in summaries:
        oi = float(s.get("open_interest", 0.0))
        if oi <= 0:
            continue

        # instrument_name example:
        # BTC-29DEC25-90000-C
        try:
            parts = s["instrument_name"].split("-")
            strike = float(parts[-2])
            option_type = "call" if parts[-1] == "C" else "put"
        except Exception:
            continue

        results.append({
            "instrument_name": s["instrument_name"],
            "strike": strike,
            "option_type": option_type,
            "open_interest": oi,
        })

    # --- store cache ---
    _OI_CACHE[currency] = results
    _OI_CACHE_TS[currency] = now

    _logger.info(
        "Deribit OI snapshot loaded: %s instruments (%s)",
        len(results),
        currency,
    )

    return results
