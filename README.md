# IBSRS — Intelligent Bank Reconciliation System

Genpact Capstone prototype: a **deterministic, file-based multi-agent pipeline**
that automates month-end bank reconciliation and GL matching, with a complete
audit trail, ERP-ready journal payloads, a **FastAPI** backend, and a
**Streamlit** UI.

## Hybrid AI architecture (deterministic-first)

AI handles *semantic understanding and reasoning*; deterministic Python handles
*financial math and compliance*. The AI layer is **optional and additive** — the
whole system runs offline with zero setup, and AI never changes the financial
decision or the journal math (so the graded Deterministic-Outputs and SOX
criteria always hold).

| Agent | Mode | AI role |
|---|---|---|
| A — Intake | rule-based | format/ risk detection unchanged |
| B — Extraction | **hybrid** | CSV/MT940 rule-based; PDF → GPT-4o-mini → synthetic fallback |
| C — Matching | **hybrid** | `sentence-transformers` semantic score blended with lexical; amount ±tol / date ±window stay hard deterministic guards |
| D — Variance / E — Duplicates | rule-based | unchanged (financial/SOX accuracy) |
| H — Triage/Orchestrator | **hybrid** | rules decide status + journals; GPT-4o writes the *"AI Reasoning"* narrative only |

Enable it:
```powershell
pip install -r requirements.txt          # one file: core + AI (sentence-transformers, openai, dotenv, pypdf)
copy .env.example .env                   # add OPENAI_API_KEY (LLM features; embeddings need no key)
python -m ibsrs.pipeline --ai            # or --no-ai to force deterministic
```
If a library or key is missing, that capability reports unavailable and the
rule-based path runs instead. Every AI/LLM call is logged to
`runs/<id>/llm_calls.log`; the natural-language reasoning lands in
`runs/<id>/ai_insights.json` (kept out of `decision.json` so deterministic
artifacts stay byte-stable). New AI knobs live under `ai:` in `policy/policy.yaml`
(`semantic_match_threshold`, `semantic_weight`, models).

## Architecture

```
Recon Bundle (manifest.yaml + statement + GL + prior recon + fees + FX)
        │
        ▼
┌─ Agent A ─ Statement Intake & Context (gatekeeper) ──► context.json
│           format classification · evidence index · risk flags
▼
┌─ Agent B ─ Transaction Extraction ───────────────────► transactions.json
│           CSV / MT940 / PDF (+ synthetic fallback) · confidence scoring
│           bounding boxes for PDFs · balance roll-forward
▼
┌─ Agents C&D ─ GL Matching + Variance/Timing ─────────► match_result.json
│           reference / exact / fuzzy / 1:M passes        timing_diffs.csv
│           outstanding checks · deposits in transit · charges · FX
▼
┌─ Agent E ─ Duplicate Detection ──────────────────────► duplicates.json
│           exact / near (cut-off) / interface double-entry / cross-period
▼
┌─ Agent H ─ Exception Triage + Lead Orchestration ────► exceptions.md
            merge · dedupe · prioritize · policy rules    journal_entries.json
            judge: final decision + idempotency hash      recon_statement.md
                                                          decision.json
                                                          metrics.json
        (every step appends to audit_log.md — SOX traceability)
```

**Coordination model:** agents only communicate through structured files in a
shared run directory `runs/<run_id>/`. The `run_id` is a hash of bundle
content + policy, so **identical inputs ⇒ identical run directory, identical
decisions, identical `deterministic_hash`** (idempotent re-runs).

## Quickstart

```powershell
python -m pip install -r requirements.txt
python scripts\generate_data.py        # create the 9 test Recon Bundles
python -m ibsrs.pipeline               # one-command demo: run all bundles
python -m pytest tests -q              # 24 tests

# Full stack
uvicorn app.api:app --port 8000        # backend  (Swagger at /docs)
streamlit run app/ui.py                # frontend (http://localhost:8501)
```

## Run artifacts (per spec)

| Artifact | Producer | Content |
|---|---|---|
| `context.json` | A | account meta, GL map, fee schedule, FX, evidence index, risk flags |
| `transactions.json` | B | normalized bank transactions + confidence + evidence |
| `match_result.json` | C | matched pairs (+rationale) & unmatched items |
| `timing_diffs.csv` | D | outstanding items by category |
| `duplicates.json` | E | duplicate groups + suggested reversals |
| `findings.json` | all | unified findings schema (severity, confidence, evidence) |
| `exceptions.md` | H | unreconciled items + next actions + routing |
| `journal_entries.json` | H | balanced suggested entries + ERP-ready payloads |
| `recon_statement.md` | H | human-readable bank-to-book reconciliation |
| `decision.json` | H | final judge decision |
| `audit_log.md` | all | step-by-step trace, every decision with rationale |
| `metrics.json` | H | match rates, exception rates, confidence, deterministic hash |

## Test scenarios (data/bundles/)

| # | Bundle | Expected outcome |
|---|---|---|
| 1 | `scenario_01_clean` | CLOSED_CLEAN — all 5 matched by reference |
| 2 | `scenario_02_bank_charge` | CLOSED_WITH_ADJUSTMENTS — auto journal for $45 fee |
| 3 | `scenario_03_outstanding_check` | CLOSED_WITH_ADJUSTMENTS — prior-month check carried forward |
| 4 | `scenario_04_duplicate_ach` | ESCALATED — ACH paid twice, reversal JE for controller |
| 5 | `scenario_05_fx_revaluation` | CLOSED_WITH_ADJUSTMENTS — EUR account, FX adjustment JE |
| 6 | `scenario_06_high_value_unmatched` | ESCALATED — $12,750 wire above materiality (+ 1:M match demo) |
| 7 | `scenario_07_truncated_memo` | CLOSED_WITH_ADJUSTMENTS — ambiguous memo → manual review |
| 8 | `scenario_08_opening_mismatch` | OPEN_EXCEPTIONS — opening ≠ prior closing → investigation |
| 9 | `scenario_09_minimal_mt940` | CLOSED_CLEAN — two-transaction MT940 account |

## Policy pack (`policy/policy.yaml`)

All thresholds and tolerances are YAML-driven — no code edits needed:
materiality, auto-journal cap, fuzzy-match threshold, date windows, duplicate
tolerances, FX tolerance, privacy masking. The Streamlit sidebar edits the
policy live; re-running the same bundle then visibly changes decisions
(e.g. materiality 5000 → 100 flips scenario 7 to ESCALATED — covered by a test).

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/bundles` | list Recon Bundles (incl. uploads) |
| GET/PUT | `/policy` | read / hot-update policy pack |
| POST | `/runs` | run pipeline on a bundle |
| GET | `/runs`, `/runs/{id}` | run history / run summary |
| GET | `/runs/{id}/artifacts/{name}` | fetch any artifact |
| POST | `/upload-statement` | single-statement intake (input mode (a)) |

## Project layout

```
ibsrs/            core package: schemas, policy loader, pipeline
  agents/         agent_a_intake, agent_b_extraction, agent_cd_matching,
                  agent_e_duplicates, agent_h_triage
  utils/io.py     deterministic JSON writer + audit logger
app/api.py        FastAPI backend          app/ui.py   Streamlit frontend
policy/           policy.yaml              scripts/    generate_data.py
data/bundles/     9 test Recon Bundles     runs/       run artifacts
tests/            pytest suite (24 tests)
```

## Success criteria mapping

- **Extraction accuracy** — confidence scoring + needs-review flags + balance
  roll-forward check (`transactions.json`, tested).
- **Matching precision** — 4 deterministic passes with rationale per match;
  scenario tests assert exact match counts and types.
- **Deterministic outputs** — sorted-key JSON, content-derived run ids,
  `deterministic_hash` asserted byte-identical across re-runs in tests.
- **Auditability** — every finding carries evidence pointers (tested); every
  agent decision logged with rationale in `audit_log.md`.
