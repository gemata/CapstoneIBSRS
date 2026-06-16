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

_MT940_TXN = re.compile(
    r"^:61:(\d{6})(\d{4})?([CD])(\d+[,\.]\d*)([A-Z]{4})?([^/]*)(//.*)?$"
)

_TRUNCATION_SUFFIX = re.compile(r"(?:\.{2,}|…|~)\s*$")
_AMBIGUOUS_KEYWORDS = re.compile(
    r"\b(UNKNOWN|MISC|VARIOUS|UNSPEC(?:IFIED)?|TBD|N/?A|SEE\s+DETAIL|PENDING)\b|\?\?"
)
_GENERIC_ONLY = re.compile(
    r"^(PAYMENT|DEPOSIT|TRANSFER|WITHDRAWAL|DEBIT|CREDIT|ACH|WIRE)$",
    re.IGNORECASE,
)


def _classify(description: str) -> str:
    up = description.upper()
    for txn_type, keys in _TYPE_KEYWORDS:
        if any(k in up for k in keys):
            return txn_type
    return "OTHER"


def _assess_description(
    description: str,
    review_threshold: float,
    *,
    min_length: int = 8,
    truncated_field_lengths: list[int] | None = None,
) -> tuple[float, bool, list[str]]:
    """Score description quality; flag truncated/ambiguous memos for manual review."""
    desc = (description or "").strip()
    reasons: list[str] = []
    conf = 1.0
    field_lengths = truncated_field_lengths or [22, 40]

    if not desc:
        reasons.append("empty_description")
        conf -= 0.40
    elif len(desc) < min_length:
        reasons.append("very_short_description")
        conf -= 0.15

    if _TRUNCATION_SUFFIX.search(desc):
        reasons.append("truncated_suffix")
        conf -= 0.30
    elif desc and len(desc) in field_lengths and desc[-1].isalnum():
        reasons.append("possible_field_length_truncation")
        conf -= 0.20

    if _AMBIGUOUS_KEYWORDS.search(desc.upper()):
        reasons.append("ambiguous_keyword")
        conf -= 0.10

    if desc and _GENERIC_ONLY.match(desc):
        reasons.append("generic_description_only")
        conf -= 0.15

    alpha_chars = sum(1 for c in desc if c.isalpha())
    if desc and alpha_chars < 3:
        reasons.append("low_text_content")
        conf -= 0.20

    conf = round(max(conf, 0.05), 2)
    needs_review = bool(reasons) or conf < review_threshold
    return conf, needs_review, reasons


def _txn_from_row(
    *,
    txn_id: str,
    date: str,
    amount: float,
    currency: str,
    description: str,
    reference: str,
    counterparty: str,
    evidence: Evidence,
    review_threshold: float,
    policy: Policy,
) -> BankTransaction:
    conf, review, reasons = _assess_description(
        description,
        review_threshold,
        min_length=int(policy.get("extraction.min_description_length", 8)),
        truncated_field_lengths=policy.get("extraction.truncated_field_lengths", [22, 40]),
    )
    return BankTransaction(
        txn_id=txn_id,
        date=date,
        amount=amount,
        currency=currency,
        description=description,
        reference=reference,
        counterparty=counterparty,
        txn_type=_classify(description),
        confidence=conf,
        needs_review=review,
        review_reasons=reasons,
        evidence=evidence,
    )


def _parse_csv(stmt_path: Path, ctx: ContextPacket, policy: Policy,
               review_threshold: float) -> list[BankTransaction]:
    txns: list[BankTransaction] = []
    lines = stmt_path.read_text(encoding="utf-8").splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if not ln.startswith("#"))
    reader = csv.DictReader([ln for ln in lines if not ln.startswith("#")])
    data_line = header_idx + 1
    for n, row in enumerate(reader, 1):
        desc = (row.get("description") or "").strip()
        txns.append(_txn_from_row(
            txn_id=f"B-{n:04d}",
            date=row["date"].strip(),
            amount=round(float(row["amount"]), 2),
            currency=(row.get("currency") or ctx.account.currency).strip(),
            description=desc,
            reference=(row.get("reference") or "").strip(),
            counterparty=(row.get("counterparty") or "").strip(),
            evidence=Evidence(
                source_file=stmt_path.name,
                locator=f"line:{data_line + n}",
                snippet=desc[:80],
            ),
            review_threshold=review_threshold,
            policy=policy,
        ))
    return txns


def _parse_mt940(stmt_path: Path, ctx: ContextPacket, policy: Policy,
                 review_threshold: float) -> list[BankTransaction]:
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
        txns.append(_txn_from_row(
            txn_id=f"B-{n:04d}",
            date=date,
            amount=amount,
            currency=ctx.account.currency,
            description=desc,
            reference=(ref or "").replace("//", "").strip(),
            counterparty="",
            evidence=Evidence(
                source_file=stmt_path.name,
                locator=f"line:{i + 1}",
                snippet=(desc or line)[:80],
            ),
            review_threshold=review_threshold,
            policy=policy,
        ))
    return txns


def _parse_pdf_or_synthetic(stmt_path: Path, ctx: ContextPacket, policy: Policy,
                            review_threshold: float, audit: AuditLog
                            ) -> tuple[list[BankTransaction], bool]:
    """Born-digital PDFs ship a sidecar text-layer JSON (<stem>.extracted.json)
    with rows + bounding boxes. Without it, fall back to deterministic
    synthetic data seeded from account+period (spec: synthetic-data fallback)."""
    sidecar = stmt_path.with_suffix(".extracted.json")
    txns: list[BankTransaction] = []
    if sidecar.exists():
        rows = json.loads(sidecar.read_text(encoding="utf-8"))["rows"]
        audit.step(
            f"PDF text layer found ({sidecar.name}); {len(rows)} rows with bounding boxes")
        for n, row in enumerate(rows, 1):
            desc = row["description"]
            bbox = row.get("bbox", [0, 0, 0, 0])
            txns.append(_txn_from_row(
                txn_id=f"B-{n:04d}",
                date=row["date"],
                amount=round(float(row["amount"]), 2),
                currency=row.get("currency", ctx.account.currency),
                description=desc,
                reference=row.get("reference", ""),
                counterparty=row.get("counterparty", ""),
                evidence=Evidence(
                    source_file=stmt_path.name,
                    locator=f"page:{row.get('page', 1)},bbox:{bbox}",
                    snippet=desc[:80],
                ),
                review_threshold=review_threshold,
                policy=policy,
            ))
        return txns, False

    audit.decision("No PDF text layer; using deterministic synthetic fallback",
                   "Seeded from account_id+period so re-runs are identical")
    seed = sum(ord(c) for c in ctx.account.account_id + ctx.account.period)
    year, month = ctx.account.period.split("-")
    descs = ["VENDOR PAYMENT ACME", "CUSTOMER DEPOSIT", "MONTHLY SERVICE FEE",
             "PAYROLL ACH BATCH", "WIRE IN - CLIENT"]
    for n in range(1, 6):
        amt = round(((seed * n * 37) % 9000) / 10 + 25, 2) * (1 if n % 2 else -1)
        desc = descs[(seed + n) % len(descs)]
        txns.append(_txn_from_row(
            txn_id=f"B-{n:04d}",
            date=f"{year}-{month}-{min(n * 5, 28):02d}",
            amount=amt,
            currency=ctx.account.currency,
            description=desc,
            reference=f"SYN{seed}{n:02d}",
            counterparty="",
            evidence=Evidence(
                source_file=stmt_path.name,
                locator=f"page:1,bbox:[72,{700 - n * 20},540,{716 - n * 20}]",
                snippet=f"(synthetic) {desc}",
            ),
            review_threshold=review_threshold,
            policy=policy,
        ))
    return txns, True


def run_agent_b(ctx: ContextPacket, run_dir: Path, policy: Policy,
                audit: AuditLog) -> tuple[TransactionsArtifact, list[Finding]]:
    audit.section("Agent B", "Transaction Extraction")
    findings: list[Finding] = []
    stmt_path = Path(ctx.files["bank_statement"])
    review_threshold = float(policy.get("thresholds.extraction_review_confidence", 0.8))
    synthetic = False

    if ctx.account.statement_format == "csv":
        txns = _parse_csv(stmt_path, ctx, policy, review_threshold)
    elif ctx.account.statement_format == "mt940":
        txns = _parse_mt940(stmt_path, ctx, policy, review_threshold)
    else:
        txns, synthetic = _parse_pdf_or_synthetic(
            stmt_path, ctx, policy, review_threshold, audit)

    audit.step(f"Extracted {len(txns)} transactions from {stmt_path.name} "
               f"({ctx.account.statement_format.upper()})")

    computed_closing = round(ctx.account.opening_balance + sum(t.amount for t in txns), 2)
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

    review_count = 0
    for t in txns:
        if not t.needs_review:
            continue
        review_count += 1
        reason_text = ", ".join(t.review_reasons) if t.review_reasons else "low_confidence"
        findings.append(Finding(
            finding_id=f"B-REV-{t.txn_id}", agent="B", category="extraction_review",
            severity="medium", confidence=t.confidence,
            title=f"Ambiguous/truncated description on {t.txn_id}",
            detail=(f"'{t.description}' flagged for manual review "
                    f"(confidence {t.confidence:.2f}, reasons: {reason_text})"),
            evidence=[t.evidence], related_txn_ids=[t.txn_id],
            recommendation="Manual review before GL mapping",
            open_question="What does this memo refer to?",
        ))
        audit.step(
            f"Flagged {t.txn_id} for manual review "
            f"(confidence {t.confidence:.2f}, reasons: {reason_text}): "
            f"'{t.description}'")

    audit.step(f"{review_count} transaction(s) flagged for description review")

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
