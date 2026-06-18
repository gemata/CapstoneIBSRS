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
        key = (f.category, frozenset(f.related_txn_ids)
               or frozenset({f.finding_id}))
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
    llm_available = bool(ai is not None and getattr(
        ai, "llm_available", False))
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
        data = ai.chat_json("H", system, json.dumps(facts),
                            model=ai.reasoning_model)
        if isinstance(data, dict) and "reasoning" in data:
            payload = {"reasoning": str(data.get("reasoning", "")).strip(),
                       "key_risks": [str(x) for x in data.get("key_risks", [])][:6],
                       "recommended_focus": str(data.get("recommended_focus", "")).strip()}
            generated_by = ai.reasoning_model

    if payload is None:  # offline / fallback
        payload = _deterministic_reasoning(
            decision, exceptions, journals, residual)

    llm_state = ai.status.get("llm_state", "unknown") if ai else "disabled"
    insights = AIInsights(
        run_id=ctx.run_id,
        ai_enabled=bool(getattr(ai, "enabled", False)) if ai else False,
        embeddings_available=bool(
            getattr(ai, "embeddings_available", False)) if ai else False,
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
    risks_md = "".join(
        f"  - {r}\n" for r in payload["key_risks"]) or "  - (none)\n"
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
    cash_code, cash_name = _gl_account(
        ctx, "cash", ("1010", "Cash - Operating"))

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
                    {"gl_account": debit[0], "dr_cr": "D",
                        "amount": round(debit[2], 2)},
                    {"gl_account": credit[0], "dr_cr": "C",
                        "amount": round(credit[2], 2)},
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
            code, name = _gl_account(
                ctx, "bank_fees", ("6210", "Bank Fees Expense"))
            je = _add_journal(f"Bank service charge not in GL - {f.detail[:60]}",
                              f.finding_id, (code, name, abs(amount)),
                              (cash_code, cash_name, abs(amount)),
                              "suggested" if abs(amount) <= auto_max else "requires_approval")
            _add_exception("bank_charge", f.severity, f.title, f.detail,
                           f"Auto-journal {je.je_id}" if abs(amount) <= auto_max
                           else f"Approve {je.je_id} (above auto-journal cap {auto_max:,.0f})",
                           "auto_journal" if abs(
                               amount) <= auto_max else "accountant",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_bank:bank_interest"):
            code, name = _gl_account(
                ctx, "interest_income", ("4210", "Interest Income"))
            je = _add_journal(f"Bank interest not in GL - {f.detail[:60]}",
                              f.finding_id, (cash_code,
                                             cash_name, abs(amount)),
                              (code, name, abs(amount)),
                              "suggested" if abs(amount) <= auto_max else "requires_approval")
            _add_exception("bank_interest", f.severity, f.title, f.detail,
                           f"Auto-journal {je.je_id}", "auto_journal",
                           f.related_txn_ids, [f.finding_id])

        elif f.category.startswith("unmatched_bank:fx_revaluation"):
            code, name = _gl_account(
                ctx, "fx_gain_loss", ("7150", "FX Gain/Loss"))
            if amount < 0:
                je = _add_journal(f"FX revaluation loss - {f.detail[:60]}", f.finding_id,
                                  (code, name, abs(amount)), (cash_code,
                                                              cash_name, abs(amount)),
                                  "suggested" if abs(amount) <= fx_tol else "requires_approval")
            else:
                je = _add_journal(f"FX revaluation gain - {f.detail[:60]}", f.finding_id,
                                  (cash_code, cash_name, abs(amount)
                                   ), (code, name, abs(amount)),
                                  "suggested" if abs(amount) <= fx_tol else "requires_approval")
            _add_exception("fx_revaluation", f.severity, f.title, f.detail,
                           f"Book FX adjustment {je.je_id}",
                           "accountant" if abs(
                               amount) <= fx_tol else "controller",
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
            sus_code, sus_name = _gl_account(
                ctx, "suspense", ("1999", "Suspense/Clearing"))
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
