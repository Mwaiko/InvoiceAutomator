"""
app.py  –  Flask web UI for KRA eTIMS GRN submission.

Run:
    cd Etims
    python app.py

Then open http://localhost:5000
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

# ── Make sure the Etims package directory is on the path ─────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from fill_kra import EtimsConfig, KraError, grn_to_receipt, run_fill
from read_salesReceipt import read_Grn

# ─────────────────────────────────────────────────────────────────────────────
# Point Flask at the templates/ folder sitting next to this file,
# regardless of where the process is launched from.
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.urandom(24)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# Hard-coded credentials (same as main.py) — move to env-vars for production
DEFAULT_CFG = EtimsConfig(
    pin      = os.environ.get("KRA_PIN",      "P051621945B"),
    username = os.environ.get("KRA_USERNAME",  "P051621945B"),
    password = os.environ.get("KRA_PASSWORD",  "Nairobi@2025"),
)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/parse", methods=["POST"])
def parse():
    """Upload a GRN file, parse it, return the parsed data as JSON."""
    file = request.files.get("grn_file")
    if not file or file.filename == "":
        return jsonify({"error": "No file selected."}), 400

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {suffix}"}), 400

    # Save to a temp file so our parsers can read it by path
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        grn = read_Grn(tmp_path)
    except Exception as exc:
        log.exception("Parse error")
        return jsonify({"error": f"Parse failed: {exc}"}), 500
    finally:
        os.unlink(tmp_path)

    # Store GRN in server-side session so /submit can use it
    session["grn"] = grn
    session["filename"] = file.filename

    return jsonify({"ok": True, "grn": grn})


@app.route("/submit", methods=["POST"])
def submit():
    """Build ReceiptHeader from session GRN + user-supplied fields, submit to KRA."""
    grn = session.get("grn")
    if not grn:
        return jsonify({"error": "Session expired — please re-upload the GRN."}), 400

    # Overlay the user-supplied non-fiscal fields
    grn["invoice_no"] = request.form.get("invoice_no", "").strip()
    grn["store_no"]   = request.form.get("store_no",   "").strip()

    # Override credentials if the user changed them in the UI
    cfg = EtimsConfig(
        pin      = request.form.get("kra_pin",      DEFAULT_CFG.pin).strip()      or DEFAULT_CFG.pin,
        username = request.form.get("kra_username", DEFAULT_CFG.username).strip() or DEFAULT_CFG.username,
        password = request.form.get("kra_password", DEFAULT_CFG.password).strip() or DEFAULT_CFG.password,
    )

    try:
        header = grn_to_receipt(grn, cfg)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422

    try:
        results = run_fill(cfg, header)
    except Exception as exc:
        log.exception("KRA submission error")
        return jsonify({"error": str(exc)}), 500

    ok  = all(r.get("status") == "ok" for r in results)
    return jsonify({"ok": ok, "results": results, "remark": header.remark,
                    "totals": {
                        "supply": header.tot_sply_amt,
                        "tax":    header.tot_tax_amt,
                        "grand":  header.sum_tot_amt,
                    }})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)