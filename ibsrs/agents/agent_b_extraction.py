from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
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
def _rate_to_base(currency: str, ctx: ContextPacket) -> float:
    currency = currency.strip().upper()
    account_currency = ctx.account.currency.strip().upper()

    if currency == account_currency:
        return 1.0

    if currency in ctx.fx_rates:
        return float(ctx.fx_rates[currency])

    raise ValueError(f"Missing FX rate for {currency}")


def _convert_currency(amount: float, from_currency: str, ctx: ContextPacket) -> float:
    from_currency = from_currency.strip().upper()
    account_currency = ctx.account.currency.strip().upper()

    if from_currency == account_currency:
        return round(amount, 2)

    amount_in_base = amount * _rate_to_base(from_currency, ctx)
    account_rate = _rate_to_base(account_currency, ctx)

    return round(amount_in_base / account_rate, 2)

def _normalize_date(value: str) -> str:
    value = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d.%m.%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Unsupported date format: {value}")

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


def _balance_tolerance(policy: Policy) -> float:
    return float(policy.get("extraction.balance_tolerance_abs", 0.005))


def _row_fingerprint(row: dict) -> tuple:
    return (
        row.get("date", ""),
        round(float(row.get("amount", 0)), 2),
        (row.get("reference") or "").strip(),
        (row.get("description") or "").strip()[:40],
    )


def _sort_pdf_rows(rows: list[dict]) -> list[dict]:
    def sort_key(row: dict) -> tuple:
        page = int(row.get("page", 1))
        bbox = row.get("bbox", [0, 0, 0, 0])
        y = bbox[1] if len(bbox) > 1 else 0
        return (page, -y)

    return sorted(rows, key=sort_key)


def _dedupe_pdf_rows(rows: list[dict]) -> tuple[list[dict], int]:
    seen: set[tuple] = set()
    deduped: list[dict] = []
    removed = 0
    for row in rows:
        fp = _row_fingerprint(row)
        if fp in seen:
            removed += 1
            continue
        seen.add(fp)
        deduped.append(row)
    return deduped, removed


def _validate_page_balance_chain(
    page_summaries: list[dict],
    opening: float,
    closing: float,
    tolerance: float,
) -> list[str]:
    if not page_summaries:
        return []

    errors: list[str] = []
    first = page_summaries[0]
    last = page_summaries[-1]
    if abs(float(first["opening_balance"]) - opening) > tolerance:
        errors.append(
            f"Page 1 opening {float(first['opening_balance']):,.2f} "
            f"does not match statement opening {opening:,.2f}")
    for i in range(len(page_summaries) - 1):
        cur_close = float(page_summaries[i]["closing_balance"])
        next_open = float(page_summaries[i + 1]["opening_balance"])
        if abs(cur_close - next_open) > tolerance:
            errors.append(
                f"Page {page_summaries[i]['page']} closing {cur_close:,.2f} "
                f"does not carry to page {page_summaries[i + 1]['page']} "
                f"opening {next_open:,.2f}")
    if abs(float(last["closing_balance"]) - closing) > tolerance:
        errors.append(
            f"Final page closing {float(last['closing_balance']):,.2f} "
            f"does not match statement closing {closing:,.2f}")
    return errors


def _validate_page_txn_totals(
    rows: list[dict],
    page_summaries: list[dict],
    tolerance: float,
) -> list[str]:
    by_page: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_page[int(row.get("page", 1))].append(row)

    errors: list[str] = []
    for summary in page_summaries:
        page = int(summary["page"])
        page_open = float(summary["opening_balance"])
        page_close = float(summary["closing_balance"])
        net = round(sum(float(r["amount"]) for r in by_page.get(page, [])), 2)
        computed = round(page_open + net, 2)
        if abs(computed - page_close) > tolerance:
            errors.append(
                f"Page {page} roll-forward mismatch: opening {page_open:,.2f} + "
                f"net {net:,.2f} = {computed:,.2f} vs page closing {page_close:,.2f}")
    return errors


def _aggregate_pdf_rows(
    rows: list[dict],
    sidecar: dict,
    ctx: ContextPacket,
    policy: Policy,
    audit: AuditLog,
) -> tuple[list[dict], list[str]]:
    """Sort, dedupe, and validate multi-page PDF extraction rows."""
    sorted_rows = _sort_pdf_rows(rows)
    deduped, dup_count = _dedupe_pdf_rows(sorted_rows)
    pages = sorted({int(r.get("page", 1)) for r in deduped})
    audit.step(
        f"PDF aggregation across {len(pages)} page(s): {len(deduped)} unique rows"
        + (f" ({dup_count} duplicate row(s) removed at page boundaries)"
           if dup_count else ""))

    tolerance = _balance_tolerance(policy)
    opening = float(sidecar.get("opening_balance", ctx.account.opening_balance))
    closing = float(sidecar.get("closing_balance", ctx.account.closing_balance))
    page_summaries = sidecar.get("page_summaries", [])

    warnings: list[str] = []
    warnings.extend(_validate_page_balance_chain(
        page_summaries, opening, closing, tolerance))
    if page_summaries:
        warnings.extend(_validate_page_txn_totals(deduped, page_summaries, tolerance))

    computed = round(opening + sum(float(r["amount"]) for r in deduped), 2)
    if abs(computed - closing) > tolerance:
        warnings.append(
            f"Aggregated document roll-forward mismatch: opening {opening:,.2f} + "
            f"net {computed - opening:,.2f} = {computed:,.2f} vs closing {closing:,.2f}")
    if abs(computed - ctx.account.closing_balance) > tolerance:
        warnings.append(
            f"Aggregated closing {computed:,.2f} does not match manifest closing "
            f"{ctx.account.closing_balance:,.2f}")

    for warning in warnings:
        audit.decision(warning, "Multi-page PDF balance consistency check")

    if not warnings and page_summaries:
        audit.decision(
            "Multi-page PDF balances reconcile across all pages and statement totals",
            "Per-page carry-forward and document roll-forward within tolerance")

    return deduped, warnings


def _statement_rollforward(
    opening: float,
    closing: float,
    txns: list[BankTransaction],
    policy: Policy,
) -> tuple[float, bool]:
    tolerance = _balance_tolerance(policy)
    computed = round(opening + sum(t.amount for t in txns), 2)
    return computed, abs(computed - closing) < tolerance


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
        raw_amount = float(row["amount"])
        raw_currency = (row.get("currency") or ctx.account.currency).strip()
        amount = _convert_currency(raw_amount, raw_currency, ctx)
        txns.append(_txn_from_row(
            txn_id=f"B-{n:04d}",
            date=_normalize_date(row["date"]),
            amount=amount,
            currency=ctx.account.currency,
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
                            ) -> tuple[list[BankTransaction], bool, list[str]]:
    """Born-digital PDFs ship a sidecar text-layer JSON (<stem>.extracted.json)
    with rows + bounding boxes. Without it, fall back to deterministic
    synthetic data seeded from account+period (spec: synthetic-data fallback)."""
    sidecar_path = stmt_path.with_suffix(".extracted.json")
    txns: list[BankTransaction] = []
    balance_warnings: list[str] = []
    if sidecar_path.exists():
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        rows, balance_warnings = _aggregate_pdf_rows(
            sidecar.get("rows", []), sidecar, ctx, policy, audit)
        audit.step(
            f"PDF text layer found ({sidecar_path.name}); "
            f"{len(rows)} aggregated rows with bounding boxes")
        for n, row in enumerate(rows, 1):
            desc = row["description"]
            bbox = row.get("bbox", [0, 0, 0, 0])
            txns.append(_txn_from_row(
                txn_id=f"B-{n:04d}",
                date=_normalize_date(row["date"]),
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
        return txns, False, balance_warnings

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
    return txns, True, balance_warnings


def run_agent_b(ctx: ContextPacket, run_dir: Path, policy: Policy,
                audit: AuditLog) -> tuple[TransactionsArtifact, list[Finding]]:
    audit.section("Agent B", "Transaction Extraction")
    findings: list[Finding] = []
    stmt_path = Path(ctx.files["bank_statement"])
    review_threshold = float(policy.get("thresholds.extraction_review_confidence", 0.8))
    synthetic = False
    pdf_balance_warnings: list[str] = []

    if ctx.account.statement_format == "csv":
        txns = _parse_csv(stmt_path, ctx, policy, review_threshold)
    elif ctx.account.statement_format == "mt940":
        txns = _parse_mt940(stmt_path, ctx, policy, review_threshold)
    else:
        txns, synthetic, pdf_balance_warnings = _parse_pdf_or_synthetic(
            stmt_path, ctx, policy, review_threshold, audit)

    audit.step(f"Extracted {len(txns)} transactions from {stmt_path.name} "
               f"({ctx.account.statement_format.upper()})")

    computed_closing, reconciles = _statement_rollforward(
        ctx.account.opening_balance, ctx.account.closing_balance, txns, policy)
    audit.decision(
        f"Balance roll-forward: opening {ctx.account.opening_balance:,.2f} + "
        f"net movement = {computed_closing:,.2f}; statement closing "
        f"{ctx.account.closing_balance:,.2f} -> "
        f"{'RECONCILES' if reconciles else 'DOES NOT RECONCILE'}",
        "Sum of extracted amounts checked against statement closing balance")

    for i, warning in enumerate(pdf_balance_warnings, 1):
        findings.append(Finding(
            finding_id=f"B-BAL-PDF-{i:03d}", agent="B",
            category="pdf_balance_mismatch", severity="high", confidence=1.0,
            title="Multi-page PDF balance inconsistency",
            detail=warning,
            evidence=[Evidence(source_file=stmt_path.name, locator="pdf_aggregation",
                               snippet=warning[:120])],
            recommendation="Verify page extraction completeness and page carry-forward balances",
        ))

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
