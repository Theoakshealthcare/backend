"""The booking Flow's data endpoint logic (WA-FR-13, WA-FR-16).

Meta calls the endpoint for ``ping`` (health check), ``INIT`` (the Flow opened), ``data_exchange`` (a screen's
button or the calendar was used) and ``BACK`` (the person went back to a screen with ``refresh_on_back``):
  https://developers.facebook.com/docs/whatsapp/flows/guides/implementingyourflowendpoint
  https://developers.facebook.com/docs/whatsapp/flows/reference/flowjson  (screens, ``data``, routing_model)
Our reply is ``{"version": "3.0", "screen": <next>, "data": {...}}``; the last screen answers ``SUCCESS`` with
the flow token so the completion message carries nothing but the token.

The answers live in a draft (a ``quotes`` row, status ``draft``) found through the signed flow token, never in
the Flow itself, so a Flow can be re-entered and nothing sensitive travels through the webhook or the Redis
queue: the condition and special instructions are Fernet-encrypted straight into ``quotes.sensitive_encrypted``
and are never returned in any response, logged, or echoed.

Everything the person typed is validated again here (the Flow's own checks are only a convenience), and who
the visit is for is re-derived from the contact's own care relationships: an id in the request is never trusted.
A problem on a screen answers with the same screen and a ``notice`` the screen shows; anything unusable (an
expired or forged token) answers 427 so the Flow tells the person to start again.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp import copy
from app.channels.whatsapp.flows import token as flow_token
from app.channels.whatsapp.lexicon import is_emergency
from app.core.config import settings
from app.core.phone import normalize_e164
from app.core.security import encrypt
from app.db.models.booking_settings import BookingSettings
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.whatsapp import WhatsAppContact
from app.db.models.whatsapp_booking import Quote
from app.services.pricing import price_service

logger = logging.getLogger(__name__)

VERSION = "3.0"
NOTICE_LEN = 250
MAX_CONDITION = 500
MAX_INSTRUCTIONS = 300

RELATIONSHIPS = (("parent", "Parent"), ("spouse", "Spouse or partner"), ("child", "Child"),
                 ("sibling", "Sibling"), ("relative", "Other relative"), ("friend", "Friend"),
                 ("other", "Other"))
GENDERS = (("female", "Female"), ("male", "Male"), ("other", "Other"))
_NAME = re.compile(r"^[^\W\d_][\w .'\-]{1,99}$", re.UNICODE)

STATUS_INVALID_TOKEN = 427   # Meta: flow token expired or invalid; the Flow shows "start again"
STATUS_BAD_REQUEST = 400


SUBMITTED_BY_ADDRESS = ("submitted", "out_of_area")  # where the ADDRESS screen leaves a draft


@dataclass(frozen=True)
class FlowResult:
    status: int
    body: Optional[Dict[str, Any]] = None


def _ok(body: Dict[str, Any]) -> FlowResult:
    return FlowResult(200, body)


def _screen(name: str, **data: Any) -> FlowResult:
    data.setdefault("notice", "")
    return _ok({"version": VERSION, "screen": name, "data": data})


def _options(pairs) -> List[Dict[str, str]]:
    return [{"id": key, "title": title} for key, title in pairs]


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable() or ch in "\n\t").strip()
    return cleaned if cleaned and len(cleaned) <= limit else None


class FlowService:
    def __init__(self, db: Session, now: Optional[datetime] = None):
        self.db = db
        self.now = now or datetime.utcnow()
        self._contact: Optional[WhatsAppContact] = None
        self._flow_token = ""

    # --- entry point -------------------------------------------------------------------------
    def handle(self, payload: Dict[str, Any]) -> FlowResult:
        action = payload.get("action")
        if action == "ping":
            return _ok({"version": VERSION, "data": {"status": "active"}})
        # Meta retries the final screen when our answer is slow or lost. By then the first try has committed and the
        # draft is no longer a draft, so that one screen (and only it) recognises its own finished submission.
        final_screen = action == "data_exchange" and payload.get("screen") == "ADDRESS"
        quote = self._load_draft(payload.get("flow_token"), also=SUBMITTED_BY_ADDRESS if final_screen else ())
        if quote is None:
            return FlowResult(STATUS_INVALID_TOKEN)
        self._flow_token = payload["flow_token"]
        if quote.status != "draft":
            self.db.rollback()  # nothing to change: hand back the same success the first try got
            return self._success()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        screen = payload.get("screen")
        if action == "INIT":
            result = self._render("WHO", quote)
        elif action == "BACK":
            result = self._render(screen, quote)
        elif action == "data_exchange":
            if data.get("error"):  # Meta telling us the Flow itself hit a problem; nothing to do but acknowledge
                logger.warning("whatsapp flow reported an error: %s", str(data.get("error"))[:80])
                return _ok({"version": VERSION, "data": {"acknowledged": True}})
            handler = self._HANDLERS.get(screen)
            result = handler(self, quote, data) if handler else FlowResult(STATUS_BAD_REQUEST)
        else:
            result = FlowResult(STATUS_BAD_REQUEST)
        self.db.commit()
        return result

    # --- the draft and who it belongs to -------------------------------------------------
    def _load_draft(self, token: Any, also: tuple = ()) -> Optional[Quote]:
        draft_id = flow_token.verify(token, self.now)
        if draft_id is None:
            return None
        quote = (self.db.query(Quote).filter(Quote.id == draft_id).populate_existing().with_for_update().first())
        if quote is None or quote.status not in ("draft", *also):
            self.db.rollback()
            return None
        self._contact = self.db.get(WhatsAppContact, quote.contact_id)
        if self._contact is None or self._contact.blocked or self._contact.user_id is None:
            self.db.rollback()
            return None
        return quote

    @staticmethod
    def _save(quote: Quote, **changes: Any) -> None:
        quote.payload = {**(quote.payload or {}), **changes}  # reassign: JSONB has no change tracking inside

    def _seal(self, quote: Quote, **secrets: str) -> None:
        """Merge ``secrets`` into the encrypted blob. Plaintext exists only in this call."""
        from app.core.security import decrypt
        current = json.loads(decrypt(quote.sensitive_encrypted)) if quote.sensitive_encrypted else {}
        current.update(secrets)
        quote.sensitive_encrypted = encrypt(json.dumps(current))

    # --- recipients ---------------------------------------------------------------------
    def _relationships(self) -> List[tuple]:
        return (self.db.query(CareRelationship, CareRecipient)
                .join(CareRecipient, CareRecipient.id == CareRelationship.recipient_id)
                .filter(CareRelationship.contact_user_id == self._contact.user_id)
                .order_by(CareRelationship.created_at).all())

    def _saved_count(self, rows) -> int:
        return sum(1 for rel, _ in rows if rel.role != "self")

    # --- screens ---------------------------------------------------------------------------
    def _render(self, screen: Any, quote: Quote, notice: str = "") -> FlowResult:
        renderer = self._RENDERERS.get(screen)
        if renderer is None:
            return FlowResult(STATUS_BAD_REQUEST)
        return renderer(self, quote, notice)

    def _render_who(self, quote: Quote, notice: str = "") -> FlowResult:
        rows = self._relationships()
        options = []
        if any(rel.role == "self" for rel, _ in rows):
            options.append({"id": "self", "title": "Myself"})
        for rel, recipient in rows:
            if rel.role != "self":
                options.append({"id": f"saved:{recipient.id}", "title": recipient.full_name[:30]})
        if self._saved_count(rows) < settings.WHATSAPP_MAX_RECIPIENTS:
            options.append({"id": "new", "title": "Someone new"})
        return _screen("WHO", recipients=options, notice=notice)

    def _render_recipient(self, quote: Quote, notice: str = "") -> FlowResult:
        return _screen("RECIPIENT", relationships=_options(RELATIONSHIPS), genders=_options(GENDERS), notice=notice)

    def _services(self) -> List[str]:
        row = self._settings()
        return list(rules.SERVICE_ORDER) if row is not None else []

    def _settings(self) -> Optional[BookingSettings]:
        return self.db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()

    def _render_service(self, quote: Quote, notice: str = "") -> FlowResult:
        row = self._settings()
        options = []
        for service in (rules.SERVICE_ORDER if row is not None else ()):
            per_hour = price_service(row, service, 1).total_kobo
            options.append({"id": service, "title": rules.SERVICE_LABELS[service],
                            "description": f"{rules.naira(per_hour)} per hour"})
        if not options:
            # Nothing has a configured price (WA-FR-21): end the Flow rather than leave the person on an empty list.
            quote.status = "cancelled"
            quote.payload = {"reason": "no_services"}
            quote.sensitive_encrypted = None
            return self._success()
        return _screen("SERVICE", services=options, notice=notice)

    def _success(self) -> FlowResult:
        return _ok({"version": VERSION, "screen": "SUCCESS", "data": {"extension_message_response": {
            "params": {"flow_token": self._flow_token}}}})

    def _render_when(self, quote: Quote, notice: str = "", day: Optional[date] = None) -> FlowResult:
        first, last = rules.date_range(self.now)
        shown = day or rules.first_bookable_day(self.now) or first
        return _screen("WHEN", min_date=first.isoformat(), max_date=last.isoformat(), first_date=shown.isoformat(),
                       slots=[{"id": s, "title": rules.slot_title(s)} for s in rules.slots_for(shown, self.now)],
                       notice=notice)

    def _render_details(self, quote: Quote, notice: str = "") -> FlowResult:
        cap = rules.max_hours((quote.payload or {}).get("service", ""))
        durations = [{"id": str(h), "title": f"{h} hour" + ("" if h == 1 else "s")} for h in range(1, cap + 1)]
        return _screen("DETAILS", durations=durations, notice=notice)

    def _render_address(self, quote: Quote, notice: str = "") -> FlowResult:
        cities = _options((c, c) for c in rules.service_cities()) + [{"id": "other", "title": "Somewhere else"}]
        return _screen("ADDRESS", cities=cities, notice=notice)

    _RENDERERS: Dict[str, Callable] = {
        "WHO": _render_who, "RECIPIENT": _render_recipient, "SERVICE": _render_service,
        "WHEN": _render_when, "DETAILS": _render_details, "ADDRESS": _render_address,
    }

    # --- what each screen sends -------------------------------------------------------------
    def _on_who(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        choice = data.get("who")
        rows = self._relationships()
        if choice == "new":
            if self._saved_count(rows) >= settings.WHATSAPP_MAX_RECIPIENTS:
                return self._render_who(quote, f"You can save up to {settings.WHATSAPP_MAX_RECIPIENTS} people. "
                                                "Please choose one of them.")
            self._save(quote, who="new", recipient_id=None)
            return self._render_recipient(quote)
        recipient_id = self._owned_recipient(choice, rows)
        if recipient_id is None:
            return self._render_who(quote, "Please choose who the visit is for.")
        self._save(quote, who="saved" if choice != "self" else "self", recipient_id=str(recipient_id),
                   new_recipient=None)
        return self._render_service(quote)

    @staticmethod
    def _owned_recipient(choice: Any, rows) -> Optional[uuid.UUID]:
        """The recipient id for ``self`` / ``saved:<id>``, only if it is one of the contact's own."""
        if not isinstance(choice, str):
            return None
        if choice == "self":
            return next((r.id for rel, r in rows if rel.role == "self"), None)
        if choice.startswith("saved:"):
            try:
                wanted = uuid.UUID(choice.split(":", 1)[1])
            except ValueError:
                return None
            return next((r.id for rel, r in rows if rel.role != "self" and r.id == wanted), None)
        return None

    def _on_recipient(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        if (quote.payload or {}).get("who") != "new":
            return self._render_who(quote)
        name = _clean_text(data.get("full_name"), 100)
        if not name or not _NAME.match(name):
            return self._render_recipient(quote, "Please enter their full name.")
        if data.get("relationship") not in dict(RELATIONSHIPS):
            return self._render_recipient(quote, "Please choose your relationship to them.")
        if data.get("gender") not in dict(GENDERS):
            return self._render_recipient(quote, "Please choose their gender.")
        age = self._age(data.get("age"))
        if age is None:
            return self._render_recipient(quote, "Please enter their age as a number between 0 and 120.")
        phone = None
        raw_phone = data.get("emergency_phone")
        if isinstance(raw_phone, str) and raw_phone.strip():
            phone = normalize_e164(raw_phone.strip())
            if phone is None:
                return self._render_recipient(quote, "That emergency phone number doesn't look right. "
                                                     "Please check it, or leave it empty.")
        self._save(quote, new_recipient={"full_name": name, "relationship": data["relationship"],
                                         "gender": data["gender"], "age": age, "emergency_phone": phone})
        return self._render_service(quote)

    @staticmethod
    def _age(raw: Any) -> Optional[int]:
        try:
            age = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return age if 0 <= age <= 120 else None

    def _on_service(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        service = data.get("service")
        if service not in self._services():
            return self._render_service(quote, "Please choose one of the services shown.")
        self._save(quote, service=service)
        return self._render_when(quote)

    def _on_when(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        day = rules.parse_day(data.get("date"))
        if data.get("trigger") == "date_selected":
            first, last = rules.date_range(self.now)
            if day is None or not (first <= day <= last):
                return self._render_when(quote, "Please choose a date within the next 30 days.")
            return self._render_when(quote, day=day)
        time_id = data.get("time")
        if day is None or not isinstance(time_id, str) or time_id not in rules.slots_for(day, self.now):
            return self._render_when(quote, "That time isn't available. Please choose a date and a start time.",
                                     day=day if day and rules.slots_for(day, self.now) else None)
        self._save(quote, date=day.isoformat(), time=time_id)
        return self._render_details(quote)

    def _on_details(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        service = (quote.payload or {}).get("service")
        if not service:
            return self._render_service(quote)
        try:
            hours = int(str(data.get("duration")).strip())
        except (TypeError, ValueError):
            hours = 0
        if not 1 <= hours <= rules.max_hours(service):
            return self._render_details(quote, "Please choose how long the visit should be.")
        condition = _clean_text(data.get("condition"), MAX_CONDITION)
        if condition is None:
            return self._render_details(
                quote, f"Please tell us what they need help with (up to {MAX_CONDITION} characters).")
        instructions = data.get("instructions")
        secrets = {"condition": condition}
        if isinstance(instructions, str) and instructions.strip():
            cleaned = _clean_text(instructions, MAX_INSTRUCTIONS)
            if cleaned is None:
                return self._render_details(quote, f"Please keep extra notes under {MAX_INSTRUCTIONS} characters.")
            secrets["instructions"] = cleaned
        if any(is_emergency(text) for text in secrets.values()) and not (quote.payload or {}).get("emergency_seen"):
            # WA-FR-40: words typed inside a Flow are inbound words too. Say what to do, without echoing them, and
            # let a second tap through (this is a booking, the person says): the chat then alerts a human.
            self._save(quote, emergency_seen=True)
            numbers = " or ".join(n.strip() for n in settings.WHATSAPP_EMERGENCY_NUMBERS.split(",") if n.strip()) or "112"
            return self._render_details(quote, copy.t("en", "flow_emergency_notice", numbers=numbers))
        if any(is_emergency(text) for text in secrets.values()):
            self._save(quote, emergency_flag=True)
        self._save(quote, duration_hours=hours)
        self._seal(quote, **secrets)
        return self._render_address(quote)

    def _on_address(self, quote: Quote, data: Dict[str, Any]) -> FlowResult:
        street = _clean_text(data.get("street"), 150)
        area = _clean_text(data.get("area"), 100)
        if street is None or area is None:
            return self._render_address(quote, "Please enter the house number and street, and the area.")
        raw_city = data.get("city")
        city = rules.canonical_city(raw_city)
        if city is None and raw_city != "other" and not _clean_text(raw_city, 50):
            return self._render_address(quote, "Please choose a city.")
        payload = quote.payload or {}
        if not all(payload.get(k) for k in ("service", "date", "time", "duration_hours")) or not (
                payload.get("who") in ("self", "saved") and payload.get("recipient_id")
                or payload.get("who") == "new" and payload.get("new_recipient")):
            return self._render_who(quote, "Some details were missing. Let's go through them again.")
        if city is None:
            self._decline_area(quote, str(raw_city)[:50] if raw_city != "other" else None)
        else:
            self._complete(quote, street, area, city)
        return self._success()

    def _complete(self, quote: Quote, street: str, area: str, city: str) -> None:
        payload = dict(quote.payload or {})
        if payload.get("who") == "new":
            payload["recipient_id"] = str(self._create_recipient(payload.pop("new_recipient"), street, area, city))
        payload.update(address=f"{street}, {area}", city=city, state=rules.CITY_STATE.get(city, city))
        quote.payload = payload
        quote.status = "submitted"

    def _decline_area(self, quote: Quote, named_city: Optional[str]) -> None:
        """Outside the service area (WA-FR-15). Keep only what a waitlist needs (the service and the city the
        person named); the address, the condition and the recipient details are dropped, not parked."""
        quote.payload = {"service": (quote.payload or {}).get("service"), "city": named_city}
        quote.sensitive_encrypted = None
        quote.status = "out_of_area"

    def _create_recipient(self, details: Dict[str, Any], street: str, area: str, city: str) -> uuid.UUID:
        today = rules.today_lagos(self.now)
        recipient = CareRecipient(
            full_name=details["full_name"], gender=details["gender"], phone_e164=None,
            date_of_birth=date(today.year - int(details["age"]), 7, 1),  # an approximate age is stored as mid-year
            address=f"{street}, {area}", city=city, state=rules.CITY_STATE.get(city, city),
            notes_encrypted=encrypt(json.dumps({"relationship": details["relationship"],
                                                "emergency_contact_phone": details.get("emergency_phone")})),
            created_by_user_id=self._contact.user_id,
        )
        self.db.add(recipient)
        self.db.flush()
        # Logistics only: clinical access needs the recipient's own consent (WA-FR-53, WA-FR-54).
        self.db.add(CareRelationship(contact_user_id=self._contact.user_id, recipient_id=recipient.id,
                                     role="family", clinical_access=False))
        return recipient.id

    _HANDLERS: Dict[str, Callable] = {
        "WHO": _on_who, "RECIPIENT": _on_recipient, "SERVICE": _on_service,
        "WHEN": _on_when, "DETAILS": _on_details, "ADDRESS": _on_address,
    }
