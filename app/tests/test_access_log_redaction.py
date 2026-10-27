"""Uvicorn's access log must never carry the webhook's ``hub.verify_token``.

Meta's subscription handshake is a GET whose query string holds the shared verify token; uvicorn logs the
full request path, so the secret would land in the logs (and anything that ships them).
"""

import logging

import pytest
from uvicorn.logging import AccessFormatter

import app.main  # noqa: F401  (installs the filter on import, as in production)
from app.core.log_filters import REDACTED, AccessLogRedactionFilter, install_access_log_redaction

WEBHOOK = "/api/v1/whatsapp/webhook"
SECRET = "super-secret-verify-token"


def access_record(path: str, logger_name: str = "uvicorn.access") -> logging.LogRecord:
    """A record shaped exactly like the one uvicorn's protocol layer emits."""
    return logging.LogRecord(logger_name, logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:50000", "GET", path, "1.1", 200), None)


def rendered(record: logging.LogRecord) -> str:
    return AccessFormatter("%(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                           use_colors=False).format(record)


def test_the_verify_token_is_redacted_from_the_webhook_access_line():
    record = access_record(f"{WEBHOOK}?hub.mode=subscribe&hub.verify_token={SECRET}&hub.challenge=1158201444")
    assert AccessLogRedactionFilter().filter(record) is True  # still logged, just without the secret
    line = rendered(record)
    assert SECRET not in line
    assert f"hub.verify_token={REDACTED}" in line
    assert "hub.mode=subscribe" in line and "hub.challenge=1158201444" in line  # the rest stays for debugging


@pytest.mark.parametrize("query", [
    f"hub.verify_token={SECRET}",
    f"hub.mode=subscribe&hub.verify_token={SECRET}",
    f"hub.verify_token={SECRET}&hub.mode=subscribe",
    f"hub%2Everify_token={SECRET}&hub.mode=subscribe",   # percent-encoded key
    f"hub.verify_token=&hub.mode=subscribe",
    f"hub.verify_token={SECRET}&hub.verify_token={SECRET}2",  # repeated
])
def test_every_spelling_of_the_parameter_is_redacted(query):
    record = access_record(f"{WEBHOOK}?{query}")
    AccessLogRedactionFilter().filter(record)
    line = rendered(record)
    assert SECRET not in line
    assert line.count(f"={REDACTED}") >= 1


def test_the_webhook_path_without_a_query_is_untouched():
    record = access_record(WEBHOOK)
    AccessLogRedactionFilter().filter(record)
    assert rendered(record).count(WEBHOOK) == 1 and REDACTED not in rendered(record)


def test_other_paths_are_left_alone():
    path = f"/api/v1/something/else?hub.verify_token={SECRET}&page=2"
    record = access_record(path)
    AccessLogRedactionFilter().filter(record)
    assert path in rendered(record)


@pytest.mark.parametrize("args", [(), ("only-one",), ("a", "b", 3, "d", 5), None])
def test_an_unexpected_record_shape_is_passed_through_not_raised(args):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain message", args, None)
    assert AccessLogRedactionFilter().filter(record) is True


def test_the_app_installs_the_filter_on_uvicorns_access_logger_exactly_once(caplog):
    install_access_log_redaction()  # installing again (a reload, a second worker import) must not stack filters
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, AccessLogRedactionFilter)]
    assert len(filters) == 1

    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", f"{WEBHOOK}?hub.verify_token={SECRET}", "1.1", 200)
    assert SECRET not in caplog.text and REDACTED in caplog.text
