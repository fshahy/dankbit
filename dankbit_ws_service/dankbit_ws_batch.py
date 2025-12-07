import asyncio
import json
import os
import websockets
import logging
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone
import time

# -----------------------------------------------------
# Config
# -----------------------------------------------------
WS_URL = "wss://www.deribit.com/ws/api/v2/"

last_request_ts = 0
REQUEST_INTERVAL = 0.15  # ~7 requests / second
SUB_CHUNK_SIZE = 400     # subscribe in chunks < 500 to respect Deribit limits

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger("ws")

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


# -----------------------------------------------------
# Helpers for instruments
# -----------------------------------------------------
def extract_option_type(instrument_name):
    if instrument_name.endswith("-C"):
        return "call"
    if instrument_name.endswith("-P"):
        return "put"
    return None  # should not happen for options


def extract_expiration(instrument_name):
    """
    Convert Deribit expiry code (e.g. '27FEB26') into UTC datetime.
    Deribit expiries are at 08:00 UTC on expiry day.
    """
    try:
        exp_str = instrument_name.split("-")[1]
        exp_date = datetime.strptime(exp_str, "%d%b%y")
        return exp_date.replace(hour=8, minute=0, second=0, tzinfo=timezone.utc)
    except Exception:
        return None


# -----------------------------------------------------
# DB Insert (single-row, ON CONFLICT ignore)
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

    instr_name = t.get("instrument_name")

    values = (
        instr_name,
        instr_name.split("-")[2] if instr_name else 0,  # strike
        t.get("trade_id"),
        t.get("amount"),
        t.get("price"),
        t.get("direction"),
        extract_option_type(instr_name),
        t.get("index_price"),
        t.get("iv"),
        t.get("block_trade_id"),        # block_trade_id
        bool(t.get("block_trade_id")),  # is_block_trade
        extract_expiration(instr_name),
        t.get("timestamp"),             # ms → converted in SQL
    )

    try:
        with PG_CONN.cursor() as cur:
            cur.execute(sql, values)
    except Exception as e:
        log.error(f"DB insert error: {e}")
        PG_CONN.rollback()


# -----------------------------------------------------
# WebSocket helper
# -----------------------------------------------------
async def ws_call(ws, method, params=None):
    """
    Simple RPC helper with basic rate limiting and retry on 'over_limit'.
    """
    global last_request_ts
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

    if "error" in resp:
        err = resp["error"]
        if err.get("message") == "over_limit":
            log.warning("Rate limit hit. Sleeping 0.5 seconds…")
            await asyncio.sleep(0.5)
            return await ws_call(ws, method, params)
        else:
            log.error(f"Error from Deribit for {method}: {err}")

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
    for currency in ["BTC", "ETH"]:
        resp = await ws_call(ws, "public/get_instruments", {
            "currency": currency,
            "kind": "option",
            "expired": False,
        })
        result = resp.get("result", [])
        if not isinstance(result, list):
            log.error(f"Unexpected result for {currency} instruments: {resp}")
            continue

        count = 0
        for inst in result:
            instrument = inst["instrument_name"]
            channels.append(f"trades.{instrument}.raw")
            count += 1

        log.info(f"{currency} instruments: {count}")

    # Quick summary
    btc_count = sum(1 for c in channels if c.startswith("trades.BTC"))
    eth_count = sum(1 for c in channels if c.startswith("trades.ETH"))
    log.info(f"Total channels: {len(channels)} (BTC: {btc_count}, ETH: {eth_count})")

    return channels


# -----------------------------------------------------
# Subscribe in chunks (important for ETH!)
# -----------------------------------------------------
async def subscribe_all(ws, channels, chunk_size=SUB_CHUNK_SIZE):
    total = len(channels)
    if total == 0:
        log.warning("No channels to subscribe to.")
        return

    for i in range(0, total, chunk_size):
        part = channels[i:i + chunk_size]
        log.info(f"Subscribing to channels {i+1}–{i+len(part)} of {total}…")
        resp = await ws_call(ws, "public/subscribe", {"channels": part})

        if "error" in resp:
            log.error(f"Subscribe error for chunk {i//chunk_size}: {resp['error']}")
        else:
            log.info(f"Subscribed to {len(part)} channels in this chunk.")

        # tiny pause to be nice to Deribit
        await asyncio.sleep(0.1)


# -----------------------------------------------------
# Main loop
# -----------------------------------------------------
async def run():
    while True:
        try:
            log.info("Connecting to Deribit WS…")
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:

                # 1) Auth
                await authenticate(ws)

                # 2) Fetch instruments
                log.info("Fetching instrument list…")
                channels = await fetch_instruments(ws)
                log.info(f"Found {len(channels)} option channels to subscribe.")

                # 3) Subscribe (BTC + ETH, in chunks)
                await subscribe_all(ws, channels)

                log.info("Listening for raw option trades…")

                # 4) Main receive loop
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)

                    params = msg.get("params")
                    if not params:
                        continue

                    data = params.get("data")
                    if not data:
                        continue

                    # Deribit uses either dict (single trade) or list
                    if isinstance(data, dict):
                        trades = [data]
                    else:
                        trades = data

                    for t in trades:
                        instr = t.get("instrument_name", "???")
                        try:
                            log.info(
                                f"{instr} | {t.get('direction')} | "
                                f"price {t.get('price')} | amount {t.get('amount')}"
                            )
                            insert_trade(t)
                        except Exception as e:
                            log.error(f"Error processing trade {instr}: {e}")

        except Exception as e:
            log.error(f"WS error: {e}")
            log.info("Reconnecting in 3 seconds…")
            await asyncio.sleep(3)


if __name__ == "__main__":
    log.info("Starting dankbit raw option trade listener…")
    asyncio.run(run())
