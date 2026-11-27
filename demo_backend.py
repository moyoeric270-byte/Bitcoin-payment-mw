"""
SupplierPay MW — Demo / mock backend.

Self-contained in-memory mock of the MavaPay API. Used only when
is_demo_mode() in app.py returns True.

Design rules
------------
  • No imports from app.py. Delete this file and app.py still boots in
    real-API mode — the graceful ImportError in app.py handles it.
  • No network calls, no API keys, no persistent state outside this file.
  • Every public function returns the same (dict, int) tuple that the
    real_* functions in app.py return, so app.py's routes are identical
    regardless of which backend ran.
"""

import base64
import io
import secrets
import uuid
from typing import Any

import qrcode


# ── Config ─────────────────────────────────────────────────────────────────

# Sats per unit of supplier fiat. Tuned so a 500 ZAR demo quote reads as
# a realistic ~1.55M sats.
RATES: dict[str, int] = {"ZAR": 3100, "KES": 430, "NGN": 35}

# After mark_paid() sets a quote to "processing", it takes this many
# /api/status polls (2 × ~2 s) to flip to "complete". Makes the
# PROCESSING → COMPLETE transition visible in the UI instead of instant.
AUTO_COMPLETE_POLLS = 2

BANKS: dict[str, list[dict[str, str]]] = {
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

# Bech32-safe alphabet. Keeps the mock invoice decodable by lncli even
# though it's not payable — the checksum will fail, not the charset.
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# Per-process store of demo transactions. Cleared on every Flask restart.
# status() treats unknown hashes as 404, so a stale browser tab can never
# fabricate a "complete" and poison the ledger.
_TXS: dict[str, dict[str, Any]] = {}


# ── Internal helpers ───────────────────────────────────────────────────────

def _qr_base64(data: str) -> str:
    """Encode an invoice string into a base64 PNG for <img src=data:...>."""
    if not data:
        return ""
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _fake_invoice(sats: int) -> str:
    """Return a bech32-shaped (but not payable) BOLT11-style string."""
    payload = "".join(secrets.choice(_BECH32) for _ in range(160))
    return f"lnbcrt{sats}n1{payload}"


# ── Public surface — mirrors app.py's real_* functions ────────────────────

def banks(currency: str) -> tuple[dict[str, Any], int]:
    """Return the mock bank list for a fiat currency."""
    return {"data": BANKS.get(currency, BANKS["ZAR"])}, 200


def verify_account(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a deterministic fake supplier name for any plausible account."""
    acct = body.get("accountNumber", "0000")
    bank = body.get("bankCode", "BANK")
    return {"data": {"accountName": f"Joburg Wholesale Supplies ({bank}-{acct[-4:]})"}}, 200


def quote(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Create an in-memory 'quote' with a fake BOLT11 invoice and real QR code."""
    amount = float(body.get("amount", 0))
    currency = body.get("currency", "ZAR").upper()
    rate = RATES.get(currency, 3000)

    # round() so 500 ZAR at 3100 sats/ZAR → 1_550_000, not 1_549_999.
    sats = int(round(amount * rate))
    tx_hash = f"demo_{uuid.uuid4().hex[:16]}"
    invoice = _fake_invoice(sats)

    _TXS[tx_hash] = {"state": "pending", "polls": 0}

    return {
        "demo": True,
        "invoice": invoice,
        "qr_base64": _qr_base64(invoice),
        "exchangeRate": rate,
        "amountInSats": sats,
        "hash": tx_hash,
    }, 200


def status(tx_hash: str) -> tuple[dict[str, Any], int]:
    """Poll a demo transaction. Unknown hash → 404 (never fabricated)."""
    tx = _TXS.get(tx_hash)
    if tx is None:
        # Stale tab, or Flask restarted and wiped in-memory state.
        # Honest answer is "unknown", not "complete".
        return {"state": "unknown"}, 404

    if tx["state"] == "processing":
        tx["polls"] += 1
        if tx["polls"] >= AUTO_COMPLETE_POLLS:
            tx["state"] = "complete"

    return {"state": tx["state"]}, 200


def mark_paid(tx_hash: str) -> bool:
    """Flag a demo quote as 'processing' (user clicked Simulate Payment)."""
    tx = _TXS.get(tx_hash)
    if tx is None:
        return False
    tx["state"] = "processing"
    return True