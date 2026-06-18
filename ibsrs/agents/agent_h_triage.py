from __future__ import annotations

import csv
import json
from pathlib import Path

from ibsrs.policy import Policy
from ibsrs.schemas import (AIInsights, ContextPacket, Decision, DuplicateReport,
                           ExceptionItem, Finding, GLEntry, JournalEntry,
                           JournalLine, MatchResult, Metrics,
                           TransactionsArtifact)
from ibsrs.utils.io import AuditLog, text_sha256, utc_now_iso, write_json

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _gl_account(ctx: ContextPacket, role: str, fallback: tuple[str, str]) -> tuple[str, str]:
    raw = ctx.gl_account_map.get(role, "")
    if ":" in raw:
        code, name = raw.split(":", 1)
        return code.strip(), name.strip()
    return fallback


def _merge_dedupe(findings: list[Finding], audit: AuditLog) -> list[Finding]:
    """Dedupe by (category, related txn set); keep the most severe/confident."""
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.category, frozenset(f.related_txn_ids) or frozenset({f.finding_id}))
        cur = best.get(key)
        if cur is None or (_SEVERITY_RANK[f.severity], -f.confidence) < \
                (_SEVERITY_RANK[cur.severity], -cur.confidence):
            best[key] = f
    merged = sorted(best.values(),
                    key=lambda f: (_SEVERITY_RANK[f.severity], f.finding_id))
    dropped = len(findings) - len(merged)
    audit.step(f"Merged {len(findings)} findings -> {len(merged)} "
               f"({dropped} duplicate finding(s) collapsed)")
    return merged


def _deterministic_reasoning(decision: Decision, exceptions: list[ExceptionItem],
                             journals: list[JournalEntry], residual: float) -> dict:
    """Rule-based narrative used when the LLM is unavailable (offline mode)."""
    gloss = {
        "CLOSED_CLEAN": "All bank activity reconciled to the GL with no exceptions.",
        "CLOSED_WITH_ADJUSTMENTS": "Reconciled after deterministic adjusting entries; "
                                   "book the suggested journals to close.",
        "ESCALATED": "Controller approval required before close due to high-severity items.",
        "OPEN_EXCEPTIONS": "Period cannot close: unexplained residual / items under investigation.",
    }.get(decision.status, decision.status)
    risks = [f"{e.severity.upper()}: {e.title}" for e in
             sorted(exceptions, key=lambda e: _SEVERITY_RANK[e.severity])[:3]]
    top = sorted(exceptions, key=lambda e: _SEVERITY_RANK[e.severity])
    focus = top[0].next_action if top else "None - account is clean."
    reasoning = (f"Decision **{decision.status}**. {gloss} "
                 f"{decision.exceptions_count} exception(s) and "
                 f"{decision.journals_count} suggested journal(s); unexplained residual "
                 f"{residual:,.2f}. This narrative is rule-generated (LLM offline).")
    return {"reasoning": reasoning, "key_risks": risks, "recommended_focus": focus}


def _write_ai_insights(ctx: ContextPacket, decision: Decision,
                       exceptions: list[ExceptionItem], journals: list[JournalEntry],
                       match_result: MatchResult, residual: float, run_dir: Path,
                       policy: Policy, audit: AuditLog, ai) -> None:
    llm_available = bool(ai is not None and getattr(ai, "llm_available", False))
    generated_by = "deterministic-template"
    payload = None

    if llm_available:
        # Facts are computed deterministically; the LLM only explains them.
        facts = {
            "status": decision.status, "summary": decision.summary,
            "exceptions": [{"id": e.exception_id, "severity": e.severity,
                            "category": e.category, "title": e.title,
                            "route_to": e.route_to, "next_action": e.next_action}
                           for e in exceptions],
            "journals": [{"id": j.je_id, "memo": j.memo, "status": j.status}
                         for j in journals],
            "match_rate_bank": match_result.match_rate_bank,
            "semantic_match_rate": match_result.semantic_match_rate,
            "unexplained_residual": residual, "currency": ctx.account.currency,
        }
        system = (
            "You are a senior financial-close reconciliation analyst. The reconciliation "
            "DECISION and all journal entries have ALREADY been computed deterministically "
            "and are FINAL - never contradict or change them. Explain the result for an "
            "auditor. Return ONLY a JSON object: {\"reasoning\": <2-4 sentence professional "
            "narrative>, \"key_risks\": [<short strings>], \"recommended_focus\": <one "
            "actionable sentence>}.")
        data = ai.chat_json("H", system, json.dumps(facts), model=ai.reasoning_model)
        if isinstance(data, dict) and "reasoning" in data:
            payload = {"reasoning": str(data.get("reasoning", "")).strip(),
                       "key_risks": [str(x) for x in data.get("key_risks", [])][:6],
                       "recommended_focus": str(data.get("recommended_focus", "")).strip()}
            generated_by = ai.reasoning_model

    if payload is None:  # offline / fallback
        payload = _deterministic_reasoning(decision, exceptions, journals, residual)

    llm_state = ai.status.get("llm_state", "unknown") if ai else "disabled"
    insights = AIInsights(
        run_id=ctx.run_id,
        ai_enabled=bool(getattr(ai, "enabled", False)) if ai else False,
        embeddings_available=bool(getattr(ai, "embeddings_available", False)) if ai else False,
        llm_available=llm_available, llm_state=llm_state, generated_by=generated_by,
        ai_reasoning=payload["reasoning"], key_risks=payload["key_risks"],
        recommended_focus=payload["recommended_focus"],
        semantic_match_rate=match_result.semantic_match_rate,
        ai_assisted_matches=match_result.ai_assisted_matches,
        embedding_model=getattr(ai, "embed_model", None)
        if ai and getattr(ai, "embeddings_available", False) else None,
        llm_calls=int(getattr(ai, "call_count", 0)) if ai else 0)
    write_json(run_dir / "ai_insights.json", insights)
    audit.artifact("ai_insights.json", run_dir / "ai_insights.json")

    src = (f"{generated_by} (GPT)" if generated_by not in ("", "deterministic-template")
           else f"deterministic template — LLM unavailable: {llm_state}")
    risks_md = "".join(f"  - {r}\n" for r in payload["key_risks"]) or "  - (none)\n"
    audit.note(
        f"## Agent H - AI Reasoning\n\n"
        f"*Generated by: {src}*\n\n"
        f"{payload['reasoning']}\n\n"
        f"**Key risks:**\n{risks_md}\n"
        f"**Recommended focus:** {payload['recommended_focus']}\n\n"
        f"> Note: this narrative is explanatory only. The reconciliation status, "
        f"routing, and journal entries are produced by deterministic rules for "
        f"SOX auditability and are not altered by the language model.")


def run_agent_h(ctx: ContextPacket, txn_artifact: TransactionsArtifact,
                match_result: MatchResult, dup_report: DuplicateReport,
                gl_entries: list[GLEntry], all_findings: list[Finding],
                run_dir: Path, policy: Policy, audit: AuditLog,
                started_at: str, ai=None) -> tuple[Decision, Metrics]:
    audit.section("Agent H", "Exception Triage & Lead Orchestration")
    acct = ctx.account
    materiality = float(policy.get("thresholds.materiality", 5000.0))
    auto_max = float(policy.get("thresholds.auto_journal_max", 500.0))
    fx_tol = float(policy.get("fx.revaluation_tolerance_abs", 25.0))

    findings = _merge_dedupe(all_findings, audit)
    write_json(run_dir / "findings.json",
               {"run_id": ctx.run_id, "findings": [f.model_dump() for f in findings]})
    audit.artifact("findings.json", run_dir / "findings.json")

    exceptions: list[ExceptionItem] = []
    journals: list[JournalEntry] = []
    je_date = f"{acct.period}-28"
    cash_code, cash_name = _gl_account(ctx, "cash", ("1010", "Cash - Operating"))

    def _add_exception(category: str, severity: str, title: str, detail: str,
                       next_action: str, route: str, txn_ids: list[str],
                       finding_ids: list[str]) -> None:
        exceptions.append(ExceptionItem(
            exception_id=f"EXC-{len(exceptions) + 1:03d}", category=category,
            severity=severity, title=title, detail=detail,
            next_action=f"{next_action} | {policy.get('routing.' + route, route)}",
            route_to=route, related_txn_ids=txn_ids, source_finding_ids=finding_ids))
        audit.decision(f"Exception {exceptions[-1].exception_id} [{category}] -> "
                       f"route **{route}**: {title}", detail)

    def _add_journal(memo: str, finding_id: str, debit: tuple[str, str, float],
                     credit: tuple[str, str, float], status: str) -> JournalEntry:
        je = JournalEntry(
            je_id=f"JE-{len(journals) + 1:03d}", date=je_date, memo=memo,
            source_finding_id=finding_id,
            lines=[JournalLine(account_code=debit[0], account_name=debit[1],
                               debit=round(debit[2], 2)),
                   JournalLine(account_code=credit[0], account_name=credit[1],
                               credit=round(credit[2], 2))],
            status=status,
            erp_payload={
                "erp_system": "SAP-MOCK", "doc_type": "SA",
                "company_code": "1000", "posting_date": je_date,
                "header_text": memo[:50],
                "lines": [
                    {"gl_account": debit[0], "dr_cr": "D", "amount": round(debit[2], 2)},
                    {"gl_account": credit[0], "dr_cr": "C", "amount": round(credit[2], 2)},
                ]},
        )
        journals.append(je)
        audit.decision(f"Journal {je.je_id}: Dr {debit[0]} / Cr {credit[0]} "
                       f"{debit[2]:,.2f} ({status})", memo)
        return je

    # ------------------------------------------------------------------
    # Rule-based triage over merged findings (deterministic order)
    # ------------------------------------------------------------------
    dup_txn_ids = {tid for g in dup_report.groups for tid in g.txn_ids}
    for f in findings:
        # Merge+dedupe (judge): a generic "unmatched" finding whose txn is
        # already explained by a duplicate group is absorbed into the duplicate
        # exception - one root cause must yield one routing, not two.
        if f.category.startswith("unmatched_") and f.related_txn_ids and \
                set(f.related_txn_ids) & dup_txn_ids:
            audit.step(f"Finding {f.finding_id} absorbed into duplicate exception "
                       f"(root cause already explained for "
                       f"{sorted(set(f.related_txn_ids) & dup_txn_ids)})")
            continue

        amt_items = [it for it in match_result.unmatched_bank + match_result.unmatched_gl
                     if it.item_id in f.related_txn_ids]
        amount = amt_items[0].amount if amt_items else 0.0

        if f.category.startswith("unmatched_bank:bank_charge"):
            code, name = _gl_account(ctx, "bank_fees", ("6210", "Bank Fees Expense"))
            je = _add_journal(f"Bank service charge not in GL - {f.detail[:60]}",
                              f.finding_id, (code, name, abs(amount)),
                              (cash_code, cash_name, abs(amount)),
                              "suggested" if abs(amount) <= auto_max else "requires_approval")
            _add_exception("bank_charge", f.severity, f.title, f.detail,
                           f"Auto-journal {je.je_id}" if abs(amount) <= auto_max
                           else f"Approve {je.je_id} (above auto-journal cap {auto_max:,.0f})",
                           "auto_journal" if abs(amount) <= auto_max else "accountant",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_bank:bank_interest"):
            code, name = _gl_account(ctx, "interest_income", ("4210", "Interest Income"))
            je = _add_journal(f"Bank interest not in GL - {f.detail[:60]}",
                              f.finding_id, (cash_code, cash_name, abs(amount)),
                              (code, name, abs(amount)),
                              "suggested" if abs(amount) <= auto_max else "requires_approval")
            _add_exception("bank_interest", f.severity, f.title, f.detail,
                           f"Auto-journal {je.je_id}", "auto_journal",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_bank:fx_revaluation"):
            code, name = _gl_account(ctx, "fx_gain_loss", ("7150", "FX Gain/Loss"))
            if amount < 0:
                je = _add_journal(f"FX revaluation loss - {f.detail[:60]}", f.finding_id,
                                  (code, name, abs(amount)), (cash_code, cash_name, abs(amount)),
                                  "suggested" if abs(amount) <= fx_tol else "requires_approval")
            else:
                je = _add_journal(f"FX revaluation gain - {f.detail[:60]}", f.finding_id,
                                  (cash_code, cash_name, abs(amount)), (code, name, abs(amount)),
                                  "suggested" if abs(amount) <= fx_tol else "requires_approval")
            _add_exception("fx_revaluation", f.severity, f.title, f.detail,
                           f"Book FX adjustment {je.je_id}",
                           "accountant" if abs(amount) <= fx_tol else "controller",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_") and abs(amount) >= materiality:
            _add_exception("material_unmatched", "critical", f.title,
                           f"{f.detail} | Above materiality {materiality:,.0f}",
                           "Controller approval packet created", "controller",
                           f.related_txn_ids, [f.finding_id])

        elif f.category in ("unmatched_gl:outstanding_check",
                            "unmatched_gl:deposit_in_transit"):
            _add_exception(f.category.split(":")[1], "low", f.title,
                           f"{f.detail} | Timing difference, expected to clear next period",
                           "Carry forward on reconciliation statement", "accountant",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_"):
            _add_exception("timing_difference", f.severity, f.title, f.detail,
                           "Investigate and clear before next close", "accountant",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("duplicate:"):
            dup_amt = 0.0
            for t in txn_artifact.transactions:
                if t.txn_id in f.related_txn_ids:
                    dup_amt = abs(t.amount)
                    break
            sus_code, sus_name = _gl_account(ctx, "suspense", ("1999", "Suspense/Clearing"))
            if dup_amt > 0:
                je = _add_journal(f"Reversal of duplicate - {f.detail[:60]}", f.finding_id,
                                  (cash_code, cash_name, dup_amt),
                                  (sus_code, sus_name, dup_amt), "requires_approval")
                action = f"Reversal {je.je_id} pending bank confirmation"
            else:
                action = "Reverse duplicate GL posting"
            _add_exception("duplicate", "high", f.title, f.detail, action,
                           "controller", f.related_txn_ids, [f.finding_id])

        elif f.category == "extraction_review":
            _add_exception("manual_review", "medium", f.title,
                           f"{f.detail} | {f.open_question}",
                           "Obtain full memo from bank portal", "accountant",
                           f.related_txn_ids, [f.finding_id])

        elif f.category == "balance_mismatch" or \
                (f.category == "risk_flag" and f.title == "OPENING_BALANCE_MISMATCH"):
            _add_exception("balance_investigation", "critical", f.title, f.detail,
                           "Do not close period until resolved", "investigation",
                           f.related_txn_ids, [f.finding_id])

        elif f.category == "risk_flag" and f.severity in ("high", "critical"):
            _add_exception("risk_flag", f.severity, f.title, f.detail,
                           f.recommendation or "Review flagged risk", "accountant",
                           f.related_txn_ids, [f.finding_id])

    # ------------------------------------------------------------------
    # Carry-forward: prior-period outstanding items still uncleared
    # ------------------------------------------------------------------
    from ibsrs.schemas import UnmatchedItem
    carried: list[UnmatchedItem] = []
    if "prior_recon" in ctx.files and Path(ctx.files["prior_recon"]).exists():
        prior = json.loads(Path(ctx.files["prior_recon"]).read_text(encoding="utf-8"))
        for it in prior.get("outstanding_items", []):
            cleared = any(
                (it.get("reference") and t.reference == it.get("reference"))
                or abs(t.amount - float(it["amount"])) < 0.005
                for t in txn_artifact.transactions)
            if cleared:
                audit.step(f"Prior outstanding item {it.get('item_id')} "
                           f"({float(it['amount']):,.2f}) cleared the bank this period")
                continue
            ui = UnmatchedItem(side="prior", item_id=str(it.get("item_id", "PRIOR")),
                               date=str(it.get("date", "")),
                               amount=round(float(it["amount"]), 2),
                               description=str(it.get("description", "")),
                               timing_category=str(it.get("category",
                                                          "outstanding_check")))
            carried.append(ui)
            audit.decision(
                f"Carry-forward {ui.item_id}: still outstanding from "
                f"{prior.get('period')} ({ui.amount:,.2f})",
                "No matching bank movement by reference or amount this period")
            _add_exception(ui.timing_category, "low",
                           f"Prior-period item still outstanding ({ui.item_id})",
                           f"{ui.description} {ui.amount:,.2f} from "
                           f"{prior.get('period')} has not cleared the bank",
                           "Carry forward; chase if older than 90 days",
                           "accountant", [ui.item_id], [])

    if carried:  # keep timing_diffs.csv complete: carried items are timing items
        timing_path = run_dir / "timing_diffs.csv"
        fields = ["side", "item_id", "date", "amount", "category", "description"]
        rows = []
        if timing_path.exists():
            with open(timing_path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        rows += [{"side": ui.side, "item_id": ui.item_id, "date": ui.date,
                  "amount": f"{ui.amount:.2f}", "category": ui.timing_category,
                  "description": ui.description} for ui in carried]
        rows.sort(key=lambda r: (r["category"], r["item_id"]))
        with open(timing_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        audit.artifact("timing_diffs.csv (carry-forward items added)", timing_path)

    # ------------------------------------------------------------------
    # Reconciliation statement (bank-to-book)
    # ------------------------------------------------------------------
    dep_in_transit = [i for i in match_result.unmatched_gl
                      if i.timing_category == "deposit_in_transit"] + \
                     [i for i in carried if i.timing_category == "deposit_in_transit"]
    out_checks = [i for i in match_result.unmatched_gl
                  if i.timing_category == "outstanding_check"] + \
                 [i for i in carried if i.timing_category == "outstanding_check"]
    bank_charges = [i for i in match_result.unmatched_bank
                    if i.timing_category == "bank_charge"]
    bank_interest = [i for i in match_result.unmatched_bank
                     if i.timing_category == "bank_interest"]
    fx_items = [i for i in match_result.unmatched_bank
                if i.timing_category == "fx_revaluation"]
    other_bank = [i for i in match_result.unmatched_bank
                  if i.timing_category not in ("bank_charge", "bank_interest",
                                               "fx_revaluation")]

    adj_bank = round(acct.closing_balance + sum(i.amount for i in dep_in_transit)
                     + sum(i.amount for i in out_checks), 2)  # checks are negative
    gl_opening = (acct.gl_opening_balance if acct.gl_opening_balance is not None
                  else acct.opening_balance)
    book_balance = round(gl_opening + sum(g.amount for g in gl_entries), 2)
    adj_book = round(book_balance + sum(i.amount for i in bank_charges)
                     + sum(i.amount for i in bank_interest)
                     + sum(i.amount for i in fx_items), 2)
    diff = round(adj_bank - adj_book, 2)
    open_items_total = round(sum(i.amount for i in other_bank), 2)
    residual = round(diff - open_items_total, 2)  # unexplained portion only
    balanced = abs(residual) < 0.01
    if abs(diff) < 0.01:
        stmt_status = "RECONCILED - balances agree after adjustments"
    elif balanced:
        stmt_status = (f"RECONCILED WITH OPEN ITEMS - difference of {diff:,.2f} is fully "
                       f"attributed to {len(other_bank)} unresolved bank item(s) listed "
                       f"under Open Items; exceptions remain open until actioned")
    else:
        stmt_status = f"NOT RECONCILED - unexplained residual of {residual:,.2f} remains"
    audit.decision(f"Statement result: {stmt_status}",
                   f"adjusted bank {adj_bank:,.2f} vs adjusted book {adj_book:,.2f}; "
                   f"{open_items_total:,.2f} attributed to open items, "
                   f"{residual:,.2f} unexplained")

    # Pipeline decision (judge) - computed BEFORE the statement is rendered so
    # the statement footer and the run status can never contradict each other.
    routes = {e.route_to for e in exceptions}
    if "investigation" in routes or not balanced:
        status = "OPEN_EXCEPTIONS"
    elif "controller" in routes:
        status = "ESCALATED"
    elif exceptions or journals:
        status = "CLOSED_WITH_ADJUSTMENTS"
    else:
        status = "CLOSED_CLEAN"
    decision_gloss = {
        "CLOSED_CLEAN": "confirm and close",
        "CLOSED_WITH_ADJUSTMENTS": "close permitted once suggested adjustments are booked",
        "ESCALATED": "controller approval required before close",
        "OPEN_EXCEPTIONS": "period cannot close until investigated",
    }[status]

    mask = bool(policy.get("compliance.mask_account_numbers", True))
    acct_display = f"****{acct.account_id[-4:]}" if mask else acct.account_id

    def _sect(title: str, items, sign: int = 1) -> str:
        if not items:
            return ""
        rows = "".join(f"| {i.item_id} | {i.date} | {i.description[:45]} "
                       f"| {sign * i.amount:,.2f} |\n" for i in items)
        return (f"\n**{title}**\n\n| Item | Date | Description | Amount |\n"
                f"|---|---|---|---:|\n{rows}")

    open_block = ""
    if other_bank:
        open_block = ("\n## Open Items Requiring Action\n" +
                      _sect("Unreconciled bank items pending exception resolution",
                            other_bank))

    stmt = f"""# Bank Reconciliation Statement

- **Account:** {acct_display} - {acct.account_name} ({acct.bank_name})
- **Period:** {acct.period}  |  **Currency:** {acct.currency}
- **Run ID:** `{ctx.run_id}`

## Bank Side

| | Amount ({acct.currency}) |
|---|---:|
| Balance per bank statement ({acct.period}) | {acct.closing_balance:,.2f} |
| Add: Deposits in transit | {sum(i.amount for i in dep_in_transit):,.2f} |
| Less: Outstanding checks | {abs(sum(i.amount for i in out_checks)):,.2f} |
| **Adjusted bank balance** | **{adj_bank:,.2f}** |
{_sect('Deposits in transit', dep_in_transit)}{_sect('Outstanding checks', out_checks)}
## Book (GL) Side

| | Amount ({acct.currency}) |
|---|---:|
| Balance per general ledger | {book_balance:,.2f} |
| Less: Bank service charges not recorded | {abs(sum(i.amount for i in bank_charges)):,.2f} |
| Add: Bank interest not recorded | {sum(i.amount for i in bank_interest):,.2f} |
| Add/Less: FX revaluation | {sum(i.amount for i in fx_items):,.2f} |
| **Adjusted book balance** | **{adj_book:,.2f}** |
{_sect('Bank charges to book', bank_charges)}{_sect('Bank interest to book', bank_interest)}{_sect('FX revaluation items', fx_items)}{open_block}
## Result

| | Amount ({acct.currency}) |
|---|---:|
| Adjusted bank balance | {adj_bank:,.2f} |
| Adjusted book balance | {adj_book:,.2f} |
| Difference | {diff:,.2f} |
| Attributed to open (unresolved) items | {open_items_total:,.2f} |
| **Unexplained residual** | **{residual:,.2f}** |

**Statement result:** {stmt_status}
**Pipeline decision:** {status} - {decision_gloss}
**Suggested journal entries:** {len(journals)}  |  **Exceptions raised:** {len(exceptions)}
"""
    (run_dir / "recon_statement.md").write_text(stmt, encoding="utf-8")
    audit.artifact("recon_statement.md", run_dir / "recon_statement.md")

    # ------------------------------------------------------------------
    # exceptions.md
    # ------------------------------------------------------------------
    if exceptions:
        rows = "".join(
            f"| {e.exception_id} | {e.severity.upper()} | {e.category} | {e.title} "
            f"| {e.route_to} | {e.next_action} |\n"
            for e in sorted(exceptions, key=lambda e: (_SEVERITY_RANK[e.severity],
                                                       e.exception_id)))
        body = (f"| ID | Severity | Category | Title | Route | Next action |\n"
                f"|---|---|---|---|---|---|\n{rows}")
    else:
        body = "_No exceptions - account reconciles clean._\n"
    exc_md = (f"# Exceptions - Run `{ctx.run_id}`\n\n"
              f"Account {acct_display} | Period {acct.period}\n\n{body}\n"
              f"## Detail\n\n" +
              "".join(f"### {e.exception_id} - {e.title}\n\n- **Severity:** {e.severity}"
                      f"\n- **Category:** {e.category}\n- **Detail:** {e.detail}"
                      f"\n- **Next action:** {e.next_action}"
                      f"\n- **Related items:** {', '.join(e.related_txn_ids) or '-'}"
                      f"\n- **Source findings:** {', '.join(e.source_finding_ids)}\n\n"
                      for e in exceptions))
    (run_dir / "exceptions.md").write_text(exc_md, encoding="utf-8")
    audit.artifact("exceptions.md", run_dir / "exceptions.md")

    write_json(run_dir / "journal_entries.json",
               {"run_id": ctx.run_id, "currency": acct.currency,
                "entries": [j.model_dump() for j in journals]})
    audit.artifact("journal_entries.json", run_dir / "journal_entries.json")

    # ------------------------------------------------------------------
    # Final decision (judge) - status was already fixed above, pre-statement
    # ------------------------------------------------------------------
    ai_used = bool(ai is not None and (getattr(ai, "embeddings_available", False)
                                       or getattr(ai, "llm_available", False)))
    decision = Decision(
        run_id=ctx.run_id, status=status,
        summary=(f"{len(match_result.matched)} matches "
                 f"({match_result.match_rate_bank:.0%} bank match rate), "
                 f"{len(exceptions)} exception(s), {len(journals)} journal(s); "
                 f"unexplained residual {residual:,.2f} {acct.currency}"
                 + (f" ({abs(open_items_total):,.2f} in open unresolved items)"
                    if abs(open_items_total) >= 0.01 else "")),
        exceptions_count=len(exceptions), journals_count=len(journals),
        requires_controller="controller" in routes, ai_assisted=ai_used)
    write_json(run_dir / "decision.json", decision)
    audit.artifact("decision.json", run_dir / "decision.json")
    audit.decision(f"FINAL STATUS: **{status}**",
                   "Routing precedence: investigation/unbalanced > controller > "
                   "adjustments > clean (policy-driven, deterministic - NOT AI-decided)")

    # ------------------------------------------------------------------
    # AI Reasoning narrative (Agent H). The LLM EXPLAINS the deterministic
    # decision in natural language; it does NOT make or change it. Free-text
    # lives in ai_insights.json (never in decision.json) so the deterministic
    # artifacts stay byte-stable.
    # ------------------------------------------------------------------
    _write_ai_insights(ctx, decision, exceptions, journals, match_result,
                       residual, run_dir, policy, audit, ai)

    # ------------------------------------------------------------------
    # Metrics + deterministic hash (timestamps excluded from the hash)
    # ------------------------------------------------------------------
    canonical = json.dumps({
        "decision": decision.model_dump(),
        "exceptions": [e.model_dump() for e in exceptions],
        "journals": [j.model_dump() for j in journals],
        "match": match_result.model_dump(),
        "duplicates": dup_report.model_dump(),
    }, sort_keys=True)
    finished = utc_now_iso()
    auto = sum(1 for e in exceptions if e.route_to == "auto_journal")
    human = len(exceptions) - auto
    n_txn = len(txn_artifact.transactions)
    metrics = Metrics(
        run_id=ctx.run_id, started_at=started_at, finished_at=finished,
        duration_seconds=0.0,
        bank_txn_count=n_txn, gl_entry_count=len(gl_entries),
        match_rate_bank=match_result.match_rate_bank,
        match_rate_gl=match_result.match_rate_gl,
        extraction_avg_confidence=round(
            sum(t.confidence for t in txn_artifact.transactions) / n_txn, 4)
            if n_txn else 1.0,
        duplicate_groups=len(dup_report.groups),
        exception_count=len(exceptions),
        exception_rate=round(len(exceptions) / n_txn, 4) if n_txn else 0.0,
        journal_entries=len(journals),
        auto_resolved=auto, needs_human_review=human,
        deterministic_hash=text_sha256(canonical)[:16],
        semantic_match_rate=match_result.semantic_match_rate,
        ai_assisted_matches=match_result.ai_assisted_matches,
        llm_calls=int(getattr(ai, "call_count", 0)) if ai else 0,
        ai_enabled=bool(getattr(ai, "enabled", False)) if ai else False,
        embeddings_available=bool(getattr(ai, "embeddings_available", False)) if ai else False,
        llm_available=bool(getattr(ai, "llm_available", False)) if ai else False,
    )
    from datetime import datetime
    try:
        t0 = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
        t1 = datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ")
        metrics.duration_seconds = round((t1 - t0).total_seconds(), 3)
    except ValueError:
        pass
    write_json(run_dir / "metrics.json", metrics)
    audit.artifact("metrics.json", run_dir / "metrics.json")
    audit.step(f"Deterministic decision hash: `{metrics.deterministic_hash}` "
               f"(stable across re-runs of identical inputs)")
    return decision, metrics
