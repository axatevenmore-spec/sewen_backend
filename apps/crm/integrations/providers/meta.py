"""
Meta (Facebook + Instagram) Lead Ads provider adapter.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone as dt_timezone
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from ..base import InboundLeadEvent, LeadSourceProvider, NormalizedLead
from ..crypto import decrypt_credentials

logger = logging.getLogger(__name__)


class MetaAuthError(Exception):
    """Raised when Meta Graph API returns an authorization/token error (code 190)."""
    pass


class MetaLeadSourceProvider(LeadSourceProvider):
    """
    Adapter for Meta Lead Ads webhooks and Graph API lead retrieval.
    """

    @property
    def key(self) -> str:
        return "meta"

    def handle_verification(self, request: HttpRequest) -> Optional[HttpResponse]:
        """
        Handles Meta webhook verification handshake:
        GET with hub.mode, hub.verify_token, hub.challenge.
        """
        mode = request.GET.get("hub.mode")
        verify_token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")

        configured_token = getattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "") or ""

        if mode == "subscribe" and configured_token and hmac.compare_digest(verify_token, configured_token):
            logger.info("Meta webhook verification successful")
            return HttpResponse(challenge, content_type="text/plain", status=200)

        logger.warning(
            "Meta webhook verification failed. mode=%s, token_match=%s",
            mode,
            bool(configured_token and hmac.compare_digest(verify_token, configured_token)),
        )
        return HttpResponse("Verification failed", content_type="text/plain", status=403)

    def verify_authenticity(self, request: HttpRequest) -> bool:
        """
        Validates the incoming request using HMAC-SHA256 signature in X-Hub-Signature-256.
        """
        app_secret = getattr(settings, "META_APP_SECRET", "") or ""
        if not app_secret:
            logger.error("META_APP_SECRET is not configured in settings. Cannot verify authenticity.")
            return False

        signature_header = (
            request.headers.get("X-Hub-Signature-256")
            or request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
        )
        if not signature_header:
            logger.warning("Missing X-Hub-Signature-256 header in Meta webhook request.")
            return False

        if not signature_header.startswith("sha256="):
            logger.warning("Invalid X-Hub-Signature-256 prefix: %s", signature_header[:10])
            return False

        received_hash = signature_header[7:].strip()
        raw_body = getattr(request, "body", b"")
        computed_hash = hmac.new(
            app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        is_valid = hmac.compare_digest(computed_hash, received_hash)
        if not is_valid:
            logger.warning("Meta webhook signature mismatch.")
        return is_valid

    def parse_events(self, request: HttpRequest) -> List[InboundLeadEvent]:
        """
        Extracts leadgen changes from payload:
        entry[].changes[] where field == 'leadgen'.
        """
        events: List[InboundLeadEvent] = []
        try:
            body_text = request.body.decode("utf-8")
            payload = json.loads(body_text) if body_text else {}
        except Exception as exc:
            logger.error("Failed to parse Meta webhook payload as JSON: %s", exc)
            return []

        entries = payload.get("entry", [])
        if not isinstance(entries, list):
            return []

        for entry in entries:
            entry_page_id = str(entry.get("id", "")) if entry.get("id") else None
            changes = entry.get("changes", [])
            if not isinstance(changes, list):
                continue

            for change in changes:
                if change.get("field") != "leadgen":
                    continue
                val = change.get("value", {})
                if not isinstance(val, dict):
                    continue

                leadgen_id = str(val.get("leadgen_id") or "")
                if not leadgen_id:
                    continue

                created_ts = val.get("created_time")
                created_dt = None
                if created_ts:
                    try:
                        created_dt = datetime.fromtimestamp(int(created_ts), tz=dt_timezone.utc)
                    except (ValueError, TypeError, OverflowError):
                        created_dt = None

                page_id = str(val.get("page_id") or entry_page_id or "")
                form_id = str(val.get("form_id") or "") if val.get("form_id") else None
                ad_id = str(val.get("ad_id") or "") if val.get("ad_id") else None
                adset_id = str(val.get("adgroup_id") or val.get("adset_id") or "") if (val.get("adgroup_id") or val.get("adset_id")) else None

                events.append(
                    InboundLeadEvent(
                        external_lead_id=leadgen_id,
                        external_account_id=page_id or None,
                        external_form_id=form_id,
                        external_adset_id=adset_id,
                        external_ad_id=ad_id,
                        created_time=created_dt,
                        raw_payload=val,
                    )
                )

        logger.info("Parsed %d lead event(s) from Meta webhook", len(events))
        return events

    def fetch_lead_details(self, event: InboundLeadEvent, integration: Any) -> Dict[str, Any]:
        """
        Retrieves lead details via Meta Graph API:
        GET https://graph.facebook.com/{version}/{leadgen_id}
        """
        credentials = decrypt_credentials(getattr(integration, "encrypted_credentials", ""))
        access_token = credentials.get("access_token") or credentials.get("page_access_token")
        if not access_token:
            raise ValueError(f"Integration {integration.id} has no valid Meta access token")

        version = getattr(settings, "META_GRAPH_API_VERSION", "v21.0") or "v21.0"
        url = (
            f"https://graph.facebook.com/{version}/{event.external_lead_id}?"
            + urllib.parse.urlencode({
                "fields": "id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,platform",
                "access_token": access_token,
            })
        )

        masked_token = access_token[:4] + "..." + access_token[-4:] if len(access_token) > 8 else "***"
        logger.info(
            "Fetching Meta lead %s via Graph API %s with token %s",
            event.external_lead_id,
            version,
            masked_token,
        )

        req = urllib.request.Request(url, headers={"User-Agent": "Evenmore-ERP-LeadIngestion/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(err_body)
                error_obj = err_data.get("error", {})
                code = error_obj.get("code")
                msg = error_obj.get("message", "")
                if code == 190:
                    logger.error(
                        "Meta Graph API authentication error (code 190): %s. Integration %s needs re-auth.",
                        msg,
                        integration.id,
                    )
                    # Mark integration setting if possible
                    if hasattr(integration, "settings") and isinstance(integration.settings, dict):
                        integration.settings["needs_reauth"] = True
                        integration.settings["reauth_reason"] = msg
                        integration.save(update_fields=["settings", "updated_at"])
                    raise MetaAuthError(f"Meta token invalid/expired (code 190): {msg}")
            except (json.JSONDecodeError, AttributeError):
                pass
            logger.error("HTTP error fetching Meta lead %s: %s %s", event.external_lead_id, exc.code, err_body)
            raise
        except Exception as exc:
            logger.error("Failed to fetch Meta lead %s: %s", event.external_lead_id, exc)
            raise

    def normalize(self, details: Dict[str, Any], event: Optional[InboundLeadEvent] = None) -> NormalizedLead:
        """
        Transforms Graph API lead payload into a canonical NormalizedLead.
        """
        external_lead_id = str(details.get("id") or (event.external_lead_id if event else ""))
        field_data = details.get("field_data", [])

        custom_fields: Dict[str, Any] = {}
        first_name: Optional[str] = None
        last_name: Optional[str] = None
        full_name: Optional[str] = None
        email: Optional[str] = None
        phone: Optional[str] = None
        city: Optional[str] = None
        company: Optional[str] = None
        job_title: Optional[str] = None

        for item in field_data:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip().lower()
            values = item.get("values", [])
            val = values[0].strip() if (values and isinstance(values[0], str)) else (values[0] if values else None)
            if val is None:
                continue

            if name in ("full_name", "fullname", "name"):
                full_name = str(val)
            elif name in ("first_name", "firstname"):
                first_name = str(val)
            elif name in ("last_name", "lastname"):
                last_name = str(val)
            elif name in ("email", "email_address"):
                email = str(val).strip().lower()
            elif name in ("phone_number", "phone", "mobile", "contact_number"):
                phone = str(val)
            elif name in ("city", "town"):
                city = str(val)
            elif name in ("company_name", "company", "organization"):
                company = str(val)
            elif name in ("job_title", "jobtitle", "designation", "title"):
                job_title = str(val)
            else:
                custom_fields[item.get("name") or name] = val if len(values) <= 1 else values

        # Handle attribution
        platform = str(details.get("platform") or "").lower()
        if not platform:
            platform = "facebook"

        attribution = {
            "platform": platform,
            "campaignId": details.get("campaign_id"),
            "campaignName": details.get("campaign_name"),
            "adSetId": details.get("adset_id") or (event.external_adset_id if event else None),
            "adSetName": details.get("adset_name"),
            "adId": details.get("ad_id") or (event.external_ad_id if event else None),
            "adName": details.get("ad_name"),
            "formId": details.get("form_id") or (event.external_form_id if event else None),
            "formName": details.get("form_name"),
        }

        # Submitted at
        submitted_at = None
        created_time_str = details.get("created_time")
        if created_time_str:
            try:
                # Graph API returns ISO-8601 like "2026-10-19T10:00:00+0000"
                # Strip and parse
                clean_str = created_time_str.replace("+0000", "+00:00")
                submitted_at = datetime.fromisoformat(clean_str)
            except Exception:
                submitted_at = timezone.now()
        elif event and event.created_time:
            submitted_at = event.created_time
        else:
            submitted_at = timezone.now()

        return NormalizedLead(
            provider=self.key,
            external_lead_id=external_lead_id,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            email=email,
            phone=phone,
            city=city,
            company=company,
            job_title=job_title,
            custom_fields=custom_fields,
            attribution=attribution,
            submitted_at=submitted_at,
            raw=details,
        )

    def backfill(self, integration: Any, since: Optional[datetime] = None) -> List[NormalizedLead]:
        """
        Fetches historical leads from Meta forms:
        GET /{form_id}/leads?filtering=[{"field":"time_created","operator":"GREATER_THAN","value":<unix_ts>}]
        """
        credentials = decrypt_credentials(getattr(integration, "encrypted_credentials", ""))
        access_token = credentials.get("access_token") or credentials.get("page_access_token")
        if not access_token:
            logger.warning("No access token found for integration %s during backfill", integration.id)
            return []

        form_ids = (integration.settings or {}).get("form_ids") or []
        if not form_ids:
            logger.info("No form_ids configured for integration %s backfill", integration.id)
            return []

        version = getattr(settings, "META_GRAPH_API_VERSION", "v21.0") or "v21.0"
        normalized_leads: List[NormalizedLead] = []

        for form_id in form_ids:
            query_params: Dict[str, Any] = {
                "fields": "id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,platform",
                "access_token": access_token,
                "limit": "50",
            }
            if since:
                unix_ts = int(since.timestamp())
                filtering = json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": unix_ts}])
                query_params["filtering"] = filtering

            url = f"https://graph.facebook.com/{version}/{form_id}/leads?" + urllib.parse.urlencode(query_params)
            while url:
                req = urllib.request.Request(url, headers={"User-Agent": "Evenmore-ERP-LeadIngestion/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        data = res.get("data", [])
                        for item in data:
                            normalized_leads.append(self.normalize(item))
                        paging = res.get("paging", {})
                        url = paging.get("next")
                except Exception as exc:
                    logger.error("Error during Meta backfill on form %s: %s", form_id, exc)
                    break

        return normalized_leads
