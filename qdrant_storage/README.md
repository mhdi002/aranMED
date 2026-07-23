# Qdrant storage (local only)

Live Qdrant server persistence directory. Contents are **gitignored**.

## Env

```env
MEDRAG_QDRANT_STORAGE=./qdrant_storage
# QDRANT_PATH=./qdrant_storage   # alias used by some scripts
QDRANT_URL=http://127.0.0.1:6333
```

Compose / Docker typically mounts this path into the Qdrant container (`docker-compose.medrag.yml`).

## One-time migration (optional)

If you already have Qdrant data from a previous MedicalRAG install:

1. Stop Qdrant, then either set `MEDRAG_QDRANT_STORAGE` in local `.env` to the old folder, or
2. Copy/junction into `./qdrant_storage` (do not commit the blobs).

Never commit vector indexes.
