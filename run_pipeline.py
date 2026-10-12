"""Run the MedicalRAG ingestion pipeline from a source checkout.

The implementation lives in ``src/medrag/run_pipeline.py`` (installed as the
``medrag-pipeline`` console script); this shim keeps ``python run_pipeline.py``
working without ``pip install -e .``.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from medrag.run_pipeline import main  # noqa: E402

if __name__ == "__main__":
    main()
