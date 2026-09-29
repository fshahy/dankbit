# -*- coding: utf-8 -*-
"""/4l chat panel backend — answers questions about option trades with a
LOCAL model served by Ollama (no external API calls).

Pipeline per question (POST /api/chat/<asset>):
  1. The model picks tool(s) from chat_tools.TOOL_SPECS (thinking off —
     tested far too slow on qwen3:14b for no gain).
  2. Python validates/repairs the arguments: asset must be BTC/ETH (else
     the page's asset), instrument checked against that asset's real
     expiry list (the model was observed inventing instruments), a past
     at_time/timezone converted to UTC here (the model got Berlin->UTC
     wrong), "ignore the window" questions forced to hours=all, unknown
     args dropped, at most MAX_TOOL_CALLS calls. Every result is stamped
     with the moment it describes (UTC + Berlin).
  3. Python runs the tools (read-only, pre-digested results).
  4. The model writes the answer from those results only.
  5. Number check: every number in the answer must appear in the tool
     results / page context / question (within tolerance). One rewrite
     is requested if not; any still-unverified numbers are returned to
     the client, which flags them.
Every exchange is logged to dankbit.chat.log for later review.

Configurable via ir.config_parameter:
  dankbit.chat_ollama_url  (default http://host.docker.internal:11434)
  dankbit.chat_model       (default qwen3:14b)
"""
import json
import logging
import math
from datetime import timedelta
import re
import time
from collections import Counter
import urllib.error
import urllib.request

import pytz

from odoo import http
from odoo.http import request

from . import chat_tools

_logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434"
DEFAULT_MODEL = "qwen3:14b"
OLLAMA_TIMEOUT = 240
MAX_TOOL_CALLS = 2
MAX_QUESTION_CHARS = 1000
MAX_HISTORY_TURNS = 2
NUM_CTX = 8192

_PERSIAN_RE = re.compile(r"[؀-ۿ]")
# An explicit language request wins over the script the question is typed in
# ("give previous answer in Persian" is English text asking for Persian).
_ASK_PERSIAN_RE = re.compile(r"\b(persian|farsi)\b|فارسی|پارسی", re.IGNORECASE)
_ASK_ENGLISH_RE = re.compile(r"\benglish\b|انگلیسی", re.IGNORECASE)


def _wants_persian(question):
    if _ASK_PERSIAN_RE.search(question):
        return True
    if _ASK_ENGLISH_RE.search(question):
        return False
    return bool(_PERSIAN_RE.search(question))


# "ignore the time window" / "whole history" — the model kept sending the
# page's 24h window anyway and called that the total (2026-09-23).
_ALL_HISTORY_RE = re.compile(
    r"ignore\s+(the\s+)?(time\s+)?window|\bno\s+(time\s+)?window|without\s+(a\s+|the\s+|any\s+)?(time\s+)?window"
    r"|(whole|entire|full|all)\s+(trade\s+)?history|\ball[\s-]time\b|کل\s+تاریخچه|بدون\s+(بازه|پنجره)",
    re.IGNORECASE)
_TOOL_CALL_TEXT_RE = re.compile(r'"arguments"\s*:|</?tool_call>|^\s*\{\s*"name"\s*:')

_TIME_RANGE_RE = re.compile(
    r"\b(?:between|from)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:and|to|until|till|-|–)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
    r"|از\s+(?:ساعت\s+)?(\d{1,2})(?::(\d{2}))?\s+تا\s+(?:ساعت\s+)?(\d{1,2})(?::(\d{2}))?",
    re.IGNORECASE)

_AT_MOMENT_RE = re.compile(r"\bat\s+\d{1,2}(?::\d{2}|\s*(?:am|pm|utc|berlin|tehran|o'?clock))"
                           r"|(?:در\s+)?ساعت\s+\d{1,2}(?::\d{2})?(?!\s*تا)", re.IGNORECASE)

# "since midnight" / "today" — the model turned it into hours=24.
_SINCE_MIDNIGHT_RE = re.compile(
    r"since\s+(00:00|0:00|midnight)|\btoday\b|\bthis\s+day\b|از\s+نیمه\s*شب|امروز",
    re.IGNORECASE)
# A tool-less answer claiming data is absent ("No block trades were found in
# the entire history" — there were 21) contains no numbers for the number
# check to catch, so it gets the same forced re-pick.
_NO_DATA_CLAIM_RE = re.compile(
    r"\bno\s+(\w+\s+){0,2}(trades?|data|information|records?)\b|\b(don'?t|do\s+not)\s+have\s+(any\s+)?"
    r"(information|data|access)|\bnot\s+(found|available|supported|provided)\b|\bfunctions?\b|\btools?\b|هیچ\s+معامله|اطلاعاتی\s+ندارم|داده‌ای\s+ندارم",
    re.IGNORECASE)

# "give previous answer in Persian" / "say that in English" / "ترجمه کن" —
# handled by a dedicated translation call on the prior answer. Answering it
# in the normal chat context, qwen3:14b rewrote "SC recently vs LC
# historically" as "LC recently vs LC historically" (2026-09-24) — no
# numbers changed, so the number check could not catch it.
_TRANSLATE_RE = re.compile(
    r"\btranslat|\b(previous|last|above|that|your|same)\s+(answer|reply|response|one)\b"
    r"|\b(say|write|repeat|give|answer)\s+(it|that|this)\b.{0,20}\bin\s+(persian|farsi|english)\b"
    r"|ترجمه|(جواب|پاسخ)\s*(قبلی|بالا)", re.IGNORECASE)
_TRANSLATE_WORD_RE = re.compile(r"translat|ترجمه", re.IGNORECASE)
# ASCII-only boundaries: "LCها" in Persian text has no \b between C and ه.
_LEG_RE = re.compile(r"(?<![A-Za-z])(LC|LP|SC|SP)(?![A-Za-z])")

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬٫،", "01234567890123456789,.,")  # incl. Persian comma
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

GLOSSARY = (
    "Glossary: LC = long call (calls bought), LP = long put (puts bought), SC = short call (calls sold), "
    "SP = short put (puts sold). A leg's gamma peak price is where that leg's gamma is largest. "
    "Dominant leg = the leg with the largest gamma (not the leg with the most trades or activity). Delta zero = price where combined delta crosses zero. "
    "Gamma flip = price where combined taker gamma changes sign. Max pain = strike where option buyers lose the "
    "most at expiry, from current open interest. Contracts are counted in the underlying (BTC/ETH). "
    "Volume = contracts traded; open interest = contracts still open (not the same thing). "
    "Buying puts or selling calls is bearish positioning (betting on or protecting against a fall); buying "
    "calls or selling puts is bullish positioning. Net put buying leans bearish, though it can also be hedging."
)
# Only sent for Persian questions — including it on English questions made
# qwen3:14b answer English questions in Persian (2026-09-23).
PERSIAN_TERMS = (
    "Persian terms (use exactly these): call = کال, put = پوت, buy/bought = خرید, sell/sold = فروش, "
    "long = لانگ, short = شورت, strike = استرایک, expiry = سررسید, gamma = گاما, delta = دلتا, "
    "block trade = معامله بلاک, premium = پریمیوم, trade = معامله, "
    "contracts = قرارداد (contract amounts are قرارداد, never معامله), "
    "volume = حجم معاملات, open interest = اوپن اینترست (never call open interest حجم), leg = لگ, dominant = غالب, net = خالص, zone = زون, "
    "resistance = مقاومت, support = حمایت, gamma band = باند گاما, gamma peak price = قیمت اوج گاما "
    "(a price level, not a premium), long put = لانگ پوت, long call = لانگ کال, short put = شورت پوت, "
    "short call = شورت کال."
)


# What the user is looking at, per page (body "page"); /4l sends none.
PAGE_NOTES = {
    "ll": "The user is on the LL chart: one line per active expiry, at that expiry's dominant gamma leg — there "
          "is no selected expiry. For anything comparing expiries or about 'the lines' / 'all expiries' / the "
          "strongest expiry, use dominant_legs_by_expiry; for a question needing one expiry, use the nearest "
          "unless the user names one. ",
    "chart": "The user is on the Delta Chart: candles plus the nearest expiry's Zones (High Zone, Low Zone, "
             "Middle Zone with SMP/BML), Bands (High/Resistance, Low/Support, Gamma Band), Smart Liquidity, the "
             "Signal Bot and the Thales Forecast candles. For those, use zones_and_bands, signal_bot or "
             "next_candle_forecast (for any forecast question, whatever the horizon; the forecast is the "
             "engine's output — say so). ",
}


def _visible_lines(flags):
    """/ll's checkbox states -> one prompt sentence. By default only the
    strongest expiry's line is shown; listing all 10 expiries as "the
    lines on the chart" didn't match what the user saw (2026-09-24)."""
    if not isinstance(flags, dict):
        return ""
    shown = [label for key, label in (("strongest", "the strongest expiry's line"),
                                      ("other_expiries", "the other expiries' lines"),
                                      ("window_1d_2d_3d", "the 1D/2D/3D lines (the strongest reading over fixed "
                                                          "24h/48h/72h windows)")) if flags.get(key)]
    return ("Currently visible on the chart: " + (", ".join(shown) if shown else "no gamma lines") +
            ". Lines not listed are hidden by the user's checkboxes — if asked about 'the lines on the chart', "
            "describe the visible ones and say the others are hidden. ")


def _ollama(payload):
    icp = request.env["ir.config_parameter"].sudo()
    url = icp.get_param("dankbit.chat_ollama_url", DEFAULT_OLLAMA_URL).rstrip("/") + "/api/chat"
    payload = {"model": icp.get_param("dankbit.chat_model", DEFAULT_MODEL), "stream": False, "think": False,
               "keep_alive": "30m", **payload}
    payload.setdefault("options", {"num_ctx": NUM_CTX, "temperature": 0.1})
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        return json.load(resp), payload["model"]


def _numbers(text):
    return {float(x.replace(",", "")) for x in _NUM_RE.findall((text or "").translate(_DIGITS))}


def _unverified(answer, known_text):
    known = [k for k in _numbers(known_text) if k]

    def ok(n):
        if n <= 10:
            return True  # ordinals, list positions, small counts
        for k in known:
            for scale in (1, 1e3, 1e6, 1e9):  # "2.98 billion", "71M", "110k"
                if abs(n * scale - k) <= 0.01 * k:
                    return True
        return False

    return sorted({n for n in _numbers(answer) if not ok(n)})


def _parse_time_range(question, now=None):
    """"between 08:00 and 10:00 UTC today" / "from 14 to 16 Berlin" /
    "از ساعت ۸ تا ۱۰" -> (start, end) as naive UTC, end capped at now, or
    None. Parsed here, not by the model: asked for 08:00-10:00 UTC it sent
    at_time=08:00, hours=8 and presented 00:00-08:00 data as 08:00-10:00
    (2026-09-24). Timezone from the question (Berlin/Tehran, else UTC);
    date from an explicit YYYY-MM-DD, "yesterday"/"دیروز", else today in
    that timezone. A range ending before it starts crosses midnight."""
    text = question.translate(_DIGITS)
    m = _TIME_RANGE_RE.search(text)
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = (m.group(i) for i in range(1, 7))
    if h1 is None:  # the Persian alternative
        h1, m1, h2, m2 = m.group(7), m.group(8), m.group(9), m.group(10)
    # Only a clear time range — not "between 5 and 10 trades".
    if not (m1 or m2 or ap1 or ap2 or re.search(r"utc|gmt|berlin|tehran|o'?clock|ساعت|برلین|تهران", text, re.I)):
        return None

    def hm(h, mi, ap):
        h, mi = int(h), int(mi or 0)
        if ap:
            h = h % 12 + (12 if ap.lower() == "pm" else 0)
        return (h, mi) if h < 24 and mi < 60 else None

    t1, t2 = hm(h1, m1, ap1 or ap2), hm(h2, m2, ap2)
    if not t1 or not t2:
        return None
    tz = ("Berlin" if re.search(r"berlin|برلین", text, re.I)
          else "Tehran" if re.search(r"tehran|تهران", text, re.I) else "UTC")
    now = now or chat_tools._now()
    local_now = pytz.utc.localize(now).astimezone(pytz.timezone(chat_tools.TIMEZONES[tz]))
    date_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    day = (date_m.group(1) if date_m else
           (local_now - timedelta(days=1)).strftime("%Y-%m-%d") if re.search(r"yesterday|دیروز", text, re.I)
           else local_now.strftime("%Y-%m-%d"))
    start = chat_tools.parse_at(f"{day} {t1[0]:02d}:{t1[1]:02d}", tz)
    end = chat_tools.parse_at(f"{day} {t2[0]:02d}:{t2[1]:02d}", tz)
    if not start or not end:
        return None
    if end <= start:
        start -= timedelta(days=1)
    end = min(end, now)
    return (start, end) if start < end else None


def _clean_args(name, raw, ctx, instruments_for, all_history, since_midnight=False, time_range=None):
    """Returns (args, time_note). time_note is set when the model asked for
    a past moment this tool can't serve, so the result says so."""
    raw = raw or {}
    allowed = chat_tools.ALLOWED_ARGS[name]
    args = {k: v for k, v in raw.items() if k in allowed and v not in (None, "")}
    asset = str(raw.get("asset") or "").upper()
    args["asset"] = asset = asset if asset in chat_tools.ASSETS else ctx["asset"]

    as_of, time_note = None, None
    if raw.get("at_time"):
        as_of = chat_tools.parse_at(raw["at_time"], raw.get("timezone"))
        if name not in chat_tools.AS_OF_TOOLS:
            as_of, time_note = None, (f"The user asked about {raw['at_time']} {raw.get('timezone') or 'UTC'}, but "
                                      "history is NOT stored for this data. Say that clearly, then give the value "
                                      "below labeled as the CURRENT value, not the past one.")
        elif as_of is None:
            time_note = f"Could not read the time {raw['at_time']!r}; this is LIVE (current) data."
        elif as_of >= chat_tools._now():
            as_of = None  # "now" or the future — just live data

    if time_range and name in chat_tools.AS_OF_TOOLS and "hours" in allowed:
        # The range ends at as_of (None = up to now, i.e. live data).
        start, end = time_range
        as_of, time_note = (end if end < chat_tools._now() - timedelta(minutes=1) else None), None

    instruments = instruments_for(asset, as_of)
    inst = str(args.get("instrument") or "").upper()
    if inst and inst not in instruments:
        # Invented/expired/other-asset instrument — fall back below, and
        # say so: asked about BTC-31DEC27, it silently answered for the
        # nearest expiry (2026-09-24).
        time_note = ((time_note + " ") if time_note else "") + (
            f"The user asked about {inst}, which is not an active {asset} expiry. Say that clearly first, "
            "then give the data below, naming the expiry it is actually for.")
        inst = ""
    if name in chat_tools.NEEDS_INSTRUMENT:
        page = ctx["instrument"] if ctx["instrument"] in instruments else None
        inst = inst or page or (instruments[0] if instruments else "")
    if "instrument" in allowed:  # zones_and_bands / signal_bot take none
        args["instrument"] = inst or None
    if time_range and name in chat_tools.AS_OF_TOOLS and "hours" in allowed:
        start, end = time_range
        args["hours"] = max(1, math.ceil((end - start).total_seconds() / 3600))
    elif since_midnight and "hours" in allowed and not all_history:
        # "since midnight" / "today": hours since 00:00 UTC of the moment
        # asked about — the model sent 24 at 11:00 UTC (2026-09-24).
        moment = as_of or chat_tools._now()
        elapsed = (moment - moment.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
        args["hours"] = max(1, math.ceil(elapsed / 3600))
    elif name in chat_tools.RECENT_WINDOW_TOOLS:
        # Recent window: the model's value, else the page's Window, else 4h.
        if chat_tools._hours(args.get("hours"), None) is None:
            args["hours"] = ctx["hours"] if isinstance(ctx["hours"], int) else 4
    elif "hours" in allowed:
        if all_history:
            args["hours"] = 0
        elif "hours" not in args or (name in chat_tools.CHART_WINDOW_TOOLS and chat_tools._hours(args["hours"]) is None):
            args["hours"] = ctx["hours"]  # the model sends hours=0 unasked; these must match the chart
    if as_of:
        args["as_of"] = as_of
    return args, time_note


def _language_rule(persian):
    return ("You MUST write your answer in Persian (Farsi)." if persian
            else "You MUST write your answer in English only, even if other languages appear above.")


def _system_prompt(ctx, instruments, persian):
    # The language rule goes last: a small model follows the most recent
    # instruction far more reliably than one buried mid-prompt.
    return (
        "You are Dankbit's assistant for Deribit options trade data. "
        "Use the tools to fetch data; never guess data. When you answer, use ONLY numbers that appear in the "
        "tool results. Do not calculate new numbers. Do not give trading advice or make your own predictions; describe the data (a forecast engine's stored output is data — report it as that engine's forecast). "
        "Do not add conclusions the results do not state. If the data does not match the exact period or item "
        "asked, give the closest data you have and say exactly what it covers — do not just refuse. Say which expiry the data covers (or all expiries). "
        "Be concise: at most 6 short sentences or bullets. Times are UTC. "
        "Never mention tool or function names to the user. "
        f"{GLOSSARY} {PERSIAN_TERMS if persian else ''} "
        "Every tool result states the moment it describes (data_as_of_utc / data_as_of_berlin, live or "
        "historical): for a question about a past moment, pass at_time and timezone to the tool, and never "
        "present live data as past data. If a result says it is not a complete list, give the total it states "
        "and do not generalize from the listed items. If the user names the other asset, use that asset. "
        f"Page context (use it for anything the user does not specify): asset={ctx['asset']}, "
        + (f"selected expiry={ctx['instrument']}, " if ctx["instrument"] else "selected expiry=none, ")
        + f"window={ctx['window_label'] + ' = ' if ctx.get('window_label') else ''}{ctx['hours']}h, "
        f"now={ctx['now']} UTC = {ctx['now_berlin']} Berlin. "
        + "".join(f"Active {a} expiries, soonest first: {', '.join(lst[:12])}. " for a, lst in instruments.items())
        + PAGE_NOTES.get(ctx.get("page"), "")
        + (ctx.get("visible_lines") or "")
        + 
        f"{_language_rule(persian)}"
    )


def _translation_problems(source, text):
    """Leg codes must survive translation one-for-one, and no number may
    appear that isn't in the source."""
    problems = []
    if Counter(_LEG_RE.findall(source)) != Counter(_LEG_RE.findall(text)):
        problems.append("the codes LC/LP/SC/SP must appear exactly as in the source, each in the same place "
                        f"(source order: {', '.join(_LEG_RE.findall(source)) or 'none'})")
    bad = _unverified(text, source)
    if bad:
        problems.append("these numbers are not in the source: " + ", ".join(f"{n:g}" for n in bad))
    return problems


def _translate(source, persian):
    """Returns (translation, model, problems_left, retried)."""
    lang = "Persian (Farsi)" if persian else "English"
    system = (f"You are a translator. Translate the user's text into {lang}. Translate sentence by sentence, "
              "keeping the meaning exact. Keep every number, date, instrument name (e.g. BTC-25SEP26) and the "
              "codes LC, LP, SC, SP exactly as written, each attached to the same statement as in the source. "
              "Do not add, drop or merge statements. Output only the translation."
              + (" " + PERSIAN_TERMS if persian else ""))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": source}]
    resp, model = _ollama({"messages": messages})
    text = ((resp.get("message") or {}).get("content") or "").strip()
    problems, retried = _translation_problems(source, text), False
    if problems:
        retried = True
        fix = "Your translation is wrong: " + "; ".join(problems) + f". Translate the text again into {lang}."
        resp, model = _ollama({"messages": messages + [{"role": "assistant", "content": text},
                                                        {"role": "user", "content": fix}]})
        text = ((resp.get("message") or {}).get("content") or "").strip() or text
        problems = _translation_problems(source, text)
    return text, model, problems, retried


NO_DATA_EN = ("I couldn't fetch data for that question, so I won't guess numbers. "
              "Try rephrasing it, e.g. mention the expiry or the time window.")
NO_DATA_FA = "نتوانستم داده‌ای برای این سؤال دریافت کنم و عدد حدسی نمی‌دهم. لطفاً سؤال را دقیق‌تر بپرسید (مثلاً سررسید یا بازه زمانی)."


# "should I buy puts?" / "is it a good time to short?" / "بخرم؟"
_ADVICE_RE = re.compile(
    r"\bshould\s+i\s+(buy|sell|long|short|enter|exit|close|hold|open|trade)\b"
    r"|\b(is\s+it|good)\s+(a\s+)?(good\s+)?time\s+to\s+(buy|sell|long|short)\b"
    r"|\bwhat\s+should\s+i\s+(buy|sell|trade|do)\b|\b(do|would)\s+you\s+recommend\b"
    r"|بخرم|بفروشم|لانگ\s+بگیرم|شورت\s+بگیرم|توصیه\s+(می\s*کنی|میکنی)|پیشنهاد\s+(می\s*دهی|میدی)",
    re.IGNORECASE)
ADVICE_EN = ("I don't give trading advice. I can show the data behind a decision — for example put buying and "
             "selling, the dominant gamma leg, max pain, the zones, or the Signal Bot's recorded decision.")
ADVICE_FA = ("من توصیه معاملاتی نمی‌دهم. می‌توانم داده‌های پشت یک تصمیم را نشان دهم — مثلاً خرید و فروش پوت، "
             "لگ غالب گاما، ماکس پین، زون‌ها، یا تصمیم ثبت‌شده سیگنال بات.")


class DankbitChat(http.Controller):

    @http.route("/api/chat/<string:asset>", type="http", auth="user", methods=["POST"], website=False, csrf=False)
    def chat(self, asset):
        t0 = time.time()
        asset = asset.upper()
        if asset not in chat_tools.ASSETS:
            return self._json({"error": "Unknown asset"}, 400)
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return self._json({"error": "Invalid JSON"}, 400)
        question = str(body.get("question") or "").strip()[:MAX_QUESTION_CHARS]
        if not question:
            return self._json({"error": "Empty question"}, 400)

        env = request.env
        _inst_cache = {}

        def instruments_for(a, as_of=None):
            key = (a, as_of)
            if key not in _inst_cache:
                _inst_cache[key] = chat_tools.active_instruments(env, a, as_of)
            return _inst_cache[key]

        instruments = instruments_for(asset)
        page_inst = str(body.get("instrument") or "").upper()
        # /ll/<asset> has no Expiry dropdown — it draws every active
        # expiry's dominant gamma leg — so there's no selected expiry to
        # default to (unlike /4l, where a missing one means the nearest).
        page = body.get("page") if body.get("page") in PAGE_NOTES else None
        all_expiries_page = page == "ll"
        stamp = chat_tools.as_of_stamp(None)
        ctx = {
            "asset": asset,
            "instrument": (None if all_expiries_page
                           else page_inst if page_inst in instruments else (instruments[0] if instruments else None)),
            "hours": chat_tools._hours(body.get("hours"), 24) or "all",
            "page": page,
            # A page-supplied label for the window, e.g. "since 00:00 UTC"
            # on the Delta Chart, where `hours` is only its current length.
            "window_label": str(body.get("window_label") or "")[:40],
            "visible_lines": _visible_lines(body.get("visible_lines")) if page == "ll" else None,
            "visible_flags": body.get("visible_lines") if page == "ll" and isinstance(body.get("visible_lines"), dict)
                             else None,
            "now": stamp["data_as_of_utc"],
            "now_berlin": stamp["data_as_of_berlin"],
        }
        persian = _wants_persian(question)
        all_history = bool(_ALL_HISTORY_RE.search(question))
        # "at 10:00 UTC today" names a moment — "today" is only its date, so
        # the window stays the page's (it had become since-midnight = 10h).
        since_midnight = bool(_SINCE_MIDNIGHT_RE.search(question)) and not _AT_MOMENT_RE.search(
            question.translate(_DIGITS))
        time_range = _parse_time_range(question)
        all_instruments = {a: instruments_for(a) for a in chat_tools.ASSETS}

        messages = [{"role": "system", "content": _system_prompt(ctx, all_instruments, persian)}]
        for turn in (body.get("history") or [])[-MAX_HISTORY_TURNS:]:
            q, a = str(turn.get("question") or "")[:MAX_QUESTION_CHARS], str(turn.get("answer") or "")[:2000]
            if q and a:
                messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "user", "content": question})

        log = {"asset": asset, "instrument": ctx["instrument"], "window_hours": str(ctx["hours"]),
               "question": question, "user_id": env.uid}
        calls_made, answer, unverified, retried, model, discarded, warning = [], "", [], False, None, "", None
        last_answer = next((str(t.get("answer") or "") for t in reversed(body.get("history") or [])
                            if t.get("answer")), "")[:2000]
        translate = bool(last_answer and _TRANSLATE_RE.search(question) and (
            _ASK_PERSIAN_RE.search(question) or _ASK_ENGLISH_RE.search(question)
            or _TRANSLATE_WORD_RE.search(question)))
        try:
            if _ADVICE_RE.search(question):
                # Answered without the model: asked "should I buy puts now?" it
                # fetched the Signal Bot, called its decision a "recommendation",
                # and took 234 s (2026-09-24).
                answer, model = (ADVICE_FA if persian else ADVICE_EN), "fixed reply"
            elif translate:
                answer, model, problems, retried = _translate(last_answer, persian)
                if problems:
                    discarded = "translation check: " + "; ".join(problems)
                    warning = ("This translation may not match the previous answer (leg codes or numbers "
                               "differ) — check it against the original.")
            else:
                # 1. tool selection
                resp, model = _ollama({"messages": messages, "tools": chat_tools.TOOL_SPECS,
                                       "options": {"num_ctx": NUM_CTX, "temperature": 0}})
                msg = resp.get("message") or {}
                tool_calls = (msg.get("tool_calls") or [])[:MAX_TOOL_CALLS]

                if not tool_calls:
                    # General question (e.g. "what is a gamma flip?") or a follow-up on a
                    # previous answer — fine without data, as long as it states no numbers
                    # from nowhere. qwen3:14b was observed skipping the tools on a plain data
                    # question and inventing every figure (2026-09-23), so an unbacked answer
                    # gets one forced re-pick and is never shown as-is.
                    answer = (msg.get("content") or "").strip()
                    # Earlier answers' numbers don't count here: "where is smart
                    # liquidity?" was answered tool-less with the previous answer's
                    # Gamma Band value (2026-09-24). A genuine rephrase is handled by
                    # the translation path; any other follow-up re-fetches its data.
                    unverified = _unverified(answer, json.dumps(ctx) + question + " ".join(instruments))
                    no_data_claim = bool(_NO_DATA_CLAIM_RE.search(answer))
                    if unverified or no_data_claim or all_history:
                        retried = True
                        discarded = "discarded tool-less answer: " + (", ".join(f"{n:g}" for n in unverified) or (
                            "no-data claim" if no_data_claim else "all-history question"))
                        nudge = ("Do not answer from memory. Call one of the tools to fetch the data needed "
                                 "for my question: " + question)
                        resp, model = _ollama({"messages": messages + [{"role": "user", "content": nudge}],
                                               "tools": chat_tools.TOOL_SPECS,
                                               "options": {"num_ctx": NUM_CTX, "temperature": 0}})
                        tool_calls = ((resp.get("message") or {}).get("tool_calls") or [])[:MAX_TOOL_CALLS]
                        if not tool_calls:
                            answer = (NO_DATA_FA if persian else NO_DATA_EN)
                            unverified = []
                if tool_calls:
                    # 2-3. validate + run tools
                    assistant_calls, tool_msgs = [], []
                    for call in tool_calls:
                        fn = call.get("function") or {}
                        name = fn.get("name")
                        if name not in chat_tools.TOOLS:
                            continue
                        raw = fn.get("arguments") or {}
                        if isinstance(raw, str):
                            try:
                                raw = json.loads(raw)
                            except ValueError:
                                raw = {}
                        args, time_note = _clean_args(name, raw, ctx, instruments_for, all_history, since_midnight, time_range)
                        try:
                            result = chat_tools.TOOLS[name](env, **args)
                        except Exception as e:
                            _logger.exception("chat tool %s failed", name)
                            result = {"error": f"tool failed: {e}"}
                        if name != "compare_windows":  # carries its own two timestamps
                            result = {**chat_tools.as_of_stamp(args.get("as_of")), **result}
                        if time_note:
                            result = {"time_note": time_note, **result}
                        flags = ctx.get("visible_flags")
                        if flags and name == "dominant_legs_by_expiry" and result.get("ranked_by_gamma"):
                            # Marked per row — the prompt sentence alone was ignored and
                            # all 10 expiries were listed as "the lines on the chart".
                            for row in result["ranked_by_gamma"]:
                                # /ll draws other expiries only above 1000M |gamma| (BTC) / 100M (ETH).
                                row["shown_on_chart"] = bool(flags.get("strongest") if row["rank"] == 1
                                                             else flags.get("other_expiries")
                                                             and row.get("gamma_millions", 0) > (100 if args.get("asset") == "ETH" else 1000))
                            result = {"chart_note": "shown_on_chart says whether that expiry's line is visible "
                                                    "on the user's chart right now (the rest are hidden by "
                                                    "checkboxes)", **result}
                        if time_range and name in chat_tools.AS_OF_TOOLS and "hours" in args:
                            start, end = time_range
                            result = {"window": f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC "
                                                "(the time range asked, counted in whole hours ending at the "
                                                "end time)", **result}
                        elif since_midnight and "hours" in args and not all_history:
                            # Else it reports "the past 11 hours" for "today".
                            result = {"window": "since 00:00 UTC today", **result}
                        args = {k: (v.strftime("%Y-%m-%d %H:%M") if k == "as_of" else v) for k, v in args.items()}
                        calls_made.append({"name": name, "args": args, "result": result})
                        assistant_calls.append({"function": {"name": name, "arguments": args}})
                        tool_msgs.append({"role": "tool", "content": json.dumps(result, default=str)})

                    # 4. write the answer from the results
                    messages += [{"role": "assistant", "content": "", "tool_calls": assistant_calls}] + tool_msgs
                    # Repeat the language rule right before the answer is written —
                    # the tool results in between would otherwise dilute it.
                    messages.append({"role": "system", "content": _language_rule(persian)})
                    resp, model = _ollama({"messages": messages})
                    answer = ((resp.get("message") or {}).get("content") or "").strip()

                    # The model sometimes writes a tool call as plain text instead of
                    # an answer (seen when a tool returned an error, 2026-09-24).
                    if _TOOL_CALL_TEXT_RE.search(answer):
                        retried = True
                        resp, model = _ollama({"messages": messages + [
                            {"role": "assistant", "content": answer},
                            {"role": "user", "content": "Do not call tools. Write the answer in plain text from the "
                                                        "results above. " + _language_rule(persian)}]})
                        answer = ((resp.get("message") or {}).get("content") or "").strip()
                        if not answer or _TOOL_CALL_TEXT_RE.search(answer):
                            discarded = "answer was tool-call text"
                            answer = NO_DATA_FA if persian else NO_DATA_EN

                    # 5. number check (+ one rewrite)
                    known = json.dumps([c["result"] for c in calls_made], default=str) + json.dumps(ctx) + question
                    unverified = _unverified(answer, known)
                    if unverified:
                        retried = True
                        fix = ("Your answer contains numbers that are not in the tool results: "
                               + ", ".join(f"{n:g}" for n in unverified)
                               + ". Rewrite the answer using only numbers that appear exactly in the tool results. "
                               + _language_rule(persian))
                        resp, model = _ollama({"messages": messages + [
                            {"role": "assistant", "content": answer}, {"role": "user", "content": fix}]})
                        answer = ((resp.get("message") or {}).get("content") or "").strip() or answer
                        unverified = _unverified(answer, known)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _logger.warning("chat: Ollama unreachable: %s", e)
            log.update(error=f"Ollama unreachable: {e}")
        except Exception as e:
            _logger.exception("chat failed")
            log.update(error=str(e))

        elapsed = round(time.time() - t0, 1)
        log.update(answer=answer, model=model, retried=retried, elapsed_seconds=elapsed,
                   tool_calls=json.dumps(calls_made, default=str, ensure_ascii=False),
                   unverified_numbers=", ".join(filter(None, [
                       ", ".join(f"{n:g}" for n in unverified),
                       discarded])))
        try:
            env["dankbit.chat.log"].sudo().create(log)
        except Exception:
            _logger.exception("chat: could not write chat log")

        if log.get("error"):
            return self._json({"error": log["error"], "elapsed": elapsed}, 502)
        return self._json({
            "answer": answer or "(no answer)",
            "tools": calls_made,
            "unverified_numbers": unverified,
            "warning": warning,
            "retried": retried,
            "model": model,
            "elapsed": elapsed,
            "context": ctx,
        })

    @staticmethod
    def _json(data, status=200):
        return request.make_response(json.dumps(data, default=str, ensure_ascii=False), status=status,
                                     headers=[("Content-Type", "application/json; charset=utf-8")])
