"""Every word the bot says, in one place (WA-NFR-13: copy lives in a string table, not in the engine).

Rules (PRD 9.3): warm, short, plain English, one question per message, no emoji-only meaning. Nothing
here may carry health data, and nothing a user typed is ever interpolated into a reply: the only
variables are config values (numbers, a URL, a phone line).

Nigerian Pidgin arrives in Phase 2; ``STRINGS["pcm"]`` is where it will go and lookups fall back to English.
"""

from app.channels.whatsapp.messages import ButtonsMessage, ListMessage, ListRow, ListSection, TextMessage

STRINGS = {
    "en": {
        "consent_body": (
            "Welcome to Oakly. We send trusted nurses and caregivers to your home in Lagos.\n\n"
            "Before we start: to arrange care we need to use your health information. WhatsApp (Meta) "
            "processes these messages outside Nigeria. Read how we handle your data: {privacy_url}\n\n"
            "Do you agree to our Terms and Privacy Policy, and to us using your health information "
            "to deliver care?"
        ),
        "consent_accept": "I agree",
        "consent_decline": "No thanks",
        "consent_declined_body": (
            "No problem. Without your agreement we can't book visits or keep health information, "
            "so I can't take bookings here.{phone_line} You can talk to a person, or review the "
            "notice again whenever you like."
        ),
        "declined_phone_line": " You can also call us on {phone}.",
        "declined_talk": "Talk to a person",
        "declined_review": "Review again",
        "consent_thanks_menu": "Thank you. What would you like to do?",
        "menu_body": "What would you like to do?",
        "menu_button": "Open menu",
        "menu_section": "Oakly",
        "menu_book": "Book a visit",
        "menu_bookings": "My bookings",
        "menu_doctor": "Doctor home visit",
        "menu_human": "Talk to a person",
        "menu_help": "Help",
        "help_body": (
            "I can help you book a visit, check your bookings, or connect you with our team. "
            "Reply STOP to stop messages, or STOP DATA to withdraw your consent."
        ),
        "miss_body": "Sorry, I didn't catch that. Please choose from the menu.",
        "resend_body": "Sorry, we couldn't read your last message. Please send it again.",
        "miss_media_body": "I can only read text and buttons. Please choose from the menu.",
        "handover_body": "I'm connecting you with a member of our team. They will reply here as soon as they can.",
        "two_misses_body": (
            "I'm sorry I'm not getting it right. I'm connecting you with a member of our team, "
            "who will reply here as soon as they can."
        ),
        "booking_placeholder_body": (
            "Booking right here in chat is coming soon. I'm connecting you with our team, who can "
            "help you now and will reply here as soon as they can."
        ),
        "emergency_body": (
            "This may be an emergency. Please call {numbers} now, or go to the nearest hospital. "
            "Don't wait for a reply here. We have alerted our team and a person will contact you as "
            "soon as possible."
        ),
        "clinical_body": (
            "I can't give medical advice, diagnose, or suggest medicines. To see a doctor or nurse, "
            "choose Book a visit from the menu. If this is an emergency, call {numbers} now."
        ),
        "stop_body": "You have been unsubscribed from Oakly messages. Reply START to receive them again.",
        "stop_hint_body": "You have unsubscribed from Oakly messages. Reply START to receive them again.",
        "stop_data_body": (
            "We have received your request to withdraw your consent and our team will follow it up. "
            "Messages about a booking you have already paid for may continue until it is complete. "
            "Reply START if you change your mind."
        ),
        "start_body": "Welcome back.",
        # --- W2: booking in chat. Variables are config values, money, dates, codes and first names only. ---
        "expired_body": "It's been a while, so I've cleared that booking. What would you like to do?",
        "cancelled_body": "No problem, I've cancelled that. What would you like to do?",
        "flow_body": "Tap below to tell us about the visit. It takes about a minute.",
        "flow_cta": "Book a visit",
        "flow_resume_body": "Please finish the booking form with the button below, or reply CANCEL to stop.",
        "flow_stale_body": "That booking form has expired. Choose Book a visit to start again.",
        "flow_emergency_notice": (
            "If this is an emergency, please call {numbers} now or go to the nearest hospital. "
            "If you want a visit booked as well, tap Continue again."
        ),
        "no_services_body": (
            "Sorry, we can't take bookings in chat right now. I'm connecting you with our team, who will "
            "reply here as soon as they can."
        ),
        "location_body": (
            "Please share the location for this visit so we can find the address. If you'd rather not, "
            "reply SKIP and we'll confirm the address with you."
        ),
        "location_resume_body": (
            "Please tap the button to share the location, or reply SKIP and we'll confirm the address with you."
        ),
        "quote_intro_self": "Here's your quote:",
        "quote_intro_other": "Here's your quote for {name}:",
        "quote_service_line": "{service} \u00b7 {when}",
        "quote_base": "Base fee ({hours} h)",
        "quote_surge": "Busy-time pricing",
        "quote_discount": "Discount",
        "quote_service_fee": "Service fee",
        "quote_platform_fee": "Platform fee",
        "quote_total": "Total",
        "quote_cancellation": (
            "Free cancellation until {hours} hours before the visit. After that a {fee}% fee applies on the service "
            "fee, and {late_fee}% within {late} hours of the start or once your professional is on the way."
        ),
        "quote_confirm": "Confirm & pay",
        "quote_change": "Change",
        "quote_cancel": "Cancel",
        "quote_reminder": "Please choose an option below.",
        "quote_expired_body": (
            "That quote has expired, so I've cleared it. Choose Book a visit and we'll get you a fresh price."
        ),
        "quote_no_price_body": (
            "Sorry, I can't price that visit right now. I'm connecting you with our team, who will reply here "
            "as soon as they can."
        ),
        "quote_pending_body": (
            "Sorry, we couldn't price this yet. A person from our team will follow up with you here shortly."
        ),
        "pay_body": (
            "Tap below to pay securely with card, transfer or USSD. This link expires in {minutes} minutes."
        ),
        "pay_cta": "Pay now",
        "pay_preparing_body": "One moment, we're getting your payment link ready.",
        "pay_waiting_body": (
            "We haven't received your payment yet. You can still pay with the button below. This link expires "
            "in {minutes} minutes from when it was sent."
        ),
        "pay_failed_body": (
            "Sorry, we couldn't create your payment link just now. Nothing has been charged. "
            "Please choose Confirm & pay to try again."
        ),
        "pay_cancelled_body": "I've cancelled that booking. Nothing was charged.",
        "pay_cancel_paid_body": (
            "That booking is already paid, so I can't cancel it here. Our team can help: choose Talk to a person."
        ),
        "pay_check_failed_body": "We couldn't confirm your payment status right now. Please try again in a minute.",
        "waitlist_offer_body": (
            "We don't cover {city} yet. We're in Lagos and Abuja. Would you like us to message you when "
            "we arrive?"
        ),
        "waitlist_offer_body_unnamed": (
            "We don't cover that area yet. We're in Lagos and Abuja. Would you like us to message you when "
            "we arrive?"
        ),
        "waitlist_yes": "Yes, add me",
        "waitlist_no": "No thanks",
        "waitlist_thanks_body": "Thank you, we've added you to the list and will message you when we reach you.",
        "waitlist_declined_body": "No problem. What would you like to do?",
        # --- W3: my bookings and cancelling (WA-FR-38, WA-FR-39). Codes, dates, money and first names only. ---
        "bookings_none_body": "You don't have any bookings yet. Choose Book a visit to make one.",
        "bookings_list_body": "Here are your most recent bookings. Choose one to see the details.",
        "bookings_list_button": "View bookings",
        "bookings_section": "Your bookings",
        "bookings_row_description": "{service} \u00b7 {when} \u00b7 {status}",
        "bookings_not_found_body": "I couldn't find that booking.",
        "bookings_back": "My bookings",
        "bookings_cancel_button": "Cancel booking",
        "bookings_book_again": "Book again",
        "bookings_talk": "Talk to a person",
        "booking_detail_head": "Booking {code}",
        "booking_detail_for": "For: {name}",
        "booking_detail_for_self": "For: you",
        "booking_detail_when": "{service} \u00b7 {when}",
        "booking_detail_status": "Status: {status}",
        "booking_detail_total": "Total: {total}",
        "booking_detail_caregiver": "Professional: {name}",
        "booking_detail_code": "Visit code: {code}. Give it to the professional when they arrive, and to no one else.",
        "booking_detail_refund": "Refund: {amount}, being processed.",
        "status_awaiting_payment": "Awaiting payment",
        "status_finding": "Finding a professional",
        "status_assigned": "Professional assigned",
        "status_en_route": "On the way",
        "status_in_progress": "In progress",
        "status_completed": "Completed",
        "status_cancelled": "Cancelled",
        "status_declined": "Declined",
        "cancel_ask_unpaid": "Cancel booking {code}? You haven't paid yet, so nothing will be charged.",
        "cancel_ask_full": (
            "Cancel booking {code}? You'll get a full refund of {refund}."
        ),
        "cancel_ask_fee": (
            "Cancel booking {code}? The visit starts in less than {hours} hours, so a {percent}% fee on the "
            "service fee applies ({fee}). You'll get {refund} back."
        ),
        "cancel_ask_late_fee": (
            "Cancel booking {code}? The visit is within {hours} hours (or your professional is on the way), so a "
            "{percent}% fee on the service fee applies ({fee}). You'll get {refund} back."
        ),
        "cancel_terms_changed": "The cancellation terms have changed since you asked.",
        "cancel_yes": "Yes, cancel",
        "cancel_keep": "Keep booking",
        "cancel_done_unpaid": "I've cancelled booking {code}. Nothing was charged.",
        "cancel_done_refund": "Booking {code} is cancelled. We've started your refund of {refund}.",
        "cancel_done_fee": (
            "Booking {code} is cancelled. We've started your refund of {refund} (a fee of {fee} was kept)."
        ),
        "cancel_already_body": "Booking {code} is already cancelled.",
        "cancel_closed_body": "Booking {code} can't be cancelled because it is {status}.",
        "cancel_in_progress_body": (
            "That visit is already under way, so I can't cancel it here. I'm connecting you with our team, who "
            "will reply here as soon as they can."
        ),
        # --- a caregiver dropped out: the answers to the three buttons ---
        "dropout_wait_body": (
            "Okay, we'll keep looking for a replacement for booking {code} until the visit time. If we can't find "
            "one by then, our team will take over."
        ),
        "dropout_resched_body": (
            "Thanks. I'm connecting you with our team to find a new time for booking {code}. They'll reply here as "
            "soon as they can."
        ),
        "dropout_refund_body": "Booking {code} is cancelled and we've started your full refund of {refund}. Sorry about this.",
        "dropout_stale_body": "Booking {code} has already been dealt with, so there's nothing to choose now.",
        "dropout_not_booker_body": (
            "Only the person who made this booking can choose here. Choose Talk to a person and our team will help."
        ),
        "cancel_not_booker_body": (
            "Only the person who made this booking can cancel it. Choose Talk to a person and our team will help."
        ),
    },
}


def t(language: str, key: str, **values: str) -> str:
    table = STRINGS.get(language) or STRINGS["en"]
    text = table.get(key) or STRINGS["en"][key]
    return text.format(**values) if values else text


def consent_prompt(language: str, privacy_url: str) -> ButtonsMessage:
    return ButtonsMessage(
        t(language, "consent_body", privacy_url=privacy_url),
        (("consent:accept", t(language, "consent_accept")), ("consent:decline", t(language, "consent_decline"))),
    )


def declined_notice(language: str, support_phone: str) -> ButtonsMessage:
    phone_line = t(language, "declined_phone_line", phone=support_phone) if support_phone else ""
    return ButtonsMessage(
        t(language, "consent_declined_body", phone_line=phone_line),
        (("declined:human", t(language, "declined_talk")), ("consent:review", t(language, "declined_review"))),
    )


def menu(language: str, body_key: str = "menu_body") -> ListMessage:
    rows = tuple(ListRow(f"menu:{name}", t(language, f"menu_{name}"))
                 for name in ("book", "bookings", "doctor", "human", "help"))
    return ListMessage(t(language, body_key), t(language, "menu_button"),
                       (ListSection(t(language, "menu_section"), rows),))


def menu_with_body(language: str, body: str) -> ListMessage:
    base = menu(language)
    return ListMessage(body, base.button_label, base.sections)


def text(language: str, key: str, **values: str) -> TextMessage:
    return TextMessage(t(language, key, **values))
