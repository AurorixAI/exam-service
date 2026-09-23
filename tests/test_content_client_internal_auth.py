"""content-service now requires X-Internal-Service-Token on every call
(see content-service SEC-01 fix); this locks in that the shared httpx client
actually attaches it, so a future refactor of get_client() can't silently
drop the header and start getting 403s from content-service in production.
"""
from src.clients import content_client
from src.core.config import get_settings


def _reset():
    content_client._client = None
    get_settings.cache_clear()


def test_get_client_attaches_internal_service_token_header(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "shared-secret-xyz")
    _reset()
    try:
        client = content_client.get_client()
        assert client.headers["X-Internal-Service-Token"] == "shared-secret-xyz"
    finally:
        _reset()
