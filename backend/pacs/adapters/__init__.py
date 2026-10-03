"""Remote PACS adapters, selected by the node's ``kind``."""
from __future__ import annotations

from pacs.adapters.base import AdapterError, RemotePacs
from pacs.adapters.dicomweb import DicomwebAdapter
from pacs.adapters.dimse import DimseAdapter
from pacs.adapters.orthanc import OrthancAdapter

_KINDS = {"dicomweb": DicomwebAdapter, "orthanc": OrthancAdapter, "dimse": DimseAdapter}


def for_node(node: dict) -> RemotePacs:
    cls = _KINDS.get(node.get("kind") or "dimse")
    if cls is None:
        raise AdapterError(f"unsupported node kind {node.get('kind')!r}")
    return cls(node)


__all__ = ["AdapterError", "RemotePacs", "for_node", "DicomwebAdapter", "OrthancAdapter",
           "DimseAdapter"]
