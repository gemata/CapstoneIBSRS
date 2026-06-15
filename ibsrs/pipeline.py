from __future__ import annotations

import hashlib
from pathlib import Path

from ibsrs.agents.agent_a_intake import run_agent_a
from ibsrs.schemas import Finding
from ibsrs.utils.io import AuditLog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
BUNDLES_DIR = PROJECT_ROOT / "data" / "bundles"

ARTIFACTS = ["context.json", "transactions.json", "match_result.json",
             "timing_diffs.csv", "duplicates.json", "findings.json",
             "exceptions.md", "journal_entries.json", "recon_statement.md",
             "decision.json", "audit_log.md", "metrics.json"]


def compute_run_id(bundle_dir: Path, policy_path: Path | None = None) -> str:
    """Deterministic run id: bundle name + sha256 of all inputs + policy."""
    h = hashlib.sha256()
    for f in sorted(Path(bundle_dir).glob("**/*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    if policy_path and Path(policy_path).exists():
        h.update(Path(policy_path).read_bytes())
    return f"{Path(bundle_dir).name}-{h.hexdigest()[:8]}"

"""
return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "bundle": bundle_dir.name,
        "status": decision.status,
        "summary": decision.summary,
        "exceptions": decision.exceptions_count,
        "journal_entries": decision.journals_count,
        "match_rate_bank": metrics.match_rate_bank,
        "deterministic_hash": metrics.deterministic_hash,
        "artifacts": {a: str(run_dir / a) for a in ARTIFACTS
                      if (run_dir / a).exists()},
    }
"""
def prepare_run(bundle_dir: Path, policy_path: Path | None = None) -> tuple[str, Path, AuditLog]:
    """Create the deterministic run folder and start the audit log."""
    run_id = compute_run_id(bundle_dir, policy_path)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    audit = AuditLog(run_dir, run_id)
    audit.section("Pipeline", "Run setup")
    audit.step(f"Created run directory: {run_dir}")
    audit.decision(
        "Use file-based coordination for this run.",
        "Agents exchange artifacts through files under runs/<run_id>/.",
    )

    return run_id, run_dir, audit