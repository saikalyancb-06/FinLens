"""
Bank adapter registry / factory.

The runner and routes depend on this module instead of importing bank-specific
adapters directly. New banks can be added by registering a new adapter here.
"""

from app.rpa.canara_adapter import CanaraAdapter
from app.rpa.hdfc_adapter import HDFCAdapter
from app.rpa.icici_adapter import ICICIAdapter
from app.rpa.sbi_adapter import SBIAdapter


ADAPTER_CLASSES = {
    "sbi": SBIAdapter,
    "icici": ICICIAdapter,
    "hdfc": HDFCAdapter,
    "canara": CanaraAdapter,
}


def get_supported_bank_keys() -> list[str]:
    return list(ADAPTER_CLASSES.keys())


def get_supported_banks() -> list[dict[str, str]]:
    return [
        {"key": key, "name": adapter_cls.bank_display_name}
        for key, adapter_cls in ADAPTER_CLASSES.items()
    ]


def get_adapter_class(bank_key: str):
    return ADAPTER_CLASSES.get((bank_key or "").lower())


def create_bank_adapter(bank_key: str, **kwargs):
    adapter_cls = get_adapter_class(bank_key)
    if not adapter_cls:
        raise KeyError(bank_key)
    return adapter_cls(**kwargs)