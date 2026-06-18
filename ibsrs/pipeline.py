
from __future__ import annotations

import hashlib
from pathlib import Path

from ibsrs.agents.agent_a_intake import run_agent_a
from ibsrs.agents.agent_b_extraction import run_agent_b
from ibsrs.agents.agent_cd_matching import load_gl_entries, run_agents_cd
from ibsrs.agents.agent_e_duplicates import run_agent_e
from ibsrs.agents.agent_h_triage import run_agent_h
from ibsrs.ai import AIRuntime
from ibsrs.policy import Policy, load_policy
from ibsrs.schemas import Finding
from ibsrs.utils.io import AuditLog, utc_now_iso

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
BUNDLES_DIR = PROJECT_ROOT / "data" / "bundles"

ARTIFACTS = ["context.json", "transactions.json", "match_result.json",
             "timing_diffs.csv", "duplicates.json", "findings.json",
             "exceptions.md", "journal_entries.json", "recon_statement.md",
             "decision.json", "audit_log.md", "metrics.json",
             "ai_insights.json", "llm_calls.log"]


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


def run_pipeline(bundle_dir: str | Path, policy_path: str | Path | None = None,
                 runs_dir: str | Path | None = None,
                 use_ai: bool | None = None) -> dict:
    """Run the full 6-agent pipeline. Returns a summary dict for API/UI.

    ``use_ai``: None = auto (policy.ai.enabled + library/key availability);
    True/False force the AI layer on/off. The financial decision and journal
    math are deterministic regardless - AI only augments matching/extraction
    and writes a natural-language reasoning narrative.
    """
    bundle_dir = Path(bundle_dir)
    policy: Policy = load_policy(policy_path)
    runs_root = Path(runs_dir) if runs_dir else RUNS_DIR

    run_id = compute_run_id(bundle_dir, policy_path)
    run_dir = runs_root / run_id
    if run_dir.exists():  # idempotent re-run: rebuild from scratch
        for f in run_dir.glob("*"):
            f.unlink()
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = utc_now_iso()
    audit = AuditLog(run_dir, run_id)
    # Initialize the AI runtime ONCE (loads the embedding model a single time,
    # creates the OpenAI client lazily, opens llm_calls.log for this run).
    ai = AIRuntime(policy, run_dir=run_dir, use_ai=use_ai)
    audit.section("AI Runtime", "Hybrid capability detection")
    audit.step(f"AI status: {ai.status}")
    all_findings: list[Finding] = []

    # Agent A - intake, context packet, evidence index, risk gating
    ctx, f_a = run_agent_a(bundle_dir, run_dir, run_id, policy, audit)
    all_findings += f_a

    # Agent B - extraction + normalization + balance roll-forward (hybrid LLM PDF)
    txn_artifact, f_b = run_agent_b(ctx, run_dir, policy, audit, ai=ai)
    all_findings += f_b

    # Agents C & D - GL matching (semantic + rule) + variance/timing analysis
    match_result, f_cd = run_agents_cd(
        ctx, txn_artifact, run_dir, policy, audit, ai=ai)
    all_findings += f_cd

    # Agent E - duplicate detection (100% rule-based, unchanged)
    gl_entries = load_gl_entries(Path(ctx.files["gl_export"]))
    dup_report, f_e = run_agent_e(ctx, txn_artifact, gl_entries, run_dir,
                                  policy, audit)
    all_findings += f_e

    # Agent H - triage, journals, statement, decision (rules) + AI reasoning narrative
    decision, metrics = run_agent_h(ctx, txn_artifact, match_result, dup_report,
                                    gl_entries, all_findings, run_dir, policy,
                                    audit, started_at, ai=ai)

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "bundle": bundle_dir.name,
        "status": decision.status,
        "summary": decision.summary,
        "exceptions": decision.exceptions_count,
        "journal_entries": decision.journals_count,
        "match_rate_bank": metrics.match_rate_bank,
        "semantic_match_rate": metrics.semantic_match_rate,
        "ai_assisted_matches": metrics.ai_assisted_matches,
        "ai_status": ai.status,
        "deterministic_hash": metrics.deterministic_hash,
        "artifacts": {a: str(run_dir / a) for a in ARTIFACTS
                      if (run_dir / a).exists()},
    }


def list_bundles() -> list[dict]:
    """Discover available Recon Bundles for the API/UI."""
    out = []
    if BUNDLES_DIR.exists():
        for d in sorted(BUNDLES_DIR.iterdir()):
            if (d / "manifest.yaml").exists():
                import yaml
                m = yaml.safe_load(
                    (d / "manifest.yaml").read_text(encoding="utf-8"))
                out.append({"name": d.name, "path": str(d),
                            "bundle_id": m.get("bundle_id", d.name),
                            "description": m.get("description", ""),
                            "account": m.get("account", {}).get("account_id", "?"),
                            "period": m.get("account", {}).get("period", "?")})
    return out


def list_runs(runs_dir: str | Path | None = None) -> list[dict]:
    root = Path(runs_dir) if runs_dir else RUNS_DIR
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            dec = d / "decision.json"
            if dec.exists():
                from ibsrs.utils.io import read_json
                data = read_json(dec)
                out.append({"run_id": d.name, "status": data.get("status"),
                            "summary": data.get("summary"), "run_dir": str(d)})
    return out


# one-command demo: python -m ibsrs.pipeline [bundle]
if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    use_ai = True if "--ai" in flags else (
        False if "--no-ai" in flags else None)

    def _resolve(arg: str) -> Path:
        p = Path(arg)
        return p if (p / "manifest.yaml").exists() else (BUNDLES_DIR / arg)

    targets = ([_resolve(args[0])] if args
               else [Path(b["path"]) for b in list_bundles()])
    for t in targets:
        res = run_pipeline(t, use_ai=use_ai)
        st = res["ai_status"]
        print(f"{res['bundle']:<38} {res['status']:<24} "
              f"exc={res['exceptions']:<2} je={res['journal_entries']:<2} "
              f"sem={res['semantic_match_rate']:.0%} ai={res['ai_assisted_matches']:<2} "
              f"hash={res['deterministic_hash']} "
              f"[emb={st['embeddings_available']} llm={st['llm_available']}]")
