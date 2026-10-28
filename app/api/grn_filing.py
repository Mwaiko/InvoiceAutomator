"""
app/routers/grn_filing.py

Batch GRN filing desk: upload many → extract → human review → guarded KRA filing.

Mount:   app.include_router(grn_filing.router)         (add your auth dependency)
UI:      GET /api/grn-filing/ui   (serves app/static/grn_filing.html)

Safety rules enforced here
──────────────────────────
 1. Only ONE filing batch runs at a time and jobs are filed strictly one-by-one
    (the per-store invoice counter is read-then-write, so parallel filing would
    hand out duplicate invoice numbers).
 2. A job can only be filed after a human has confirmed it AND server-side
    validation passes (re-run at file time, never trusted from the browser).
 3. eTIMS health (DNS→TCP→TLS→HTTP→cookie→LOGIN→sales endpoint) must be green
    before a batch starts and before every job. If KRA is down the queue is HELD
    (not failed) and nothing is sent.
 4. One bad GRN never stops the batch: it is marked needs_fix / failed with the
    reason, and the worker moves on to the next.
 5. KRA POSTs are NEVER auto-retried. A read-timeout means KRA probably accepted
    the invoice, so the job becomes "verify" (human checks the portal).
 6. If KRA accepted but our DB write fails, the job becomes "verify" with the
    invoice number in the message and a CRITICAL log line — never re-filed.
 7. Job state is snapshotted to disk; after a crash anything that was mid-filing
    comes back as "verify", never as "confirmed".
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal          # ← adjust to your session factory
from app.services.business_resolver import (
    post_confirmation_update_balances,
    resolve_business_and_branch,
)
from app.services.etims_health import probe_etims
from app.services.etims_item_map import resolve_item
from app.services.etims_mapper import build_etims_payload
from app.services.file_storage import delete_file, save_grn_upload
from app.services.fill_kra import EtimsConfig, invoice_dict_to_receipt, run_fill
from app.services.grn_extractor import extract_grn

logger = get_logger(__name__)
router = APIRouter(prefix="/api/grn-filing", tags=["grn-filing"])

UI_PATH = Path(__file__).resolve().parent.parent / "static" / "grn_filing.html"
STORE = Path(settings.grn_storage_path) / "_jobs.json"

# GRN item_code is the retailer's barcode, NOT the KRA itemCd. When True, the
# mapper is fed only the catalogue's itemCd/class/tax/uom, never GRN-supplied ones.
TRUST_CATALOGUE = True

LOCKED = {"extracting", "queued", "filing", "filed", "verify"}
FILED_LIKE = {"filed", "verify"}

JOBS: dict[str, dict] = {}
BATCH = {"running": False, "message": "", "total": 0, "done": 0}
_file_lock = asyncio.Lock()
_extract_sem = asyncio.Semaphore(2)
_tasks: set[asyncio.Task] = set()
_hc: dict = {"at": 0.0, "report": None}


class _Fix(Exception):
    """Operator must change the data; nothing was sent to KRA."""


class _KraFail(Exception):
    """KRA definitively rejected / errored; nothing was accepted."""


# ── persistence ──────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save() -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(JOBS, default=str))
        tmp.replace(STORE)
    except Exception:
        logger.exception("grn_filing: could not snapshot jobs")


def _load() -> None:
    if not STORE.exists():
        return
    try:
        JOBS.update(json.loads(STORE.read_text()))
    except Exception:
        logger.exception("grn_filing: could not read job snapshot")
        return
    for j in JOBS.values():
        if j["status"] == "extracting":
            j.update(status="extract_failed", message="Interrupted by restart — retry reading.")
        elif j["status"] == "filing":
            j.update(status="verify", message="Server stopped mid-filing. Check the KRA portal before doing anything.")
        elif j["status"] == "queued":
            j.update(status="confirmed", message="Restart cleared the queue — file again.")


_load()


def _set(job: dict, status: str, message: str = "", **extra) -> None:
    job.update(status=status, message=message, updated=_now(), **extra)
    _save()


def _get(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ── validation (authoritative; re-run at file time) ──────────────────────────
def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def validate(data: dict | None) -> list[dict]:
    out: list[dict] = []

    def add(level, field, msg):
        out.append({"level": level, "field": field, "msg": msg})

    data = data or {}
    store = data.get("store") or {}
    if not (store.get("company_name") or "").strip():
        add("error", "store.company_name", "Buyer company is missing")
    if not (store.get("store_name") or "").strip():
        add("error", "store.store_name", "Store name is missing")
    for key, label in (("lpo_number", "LPO / Order No"),
                       ("delivery_invoice_no", "Delivery note / Invoice No"),
                       ("receipt_voucher_no", "GRN / Receipt voucher No")):
        if not str(data.get(key) or "").strip():
            add("error", key, f"{label} is missing (it is printed in the KRA remark)")

    items = data.get("items") or []
    if not items:
        add("error", "items", "No line items")
    calc = 0.0
    for i, it in enumerate(items):
        n = i + 1
        desc = (it.get("description") or "").strip()
        qty, prc = _num(it.get("qty_received")), _num(it.get("unit_price"))
        if not desc:
            add("error", f"items.{i}.description", f"Line {n}: description missing")
        elif TRUST_CATALOGUE and not resolve_item(desc):
            add("error", f"items.{i}.description", f"Line {n}: “{desc}” is not in the eTIMS item catalogue")
        if qty is None or qty <= 0:
            add("error", f"items.{i}.qty_received", f"Line {n}: quantity must be above 0")
        if prc is None or prc <= 0:
            add("error", f"items.{i}.unit_price", f"Line {n}: unit price must be above 0")
        if qty and prc:
            calc += qty * prc

    total = _num(data.get("order_total"))
    if total is None:
        add("warn", "order_total", "Order total missing — cannot cross-check lines")
    elif items and abs(total - calc) > 1.0:
        add("warn", "order_total", f"Order total {total:,.2f} ≠ sum of lines {calc:,.2f}")
    return out


def _errors(data) -> list[dict]:
    return [i for i in validate(data) if i["level"] == "error"]


def _for_mapper(data: dict) -> dict:
    d = copy.deepcopy(data)
    for it in d.get("items") or []:
        it["qty_received"] = _num(it.get("qty_received"))
        it["unit_price"] = _num(it.get("unit_price"))
        if TRUST_CATALOGUE:
            for k in ("item_code", "item_cls_cd", "tax_ty_cd", "uom"):
                it.pop(k, None)
    return d


def _ref_key(d: dict) -> tuple:
    return tuple(str(d.get(k) or "").strip().lower()
                 for k in ("lpo_number", "delivery_invoice_no", "receipt_voucher_no"))


def _duplicate_of(job: dict) -> str | None:
    key = _ref_key(job["data"])
    for o in JOBS.values():
        if o["id"] != job["id"] and o["status"] in FILED_LIKE and o.get("data") and _ref_key(o["data"]) == key:
            return o["filename"]
    return None


def _pub(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k != "path"}
    out["issues"] = validate(job.get("data")) if job.get("data") is not None else []
    return out


# ── eTIMS health ─────────────────────────────────────────────────────────────
def _cfg() -> EtimsConfig:
    return EtimsConfig(
        pin=os.environ.get("KRA_PIN", ""),
        username=os.environ.get("KRA_USERNAME", ""),
        password=os.environ.get("KRA_PASSWORD", ""),
        branch=os.environ.get("KRA_BRANCH", "00"),
    )


async def _probe(fresh: bool = False) -> dict:
    if not fresh and _hc["report"] and time.time() - _hc["at"] < 15:
        return _hc["report"]
    cfg = _cfg()
    if not (cfg.username and cfg.password):
        rep = {"site_up": False, "timestamp": _now(), "checks": [
            {"name": "Credentials", "passed": False, "elapsed_s": None, "detail": None,
             "message": "KRA_USERNAME / KRA_PASSWORD are not set on the server"}]}
    else:
        try:
            r = await asyncio.get_running_loop().run_in_executor(
                None, partial(probe_etims, timeout=10, username=cfg.username, password=cfg.password))
            rep = r.to_dict()
        except Exception as exc:
            rep = {"site_up": False, "timestamp": _now(), "checks": [
                {"name": "Health probe", "passed": False, "elapsed_s": None, "detail": None,
                 "message": f"Probe crashed: {exc}"}]}
    _hc.update(at=time.time(), report=rep)
    return rep


async def _kra_ready(fresh: bool) -> bool:
    for attempt in range(4):
        rep = await _probe(fresh=fresh or attempt > 0)
        if rep["site_up"]:
            BATCH["message"] = ""
            return True
        bad = next((c for c in rep["checks"] if not c["passed"]), None)
        BATCH["message"] = (f"KRA not ready ({bad['name']}: {bad['message']}) — "
                            f"re-checking in 30s ({attempt + 1}/4)") if bad else "KRA not ready"
        if attempt < 3:
            await asyncio.sleep(30)
    return False


@router.get("/health")
async def health(fresh: bool = False):
    return await _probe(fresh=fresh)


# ── upload + extraction ──────────────────────────────────────────────────────
async def _extract_job(jid: str) -> None:
    job = JOBS[jid]
    _set(job, "extracting", "Reading document…")
    async with _extract_sem:
        try:
            data = await extract_grn(job["path"])
            if not isinstance(data, dict):
                raise ValueError("extractor returned nothing")
            data["store"] = data.get("store") or {}
            data["supplier"] = data.get("supplier") or {}
            data["items"] = data.get("items") or []
            job["data"] = data
            _set(job, "review", f"{len(data['items'])} line(s) read — please check")
        except Exception as exc:
            logger.exception("extraction failed for %s", job["filename"])
            _set(job, "extract_failed", f"Could not read document: {exc}")


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@router.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    created, rejected = [], []
    for f in files:
        try:
            path, ftype = await save_grn_upload(f)
        except Exception as exc:                      # bad type etc. → skip, keep going
            rejected.append({"filename": f.filename, "error": str(exc)})
            continue
        jid = uuid.uuid4().hex[:10]
        JOBS[jid] = {"id": jid, "filename": f.filename, "file_type": ftype, "path": path,
                     "status": "extracting", "message": "Queued for reading", "data": None,
                     "created": _now(), "updated": _now()}
        created.append(jid)
        _spawn(_extract_job(jid))
    _save()
    return {"created": created, "rejected": rejected}


@router.get("/jobs")
async def list_jobs():
    jobs = sorted(JOBS.values(), key=lambda j: j["created"])
    return {"jobs": [_pub(j) for j in jobs], "batch": BATCH}


@router.get("/jobs/{jid}/file")
async def source_file(jid: str):
    return FileResponse(_get(jid)["path"])


@router.post("/jobs/{jid}/reextract")
async def reextract(jid: str):
    job = _get(jid)
    if job["status"] in LOCKED:
        raise HTTPException(409, f"Cannot re-read a job that is {job['status']}")
    _spawn(_extract_job(jid))
    return {"ok": True}


@router.delete("/jobs/{jid}")
async def remove(jid: str):
    job = _get(jid)
    if job["status"] in {"filing", "queued"}:
        raise HTTPException(409, "Job is being filed")
    if job["status"] in FILED_LIKE:
        raise HTTPException(409, "Filed jobs are kept as a record")
    delete_file(job["path"])
    JOBS.pop(jid, None)
    _save()
    return {"ok": True}


# ── review ───────────────────────────────────────────────────────────────────
class SaveBody(BaseModel):
    data: dict
    confirm: bool = False


@router.put("/jobs/{jid}")
async def save(jid: str, body: SaveBody):
    job = _get(jid)
    if job["status"] in LOCKED:
        raise HTTPException(409, f"Job is {job['status']} and cannot be edited")
    job["data"] = body.data
    if body.confirm:
        errs = _errors(body.data)
        if errs:
            return JSONResponse(status_code=422, content={"detail": "Fix the errors before confirming", "issues": validate(body.data)})
        _set(job, "confirmed", "Confirmed — ready to file", confirmed_at=_now())
    else:
        _set(job, "review", "Draft saved")
    return _pub(job)


@router.get("/jobs/{jid}/preview")
async def preview(jid: str):
    """Exactly what would be sent to KRA (read-only; DB changes are rolled back)."""
    job = _get(jid)
    data = job.get("data") or {}
    errs = _errors(data)
    if errs:
        raise HTTPException(422, "; ".join(e["msg"] for e in errs[:3]))
    async with AsyncSessionLocal() as db:
        try:
            biz, br = await resolve_business_and_branch(db, data)
            inv, items, meta = await build_etims_payload(
                _for_mapper(data), data.get("receipt_voucher_no") or "", db,
                business_id=biz.id if biz else None, branch_id=br.id if br else None,
                business_name=biz.name if biz else None, branch_name=br.branch_name if br else None)
        except Exception as exc:
            raise HTTPException(422, f"Could not build KRA payload: {exc}")
        finally:
            await db.rollback()
    return {"invoice": inv, "items": items,
            "meta": {k: (None if v is None else str(v)) for k, v in meta.items()}}


# ── filing ───────────────────────────────────────────────────────────────────
class FileBody(BaseModel):
    job_ids: list[str]


async def _persist(db, meta: dict, res: dict, data: dict, unverified: bool) -> None:
    from app.db.models.etims_invoice import EtimsInvoice
    cols = {c.name for c in EtimsInvoice.__table__.columns}
    if not {"invoice_no", "store_number"} <= cols:
        raise RuntimeError("EtimsInvoice lacks invoice_no/store_number — counter would not advance")
    vals = {
        "invoice_no": meta["invoice_number"], "store_number": meta["store_number"],
        "business_id": meta.get("business_id"), "branch_id": meta.get("branch_id"),
        "business_name": meta.get("business_name"), "branch_name": meta.get("branch_name"),
        "invoice_amount": meta.get("invoice_amount"),
        # NB: etims_mapper's meta has lpo_number/grn_no swapped — take them from the GRN directly.
        "lpo_number": data.get("lpo_number"), "grn_no": data.get("receipt_voucher_no"),
        "kra_invoice_no": res.get("kra_invoice_no"),
        "status": "unverified" if unverified else "submitted",
    }
    db.add(EtimsInvoice(**{k: v for k, v in vals.items() if k in cols}))
    await db.flush()


async def _file_one(job: dict) -> None:
    _set(job, "filing", "Submitting to KRA…")
    data, loop = job["data"], asyncio.get_running_loop()
    try:
        errs = _errors(data)
        if errs:
            raise _Fix("; ".join(e["msg"] for e in errs[:3]))
        dup = _duplicate_of(job)
        if dup:
            raise _Fix(f"Same LPO / delivery / GRN numbers as already-filed “{dup}”")

        async with AsyncSessionLocal() as db:
            try:                                           # ── build (nothing sent yet)
                biz, br = await resolve_business_and_branch(db, data)
                inv, items, meta = await build_etims_payload(
                    _for_mapper(data), data.get("receipt_voucher_no") or "", db,
                    business_id=biz.id if biz else None, branch_id=br.id if br else None,
                    business_name=biz.name if biz else None, branch_name=br.branch_name if br else None)
                if meta.get("store_number") in (None, "", "?"):
                    raise _Fix(f"Store “{meta.get('branch_name')}” has no eTIMS store number — add it to the branch record")
                if not meta.get("invoice_amount"):
                    meta["invoice_amount"] = round(sum(i["qty"] * i["prc"] for i in items), 2)
                header = invoice_dict_to_receipt(inv, items)
            except Exception:
                await db.rollback()
                raise

            try:                                           # ── send (single attempt, no retry)
                results = await loop.run_in_executor(None, partial(run_fill, _cfg(), header))
                res = results[0] if results else {"status": "error", "error": "empty response from run_fill"}
            except Exception as exc:
                res = {"status": "error", "error": str(exc)}

            st = res.get("status")
            if st not in ("ok", "timeout"):
                await db.rollback()
                raise _KraFail(res.get("error") or "KRA rejected the receipt")

            try:                                           # ── record (KRA already has it)
                await _persist(db, meta, res, data, unverified=(st == "timeout"))
                if st == "ok" and biz and br:
                    await post_confirmation_update_balances(db, biz, br, float(meta["invoice_amount"] or 0))
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.critical("SENT TO KRA but DB save failed: job=%s invoice=%s err=%s",
                                job["id"], meta["invoice_number"], exc)
                _set(job, "verify",
                     f"Sent to KRA as invoice {meta['invoice_number']} but the local save failed ({exc}). "
                     "Do NOT re-file — record it manually.",
                     invoice_no=meta["invoice_number"], kra_invoice_no=res.get("kra_invoice_no"))
                return

        extra = {"invoice_no": meta["invoice_number"], "store_no": meta["store_number"],
                 "kra_invoice_no": res.get("kra_invoice_no")}
        if st == "timeout":
            _set(job, "verify", res.get("error") or "KRA timed out — verify on the portal. Not retried.", **extra)
        else:
            _set(job, "filed", f"Filed — invoice {meta['invoice_number']}", **extra)
    except _Fix as e:
        _set(job, "needs_fix", str(e))
    except _KraFail as e:
        _hc["at"] = 0                                      # force a fresh health probe next job
        _set(job, "failed", str(e))
    except Exception as e:
        logger.exception("unexpected error filing %s", job["filename"])
        _set(job, "failed", f"Unexpected error before submission: {e}")


async def _run_batch(ids: list[str]) -> None:
    BATCH.update(running=True, total=len(ids), done=0, message="")
    try:
        async with _file_lock:
            for pos, jid in enumerate(ids):
                job = JOBS.get(jid)
                if not job or job["status"] != "queued":
                    BATCH["done"] += 1
                    continue
                if not await _kra_ready(fresh=False):
                    for rid in ids[pos:]:
                        j = JOBS.get(rid)
                        if j and j["status"] == "queued":
                            _set(j, "held", "KRA eTIMS unavailable — nothing was sent. File again when healthy.")
                    break
                await _file_one(job)
                BATCH["done"] += 1
                await asyncio.sleep(1)
    finally:
        BATCH["running"] = False
        _save()


@router.post("/file")
async def file_jobs(body: FileBody):
    if BATCH["running"]:
        raise HTTPException(409, "A filing batch is already running")
    rep = await _probe(fresh=True)                          # pre-flight, before touching any job
    if not rep["site_up"]:
        return JSONResponse(status_code=503, content={"detail": "KRA eTIMS is not healthy — nothing was queued", "report": rep})
    queued, skipped = [], []
    for jid in body.job_ids:
        j = JOBS.get(jid)
        if not j or j["status"] not in {"confirmed", "failed", "held"}:
            skipped.append(jid)
            continue
        if _errors(j.get("data")):
            _set(j, "needs_fix", "Validation failed — please review")
            skipped.append(jid)
            continue
        _set(j, "queued", "Waiting for its turn")
        queued.append(jid)
    if not queued:
        raise HTTPException(400, "Nothing eligible to file")
    _spawn(_run_batch(queued))
    return {"queued": queued, "skipped": skipped}


class ResolveBody(BaseModel):
    outcome: str  # "filed" | "not_filed"


@router.post("/jobs/{jid}/resolve")
async def resolve(jid: str, body: ResolveBody):
    """Operator checked the KRA portal after a timeout / DB failure."""
    job = _get(jid)
    if job["status"] != "verify":
        raise HTTPException(409, "Only 'verify' jobs can be resolved")
    if body.outcome == "filed":
        _set(job, "filed", "Operator verified on KRA portal")
    elif body.outcome == "not_filed":
        _set(job, "confirmed", "Operator verified NOT on KRA — safe to file again")
    else:
        raise HTTPException(400, "outcome must be 'filed' or 'not_filed'")
    return _pub(job)


@router.get("/ui", include_in_schema=False)
async def ui():
    return FileResponse(UI_PATH)