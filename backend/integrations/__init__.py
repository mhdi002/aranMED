"""External microservice clients (MedicalRAG, etc.)."""

from .medrag_client import MedragClient, MedragError, get_medrag_client

__all__ = ["MedragClient", "MedragError", "get_medrag_client"]
