"""delete_domain MCP tool: forwards the confirmation, is marked destructive."""
import asyncio

import pytest

from ai_rete_rag_mcp import server as srv


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_calls_the_delete_endpoint_with_confirm(monkeypatch):
    seen = {}

    async def fake_request(method, path, **kwargs):
        seen.update(method=method, path=path, **kwargs)
        return '{"deleted": true}'
    monkeypatch.setattr(srv, "_request", fake_request)
    assert await srv.delete_domain("acme_hr", "acme_hr") == '{"deleted": true}'
    assert seen == {"method": "DELETE", "path": "/domains/acme_hr", "params": {"confirm": "acme_hr"}}


def test_is_annotated_destructive():
    tool = next(t for t in asyncio.run(srv.mcp.list_tools()) if t.name == "delete_domain")
    ann = tool.annotations.model_dump(by_alias=True)
    assert ann["destructiveHint"] is True
    assert ann["readOnlyHint"] is False
