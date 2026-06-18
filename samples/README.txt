PAIRED SAMPLES - bank statement + GL export, for ALL formats
============================================================

Bank reconciliation needs TWO files:
  - bank_statement.(csv/mt940/pdf) : the BANK's record
  - gl_export.csv                  : YOUR accounting (general ledger)

Upload BOTH together (statement box + GL box) -> real reconciliation.
Upload only the statement -> 0% (nothing to match against).

Folders:
  csv_1_clean / mt940_1_clean / pdf_1_clean   -> 100% match, CLOSED_CLEAN
  csv_2_bank_fee / mt940_2_bank_fee           -> bank fee -> auto journal
  csv_3_duplicate / pdf_2_duplicate           -> duplicate -> ESCALATED

Every format has its own matching GL export and reconciles on upload.
