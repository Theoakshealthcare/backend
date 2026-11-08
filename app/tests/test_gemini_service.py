"""GeminiService on the google-genai SDK, with the client mocked (the network guard blocks real calls)."""

import asyncio
import warnings
from types import SimpleNamespace

import logging

import pytest

from app.core.config import settings
from app.services.gemini import FALLBACK_MODELS, GeminiService


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
    assert model == settings.GEMINI_MODEL and "120/80" in prompt


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


class ApiError(Exception):
    """Shaped like google.genai.errors.APIError: the HTTP status is ``code``."""

    def __init__(self, code, message="SECRET prompt text echoed by the server"):
        super().__init__(message)
        self.code = code


class ScriptedClient(FakeClient):
    """Fails with the scripted error for each model named in ``failing``; answers OK for the others."""

    def __init__(self, failing, text='{"summary": "s", "recommendations": []}'):
        super().__init__(text)
        self._failing = failing

    async def _generate(self, *, model, contents):
        self.calls.append((model, contents))
        if model in self._failing:
            raise self._failing[model]
        return SimpleNamespace(text=self._text)


def scripted(failing) -> tuple[GeminiService, ScriptedClient]:
    svc, fake = GeminiService(), ScriptedClient(failing)
    svc._client = fake
    return svc, fake


def test_the_model_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-test-model")
    svc, fake = service('{"summary": "s", "recommendations": []}')
    run(svc.analyze_fallguard_assessment([], 0))
    assert [m for m, _ in fake.calls] == ["gemini-test-model"]


def test_default_model_is_not_the_retired_one():
    assert settings.GEMINI_MODEL == "gemini-3.5-flash"
    assert "gemini-2.5-flash" not in (settings.GEMINI_MODEL, *FALLBACK_MODELS)


def test_a_404_on_the_first_model_falls_back_to_the_next(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-primary")
    svc, fake = scripted({"gemini-primary": ApiError(404)})
    out = run(svc.analyze_fallguard_assessment([], 1))
    assert out == {"summary": "s", "recommendations": []}
    assert [m for m, _ in fake.calls] == ["gemini-primary", FALLBACK_MODELS[0]]


def test_a_non_404_error_does_not_try_the_fallback(monkeypatch, caplog):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-primary")
    svc, fake = scripted({"gemini-primary": ApiError(429)})
    out = run(svc.analyze_fallguard_assessment([], 1))
    assert out == {"summary": "Unable to analyze", "recommendations": []}
    assert [m for m, _ in fake.calls] == ["gemini-primary"]


def test_total_failure_returns_the_no_advice_result_and_logs_class_and_status_only(monkeypatch, caplog):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-primary")
    svc, fake = scripted({"gemini-primary": ApiError(404), FALLBACK_MODELS[0]: ApiError(404)})
    with caplog.at_level(logging.WARNING, logger="app.services.gemini"):
        out = run(svc.analyze_fallguard_assessment([{"answer": "PRIVATE-ANSWER"}], 9))
    assert out == {"summary": "Unable to analyze", "recommendations": []}
    assert len(fake.calls) == 2
    text = caplog.text
    assert "ApiError" in text and "status=404" in text
    assert "SECRET" not in text and "PRIVATE-ANSWER" not in text and "Total Score" not in text
    assert all(r.exc_info is None for r in caplog.records)  # no traceback (it would carry the exception text)


def test_the_primary_model_is_not_tried_twice_when_it_is_the_fallback(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", FALLBACK_MODELS[0])
    svc, fake = scripted({FALLBACK_MODELS[0]: ApiError(404)})
    run(svc.analyze_vitals([]))
    assert [m for m, _ in fake.calls] == [FALLBACK_MODELS[0]]
