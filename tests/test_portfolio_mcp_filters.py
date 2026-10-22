"""Codex investigation filters must reach the same REST contract."""

import asyncio
from app.portfolio import mcp_server


def test_records_forwards_investigation_filters(monkeypatch):
    captured = {}

    async def request(method, path, params=None, body=None):
        captured.update(method=method, path=path, params=params)
        return {"total": 0, "rows": []}

    monkeypatch.setattr(mcp_server, "request", request)
    result = asyncio.run(
        mcp_server.records(
            "reviews",
            entity_id=3,
            account_id=5,
            start="2025-01-01",
            end="2025-12-31",
            status="open",
            property_id=7,
            offset=100,
            limit=50,
        )
    )
    assert result == {"total": 0, "rows": []}
    assert captured == {
        "method": "GET",
        "path": "/records/reviews",
        "params": {
            "entity_id": 3,
            "account_id": 5,
            "start": "2025-01-01",
            "end": "2025-12-31",
            "status": "open",
            "property_id": 7,
            "offset": 100,
            "limit": 50,
        },
    }


def test_domain_rejection_is_mcp_tool_error(monkeypatch):
    import http.client
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    class Response:
        status = 409

        def read(self):
            return b'{"detail":{"code":"allocation_source_deficit","diagnostics":{"after_cents":-100}}}'

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        mcp_server, "configuration", lambda: ("http://127.0.0.1:3001", "synthetic")
    )
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(ToolError, match="allocation_source_deficit"):
        asyncio.run(mcp_server.request("POST", "/commands", body={}))
