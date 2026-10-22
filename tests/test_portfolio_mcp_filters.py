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
