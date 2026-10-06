"""W2: the booking Flow JSON (``app/channels/whatsapp/flows/booking.json``) is well formed and agrees with the endpoint.

This cannot replace uploading the Flow to Meta's builder (its validator is the authority), but it catches the mistakes
that would otherwise only show up there: a screen the endpoint does not know, a data field the screen declares but the
endpoint never sends, a payload key the endpoint does not read, a condition box with no length limit.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp.flows.service import VERSION
from app.tests.whatsapp_booking_helpers import (  # noqa: F401  (fixtures)
    Chat, FlowDriver, flow_token_from, onboarded, rules_now,
)

PATH = Path("app/channels/whatsapp/flows/booking.json")
FLOW = json.loads(PATH.read_text())
SCREENS = {s["id"]: s for s in FLOW["screens"]}

KNOWN_COMPONENTS = {"TextHeading", "TextCaption", "TextBody", "Form", "TextInput", "TextArea", "Dropdown",
                    "RadioButtonsGroup", "CalendarPicker", "Footer"}
INPUTS = {"TextInput", "TextArea", "Dropdown", "RadioButtonsGroup", "CalendarPicker"}
EXPECTED_FIELDS = {  # what each screen sends to the endpoint, in the names FlowService reads
    "WHO": {"who"},
    "RECIPIENT": {"full_name", "relationship", "age", "gender", "emergency_phone"},
    "SERVICE": {"service"},
    "WHEN": {"trigger", "date", "time"},
    "DETAILS": {"duration", "condition", "instructions"},
    "ADDRESS": {"street", "area", "city"},
}


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_the_file_is_valid_json_with_the_flow_json_envelope():
    major, _, _ = FLOW["version"].partition(".")
    assert int(major) >= 6  # the task asks for Flow JSON v6 or later
    assert FLOW["data_api_version"] == "3.0"  # the endpoint speaks data-api 3.0
    assert VERSION == FLOW["data_api_version"]
    assert set(FLOW) == {"version", "data_api_version", "routing_model", "screens"}


def test_screens_follow_the_prd_order_and_ids_are_unique():
    ids = [s["id"] for s in FLOW["screens"]]
    assert ids == ["WHO", "RECIPIENT", "SERVICE", "WHEN", "DETAILS", "ADDRESS"] and len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[A-Z_]+", i) for i in ids)  # Flow screen ids: letters and underscores only


def test_every_screen_has_a_title_and_a_single_column_layout():
    for screen in FLOW["screens"]:
        assert screen["title"] and len(screen["title"]) <= 30
        assert screen["layout"]["type"] == "SingleColumnLayout" and screen["layout"]["children"]


def test_the_routing_model_only_names_real_screens_and_every_screen_is_reachable_from_the_first():
    routing = FLOW["routing_model"]
    assert set(routing) == set(SCREENS)
    assert all(set(targets) <= set(SCREENS) for targets in routing.values())
    seen, queue = set(), ["WHO"]
    while queue:
        node = queue.pop()
        if node not in seen:
            seen.add(node)
            queue.extend(routing[node])
    assert seen == set(SCREENS)
    assert routing["ADDRESS"] == []  # the last screen ends with SUCCESS, which is not a screen of the Flow


def test_only_known_components_are_used():
    for screen in FLOW["screens"]:
        for node in walk(screen["layout"]["children"]):
            if "type" in node:
                assert node["type"] in KNOWN_COMPONENTS, (screen["id"], node["type"])


def test_every_data_reference_is_declared_on_the_screen_and_every_declaration_has_an_example():
    for screen in FLOW["screens"]:
        declared = screen["data"]
        for name, schema in declared.items():
            assert "__example__" in schema and "type" in schema, (screen["id"], name)
        text = json.dumps(screen["layout"])
        for ref in re.findall(r"\$\{data\.([a-z_]+)\}", text):
            assert ref in declared, (screen["id"], ref)
        for name in declared:
            assert f"${{data.{name}}}" in text, (screen["id"], name, "declared but never used")


def test_every_form_reference_is_an_input_on_the_same_screen():
    for screen in FLOW["screens"]:
        inputs = {n["name"] for n in walk(screen["layout"]["children"]) if n.get("type") in INPUTS}
        for ref in re.findall(r"\$\{form\.([a-z_]+)\}", json.dumps(screen["layout"])):
            assert ref in inputs, (screen["id"], ref)


def test_each_form_ends_in_a_footer_that_exchanges_data_with_the_endpoint():
    for screen in FLOW["screens"]:
        forms = [n for n in walk(screen["layout"]["children"]) if n.get("type") == "Form"]
        assert len(forms) == 1
        footer = forms[0]["children"][-1]
        assert footer["type"] == "Footer" and footer["on-click-action"]["name"] == "data_exchange"


def test_the_payload_each_screen_sends_is_exactly_what_the_endpoint_reads():
    for screen in FLOW["screens"]:
        footer = next(n for n in walk(screen["layout"]["children"]) if n.get("type") == "Footer")
        sent = set(footer["on-click-action"]["payload"])
        assert sent == EXPECTED_FIELDS[screen["id"]], screen["id"]
    picker = next(n for n in walk(SCREENS["WHEN"]["layout"]) if n.get("type") == "CalendarPicker")
    assert set(picker["on-select-action"]["payload"]) == {"trigger", "date"}
    assert picker["on-select-action"]["name"] == "data_exchange"


def test_required_inputs_are_marked_and_the_optional_ones_are_the_optional_ones():
    optional = {"emergency_phone", "instructions"}
    for screen in FLOW["screens"]:
        for node in walk(screen["layout"]["children"]):
            if node.get("type") in INPUTS:
                assert node["required"] is (node["name"] not in optional), (screen["id"], node["name"])


def test_the_condition_box_is_limited_to_500_characters_and_says_it_is_private():
    box = next(n for n in walk(SCREENS["DETAILS"]["layout"]) if n.get("name") == "condition")
    assert box["type"] == "TextArea" and box["max-length"] == 500
    assert "encrypted" in box["helper-text"] and "never sent in chat" in box["helper-text"]
    notes = next(n for n in walk(SCREENS["DETAILS"]["layout"]) if n.get("name") == "instructions")
    assert notes["max-length"] <= 300


def test_the_calendar_is_bounded_by_the_endpoint_not_hard_coded():
    picker = next(n for n in walk(SCREENS["WHEN"]["layout"]) if n.get("type") == "CalendarPicker")
    assert picker["min-date"] == "${data.min_date}" and picker["max-date"] == "${data.max_date}"
    assert picker["mode"] == "single"


def test_screens_after_the_first_refresh_on_back_so_the_endpoint_can_redraw_them():
    for screen_id, screen in SCREENS.items():
        assert screen.get("refresh_on_back", False) is (screen_id != "WHO"), screen_id


def test_no_price_city_or_service_is_baked_into_the_layout():
    layout = json.dumps([s["layout"] for s in FLOW["screens"]])
    assert "₦" not in layout and "\\u20a6" not in layout
    for hard_coded in ("Abuja", "Port Harcourt", "nurse", "caregiver", "physiotherapist"):
        assert hard_coded not in layout, hard_coded


# --- the file and the endpoint agree -------------------------------------------------------------------------
@pytest.fixture
def driver(db, client, fake_meta, booking_env):
    chat = Chat(db, fake_meta)
    onboarded(chat)
    chat.pick("menu:book")
    return FlowDriver(client, booking_env.public, flow_token_from(fake_meta.bodies))


def declared(screen_id):
    return set(SCREENS[screen_id]["data"])


RECIPIENT = {"full_name": "Folake Adeyemi", "relationship": "parent", "age": "67", "gender": "female",
             "emergency_phone": ""}


def test_every_screen_the_endpoint_returns_carries_exactly_the_data_the_screen_declares(driver):
    _, body = driver.call("INIT")
    assert body["screen"] == "WHO" and set(body["data"]) == declared("WHO")
    _, body = driver.exchange("WHO", {"who": "new"})
    assert body["screen"] == "RECIPIENT" and set(body["data"]) == declared("RECIPIENT")
    driver.exchange("RECIPIENT", RECIPIENT)
    _, body = driver.call("BACK", "SERVICE")
    assert body["screen"] == "SERVICE" and set(body["data"]) == declared("SERVICE")
    _, body = driver.exchange("SERVICE", {"service": "nurse"})
    assert body["screen"] == "WHEN" and set(body["data"]) == declared("WHEN")
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    _, body = driver.exchange("WHEN", {"trigger": "date_selected", "date": day})
    assert body["screen"] == "WHEN" and set(body["data"]) == declared("WHEN")
    _, body = driver.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    assert body["screen"] == "DETAILS" and set(body["data"]) == declared("DETAILS")
    _, body = driver.exchange("DETAILS", {"duration": "2", "condition": "needs help", "instructions": ""})
    assert body["screen"] == "ADDRESS" and set(body["data"]) == declared("ADDRESS")


def test_a_validation_error_redraws_the_same_screen_with_all_its_data(driver):
    driver.call("INIT")
    _, body = driver.exchange("WHO", {"who": "nonsense"})
    assert body["screen"] == "WHO" and set(body["data"]) == declared("WHO") and body["data"]["notice"]


def test_every_screen_transition_the_endpoint_makes_is_allowed_by_the_routing_model(driver):
    routing = FLOW["routing_model"]
    driver.call("INIT")
    _, body = driver.exchange("WHO", {"who": "new"})
    assert body["screen"] in routing["WHO"]
    _, body = driver.exchange("RECIPIENT", RECIPIENT)
    assert body["screen"] in routing["RECIPIENT"]
    _, body = driver.exchange("SERVICE", {"service": "nurse"})
    assert body["screen"] in routing["SERVICE"]
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    _, body = driver.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    assert body["screen"] in routing["WHEN"]
    _, body = driver.exchange("DETAILS", {"duration": "2", "condition": "x", "instructions": ""})
    assert body["screen"] in routing["DETAILS"]
    _, body = driver.exchange("ADDRESS", {"street": "1 Rd", "area": "Yaba", "city": "Lagos"})
    assert body["screen"] == "SUCCESS"  # leaves the Flow: not in the routing model, by design
