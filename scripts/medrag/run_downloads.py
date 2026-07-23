"""Launch EN textbook bulk download from docx catalog lists.

Expects an external downloader tree. Override with MEDRAG_DOWNLOADER_SRC
(defaults to sibling Data-main/medrag/src relative to this repo).
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import REPORTS_DIR  # noqa: E402

_default_src = (ROOT / "../../Data-main/medrag/src").resolve()
MEDRAG_SRC = Path(
    os.environ.get("MEDRAG_DOWNLOADER_SRC", str(_default_src))
).expanduser().resolve()
LOG = REPORTS_DIR / "download_run.log"


def main():
    if not MEDRAG_SRC.is_dir():
        raise SystemExit(
            f"Downloader src not found: {MEDRAG_SRC}\n"
            "Set MEDRAG_DOWNLOADER_SRC to the folder containing parse_catalog.py"
        )
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n[{stamp}] === download batch started ===\n")
        f.flush()
        subprocess.run(
            [sys.executable, "parse_catalog.py"],
            cwd=MEDRAG_SRC,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
        subprocess.run(
            [sys.executable, "downloader.py", "--retry-failed", "--concurrency", "6"],
            cwd=MEDRAG_SRC,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === download batch finished ===\n")


if __name__ == "__main__":
    main()
