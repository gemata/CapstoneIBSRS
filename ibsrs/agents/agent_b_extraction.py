
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from ibsrs.policy import Policy
from ibsrs.schemas import (BankTransaction, ContextPacket, Evidence, Finding,
                           TransactionsArtifact)
from ibsrs.utils.io import AuditLog, write_json

_TYPE_KEYWORDS = [
    ("FEE", ["FEE", "SERVICE CHARGE", "MAINTENANCE", "WIRE CHARGE", "CHARGE"]),
    ("INTEREST", ["INTEREST"]),
    ("CHECK", ["CHECK", "CHEQUE", "CHK"]),
    ("ACH", ["ACH", "DIRECT DEP", "PAYROLL", "VENDOR PAY"]),
    ("WIRE", ["WIRE", "SWIFT", "TT "]),
    ("TRANSFER", ["TRANSFER", "XFER", "SWEEP"]),
]


def _classify(description: str) -> str:
    up = description.upper()
    for txn_type, keys in _TYPE_KEYWORDS:
        if any(k in up for k in keys):
            return txn_type
    return "OTHER"


def _confidence(description: str, review_threshold: float) -> tuple[float, bool]:
    """Truncated/ambiguous descriptions get reduced confidence."""
    conf = 1.0
    if description.endswith(("...", "..", "~")):
        conf -= 0.30
    if len(description.strip()) < 8:
        conf -= 0.15
    if re.search(r"\bUNKNOWN\b|\bMISC\b|\?\?", description.upper()):
        conf -= 0.10
    conf = round(max(conf, 0.05), 2)
    return conf, conf < review_threshold


def _parse_csv(stmt_path: Path, ctx: ContextPacket, review_threshold: float
               ) -> list[BankTransaction]:
    txns: list[BankTransaction] = []
    lines = stmt_path.read_text(encoding="utf-8").splitlines()
    header_idx = next(i for i, ln in enumerate(lines)
                      if not ln.startswith("#"))
    reader = csv.DictReader([ln for ln in lines if not ln.startswith("#")])
    # first data row line number (0-based list, 1-based locator)
    data_line = header_idx + 1
    n = 0
    for row in reader:
        # Skip malformed/summary rows (real statements often carry total lines)
        # rather than crashing the whole extraction.
        date_raw = (row.get("date") or "").strip()
        amt_raw = (row.get("amount") or "").strip().replace(",", "")
        try:
            amount = round(float(amt_raw), 2)
        except (ValueError, TypeError):
            continue
        if not date_raw:
            continue
        n += 1
        desc = (row.get("description") or "").strip()
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}",
            date=date_raw,
            amount=amount,
            currency=(row.get("currency") or ctx.account.currency).strip(),
            description=desc,
            reference=(row.get("reference") or "").strip(),
            counterparty=(row.get("counterparty") or "").strip(),
            txn_type=_classify(desc),
            confidence=conf, needs_review=review,
            evidence=Evidence(source_file=stmt_path.name,
                              locator=f"line:{data_line + n}",
                              snippet=desc[:80]),
        ))
    return txns


_MT940_TXN = re.compile(
    r"^:61:(\d{6})(\d{4})?([CD])(\d+[,.]?\d*)N(\w{3})(\S*)")


def _parse_mt940(stmt_path: Path, ctx: ContextPacket, review_threshold: float
                 ) -> list[BankTransaction]:
    txns: list[BankTransaction] = []
    lines = stmt_path.read_text(encoding="utf-8").splitlines()
    n = 0
    for i, line in enumerate(lines):
        m = _MT940_TXN.match(line.strip())
        if not m:
            continue
        n += 1
        yymmdd, _, dc, amt_raw, _code, ref = m.groups()
        date = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        amount = round(float(amt_raw.replace(",", ".")), 2)
        if dc == "D":
            amount = -amount
        desc = ""
        if i + 1 < len(lines) and lines[i + 1].startswith(":86:"):
            desc = lines[i + 1][4:].strip()
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}", date=date, amount=amount,
            currency=ctx.account.currency, description=desc,
            reference=ref.replace("//", "").strip(), counterparty="",
            txn_type=_classify(desc), confidence=conf, needs_review=review,
            evidence=Evidence(source_file=stmt_path.name,
                              locator=f"line:{i + 1}", snippet=line[:80]),
        ))
    return txns


def _extract_pdf_text(pdf_path: Path) -> str:
    """Pull the text layer from a born-digital PDF (optional pypdf dep)."""
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""


# A transaction row in a born-digital statement: date, text, amount[, balance].
_PDF_TXN = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})(?:\s+(-?[\d,]+\.\d{2}))?\s*$")
# e.g. PAY0502, ACH7781, CHK3041
_PDF_REF = re.compile(r"^[A-Z]{2,6}\d{2,}[A-Z0-9-]*$")


def _parse_pdf_born_digital(stmt_path: Path, ctx: ContextPacket,
                            review_threshold: float, audit: AuditLog
                            ) -> list[BankTransaction] | None:
    """DETERMINISTIC parser for clean/born-digital PDF statements (spec: clean
    PDFs). Reads the text layer with pypdf and parses tabular transaction rows
    (date / description / amount [/ running balance]). No API, no randomness -
    so PDF uploads extract real data and reconcile. Returns None if the text
    layer is missing or has no recognizable rows (caller then tries LLM)."""
    text = _extract_pdf_text(stmt_path)
    if not text:
        return None
    txns: list[BankTransaction] = []
    page = 1
    for i, raw in enumerate(text.splitlines(), 1):
        if raw.strip().startswith("Page ") or "Statement Period" in raw:
            continue
        m = _PDF_TXN.match(raw)
        if not m:
            continue
        date, middle, amt_raw, _bal = m.groups()
        try:
            amount = round(float(amt_raw.replace(",", "")), 2)
        except ValueError:
            continue
        tokens = middle.split()
        reference = ""
        if tokens and _PDF_REF.match(tokens[-1]):
            reference = tokens[-1]
            tokens = tokens[:-1]
        desc = " ".join(tokens).strip()
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{len(txns) + 1:04d}", date=date, amount=amount,
            currency=ctx.account.currency, description=desc, reference=reference,
            counterparty="", txn_type=_classify(desc), confidence=conf,
            needs_review=review, extraction_method="pdf_text",
            evidence=Evidence(source_file=stmt_path.name, locator=f"page:{page},line:{i}",
                              snippet=raw.strip()[:80]),
        ))
    if not txns:
        return None
    audit.decision(f"Born-digital PDF parsed deterministically: {len(txns)} rows from "
                   f"the {stmt_path.name} text layer (pypdf)",
                   "Rule-based clean-PDF parser - no LLM, fully reproducible")
    return txns


def _parse_pdf_with_llm(stmt_path: Path, ctx: ContextPacket, review_threshold: float,
                        audit: AuditLog, ai) -> list[BankTransaction] | None:
    """HYBRID: read the PDF text layer and let GPT-4o-mini structure it into
    normalized rows. Returns None on any failure so the caller falls back to
    the deterministic synthetic path."""
    text = _extract_pdf_text(stmt_path)
    if not text:
        audit.step("PDF has no extractable text layer; cannot use LLM extraction")
        return None
    system = (
        "You are a precise bank-statement extraction engine. Return ONLY a JSON "
        "object with a 'rows' array. Each row: date (YYYY-MM-DD), description "
        "(string), amount (number, negative for debits/payments, positive for "
        "credits/deposits), reference (string, may be empty), counterparty "
        "(string, may be empty), currency (3-letter), confidence (0..1). Do not "
        "invent transactions; transcribe only what appears in the text.")
    user = (f"Account currency default: {ctx.account.currency}. Period: "
            f"{ctx.account.period}.\n\nStatement text:\n{text[:6000]}")
    data = ai.chat_json("B", system, user, model=ai.extraction_model)
    if not data or "rows" not in data or not isinstance(data["rows"], list):
        audit.step("LLM extraction returned no usable rows; falling back")
        return None
    txns: list[BankTransaction] = []
    for n, row in enumerate(data["rows"], 1):
        try:
            desc = str(row["description"]).strip()
            amount = round(float(row["amount"]), 2)
            date = str(row["date"]).strip()
        except (KeyError, ValueError, TypeError):
            continue
        base_conf, _ = _confidence(desc, review_threshold)
        llm_conf = row.get("confidence")
        conf = round(min(base_conf, float(llm_conf)), 2) if isinstance(
            llm_conf, (int, float)) else base_conf
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}", date=date, amount=amount,
            currency=str(row.get("currency") or ctx.account.currency),
            description=desc, reference=str(row.get("reference") or ""),
            counterparty=str(row.get("counterparty") or ""),
            txn_type=_classify(desc), confidence=conf,
            needs_review=conf < review_threshold, extraction_method="llm",
            evidence=Evidence(source_file=stmt_path.name,
                              locator=f"page:1 (llm:{ai.extraction_model})",
                              snippet=desc[:80]),
        ))
    if not txns:
        return None
    audit.decision(f"LLM ({ai.extraction_model}) extracted {len(txns)} rows from PDF "
                   f"text layer", "Hybrid extraction; deterministic synthetic fallback "
                   "available if the model is unavailable")
    return txns


def _parse_pdf_or_synthetic(stmt_path: Path, ctx: ContextPacket,
                            review_threshold: float, audit: AuditLog, ai=None
                            ) -> tuple[list[BankTransaction], bool]:
    """PDF extraction order: (1) sidecar text-layer JSON with bounding boxes,
    (2) HYBRID LLM extraction from the PDF text layer (if available),
    (3) deterministic synthetic-data fallback seeded from account+period."""
    sidecar = stmt_path.with_suffix(".extracted.json")
    txns: list[BankTransaction] = []
    if sidecar.exists():
        rows = json.loads(sidecar.read_text(encoding="utf-8"))["rows"]
        audit.step(
            f"PDF text layer found ({sidecar.name}); {len(rows)} rows with bounding boxes")
        for n, row in enumerate(rows, 1):
            desc = row["description"]
            conf, review = _confidence(desc, review_threshold)
            bbox = row.get("bbox", [0, 0, 0, 0])
            txns.append(BankTransaction(
                txn_id=f"B-{n:04d}", date=row["date"],
                amount=round(float(row["amount"]), 2),
                currency=row.get("currency", ctx.account.currency),
                description=desc, reference=row.get("reference", ""),
                counterparty=row.get("counterparty", ""),
                txn_type=_classify(desc), confidence=conf, needs_review=review,
                evidence=Evidence(source_file=stmt_path.name,
                                  locator=f"page:{row.get('page', 1)},bbox:{bbox}",
                                  snippet=desc[:80]),
            ))
        return txns, False

    # ---- deterministic born-digital PDF parser (clean PDFs, no API) -------
    born = _parse_pdf_born_digital(stmt_path, ctx, review_threshold, audit)
    if born:
        return born, False

    # ---- HYBRID: LLM extraction for messy/scanned PDF text ----------------
    if ai is not None and getattr(ai, "llm_available", False):
        llm_txns = _parse_pdf_with_llm(
            stmt_path, ctx, review_threshold, audit, ai)
        if llm_txns:
            return llm_txns, False

    # ---- deterministic synthetic fallback (last resort) ------------------
    audit.decision("No PDF text layer / LLM unavailable; using deterministic "
                   "synthetic fallback",
                   "Seeded from account_id+period so re-runs are identical")
    seed = sum(ord(c) for c in ctx.account.account_id + ctx.account.period)
    year, month = ctx.account.period.split("-")
    descs = ["VENDOR PAYMENT ACME", "CUSTOMER DEPOSIT", "MONTHLY SERVICE FEE",
             "PAYROLL ACH BATCH", "WIRE IN - CLIENT"]
    for n in range(1, 6):
        amt = round(((seed * n * 37) % 9000) / 10 + 25, 2) * \
            (1 if n % 2 == 0 else -1)
        desc = descs[(seed + n) % len(descs)]
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}", date=f"{year}-{month}-{min(n * 5, 28):02d}",
            amount=amt, currency=ctx.account.currency, description=desc,
            reference=f"SYN{seed}{n:02d}", counterparty="",
            txn_type=_classify(desc), confidence=conf, needs_review=review,
            extraction_method="synthetic",
            evidence=Evidence(source_file=stmt_path.name,
                              locator=f"page:1,bbox:[72,{700 - n * 20},540,{716 - n * 20}]",
                              snippet=f"(synthetic) {desc}"),
        ))
    return txns, True


def run_agent_b(ctx: ContextPacket, run_dir: Path, policy: Policy,
                audit: AuditLog, ai=None) -> tuple[TransactionsArtifact, list[Finding]]:
    audit.section("Agent B", "Transaction Extraction")
    findings: list[Finding] = []
    stmt_path = Path(ctx.files["bank_statement"])
    review_threshold = float(policy.get(
        "thresholds.extraction_review_confidence", 0.8))
    synthetic = False

    if ctx.account.statement_format == "csv":
        txns = _parse_csv(stmt_path, ctx, review_threshold)
    elif ctx.account.statement_format == "mt940":
        txns = _parse_mt940(stmt_path, ctx, review_threshold)
    else:
        txns, synthetic = _parse_pdf_or_synthetic(stmt_path, ctx, review_threshold,
                                                  audit, ai=ai)

    audit.step(f"Extracted {len(txns)} transactions from {stmt_path.name} "
               f"({ctx.account.statement_format.upper()})")

    # multi-page/statement-level aggregation: opening + sum(txns) vs closing
    computed_closing = round(
        ctx.account.opening_balance + sum(t.amount for t in txns), 2)
    reconciles = abs(computed_closing - ctx.account.closing_balance) < 0.005
    audit.decision(
        f"Balance roll-forward: opening {ctx.account.opening_balance:,.2f} + "
        f"net movement = {computed_closing:,.2f}; statement closing "
        f"{ctx.account.closing_balance:,.2f} -> "
        f"{'RECONCILES' if reconciles else 'DOES NOT RECONCILE'}",
        "Sum of extracted amounts checked against statement closing balance")

    if not reconciles:
        findings.append(Finding(
            finding_id="B-BAL-001", agent="B", category="balance_mismatch",
            severity="critical", confidence=1.0,
            title="Statement does not roll forward",
            detail=(f"Computed closing {computed_closing:,.2f} vs stated "
                    f"{ctx.account.closing_balance:,.2f}"),
            evidence=[Evidence(source_file=stmt_path.name, locator="aggregate",
                               snippet="opening+sum(txns) != closing")],
            recommendation="Verify extraction completeness / request statement re-issue",
        ))

    for t in txns:
        if t.needs_review:
            findings.append(Finding(
                finding_id=f"B-REV-{t.txn_id}", agent="B", category="extraction_review",
                severity="medium", confidence=t.confidence,
                title=f"Ambiguous/truncated description on {t.txn_id}",
                detail=f"'{t.description}' scored {t.confidence:.2f} < "
                f"{review_threshold:.2f} review threshold",
                evidence=[t.evidence], related_txn_ids=[t.txn_id],
                recommendation="Manual review before GL mapping",
                open_question="What does this memo refer to?",
            ))
            audit.step(f"Flagged {t.txn_id} for manual review "
                       f"(confidence {t.confidence:.2f}): '{t.description}'")

    artifact = TransactionsArtifact(
        run_id=ctx.run_id, account_id=ctx.account.account_id,
        period=ctx.account.period, currency=ctx.account.currency,
        opening_balance=ctx.account.opening_balance,
        closing_balance=ctx.account.closing_balance,
        computed_closing_balance=computed_closing,
        balance_reconciles=reconciles,
        transactions=txns, synthetic_fallback_used=synthetic,
    )
    out = run_dir / "transactions.json"
    write_json(out, artifact)
    audit.artifact("transactions.json", out)
    return artifact, findings
