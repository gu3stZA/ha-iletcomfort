"""Local (LAN) transport for iLetComfort heat pumps.

The Dollin cloud endpoint ``appliance/control/hexadecimal`` takes a raw C3
frame (``AA ...`` including checksum) and returns the device's reply frame.
The heat pump's Midea Wi-Fi module accepts exactly the same frames over its
local V3 protocol (TCP 6444, token/key authenticated), so this transport is a
drop-in replacement for ``ILetComfortClient.send_hex_command``: everything above
it (frame building, decoding, model profiles, entities) is unchanged.

The Midea V3 framing, encryption and handshake come from ``msmart-ng``'s
``msmart.lan.LAN``. That API is asyncio-based while ``ILetComfortClient`` is
synchronous and runs in the executor, so requests are marshalled onto the
Home Assistant event loop with ``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from .api import ApiError
from .protocols.atw import (
    ATW_ECO_MIN_LEN, ATW_ECO_SUBTYPE, ATW_SILENT_FULL_LEN, ATW_SILENT_SUBTYPE,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPE_C3 = 0xC3
SET_MESSAGE_TYPE = 0x02
QUERY_MESSAGE_TYPE = 0x03
NOTIFY_MESSAGE_TYPE = 0x04

# Attempts per command. After a state change the module streams unsolicited
# status notifications (message type 0x04), so the reply to a query can arrive
# late, behind them; resending lets it be picked up on the next read.
SEND_ATTEMPTS = 3


def _is_c3_frame(frame: bytes) -> bool:
    return len(frame) >= 12 and frame[0] == 0xAA and frame[2] == DEVICE_TYPE_C3


def select_response(request: bytes, responses: list[bytes]) -> bytes | None:
    """Pick the reply frame that answers ``request``, or None if none does.

    The module also delivers unsolicited frames on the same connection: status
    notifications (message type 0x04, body subtype 0x01), and late replies to
    earlier requests. Match the requested subtype so a status body is never
    decoded as sensors. Some ATW units use a short 0x05 reply to a 0x07 ECO
    query, so accept that documented fallback after checking for exact matches. Query replies (0x03) win over notifications (0x04) with
    the same subtype; the newest frame wins within each group.

    For a SET (0x02) the echo is preferred, falling back to the newest C3
    frame; ``set_device`` does not interpret the reply.
    """
    frames = [bytes(f) for f in responses if _is_c3_frame(bytes(f))]
    if len(request) > 10 and request[9] == QUERY_MESSAGE_TYPE:
        subtype = request[10]
        for wanted_type in (QUERY_MESSAGE_TYPE, NOTIFY_MESSAGE_TYPE):
            for frame in reversed(frames):
                if frame[9] == wanted_type and frame[10] == subtype:
                    return frame
        if subtype == ATW_ECO_SUBTYPE:
            for frame in reversed(frames):
                body = frame[10:-1]
                if (frame[9] == QUERY_MESSAGE_TYPE and body[0] == ATW_SILENT_SUBTYPE
                        and ATW_ECO_MIN_LEN <= len(body) < ATW_SILENT_FULL_LEN):
                    return frame
        return None
    for frame in reversed(frames):
        if frame[9] == SET_MESSAGE_TYPE:
            return frame
    return frames[-1] if frames else None


def _lan_errors() -> tuple[type[BaseException], ...]:
    from msmart.lan import AuthenticationError, ProtocolError

    return (
        TimeoutError,
        asyncio.TimeoutError,
        ProtocolError,
        AuthenticationError,
        OSError,
    )


class LocalTransport:
    """Send C3 frames to the heat pump directly over the Midea V3 LAN protocol."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        device_id: int,
        token: str,
        key: str,
        timeout: float = 15,
    ) -> None:
        self._loop = loop
        self._host = host
        self._port = port
        self._device_id = device_id
        self._token = token
        self._key = key
        self._timeout = timeout
        self._lan: Any = None
        self._lock: asyncio.Lock | None = None

    @property
    def host(self) -> str:
        return self._host

    # -- sync API (called from the executor, mirrors send_hex_command) --

    def send_hex_command(self, command_hex: str) -> str:
        """Send a hex C3 frame and return the reply frame as hex.

        Must be called from a worker thread, never from the event loop thread.
        Failures raise ``ApiError`` so the coordinator's cache fallback and
        offline handling treat them exactly like a cloud failure.
        """
        try:
            frame = bytes.fromhex(command_hex)
        except ValueError as err:
            raise ApiError(f"Invalid command hex: {command_hex}") from err

        future = asyncio.run_coroutine_threadsafe(
            self.async_send(frame), self._loop,
        )
        try:
            reply = future.result(self._timeout + 5)
        except concurrent.futures.TimeoutError as err:
            future.cancel()
            raise ApiError(
                f"Local command to {self._host} timed out"
            ) from err
        return reply.hex()

    # -- async API (event loop) --

    async def async_send(self, frame: bytes) -> bytes:
        """Send one C3 frame, (re)connecting and authenticating as needed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        errors = _lan_errors()
        is_query = len(frame) > 10 and frame[9] == QUERY_MESSAGE_TYPE
        async with self._lock:
            try:
                async with asyncio.timeout(self._timeout):
                    if self._lan is None:
                        await self._async_connect()
                    for attempt in range(1, SEND_ATTEMPTS + 1):
                        _LOGGER.debug(
                            "Local send to %s (attempt %d): %s",
                            self._host, attempt, frame.hex(),
                        )
                        responses = await self._lan.send(frame)
                        _LOGGER.debug(
                            "Local replies from %s: %s",
                            self._host,
                            [bytes(r).hex() for r in responses],
                        )
                        reply = select_response(frame, responses)
                        if reply is not None:
                            return reply
                        if not is_query:
                            break
                        # Only unrelated frames so far: give the real reply a
                        # moment to arrive, then resend (stale frames are
                        # drained into the next send's responses).
                        await asyncio.sleep(0.5)
            except errors as err:
                # Drop the connection so the next call starts a fresh
                # connect + V3 handshake instead of reusing a dead session.
                self._lan = None
                raise ApiError(
                    f"Local command to {self._host} failed: "
                    f"{type(err).__name__}: {err}"
                ) from err
        raise ApiError(
            f"Local command to {self._host} got no matching reply for "
            f"{frame.hex()}"
        )

    async def _async_connect(self) -> None:
        from msmart.lan import LAN

        lan = LAN(self._host, self._port, self._device_id)
        await lan.authenticate(self._token, self._key)
        self._lan = lan

    async def async_close(self) -> None:
        """Drop the LAN connection (entry unload)."""
        lan, self._lan = self._lan, None
        if lan is not None:
            try:
                lan._disconnect()  # noqa: SLF001 — msmart has no public close
            except Exception:  # noqa: BLE001
                pass


async def async_validate_local(
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int,
    device_id: int,
    token: str,
    key: str,
) -> bytes:
    """Authenticate and send one status query; return the reply frame.

    Used by the options flow to reject wrong host/token/key before saving.
    """
    from .api import build_c3_query

    transport = LocalTransport(loop, host, port, device_id, token, key)
    try:
        return await transport.async_send(bytes.fromhex(build_c3_query(0x01)))
    finally:
        await transport.async_close()
