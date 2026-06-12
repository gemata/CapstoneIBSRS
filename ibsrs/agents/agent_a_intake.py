
from __future__ import annotations

import csv
from pathlib import Path

import yaml

from ibsrs.policy import Policy
from ibsrs.schemas import AccountMeta, ContextPacket, Evidence, Finding, RiskFlag
from ibsrs.utils.io import AuditLog, write_json


def _sniff_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    head = path.read_text(encoding="utf-8", errors="replace")[:400]
    if ":20:" in head or ":60F:" in head:
        return "mt940"
    if suffix in (".csv", ".txt"):
        return "csv"
    return "unknown"


def _read_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        clean = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(clean))


def run_agent_a(bundle_dir: Path, run_dir: Path, run_id: str,
                policy: Policy, audit: AuditLog) -> tuple[ContextPacket, list[Finding]]:
    bundle_dir = Path(bundle_dir)
    audit.section("Agent A", "Statement Intake & Context")
    findings: list[Finding] = []

    manifest_path = bundle_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Recon Bundle has no manifest.yaml: {bundle_dir}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    audit.step(
        f"Loaded manifest for bundle `{manifest.get('bundle_id', bundle_dir.name)}`")

    files = {k: str(bundle_dir / v)
             for k, v in manifest.get("files", {}).items()}
    stmt_path = Path(files["bank_statement"])
    if not stmt_path.exists():
        raise FileNotFoundError(f"Bank statement missing: {stmt_path}")

    fmt = _sniff_format(stmt_path)
    if fmt == "unknown":
        raise ValueError(
            f"Unsupported/corrupt statement format: {stmt_path.name}")
    audit.decision(f"Classified statement `{stmt_path.name}` as **{fmt.upper()}**",
                   "Extension and content sniffing (MT940 tags / CSV header / PDF magic)")

    acct_cfg = manifest["account"]
    account = AccountMeta(
        account_id=acct_cfg["account_id"],
        account_name=acct_cfg["account_name"],
        currency=acct_cfg["currency"],
        bank_name=acct_cfg["bank_name"],
        period=acct_cfg["period"],
        statement_format=fmt,
        opening_balance=float(acct_cfg["opening_balance"]),
        closing_balance=float(acct_cfg["closing_balance"]),
        gl_opening_balance=float(acct_cfg["gl_opening_balance"])
        if "gl_opening_balance" in acct_cfg else None,
    )

    # --- prior period reconciliation
    prior_closing = None
    if "prior_recon" in files and Path(files["prior_recon"]).exists():
        import json
        prior = json.loads(
            Path(files["prior_recon"]).read_text(encoding="utf-8"))
        prior_closing = float(prior.get("closing_balance_bank", 0.0))
        audit.step(f"Loaded prior period reconciliation ({prior.get('period')}), "
                   f"closing balance {prior_closing:,.2f}")

    # bank fee schedule & FX rates
    fee_schedule: list[dict] = []
    if "bank_fee_schedule" in files and Path(files["bank_fee_schedule"]).exists():
        fee_schedule = _read_csv_rows(Path(files["bank_fee_schedule"]))
        audit.step(f"Loaded bank fee schedule ({len(fee_schedule)} fee types)")

    fx_rates: dict[str, float] = {}
    if "fx_rates" in files and Path(files["fx_rates"]).exists():
        for row in _read_csv_rows(Path(files["fx_rates"])):
            fx_rates[row["currency"]] = float(row["rate_to_base"])
        audit.step(f"Loaded FX rates for {sorted(fx_rates)}")

    #  universal evidence index
    evidence_index: list[Evidence] = []
    if fmt in ("csv", "mt940"):
        for i, line in enumerate(stmt_path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() and not line.startswith("#"):
                evidence_index.append(Evidence(
                    source_file=stmt_path.name, locator=f"line:{i}", snippet=line[:120]))
    else:  # pdf - page-level pointers;
        evidence_index.append(Evidence(source_file=stmt_path.name,
                                       locator="page:1", snippet="(PDF statement)"))
    audit.step(
        f"Built evidence index with {len(evidence_index)} source pointers")

    # risk heuristics
    risk_flags: list[RiskFlag] = []
    high_value = float(policy.get("thresholds.high_value", 10000.0))

    if account.currency != "USD":
        rate = fx_rates.get(account.currency)
        risk_flags.append(RiskFlag(
            code="FX_EXPOSURE",
            detail=f"Foreign-currency account ({account.currency}, "
            f"rate to base {rate}); revaluation exposure at period end",
            severity="medium"))

    if fmt == "csv":
        rows = _read_csv_rows(stmt_path)
        bad = [r for r in rows if not r.get("date") or not r.get("amount")]
        if bad:
            risk_flags.append(RiskFlag(code="FORMAT_INCONSISTENCY",
                                       detail=f"{len(bad)} statement rows missing date/amount",
                                       severity="high"))
        for r in rows:
            try:
                if abs(float(r["amount"])) >= high_value:
                    # Informational gatekeeper flag for unusually large transactions
                    risk_flags.append(RiskFlag(
                        code="HIGH_VALUE",
                        detail=f"{r['date']} {r['description'][:40]} amount {float(r['amount']):,.2f}",
                        severity="medium"))
            except (ValueError, KeyError):
                pass

    if prior_closing is not None and abs(account.opening_balance - prior_closing) > 0.005:
        risk_flags.append(RiskFlag(
            code="OPENING_BALANCE_MISMATCH",
            detail=(f"Opening balance {account.opening_balance:,.2f} differs from prior "
                    f"period closing {prior_closing:,.2f} "
                    f"(delta {account.opening_balance - prior_closing:,.2f})"),
            severity="critical"))

    for rf in risk_flags:
        audit.decision(f"Risk flag **{rf.code}**: {rf.detail}",
                       "Gatekeeper heuristics from policy pack")
        findings.append(Finding(
            finding_id=f"A-{rf.code}-{len(findings)+1:03d}",
            agent="A", category="risk_flag", severity=rf.severity,
            title=rf.code, detail=rf.detail,
            evidence=[Evidence(source_file="manifest.yaml", locator="account",
                               snippet=account.account_id)],
            recommendation="Investigate before close" if rf.severity in ("high", "critical")
                           else "Monitor downstream",
        ))

    context = ContextPacket(
        run_id=run_id, bundle_path=str(bundle_dir), account=account,
        prior_period_closing_balance=prior_closing,
        gl_account_map=manifest.get("gl_account_map", {}),
        bank_fee_schedule=fee_schedule, fx_rates=fx_rates,
        evidence_index=evidence_index, risk_flags=risk_flags, files=files,
    )
    out = run_dir / "context.json"
    write_json(out, context)
    audit.artifact("context.json", out)
    return context, findings
