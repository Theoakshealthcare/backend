"""Capture the patient FallGuard flow against a RUNNING Oakly API and write every request/response to
docs/fe-captures/patient-fallguard/*.json. docs/fe-integration-guide-patient-fallguard.md is copied from these files,
never from schemas.

    # same stack as capture_patient_flow.py: a scratch env file, a throwaway Postgres migrated to head, the API from this checkout
    poetry run python -m scripts.capture_patient_fallguard --env-file /path/to/scratch.env \\
        --db-url postgresql://oakly:oakly@127.0.0.1:55432/oakly

What is real and what is not
- Every API call is a real HTTP call to the running API: accounts are registered and verified through the endpoints and the
  questionnaire is created through the admin endpoint, from the real seed data (app/scripts/seed_fallguard_questions.py).
- The only direct database write is the admin user (there is no API for the first admin). Labelled in the captures.
- The AI summary is NOT exercised: the scratch stack has no Gemini key, so ``ai_summary`` is the service's own fallback
  ("Unable to analyze") and ``ai_recommendations`` is []. What the summary says when Gemini answers is not captured.

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

from app.scripts.seed_fallguard_questions import QUESTIONS  # noqa: E402


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
    for q in QUESTIONS:
        rec.call("admin-create-question", "POST", f"{api}/admin/question", token=admin, json_body={**q, "is_active": True})
    rec.quiet = False

    questions = rec.call("p-questions", "GET", f"{api}/questions", token=tp,
                         note="The seeded questionnaire as the patient app receives it (24 questions, in order).").json()
    rec.call("c-questions-as-caregiver", "GET", f"{api}/questions", token=tc, expect=403)
    rec.call("none-questions-no-token", "GET", f"{api}/questions", expect=401)
    rec.call("p-history-empty", "GET", f"{api}/history", token=tp)

    def answer(q, index, score="omit"):
        a = {"question_id": q["id"], "answer": q["options"][index]["text"]}
        if score != "omit":
            a["score"] = score
        return a

    first = questions[0]
    rec.call("p-submit-full", "POST", f"{api}/submit", token=tp,
             json_body={"answers": [answer(q, len(q["options"]) - 1, score=q["options"][-1]["score"]) for q in questions]},
             note="All 24 questions, each with the LAST option and the score the older app sends. The total is the server's "
                  "sum of the stored option scores.")
    rec.call("p-submit-without-score", "POST", f"{api}/submit", token=tp, json_body={"answers": [answer(first, 1)]},
             note="`score` may be left out: the server never reads it.")
    rec.call("p-submit-tampered-score", "POST", f"{api}/submit", token=tp, json_body={"answers": [answer(first, 0, score=99)]},
             note="A score the client made up (99) is ignored: the answer is stored and counted with the option's own score.")
    rec.call("p-submit-empty", "POST", f"{api}/submit", token=tp, expect=422, json_body={"answers": []})
    rec.call("p-submit-duplicate-question", "POST", f"{api}/submit", token=tp, expect=422,
             json_body={"answers": [answer(first, 0), answer(first, 1)]})
    rec.call("p-submit-unknown-question", "POST", f"{api}/submit", token=tp, expect=422,
             json_body={"answers": [{"question_id": str(uuid.uuid4()), "answer": "No"}]},
             note="This used to be a 500 Internal Server Error.")
    rec.call("p-submit-invalid-option", "POST", f"{api}/submit", token=tp, expect=422,
             json_body={"answers": [{"question_id": first["id"], "answer": "Placeholder: not a choice"}]})
    rec.call("p-submit-bad-body", "POST", f"{api}/submit", token=tp, expect=422, json_body={"answers": [{"answer": "No"}]},
             note="A malformed body is still FastAPI's own 422 (detail is a LIST here, an object for the four codes above).")
    rec.call("c-submit-as-caregiver", "POST", f"{api}/submit", token=tc, expect=403, json_body={"answers": [answer(first, 0)]})
    rec.call("none-submit-no-token", "POST", f"{api}/submit", expect=401, json_body={"answers": [answer(first, 0)]})
    rec.call("p-history", "GET", f"{api}/history", token=tp, note="Newest first. Only the three accepted submissions are here.")

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
