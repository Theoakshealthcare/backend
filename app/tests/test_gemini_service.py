"""GeminiService on the google-genai SDK, with the client mocked (the network guard blocks real calls)."""

import asyncio
import warnings
from types import SimpleNamespace

from app.services.gemini import MODEL, GeminiService


class FakeClient:
    """Stands in for genai.Client: records calls, returns canned text or raises."""

    def __init__(self, text=None, error=None):
        self.calls = []
        self._text, self._error = text, error
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents):
        self.calls.append((model, contents))
        if self._error:
            raise self._error
        return SimpleNamespace(text=self._text)


def service(text=None, error=None) -> tuple[GeminiService, FakeClient]:
    svc, fake = GeminiService(), FakeClient(text, error)
    svc._client = fake
    return svc, fake


def run(coro):
    return asyncio.run(coro)


def test_importing_the_service_needs_no_api_key_or_network():
    assert GeminiService()._client is None


def test_no_deprecation_warning_from_the_sdk_import():
    import importlib
    import sys

    sys.modules.pop("app.services.gemini", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("app.services.gemini")
    assert not [w for w in caught if "google.generativeai" in str(w.message) or "generativeai" in str(w.filename)]


def test_analyze_vitals_parses_fenced_json_and_sends_the_vitals():
    body = '```json\n{"summary": "stable", "concerns": [], "recommendations": ["rest"], "key_vitals": ["bp"]}\n```'
    svc, fake = service(body)
    out = run(svc.analyze_vitals([{"type": "bp", "value": "120/80"}]))
    assert out == {"summary": "stable", "concerns": [], "recommendations": ["rest"], "key_vitals": ["bp"]}
    model, prompt = fake.calls[0]
    assert model == MODEL and "120/80" in prompt


def test_analyze_vitals_falls_back_on_error():
    svc, _ = service(error=RuntimeError("boom"))
    assert run(svc.analyze_vitals([])) == {
        "summary": "Unable to analyze vitals.", "concerns": [], "recommendations": [], "key_vitals": []}


def test_fallguard_summary_and_fallback():
    svc, fake = service('{"summary": "moderate risk", "recommendations": ["rails"]}')
    assert run(svc.analyze_fallguard_assessment([{"q": 1}], 7)) == {
        "summary": "moderate risk", "recommendations": ["rails"]}
    assert "Total Score: 7" in fake.calls[0][1]
    bad, _ = service("not json")
    assert run(bad.analyze_fallguard_assessment([], 0)) == {"summary": "Unable to analyze", "recommendations": []}


def test_booking_notes_plain_json_and_fallback():
    payload = ('{"summary": "ok", "observations": [], "recommendations": [], "keywords": ["k"],'
               ' "urgency_level": "low", "follow_up_required": false}')
    svc, fake = service(payload)
    out = run(svc.transcribe_and_analyze_audio("patient rested", {"service_type": "nurse"}))
    assert out["summary"] == "ok" and out["keywords"] == ["k"]
    assert "patient rested" in fake.calls[0][1]
    failing, _ = service(error=RuntimeError("x"))
    assert run(failing.transcribe_and_analyze_audio("t", {}))["summary"] == "Unable to analyze transcript"


def test_recommendations_and_patterns():
    svc, _ = service("```json\nuse a walker\n```")
    assert run(svc.generate_care_recommendations([{"summary": "s"}], "frail")) == "use a walker"
    err, _ = service(error=RuntimeError("x"))
    assert run(err.generate_care_recommendations([], "c")) == "Unable to generate recommendations at this time."
    pat, _ = service('{"improvement_trends": ["a"], "concerning_patterns": [], "care_effectiveness": "improving", "recommendations": []}')
    assert run(pat.analyze_care_patterns([{"content": "c"}]))["care_effectiveness"] == "improving"
