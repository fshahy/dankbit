import asyncio
import json
import os
import websockets
import logging
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone, timedelta
import time

last_request_ts = 0
REQUEST_INTERVAL = 0.15  # ~7 requests / second


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger("ws")

WS_URL = "wss://www.deribit.com/ws/api/v2/"


# -----------------------------------------------------
# PostgreSQL connection (global)
# -----------------------------------------------------
PG_CONN = psycopg2.connect(
    dbname=os.getenv("POSTGRES_DB"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST", "db"),
    port=os.getenv("POSTGRES_PORT", "5432")
)
PG_CONN.autocommit = True
print("WS connecting to DB:", PG_CONN.dsn, flush=True)


def extract_option_type(instrument_name):
    if instrument_name.endswith("-C"):
        return "call"
    if instrument_name.endswith("-P"):
        return "put"
    return None  # should not happen for options

def extract_expiration(instrument_name):
    """
    Convert Deribit expiry code (e.g. '27FEB26') into UTC datetime.
    """
    try:
        exp_str = instrument_name.split("-")[1]
        exp_date = datetime.strptime(exp_str, "%d%b%y")
        # Deribit expiries are always 08:00 UTC on expiry day
        return exp_date.replace(hour=8, minute=0, second=0, tzinfo=timezone.utc)
    except Exception:
        return None


# -----------------------------------------------------
# DB Insert (single-row, no batching)
# -----------------------------------------------------
def insert_trade(t):
    sql = """
        INSERT INTO dankbit_trade
        (
            name, strike, active, deribit_trade_identifier, amount, price, direction,
            option_type, index_price, iv, block_trade_id, is_block_trade, 
            expiration, deribit_ts,
            create_uid, create_date, write_uid, write_date
        )
        VALUES
        (
            %s,%s,True,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TO_TIMESTAMP(%s/1000.0),
            1, NOW(), 1, NOW()
        )
        ON CONFLICT (deribit_trade_identifier) DO NOTHING;
    """

    values = (
        t.get("instrument_name"),
        t.get("instrument_name").split("-")[2] if t.get("instrument_name") else 0,  # strike
        t.get("trade_id"),
        t.get("amount"),
        t.get("price"),
        t.get("direction"),
        extract_option_type(t.get("instrument_name")),
        t.get("index_price"),
        t.get("iv"),
        #t.get("amount"),                  # contracts = amount
        t.get("block_trade_id"),      # block_trade_id
        bool(t.get("block_trade_id")),  # is_block_trade
        extract_expiration(t.get("instrument_name")),   # <-- NEW FIELD
        t.get("timestamp"),               # ms → converted in SQL
    )

    try:
        with PG_CONN.cursor() as cur:
            cur.execute(sql, values)
    except Exception as e:
        log.error(f"DB insert error: {e}")
        PG_CONN.rollback()


async def backfill_instrument(ws, instrument):
    log.info(f"Backfilling: {instrument}")

    end_id = None
    page = 0

    while True:
        params = {
            "instrument_name": instrument,
            "count": 1000,
            "include_old": True,
            "sorting": "desc",
        }

        if end_id:
            params["end_seq"] = end_id  # continue before last batch

        resp = await ws_call(ws, "public/get_last_trades_by_instrument", params)

        trades = resp["result"]["trades"]
        if not trades:
            break

        # Insert trades
        for t in trades:
            insert_trade(t)

        # Prepare next pagination step
        end_id = trades[-1]["trade_id"]  # smallest ID from this batch
        page += 1

        log.info(f"{instrument}: page {page}, got {len(trades)} trades")

        if len(trades) < 1000:
            break  # finished

        # gentle pacing
        await asyncio.sleep(0.05)

    log.info(f"Backfill finished for {instrument}")



# -----------------------------------------------------
# WebSocket helper
# -----------------------------------------------------
async def ws_call(ws, method, params=None):
    global last_request_ts
    # rate limit
    now = time.time()
    if now - last_request_ts < REQUEST_INTERVAL:
        await asyncio.sleep(REQUEST_INTERVAL - (now - last_request_ts))
    last_request_ts = time.time()

    req = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params or {}
    }
    await ws.send(json.dumps(req))
    resp = json.loads(await ws.recv())

    # handle Deribit over_limit error gracefully
    if "error" in resp and resp["error"]["message"] == "over_limit":
        log.warning("Rate limit hit. Sleeping 0.5 seconds…")
        await asyncio.sleep(0.5)
        return await ws_call(ws, method, params)

    return resp



# -----------------------------------------------------
# Authentication
# -----------------------------------------------------
async def authenticate(ws):
    params = {
        "grant_type": "client_credentials",
        "client_id": os.getenv("DERIBIT_KEY"),
        "client_secret": os.getenv("DERIBIT_SECRET"),
    }
    resp = await ws_call(ws, "public/auth", params)
    if "error" in resp:
        raise Exception(f"Auth failed: {resp}")
    log.info("Authenticated.")
    return True


# -----------------------------------------------------
# Fetch BTC + ETH option instruments
# -----------------------------------------------------
async def fetch_instruments(ws):
    channels = []
    for currency in ["BTC"]:
        resp = await ws_call(ws, "public/get_instruments", {
            "currency": currency,
            "kind": "option",
            "expired": False,
        })
        for inst in resp["result"]:
            instrument = inst["instrument_name"]
            channels.append(f"trades.{instrument}.raw")
    return channels


# -----------------------------------------------------
# Subscribe
# -----------------------------------------------------
async def subscribe(ws, channels):
    msg = {
        "jsonrpc": "2.0",
        "id": 777,
        "method": "public/subscribe",
        "params": {"channels": channels[:500]},  # Deribit limit
    }
    await ws.send(json.dumps(msg))
    resp = json.loads(await ws.recv())
    log.info(f"Subscribed to {len(channels[:500])} raw channels.")
    return resp


# -----------------------------------------------------
# Main loop
# -----------------------------------------------------
async def run():
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:

                await authenticate(ws)

                log.info("Fetching instrument list…")
                channels = await fetch_instruments(ws)
                log.info(f"Found {len(channels)} option instruments.")

                # -------------------------------
                # BACKFILL ALL NON-EXPIRED INSTRUMENTS
                # -------------------------------
                log.info("Starting full backfill…")
                instruments = [c.split(".")[1] for c in channels]  # extract instrument names
                for inst in instruments:
                    await backfill_instrument(ws, inst)
                    await asyncio.sleep(1)  # pacing between instruments

                log.info("Backfill completed. Now subscribing for live trades…")

                # -------------------------------
                # LIVE STREAM
                # -------------------------------
                await subscribe(ws, channels)

                log.info("Listening for raw option trades…")

                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)

                    if "params" not in msg or "data" not in msg["params"]:
                        continue

                    trades = msg["params"]["data"]
                    if isinstance(trades, dict):
                        trades = [trades]

                    for t in trades:
                        log.info(
                            f"{t['instrument_name']} | {t['direction']} | "
                            f"price {t['price']} | amount {t['amount']}"
                        )
                        insert_trade(t)

        except Exception as e:
            log.error(f"WS error: {e}")
            log.info("Reconnecting in 3 seconds…")
            await asyncio.sleep(3)


if __name__ == "__main__":
    log.info("Starting dankbit raw option trade listener…")
    asyncio.run(run())
