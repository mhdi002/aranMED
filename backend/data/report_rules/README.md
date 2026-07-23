# Hospital / insurance report-title rules (DOCX + TXT + PDF siblings).
#
# Paths are configured via env (see repo-root `.env.example`):
#   REPORT_RULES_DIR
#   REPORT_RULES_DOCX
#   REPORT_RULES_TXT
#   REPORT_RULES_PDF
#   REPORT_RULES_PATH   → backend/data/REPORT_RULES.json
#
# Never point these at absolute Downloads folders in application code.
# Re-copy from the institutional source with:
#   copy "<source>\all.docx" backend\data\report_rules\hospital_insurance_report_titles.docx
#
# Embed into MedicalRAG/Qdrant (external service; no MedRAG source edits):
#   .venv\Scripts\python.exe scripts\ingest_report_rules_medrag.py
