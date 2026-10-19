"""
Provider registry mapping provider keys to adapter instances.
"""
from typing import Dict, List, Optional
from .base import LeadSourceProvider

_REGISTRY: Dict[str, LeadSourceProvider] = {}


def register_provider(provider: LeadSourceProvider) -> None:
    """Register a provider adapter instance."""
    _REGISTRY[provider.key.lower()] = provider


def get_provider(provider_key: str) -> Optional[LeadSourceProvider]:
    """Retrieve the provider adapter registered for the given key."""
    if not provider_key:
        return None
    return _REGISTRY.get(provider_key.strip().lower())


def list_providers() -> List[str]:
    """List all registered provider keys."""
    return list(_REGISTRY.keys())
