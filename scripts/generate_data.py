
from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ROOT / "data" / "bundles"

GL_ACCOUNT_MAP = {
    "cash": "1010:Cash - Operating",
    "bank_fees": "6210:Bank Fees Expense",
    "interest_income": "4210:Interest Income",
    "fx_gain_loss": "7150:FX Gain/Loss",
    "suspense": "1999:Suspense Clearing",
}
FEE_SCHEDULE = [
    {"fee_type": "MONTHLY SERVICE CHARGE", "amount": "45.00"},
    {"fee_type": "MONTHLY MAINTENANCE FEE", "amount": "25.00"},
    {"fee_type": "WIRE TRANSFER FEE", "amount": "15.00"},
]
FX_RATES = [
    {"currency": "USD", "rate_to_base": "1.0"},
    {"currency": "EUR", "rate_to_base": "1.09"},
    {"currency": "GBP", "rate_to_base": "1.27"},
]

# row = (date, description, reference, counterparty, amount)
# gl  = (gl_id, date, description, reference, amount)


def S(name, bundle_id, desc, acct_id, acct_name, bank, currency,
      opening, closing, rows, gl_rows, prior, gl_opening=None, mt940=None):
    return dict(name=name, bundle_id=bundle_id, description=desc,
                account=dict(account_id=acct_id, account_name=acct_name,
                             bank_name=bank, currency=currency, period="2026-05",
                             opening_balance=opening, closing_balance=closing,
                             **({"gl_opening_balance": gl_opening}
                                if gl_opening is not None else {})),
                rows=rows, gl_rows=gl_rows, prior=prior, mt940=mt940)


def prior_recon(closing_bank, closing_gl=None, outstanding=None, txns=None):
    return {"period": "2026-04", "closing_balance_bank": closing_bank,
            "closing_balance_gl": closing_gl if closing_gl is not None else closing_bank,
            "outstanding_items": outstanding or [], "transactions": txns or []}


PRIOR_TXNS = [{"txn_id": "P-0001", "date": "2026-04-18", "amount": 2750.00,
               "reference": "INV-0418", "description": "CUSTOMER PAYMENT APRIL"}]

SCENARIOS = [
    S("scenario_01_clean", "SC01-CLEAN",
      "Clean account: every bank transaction matches GL - confirm and close.",
      "US-001-5521", "Operating Account - Main", "First National", "USD",
      52400.00, 55100.00,
      rows=[("2026-05-04", "CUSTOMER PAYMENT - ACME CORP", "INV-2201", "ACME CORP", 9500.00),
            ("2026-05-08", "VENDOR PAYMENT BLUE OFFICE SUPPLIES",
             "PO-7741", "BLUE OFFICE", -3200.00),
            ("2026-05-12", "PAYROLL ACH BATCH MAY-A",
             "PAY-0512", "ADP PAYROLL", -8750.00),
            ("2026-05-18", "WIRE IN - NORTHWIND TRADING",
             "WIRE-553", "NORTHWIND", 6300.00),
            ("2026-05-26", "RENT PAYMENT MAYFIELD PROPERTIES", "RENT-05", "MAYFIELD", -1150.00)],
      gl_rows=[("G-101", "2026-05-04", "AR receipt Acme Corp inv 2201", "INV-2201", 9500.00),
               ("G-102", "2026-05-08",
                "AP payment Blue Office Supplies", "PO-7741", -3200.00),
               ("G-103", "2026-05-12", "Payroll batch May A", "PAY-0512", -8750.00),
               ("G-104", "2026-05-18",
                "Wire receipt Northwind Trading", "WIRE-553", 6300.00),
               ("G-105", "2026-05-26", "Office rent May Mayfield", "RENT-05", -1150.00)],
      prior=prior_recon(52400.00, txns=PRIOR_TXNS)),

    S("scenario_02_bank_charge", "SC02-FEE",
      "Bank service charge not recorded in GL - auto journal entry suggestion.",
      "US-002-8810", "Operating Account - Branch", "First National", "USD",
      18250.00, 19605.00,
      rows=[("2026-05-06", "CUSTOMER PAYMENT RIVERSIDE LLC", "INV-1104", "RIVERSIDE LLC", 4200.00),
            ("2026-05-15", "VENDOR PAYMENT OFFICEMART", "", "OFFICEMART", -2800.00),
            ("2026-05-31", "MONTHLY SERVICE CHARGE", "FEE-0531", "FIRST NATIONAL", -45.00)],
      gl_rows=[("G-201", "2026-05-06", "AR receipt Riverside LLC", "INV-1104", 4200.00),
               ("G-202", "2026-05-16", "OFFICEMART VENDOR PAYMENT PO 8810", "", -2800.00)],
      prior=prior_recon(18250.00, txns=PRIOR_TXNS)),

    S("scenario_03_outstanding_check", "SC03-OUTCHK",
      "Outstanding check from prior month still uncashed - timing difference.",
      "US-003-2041", "Disbursement Account", "First National", "USD",
      27600.00, 30900.00, gl_opening=25750.00,
      rows=[("2026-05-07", "CUSTOMER PAYMENT ORBIT MEDIA", "INV-3302", "ORBIT MEDIA", 5400.00),
            ("2026-05-20", "VENDOR PAYMENT CLEARWATER SUPPLIES", "PO-9921", "CLEARWATER", -2100.00)],
      gl_rows=[("G-301", "2026-05-07", "AR receipt Orbit Media", "INV-3302", 5400.00),
               ("G-302", "2026-05-20", "AP payment Clearwater Supplies", "PO-9921", -2100.00)],
      prior=prior_recon(27600.00, closing_gl=25750.00, txns=PRIOR_TXNS,
                        outstanding=[{"item_id": "CHK-2041", "date": "2026-04-22",
                                      "amount": -1850.00, "category": "outstanding_check",
                                      "reference": "CHK2041",
                                      "description": "CHECK 2041 - GREENFIELD MAINTENANCE"}])),

    S("scenario_04_duplicate_ach", "SC04-DUPACH",
      "Duplicate ACH payment processed twice by bank - reversal required.",
      "US-004-7702", "Operating Account - Main", "First National", "USD",
      41000.00, 40000.00,
      rows=[("2026-05-05", "CUSTOMER ACH ORION RETAIL", "INV-5501", "ORION RETAIL", 7500.00),
            ("2026-05-14", "ACH VENDOR PAY - STELLAR PARTS",
             "ACH-7702", "STELLAR PARTS", -4250.00),
            ("2026-05-15", "ACH VENDOR PAY - STELLAR PARTS", "ACH-7702", "STELLAR PARTS", -4250.00)],
      gl_rows=[("G-401", "2026-05-05", "AR receipt Orion Retail ACH", "INV-5501", 7500.00),
               ("G-402", "2026-05-14", "AP payment Stellar Parts ACH", "ACH-7702", -4250.00)],
      prior=prior_recon(41000.00, txns=PRIOR_TXNS)),

    S("scenario_05_fx_revaluation", "SC05-FX",
      "FX revaluation difference on foreign currency (EUR) account - adjustment.",
      "EU-005-3309", "EUR Operating Account", "Continental Bank", "EUR",
      22000.00, 26181.60,
      rows=[("2026-05-09", "CLIENT PAYMENT - BERLIN GMBH", "INV-EU-204", "BERLIN GMBH", 9800.00),
            ("2026-05-21", "SUPPLIER PAYMENT - LYON SARL",
             "PO-EU-118", "LYON SARL", -5600.00),
            ("2026-05-30", "FX REVALUATION ADJUSTMENT", "FXR-0530", "CONTINENTAL BANK", -18.40)],
      gl_rows=[("G-501", "2026-05-09", "AR receipt Berlin GmbH", "INV-EU-204", 9800.00),
               ("G-502", "2026-05-21", "AP payment Lyon SARL", "PO-EU-118", -5600.00)],
      prior=prior_recon(22000.00, txns=PRIOR_TXNS)),

    S("scenario_06_high_value_unmatched", "SC06-HIGHVAL",
      "High-value unmatched wire above materiality - controller escalation. "
      "Also shows 1:M matching (one deposit split across two GL postings).",
      "US-006-8812", "Treasury Account", "First National", "USD",
      64000.00, 57550.00,
      rows=[("2026-05-08", "CUSTOMER PAYMENT VERTEX LABS", "INV-7710", "VERTEX LABS", 8200.00),
            ("2026-05-17", "WIRE OUT - UNKNOWN BENEFICIARY REF 88123",
             "WIRE-88123", "", -12750.00),
            ("2026-05-28", "VENDOR PAYMENT CEDAR SUPPLIES", "PO-3308", "CEDAR SUPPLIES", -1900.00)],
      gl_rows=[("G-601", "2026-05-08", "AR receipt Vertex Labs part 1", "INV-7710A", 5000.00),
               ("G-602", "2026-05-08",
                "AR receipt Vertex Labs part 2", "INV-7710B", 3200.00),
               ("G-603", "2026-05-28", "AP payment Cedar Supplies", "PO-3308", -1900.00)],
      prior=prior_recon(64000.00, txns=PRIOR_TXNS)),

    S("scenario_07_truncated_memo", "SC07-TRUNC",
      "Bank memo truncated - ambiguous transaction flagged for manual review.",
      "US-007-1190", "Operating Account - Retail", "First National", "USD",
      9800.00, 10980.00,
      rows=[("2026-05-05", "CUSTOMER PAYMENT HARBORLIGHT", "INV-4401", "HARBORLIGHT", 2300.00),
            ("2026-05-16", "POS 88231 MEM...", "", "", -640.00),
            ("2026-05-27", "VENDOR PAYMENT MILLBROOK", "PO-1190", "MILLBROOK", -480.00)],
      gl_rows=[("G-701", "2026-05-05", "AR receipt Harborlight", "INV-4401", 2300.00),
               ("G-702", "2026-05-27", "AP payment Millbrook", "PO-1190", -480.00)],
      prior=prior_recon(9800.00, txns=PRIOR_TXNS)),

    S("scenario_08_opening_mismatch", "SC08-OPENBAL",
      "Opening balance differs from prior period closing - investigation.",
      "US-008-6612", "Operating Account - South", "First National", "USD",
      33150.00, 33600.00, gl_opening=33500.00,
      rows=[("2026-05-10", "CUSTOMER PAYMENT SUNRISE CAFE", "INV-2210", "SUNRISE CAFE", 1200.00),
            ("2026-05-22", "VENDOR PAYMENT PEAK TOOLS", "PO-6612", "PEAK TOOLS", -750.00)],
      gl_rows=[("G-801", "2026-05-10", "AR receipt Sunrise Cafe", "INV-2210", 1200.00),
               ("G-802", "2026-05-22", "AP payment Peak Tools", "PO-6612", -750.00)],
      prior=prior_recon(33500.00, txns=PRIOR_TXNS)),

    S("scenario_09_minimal_mt940", "SC09-MINIMAL",
      "Clean low-activity account (MT940 format) with two transactions.",
      "US-009-7782", "Petty Operations Account", "First National", "USD",
      6500.00, 7580.00,
      rows=None,
      mt940="\n".join([
          ":20:STMT-2026-05-009",
          ":25:FN-009-7782",
          ":28C:00005/001",
          ":60F:C260501USD6500,00",
          ":61:2605120512C1500,00NTRFINV-9901",
          ":86:CUSTOMER PAYMENT - SMALL WORKS LLC INV-9901",
          ":61:2605250525D420,00NCHKCHK1108",
          ":86:CHECK 1108 - UTILITIES MAYFIELD POWER",
          ":62F:C260531USD7580,00", ""]),
      gl_rows=[("G-901", "2026-05-12", "CUSTOMER PAYMENT SMALL WORKS LLC", "INV-9901", 1500.00),
               ("G-902", "2026-05-25", "CHECK 1108 UTILITIES MAYFIELD POWER", "CHK1108", -420.00)],
      prior=prior_recon(6500.00, txns=PRIOR_TXNS)),
]


def write_bundle(sc: dict) -> Path:
    d = BUNDLES / sc["name"]
    d.mkdir(parents=True, exist_ok=True)

    if sc["mt940"] is not None:
        stmt_name = "bank_statement.mt940"
        (d / stmt_name).write_text(sc["mt940"], encoding="utf-8")
    else:
        stmt_name = "bank_statement.csv"
        with open(d / stmt_name, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["date", "description", "reference", "counterparty",
                        "amount", "currency"])
            for (dt, desc, ref, cp, amt) in sc["rows"]:
                w.writerow([dt, desc, ref, cp, f"{amt:.2f}",
                            sc["account"]["currency"]])

    with open(d / "gl_export.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["gl_id", "date", "account_code", "description",
                    "reference", "amount"])
        for (gid, dt, desc, ref, amt) in sc["gl_rows"]:
            w.writerow([gid, dt, "1010", desc, ref, f"{amt:.2f}"])

    (d / "prior_recon.json").write_text(
        json.dumps(sc["prior"], indent=2, sort_keys=True), encoding="utf-8")

    for fname, rows, cols in [
            ("bank_fee_schedule.csv", FEE_SCHEDULE, ["fee_type", "amount"]),
            ("fx_rates.csv", FX_RATES, ["currency", "rate_to_base"])]:
        with open(d / fname, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)

    manifest = {
        "bundle_id": sc["bundle_id"],
        "description": sc["description"],
        "account": sc["account"],
        "files": {"bank_statement": stmt_name,
                  "gl_export": "gl_export.csv",
                  "prior_recon": "prior_recon.json",
                  "bank_fee_schedule": "bank_fee_schedule.csv",
                  "fx_rates": "fx_rates.csv"},
        "gl_account_map": GL_ACCOUNT_MAP,
    }
    (d / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    return d


if __name__ == "__main__":
    for sc in SCENARIOS:
        path = write_bundle(sc)
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"\n{len(SCENARIOS)} Recon Bundles generated under data/bundles/")
