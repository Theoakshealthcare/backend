"""Capture the patient FallGuard v2.0 flow against a RUNNING Oakly API and write every request/response to
docs/fe-captures/patient-fallguard/*.json. docs/fe-integration-guide-patient-fallguard.md is copied from these files,
never from schemas.

    # a scratch env file (generated values, never committed), a throwaway Postgres migrated to head, the API from this checkout
    DATABASE_URL=postgresql://oakly:oakly@127.0.0.1:55432/oakly OAKLY_ENV_FILE=/path/to/scratch.env poetry run alembic upgrade head
    OAKLY_ENV_FILE=/path/to/scratch.env poetry run uvicorn app.main:app --port 8000
    poetry run python -m scripts.capture_patient_fallguard --env-file /path/to/scratch.env \\
        --db-url postgresql://oakly:oakly@127.0.0.1:55432/oakly

What is real and what is not
- Every API call is a real HTTP call to the running API: accounts are registered and verified through the endpoints. The
  questionnaire needs no seeding: v2.0 is versioned JSON served by the API.
- The only direct database write is the admin user (there is no API for the first admin). Labelled in the captures.
- Not captured live: a 503 ``SCORING_UNAVAILABLE`` (it needs a fault the running app cannot be asked to produce; the shape
  is asserted in app/tests/test_fallguard_submit.py), and the optional AI summary (off by default, and the scratch stack has no
  Gemini key).
- ``assessment_date`` is the server's own "today" in Africa/Lagos, so the age in a capture depends on the day it was taken.

Placeholder data only (example.com emails). JWTs and passwords are replaced by placeholders in the files.
"""

import argparse
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, read_env, register_verify_login, seed_admin_session,
)


ZERO_B = [{"question_id": f"B{n}", "answer": 0} for n in (2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 15, 16, 18, 19)] + [
    {"question_id": q, "answer": ["NONE"]} for q in ("B8", "B14", "B17")]
PART_A_ZERO = [{"question_id": q, "answer": 0} for q in ("A1", "A4", "A5", "A6")]
# Appendix A of the handover PDF: its one worked example, as the app sends it.
APPENDIX_A = [
    {"question_id": "A1", "answer": 1}, {"question_id": "A2", "answer": 0}, {"question_id": "A3", "answer": 0},
    {"question_id": "A4", "answer": 0}, {"question_id": "A5", "answer": 1}, {"question_id": "A6", "answer": 2},
    {"question_id": "A7", "answer": 0},
    {"question_id": "B2", "answer": 1}, {"question_id": "B3", "answer": 1}, {"question_id": "B4", "answer": 1},
    {"question_id": "B5", "answer": 2}, {"question_id": "B6", "answer": 1}, {"question_id": "B7", "answer": 2},
    {"question_id": "B8", "answer": ["DIABETES", "ARTHRITIS"]}, {"question_id": "B9", "answer": 1},
    {"question_id": "B10", "answer": 0}, {"question_id": "B11", "answer": 1}, {"question_id": "B12", "answer": 0},
    {"question_id": "B13", "answer": 1}, {"question_id": "B14", "answer": ["CARDIO_BP_DIURETIC"]},
    {"question_id": "B15", "answer": 1}, {"question_id": "B16", "answer": 0},
    {"question_id": "B17", "answer": ["LOOSE_RUGS", "NO_BACKUP_LIGHT"]}, {"question_id": "B18", "answer": 1},
    {"question_id": "B19", "answer": 0},
    {"question_id": "C1", "answer": 1}, {"question_id": "C2", "answer": "SOMETIMES"},
    {"question_id": "C5", "answer": 0}, {"question_id": "C6", "answer": 0},
]


def run(args) -> None:
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    env = read_env(args.env_file)
    rec = Recorder(args.base_url, out)
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    ctx = Ctx(rec, db, env, None, tag, FakePaystack())  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731
    api = "/api/v1/fallguard"

    rec.quiet = True
    admin = seed_admin_session(rec, db, env, mail("admin"))
    tp = register_verify_login(ctx, "p", mail("patient"), "Placeholder Patient", "patient")["access_token"]
    tc = register_verify_login(ctx, "c", mail("caregiver"), "Placeholder Caregiver", "caregiver")["access_token"]
    rec.quiet = False

    def submit(label, answers, *, dob="1960-06-15", expect=200, note=None, **extra):
        body = {"answers": answers, **({"date_of_birth": dob} if dob else {}), **extra}
        return rec.call(label, "POST", f"{api}/submit", token=tp, json_body=body, expect=expect, note=note)

    questions = rec.call("p-questions", "GET", f"{api}/questions", token=tp,
                         note="The v2.0 questionnaire as a list, in order (34 items, A1 to C8).").json()
    rec.call("p-config", "GET", f"{api}/config", token=tp, params={"lang": "en"},
             note="Versions, items, the red-flag items, the fixed messages and the emergency number.")
    rec.call("c-questions-as-caregiver", "GET", f"{api}/questions", token=tc, expect=403)
    rec.call("none-questions-no-token", "GET", f"{api}/questions", expect=401)
    rec.call("p-history-empty", "GET", f"{api}/history", token=tp)
    assert len(questions) == 34

    submit("p-submit-appendix-a", APPENDIX_A, dob="1946-05-20",
           note="The handover PDF's Appendix A example (born 1946-05-20). The date of birth is in the body; the response "
                "`result` is the scoring output. The assessment date is the server's today, so `age` here depends on the "
                "day of capture (the PDF's own example is dated 2026-11-10 and is reproduced exactly by the unit tests).")
    submit("p-submit-low", PART_A_ZERO + ZERO_B, note="Every best answer, born 1960-06-15: a LOW result.")
    submit("p-submit-gate-raises-tier", [{"question_id": "A1", "answer": 0}, {"question_id": "A4", "answer": 1},
                                         {"question_id": "A5", "answer": 0}, {"question_id": "A6", "answer": 0}, *ZERO_B],
           note="A4 = 1 (fainted or nearly): the gate tier is HIGH even though the score tier is LOW, so the final tier is "
                "HIGH, and `prompt_attention` is true.")
    submit("p-submit-incomplete", PART_A_ZERO,
           note="Part A only: all 18 Part B items are unknown. The result is provisional (`incomplete` and `provisional` "
                "true) and unknown items score 1, never 0.")
    submit("p-submit-under-60", PART_A_ZERO + ZERO_B, dob="1990-01-01",
           note="Under 60: no score and no tier. Only `urgent` is still decided.")
    submit("p-submit-urgent", PART_A_ZERO + ZERO_B + [{"question_id": "R3", "answer": True}],
           note="Any of R2 to R7 answered yes: `urgent` true. The tier is unchanged.")
    submit("p-submit-r1-no-blood-thinner", PART_A_ZERO + ZERO_B + [
        {"question_id": "C5", "answer": 0}, {"question_id": "R1", "answer": True}],
        note="R1 yes with C5 = 0 (no blood thinners): not urgent, but `prompt_attention` is true.")
    submit("p-submit-r1-blood-thinner", PART_A_ZERO + ZERO_B + [
        {"question_id": "C5", "answer": 1}, {"question_id": "R1", "answer": True}],
        note="R1 yes with C5 = 1 (on blood thinners): urgent, and R1 is the item that triggered it.")
    submit("p-submit-ignored-score", [{**a, "score": 99} for a in PART_A_ZERO + ZERO_B],
           note="`score` is accepted and ignored: this is the same result as p-submit-low.")
    rec.call("p-submit-empty", "POST", f"{api}/submit", token=tp, expect=422, json_body={"answers": []})
    submit("p-submit-duplicate-question", [*PART_A_ZERO, {"question_id": "A1", "answer": 2}], expect=422)
    submit("p-submit-unknown-question", [*PART_A_ZERO, {"question_id": str(uuid.uuid4()), "answer": "No"}], expect=422,
           note="A v1 (24-question) client sending its UUID question ids lands here.")
    submit("p-submit-invalid-option", [{"question_id": "A1", "answer": 7}, *PART_A_ZERO[1:]], expect=422)
    submit("p-submit-missing-required", [{"question_id": "A1", "answer": 1}, {"question_id": "A4", "answer": 0}], expect=422,
           note="Here A5 is the first missing required item (A1, A4, A5, A6 are checked first, then A2, A3, A7 when A1 is 1 or "
                "more). The library names the FIRST missing item only.")
    submit("p-submit-red-flag-not-boolean", [*PART_A_ZERO, {"question_id": "R2", "answer": "yes"}], expect=422,
           note="A red-flag answer must be the JSON boolean true or false.")
    submit("p-submit-part-c-invalid", [*PART_A_ZERO, {"question_id": "C2", "answer": "WEEKLY"}], expect=422,
           note="Part C answers are checked against the questionnaire's codes.")
    submit("p-submit-no-date-of-birth", PART_A_ZERO + ZERO_B, dob=None, expect=422,
           note="This patient's profile has no date of birth and the body has none.")
    submit("p-submit-future-date-of-birth", PART_A_ZERO + ZERO_B, dob="2999-01-01", expect=422)
    rec.call("p-submit-bad-body", "POST", f"{api}/submit", token=tp, expect=422, json_body={"answers": [{"answer": 0}]},
             note="A malformed body is still FastAPI's own 422 (detail is a LIST here, an object for the codes above).")
    rec.call("c-submit-as-caregiver", "POST", f"{api}/submit", token=tc, expect=403, json_body={"answers": PART_A_ZERO})
    rec.call("none-submit-no-token", "POST", f"{api}/submit", expect=401, json_body={"answers": PART_A_ZERO})
    rec.call("p-history", "GET", f"{api}/history", token=tp, note="Newest first: only the accepted submissions are here.")

    rec.quiet = True  # top up to the limit without capturing, then capture the refusal
    accepted = len(rec.http.get(f"{api}/history", headers={"Authorization": f"Bearer {tp}"}).json())
    for _ in range(10 - accepted):
        submit("fill", PART_A_ZERO + ZERO_B)
    rec.quiet = False
    submit("p-submit-rate-limited", PART_A_ZERO + ZERO_B, expect=429,
           note="The 11th submission within an hour.")

    rec.call("a-admin-questions", "GET", f"{api}/admin/questions", token=admin,
             note="The admin list is the same read-only v2.0 questionnaire. (The admin token is a real sign-in; the admin "
                  "user row was inserted directly because there is no API for the first admin.)")
    rec.call("a-admin-create-question-retired", "POST", f"{api}/admin/question", token=admin, expect=410, json_body={})
    rec.call("a-admin-update-question-retired", "PUT", f"{api}/admin/question/{uuid.uuid4()}", token=admin, expect=410,
             json_body={})
    rec.call("a-admin-delete-question-retired", "DELETE", f"{api}/admin/question/{uuid.uuid4()}", token=admin, expect=410)

    (out / "000-index.json").write_text(json.dumps({
        "generated_by": "scripts/capture_patient_fallguard.py", "run_tag": tag,
        "captured_at": datetime.utcnow().isoformat() + "Z", "captures": rec.index}, indent=2) + "\n")
    print(f"wrote {len(rec.index)} captures to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:55432/oakly")
    p.add_argument("--env-file", required=True, help="the env file the API was started with")
    p.add_argument("--out", default="docs/fe-captures/patient-fallguard")
    run(p.parse_args())


if __name__ == "__main__":
    main()
