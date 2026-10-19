"""
Provider adapter interface and standard normalized data structures.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from django.http import HttpRequest, HttpResponse


@dataclass
class NormalizedLead:
    """
    Standard common representation of an ingested lead regardless of provider source.
    """
    provider: str
    external_lead_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    custom_fields: Dict[str, Any] = field(default_factory=dict)
    attribution: Dict[str, Any] = field(default_factory=dict)
    submitted_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Resolved display name: full_name or combined first + last name."""
        if self.full_name and self.full_name.strip():
            return self.full_name.strip()
        parts = [p for p in (self.first_name, self.last_name) if p and p.strip()]
        if parts:
            return " ".join(parts).strip()
        return "Unknown Lead"


@dataclass
class InboundLeadEvent:
    """
    Extracted lead event parsed from an inbound webhook payload.
    """
    external_lead_id: str
    external_account_id: Optional[str] = None  # e.g. Page ID, Ad Account ID
    external_form_id: Optional[str] = None
    external_campaign_id: Optional[str] = None
    external_adset_id: Optional[str] = None
    external_ad_id: Optional[str] = None
    created_time: Optional[datetime] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class LeadSourceProvider(ABC):
    """
    Common provider adapter interface. Every external lead source (Meta, Google, LinkedIn, etc.)
    implements this interface.
    """

    @property
    @abstractmethod
    def key(self) -> str:
        """Unique provider identifier (e.g. 'meta', 'google', 'linkedin')."""
        pass

    @abstractmethod
    def handle_verification(self, request: HttpRequest) -> Optional[HttpResponse]:
        """
        Handles provider-specific URL verification / webhook handshake.
        Return an HttpResponse (e.g. 200 with challenge) or None if verification is not supported.
        """
        pass

    @abstractmethod
    def verify_authenticity(self, request: HttpRequest) -> bool:
        """
        Validates that the incoming request is genuine.
        Uses HMAC signature, shared key, or token based on provider requirement.
        """
        pass

    @abstractmethod
    def parse_events(self, request: HttpRequest) -> List[InboundLeadEvent]:
        """
        Extracts one or more lead events from the webhook payload.
        A single request may contain multiple entries/events.
        """
        pass

    @abstractmethod
    def fetch_lead_details(self, event: InboundLeadEvent, integration: Any) -> Dict[str, Any]:
        """
        Retrieves full lead data. If the webhook only contained IDs (like Meta Graph API),
        makes an API call using integration credentials. If full data is already in payload,
        returns the payload.
        """
        pass

    @abstractmethod
    def normalize(self, details: Dict[str, Any], event: Optional[InboundLeadEvent] = None) -> NormalizedLead:
        """
        Converts provider-specific data dictionary into a canonical NormalizedLead object.
        """
        pass

    def backfill(self, integration: Any, since: Optional[datetime] = None) -> List[NormalizedLead]:
        """
        Optional: Pulls leads via provider API to recover any submissions missed
        while the server was down or unhooked.
        """
        return []
