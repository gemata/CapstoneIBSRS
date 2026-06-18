
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "with_gl"

# bank tx = (date, description, reference, amount)   [counterparty derived = ""]
# gl tx   = (gl_id, date, description, reference, amount)


# ----------------------------------------------------------------- writers
def _write_gl(folder: Path, gl: list[tuple]) -> None:
    with open(folder / "gl_export.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["gl_id", "date", "account_code",
                   "description", "reference", "amount"])
        for (gid, dt, desc, ref, amt) in gl:
            w.writerow([gid, dt, "1010", desc, ref, f"{amt:.2f}"])


def _write_csv_stmt(folder: Path, currency: str, opening: float,
                    bank: list[tuple]) -> float:
    running = opening
    with open(folder / "bank_statement.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["date", "description", "reference", "counterparty",
                    "amount", "currency", "running_balance"])
        for (dt, desc, ref, amt) in bank:
            running = round(running + amt, 2)
            w.writerow(
                [dt, desc, ref, "", f"{amt:.2f}", currency, f"{running:.2f}"])
    return round(running, 2)


def _write_mt940_stmt(folder: Path, currency: str, opening: float,
                      bank: list[tuple]) -> float:
    # date strings are ISO YYYY-MM-DD -> MT940 YYMMDD
    out = [":20:STMT-2026-05", ":25:UPLOAD-ACCT", ":28C:00001/001",
           f":60F:C260501{currency}{opening:.2f}".replace(".", ",")]
    running = opening
    for (dt, desc, ref, amt) in bank:
        yymmdd = dt[2:4] + dt[5:7] + dt[8:10]
        dc = "C" if amt >= 0 else "D"
        amt_str = f"{abs(amt):.2f}".replace(".", ",")
        code = ("CHG" if "CHARGE" in desc.upper() or "FEE" in desc.upper()
                else "INT" if "INTEREST" in desc.upper() else "TRF")
        out.append(f":61:{yymmdd}{dt[5:7]}{dt[8:10]}{dc}{amt_str}N{code}{ref}")
        out.append(f":86:{desc}")
        running = round(running + amt, 2)
    out.append(f":62F:C260531{currency}{running:.2f}".replace(".", ","))
    (folder / "bank_statement.mt940").write_text("\n".join(out) + "\n", encoding="utf-8")
    return round(running, 2)


def _write_pdf_stmt(folder: Path, currency: str, opening: float, bank: list[tuple],
                    bank_name: str, account: str) -> float:
    running = opening
    body = []
    for (dt, desc, ref, amt) in bank:
        running = round(running + amt, 2)
        body.append(
            f"{dt}  {desc[:38]:<38} {ref[:10]:<10} {amt:>11,.2f} {running:>12,.2f}")
    lines = ([bank_name, "Statement of Account", f"Account: {account}",
              "Statement Period: May 1, 2026 - May 31, 2026",
              f"Opening Balance: {opening:,.2f} {currency}", "",
              "Date        Description                            Ref         "
              "      Amount      Balance", "-" * 96] + body
             + ["", f"{'Closing balance':>66} {running:>12,.2f} {currency}"])
    content = "BT /F1 9 Tf 30 760 Td 12 TL\n"
    for ln in lines:
        esc = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({esc}) Tj T*\n"
    content += "ET"
    cb = content.encode("latin-1", "replace")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(cb)).encode() +
            b" >>\nstream\n" + cb + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n".encode() + b"0000000000 65535 f \n"
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF".encode()
    (folder / "bank_statement.pdf").write_bytes(bytes(out))
    return round(running, 2)


def _how_to(folder: Path, fmt: str, account: str, currency: str, opening: float,
            closing: float, story: str, expected: str) -> None:
    ext = {"csv": "csv", "mt940": "mt940", "pdf": "pdf"}[fmt]
    (folder / "HOW_TO_UPLOAD.txt").write_text(
        f"HOW TO UPLOAD THIS PAIR  ({fmt.upper()} statement + CSV general ledger)\n"
        f"{'=' * 60}\n\n{story}\n\n"
        f"In the Streamlit sidebar -> 'Upload statement':\n"
        f"  1. Bank statement box  ->  bank_statement.{ext}\n"
        f"  2. GL export box       ->  gl_export.csv     <-- enables matching!\n"
        f"  3. Account ID          ->  {account}\n"
        f"  4. Currency / Period   ->  {currency} / 2026-05\n"
        f"  5. Leave 'Auto-detect opening/closing' ON "
        f"(opening {opening:,.2f}, closing {closing:,.2f} read from the file)\n"
        f"  6. Click 'Upload & run'.\n\n"
        f"EXPECTED RESULT:\n  {expected}\n\n"
        f"Upload the statement WITHOUT gl_export.csv and you get 0% "
        f"(statement-only analysis - nothing to match against).\n",
        encoding="utf-8")


# ----------------------------------------------------------------- scenarios
CLEAN_BANK = [
    ("2026-05-04", "CUSTOMER PAYMENT - HARBOR WORKS", "INV7710", 3500.00),
    ("2026-05-15", "VENDOR PAYMENT - SUMMIT TOOLS", "PO9921", -2100.00),
    ("2026-05-28", "CUSTOMER PAYMENT - ORBIT MEDIA", "INV7720", 1800.00)]
CLEAN_GL = [
    ("G001", "2026-05-04", "AR receipt Harbor Works", "INV7710", 3500.00),
    ("G002", "2026-05-15", "AP payment Summit Tools", "PO9921", -2100.00),
    ("G003", "2026-05-28", "AR receipt Orbit Media", "INV7720", 1800.00)]

FEE_BANK = [
    ("2026-05-02", "CUSTOMER PAYMENT - ACME CORP", "INV2201", 5000.00),
    ("2026-05-08", "VENDOR PAYMENT - BLUE OFFICE", "PO7741", -1800.00),
    ("2026-05-16", "CUSTOMER PAYMENT - RIVERSIDE", "INV2210", 2200.00),
    ("2026-05-31", "MONTHLY SERVICE CHARGE", "FEE0531", -45.00)]
FEE_GL = [
    ("G001", "2026-05-02", "AR receipt Acme Corp", "INV2201", 5000.00),
    ("G002", "2026-05-08", "AP payment Blue Office", "PO7741", -1800.00),
    ("G003", "2026-05-16", "AR receipt Riverside", "INV2210", 2200.00)]

DUP_BANK = [
    ("2026-05-03", "CUSTOMER ACH - ORION RETAIL", "INV5501", 7500.00),
    ("2026-05-10", "ACH PAYMENT - STELLAR PARTS", "ACH7702", -4250.00),
    ("2026-05-11", "ACH PAYMENT - STELLAR PARTS", "ACH7702", -4250.00),
    ("2026-05-20", "CUSTOMER PAYMENT - CEDAR", "INV5510", 3100.00)]
DUP_GL = [
    ("G001", "2026-05-03", "AR receipt Orion Retail", "INV5501", 7500.00),
    ("G002", "2026-05-10", "AP payment Stellar Parts", "ACH7702", -4250.00),
    ("G003", "2026-05-20", "AR receipt Cedar", "INV5510", 3100.00)]

CLEAN_EXP = "100% bank match rate, 0 exceptions -> CLOSED_CLEAN (confirm & close)."
FEE_EXP = ("75% match -> CLOSED_WITH_ADJUSTMENTS: the $45 bank fee (not in the GL) "
           "becomes an auto journal entry.")
DUP_EXP = ("75% match -> ESCALATED: the Stellar Parts ACH was charged twice; the "
           "duplicate is detected and a reversal journal entry is suggested.")
CLEAN_STORY = "Clean account: all 3 bank lines are recorded in the GL (same ref+amount)."
FEE_STORY = ("3 of 4 bank lines match the GL. The bank's $45 monthly service charge "
             "was never booked in the ledger.")
DUP_STORY = ("The bank processed the Stellar Parts ACH ($4,250) TWICE (May 10 & 11); "
             "the GL booked it once.")

# (folder, fmt, account, currency, opening, bank, gl, story, expected)
SCENARIOS = [
    ("csv_1_clean", "csv", "UPL-CSV-CLEAN", "USD", 15000.00,
     CLEAN_BANK, CLEAN_GL, CLEAN_STORY, CLEAN_EXP),
    ("csv_2_bank_fee", "csv", "UPL-CSV-FEE", "USD",
     20000.00, FEE_BANK, FEE_GL, FEE_STORY, FEE_EXP),
    ("csv_3_duplicate", "csv", "UPL-CSV-DUP", "USD",
     30000.00, DUP_BANK, DUP_GL, DUP_STORY, DUP_EXP),
    ("mt940_1_clean", "mt940", "UPL-MT940-CLEAN", "USD",
     18000.00, CLEAN_BANK, CLEAN_GL, CLEAN_STORY, CLEAN_EXP),
    ("mt940_2_bank_fee", "mt940", "UPL-MT940-FEE", "USD",
     25000.00, FEE_BANK, FEE_GL, FEE_STORY, FEE_EXP),
    ("pdf_1_clean", "pdf", "UPL-PDF-CLEAN", "USD", 16000.00,
     CLEAN_BANK, CLEAN_GL, CLEAN_STORY, CLEAN_EXP),
    ("pdf_2_duplicate", "pdf", "UPL-PDF-DUP", "USD",
     30000.00, DUP_BANK, DUP_GL, DUP_STORY, DUP_EXP),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for (folder, fmt, acct, cur, opening, bank, gl, story, expected) in SCENARIOS:
        d = OUT / folder
        d.mkdir(parents=True, exist_ok=True)
        if fmt == "csv":
            closing = _write_csv_stmt(d, cur, opening, bank)
        elif fmt == "mt940":
            closing = _write_mt940_stmt(d, cur, opening, bank)
        else:
            closing = _write_pdf_stmt(d, cur, opening, bank,
                                      f"{acct} COMMUNITY BANK", acct)
        _write_gl(d, gl)
        _how_to(d, fmt, acct, cur, opening, closing, story, expected)
        rows.append((folder, fmt, acct, opening, closing))

    (OUT / "README.txt").write_text(
        "PAIRED SAMPLES - bank statement + GL export, for ALL formats\n"
        "============================================================\n\n"
        "Bank reconciliation needs TWO files:\n"
        "  - bank_statement.(csv/mt940/pdf) : the BANK's record\n"
        "  - gl_export.csv                  : YOUR accounting (general ledger)\n\n"
        "Upload BOTH together (statement box + GL box) -> real reconciliation.\n"
        "Upload only the statement -> 0% (nothing to match against).\n\n"
        "Folders:\n"
        "  csv_1_clean / mt940_1_clean / pdf_1_clean   -> 100% match, CLOSED_CLEAN\n"
        "  csv_2_bank_fee / mt940_2_bank_fee           -> bank fee -> auto journal\n"
        "  csv_3_duplicate / pdf_2_duplicate           -> duplicate -> ESCALATED\n\n"
        "Every format has its own matching GL export and reconciles on upload.\n",
        encoding="utf-8")

    print(f"Wrote {len(rows)} paired samples (CSV + MT940 + PDF) to "
          f"{OUT.relative_to(ROOT)}/")
    for (folder, fmt, acct, op, cl) in rows:
        print(
            f"  {folder:20} [{fmt:5}] {acct:18} open={op:>10,.2f} close={cl:>10,.2f}")


if __name__ == "__main__":
    main()
