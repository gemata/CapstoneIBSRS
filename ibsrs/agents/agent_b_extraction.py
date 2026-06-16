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


def _parse_csv(stmt_path: Path, ctx: ContextPacket, review_threshold: float
               ) -> list[BankTransaction]:
    txns: list[BankTransaction] = []
    lines = stmt_path.read_text(encoding="utf-8").splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if not ln.startswith("#"))
    reader = csv.DictReader([ln for ln in lines if not ln.startswith("#")])
    data_line = header_idx + 1  # first data row line number (0-based list, 1-based locator)
    for n, row in enumerate(reader, 1):
        desc = (row.get("description") or "").strip()
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}",
            date=row["date"].strip(),
            amount=round(float(row["amount"]), 2),
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

def _parse_pdf_or_synthetic(stmt_path: Path, ctx: ContextPacket,
                            review_threshold: float, audit: AuditLog
                            ) -> tuple[list[BankTransaction], bool]:
    """Born-digital PDFs ship a sidecar text-layer JSON (<stem>.extracted.json)
    with rows + bounding boxes. Without it, fall back to deterministic
    synthetic data seeded from account+period (spec: synthetic-data fallback)."""
    sidecar = stmt_path.with_suffix(".extracted.json")
    txns: list[BankTransaction] = []
    if sidecar.exists():
        rows = json.loads(sidecar.read_text(encoding="utf-8"))["rows"]
        audit.step(f"PDF text layer found ({sidecar.name}); {len(rows)} rows with bounding boxes")
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

    # ---- deterministic synthetic fallback --------------------------------
    audit.decision("No PDF text layer; using deterministic synthetic fallback",
                   "Seeded from account_id+period so re-runs are identical")
    seed = sum(ord(c) for c in ctx.account.account_id + ctx.account.period)
    year, month = ctx.account.period.split("-")
    descs = ["VENDOR PAYMENT ACME", "CUSTOMER DEPOSIT", "MONTHLY SERVICE FEE",
             "PAYROLL ACH BATCH", "WIRE IN - CLIENT"]
    for n in range(1, 6):
        amt = round(((seed * n * 37) % 9000) / 10 + 25, 2) * (1 if n % 2 == 0 else -1)
        desc = descs[(seed + n) % len(descs)]
        conf, review = _confidence(desc, review_threshold)
        txns.append(BankTransaction(
            txn_id=f"B-{n:04d}", date=f"{year}-{month}-{min(n * 5, 28):02d}",
            amount=amt, currency=ctx.account.currency, description=desc,
            reference=f"SYN{seed}{n:02d}", counterparty="",
            txn_type=_classify(desc), confidence=conf, needs_review=review,
            evidence=Evidence(source_file=stmt_path.name,
                              locator=f"page:1,bbox:[72,{700 - n * 20},540,{716 - n * 20}]",
                              snippet=f"(synthetic) {desc}"),
        ))
    return txns, True