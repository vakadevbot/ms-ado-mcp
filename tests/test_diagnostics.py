import logging
import socket
import ssl

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient, AzureDevOpsError
from ms_ado_mcp.diagnostics import configure_error_log, logger


@pytest.fixture
def error_log(tmp_path):
    previous = logger.handlers[:]
    # Keep other tests' handlers available for restoration.
    logger.handlers = []
    path = configure_error_log(tmp_path / ".env")
    yield path
    for handler in logger.handlers[:]:
        handler.close()
    logger.handlers = previous or [logging.NullHandler()]


@pytest.mark.parametrize(
    ("error", "cause", "expected"),
    [
        (httpx.ConnectTimeout, None, "timed out"),
        (httpx.ProxyError, None, "Proxy connection failed"),
        (httpx.ConnectError, socket.gaierror("secret-dns"), "DNS lookup failed"),
        (httpx.ConnectError, ssl.SSLError("secret-tls"), "TLS failed"),
        (httpx.ConnectError, None, "VPN and proxy"),
    ],
)
async def test_connection_failure_logged_safely(settings, error_log, error, cause, expected):
    def fail(request):
        raise error("secret-exception", request=request) from cause

    client = AzureDevOpsClient(settings, transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(AzureDevOpsError, match=expected):
            await client.query("secret-project", "secret-query", 1)
    finally:
        await client.close()
    contents = error_log.read_text(encoding="utf-8")
    assert expected in contents
    assert "host=ado.example" in contents
    assert "endpoint=wiql" in contents
    assert error.__name__ in contents
    assert "secret-" not in contents
    assert settings.pat not in contents


@pytest.mark.parametrize("status", [401, 403, 404, 407, 429, 500])
async def test_http_failure_logged_without_response_or_auth(settings, error_log, status):
    client = AzureDevOpsClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text="secret-body")),
    )
    try:
        with pytest.raises(AzureDevOpsError):
            await client.query("secret-project", "secret-query", 1)
    finally:
        await client.close()
    contents = error_log.read_text(encoding="utf-8")
    assert f"status={status}" in contents
    assert "secret-" not in contents
    assert settings.pat not in contents


async def test_invalid_json_is_logged(settings, error_log):
    client = AzureDevOpsClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret-body")),
    )
    try:
        with pytest.raises(AzureDevOpsError, match="invalid JSON"):
            await client.query("secret-project", "secret-query", 1)
    finally:
        await client.close()
    assert "invalid JSON" in error_log.read_text(encoding="utf-8")
    assert "secret-" not in error_log.read_text(encoding="utf-8")
