# MedRAG `data/` (local only)

Working corpora for OCR text, chunks, manifests, and archives live here.
Contents are **gitignored** — only this README and `.gitkeep` are tracked.

## Env

```env
MEDRAG_ROOT=.
MEDRAG_DATA_DIR=./data
```

## One-time migration (optional)

If you already have a previous MedicalRAG checkout with a populated `data/` folder,
either:

1. **Point env at it** (no copy): set `MEDRAG_DATA_DIR` in your local `.env` to that folder, or
2. **Copy/symlink into this repo** (PowerShell example — replace `SOURCE` yourself):

```powershell
# robocopy "SOURCE\data" ".\data" /E
# OR: New-Item -ItemType Junction -Path .\data -Target "SOURCE\data"
```

Do not hardcode machine paths in source or committed config.
