"""Drive a repair's fix flow over the HTTP API, as the frontend does."""

from http import HTTPStatus
from typing import Any

from aiohttp.test_utils import TestClient
from homeassistant.components.repairs.websocket_api import (
    RepairsFlowIndexView,
    RepairsFlowResourceView,
)


async def start_repair_fix_flow(
    client: TestClient, handler: str, issue_id: str
) -> dict[str, Any]:
    """Start the fix flow of an issue."""
    resp = await client.post(
        RepairsFlowIndexView.url, json={"handler": handler, "issue_id": issue_id}
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    return await resp.json()


async def process_repair_fix_flow(
    client: TestClient, flow_id: str, json: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Submit a step of a fix flow."""
    resp = await client.post(
        RepairsFlowResourceView.url.format(flow_id=flow_id), json=json
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    return await resp.json()
