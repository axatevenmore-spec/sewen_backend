"""
Lead source integrations package.
"""
from .base import InboundLeadEvent, LeadSourceProvider, NormalizedLead
from .crypto import decrypt_credentials, encrypt_credentials
from .providers.meta import MetaLeadSourceProvider
from .registry import get_provider, list_providers, register_provider

# Register default providers
register_provider(MetaLeadSourceProvider())

__all__ = [
    "LeadSourceProvider",
    "NormalizedLead",
    "InboundLeadEvent",
    "register_provider",
    "get_provider",
    "list_providers",
    "encrypt_credentials",
    "decrypt_credentials",
]
