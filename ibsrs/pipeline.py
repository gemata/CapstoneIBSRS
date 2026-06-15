
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ibsrs.agents.agent_a_intake import run_agent_a
from ibsrs.policy import Policy, load_policy
from ibsrs.utils.io import AuditLog, write_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
BUNDLES_DIR = PROJECT_ROOT / "data" / "bundles"

ARTIFACTS = ["context.json", "findings.json", "audit_log.md"]


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


def run_pipeline(
    bundle_dir: str | Path,
    policy_path: str | Path | None = None,
    runs_dir: str | Path | None = None,
) -> dict:
    """Run Agent A intake only. Returns a summary dict."""
    bundle_dir = Path(bundle_dir)
    policy: Policy = load_policy(policy_path)
    policy_file = Path(policy_path) if policy_path else None
    runs_root = Path(runs_dir) if runs_dir else RUNS_DIR

    run_id = compute_run_id(bundle_dir, policy_file)
    run_dir = runs_root / run_id
    if run_dir.exists():
        for f in run_dir.glob("*"):
            f.unlink()
    run_dir.mkdir(parents=True, exist_ok=True)

    audit = AuditLog(run_dir, run_id)
    ctx, findings = run_agent_a(bundle_dir, run_dir, run_id, policy, audit)
    write_json(run_dir / "findings.json", findings)
    audit.close()

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "bundle": bundle_dir.name,
        "statement_format": ctx.account.statement_format,
        "risk_flags": len(ctx.risk_flags),
        "artifacts": {
            a: str(run_dir / a) for a in ARTIFACTS if (run_dir / a).exists()
        },
    }


def list_bundles() -> list[dict]:
    """Discover available Recon Bundles."""
    out = []
    if BUNDLES_DIR.exists():
        for d in sorted(BUNDLES_DIR.iterdir()):
            if (d / "manifest.yaml").exists():
                m = yaml.safe_load(
                    (d / "manifest.yaml").read_text(encoding="utf-8"))
                out.append({
                    "name": d.name,
                    "path": str(d),
                    "bundle_id": m.get("bundle_id", d.name),
                    "description": m.get("description", ""),
                    "account": m.get("account", {}).get("account_id", "?"),
                    "period": m.get("account", {}).get("period", "?"),
                })
    return out


def list_runs(runs_dir: str | Path | None = None) -> list[dict]:
    """List completed Agent A runs (those with context.json)."""
    root = Path(runs_dir) if runs_dir else RUNS_DIR
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            ctx_path = d / "context.json"
            if ctx_path.exists():
                from ibsrs.utils.io import read_json

                data = read_json(ctx_path)
                account = data.get("account", {})
                out.append({
                    "run_id": d.name,
                    "account_id": account.get("account_id"),
                    "period": account.get("period"),
                    "statement_format": account.get("statement_format"),
                    "run_dir": str(d),
                })
    return out


if __name__ == "__main__":
    import sys

    targets = (
        [Path(sys.argv[1])]
        if len(sys.argv) > 1
        else [Path(b["path"]) for b in list_bundles()]
    )
    for t in targets:
        res = run_pipeline(t)
        print(
            f"{res['bundle']:<38} {res['statement_format']:<6} "
            f"risks={res['risk_flags']:<2} run={res['run_id']}"
        )
