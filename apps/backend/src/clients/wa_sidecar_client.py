"""Thin async HTTP client for the Node.js wa-sidecar service. The sidecar
owns Baileys WS connections; this module is the only place the api speaks
to it. Routes:

    POST /sessions               { session_id, display_name }
    GET  /sessions/{id}
    POST /sessions/{id}/logout

Sidecar lives on the internal Docker network — no auth between services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


class WaSidecarError(Exception):
    """Raised when the sidecar is unreachable or returns a non-2xx response."""


@dataclass
class SidecarSession:
    session_id: str
    status: str  # pending | qr_pending | connecting | connected | disconnected | logged_out
    qr_data_url: str | None
    msisdn: str | None
    display_name: str | None
    last_updated: int | None  # unix ms


def _parse(payload: dict[str, Any]) -> SidecarSession:
    return SidecarSession(
        session_id=payload["session_id"],
        status=payload.get("status", "pending"),
        qr_data_url=payload.get("qr_data_url"),
        msisdn=payload.get("msisdn"),
        display_name=payload.get("display_name"),
        last_updated=payload.get("last_updated"),
    )


async def create_session(session_id: str, display_name: str | None) -> SidecarSession:
    url = f"{settings.wa_sidecar_url}/sessions"
    body = {"session_id": session_id, "display_name": display_name}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        try:
            r = await c.post(url, json=body)
        except httpx.HTTPError as e:
            raise WaSidecarError(f"sidecar unreachable: {e}") from e
    if r.status_code not in (200, 201):
        raise WaSidecarError(f"sidecar create failed: {r.status_code} {r.text[:200]}")
    return _parse(r.json())


async def get_session(session_id: str) -> SidecarSession | None:
    url = f"{settings.wa_sidecar_url}/sessions/{session_id}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            raise WaSidecarError(f"sidecar unreachable: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise WaSidecarError(f"sidecar get failed: {r.status_code} {r.text[:200]}")
    return _parse(r.json())


@dataclass
class SidecarGroup:
    jid: str
    subject: str
    participants_count: int
    announce: bool  # True if only admins can post


@dataclass
class SidecarSendResult:
    message_id: str | None
    timestamp: int | None


class NotConnected(WaSidecarError):
    """Raised when the sidecar reports the session isn't in a connected state.
    Caller should surface the message to the user (typically: 'reconnect the
    primary phone' or 'remove and re-link the number')."""


async def list_groups(session_id: str) -> list[SidecarGroup]:
    url = f"{settings.wa_sidecar_url}/sessions/{session_id}/groups"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            raise WaSidecarError(f"sidecar unreachable: {e}") from e
    if r.status_code == 409:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        raise NotConnected(f"session not connected (status={body.get('status')})")
    if r.status_code != 200:
        raise WaSidecarError(f"sidecar groups failed: {r.status_code} {r.text[:200]}")
    payload = r.json()
    return [
        SidecarGroup(
            jid=g["jid"],
            subject=g.get("subject", ""),
            participants_count=int(g.get("participants_count", 0)),
            announce=bool(g.get("announce", False)),
        )
        for g in payload.get("groups", [])
    ]


async def send_message(session_id: str, jid: str, text: str) -> SidecarSendResult:
    url = f"{settings.wa_sidecar_url}/sessions/{session_id}/messages"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        try:
            r = await c.post(url, json={"jid": jid, "text": text})
        except httpx.HTTPError as e:
            raise WaSidecarError(f"sidecar unreachable: {e}") from e
    if r.status_code == 409:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        raise NotConnected(f"session not connected (status={body.get('status')})")
    if r.status_code != 200:
        raise WaSidecarError(f"sidecar send failed: {r.status_code} {r.text[:200]}")
    payload = r.json()
    return SidecarSendResult(
        message_id=payload.get("message_id"),
        timestamp=payload.get("timestamp"),
    )


async def logout_session(session_id: str) -> None:
    url = f"{settings.wa_sidecar_url}/sessions/{session_id}/logout"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        try:
            r = await c.post(url)
        except httpx.HTTPError as e:
            raise WaSidecarError(f"sidecar unreachable: {e}") from e
    # 404 is OK — session might already have been cleaned up on the sidecar
    # (e.g. logged_out state). Treat anything else as a hard failure.
    if r.status_code not in (200, 404):
        raise WaSidecarError(f"sidecar logout failed: {r.status_code} {r.text[:200]}")
