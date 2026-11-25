"""0031_care_templates_docs_space: the template text edits, the Elderly flag and the documentation table, both directions.

Runs on a scratch database, like the other migration round trips.
"""

import json

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0031_care_templates_docs_space", "0030_bank_name_review"
FAMILY_LINE = "Contact the family and inform the family's doctor"


def _templates(eng):
    """key -> (version, caregiver_can_use or None before 0031, body)."""
    flag = "caregiver_can_use" if "caregiver_can_use" in _before_columns(eng) else "NULL"
    with eng.connect() as conn:
        rows = conn.execute(text(f"SELECT key, version, {flag} AS flag, body FROM care_plan_templates ORDER BY key")).all()
    return {r.key: (r.version, r.flag, r.body if isinstance(r.body, dict) else json.loads(r.body)) for r in rows}


def _before_columns(eng):
    return {c["name"] for c in inspect(eng).get_columns("care_plan_templates")}


def test_0031_follows_its_predecessor_and_is_the_only_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == PREVIOUS
    assert script.get_heads() == [THIS]


def test_upgrade_rewrites_four_templates_and_downgrade_puts_the_old_text_back(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    before = _templates(scratch)
    assert "caregiver_can_use" not in _before_columns(scratch)
    assert {version for version, _, _ in before.values()} == {1}

    _run(scratch, command.upgrade, THIS)
    after = _templates(scratch)
    assert "visit_documentation" in inspect(scratch).get_table_names()
    assert {k: (v, flag) for k, (v, flag, _) in after.items()} == {
        "cardiac_rehabilitation": (2, False), "diabetes_management": (2, False), "elderly_daily_living_dementia": (1, True),
        "post_surgery_recovery": (2, False), "stroke_recovery": (2, False)}
    assert after["elderly_daily_living_dementia"][2] == before["elderly_daily_living_dementia"][2]  # text untouched
    for key in ("stroke_recovery", "diabetes_management", "post_surgery_recovery", "cardiac_rehabilitation"):
        assert FAMILY_LINE in after[key][2]["special_instructions"]
        assert FAMILY_LINE not in before[key][2]["special_instructions"]
    titles = [t["title"] for t in after["post_surgery_recovery"][2]["tasks"]]
    assert titles.count("Ask about pain and record it") == 2

    _run(scratch, command.downgrade, PREVIOUS)
    assert "visit_documentation" not in inspect(scratch).get_table_names()
    assert "caregiver_can_use" not in _before_columns(scratch)
    assert {k: (v, body) for k, (v, _, body) in _templates(scratch).items()} == {k: (v, body) for k, (v, _, body) in before.items()}

    _run(scratch, command.upgrade, THIS)  # and forward again
    assert _templates(scratch)["post_surgery_recovery"][0] == 2
