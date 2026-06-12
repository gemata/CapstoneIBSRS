from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field



class Evidence(BaseModel):
    """Pointer that lets an auditor trace a conclusion back to its source."""
    source_file: str
    locator: str  
    snippet: str = ""


class Finding(BaseModel):
    """One unit of agent output, consolidated by the orchestrator (Agent H)."""
    finding_id: str
    agent: str  
    category: str  
    severity: str = "info"  
    confidence: float = 1.0
    title: str
    detail: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    recommendation: str = ""
    open_question: str = ""  
    related_txn_ids: list[str] = Field(default_factory=list)


# Agent A - context packet

class AccountMeta(BaseModel):
    account_id: str
    account_name: str
    currency: str
    bank_name: str
    period: str  
    statement_format: str  
    opening_balance: float
    closing_balance: float
    gl_opening_balance: Optional[float] = None  


class RiskFlag(BaseModel):
    code: str  
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
    files: dict[str, str] = Field(default_factory=dict)  


# Agent B - normalized transactions

class BankTransaction(BaseModel):
    """Normalized bank-statement transaction -> transactions.json"""
    txn_id: str
    date: str  
    amount: float 
    currency: str
    description: str
    reference: str = ""
    counterparty: str = ""
    txn_type: str = "" 
    confidence: float = 1.0
    needs_review: bool = False  
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


# Agents C & D - match result + timing differences

class GLEntry(BaseModel):
    gl_id: str
    date: str
    amount: float
    account_code: str
    description: str
    reference: str = ""


class MatchPair(BaseModel):
    match_id: str
    match_type: str  
    bank_txn_ids: list[str]
    gl_ids: list[str]
    score: float
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)


class UnmatchedItem(BaseModel):
    side: str 
    item_id: str
    date: str
    amount: float
    description: str
    timing_category: str = "uncategorized"
  


class MatchResult(BaseModel):
    """Output of Agents C&D -> match_result.json"""
    run_id: str
    matched: list[MatchPair]
    unmatched_bank: list[UnmatchedItem]
    unmatched_gl: list[UnmatchedItem]
    match_rate_bank: float
    match_rate_gl: float


# Agent E - duplicates

class DuplicateGroup(BaseModel):
    dup_id: str
    kind: str 
    txn_ids: list[str]
    detail: str
    suggested_action: str
    confidence: float


class DuplicateReport(BaseModel):
    run_id: str
    groups: list[DuplicateGroup]


# Agent H - journal entries, decision, metrics

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
    status: str = "suggested"  
    erp_payload: dict = Field(default_factory=dict)


class ExceptionItem(BaseModel):
    exception_id: str
    category: str
    severity: str
    title: str
    detail: str
    next_action: str
    route_to: str  
    related_txn_ids: list[str] = Field(default_factory=list)
    source_finding_ids: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """Final orchestrator decision -> decision.json"""
    run_id: str
    status: str 
    summary: str
    exceptions_count: int
    journals_count: int
    requires_controller: bool


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
