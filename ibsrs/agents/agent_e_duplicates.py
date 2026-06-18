from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

from ibsrs.policy import Policy
from ibsrs.schemas import (ContextPacket, DuplicateGroup, DuplicateReport,
                           Evidence, Finding, GLEntry, TransactionsArtifact)
from ibsrs.utils.io import AuditLog, write_json
from ibsrs.agents.agent_cd_matching import _days_apart, _token_similarity


def run_agent_e(ctx: ContextPacket, txn_artifact: TransactionsArtifact,
                gl_entries: list[GLEntry], run_dir: Path, policy: Policy,
                audit: AuditLog) -> tuple[DuplicateReport, list[Finding]]:
    audit.section("Agent E", "Duplicate Detection")
    findings: list[Finding] = []
    groups: list[DuplicateGroup] = []
    near_tol = float(policy.get("duplicates.near_amount_tolerance_abs", 0.05))
    date_win = int(policy.get("duplicates.near_date_window_days", 2))
    seq = 0

    def _add(kind: str, txn_ids: list[str], detail: str, action: str,
             conf: float, severity: str, ev: list[Evidence]) -> None:
        nonlocal seq
        seq += 1
        dup_id = f"DUP-{seq:03d}"
        groups.append(DuplicateGroup(dup_id=dup_id, kind=kind, txn_ids=txn_ids,
                                     detail=detail, suggested_action=action,
                                     confidence=conf))
        audit.decision(f"{dup_id} [{kind}] {txn_ids}: {detail}",
                       "Amount/date/reference/description comparison within policy tolerances")
        findings.append(Finding(
            finding_id=f"E-{dup_id}", agent="E", category=f"duplicate:{kind}",
            severity=severity, confidence=conf, title=f"Duplicate ({kind})",
            detail=detail, evidence=ev, related_txn_ids=txn_ids,
            recommendation=action,
        ))

    bank = sorted(txn_artifact.transactions, key=lambda t: t.txn_id)
    flagged: set[frozenset] = set()

    # --- bank-side exact & near duplicates ---------------------------------
    for a, b in combinations(bank, 2):
        key = frozenset((a.txn_id, b.txn_id))
        if key in flagged:
            continue
        same_ref = bool(a.reference) and a.reference == b.reference
        desc_sim = _token_similarity(a.description, b.description)
        if a.date == b.date and a.amount == b.amount and same_ref and desc_sim >= 0.99:
            flagged.add(key)
            _add("exact_duplicate", [a.txn_id, b.txn_id],
                 f"Identical date/amount/reference: {a.date} {a.amount:,.2f} ref '{a.reference}'",
                 "Reverse one posting; confirm with bank before close",
                 0.99, "high", [a.evidence, b.evidence])
        elif (abs(a.amount - b.amount) <= near_tol
              and _days_apart(a.date, b.date) <= date_win
              and (same_ref or desc_sim >= 0.8)):
            flagged.add(key)
            _add("near_duplicate", [a.txn_id, b.txn_id],
                 f"Same/near amount {a.amount:,.2f}~{b.amount:,.2f} within "
                 f"{date_win}d ({a.date}/{b.date}); similarity {desc_sim:.2f} - "
                 f"possible double-processed payment (e.g. ACH ran twice)",
                 "Suggest reversal journal entry; controller review required",
                 0.9, "high", [a.evidence, b.evidence])

    # --- GL interface double-entries ---------------------------------------
    gl_sorted = sorted(gl_entries, key=lambda g: g.gl_id)
    gl_file = Path(ctx.files["gl_export"]).name
    for a, b in combinations(gl_sorted, 2):
        if a.date == b.date and a.amount == b.amount and \
                _token_similarity(a.description, b.description) >= 0.8:
            _add("interface_double_entry", [a.gl_id, b.gl_id],
                 f"Same-day same-amount GL postings {a.date} {a.amount:,.2f} "
                 f"('{a.description[:40]}') - likely interface/system double-entry",
                 "Reverse duplicate GL posting",
                 0.85, "high",
                 [Evidence(source_file=gl_file, locator=f"gl_id:{g.gl_id}",
                           snippet=g.description[:80]) for g in (a, b)])

    # --- cross-period duplicates vs prior reconciliation --------------------
    if bool(policy.get("duplicates.cross_period_lookback", True)) and \
            "prior_recon" in ctx.files and Path(ctx.files["prior_recon"]).exists():
        prior = json.loads(Path(ctx.files["prior_recon"]).read_text(encoding="utf-8"))
        prior_txns = prior.get("transactions", [])
        for t in bank:
            for p in prior_txns:
                if t.reference and t.reference == p.get("reference") and \
                        abs(t.amount - float(p.get("amount", 0))) <= near_tol:
                    _add("cross_period_duplicate", [t.txn_id, p.get("txn_id", "prior")],
                         f"Reference '{t.reference}' {t.amount:,.2f} already settled in "
                         f"prior period {prior.get('period')}",
                         "Verify not double-settled across cut-off; reverse if confirmed",
                         0.8, "medium",
                         [t.evidence, Evidence(source_file=Path(ctx.files['prior_recon']).name,
                                               locator=f"transactions[ref={t.reference}]",
                                               snippet=p.get("description", "")[:80])])

    report = DuplicateReport(run_id=ctx.run_id, groups=groups)
    out = run_dir / "duplicates.json"
    write_json(out, report)
    audit.artifact("duplicates.json", out)
    audit.step(f"Duplicate scan complete: {len(groups)} group(s) found")
    return report, findings
