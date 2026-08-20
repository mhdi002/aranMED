"""Run code-switch batch test using Anaconda CUDA Python when venv torch is missing."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
# Override with ASR_CONDA_PYTHON if your CUDA-enabled interpreter lives
# somewhere other than the default per-user Anaconda install.
CONDA_PY = Path(
    os.environ.get(
        "ASR_CONDA_PYTHON",
        str(Path.home() / "anaconda3" / "python.exe"),
    )
)


def _pick_python() -> Path:
    if VENV_PY.exists():
        try:
            out = subprocess.check_output(
                [str(VENV_PY), "-c", "import torch; print(torch.cuda.is_available())"],
                text=True,
                timeout=20,
            ).strip()
            if out == "True":
                return VENV_PY
        except Exception:
            pass
    if CONDA_PY.exists():
        return CONDA_PY
    return Path(sys.executable)


def main() -> None:
    py = _pick_python()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT / "backend")
    script = ROOT / "scripts" / "test_codeswitch_batch.py"
    print(f"Using Python: {py}")
    raise SystemExit(subprocess.call([str(py), str(script)], env=env, cwd=str(ROOT)))


if __name__ == "__main__":
    main()
