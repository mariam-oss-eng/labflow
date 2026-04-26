"""Official Python SDK for the LabFlow API (v0.7).

Lightweight, dependency-free wrapper around the REST surface. Uses
``httpx`` when installed and falls back to the stdlib ``urllib`` so the
SDK works in any environment, including Lambda runtimes that pin against
a minimal install footprint.
"""
from .client import AsyncLabFlow, LabFlow, LabFlowError

__version__ = "0.7.0"
__all__ = ["LabFlow", "AsyncLabFlow", "LabFlowError", "__version__"]
