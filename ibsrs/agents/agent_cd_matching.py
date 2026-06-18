from __future__ import annotations
import csv
from datetime import date
from itertools import combinations
from pathlib import Path

from ibsrs.policy import Policy
from ibsrs.schemas import (BankTransaction, ContextPacket, Evidence, Finding,
                           GLEntry, MatchPair, MatchResult, TransactionsArtifact,
                           UnmatchedItem)
from ibsrs.utils.io import AuditLog, write_json


def _days_apart(d1: str, d2: str) -> int:
    return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)


def _amounts_equal(a: float, b: float, policy: Policy) -> bool:
    tol_abs = float(policy.get("matching.amount_tolerance_abs", 0.05))
    tol_pct = float(policy.get("matching.amount_tolerance_pct", 0.001))
    return abs(a - b) <= max(tol_abs, abs(a) * tol_pct)


def _token_similarity(a: str, b: str) -> float:
    """Deterministic Jaccard similarity over uppercase word tokens."""
    ta = {w for w in a.upper().split() if len(w) > 2}
    tb = {w for w in b.upper().split() if len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def load_gl_entries(path: Path) -> list[GLEntry]:
    entries: list[GLEntry] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            entries.append(GLEntry(
                gl_id=row["gl_id"], date=row["date"].strip(),
                amount=round(float(row["amount"]), 2),
                account_code=row["account_code"].strip(),
                description=(row.get("description") or "").strip(),
                reference=(row.get("reference") or "").strip(),
            ))
    return entries


def _categorize(item_side: str, txn_type: str, description: str, amount: float,
                ctx: ContextPacket, policy: Policy) -> str:
    """Agent D timing/variance taxonomy."""
    up = description.upper()
    if item_side == "gl":
        if amount < 0 and ("CHECK" in up or "CHK" in up or txn_type == "CHECK"):
            return "outstanding_check"
        if amount > 0:
            return "deposit_in_transit"
        return "timing_difference"
    # bank-side leftovers
    fee_names = [f.get("fee_type", "").upper() for f in ctx.bank_fee_schedule]
    if txn_type == "FEE" or any(f and f in up for f in fee_names):
        return "bank_charge"
    if txn_type == "INTEREST":
        return "bank_interest"
    if "FX" in up or "REVAL" in up or (
            ctx.account.currency != "USD" and ctx.fx_rates
            and abs(amount) <= float(policy.get("fx.revaluation_tolerance_abs", 25.0))):
        return "fx_revaluation"
    return "timing_difference"


def run_agents_cd(ctx: ContextPacket, txn_artifact: TransactionsArtifact,
                  run_dir: Path, policy: Policy, audit: AuditLog, ai=None
                  ) -> tuple[MatchResult, list[Finding]]:
    audit.section("Agents C & D", "GL Matching & Variance/Timing Analysis")
    findings: list[Finding] = []

    gl_path = Path(ctx.files["gl_export"])
    gl_entries = load_gl_entries(gl_path)
    audit.step(f"Loaded {len(gl_entries)} GL entries from {gl_path.name}")

    bank = sorted(txn_artifact.transactions, key=lambda t: (t.date, t.txn_id))
    gl = sorted(gl_entries, key=lambda g: (g.date, g.gl_id))
    date_window = int(policy.get("matching.date_window_days", 3))
    fuzzy_min = float(policy.get("matching.fuzzy_description_threshold", 0.62))
    use_semantic = bool(ai is not None and getattr(ai, "embeddings_available", False))
    sem_threshold = float(policy.get("ai.semantic_match_threshold", 0.75))
    sem_weight = float(policy.get("ai.semantic_weight", 0.7))
    if use_semantic:
        audit.step(f"Semantic matching ENABLED ({ai.embed_model}); blended score = "
                   f"semantic*{sem_weight} + lexical*{round(1 - sem_weight, 2)}, "
                   f"threshold {sem_threshold} (amount/date stay deterministic guards)")

    matched: list[MatchPair] = []
    used_bank: set[str] = set()
    used_gl: set[str] = set()
    ai_assisted_count = 0
    seq = 0

    def _pair(mtype: str, b: BankTransaction, gls: list[GLEntry],
              score: float, why: str, semantic_score: float | None = None,
              exact_score: float | None = None, ai_assisted: bool = False) -> None:
        nonlocal seq
        seq += 1
        matched.append(MatchPair(
            match_id=f"M-{seq:04d}", match_type=mtype,
            bank_txn_ids=[b.txn_id], gl_ids=[g.gl_id for g in gls],
            score=round(score, 3), rationale=why,
            semantic_score=semantic_score, exact_score=exact_score,
            ai_assisted=ai_assisted,
            evidence=[b.evidence] + [Evidence(source_file=gl_path.name,
                                              locator=f"gl_id:{g.gl_id}",
                                              snippet=g.description[:80])
                                     for g in gls],
        ))
        used_bank.add(b.txn_id)
        used_gl.update(g.gl_id for g in gls)

    # Pass 1 - exact reference (strongest evidence)
    for b in bank:
        if b.txn_id in used_bank or not b.reference:
            continue
        for g in gl:
            if g.gl_id in used_gl or not g.reference:
                continue
            if b.reference == g.reference and _amounts_equal(b.amount, g.amount, policy):
                _pair("reference_1to1", b, [g], 1.0,
                      f"Identical reference '{b.reference}' and amount within tolerance")
                break

    # Pass 2 - exact amount on the same value date
    for b in bank:
        if b.txn_id in used_bank:
            continue
        for g in gl:
            if g.gl_id in used_gl:
                continue
            if _amounts_equal(b.amount, g.amount, policy) and b.date == g.date:
                _pair("exact_1to1", b, [g], 0.95,
                      f"Amount {b.amount:,.2f} equal within tolerance on the "
                      f"same value date {b.date}")
                break

    # Pass 3 - description matching (HYBRID: semantic + lexical).
   
    for b in bank:
        if b.txn_id in used_bank:
            continue
        best = None
        best_blend = best_lex = best_sem = 0.0
        for g in gl:
            if g.gl_id in used_gl:
                continue
            if not _amounts_equal(b.amount, g.amount, policy):
                continue
            if _days_apart(b.date, g.date) > date_window:
                continue
            lex = _token_similarity(b.description, g.description)
            if use_semantic:
                sem = ai.semantic_similarity(b.description, g.description) or 0.0
                blend = round(sem * sem_weight + lex * (1 - sem_weight), 4)
            else:
                sem, blend = 0.0, lex
            if blend > best_blend:
                best, best_blend, best_lex, best_sem = g, blend, lex, sem
        if best is None:
            continue
        threshold = sem_threshold if use_semantic else fuzzy_min
        if best_blend >= threshold:
            # AI-assisted = lexical alone would have missed it but semantics rescued it
            ai_assisted = use_semantic and best_lex < fuzzy_min and best_sem >= sem_threshold
            if use_semantic:
                _pair("semantic_1to1" if ai_assisted else "fuzzy_1to1", b, [best],
                      best_blend,
                      f"Blended description match {best_blend:.2f} >= {threshold:.2f} "
                      f"(semantic {best_sem:.2f}, lexical {best_lex:.2f}) "
                      f"('{b.description[:30]}' ~ '{best.description[:30]}')",
                      semantic_score=round(best_sem, 4), exact_score=round(best_lex, 4),
                      ai_assisted=ai_assisted)
                if ai_assisted:
                    ai_assisted_count += 1
            else:
                _pair("fuzzy_1to1", b, [best], 0.5 + best_lex / 2,
                      f"Description similarity {best_lex:.2f} >= {fuzzy_min:.2f} "
                      f"('{b.description[:30]}' ~ '{best.description[:30]}')",
                      exact_score=round(best_lex, 4))

    # Pass 4 - 1:M composite (e.g. one bank batch deposit = several GL postings)
    if bool(policy.get("matching.enable_one_to_many", True)):
        max_parts = int(policy.get("matching.one_to_many_max_parts", 4))
        for b in bank:
            if b.txn_id in used_bank:
                continue
            cands = [g for g in gl if g.gl_id not in used_gl
                     and _days_apart(b.date, g.date) <= date_window
                     and (g.amount > 0) == (b.amount > 0)]
            hit = None
            for k in range(2, min(max_parts, len(cands)) + 1):
                for combo in combinations(cands, k):
                    if _amounts_equal(b.amount, round(sum(g.amount for g in combo), 2), policy):
                        hit = combo
                        break
                if hit:
                    break
            if hit:
                _pair("one_to_many", b, list(hit), 0.9,
                      f"Bank {b.amount:,.2f} equals sum of {len(hit)} GL postings "
                      f"({' + '.join(f'{g.amount:,.2f}' for g in hit)})")

    for m in matched:
        audit.decision(f"Match {m.match_id} [{m.match_type}] bank "
                       f"{m.bank_txn_ids} <-> GL {m.gl_ids} (score {m.score})",
                       m.rationale)
        findings.append(Finding(
            finding_id=f"C-{m.match_id}", agent="C", category="matched",
            severity="info", confidence=m.score, title=f"{m.match_type} match",
            detail=m.rationale, evidence=m.evidence,
            related_txn_ids=m.bank_txn_ids + m.gl_ids,
        ))

    # ---- Agent D: variance & timing categorization -------------------------
    unmatched_bank: list[UnmatchedItem] = []
    for b in bank:
        if b.txn_id in used_bank:
            continue
        cat = _categorize("bank", b.txn_type, b.description, b.amount, ctx, policy)
        unmatched_bank.append(UnmatchedItem(
            side="bank", item_id=b.txn_id, date=b.date, amount=b.amount,
            description=b.description, timing_category=cat))
        audit.decision(f"Unmatched bank {b.txn_id} ({b.amount:,.2f}) -> **{cat}**",
                       f"Type={b.txn_type}; fee schedule / keyword / FX heuristics")
        findings.append(Finding(
            finding_id=f"D-BANK-{b.txn_id}", agent="D", category=f"unmatched_bank:{cat}",
            severity="medium" if cat in ("bank_charge", "bank_interest", "fx_revaluation")
                     else "high",
            confidence=0.9, title=f"Unmatched bank item ({cat})",
            detail=f"{b.date} '{b.description}' {b.amount:,.2f} has no GL counterpart",
            evidence=[b.evidence], related_txn_ids=[b.txn_id],
            recommendation={"bank_charge": "Book service charge journal entry",
                            "bank_interest": "Book interest income journal entry",
                            "fx_revaluation": "Book FX revaluation adjustment",
                            }.get(cat, "Investigate / carry as reconciling item"),
        ))

    unmatched_gl: list[UnmatchedItem] = []
    for g in gl:
        if g.gl_id in used_gl:
            continue
        cat = _categorize("gl", "", g.description, g.amount, ctx, policy)
        unmatched_gl.append(UnmatchedItem(
            side="gl", item_id=g.gl_id, date=g.date, amount=g.amount,
            description=g.description, timing_category=cat))
        audit.decision(f"Unmatched GL {g.gl_id} ({g.amount:,.2f}) -> **{cat}**",
                       "Sign + description keywords (check/deposit) heuristics")
        findings.append(Finding(
            finding_id=f"D-GL-{g.gl_id}", agent="D", category=f"unmatched_gl:{cat}",
            severity="medium" if cat in ("outstanding_check", "deposit_in_transit")
                     else "high",
            confidence=0.9, title=f"Unmatched GL item ({cat})",
            detail=f"{g.date} '{g.description}' {g.amount:,.2f} not on bank statement",
            evidence=[Evidence(source_file=Path(ctx.files['gl_export']).name,
                               locator=f"gl_id:{g.gl_id}", snippet=g.description[:80])],
            related_txn_ids=[g.gl_id],
            recommendation="Timing difference - expect to clear next period"
                           if cat in ("outstanding_check", "deposit_in_transit")
                           else "Investigate booking",
        ))

    rate_bank = round(len(used_bank) / len(bank), 4) if bank else 1.0
    rate_gl = round(len(used_gl) / len(gl), 4) if gl else 1.0
    sem_rate = round(ai_assisted_count / len(bank), 4) if bank else 0.0
    result = MatchResult(run_id=ctx.run_id, matched=matched,
                         unmatched_bank=unmatched_bank, unmatched_gl=unmatched_gl,
                         match_rate_bank=rate_bank, match_rate_gl=rate_gl,
                         semantic_match_rate=sem_rate,
                         ai_assisted_matches=ai_assisted_count)
    if use_semantic:
        audit.decision(f"Semantic matching contributed {ai_assisted_count} AI-assisted "
                       f"match(es) ({sem_rate:.1%} of bank transactions)",
                       "Matches where lexical similarity alone was below threshold but "
                       "embedding similarity confirmed the pairing")

    out = run_dir / "match_result.json"
    write_json(out, result)
    audit.artifact("match_result.json", out)

    # timing_diffs.csv - outstanding items by category
    timing_path = run_dir / "timing_diffs.csv"
    with open(timing_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["side", "item_id", "date", "amount", "category", "description"])
        for it in sorted(unmatched_bank + unmatched_gl,
                         key=lambda x: (x.timing_category, x.item_id)):
            w.writerow([it.side, it.item_id, it.date, f"{it.amount:.2f}",
                        it.timing_category, it.description])
    audit.artifact("timing_diffs.csv", timing_path)
    audit.step(f"Match rates - bank: {rate_bank:.1%}, GL: {rate_gl:.1%}; "
               f"{len(unmatched_bank)} bank + {len(unmatched_gl)} GL items unmatched")
    return result, findings
