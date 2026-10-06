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
