import json
from pathlib import Path

from ibsrs.agents.agent_b_extraction import (
    _aggregate_pdf_rows,
    _dedupe_pdf_rows,
    _sort_pdf_rows,
    _validate_page_balance_chain,
)
from ibsrs.policy import load_policy
from ibsrs.schemas import AccountMeta, ContextPacket
from ibsrs.utils.io import AuditLog

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ctx(opening: float = 10000.0, closing: float = 10500.0) -> ContextPacket:
    return ContextPacket(
        run_id="test",
        bundle_path=".",
        account=AccountMeta(
            account_id="TEST-001",
            account_name="Test",
            currency="USD",
            bank_name="Test Bank",
            period="2026-05",
            statement_format="pdf",
            opening_balance=opening,
            closing_balance=closing,
        ),
        files={"bank_statement": "bank_statement.pdf"},
    )


def test_sort_pdf_rows_orders_by_page_then_position():
    rows = [
        {"page": 2, "amount": 1, "bbox": [0, 700, 0, 0]},
        {"page": 1, "amount": 2, "bbox": [0, 680, 0, 0]},
        {"page": 1, "amount": 3, "bbox": [0, 700, 0, 0]},
    ]
    sorted_rows = _sort_pdf_rows(rows)
    assert [r["amount"] for r in sorted_rows] == [3, 2, 1]


def test_dedupe_pdf_rows_removes_page_boundary_duplicate():
    rows = [
        {"page": 1, "date": "2026-05-02", "amount": -200.0,
         "description": "VENDOR PAYMENT", "reference": "PAY-002"},
        {"page": 2, "date": "2026-05-02", "amount": -200.0,
         "description": "VENDOR PAYMENT", "reference": "PAY-002"},
        {"page": 2, "date": "2026-05-10", "amount": 150.0,
         "description": "WIRE IN CLIENT", "reference": "WIRE-010"},
    ]
    deduped, removed = _dedupe_pdf_rows(rows)
    assert removed == 1
    assert len(deduped) == 2


def test_validate_page_balance_chain_detects_break():
    summaries = [
        {"page": 1, "opening_balance": 10000.0, "closing_balance": 10300.0},
        {"page": 2, "opening_balance": 10350.0, "closing_balance": 10500.0},
    ]
    errors = _validate_page_balance_chain(summaries, 10000.0, 10500.0, 0.005)
    assert any("does not carry" in e for e in errors)


def test_aggregate_pdf_rows_ok_fixture_has_no_warnings(tmp_path):
    sidecar = json.loads((FIXTURES / "multipage_ok.extracted.json").read_text())
    audit = AuditLog(tmp_path, "test")
    rows, warnings = _aggregate_pdf_rows(
        sidecar["rows"], sidecar, _ctx(), load_policy(), audit)
    assert len(rows) == 5
    assert warnings == []


def test_aggregate_pdf_rows_break_fixture_has_warnings(tmp_path):
    sidecar = json.loads((FIXTURES / "multipage_break.extracted.json").read_text())
    audit = AuditLog(tmp_path, "test")
    _, warnings = _aggregate_pdf_rows(
        sidecar["rows"], sidecar, _ctx(), load_policy(), audit)
    assert warnings
