"""
SupplierPay MW — Flask backend.

Structural map
--------------
    This file contains the config, shared helpers, the real MavaPay
    backend, and the Flask routes. The demo / mock backend lives in
    demo_backend.py — a fully self-contained module.

    is_demo_mode() in §1 is the ONLY thing that decides which backend
    runs. Every route in §3 delegates to either a real_* function
    (this file) or a demo_backend.* function (that file).

    Delete demo_backend.py and this app still boots — the import below
    fails gracefully, is_demo_mode() returns False, and every request
    hits MavaPay. Zero other changes.

    §0  Config
    §1  Shared helpers       — used by both backends
    §2  Real MavaPay backend — production path (network calls)
    §3  Flask routes         — thin controllers, pick backend per-request
    §4  Entry point
"""

import base64
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Optional import: deleting demo_backend.py disables demo mode gracefully.
try:
    import demo_backend
    DEMO_AVAILABLE = True
except ImportError:
    demo_backend = None  # type: ignore[assignment]
    DEMO_AVAILABLE = False

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 0 — CONFIG
# ═══════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

MAVAPAY_API_KEY  = os.getenv("MAVAPAY_API_KEY", "").strip()
MAVAPAY_BASE_URL = os.getenv("MAVAPAY_BASE_URL", "https://mavapay.co").rstrip("/")
PAYOUTS_PATH     = Path("payouts.json")
REQUEST_TIMEOUT  = 15

# Any upstream state string that means "money has moved". Normalized to
# the single string "complete" before the frontend sees them, so
# index.html only compares against one value.
COMPLETE_STATES = {"complete", "completed", "paid", "success", "settled"}


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 1 — SHARED HELPERS  (used by both real and demo paths)
# ═══════════════════════════════════════════════════════════════════════════


# ⭐ THE DEMO SWITCH ⭐
# ────────────────────────────────────────────────────────────────────────────
#   The ONLY function in the codebase that decides real vs demo.
#   Returns True when either:
#     (a) the caller explicitly passed demo=true      → per-request override
#     (b) no valid MAVAPAY_API_KEY is configured      → cannot call real API
#     (c) demo_backend.py was deleted from the repo   → falls back to real
# ────────────────────────────────────────────────────────────────────────────
def is_demo_mode(req_demo_flag: bool = False) -> bool:
    """Return True if this request should use the demo backend."""
    if not DEMO_AVAILABLE:
        return False
    has_real_key = bool(MAVAPAY_API_KEY) and MAVAPAY_API_KEY != "your_mavapay_staging_key_here"
    return req_demo_flag or not has_real_key


def generate_qr_base64(data: str) -> str:
    """Encode an invoice string into a base64 PNG for <img src=data:...>."""
    if not data:
        return ""
    qr_image = qrcode.make(data)
    buffer = io.BytesIO()
    qr_image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ── Payout ledger (JSON file on disk) ──────────────────────────────────────

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
#  Every function here makes an HTTPS call to MavaPay. Same (dict, int)
#  contract as demo_backend's functions, so §3 routes don't care which
#  backend ran.
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

    # Normalize to the same envelope demo_backend.quote() returns so §3
    # routes don't care which backend ran.
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
#  SECTION 3 — FLASK ROUTES  (thin controllers)
# ═══════════════════════════════════════════════════════════════════════════
#
#  Every route follows the same shape:
#      1. Parse input.
#      2. Pick demo_backend.* or real_* via is_demo_mode() (§1).
#      3. Call the backend — it returns (dict, int).
#      4. jsonify and return.
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
    backend = demo_backend.banks if wants_demo else real_banks
    payload, status = backend(currency)
    return jsonify(payload), status


@app.route("/api/verify-account", methods=["POST"])
def route_verify_account():
    """POST /api/verify-account — confirm a supplier's bank details."""
    body = request.get_json(silent=True) or {}
    wants_demo = is_demo_mode(bool(body.get("demo")))
    backend = demo_backend.verify_account if wants_demo else real_verify_account
    payload, status = backend(body)
    return jsonify(payload), status


@app.route("/api/quote", methods=["POST"])
def route_quote():
    """POST /api/quote — generate a Lightning invoice for a supplier payout."""
    body = request.get_json(silent=True) or {}
    wants_demo = is_demo_mode(bool(body.get("demo")))
    backend = demo_backend.quote if wants_demo else real_quote
    payload, status = backend(body)
    return jsonify(payload), status


@app.route("/api/demo/pay/<tx_hash>", methods=["POST"])
def route_demo_pay(tx_hash: str):
    """Simulate paying a demo Lightning invoice.

    Structurally safe: only succeeds if tx_hash is in demo_backend._TXS,
    which can only be populated by demo_backend.quote(). A real production
    deployment never calls demo quote, so this returns 404 for every input.
    """
    if not DEMO_AVAILABLE:
        return jsonify({"error": "not found"}), 404
    if demo_backend.mark_paid(tx_hash):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown demo hash"}), 404


@app.route("/api/status/<tx_hash>")
def route_status(tx_hash: str):
    """Poll transaction state; log a payout exactly once on settlement."""
    if tx_hash.startswith("demo_"):
        if not DEMO_AVAILABLE:
            return jsonify({"error": "not found"}), 404
        payload, status = demo_backend.status(tx_hash)
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
#  SECTION 4 — ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", 5001)),
        # Never hardcode debug=True — it enables Werkzeug's interactive
        # debugger, which is RCE if ever exposed. Opt-in via .env.
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )