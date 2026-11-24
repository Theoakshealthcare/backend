"""GET /caregiver/chats: the caregiver's conversation list (one row per booking chat they may open)."""

from datetime import datetime, timedelta

from sqlalchemy import event

from app.db.models.booking import BookingNotification, BookingStatus, ChatMessage
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient, refresh_headers

URL = "/api/v1/caregiver/chats"
ROW_KEYS = {"booking_id", "patient_name", "service_type", "status", "scheduled_date", "last_message"}
MESSAGE_KEYS = {"text", "sent_at", "from_me"}
NOW = datetime(2026, 11, 20, 12, 0, 0)


def say(db, booking, sender, text, minutes_ago=0):
    m = ChatMessage(booking_id=booking.id, sender_id=sender.id, message=text, created_at=NOW - timedelta(minutes=minutes_ago))
    db.add(m)
    db.commit()
    return m


def get(client, user, **params):
    r = client.get(URL, headers=auth_headers(user), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def visit(db, patient, cg, age_minutes=0, **kw):
    """A booking of this caregiver whose creation time is `age_minutes` before NOW."""
    return make_booking(db, patient, cg, status=kw.pop("status", BookingStatus.ACCEPTED),
                        created_at=NOW - timedelta(minutes=age_minutes), **kw)


# --- auth -------------------------------------------------------------------------------


def test_requires_authentication_and_a_caregiver(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=refresh_headers(cg.user)).status_code == 401
    r = client.get(URL, headers=auth_headers(patient.user))
    assert r.status_code == 403
    assert r.json()["detail"] == "Access denied. Caregiver account required."


def test_no_caching(db, client):
    cg = make_caregiver(db)
    r = client.get(URL, headers=auth_headers(cg.user))
    assert r.headers["cache-control"] == "no-store"


# --- empty and shape -------------------------------------------------------------------


def test_a_caregiver_with_no_bookings_gets_an_empty_list(db, client):
    assert get(client, make_caregiver(db).user) == {"items": [], "next_before": None, "next_before_id": None}


def test_row_shape_with_and_without_a_message(db, client):
    cg, patient = make_caregiver(db), make_patient(db, full_name="Ada Okafor")
    quiet = visit(db, patient, cg, age_minutes=600, service_type="nurse")
    talked = visit(db, patient, cg, age_minutes=500, service_type="companion")
    say(db, talked, patient.user, "Hello nurse", minutes_ago=5)
    say(db, talked, cg.user, "On my way", minutes_ago=1)

    body = get(client, cg.user)
    assert [i["booking_id"] for i in body["items"]] == [str(talked.id), str(quiet.id)]
    first, second = body["items"]
    assert set(first) == ROW_KEYS and set(first["last_message"]) == MESSAGE_KEYS
    assert first["last_message"] == {"text": "On my way", "sent_at": (NOW - timedelta(minutes=1)).isoformat(), "from_me": True}
    assert first["service_type"] == "companion" and first["status"] == "accepted"
    assert first["scheduled_date"] == talked.scheduled_date.isoformat()
    assert second["last_message"] is None
    assert body["next_before"] is None and body["next_before_id"] is None


def test_from_me_is_false_for_the_patients_message(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    b = visit(db, patient, cg)
    say(db, b, patient.user, "Are you close?")
    assert get(client, cg.user)["items"][0]["last_message"]["from_me"] is False


def test_the_last_message_is_cut_to_140_characters_and_nothing_else_of_the_thread_leaks(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    b = visit(db, patient, cg)
    say(db, b, patient.user, "earlier secret 0801 234 5678", minutes_ago=9)
    say(db, b, patient.user, "x" * 140, minutes_ago=3)
    exact = get(client, cg.user)["items"][0]["last_message"]["text"]
    assert exact == "x" * 140
    say(db, b, patient.user, "y" * 141 + " 12 Hidden Street", minutes_ago=2)
    cut = get(client, cg.user)["items"][0]["last_message"]["text"]
    assert len(cut) == 140 and cut.endswith("…") and cut == "y" * 139 + "…"
    raw = client.get(URL, headers=auth_headers(cg.user)).text
    assert "earlier secret" not in raw and "Hidden Street" not in raw


def test_no_address_phone_or_other_patient_data_in_the_response(db, client):
    cg, patient = make_caregiver(db), make_patient(db, phone_number="+2340000999111")
    visit(db, patient, cg, patient_address="7 Private Close")
    raw = client.get(URL, headers=auth_headers(cg.user)).text
    assert "Private Close" not in raw and "+2340000999111" not in raw and "Lagos" not in raw


def test_there_is_no_unread_count_because_nothing_records_reads(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    b = visit(db, patient, cg)
    say(db, b, patient.user, "hi")
    row = get(client, cg.user)["items"][0]
    assert "unread_count" not in row and "unread_count" not in row["last_message"]


# --- who sees what ----------------------------------------------------------------------


def test_only_this_caregivers_conversations(db, client):
    cg, other, patient = make_caregiver(db), make_caregiver(db), make_patient(db)
    mine, theirs = visit(db, patient, cg), visit(db, patient, other)
    say(db, theirs, patient.user, "meant for the other caregiver")
    ids = [i["booking_id"] for i in get(client, cg.user)["items"]]
    assert ids == [str(mine.id)]
    assert [i["booking_id"] for i in get(client, other.user)["items"]] == [str(theirs.id)]
    assert "meant for the other" not in client.get(URL, headers=auth_headers(cg.user)).text


def test_an_offer_you_do_not_hold_is_not_a_conversation(db, client):
    """Chat opens only for the assigned caregiver, so a booking you were merely offered (or one you dropped) is not listed."""
    cg, patient = make_caregiver(db), make_patient(db, full_name="Ada Okafor")
    offered = make_booking(db, patient, None, status=BookingStatus.PENDING)
    db.add(BookingNotification(booking_id=offered.id, caregiver_id=cg.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    assert get(client, cg.user)["items"] == []
    offered.caregiver_id = cg.id  # accepted: now it is theirs and the chat opens
    offered.status = BookingStatus.ACCEPTED
    db.commit()
    assert [i["booking_id"] for i in get(client, cg.user)["items"]] == [str(offered.id)]


def test_the_patient_name_is_the_full_name_because_only_assigned_bookings_are_listed(db, client):
    cg = make_caregiver(db)
    visit(db, make_patient(db, full_name="Ada Okafor"), cg)
    assert get(client, cg.user)["items"][0]["patient_name"] == "Ada Okafor"


def test_every_status_the_chat_route_allows_is_listed(db, client):
    """The chat routes check only the assignment, so a finished or cancelled visit keeps its conversation."""
    cg, patient = make_caregiver(db), make_patient(db)
    for n, st in enumerate((BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.IN_PROGRESS)):
        visit(db, patient, cg, age_minutes=n, status=st)
    assert {i["status"] for i in get(client, cg.user)["items"]} == {"completed", "cancelled", "in_progress"}


def test_a_listed_conversation_opens_through_the_chat_route(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    b = visit(db, patient, cg)
    say(db, b, patient.user, "hello")
    row = get(client, cg.user)["items"][0]
    r = client.get(f"/api/v1/booking/chat/{row['booking_id']}/messages", headers=auth_headers(cg.user))
    assert r.status_code == 200 and [m["message"] for m in r.json()] == ["hello"]


# --- ordering and pagination --------------------------------------------------------------


def test_ordered_by_last_activity_then_booking_creation(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    old_but_chatting = visit(db, patient, cg, age_minutes=5000)
    newest_quiet = visit(db, patient, cg, age_minutes=10)
    older_quiet = visit(db, patient, cg, age_minutes=100)
    chatted_recently = visit(db, patient, cg, age_minutes=4000)
    say(db, old_but_chatting, patient.user, "a", minutes_ago=50)
    say(db, chatted_recently, patient.user, "b", minutes_ago=2)
    ids = [i["booking_id"] for i in get(client, cg.user)["items"]]
    assert ids == [str(b.id) for b in (chatted_recently, newest_quiet, old_but_chatting, older_quiet)]


def test_pages_chain_without_gaps_or_repeats_even_on_equal_activity(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    bookings = [visit(db, patient, cg, age_minutes=0) for _ in range(5)]  # identical activity time: the id breaks the tie
    seen, params = [], {"limit": 2}
    for _ in range(5):
        body = get(client, cg.user, **params)
        seen += [i["booking_id"] for i in body["items"]]
        if body["next_before"] is None:
            assert body["next_before_id"] is None
            break
        params = {"limit": 2, "before": body["next_before"], "before_id": body["next_before_id"]}
    assert sorted(seen) == sorted(str(b.id) for b in bookings) and len(seen) == 5 and len(set(seen)) == 5


def test_limit_is_validated(db, client):
    cg = make_caregiver(db)
    for bad in (0, 51, "x"):
        assert client.get(URL, headers=auth_headers(cg.user), params={"limit": bad}).status_code == 422


def test_one_query_for_a_page_however_many_conversations(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    for n in range(6):
        b = visit(db, patient, cg, age_minutes=n)
        say(db, b, patient.user, f"m{n}", minutes_ago=n)
    headers = auth_headers(cg.user)
    client.get(URL, headers=headers)  # warm up anything cached per process
    statements = []
    engine = db.get_bind()

    def count(conn, cursor, statement, *a):
        if "chat_messages" in statement or "FROM bookings" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert client.get(URL, headers=headers).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 1, statements
