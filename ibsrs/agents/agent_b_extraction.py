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