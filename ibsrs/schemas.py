
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field
------------------------------------------------------------------------


class Evidence(BaseModel):
    """Pointer that lets an auditor trace a conclusion back to its source."""
    source_file: str
    # e.g. "row:14" for CSV, "line:62" for MT940, "page:1,bbox:[72,540,310,556]" for PDF
    locator: str
    snippet: str = ""


class Finding(BaseModel):
    """One unit of agent output, consolidated by the orchestrator (Agent H)."""
    finding_id: str
    agent: str  # A | B | C | D | E | H
    category: str  # e.g. matched, unmatched_bank, duplicate, risk_flag ...
    severity: str = "info"  # info | low | medium | high | critical
    confidence: float = 1.0
    title: str
    detail: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    recommendation: str = ""
    open_question: str = ""  # human-oversight hook when automation is unsure
    related_txn_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent A - context packet
# ---------------------------------------------------------------------------

class AccountMeta(BaseModel):
    account_id: str
    account_name: str
    currency: str
    bank_name: str
    period: str  # YYYY-MM
    statement_format: str  # csv | mt940 | pdf
    opening_balance: float
    closing_balance: float
    # book opening, if != bank opening
    gl_opening_balance: Optional[float] = None


class RiskFlag(BaseModel):
    code: str  # FX_EXPOSURE | HIGH_VALUE | FORMAT_INCONSISTENCY | OPENING_BALANCE_MISMATCH
    detail: str
    severity: str = "medium"


class ContextPacket(BaseModel):
    """Output of Agent A -> context.json"""
    run_id: str
    bundle_path: str
    account: AccountMeta
    prior_period_closing_balance: Optional[float] = None
    gl_account_map: dict[str, str] = Field(default_factory=dict)
    bank_fee_schedule: list[dict] = Field(default_factory=list)
    fx_rates: dict[str, float] = Field(default_factory=dict)
    evidence_index: list[Evidence] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)  # logical name -> path


# ---------------------------------------------------------------------------
# Agent B - normalized transactions
# ---------------------------------------------------------------------------

class BankTransaction(BaseModel):
    """Normalized bank-statement transaction -> transactions.json"""
    txn_id: str
    date: str  # ISO YYYY-MM-DD
    amount: float  # signed; +credit (deposit), -debit (payment)
    currency: str
    description: str
    reference: str = ""
    counterparty: str = ""
    txn_type: str = ""  # ACH | WIRE | CHECK | FEE | INTEREST | TRANSFER | OTHER
    confidence: float = 1.0
    needs_review: bool = False  # truncated/ambiguous description
    extraction_method: str = "rule"  # rule | llm | synthetic
    evidence: Evidence


class TransactionsArtifact(BaseModel):
    run_id: str
    account_id: str
    period: str
    currency: str
    opening_balance: float
    closing_balance: float
    computed_closing_balance: float
    balance_reconciles: bool
    transactions: list[BankTransaction]
    synthetic_fallback_used: bool = False


# ---------------------------------------------------------------------------
# Agents C & D - match result + timing differences
# ---------------------------------------------------------------------------

class GLEntry(BaseModel):
    gl_id: str
    date: str
    amount: float
    account_code: str
    description: str
    reference: str = ""


class MatchPair(BaseModel):
    match_id: str
    match_type: str  # exact_1to1 | reference_1to1 | fuzzy_1to1 | semantic_1to1 | one_to_many
    bank_txn_ids: list[str]
    gl_ids: list[str]
    score: float
    rationale: str
    semantic_score: Optional[float] = None  # sentence-transformers cosine (AI)
    # deterministic lexical/token score
    exact_score: Optional[float] = None
    ai_assisted: bool = False                # True if semantics decided the match
    evidence: list[Evidence] = Field(default_factory=list)


class UnmatchedItem(BaseModel):
    side: str  # bank | gl
    item_id: str
    date: str
    amount: float
    description: str
    timing_category: str = "uncategorized"
    # outstanding_check | deposit_in_transit | bank_charge | bank_interest |
    # fx_revaluation | timing_difference | unknown


class MatchResult(BaseModel):
    """Output of Agents C&D -> match_result.json"""
    run_id: str
    matched: list[MatchPair]
    unmatched_bank: list[UnmatchedItem]
    unmatched_gl: list[UnmatchedItem]
    match_rate_bank: float
    match_rate_gl: float
    semantic_match_rate: float = 0.0   # fraction of bank txns matched via AI semantics
    ai_assisted_matches: int = 0


# ---------------------------------------------------------------------------
# Agent E - duplicates
# ---------------------------------------------------------------------------

class DuplicateGroup(BaseModel):
    dup_id: str
    kind: str  # exact_duplicate | near_duplicate | interface_double_entry | cross_period_duplicate
    txn_ids: list[str]
    detail: str
    suggested_action: str
    confidence: float


class DuplicateReport(BaseModel):
    run_id: str
    groups: list[DuplicateGroup]


# ---------------------------------------------------------------------------
# Agent H - journal entries, decision, metrics
# ---------------------------------------------------------------------------

class JournalLine(BaseModel):
    account_code: str
    account_name: str
    debit: float = 0.0
    credit: float = 0.0


class JournalEntry(BaseModel):
    je_id: str
    date: str
    memo: str
    source_finding_id: str
    lines: list[JournalLine]
    status: str = "suggested"  # suggested | requires_approval
    erp_payload: dict = Field(default_factory=dict)


class ExceptionItem(BaseModel):
    exception_id: str
    category: str
    severity: str
    title: str
    detail: str
    next_action: str
    route_to: str  # auto_journal | accountant | controller | investigation
    related_txn_ids: list[str] = Field(default_factory=list)
    source_finding_ids: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """Final orchestrator decision -> decision.json"""
    run_id: str
    status: str  # CLOSED_CLEAN | CLOSED_WITH_ADJUSTMENTS | OPEN_EXCEPTIONS | ESCALATED
    summary: str
    exceptions_count: int
    journals_count: int
    requires_controller: bool
    ai_assisted: bool = False  # whether any AI capability contributed this run


class Metrics(BaseModel):
    """metrics.json"""
    run_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    bank_txn_count: int
    gl_entry_count: int
    match_rate_bank: float
    match_rate_gl: float
    extraction_avg_confidence: float
    duplicate_groups: int
    exception_count: int
    exception_rate: float
    journal_entries: int
    auto_resolved: int
    needs_human_review: int
    deterministic_hash: str = ""
    # --- AI observability (additive; excluded from deterministic_hash) ---
    semantic_match_rate: float = 0.0
    ai_assisted_matches: int = 0
    llm_calls: int = 0
    ai_enabled: bool = False
    embeddings_available: bool = False
    llm_available: bool = False


class AIInsights(BaseModel):
    """ai_insights.json - the natural-language AI layer (narrative only).

    Free-text / model-dependent content lives here, NOT in decision.json, so
    the deterministic decision artifacts stay byte-stable across re-runs.
    """
    run_id: str
    ai_enabled: bool
    embeddings_available: bool
    llm_available: bool
    # ready | no API key | invalid OpenAI API key (401) | ...
    llm_state: str = ""
    generated_by: str  # "gpt-4o" | "deterministic-template"
    ai_reasoning: str  # the "AI Reasoning" narrative shown in the audit log
    key_risks: list[str] = Field(default_factory=list)
    recommended_focus: str = ""
    semantic_match_rate: float = 0.0
    ai_assisted_matches: int = 0
    embedding_model: Optional[str] = None
    llm_calls: int = 0
