from __future__ import annotations
import csv
import io
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibsrs import __version__                                  # noqa: E402
from ibsrs.pipeline import (ARTIFACTS, BUNDLES_DIR, RUNS_DIR,  # noqa: E402
                            list_bundles, list_runs, run_pipeline)
from ibsrs.policy import DEFAULT_POLICY_PATH                   # noqa: E402

UPLOADS_DIR = BUNDLES_DIR.parent / "uploads"

app = FastAPI(
    title="IBSRS - Intelligent Bank Reconciliation System",
    description="Deterministic file-based multi-agent reconciliation pipeline "
                "(Agents A, B, C&D, E, H) with full audit trail.",
    version=__version__,
)


class RunRequest(BaseModel):
    bundle_name: str | None = None   # name under data/bundles or data/uploads
    bundle_path: str | None = None   # explicit path (takes precedence)
    use_ai: bool | None = None       # None=auto (policy+availability), True/False force


def _resolve_bundle(req: RunRequest) -> Path:
    if req.bundle_path:
        p = Path(req.bundle_path)
    elif req.bundle_name:
        p = BUNDLES_DIR / req.bundle_name
        if not p.exists():
            p = UPLOADS_DIR / req.bundle_name
    else:
        raise HTTPException(422, "Provide bundle_name or bundle_path")
    if not (p / "manifest.yaml").exists():
        raise HTTPException(404, f"Recon Bundle not found: {p}")
    return p


@app.get("/")
def root():
    return {"service": "IBSRS", "version": __version__,
            "agents": ["A intake", "B extraction", "C&D matching+variance",
                       "E duplicates", "H triage+orchestration"],
            "endpoints": ["/bundles", "/policy", "/runs", "/docs"]}


@app.get("/bundles")
def get_bundles():
    out = list_bundles()
    if UPLOADS_DIR.exists():
        for d in sorted(UPLOADS_DIR.iterdir()):
            if (d / "manifest.yaml").exists():
                m = yaml.safe_load((d / "manifest.yaml").read_text(encoding="utf-8"))
                out.append({"name": d.name, "path": str(d),
                            "bundle_id": m.get("bundle_id", d.name),
                            "description": m.get("description", "(uploaded)"),
                            "account": m.get("account", {}).get("account_id", "?"),
                            "period": m.get("account", {}).get("period", "?")})
    return out


@app.get("/policy")
def get_policy():
    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


@app.put("/policy")
def put_policy(policy: dict):
    """Replace the policy pack (YAML-driven reconfiguration, no code edits)."""
    current = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    for section, values in policy.items():
        if isinstance(values, dict) and isinstance(current.get(section), dict):
            current[section].update(values)
        else:
            current[section] = values
    DEFAULT_POLICY_PATH.write_text(
        yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    return {"status": "updated", "policy": current}


@app.post("/runs")
def create_run(req: RunRequest):
    bundle = _resolve_bundle(req)
    try:
        # AI agents (semantic matching / LLM) may take longer; FastAPI runs this
        # sync endpoint in a worker thread so the event loop is not blocked.
        return run_pipeline(bundle, use_ai=req.use_ai)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        raise HTTPException(422, f"Pipeline rejected bundle: {exc}")


@app.get("/ai/status")
def ai_status():
    """Report which AI capabilities are available in this deployment."""
    from ibsrs.ai import AIRuntime
    from ibsrs.policy import load_policy
    rt = AIRuntime(load_policy())
    return rt.status


@app.get("/runs")
def get_runs():
    return list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, f"Run not found: {run_id}")
    import json
    out = {"run_id": run_id, "artifacts": [a for a in ARTIFACTS
                                           if (run_dir / a).exists()]}
    for name in ("decision.json", "metrics.json", "ai_insights.json"):
        f = run_dir / name
        if f.exists():
            out[name.split(".")[0]] = json.loads(f.read_text(encoding="utf-8"))
    return out


@app.get("/runs/{run_id}/artifacts/{name}")
def get_artifact(run_id: str, name: str):
    if name not in ARTIFACTS:
        raise HTTPException(404, f"Unknown artifact: {name}")
    f = RUNS_DIR / run_id / name
    if not f.exists():
        raise HTTPException(404, f"Artifact not found: {name}")
    text = f.read_text(encoding="utf-8")
    if name.endswith(".json"):
        import json
        return JSONResponse(json.loads(text))
    return PlainTextResponse(text)


# Standard auxiliary files so uploads have the same structure as curated bundles.
_DEFAULT_FEES = [("MONTHLY SERVICE CHARGE", "45.00"),
                 ("MONTHLY MAINTENANCE FEE", "25.00"),
                 ("WIRE TRANSFER FEE", "15.00")]
_DEFAULT_FX = [("USD", "1.0"), ("EUR", "1.09"), ("GBP", "1.27")]
_DEFAULT_GL_MAP = {"cash": "1010:Cash - Operating",
                   "bank_fees": "6210:Bank Fees Expense",
                   "interest_income": "4210:Interest Income",
                   "fx_gain_loss": "7150:FX Gain/Loss",
                   "suspense": "1999:Suspense Clearing"}


def _detect_balances(raw: bytes, suffix: str) -> tuple[float | None, float | None]:
    """Read opening/closing balances straight from the statement so the user
    does not have to type them. MT940 :60F:/:62F: are authoritative; PDFs and
    CSVs are parsed from their text. Returns (opening, closing) or (None, None)."""
    import re
    suffix = suffix.lower()

    def _num(s: str) -> float:
        return round(float(s.replace(",", "")), 2)

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io as _io
            txt = "\n".join((p.extract_text() or "")
                            for p in PdfReader(_io.BytesIO(raw)).pages)
        except Exception:
            return None, None
        o = re.search(r"Opening Balance[:\s]+([\d,]+\.\d{2})", txt, re.I)
        c = re.search(r"Closing [Bb]alance[:\s]+([\d,]+\.\d{2})", txt, re.I)
        return (_num(o.group(1)) if o else None, _num(c.group(1)) if c else None)

    text = raw.decode("utf-8", "replace")
    if ":60F:" in text or ":62F:" in text:  # MT940 (comma decimal)
        def _mt(tag: str) -> float | None:
            m = re.search(rf"{tag}([CD])\d{{6}}[A-Z]{{3}}([\d.,]+)", text)
            if not m:
                return None
            val = round(float(m.group(2).replace(".", "").replace(",", ".")), 2)
            return -val if m.group(1) == "D" else val
        return _mt(":60F:"), _mt(":62F:")

    # CSV: prefer a header note, else derive from the running_balance column
    o = re.search(r"Opening Balance[:\s]+([\d,]+\.\d{2})", text, re.I)
    opening = _num(o.group(1)) if o else None
    closing = None
    try:
        rows = list(csv.DictReader([ln for ln in text.splitlines()
                                    if not ln.startswith("#")]))
        if rows and "running_balance" in rows[0]:
            if opening is None:
                opening = round(float(rows[0]["running_balance"])
                                - float(rows[0]["amount"]), 2)
            closing = round(float(rows[-1]["running_balance"]), 2)
    except Exception:
        pass
    return opening, closing


def _write_aux_bundle_files(bundle_dir: Path, opening_balance: float,
                            period: str) -> None:
    """Give an uploaded bundle the same auxiliary files a curated bundle has."""
    with open(bundle_dir / "bank_fee_schedule.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["fee_type", "amount"])
        w.writerows(_DEFAULT_FEES)
    with open(bundle_dir / "fx_rates.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["currency", "rate_to_base"])
        w.writerows(_DEFAULT_FX)
    # prior period closing == opening so we don't raise a false opening mismatch
    import json as _json
    prior_period = period
    try:
        y, m = period.split("-")
        pm = int(m) - 1 or 12
        py = int(y) - (1 if int(m) == 1 else 0)
        prior_period = f"{py:04d}-{pm:02d}"
    except Exception:
        pass
    (bundle_dir / "prior_recon.json").write_text(_json.dumps({
        "period": prior_period, "closing_balance_bank": opening_balance,
        "closing_balance_gl": opening_balance,
        "outstanding_items": [], "transactions": []}, indent=2, sort_keys=True),
        encoding="utf-8")


@app.post("/upload-statement")
async def upload_statement(
        file: UploadFile = File(...),
        account_id: str = Form(...),
        account_name: str = Form("Uploaded Account"),
        bank_name: str = Form("Uploaded Bank"),
        currency: str = Form("USD"),
        period: str = Form(...),
        opening_balance: float | None = Form(None),
        closing_balance: float | None = Form(None),
        gl_file: UploadFile | None = File(None),
        run_now: bool = Form(False),
        use_ai: str | None = Form(None)):
    """Statement intake (deliverable input mode (a)/(b)): wraps an uploaded
    CSV/MT940/PDF statement into a full Recon Bundle - with the same auxiliary
    files curated bundles have (fee schedule, FX rates, prior reconciliation)
    and an optional uploaded GL export. Opening/closing balances are auto-
    detected from the statement when not supplied. With ``run_now`` it also
    executes the pipeline and returns the run result, so it lands in runs/."""
    # use_ai arrives as a string form field; parse to tri-state (avoids 422 on "")
    use_ai_flag = {"true": True, "false": False}.get((use_ai or "").strip().lower())

    raw = await file.read()
    suffix = Path(file.filename or "statement.csv").suffix or ".csv"
    stmt_name = f"bank_statement{suffix}"
    gl_bytes = await gl_file.read() if (gl_file is not None
               and (gl_file.filename or "").strip()) else None
    has_gl = gl_bytes is not None

    # Unique, content-addressed bundle folder so distinct uploads each get their
    # own folder in data/uploads/ (like runs/) instead of overwriting each other.
    # Identical re-uploads (same file + account + period) reuse the same folder.
    import hashlib
    acct_safe = "".join(c if c.isalnum() or c in "-_" else "_"
                        for c in (account_id or "upload"))[:30]
    fp = hashlib.sha256(raw + (gl_bytes or b"") + account_id.encode()
                        + period.encode() + currency.encode()).hexdigest()[:8]
    bundle_dir = UPLOADS_DIR / f"upload_{acct_safe}_{fp}"
    if bundle_dir.exists():  # idempotent: rebuild this upload's bundle cleanly
        for f in bundle_dir.glob("*"):
            f.unlink()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / stmt_name).write_bytes(raw)

    # auto-detect balances from the statement; explicit form values override
    det_open, det_close = _detect_balances(raw, suffix)
    opening_balance = opening_balance if opening_balance is not None else (det_open or 0.0)
    closing_balance = closing_balance if closing_balance is not None else (det_close or 0.0)

    # GL export: use the uploaded one (full reconciliation) or an empty stub
    # (statement-only analysis: extraction, typing, duplicates, risk flags).
    if has_gl:
        (bundle_dir / "gl_export.csv").write_bytes(gl_bytes)
    else:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(
            ["gl_id", "date", "account_code", "description", "reference", "amount"])
        (bundle_dir / "gl_export.csv").write_text(buf.getvalue(), encoding="utf-8")

    _write_aux_bundle_files(bundle_dir, opening_balance, period)

    manifest = {"bundle_id": f"UPLOAD-{acct_safe}-{fp}",
                "description": f"Uploaded statement ({file.filename})"
                               + ("" if has_gl else " - statement-only, no GL"),
                "account": {"account_id": account_id, "account_name": account_name,
                            "bank_name": bank_name, "currency": currency,
                            "period": period,
                            "opening_balance": opening_balance,
                            "closing_balance": closing_balance},
                "files": {"bank_statement": stmt_name,
                          "gl_export": "gl_export.csv",
                          "prior_recon": "prior_recon.json",
                          "bank_fee_schedule": "bank_fee_schedule.csv",
                          "fx_rates": "fx_rates.csv"},
                "gl_account_map": _DEFAULT_GL_MAP}
    (bundle_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = {"status": "created", "bundle_name": bundle_dir.name,
              "bundle_path": str(bundle_dir), "has_gl": has_gl,
              "detected_opening": det_open, "detected_closing": det_close,
              "opening_balance": opening_balance, "closing_balance": closing_balance}
    if run_now:
        try:
            result["run"] = run_pipeline(bundle_dir, use_ai=use_ai_flag)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            raise HTTPException(422, f"Uploaded bundle could not be processed: {exc}")
    return result
