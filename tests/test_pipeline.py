from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ibsrs.pipeline import ARTIFACTS, BUNDLES_DIR, run_pipeline  # noqa: E402

EXPECTED = {
    "scenario_01_clean": ("CLOSED_CLEAN", 0, 0),
    "scenario_02_bank_charge": ("CLOSED_WITH_ADJUSTMENTS", 1, 1),
    "scenario_03_outstanding_check": ("CLOSED_WITH_ADJUSTMENTS", 1, 0),
    "scenario_04_duplicate_ach": ("ESCALATED", 1, 1),
    "scenario_05_fx_revaluation": ("CLOSED_WITH_ADJUSTMENTS", 1, 1),
    "scenario_06_high_value_unmatched": ("ESCALATED", 1, 0),
    "scenario_07_truncated_memo": ("CLOSED_WITH_ADJUSTMENTS", 2, 0),
    "scenario_08_opening_mismatch": ("OPEN_EXCEPTIONS", 1, 0),
    "scenario_09_minimal_mt940": ("CLOSED_CLEAN", 0, 0),
}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    runs_dir = tmp_path_factory.mktemp("runs")
    out = {}
    for name in EXPECTED:
        # use_ai=False locks the deterministic baseline so the asserted outcomes
        # are hermetic whether or not the AI stack is installed in the test env.
        out[name] = run_pipeline(BUNDLES_DIR / name, runs_dir=runs_dir, use_ai=False)
    return out


@pytest.mark.parametrize("name", list(EXPECTED))
def test_scenario_outcomes(results, name):
    status, exc, je = EXPECTED[name]
    r = results[name]
    assert r["status"] == status, f"{name}: {r['status']} != {status}"
    assert r["exceptions"] == exc
    assert r["journal_entries"] == je


@pytest.mark.parametrize("name", list(EXPECTED))
def test_all_artifacts_written(results, name):
    run_dir = Path(results[name]["run_dir"])
    for artifact in ARTIFACTS:
        assert (run_dir / artifact).exists(), f"{name} missing {artifact}"


def test_deterministic_rerun(tmp_path):
    """Identical inputs -> identical run id, decision hash, and artifacts."""
    bundle = BUNDLES_DIR / "scenario_04_duplicate_ach"
    r1 = run_pipeline(bundle, runs_dir=tmp_path / "a", use_ai=False)
    r2 = run_pipeline(bundle, runs_dir=tmp_path / "b", use_ai=False)
    assert r1["run_id"] == r2["run_id"]
    assert r1["deterministic_hash"] == r2["deterministic_hash"]
    for name in ("match_result.json", "journal_entries.json", "decision.json",
                 "exceptions.md", "recon_statement.md", "timing_diffs.csv"):
        a = (Path(r1["run_dir"]) / name).read_text(encoding="utf-8")
        b = (Path(r2["run_dir"]) / name).read_text(encoding="utf-8")
        assert a == b, f"{name} differs between identical re-runs"


def test_journals_are_balanced(results):
    for name, r in results.items():
        je = json.loads((Path(r["run_dir"]) / "journal_entries.json")
                        .read_text(encoding="utf-8"))
        for e in je["entries"]:
            dr = round(sum(l["debit"] for l in e["lines"]), 2)
            cr = round(sum(l["credit"] for l in e["lines"]), 2)
            assert dr == cr and dr > 0, f"{name} {e['je_id']} unbalanced"


def test_findings_carry_evidence(results):
    """Compliance: every finding must point back to source evidence."""
    for name, r in results.items():
        data = json.loads((Path(r["run_dir"]) / "findings.json")
                          .read_text(encoding="utf-8"))
        for f in data["findings"]:
            assert f["evidence"], f"{name} finding {f['finding_id']} lacks evidence"


def test_match_evidence_traceable(results):
    r = results["scenario_01_clean"]
    mr = json.loads((Path(r["run_dir"]) / "match_result.json")
                    .read_text(encoding="utf-8"))
    assert len(mr["matched"]) == 5
    assert all(m["match_type"] == "reference_1to1" for m in mr["matched"])
    assert mr["match_rate_bank"] == 1.0


def test_one_to_many_match(results):
    r = results["scenario_06_high_value_unmatched"]
    mr = json.loads((Path(r["run_dir"]) / "match_result.json")
                    .read_text(encoding="utf-8"))
    assert any(m["match_type"] == "one_to_many" and len(m["gl_ids"]) == 2
               for m in mr["matched"])


def test_policy_changes_decisions(tmp_path):
    """Demo expectation: lowering materiality flips a clean-ish run to
    escalation - policy edits visibly change decisions without code edits."""
    base = yaml.safe_load((ROOT / "policy" / "policy.yaml").read_text(encoding="utf-8"))
    strict = json.loads(json.dumps(base))
    strict["thresholds"]["materiality"] = 100.0  # tiny materiality
    strict_path = tmp_path / "strict_policy.yaml"
    strict_path.write_text(yaml.safe_dump(strict), encoding="utf-8")

    bundle = BUNDLES_DIR / "scenario_07_truncated_memo"
    normal = run_pipeline(bundle, runs_dir=tmp_path / "n", use_ai=False)
    tight = run_pipeline(bundle, policy_path=strict_path, runs_dir=tmp_path / "t",
                         use_ai=False)
    assert normal["status"] == "CLOSED_WITH_ADJUSTMENTS"
    assert tight["status"] == "ESCALATED"  # 640.00 item now above materiality


# ---------------------------------------------------------------------------
# Hybrid AI layer (optional) - skipped entirely when the AI stack is absent
# ---------------------------------------------------------------------------
_HAS_ST = False
try:
    import sentence_transformers  # noqa: F401
    _HAS_ST = True
except Exception:
    pass


def test_ai_fields_present_and_backward_compatible(results):
    """Even with AI off, the new additive fields exist and are inert."""
    r = results["scenario_01_clean"]
    mr = json.loads((Path(r["run_dir"]) / "match_result.json").read_text("utf-8"))
    assert "semantic_match_rate" in mr and mr["semantic_match_rate"] == 0.0
    assert all("semantic_score" in m for m in mr["matched"])
    ai_ins = json.loads((Path(r["run_dir"]) / "ai_insights.json").read_text("utf-8"))
    assert ai_ins["generated_by"] == "deterministic-template"  # no key in tests
    assert ai_ins["ai_reasoning"]  # narrative always populated
    assert (Path(r["run_dir"]) / "llm_calls.log").exists()


def test_upload_creates_distinct_folders(tmp_path, monkeypatch):
    """Each distinct upload gets its own data/uploads folder (content-addressed);
    an identical re-upload is idempotent. Regression guard for the bundle-name
    collision (all sample files are named bank_statement.*) and the manifest
    NameError."""
    from fastapi.testclient import TestClient
    from app import api
    monkeypatch.setattr(api, "UPLOADS_DIR", tmp_path / "uploads")
    client = TestClient(api.app)
    gl = (b"gl_id,date,account_code,description,reference,amount\n"
          b"G1,2026-05-01,1010,pay,REF1,100.00\n")
    stmt_a = (b"date,description,reference,amount,running_balance\n"
              b"2026-05-01,PAYMENT,REF1,100.00,100.00\n")
    stmt_b = stmt_a.replace(b"100.00", b"250.00").replace(b"REF1", b"REF2")

    def up(stmt, acct):
        return client.post("/upload-statement",
                           files={"file": ("bank_statement.csv", stmt),
                                  "gl_file": ("gl_export.csv", gl)},
                           data={"account_id": acct, "period": "2026-05"})

    assert up(stmt_a, "ACC-A").status_code == 200
    assert up(stmt_b, "ACC-B").status_code == 200
    assert up(stmt_a, "ACC-A").status_code == 200  # identical re-upload
    folders = sorted((tmp_path / "uploads").glob("upload_*"))
    assert len(folders) == 2, f"expected 2 distinct upload folders, got {folders}"
    # each is a complete bundle (parity with curated bundles)
    for f in folders:
        for name in ("bank_statement.csv", "gl_export.csv", "manifest.yaml",
                     "prior_recon.json", "bank_fee_schedule.csv", "fx_rates.csv"):
            assert (f / name).exists(), f"{f.name} missing {name}"


@pytest.mark.skipif(not _HAS_ST, reason="sentence-transformers not installed")
def test_semantic_matching_preserves_outcomes(tmp_path, monkeypatch):
    """AI ON must not change the deterministic outcomes of the 9 scenarios:
    semantics only choose among amount+date-eligible candidates. The LLM is
    disabled here (no API key) so the test exercises local embeddings only -
    free and offline, never billing a real OpenAI call."""
    monkeypatch.setattr("ibsrs.ai.runtime._read_api_key", lambda: "")
    for name, (status, exc, je) in EXPECTED.items():
        r = run_pipeline(BUNDLES_DIR / name, runs_dir=tmp_path / name, use_ai=True)
        assert r["status"] == status, f"{name} flipped to {r['status']} with AI on"
        assert r["exceptions"] == exc and r["journal_entries"] == je
        assert r["ai_status"]["embeddings_available"] is True
        assert r["ai_status"]["llm_available"] is False  # no key in test
