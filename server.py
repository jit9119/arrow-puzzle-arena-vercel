#!/usr/bin/env python3
"""
Arrow Puzzle: Arena — Vercel Serverless & Production Flask Backend (v8)
- Runs seamlessly on:
  * Vercel Python Serverless (`api/index.py` + `vercel.json`)
  * Gunicorn / Render / Railway / VPS (`gunicorn -w 1 --threads 8 -b 0.0.0.0:3000 app:app`)
  * Local Python (`python3 server.py`)
- Smart Storage Engine:
  * Automatically uses writable `/tmp/arrow_puzzle_arena_data` on Vercel read-only filesystems
  * Optional automatic cloud persistence via Vercel KV / Upstash Redis REST env vars
  * Environment variable defaults for Razorpay & UPI Merchant keys on Vercel
  * Stateless HMAC-signed `orderToken` fallback so multi-instance serverless payment verification never drops orders
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor_pkgs")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.append(_VENDOR_DIR)

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
SEED_DATA_DIR = os.path.join(BASE_DIR, "data")

IS_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))

# Optional Vercel KV / Upstash Redis REST credentials (zero extra dependencies)
KV_REST_URL = (
    os.environ.get("KV_REST_API_URL")
    or os.environ.get("UPSTASH_REDIS_REST_URL")
    or ""
).strip().rstrip("/")
KV_REST_TOKEN = (
    os.environ.get("KV_REST_API_TOKEN")
    or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    or ""
).strip()
HAS_CLOUD_KV = bool(KV_REST_URL and KV_REST_TOKEN)


def _select_runtime_data_dir() -> str:
    override = os.environ.get("ARENA_DATA_DIR", "").strip()
    if override:
        return override
    if IS_VERCEL or not os.access(BASE_DIR, os.W_OK):
        return "/tmp/arrow_puzzle_arena_data"
    return SEED_DATA_DIR


DATA_DIR = _select_runtime_data_dir()

LEADERBOARD_FILE = os.path.join(DATA_DIR, "leaderboard.json")
DAILY_ARENA_FILE = os.path.join(DATA_DIR, "daily_arena.json")
WEEKLY_LEAGUE_FILE = os.path.join(DATA_DIR, "weekly_league.json")
FRIENDS_FILE = os.path.join(DATA_DIR, "friends.json")
CHALLENGES_FILE = os.path.join(DATA_DIR, "challenges.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
PAYMENT_CONFIG_FILE = os.path.join(DATA_DIR, "payment_config.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")

_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS = defaultdict(threading.RLock)


def get_file_lock(filepath: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _FILE_LOCKS[os.path.abspath(filepath)]


# Official 4 Gem & Cosmetic Bundle Packs in INR
GEM_PACK_CATALOG = {
    "starter": {
        "id": "starter",
        "title": "Starter Pack",
        "gems": 25,
        "amountInr": 19,
        "bonusSkins": [],
        "bonusTrails": [],
    },
    "pouch": {
        "id": "pouch",
        "title": "Cosmic Pouch + Celestial Trident",
        "gems": 80,
        "amountInr": 69,
        "bonusSkins": ["sovereign"],
        "bonusTrails": [],
    },
    "satchel": {
        "id": "satchel",
        "title": "Thunder Sparks + Rainbow Cosmic",
        "gems": 150,
        "amountInr": 129,
        "bonusSkins": ["rainbow"],
        "bonusTrails": ["thunder", "rainbow_trail"],
    },
    "vault": {
        "id": "vault",
        "title": "Rainbow Cosmic + Frozen Snowflakes + Electric Lightning + Neon Cyber",
        "gems": 500,
        "amountInr": 299,
        "bonusSkins": ["rainbow", "electric", "cyber"],
        "bonusTrails": ["snow", "rainbow_trail"],
    },
}

LEAGUE_TIERS = [
    {"id": "bronze", "name": "Bronze", "minPoints": 0},
    {"id": "silver", "name": "Silver", "minPoints": 600},
    {"id": "gold", "name": "Gold", "minPoints": 1500},
    {"id": "diamond", "name": "Diamond", "minPoints": 3000},
    {"id": "legend", "name": "Legend", "minPoints": 5000},
]

SEED_LEADERS = [
    {"playerId": "seed-1", "friendCode": "ARENA-A1R9", "name": "Aarav_99", "avatar": "🦅", "city": "Jaipur", "score": 5420, "weeklyPoints": 5420, "levelsCleared": 38, "flawlessCount": 24, "bestCombo": 14, "albumCount": 38, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-2", "friendCode": "ARENA-M2Y7", "name": "MeeraGlow", "avatar": "🦋", "city": "Mumbai", "score": 4890, "weeklyPoints": 3620, "levelsCleared": 33, "flawlessCount": 21, "bestCombo": 12, "albumCount": 33, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-3", "friendCode": "ARENA-K3B5", "name": "KabirZen", "avatar": "🐯", "city": "Delhi", "score": 3910, "weeklyPoints": 2150, "levelsCleared": 28, "flawlessCount": 16, "bestCombo": 11, "albumCount": 28, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-4", "friendCode": "ARENA-R4Y2", "name": "RiyaFlow", "avatar": "🪷", "city": "Bengaluru", "score": 3180, "weeklyPoints": 1640, "levelsCleared": 22, "flawlessCount": 14, "bestCombo": 9, "albumCount": 22, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-5", "friendCode": "ARENA-D5V8", "name": "Dev_Neon", "avatar": "🚀", "city": "Pune", "score": 2540, "weeklyPoints": 980, "levelsCleared": 18, "flawlessCount": 10, "bestCombo": 8, "albumCount": 18, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-6", "friendCode": "ARENA-A6N4", "name": "AnanyaArt", "avatar": "🦚", "city": "Udaipur", "score": 1960, "weeklyPoints": 720, "levelsCleared": 14, "flawlessCount": 8, "bestCombo": 7, "albumCount": 14, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-7", "friendCode": "ARENA-V7H1", "name": "VihaanX", "avatar": "🦊", "city": "Ahmedabad", "score": 1420, "weeklyPoints": 460, "levelsCleared": 10, "flawlessCount": 6, "bestCombo": 6, "albumCount": 10, "updatedAt": "2026-10-25T05:00:00Z"},
    {"playerId": "seed-8", "friendCode": "ARENA-S8N6", "name": "SanaCalm", "avatar": "🌙", "city": "Hyderabad", "score": 980, "weeklyPoints": 290, "levelsCleared": 7, "flawlessCount": 4, "bestCombo": 5, "albumCount": 7, "updatedAt": "2026-10-25T05:00:00Z"},
]

SEED_DAILY_ARENA = [
    {"playerId": "seed-1", "name": "Aarav_99", "avatar": "🦅", "league": "Legend", "timeMs": 26420, "mistakes": 0},
    {"playerId": "seed-2", "name": "MeeraGlow", "avatar": "🦋", "league": "Diamond", "timeMs": 31850, "mistakes": 0},
    {"playerId": "seed-3", "name": "KabirZen", "avatar": "🐯", "league": "Gold", "timeMs": 37910, "mistakes": 1},
    {"playerId": "seed-4", "name": "RiyaFlow", "avatar": "🪷", "league": "Gold", "timeMs": 43280, "mistakes": 0},
    {"playerId": "seed-5", "name": "Dev_Neon", "avatar": "🚀", "league": "Silver", "timeMs": 49640, "mistakes": 1},
    {"playerId": "seed-7", "name": "VihaanX", "avatar": "🦊", "league": "Bronze", "timeMs": 58120, "mistakes": 2},
]

MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

KNOWN_API_PREFIXES = (
    "/health",
    "/leaderboard",
    "/daily-arena",
    "/weekly-league",
    "/friends",
    "/challenges/",
    "/account/",
    "/payment/",
    "/admin/",
)


# ---------------------------------------------------------------------------
# Helpers, Cloud KV Bridge & Atomic File Persistence
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_date_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def current_week_key() -> str:
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def compute_league_tier(weekly_points) -> str:
    pts = int(weekly_points or 0)
    if pts >= 5000:
        return "Legend"
    if pts >= 3000:
        return "Diamond"
    if pts >= 1500:
        return "Gold"
    if pts >= 600:
        return "Silver"
    return "Bronze"


def make_friend_code(player_id) -> str:
    digest = hashlib.sha1(str(player_id or "player").encode("utf-8")).hexdigest().upper()
    return f"ARENA-{digest[:4]}"


def _kv_key_for_file(filepath: str) -> str:
    stem = os.path.splitext(os.path.basename(filepath))[0]
    return f"arena_v8:{stem}"


def _kv_get_json(key: str):
    if not HAS_CLOUD_KV:
        return None
    try:
        encoded_key = urllib.parse.quote(key, safe="")
        req = urllib.request.Request(
            f"{KV_REST_URL}/get/{encoded_key}",
            method="GET",
            headers={"Authorization": f"Bearer {KV_REST_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            outer = json.loads(resp.read().decode("utf-8"))
            raw_val = outer.get("result")
            if raw_val is None:
                return None
            return json.loads(raw_val) if isinstance(raw_val, str) else raw_val
    except Exception:
        return None


def _kv_set_json(key: str, payload) -> None:
    if not HAS_CLOUD_KV:
        return
    try:
        encoded_key = urllib.parse.quote(key, safe="")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{KV_REST_URL}/set/{encoded_key}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {KV_REST_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urllib.request.urlopen(req, timeout=3.5):
            pass
    except Exception:
        pass


def ensure_data_files() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_file_lock(LEADERBOARD_FILE):
        if not os.path.exists(LEADERBOARD_FILE):
            seed_source = os.path.join(SEED_DATA_DIR, "leaderboard.json")
            initial_leaders = SEED_LEADERS
            if os.path.isfile(seed_source):
                try:
                    with open(seed_source, "r", encoding="utf-8") as sf:
                        loaded = json.load(sf)
                        if isinstance(loaded, list) and loaded:
                            initial_leaders = loaded
                except Exception:
                    pass
            tmp = f"{LEADERBOARD_FILE}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(initial_leaders, f, ensure_ascii=False, indent=2)
            os.replace(tmp, LEADERBOARD_FILE)


def read_json_file(filepath: str, default_val):
    if HAS_CLOUD_KV:
        cloud_val = _kv_get_json(_kv_key_for_file(filepath))
        if isinstance(cloud_val, type(default_val)):
            return cloud_val

    ensure_data_files()
    with get_file_lock(filepath):
        target = filepath
        if not os.path.exists(target):
            seed_fallback = os.path.join(SEED_DATA_DIR, os.path.basename(filepath))
            if os.path.isfile(seed_fallback):
                target = seed_fallback
            else:
                return default_val
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, type(default_val)) else default_val
        except Exception:
            return default_val


def write_json_file(filepath: str, payload) -> None:
    ensure_data_files()
    with get_file_lock(filepath):
        tmp = f"{filepath}.{secrets.token_hex(4)}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)
    if HAS_CLOUD_KV:
        _kv_set_json(_kv_key_for_file(filepath), payload)


def normalize_account_key(raw) -> str:
    val = str(raw or "").strip().lower()
    cleaned = "".join(ch for ch in val if ch.isalnum() or ch in "@._+-")
    return cleaned[:80]


def parse_request_json() -> dict:
    data = request.get_json(silent=True)
    if data is None:
        raw = request.get_data(cache=False, as_text=True)
        if not raw or not raw.strip():
            return {}
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _order_signing_key() -> bytes:
    secret = (
        os.environ.get("ORDER_SIGNING_SECRET")
        or os.environ.get("RAZORPAY_KEY_SECRET")
        or "arrow-puzzle-arena-v8-serverless-order-hmac"
    )
    return secret.encode("utf-8")


def sign_order_token(order_record: dict) -> str:
    compact = json.dumps(order_record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(compact).decode("ascii").rstrip("=")
    sig = hmac.new(_order_signing_key(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_order_token(token: str):
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(_order_signing_key(), b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        padded = b64 + "=" * (-len(b64) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return decoded if isinstance(decoded, dict) and decoded.get("orderId") else None
    except Exception:
        return None


def get_payment_config() -> dict:
    cfg = read_json_file(PAYMENT_CONFIG_FILE, {})
    env_merchant = os.environ.get("MERCHANT_NAME", "").strip()
    env_rzp_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    env_rzp_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    env_upi = os.environ.get("MERCHANT_UPI_ID", "").strip()
    env_pin = os.environ.get("MERCHANT_ADMIN_PIN", "").strip()

    # Protected by secret Admin PIN at all times (default "2580" until changed by Admin)
    effective_pin = str(cfg.get("adminPin") or env_pin or "2580").strip()[:24]

    return {
        "merchantName": str(cfg.get("merchantName") or env_merchant or "Arrow Puzzle: Arena").strip()[:48],
        "razorpayKeyId": str(cfg.get("razorpayKeyId") or env_rzp_id).strip()[:80],
        "razorpayKeySecret": str(cfg.get("razorpayKeySecret") or env_rzp_secret).strip()[:120],
        "upiId": str(cfg.get("upiId") or env_upi).strip()[:80],
        "adminPin": effective_pin,
        "updatedAt": cfg.get("updatedAt") or "",
    }


def _admin_signing_key(admin_pin: str) -> bytes:
    return _order_signing_key() + b":" + str(admin_pin or "2580").encode("utf-8")


def sign_admin_token(admin_pin: str) -> str:
    payload = {
        "role": "owner_admin",
        "exp": int(time.time()) + 86400,
        "nonce": secrets.token_hex(4),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_admin_signing_key(admin_pin), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_admin_token(token: str, admin_pin: str) -> bool:
    if not token or "." not in token:
        return False
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(_admin_signing_key(admin_pin), b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        padded = b64 + "=" * (-len(b64) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return decoded.get("role") == "owner_admin" and int(decoded.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def credit_order_to_account(order: dict, now_iso: str) -> dict:
    acct_key = normalize_account_key(order.get("accountKey"))
    gems_added = int(order.get("gems", 0) or 0)
    ord_id = str(order.get("orderId") or "").strip()

    with get_file_lock(ACCOUNTS_FILE):
        accounts = read_json_file(ACCOUNTS_FILE, {})
        acct = accounts.get(acct_key, {
            "accountKey": acct_key,
            "playerName": order.get("playerName") or "Player",
            "avatar": "🦊",
            "gems": 0,
            "purchasedGemsTotal": 0,
            "score": 0,
            "weeklyPoints": 0,
            "highestUnlocked": 1,
            "unlockedLevels": [1],
            "clearedLevels": [],
            "ownedThemes": ["cosmic"],
            "ownedArrowSkins": ["classic"],
            "ownedTrails": ["sparkles"],
            "creditedOrderIds": [],
            "creditedOrderGemsMap": {},
        })

        credited_ids = list(acct.get("creditedOrderIds") or [])
        credited_map = dict(acct.get("creditedOrderGemsMap") or {})

        if ord_id and ord_id not in credited_ids:
            acct["gems"] = int(acct.get("gems", 0) or 0) + gems_added
            acct["purchasedGemsTotal"] = int(acct.get("purchasedGemsTotal", 0) or 0) + gems_added
            credited_ids.append(ord_id)
            credited_map[ord_id] = gems_added

            owned_skins = set(acct.get("ownedArrowSkins") or ["classic"])
            owned_skins.update(order.get("bonusSkins") or [])
            acct["ownedArrowSkins"] = sorted(owned_skins)

            owned_trails = set(acct.get("ownedTrails") or ["sparkles"])
            owned_trails.update(order.get("bonusTrails") or [])
            acct["ownedTrails"] = sorted(owned_trails)

        acct["creditedOrderIds"] = credited_ids[-80:]
        acct["creditedOrderGemsMap"] = {k: credited_map[k] for k in acct["creditedOrderIds"] if k in credited_map}
        acct["updatedAt"] = now_iso
        accounts[acct_key] = acct
        write_json_file(ACCOUNTS_FILE, accounts)

    return acct


def build_admin_overview_payload() -> dict:
    cfg = get_payment_config()
    orders = read_json_file(ORDERS_FILE, [])

    pending = [o for o in orders if o.get("status") == "pending_approval"]
    pending.sort(key=lambda o: o.get("submittedAt") or o.get("createdAt") or "", reverse=True)

    history = [o for o in orders if o.get("status") in ("paid", "rejected")]
    history.sort(
        key=lambda o: o.get("paidAt") or o.get("rejectedAt") or o.get("createdAt") or "",
        reverse=True,
    )

    paid_orders = [o for o in orders if o.get("status") == "paid"]
    rejected_orders = [o for o in orders if o.get("status") == "rejected"]

    return {
        "ok": True,
        "stats": {
            "pendingCount": len(pending),
            "approvedCount": len(paid_orders),
            "rejectedCount": len(rejected_orders),
            "totalRevenueInr": sum(int(o.get("amountInr", 0) or 0) for o in paid_orders),
            "totalGemsSold": sum(int(o.get("gems", 0) or 0) for o in paid_orders),
        },
        "pendingOrders": pending[:60],
        "historyOrders": history[:60],
        "config": public_payment_config(cfg),
    }


def public_payment_config(cfg: dict) -> dict:
    has_rzp = bool(cfg["razorpayKeyId"] and cfg["razorpayKeySecret"])
    mode = (
        "live"
        if (has_rzp and cfg["razorpayKeyId"].startswith("rzp_live_"))
        else ("razorpay_test" if has_rzp else "manual_approval")
    )
    return {
        "merchantName": cfg["merchantName"],
        "razorpayKeyId": cfg["razorpayKeyId"],
        "hasRazorpayKeys": has_rzp,
        "mode": mode,
        "upiId": cfg["upiId"],
        "cardEnabled": True,
        "currency": "INR",
        "packs": list(GEM_PACK_CATALOG.values()),
    }


def sort_leaderboard(entries: list, category: str = "score") -> list:
    if category == "levels":
        key_fn = lambda x: (x.get("levelsCleared", 0), x.get("flawlessCount", 0), x.get("score", 0))
    elif category == "combo":
        key_fn = lambda x: (x.get("bestCombo", 0), x.get("score", 0))
    elif category == "weekly":
        key_fn = lambda x: (x.get("weeklyPoints", 0), x.get("score", 0))
    else:
        key_fn = lambda x: (x.get("score", 0), x.get("levelsCleared", 0))

    ordered = sorted(entries, key=key_fn, reverse=True)
    ranked = []
    for idx, item in enumerate(ordered, start=1):
        row = dict(item)
        row["rank"] = idx
        row["league"] = compute_league_tier(row.get("weeklyPoints", row.get("score", 0)))
        if not row.get("friendCode"):
            row["friendCode"] = make_friend_code(row.get("playerId", idx))
        ranked.append(row)
    return ranked


def sort_daily_arena(entries: list) -> list:
    ordered = sorted(entries, key=lambda x: (int(x.get("timeMs", 9999999)), int(x.get("mistakes", 0))))
    ranked = []
    for idx, item in enumerate(ordered, start=1):
        row = dict(item)
        row["rank"] = idx
        ranked.append(row)
    return ranked


def upsert_friend_directory(friends_db: dict, player_id: str, name, avatar, weekly_pts) -> str:
    directory = friends_db.setdefault("directory", {})
    for s in SEED_LEADERS:
        fc = s["friendCode"]
        if fc not in directory:
            directory[fc] = {
                "playerId": s["playerId"],
                "friendCode": fc,
                "name": s["name"],
                "avatar": s["avatar"],
                "league": compute_league_tier(s.get("weeklyPoints", 0)),
                "score": s["score"],
                "isBotRival": True,
            }
    my_code = make_friend_code(player_id)
    directory[my_code] = {
        "playerId": player_id,
        "friendCode": my_code,
        "name": (str(name or "Player").strip()[:24]) or "Player",
        "avatar": (str(avatar or "🦊").strip()[:8]) or "🦊",
        "league": compute_league_tier(weekly_pts),
        "score": int(weekly_pts or 0),
        "isBotRival": False,
        "updatedAt": utc_now_iso(),
    }
    return my_code


def create_razorpay_remote_order(key_id: str, key_secret: str, amount_paise: int, receipt_id: str, notes: dict) -> dict:
    payload = json.dumps({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt_id,
        "notes": notes,
    }).encode("utf-8")
    token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "ArrowPuzzleArena-Flask/8.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Vercel Serverless WSGI Path Normalizer Middleware
# ---------------------------------------------------------------------------

class VercelPathNormalizerMiddleware:
    """
    Guarantees consistent `/api/...` routing across Vercel Serverless rewrites,
    direct `/api/index.py` invocations, Gunicorn, and local Flask runs.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"

        if path in ("/api/index.py", "/api/index"):
            raw_uri = (
                environ.get("HTTP_X_FORWARDED_URI")
                or environ.get("REQUEST_URI")
                or environ.get("RAW_URI")
                or ""
            )
            parsed_path = urllib.parse.urlsplit(raw_uri).path if raw_uri else ""
            if parsed_path and parsed_path not in ("/api/index.py", "/api/index"):
                path = parsed_path
            else:
                path = "/api/health"
            environ["PATH_INFO"] = path

        elif not path.startswith("/api") and any(
            path == pref.rstrip("/") or path.startswith(pref) for pref in KNOWN_API_PREFIXES
        ):
            environ["PATH_INFO"] = f"/api{path}"

        return self.wsgi_app(environ, start_response)


# ---------------------------------------------------------------------------
# Flask Application Factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    flask_app = Flask(__name__, static_folder=None)
    flask_app.config["MAX_CONTENT_LENGTH"] = 128 * 1024  # 128 KB max JSON payload
    flask_app.json.ensure_ascii = False
    flask_app.json.sort_keys = False

    ensure_data_files()

    @flask_app.after_request
    def apply_arena_headers(response: Response) -> Response:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    @flask_app.route("/api", methods=["OPTIONS"])
    @flask_app.route("/api/<path:_any>", methods=["OPTIONS"])
    def api_cors_preflight(_any: str = ""):
        return ("", 204)

    @flask_app.errorhandler(Exception)
    def handle_unexpected_error(err: Exception):
        if isinstance(err, HTTPException):
            if request.path.startswith("/api"):
                return jsonify({"error": err.description or err.name}), err.code
            return err
        if request.path.startswith("/api"):
            return jsonify({"error": f"Server error: {str(err)[:160]}"}), 500
        return Response("Internal Server Error", status=500, mimetype="text/plain")

    # -----------------------------------------------------------------------
    # 1. Health & Vercel Diagnostics (/api & /api/health)
    # -----------------------------------------------------------------------
    @flask_app.get("/api")
    @flask_app.get("/api/health")
    def api_health():
        return jsonify({
            "ok": True,
            "status": "online",
            "game": "Arrow Puzzle: Arena",
            "engine": "Flask",
            "platform": "Vercel Serverless" if IS_VERCEL else "Local/WSGI",
            "storageMode": "cloud_kv" if HAS_CLOUD_KV else ("vercel_tmp" if IS_VERCEL else "local_disk"),
            "version": "8.0",
            "dateKey": current_date_key(),
            "weekKey": current_week_key(),
            "leagues": LEAGUE_TIERS,
            "packCount": len(GEM_PACK_CATALOG),
        })

    # -----------------------------------------------------------------------
    # 2. Global Leaderboard (/api/leaderboard)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/leaderboard")
    def api_leaderboard_get():
        category = request.args.get("category", "score")
        player_id = request.args.get("playerId", "")
        entries = read_json_file(LEADERBOARD_FILE, list(SEED_LEADERS))
        ranked = sort_leaderboard(entries, category)
        me = next((item for item in ranked if item.get("playerId") == player_id), None)
        return jsonify({
            "category": category,
            "leaders": ranked[:50],
            "me": me,
            "totalPlayers": len(ranked),
        })

    @flask_app.post("/api/leaderboard")
    def api_leaderboard_post():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        player_id = str(data.get("playerId", "")).strip()[:64]
        if not player_id:
            return jsonify({"error": "playerId is required"}), 400

        name = (str(data.get("name", "Player")).strip()[:24]) or "Player"
        avatar = (str(data.get("avatar", "🦊")).strip()[:8]) or "🦊"
        city = (str(data.get("city", "Global")).strip()[:28]) or "Global"
        score = max(0, min(9_999_999, int(data.get("score", 0) or 0)))
        weekly_points = max(0, min(9_999_999, int(data.get("weeklyPoints", score) or 0)))
        levels_cleared = max(0, min(10_000, int(data.get("levelsCleared", 0) or 0)))
        flawless_count = max(0, min(10_000, int(data.get("flawlessCount", 0) or 0)))
        best_combo = max(0, min(999, int(data.get("bestCombo", 0) or 0)))
        album_count = max(0, min(150, int(data.get("albumCount", 0) or 0)))
        now_iso = utc_now_iso()
        fcode = make_friend_code(player_id)

        with get_file_lock(LEADERBOARD_FILE):
            entries = read_json_file(LEADERBOARD_FILE, list(SEED_LEADERS))
            existing = next((e for e in entries if e.get("playerId") == player_id), None)
            if existing is not None:
                existing["friendCode"] = fcode
                existing["name"] = name
                existing["avatar"] = avatar
                existing["city"] = city
                existing["score"] = max(int(existing.get("score", 0)), score)
                existing["weeklyPoints"] = max(int(existing.get("weeklyPoints", 0)), weekly_points)
                existing["levelsCleared"] = max(int(existing.get("levelsCleared", 0)), levels_cleared)
                existing["flawlessCount"] = max(int(existing.get("flawlessCount", 0)), flawless_count)
                existing["bestCombo"] = max(int(existing.get("bestCombo", 0)), best_combo)
                existing["albumCount"] = max(int(existing.get("albumCount", 0)), album_count)
                existing["updatedAt"] = now_iso
            else:
                entries.append({
                    "playerId": player_id,
                    "friendCode": fcode,
                    "name": name,
                    "avatar": avatar,
                    "city": city,
                    "score": score,
                    "weeklyPoints": weekly_points,
                    "levelsCleared": levels_cleared,
                    "flawlessCount": flawless_count,
                    "bestCombo": best_combo,
                    "albumCount": album_count,
                    "updatedAt": now_iso,
                })
            write_json_file(LEADERBOARD_FILE, entries)

        ranked = sort_leaderboard(entries, "score")
        me = next((item for item in ranked if item.get("playerId") == player_id), None)
        return jsonify({
            "ok": True,
            "me": me,
            "leaders": ranked[:50],
            "totalPlayers": len(ranked),
        })

    # -----------------------------------------------------------------------
    # 3. Daily Arena Speedrun Leaderboard (/api/daily-arena)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/daily-arena")
    def api_daily_arena_get():
        date_key = (request.args.get("date") or current_date_key())[:12]
        player_id = request.args.get("playerId", "")
        all_days = read_json_file(DAILY_ARENA_FILE, {})
        day_entries = all_days.get(date_key)
        if not isinstance(day_entries, list):
            day_entries = list(SEED_DAILY_ARENA)
        ranked = sort_daily_arena(day_entries)
        me = next((item for item in ranked if item.get("playerId") == player_id), None)
        return jsonify({
            "dateKey": date_key,
            "leaders": ranked[:50],
            "me": me,
            "totalPlayers": len(ranked),
        })

    @flask_app.post("/api/daily-arena")
    def api_daily_arena_post():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        player_id = str(data.get("playerId", "")).strip()[:64]
        if not player_id:
            return jsonify({"error": "playerId is required"}), 400

        date_key = str(data.get("dateKey") or data.get("date") or current_date_key()).strip()[:12]
        time_ms = max(2000, min(3_600_000, int(data.get("timeMs", 999999) or 999999)))
        mistakes = max(0, min(99, int(data.get("mistakes", 0) or 0)))
        name = (str(data.get("name") or "Player").strip()[:24]) or "Player"
        avatar = (str(data.get("avatar") or "🦊").strip()[:8]) or "🦊"
        league = str(data.get("league") or "Bronze").strip()[:16]

        with get_file_lock(DAILY_ARENA_FILE):
            all_days = read_json_file(DAILY_ARENA_FILE, {})
            day_entries = all_days.get(date_key)
            if not isinstance(day_entries, list):
                day_entries = [dict(x) for x in SEED_DAILY_ARENA]

            existing = next((e for e in day_entries if e.get("playerId") == player_id), None)
            if existing is not None:
                if time_ms < int(existing.get("timeMs", 9999999)):
                    existing["timeMs"] = time_ms
                    existing["mistakes"] = mistakes
                existing["name"] = name
                existing["avatar"] = avatar
                existing["league"] = league
            else:
                day_entries.append({
                    "playerId": player_id,
                    "name": name,
                    "avatar": avatar,
                    "league": league,
                    "timeMs": time_ms,
                    "mistakes": mistakes,
                })

            all_days[date_key] = day_entries
            write_json_file(DAILY_ARENA_FILE, all_days)

        ranked = sort_daily_arena(day_entries)
        me = next((item for item in ranked if item.get("playerId") == player_id), None)
        return jsonify({
            "ok": True,
            "dateKey": date_key,
            "me": me,
            "yourRank": me["rank"] if me else None,
            "bestTimeMs": me["timeMs"] if me else time_ms,
            "leaders": ranked[:50],
        })

    # -----------------------------------------------------------------------
    # 4. Weekly Leagues (/api/weekly-league)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/weekly-league")
    def api_weekly_league_get():
        league_filter = (request.args.get("league") or "all").lower()
        player_id = request.args.get("playerId", "")
        entries = read_json_file(LEADERBOARD_FILE, list(SEED_LEADERS))
        ranked_all = sort_leaderboard(entries, "weekly")
        me = next((item for item in ranked_all if item.get("playerId") == player_id), None)

        if league_filter in ("bronze", "silver", "gold", "diamond", "legend"):
            filtered = [dict(r) for r in ranked_all if r["league"].lower() == league_filter]
            for i, r in enumerate(filtered, 1):
                r["leagueRank"] = i
        else:
            filtered = ranked_all

        return jsonify({
            "ok": True,
            "weekKey": current_week_key(),
            "leagueFilter": league_filter,
            "leagues": LEAGUE_TIERS,
            "leaders": filtered[:50],
            "me": me,
        })

    # -----------------------------------------------------------------------
    # 5. Friends System (/api/friends, /api/friends/register, /api/friends/add)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/friends")
    def api_friends_get():
        player_id = str(request.args.get("playerId", "")).strip()[:64]
        my_code = make_friend_code(player_id) if player_id else ""
        friends_db = read_json_file(FRIENDS_FILE, {"directory": {}, "links": {}})
        directory = dict(friends_db.get("directory", {}))
        links = friends_db.get("links", {})

        for s in SEED_LEADERS:
            fc = s["friendCode"]
            if fc not in directory:
                directory[fc] = {
                    "playerId": s["playerId"],
                    "friendCode": fc,
                    "name": s["name"],
                    "avatar": s["avatar"],
                    "league": compute_league_tier(s.get("weeklyPoints", 0)),
                    "score": s["score"],
                    "isBotRival": True,
                }

        my_friend_codes = links.get(my_code, ["ARENA-A1R9", "ARENA-M2Y7", "ARENA-K3B5"])
        friend_list = [directory[c] for c in my_friend_codes if c in directory and c != my_code]

        challenges = read_json_file(CHALLENGES_FILE, {})
        my_challenges = [
            ch for ch in challenges.values()
            if ch.get("hostCode") == my_code or ch.get("guestCode") in (my_code, "")
        ]
        my_challenges.sort(key=lambda c: c.get("createdAt", ""), reverse=True)

        return jsonify({
            "ok": True,
            "friendCode": my_code,
            "friends": friend_list[:40],
            "challenges": my_challenges[:12],
        })

    @flask_app.post("/api/friends/register")
    def api_friends_register():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        player_id = str(data.get("playerId", "")).strip()[:64]
        if not player_id:
            return jsonify({"error": "playerId required"}), 400

        with get_file_lock(FRIENDS_FILE):
            friends_db = read_json_file(FRIENDS_FILE, {"directory": {}, "links": {}})
            my_code = upsert_friend_directory(
                friends_db,
                player_id,
                data.get("name"),
                data.get("avatar"),
                data.get("weeklyPoints", 0),
            )
            links = friends_db.setdefault("links", {})
            if my_code not in links:
                links[my_code] = ["ARENA-A1R9", "ARENA-M2Y7", "ARENA-K3B5"]
            write_json_file(FRIENDS_FILE, friends_db)

        return jsonify({"ok": True, "friendCode": my_code})

    @flask_app.post("/api/friends/add")
    def api_friends_add():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        player_id = str(data.get("playerId", "")).strip()[:64]
        target_code = str(data.get("targetCode", "")).strip().upper()[:20]
        if not player_id or not target_code:
            return jsonify({"error": "Enter a valid Friend Code (e.g. ARENA-A1R9)"}), 400

        with get_file_lock(FRIENDS_FILE):
            friends_db = read_json_file(FRIENDS_FILE, {"directory": {}, "links": {}})
            my_code = upsert_friend_directory(
                friends_db,
                player_id,
                data.get("name"),
                data.get("avatar"),
                data.get("weeklyPoints", 0),
            )
            if target_code == my_code:
                return jsonify({"error": "That is your own Friend Code!"}), 400

            directory = friends_db.setdefault("directory", {})
            target_profile = directory.get(target_code)
            if not target_profile:
                fallback_name = str(data.get("targetName") or f"Friend {target_code[-4:]}").strip()[:24]
                target_profile = {
                    "playerId": f"ext-{target_code.lower()}",
                    "friendCode": target_code,
                    "name": fallback_name,
                    "avatar": "🐯",
                    "league": "Gold",
                    "score": 1200,
                    "isBotRival": False,
                }
                directory[target_code] = target_profile

            links = friends_db.setdefault("links", {})
            my_list = links.setdefault(my_code, ["ARENA-A1R9", "ARENA-M2Y7", "ARENA-K3B5"])
            if target_code not in my_list:
                my_list.insert(0, target_code)

            their_list = links.setdefault(target_code, [])
            if my_code not in their_list:
                their_list.insert(0, my_code)

            write_json_file(FRIENDS_FILE, friends_db)

        return jsonify({"ok": True, "friend": target_profile, "friendCode": my_code})

    # -----------------------------------------------------------------------
    # 6. Live 50/50 Split-Screen Friend Challenge Rooms (/api/challenges/*)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/challenges/state")
    def api_challenges_state():
        room_id = str(request.args.get("roomId", "")).strip()[:64]
        if not room_id:
            return jsonify({"error": "roomId is required"}), 400
        challenges = read_json_file(CHALLENGES_FILE, {})
        room = challenges.get(room_id)
        if not room:
            return jsonify({
                "ok": True,
                "room": {
                    "roomId": room_id,
                    "players": {},
                    "pendingSync": True,
                },
            })
        return jsonify({"ok": True, "room": room})

    @flask_app.post("/api/challenges/create")
    def api_challenges_create():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        host_id = str(data.get("playerId") or data.get("hostId") or "").strip()[:64]
        if not host_id:
            return jsonify({"error": "playerId required"}), 400

        room_id = str(data.get("roomId") or f"duel_{secrets.token_hex(4)}").strip()[:32]
        level_num = max(1, min(150, int(data.get("levelNum") or data.get("level") or 1)))
        is_daily = bool(data.get("isDailyArena", False))
        host_code = make_friend_code(host_id)
        now_iso = utc_now_iso()

        room = {
            "roomId": room_id,
            "levelNum": level_num,
            "isDailyArena": is_daily,
            "dateKey": current_date_key(),
            "hostId": host_id,
            "hostCode": host_code,
            "hostName": (str(data.get("hostName") or "Player").strip()[:24]) or "Player",
            "hostAvatar": (str(data.get("hostAvatar") or "🦊").strip()[:8]) or "🦊",
            "guestCode": str(data.get("guestCode") or "").strip().upper()[:20],
            "guestName": (str(data.get("guestName") or "Rival").strip()[:24]) or "Rival",
            "guestAvatar": (str(data.get("guestAvatar") or "🦅").strip()[:8]) or "🦅",
            "players": {},
            "createdAt": now_iso,
        }

        with get_file_lock(CHALLENGES_FILE):
            challenges = read_json_file(CHALLENGES_FILE, {})
            challenges[room_id] = room
            if len(challenges) > 120:
                sorted_keys = sorted(
                    challenges.keys(),
                    key=lambda k: challenges[k].get("createdAt", ""),
                    reverse=True,
                )[:100]
                challenges = {k: challenges[k] for k in sorted_keys}
            write_json_file(CHALLENGES_FILE, challenges)

        return jsonify({"ok": True, "room": room})

    @flask_app.post("/api/challenges/sync")
    def api_challenges_sync():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        room_id = str(data.get("roomId", "")).strip()[:32]
        player_id = str(data.get("playerId", "")).strip()[:64]
        if not room_id or not player_id:
            return jsonify({"error": "roomId and playerId required"}), 400

        with get_file_lock(CHALLENGES_FILE):
            challenges = read_json_file(CHALLENGES_FILE, {})
            room = challenges.get(room_id)
            if not room:
                room = {
                    "roomId": room_id,
                    "levelNum": max(1, min(150, int(data.get("levelNum", 1) or 1))),
                    "isDailyArena": bool(data.get("isDailyArena", False)),
                    "dateKey": current_date_key(),
                    "hostId": player_id,
                    "hostCode": make_friend_code(player_id),
                    "hostName": str(data.get("name") or "Player")[:24],
                    "hostAvatar": str(data.get("avatar") or "🦊")[:8],
                    "guestCode": "",
                    "guestName": "Friend",
                    "guestAvatar": "🐯",
                    "players": {},
                    "createdAt": utc_now_iso(),
                }
                challenges[room_id] = room

            players = room.setdefault("players", {})
            players[player_id] = {
                "playerId": player_id,
                "name": str(data.get("name") or "Player")[:24],
                "avatar": str(data.get("avatar") or "🦊")[:8],
                "clearedIds": [
                    int(x)
                    for x in (data.get("clearedIds") or [])[:160]
                    if isinstance(x, (int, float))
                ],
                "arrowsLeft": max(0, int(data.get("arrowsLeft", 0) or 0)),
                "mistakes": max(0, int(data.get("mistakes", 0) or 0)),
                "finished": bool(data.get("finished", False)),
                "finishTimeMs": int(data.get("finishTimeMs", 0) or 0),
                "updatedAt": utc_now_iso(),
            }
            write_json_file(CHALLENGES_FILE, challenges)

        return jsonify({"ok": True, "room": room})

    # -----------------------------------------------------------------------
    # 7. Player Account & Cloud Gem Wallet Sync (/api/account/*)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/account/lookup")
    def api_account_lookup():
        acct_key = normalize_account_key(request.args.get("accountKey", ""))
        if not acct_key:
            return jsonify({"error": "Missing accountKey"}), 400

        accounts = read_json_file(ACCOUNTS_FILE, {})
        acct = accounts.get(acct_key)
        orders = read_json_file(ORDERS_FILE, [])
        my_orders = [o for o in orders if o.get("accountKey") == acct_key]
        my_orders.sort(
            key=lambda o: o.get("paidAt") or o.get("rejectedAt") or o.get("submittedAt") or o.get("createdAt") or "",
            reverse=True,
        )
        return jsonify({
            "found": acct is not None,
            "account": acct,
            "orders": my_orders[:20],
        })

    @flask_app.post("/api/account/sync")
    def api_account_sync():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        acct_key = normalize_account_key(data.get("accountKey"))
        if not acct_key:
            return jsonify({"error": "accountKey (Email, Phone, or ID) is required"}), 400

        now_iso = utc_now_iso()

        with get_file_lock(ACCOUNTS_FILE):
            accounts = read_json_file(ACCOUNTS_FILE, {})
            existing = accounts.get(acct_key, {})

            client_gems = max(0, int(data.get("gems", 0) or 0))
            server_gems = max(0, int(existing.get("gems", 0) or 0))

            # Preserve any newly Admin-approved order gems that the client hasn't acknowledged yet
            server_credited_ids = list(existing.get("creditedOrderIds") or [])
            server_credited_map = dict(existing.get("creditedOrderGemsMap") or {})
            client_seen_ids = set(
                str(x) for x in (data.get("creditedOrderIds") or []) if isinstance(x, (str, int))
            )
            unseen_approved_gems = sum(
                int(server_credited_map.get(oid, 0) or 0)
                for oid in server_credited_ids
                if oid not in client_seen_ids
            )

            if data.get("forceClientGems") is True:
                merged_gems = client_gems + unseen_approved_gems
            else:
                merged_gems = max(client_gems + unseen_approved_gems, server_gems)

            def union_str_list(key: str, default_list: list) -> list:
                a = existing.get(key, default_list)
                b = data.get(key, default_list)
                s = set(a if isinstance(a, list) else default_list) | set(b if isinstance(b, list) else default_list)
                return sorted(str(x) for x in s)

            def union_int_list(key: str, default_list: list) -> list:
                a = existing.get(key, default_list)
                b = data.get(key, default_list)
                s = set()
                for item in (a if isinstance(a, list) else []) + (b if isinstance(b, list) else []):
                    try:
                        n = int(item)
                        if 1 <= n <= 150:
                            s.add(n)
                    except Exception:
                        pass
                if not s:
                    s.update(default_list)
                return sorted(s)

            merged_credited_ids = list(dict.fromkeys(server_credited_ids + sorted(client_seen_ids)))[-80:]

            record = {
                "accountKey": acct_key,
                "playerName": (str(data.get("playerName") or existing.get("playerName") or "Player").strip()[:24]),
                "avatar": (str(data.get("avatar") or existing.get("avatar") or "🦊").strip()[:8]),
                "email": str(data.get("email") or existing.get("email") or "").strip()[:80],
                "gems": merged_gems,
                "purchasedGemsTotal": max(
                    int(existing.get("purchasedGemsTotal", 0) or 0),
                    int(data.get("purchasedGemsTotal", 0) or 0),
                ),
                "score": max(int(existing.get("score", 0) or 0), int(data.get("score", 0) or 0)),
                "weeklyPoints": max(int(existing.get("weeklyPoints", 0) or 0), int(data.get("weeklyPoints", 0) or 0)),
                "highestUnlocked": max(
                    int(existing.get("highestUnlocked", 1) or 1),
                    int(data.get("highestUnlocked", 1) or 1),
                ),
                "unlockedLevels": union_int_list("unlockedLevels", [1]),
                "clearedLevels": union_int_list("clearedLevels", []),
                "ownedThemes": union_str_list("ownedThemes", ["cosmic"]),
                "ownedArrowSkins": union_str_list("ownedArrowSkins", ["classic"]),
                "ownedTrails": union_str_list("ownedTrails", ["sparkles"]),
                "creditedOrderIds": merged_credited_ids,
                "creditedOrderGemsMap": {
                    k: server_credited_map[k] for k in merged_credited_ids if k in server_credited_map
                },
                "updatedAt": now_iso,
            }
            accounts[acct_key] = record
            write_json_file(ACCOUNTS_FILE, accounts)

        return jsonify({"ok": True, "account": record})

    # -----------------------------------------------------------------------
    # 8. Payment Gateway: UPI UTR + Card + Razorpay (/api/payment/*)
    # -----------------------------------------------------------------------
    @flask_app.get("/api/payment/config")
    def api_payment_config_get():
        cfg = get_payment_config()
        return jsonify(public_payment_config(cfg))

    @flask_app.post("/api/payment/config")
    def api_payment_config_save():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        with get_file_lock(PAYMENT_CONFIG_FILE):
            current = get_payment_config()
            admin_token = (request.headers.get("X-Admin-Token") or str(data.get("adminToken") or "")).strip()
            supplied_pin = str(data.get("adminPin") or "").strip()

            token_ok = verify_admin_token(admin_token, current["adminPin"])
            pin_ok = bool(supplied_pin) and hmac.compare_digest(supplied_pin, current["adminPin"])
            if not (token_ok or pin_ok):
                return jsonify({"error": "Unauthorized — Admin authentication required"}), 403

            raw_new_pin = str(data.get("newAdminPin") or "").strip()[:24]
            new_pin = raw_new_pin if len(raw_new_pin) >= 4 else current["adminPin"]

            key_id = str(data.get("razorpayKeyId", current.get("razorpayKeyId", ""))).strip()[:80]
            key_secret_in = str(data.get("razorpayKeySecret", "")).strip()[:120]
            key_secret = key_secret_in if key_secret_in else str(current.get("razorpayKeySecret", ""))

            updated = {
                "merchantName": (
                    str(data.get("merchantName") or current.get("merchantName") or "Arrow Puzzle: Arena").strip()[:48]
                ),
                "razorpayKeyId": key_id,
                "razorpayKeySecret": key_secret,
                "upiId": str(data.get("upiId", current.get("upiId", ""))).strip()[:80],
                "adminPin": new_pin,
                "updatedAt": utc_now_iso(),
            }
            write_json_file(PAYMENT_CONFIG_FILE, updated)
            latest_cfg = get_payment_config()

        return jsonify({
            "ok": True,
            "config": public_payment_config(latest_cfg),
            "adminToken": sign_admin_token(latest_cfg["adminPin"]),
        })

    @flask_app.get("/api/payment/orders")
    def api_payment_orders_get():
        acct_key = normalize_account_key(request.args.get("accountKey", ""))
        orders = read_json_file(ORDERS_FILE, [])
        paid = [o for o in orders if o.get("status") == "paid"]
        if acct_key:
            paid = [o for o in paid if o.get("accountKey") == acct_key]
        paid.sort(key=lambda o: o.get("paidAt", ""), reverse=True)
        total_inr = sum(int(o.get("amountInr", 0)) for o in paid)
        total_gems = sum(int(o.get("gems", 0)) for o in paid)
        return jsonify({
            "orders": paid[:30],
            "totalOrders": len(paid),
            "totalRevenueInr": total_inr,
            "totalGemsSold": total_gems,
        })

    @flask_app.get("/api/payment/my-orders")
    def api_payment_my_orders():
        acct_key = normalize_account_key(request.args.get("accountKey", ""))
        if not acct_key:
            return jsonify({"error": "Missing accountKey"}), 400
        orders = read_json_file(ORDERS_FILE, [])
        mine = [
            o for o in orders
            if o.get("accountKey") == acct_key and o.get("status") in ("pending_approval", "paid", "rejected")
        ]
        mine.sort(
            key=lambda o: o.get("paidAt") or o.get("rejectedAt") or o.get("submittedAt") or o.get("createdAt") or "",
            reverse=True,
        )
        accounts = read_json_file(ACCOUNTS_FILE, {})
        acct = accounts.get(acct_key)
        return jsonify({
            "ok": True,
            "accountKey": acct_key,
            "orders": mine[:20],
            "account": acct,
        })

    @flask_app.post("/api/payment/create-order")
    def api_payment_create_order():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        pack_id = str(data.get("packId") or "").strip()
        pack = GEM_PACK_CATALOG.get(pack_id)
        if not pack:
            return jsonify({"error": "Invalid Gem Pack selected"}), 400

        acct_key = normalize_account_key(data.get("accountKey"))
        if not acct_key:
            return jsonify({
                "error": "Please specify which Account (Email / Phone / ID) should receive the Gems"
            }), 400

        preferred_channel = str(data.get("channel") or "").strip().lower()
        cfg = get_payment_config()
        amount_inr = int(pack["amountInr"])
        amount_paise = amount_inr * 100
        receipt_id = f"rcpt_{secrets.token_hex(5)}"
        now_iso = utc_now_iso()

        rzp_order_id = None
        gateway_mode = "manual_approval"

        if preferred_channel != "manual" and cfg["razorpayKeyId"] and cfg["razorpayKeySecret"]:
            try:
                remote = create_razorpay_remote_order(
                    cfg["razorpayKeyId"],
                    cfg["razorpayKeySecret"],
                    amount_paise,
                    receipt_id,
                    {"accountKey": acct_key, "packId": pack_id, "gems": str(pack["gems"])},
                )
                rzp_order_id = remote.get("id")
                gateway_mode = "razorpay"
            except urllib.error.HTTPError as e:
                err_txt = e.read().decode("utf-8", errors="ignore")
                return jsonify({"error": f"Razorpay API rejected keys: {err_txt[:140]}"}), 502
            except Exception as e:
                return jsonify({"error": f"Could not reach Razorpay API: {str(e)[:120]}"}), 502

        if not rzp_order_id:
            rzp_order_id = f"ord_{secrets.token_hex(6)}"

        order_record = {
            "orderId": rzp_order_id,
            "receiptId": receipt_id,
            "packId": pack["id"],
            "packTitle": pack["title"],
            "gems": pack["gems"],
            "bonusSkins": pack.get("bonusSkins", []),
            "bonusTrails": pack.get("bonusTrails", []),
            "amountInr": amount_inr,
            "amountPaise": amount_paise,
            "currency": "INR",
            "accountKey": acct_key,
            "playerName": str(data.get("playerName") or "Player").strip()[:24],
            "gatewayMode": gateway_mode,
            "status": "created",
            "createdAt": now_iso,
        }

        with get_file_lock(ORDERS_FILE):
            orders = read_json_file(ORDERS_FILE, [])
            orders.append(order_record)
            write_json_file(ORDERS_FILE, orders)

        return jsonify({
            "ok": True,
            "order": order_record,
            "orderToken": sign_order_token(order_record),
            "razorpayKeyId": cfg["razorpayKeyId"],
            "merchantName": cfg["merchantName"],
            "upiId": cfg["upiId"],
        })

    @flask_app.post("/api/payment/verify")
    def api_payment_verify():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        order_id = str(data.get("orderId") or "").strip()
        method = str(data.get("method") or "razorpay").strip()
        order_token = str(data.get("orderToken") or "").strip()
        now_iso = utc_now_iso()

        with get_file_lock(ORDERS_FILE):
            orders = read_json_file(ORDERS_FILE, [])
            order = next((o for o in orders if o.get("orderId") == order_id), None)

            # Serverless multi-instance recovery: verify HMAC-signed orderToken if `/tmp` changed instance
            if not order and order_token:
                recovered = verify_order_token(order_token)
                if recovered and recovered.get("orderId") == order_id:
                    order = dict(recovered)
                    orders.append(order)

            if not order:
                return jsonify({"error": "Order not found"}), 404

            if order.get("status") == "paid":
                return jsonify({"ok": True, "status": "paid", "alreadyCredited": True, "order": order})

            if order.get("status") == "pending_approval":
                return jsonify({
                    "ok": True,
                    "status": "pending_approval",
                    "pendingApproval": True,
                    "order": order,
                    "message": "Your payment is awaiting Admin verification.",
                })

            cfg = get_payment_config()
            payment_id = str(data.get("razorpayPaymentId") or "").strip()
            signature = str(data.get("razorpaySignature") or "").strip()
            upi_utr = str(data.get("upiUtr") or data.get("utr") or "").strip()

            # Mode A: Cryptographically verified Razorpay Payment (Card / UPI / Netbanking)
            if method == "razorpay":
                if not (payment_id and signature and cfg["razorpayKeySecret"]):
                    return jsonify({"error": "Missing Razorpay payment signature or Merchant Key Secret"}), 400
                msg = f"{order_id}|{payment_id}".encode("utf-8")
                expected = hmac.new(cfg["razorpayKeySecret"].encode("utf-8"), msg, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    return jsonify({"error": "Razorpay signature verification failed"}), 400

                order["status"] = "paid"
                order["paymentMethod"] = "razorpay"
                order["paymentId"] = payment_id
                order["paymentRef"] = f"RZP: {payment_id}"
                order["paidAt"] = now_iso
                write_json_file(ORDERS_FILE, orders)

            # Mode B: Direct UPI UTR Submission -> Goes to Admin Pending Verification Queue!
            elif method == "upi_direct":
                clean_utr = "".join(ch for ch in upi_utr if ch.isalnum()).upper()
                if len(clean_utr) < 10 or len(clean_utr) > 22:
                    return jsonify({"error": "Please enter a valid 12-digit UPI UTR / Reference number"}), 400

                # Prevent duplicate submission of an already-submitted or paid UTR
                duplicate = next(
                    (
                        o for o in orders
                        if o.get("orderId") != order_id
                        and o.get("upiUtr") == clean_utr
                        and o.get("status") in ("paid", "pending_approval")
                    ),
                    None,
                )
                if duplicate:
                    return jsonify({"error": "This UTR number has already been submitted for verification"}), 409

                order["status"] = "pending_approval"
                order["paymentMethod"] = "upi_direct"
                order["upiUtr"] = clean_utr
                order["paymentId"] = f"utr_{clean_utr[:20]}"
                order["paymentRef"] = f"UTR: {clean_utr}"
                order["submittedAt"] = now_iso
                write_json_file(ORDERS_FILE, orders)
                return jsonify({
                    "ok": True,
                    "status": "pending_approval",
                    "pendingApproval": True,
                    "order": order,
                    "message": "UTR submitted! Gems will be credited as soon as Admin approves your UTR.",
                })

            # Mode C: Card Payment Authorization Request -> Masked & Queued for Admin Approval!
            elif method == "card_manual":
                card_brand = "".join(
                    ch for ch in str(data.get("cardBrand") or "CARD").upper() if ch.isalnum()
                )[:14] or "CARD"
                card_last4 = "".join(ch for ch in str(data.get("cardLast4") or "") if ch.isdigit())[-4:]
                card_holder = str(data.get("cardHolder") or "").strip()[:36]
                card_expiry = str(data.get("cardExpiry") or "").strip()[:7]
                card_auth_ref = "".join(
                    ch for ch in str(data.get("cardAuthRef") or "").upper() if ch.isalnum()
                )[:20]

                if len(card_last4) != 4 or len(card_holder) < 2:
                    return jsonify({"error": "Please enter valid Card details and Cardholder Name"}), 400
                if len(card_auth_ref) < 4:
                    return jsonify({"error": "Please enter the Bank / Card Payment Auth or OTP Ref No."}), 400

                duplicate_ref = next(
                    (
                        o for o in orders
                        if o.get("orderId") != order_id
                        and o.get("cardAuthRef") == card_auth_ref
                        and o.get("status") in ("paid", "pending_approval")
                    ),
                    None,
                )
                if duplicate_ref:
                    return jsonify({"error": "This Card Reference number has already been submitted"}), 409

                masked_summary = f"{card_brand} •••• {card_last4} | Ref: {card_auth_ref}"
                order["status"] = "pending_approval"
                order["paymentMethod"] = "card"
                order["cardBrand"] = card_brand
                order["cardLast4"] = card_last4
                order["cardHolder"] = card_holder
                order["cardExpiry"] = card_expiry
                order["cardAuthRef"] = card_auth_ref
                order["paymentId"] = f"card_{card_last4}_{card_auth_ref[:12]}"
                order["paymentRef"] = masked_summary
                order["submittedAt"] = now_iso
                write_json_file(ORDERS_FILE, orders)
                return jsonify({
                    "ok": True,
                    "status": "pending_approval",
                    "pendingApproval": True,
                    "order": order,
                    "message": "Card payment submitted! Gems will be credited once Admin approves the transaction.",
                })

            else:
                return jsonify({"error": "Unsupported payment verification method"}), 400

        # Only cryptographically verified Razorpay reaches immediate auto-credit here
        acct = credit_order_to_account(order, now_iso)
        return jsonify({
            "ok": True,
            "status": "paid",
            "order": order,
            "account": acct,
            "gemsAdded": int(order["gems"]),
        })

    # -----------------------------------------------------------------------
    # 8B. Hidden Owner Admin Panel Endpoints (/api/admin/*)
    # -----------------------------------------------------------------------
    def _check_admin_request(body_dict=None) -> bool:
        cfg = get_payment_config()
        hdr_tok = (request.headers.get("X-Admin-Token") or "").strip()
        body_tok = str((body_dict or {}).get("adminToken") or "").strip()
        tok = hdr_tok or body_tok
        return verify_admin_token(tok, cfg["adminPin"])

    @flask_app.post("/api/admin/login")
    def api_admin_login():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        supplied_pin = str(data.get("pin") or "").strip()
        cfg = get_payment_config()
        if not supplied_pin or not hmac.compare_digest(supplied_pin, cfg["adminPin"]):
            return jsonify({"error": "Invalid Admin PIN"}), 403

        token = sign_admin_token(cfg["adminPin"])
        overview = build_admin_overview_payload()
        overview["adminToken"] = token
        return jsonify(overview)

    @flask_app.get("/api/admin/overview")
    def api_admin_overview():
        if not _check_admin_request():
            return jsonify({"error": "Admin session expired or unauthorized"}), 403
        return jsonify(build_admin_overview_payload())

    @flask_app.post("/api/admin/order-action")
    def api_admin_order_action():
        try:
            data = parse_request_json()
        except Exception:
            return jsonify({"error": "Invalid JSON payload"}), 400

        if not _check_admin_request(data):
            return jsonify({"error": "Unauthorized Admin request"}), 403

        order_id = str(data.get("orderId") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        reason = str(data.get("reason") or "").strip()[:120]

        if action not in ("approve", "reject"):
            return jsonify({"error": "Action must be 'approve' or 'reject'"}), 400

        now_iso = utc_now_iso()
        target_order = None

        with get_file_lock(ORDERS_FILE):
            orders = read_json_file(ORDERS_FILE, [])
            target_order = next((o for o in orders if o.get("orderId") == order_id), None)
            if not target_order:
                return jsonify({"error": "Order not found"}), 404

            if target_order.get("status") == "paid" and action == "reject":
                return jsonify({"error": "Order was already approved and credited"}), 409

            if action == "approve":
                target_order["status"] = "paid"
                target_order["paidAt"] = now_iso
                target_order["approvedBy"] = "admin"
                target_order.pop("rejectReason", None)
            else:
                target_order["status"] = "rejected"
                target_order["rejectedAt"] = now_iso
                target_order["rejectedBy"] = "admin"
                target_order["rejectReason"] = reason or "Invalid or unmatched UTR / payment reference"

            write_json_file(ORDERS_FILE, orders)

        credited_acct = None
        if action == "approve":
            credited_acct = credit_order_to_account(target_order, now_iso)

        resp = build_admin_overview_payload()
        resp["updatedOrder"] = target_order
        resp["creditedAccount"] = credited_acct
        return jsonify(resp)

    # Unknown /api/* route fallback -> clean JSON 404
    @flask_app.route("/api/<path:unknown_api>", methods=["GET", "POST"])
    def api_not_found(unknown_api: str):
        return jsonify({"error": f"API endpoint not found: /api/{unknown_api}"}), 404

    # -----------------------------------------------------------------------
    # 9. Static PWA Assets & Single-Page App Fallback (./public)
    # -----------------------------------------------------------------------
    @flask_app.route("/", defaults={"asset_path": ""}, methods=["GET", "HEAD"])
    @flask_app.route("/<path:asset_path>", methods=["GET", "HEAD"])
    def serve_game_assets(asset_path: str):
        rel = "index.html" if not asset_path else asset_path.lstrip("/")
        safe_norm = os.path.normpath(rel)
        if safe_norm.startswith(".."):
            return Response("Forbidden", status=403, mimetype="text/plain")

        candidate = os.path.join(PUBLIC_DIR, safe_norm)
        target_rel = safe_norm if os.path.isfile(candidate) else "index.html"
        ext = os.path.splitext(target_rel)[1].lower()
        mimetype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(target_rel)[0] or "application/octet-stream"
        return send_from_directory(PUBLIC_DIR, target_rel, mimetype=mimetype, max_age=0)

    flask_app.wsgi_app = VercelPathNormalizerMiddleware(flask_app.wsgi_app)
    return flask_app


# WSGI application instance (used by Vercel `api/index.py`, Gunicorn, and Flask CLI)
app = create_app()


if __name__ == "__main__":
    print(f"Arrow Puzzle: Arena Flask Server listening on http://{HOST}:{PORT}", flush=True)
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)
