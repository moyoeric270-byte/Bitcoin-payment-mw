"""
SupplierPay MW — Flask backend
==============================

Hackathon structural map
------------------------

    This file is deliberately split into FIVE labelled sections so the
    demo/mock behaviour is isolated, explainable, and dead-simple to
    toggle off in production:

      §0  Config              — env vars, constants
      §1  Shared helpers      — used by BOTH real and demo paths
      §2  Real MavaPay API    — the production code path (network calls)
      §3  Demo backend        — the in-memory mock (pitch / offline path)
      §4  Flask routes        — thin controllers that pick §2 or §3
      §5  Entry point

    The one knob that decides which of §2 or §3 runs is is_demo_mode()
    in §1. Every route in §4 delegates to a `real_*` or `demo_*`
    function via that decision. No route contains business logic.
"""

import base64
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 0 — CONFIG
# ═══════════════════════════════════════════════════════════════════════════
#
#  Nothing dynamic here. Change behaviour via .env, never by editing code.
# ═══════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

MAVAPAY_API_KEY  = os.getenv("MAVAPAY_API_KEY", "").strip()
MAVAPAY_BASE_URL = os.getenv("MAVAPAY_BASE_URL", "https://mavapay.co").rstrip("/")
PAYOUTS_PATH     = Path("payouts.json")
REQUEST_TIMEOUT  = 15

# Any upstream state string that means "money has moved". We normalize all
# of these to the single string "complete" before the frontend sees them,
# so index.html only ever compares against one value.
COMPLETE_STATES = {"complete", "completed", "paid", "success", "settled"}


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 1 — SHARED HELPERS  (used by both real and demo paths)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Anything in this section must be safe to call regardless of which
#  backend is active. No network, no demo-specific state.
# ═══════════════════════════════════════════════════════════════════════════


# ⭐ THE DEMO SWITCH ⭐
# ────────────────────────────────────────────────────────────────────────────
#   This is the ONLY function that decides real vs demo. It returns True
#   when either:
#     (a) the caller explicitly passed demo=true in the request  → per-request
#         override, useful when a key is present but you want to demo anyway
#     (b) no valid MAVAPAY_API_KEY is configured                  → the app
#         physically cannot call the real API, so demo is the only path
#   Everything downstream branches on this. No other function in the file
#   checks MAVAPAY_API_KEY.
# ────────────────────────────────────────────────────────────────────────────
def is_demo_mode(req_demo_flag: bool = False) -> bool:
    """Return True if this request should use the demo backend."""
    has_real_key = bool(MAVAPAY_API_KEY) and MAVAPAY_API_KEY != "your_mavapay_staging_key_here"
    return req_demo_flag or not has_real_key


def generate_qr_base64(data: str) -> str:
    """Encode a Lightning invoice string into a base64 PNG for <img src=data:...>."""
    if not data:
        return ""
    qr_image = qrcode.make(data)
    buffer = io.BytesIO()
    qr_image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ── Payout ledger (JSON file on disk) ──────────────────────────────────────

# Idempotency guard: a hash that has already been logged must never be
# written again, even if the frontend polls /api/status many times after
# settlement or if Flask restarts (in-memory set + on-disk duplicate check).
_LOGGED_HASHES: set[str] = set()
_LOGGED_LOCK = threading.Lock()


def read_payouts() -> list[dict[str, Any]]:
    """Load completed payouts from disk. Tolerates a missing or corrupt file."""
    if not PAYOUTS_PATH.exists():
        return []
    try:
        return json.loads(PAYOUTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def record_payout(entry: dict[str, Any]) -> None:
    """Persist a completed payout exactly once, thread-safely."""
    tx_hash = entry.get("tx_hash")
    if not tx_hash:
        return

    with _LOGGED_LOCK:
        if tx_hash in _LOGGED_HASHES:
            return

        payouts = read_payouts()
        if any(item.get("tx_hash") == tx_hash for item in payouts):
            _LOGGED_HASHES.add(tx_hash)
            return

        payouts.insert(0, entry)
        PAYOUTS_PATH.write_text(json.dumps(payouts, indent=2), encoding="utf-8")
        _LOGGED_HASHES.add(tx_hash)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a stored value to int, tolerating '' / None / junk from the URL."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 2 — REAL MAVAPAY BACKEND  (production path)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Every function below makes an HTTPS call to MavaPay's staging or
#  production API. Reached only when is_demo_mode() returns False.
#
#  Contract: every function returns (dict, int) — (response_body, http_status).
#  §3 demo_* functions return the EXACT SAME shape, so §4 routes are
#  identical regardless of which backend ran.
# ═══════════════════════════════════════════════════════════════════════════


def mavapay_request(method: str, endpoint: str, **kwargs: Any) -> tuple[dict[str, Any], int]:
    """Send one authenticated request to MavaPay; normalize errors to a tuple."""
    headers = {
        "X-API-KEY": MAVAPAY_API_KEY,
        "Content-Type": "application/json",
    }
    url = f"{MAVAPAY_BASE_URL}/{endpoint.lstrip('/')}"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        return response.json(), response.status_code
    except requests.RequestException as exc:
        return {"error": f"MavaPay connection failed: {exc}"}, 502
    except ValueError:
        return {"error": "Invalid JSON response from MavaPay"}, 502


def real_banks(currency: str) -> tuple[dict[str, Any], int]:
    """GET /banks — supported payout banks for a target fiat currency."""
    return mavapay_request("GET", "/banks", params={"currency": currency})


def real_verify_account(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /verify-account — confirm a supplier's bank details."""
    payload = {
        "bankCode": body.get("bankCode"),
        "accountNumber": body.get("accountNumber"),
        "currency": body.get("currency", "ZAR").upper(),
    }
    return mavapay_request("POST", "/verify-account", json=payload)


def real_quote(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /quote — request an auto-payout off-ramp quote + Lightning invoice."""
    payload = {
        "amount": float(body.get("amount", 0)),
        "currency": body.get("currency", "ZAR").upper(),
        "autopayout": True,
        "beneficiary": {
            "bankCode": body.get("bankCode"),
            "accountNumber": body.get("accountNumber"),
            "accountName": body.get("accountName", "Supplier"),
        },
    }

    res, status = mavapay_request("POST", "/quote", json=payload)
    if status >= 400:
        return res, status

    quote = res.get("data", res)
    invoice = quote.get("invoice") or quote.get("paymentRequest", "")

    # Normalize to the same envelope as demo_quote() so §4 doesn't care.
    return {
        "demo": False,
        "invoice": invoice,
        "qr_base64": generate_qr_base64(invoice),
        "exchangeRate": quote.get("exchangeRate"),
        "amountInSats": quote.get("amountInSats") or quote.get("sats"),
        "hash": quote.get("hash") or quote.get("id"),
        "raw": quote,
    }, 200


def real_status(tx_hash: str) -> tuple[dict[str, Any], int]:
    """GET /transaction/{hash} — poll a live payout state."""
    res, status = mavapay_request("GET", f"/transaction/{tx_hash}")
    if status >= 400:
        return res, status

    tx_data = res.get("data", res)
    raw_state = (tx_data.get("state") or tx_data.get("status") or "pending").lower()
    normalized = "complete" if raw_state in COMPLETE_STATES else raw_state
    return {"state": normalized}, 200


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 3 — DEMO BACKEND  (mock — pitch / offline path)
# ═══════════════════════════════════════════════════════════════════════════
#
#  In-memory mock of MavaPay. No network calls, no real money, no API key.
#  Used when is_demo_mode() returns True, and ONLY then.
#
#  Every function returns the same (dict, int) envelope as its §2 twin so
#  §4 routes are byte-identical regardless of backend.
# ═══════════════════════════════════════════════════════════════════════════

# Per-process store of demo transactions. Cleared on every Flask restart —
# demo_status() treats unknown hashes as 404, so a stale tab can never
# fabricate a "complete" and poison the ledger.
DEMO_TXS: dict[str, dict[str, Any]] = {}

DEMO_BANKS: dict[str, list[dict[str, str]]] = {
    "ZAR": [
        {"code": "FNB",     "name": "First National Bank (FNB)"},
        {"code": "ABSA",    "name": "Absa Bank South Africa"},
        {"code": "SBSA",    "name": "Standard Bank SA"},
        {"code": "CAPITEC", "name": "Capitec Bank"},
    ],
    "KES": [
        {"code": "EQBL",  "name": "Equity Bank Kenya"},
        {"code": "KCBL",  "name": "KCB Bank Kenya"},
        {"code": "MPESA", "name": "M-Pesa Paybill / Till"},
    ],
    "NGN": [
        {"code": "GTB",    "name": "Guaranty Trust Bank (GTBank)"},
        {"code": "ACCESS", "name": "Access Bank Nigeria"},
        {"code": "ZENITH", "name": "Zenith Bank"},
    ],
}

# Sats per unit of supplier fiat. Tuned so a 500 ZAR demo quote reads as a
# realistic ~1.55M sats.
DEMO_RATES: dict[str, int] = {"ZAR": 3100, "KES": 430, "NGN": 35}

# After demo_mark_paid() sets a quote to "processing", it takes this many
# /api/status polls (2 × ~2 s) to flip to "complete". Makes the
# PROCESSING → COMPLETE transition visible in the UI instead of instant.
DEMO_AUTO_COMPLETE_POLLS = 2


def demo_banks(currency: str) -> tuple[dict[str, Any], int]:
    """Return mock bank list for a fiat currency."""
    return {"data": DEMO_BANKS.get(currency, DEMO_BANKS["ZAR"])}, 200


def demo_verify_account(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a deterministic fake supplier name for any plausible account."""
    acct = body.get("accountNumber", "0000")
    bank = body.get("bankCode", "BANK")
    return {"data": {"accountName": f"Joburg Wholesale Supplies ({bank}-{acct[-4:]})"}}, 200


def demo_quote(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Create an in-memory 'quote' with a fake BOLT11 invoice and real QR code."""
    amount = float(body.get("amount", 0))
    currency = body.get("currency", "ZAR").upper()
    rate = DEMO_RATES.get(currency, 3000)

    # round() so 500 ZAR at 3100 sats/ZAR → 1_550_000, not 1_549_999.
    sats = int(round(amount * rate))
    tx_hash = f"demo_{uuid.uuid4().hex[:16]}"
    invoice = f"lnbcrt{sats}n1p0demo0supplierpay0mw0{tx_hash}"

    DEMO_TXS[tx_hash] = {"state": "pending", "polls": 0}

    return {
        "demo": True,
        "invoice": invoice,
        "qr_base64": generate_qr_base64(invoice),
        "exchangeRate": rate,
        "amountInSats": sats,
        "hash": tx_hash,
    }, 200


def demo_status(tx_hash: str) -> tuple[dict[str, Any], int]:
    """Poll a demo transaction. Unknown hash → 404 (never fabricated 'complete')."""
    tx = DEMO_TXS.get(tx_hash)
    if tx is None:
        # Stale tab, or Flask restarted and wiped in-memory state. Honest
        # answer is "unknown", not "complete" — otherwise the ledger grows
        # phantom rows on every restart.
        return {"state": "unknown"}, 404

    if tx["state"] == "processing":
        tx["polls"] += 1
        if tx["polls"] >= DEMO_AUTO_COMPLETE_POLLS:
            tx["state"] = "complete"

    return {"state": tx["state"]}, 200


def demo_mark_paid(tx_hash: str) -> bool:
    """Flag a demo quote as 'processing' (user clicked Simulate Payment)."""
    tx = DEMO_TXS.get(tx_hash)
    if tx is None:
        return False
    tx["state"] = "processing"
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 4 — FLASK ROUTES  (thin controllers)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Every route follows the same shape:
#
#      1. Parse input.
#      2. Pick demo_* or real_* via is_demo_mode() (§1).
#      3. Call the backend — it returns (dict, int).
#      4. jsonify and return.
#
#  No business logic lives here. If a route grows beyond ~10 lines, the
#  logic belongs in §2 or §3, not here.
# ═══════════════════════════════════════════════════════════════════════════


# ── Page routes ────────────────────────────────────────────────────────────

@app.route("/")
def route_index():
    """Render the supplier payment portal."""
    return render_template("index.html", default_demo=is_demo_mode(False))


@app.route("/ledger")
def route_ledger():
    """Render the merchant ledger and completed payout receipts."""
    payouts = read_payouts()
    total_sats = sum(_safe_int(p.get("amount_sats", 0)) for p in payouts)
    return render_template("ledger.html", payouts=payouts, total_sats=total_sats)


# ── API routes ─────────────────────────────────────────────────────────────

@app.route("/api/banks")
def route_banks():
    """GET /api/banks?currency=ZAR[&demo=true] — supported payout banks."""
    currency = request.args.get("currency", "ZAR").upper()
    wants_demo = is_demo_mode(request.args.get("demo") == "true")
    backend = demo_banks if wants_demo else real_banks
    payload, status = backend(currency)
    return jsonify(payload), status


@app.route("/api/verify-account", methods=["POST"])
def route_verify_account():
    """POST /api/verify-account — confirm a supplier's bank details."""
    body = request.get_json(silent=True) or {}
    wants_demo = is_demo_mode(bool(body.get("demo")))
    backend = demo_verify_account if wants_demo else real_verify_account
    payload, status = backend(body)
    return jsonify(payload), status


@app.route("/api/quote", methods=["POST"])
def route_quote():
    """POST /api/quote — generate a Lightning invoice for a supplier payout."""
    body = request.get_json(silent=True) or {}
    wants_demo = is_demo_mode(bool(body.get("demo")))
    backend = demo_quote if wants_demo else real_quote
    payload, status = backend(body)
    return jsonify(payload), status


@app.route("/api/demo/pay/<tx_hash>", methods=["POST"])
def route_demo_pay(tx_hash: str):
    """Simulate paying a demo Lightning invoice.

    Structural safety: this endpoint only succeeds if tx_hash exists in
    DEMO_TXS, which can ONLY be populated by demo_quote() (§3). A real
    production deployment never calls demo_quote(), so DEMO_TXS stays
    empty and this endpoint returns 404 for every possible input.
    """
    if demo_mark_paid(tx_hash):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown demo hash"}), 404


@app.route("/api/status/<tx_hash>")
def route_status(tx_hash: str):
    """Poll transaction state; log a payout exactly once on settlement.

    The real/demo split here is by hash prefix, not by mode flag: a demo
    hash should never be looked up against the real API, and a real hash
    should never touch the demo store. Prefix is the natural discriminator.
    """
    if tx_hash.startswith("demo_"):
        payload, status = demo_status(tx_hash)
    else:
        payload, status = real_status(tx_hash)

    if status == 200 and payload.get("state") == "complete":
        record_payout({
            "tx_hash": tx_hash,
            "supplier": request.args.get("supplier", "Supplier"),
            "currency": request.args.get("currency", "ZAR"),
            "amount_fiat": request.args.get("amount_fiat", "0"),
            "amount_sats": request.args.get("amount_sats", 0),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })

    return jsonify(payload), status


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 5 — ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", 5001)),
        # Never hardcode debug=True — it enables Werkzeug's interactive
        # debugger, which is RCE if ever exposed. Opt-in via .env.
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )