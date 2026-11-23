"""Tests for the local (LAN) transport and its wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from msmart.lan import AuthenticationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    ApiError,
    ILetComfortClient,
    build_c3_query,
    decode_its_sensors,
    decode_its_status,
    extract_c3_body,
    parse_hex_response,
)
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_CONNECTION_MODE,
    CONF_LOCAL_HOST,
    CONF_LOCAL_KEY,
    CONF_LOCAL_TOKEN,
    CONF_OPERATION_MODE,
    CONF_REGION,
    CONF_SN8,
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL,
    DOMAIN,
    OPERATION_MODE_HA_PRIMARY,
    OPERATION_MODE_PHONE_APP,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.local import LocalTransport, select_response

# Real frames captured over the LAN from a 14.5 kW ITS pool heat pump
# (sn8 171000AU) while heating: mode Heat, setpoint 40 C, compressor 83 Hz,
# water in/out 30/33 C, outdoor 16 C, 9 A / 224 V. Identical to what the
# Dollin cloud returned for the same unit at the same moment.
LAN_STATUS = bytes.fromhex(
    "aa3dc3000000000000030101014b4b2d3f370f23372528c0000080232d3f3700233700"
    "3f0000000000000280000001200c3a03c00053000001a1000001ca"
)
LAN_SENSORS = bytes.fromhex(
    "aa3dc3000000000000030201000000000000000000000102002054070001002b334473"
    "4144ef0900e00431370c31370c313707000001720000000000003b"
)
STATUS_QUERY = bytes.fromhex(build_c3_query(0x01))
SENSORS_QUERY = bytes.fromhex(build_c3_query(0x02))

DEVICE_ID = 150633095058906
TOKEN = "ab" * 64
KEY = "cd" * 32


class FakeLAN:
    """Stand-in for msmart.lan.LAN recording calls."""

    instances: list["FakeLAN"] = []
    replies: list = []

    def __init__(self, ip, port, device_id):
        self.ip, self.port, self.device_id = ip, port, device_id
        self.auth_calls = []
        self.sent = []
        self.disconnected = False
        FakeLAN.instances.append(self)

    async def authenticate(self, token, key):
        self.auth_calls.append((token, key))

    async def send(self, data):
        self.sent.append(data)
        reply = FakeLAN.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def _disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_lan():
    FakeLAN.instances = []
    FakeLAN.replies = []
    with patch("msmart.lan.LAN", FakeLAN):
        yield FakeLAN


# --- frame selection ---------------------------------------------------------

def test_select_response_prefers_matching_query_subtype():
    # An unsolicited status frame arrives alongside the sensors reply.
    assert select_response(SENSORS_QUERY, [LAN_STATUS, LAN_SENSORS]) == LAN_SENSORS
    assert select_response(STATUS_QUERY, [LAN_STATUS, LAN_SENSORS]) == LAN_STATUS


def test_select_response_falls_back_to_last_c3_frame_for_set():
    set_frame = bytes.fromhex("aa3dc3" + "00" * 6 + "02" + "00" * 52)
    assert select_response(set_frame, [LAN_STATUS]) == LAN_STATUS


def test_select_response_without_c3_frame_returns_none():
    assert select_response(STATUS_QUERY, [b"\x00\x01"]) is None
    assert select_response(STATUS_QUERY, []) is None


# Real burst captured right after a set_hvac_mode(heat): for the sensors query
# the module returned a late status reply (type 0x03, subtype 0x01) and two
# unsolicited status notifications (type 0x04, subtype 0x01) -- no sensors
# frame. Falling back to "last frame" decoded a status body as sensors
# (water out -35 C, outdoor 0 C).
LATE_STATUS_REPLY = bytes.fromhex(
    "aa3dc3000000000000030101014b4b2d3f370f23372528c0000080232d3f3700233700"
    "3f0000000000000258000001200a8203ca003b000001a2000001b9"
)
STATUS_NOTIFY_1 = bytes.fromhex(
    "aa3dc3000000000000040101014b4b2d3f370f23372528c0000080232d3f3700233700"
    "3f000000000000024e000001180a5a03ca0039000001a2000001f4"
)
STATUS_NOTIFY_2 = bytes.fromhex(
    "aa3dc3000000000000040101014b4b2d3f370f23372528c0000080232d3f3700233700"
    "3f0000000000000244000001180a3203ca0037000001a200000128"
)
STATUS_BURST = [LATE_STATUS_REPLY, STATUS_NOTIFY_1, STATUS_NOTIFY_2]


def test_sensors_query_never_takes_a_status_frame():
    assert select_response(SENSORS_QUERY, STATUS_BURST) is None


def test_status_query_prefers_reply_over_notification():
    assert select_response(STATUS_QUERY, STATUS_BURST) == LATE_STATUS_REPLY
    # A notification carries the same status body, so it is acceptable when no
    # proper reply came back.
    assert select_response(STATUS_QUERY, [STATUS_NOTIFY_1]) == STATUS_NOTIFY_1


def test_set_prefers_its_echo():
    set_frame = bytes.fromhex("aa3dc3" + "00" * 6 + "02" + "00" * 52)
    echo = bytes.fromhex("aa3dc3" + "00" * 6 + "02" + "01" * 52)
    assert select_response(set_frame, [echo, STATUS_NOTIFY_1]) == echo


async def test_query_resends_until_matching_reply(hass: HomeAssistant, fake_lan):
    fake_lan.replies = [STATUS_BURST, [STATUS_NOTIFY_1, LAN_SENSORS]]
    transport = LocalTransport(hass.loop, "10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)

    assert await transport.async_send(SENSORS_QUERY) == LAN_SENSORS
    assert fake_lan.instances[0].sent == [SENSORS_QUERY, SENSORS_QUERY]


async def test_query_without_matching_reply_raises(hass: HomeAssistant, fake_lan):
    fake_lan.replies = [STATUS_BURST, STATUS_BURST, STATUS_BURST]
    transport = LocalTransport(hass.loop, "10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)

    with pytest.raises(ApiError, match="no matching reply"):
        await transport.async_send(SENSORS_QUERY)
    assert len(fake_lan.instances[0].sent) == 3


def test_lan_frames_decode_with_existing_decoders():
    """LAN replies feed the unchanged cloud decoders."""
    _, body = extract_c3_body(parse_hex_response(LAN_STATUS.hex()))
    status = decode_its_status(body)
    assert status.mode_name == "Heat"
    assert status.t5s_def == 40
    assert status.comp_frq == 83
    assert status.comp_running is True
    assert status.error_code == 0

    _, body = extract_c3_body(parse_hex_response(LAN_SENSORS.hex()))
    sensors = decode_its_sensors(body)
    assert (sensors.twin_temp, sensors.twout_temp, sensors.t4_temp) == (30, 33, 16)
    assert sensors.odu_current == 9.0
    assert sensors.odu_voltage == 224


# --- transport -----------------------------------------------------------------

async def test_send_hex_command_from_executor(hass: HomeAssistant, fake_lan):
    fake_lan.replies = [[LAN_STATUS], [LAN_SENSORS]]
    transport = LocalTransport(hass.loop, "10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)

    status_hex = await hass.async_add_executor_job(
        transport.send_hex_command, STATUS_QUERY.hex(),
    )
    sensors_hex = await hass.async_add_executor_job(
        transport.send_hex_command, SENSORS_QUERY.hex(),
    )

    assert status_hex == LAN_STATUS.hex()
    assert sensors_hex == LAN_SENSORS.hex()
    # One connection + one V3 handshake reused for both requests.
    assert len(fake_lan.instances) == 1
    lan = fake_lan.instances[0]
    assert (lan.ip, lan.port, lan.device_id) == ("10.0.0.5", 6444, DEVICE_ID)
    assert lan.auth_calls == [(TOKEN, KEY)]
    assert lan.sent == [STATUS_QUERY, SENSORS_QUERY]


async def test_lan_error_becomes_api_error_and_reconnects(hass: HomeAssistant, fake_lan):
    fake_lan.replies = [TimeoutError("no response"), [LAN_STATUS]]
    transport = LocalTransport(hass.loop, "10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)

    with pytest.raises(ApiError, match="10.0.0.5"):
        await transport.async_send(STATUS_QUERY)
    assert await transport.async_send(STATUS_QUERY) == LAN_STATUS
    # The dead session was dropped and a fresh handshake made.
    assert len(fake_lan.instances) == 2


async def test_auth_failure_keeps_cause(hass: HomeAssistant, fake_lan):
    transport = LocalTransport(hass.loop, "10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)

    async def bad_auth(self, token, key):
        raise AuthenticationError("bad key")

    with patch.object(FakeLAN, "authenticate", bad_auth):
        with pytest.raises(ApiError) as info:
            await transport.async_send(STATUS_QUERY)
    assert isinstance(info.value.__cause__, AuthenticationError)


def test_client_routes_hex_commands_to_local_transport():
    client = ILetComfortClient()
    client._v2_request = MagicMock(side_effect=AssertionError("cloud used"))
    client.local_transport = MagicMock()
    client.local_transport.send_hex_command.return_value = LAN_STATUS.hex()

    status = client.query_status(str(DEVICE_ID), "171000AU")

    client.local_transport.send_hex_command.assert_called_once_with(
        STATUS_QUERY.hex()
    )
    assert status.comp_frq == 83


# --- coordinator wiring ----------------------------------------------------------

def _entry(options=None, data_extra=None) -> MockConfigEntry:
    data = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_APPLIANCE_CODE: str(DEVICE_ID),
        CONF_REGION: REGION_US,
        **(data_extra or {}),
    }
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"user@example.com:{DEVICE_ID}",
        data=data,
        options=options or {},
        version=2,
    )


LOCAL_OPTIONS = {
    CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
    CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL,
    CONF_LOCAL_HOST: "10.0.0.5",
    CONF_LOCAL_TOKEN: TOKEN,
    CONF_LOCAL_KEY: KEY,
}


async def test_cloud_mode_has_no_local_transport(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    assert coord.local_transport is None
    assert coord.client.local_transport is None


async def test_local_mode_attaches_transport(hass: HomeAssistant):
    entry = _entry(LOCAL_OPTIONS)
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    assert coord.is_local
    assert coord.client.local_transport is coord.local_transport
    assert coord.local_transport.host == "10.0.0.5"


async def test_local_mode_overrides_phone_app_restrictions(hass: HomeAssistant):
    entry = _entry({**LOCAL_OPTIONS, CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP})
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    assert coord._push_enabled is False

    coord.client = MagicMock()
    coord.async_request_refresh = MagicMock(return_value=_done())
    await coord.async_set_device(mode=1)  # must not raise
    coord.client.set_device.assert_called_once()


async def test_phone_app_mode_still_blocks_control_in_cloud(hass: HomeAssistant):
    entry = _entry({CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP})
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    with pytest.raises(HomeAssistantError):
        await coord.async_set_device(mode=1)


async def test_sn8_falls_back_to_persisted_value(hass: HomeAssistant):
    entry = _entry(LOCAL_OPTIONS, {CONF_SN8: "171000AU"})
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    assert coord.sn8 == "171000AU"

    # Local mode with a known sn8 must not query the cloud each poll.
    coord.client = MagicMock()
    await coord._ensure_appliance_meta()
    coord.client.list_appliances.assert_not_called()


async def test_discovered_sn8_is_persisted(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    coord.client = MagicMock()
    coord.client.list_appliances.return_value = [
        {"applianceCode": str(DEVICE_ID), "sn8": "171000AU"},
    ]
    await coord._ensure_appliance_meta()
    assert entry.data[CONF_SN8] == "171000AU"


async def test_local_mode_survives_cloud_login_failure(hass: HomeAssistant):
    entry = _entry(LOCAL_OPTIONS, {CONF_SN8: "171000AU"})
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    coord.client = MagicMock()
    coord.client.load_token.return_value = False
    coord.client.login.side_effect = ApiError("cloud down")
    coord.async_config_entry_first_refresh = MagicMock(return_value=_done())
    coord.async_start_push = MagicMock(return_value=_done())

    await coord.async_first_refresh_with_login()  # must not raise
    coord.async_config_entry_first_refresh.assert_called_once()


async def test_cloud_mode_still_fails_on_login_failure(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    coord = ILetComfortCoordinator(hass, entry)
    coord.client = MagicMock()
    coord.client.load_token.return_value = False
    coord.client.login.side_effect = ApiError("cloud down")
    with pytest.raises(ApiError):
        await coord.async_first_refresh_with_login()


def _done():
    import asyncio

    fut = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    return fut


# --- options flow --------------------------------------------------------------

async def _open_options(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return await hass.config_entries.options.async_init(entry.entry_id)


async def test_options_local_mode_validates_and_saves(hass: HomeAssistant):
    entry = _entry()
    result = await _open_options(hass, entry)
    with patch(
        "custom_components.iletcomfort.config_flow.async_validate_local",
        return_value=LAN_STATUS,
    ) as validate:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
                CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL,
                CONF_LOCAL_HOST: " 10.0.0.5 ",
                CONF_LOCAL_TOKEN: TOKEN.upper(),
                CONF_LOCAL_KEY: KEY,
            },
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LOCAL_HOST] == "10.0.0.5"
    assert entry.options[CONF_LOCAL_TOKEN] == TOKEN  # normalized to lowercase
    args = validate.call_args.args
    assert args[1:] == ("10.0.0.5", 6444, DEVICE_ID, TOKEN, KEY)


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({CONF_LOCAL_HOST: "10.0.0.5"}, "local_missing"),
        ({CONF_LOCAL_HOST: "10.0.0.5", CONF_LOCAL_TOKEN: "zz", CONF_LOCAL_KEY: KEY},
         "local_invalid_hex"),
    ],
)
async def test_options_local_mode_rejects_bad_input(hass, user_input, error):
    result = await _open_options(hass, _entry())
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
            CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL,
            **user_input,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize(
    ("cause", "error"),
    [
        (AuthenticationError("bad"), "local_auth_failed"),
        (TimeoutError("no reply"), "local_cannot_connect"),
    ],
)
async def test_options_local_mode_reports_connection_errors(hass, cause, error):
    result = await _open_options(hass, _entry())
    api_error = ApiError("failed")
    api_error.__cause__ = cause
    with patch(
        "custom_components.iletcomfort.config_flow.async_validate_local",
        side_effect=api_error,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
                CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL,
                CONF_LOCAL_HOST: "10.0.0.5",
                CONF_LOCAL_TOKEN: TOKEN,
                CONF_LOCAL_KEY: KEY,
            },
        )
    assert result["errors"] == {"base": error}


async def test_options_cloud_mode_needs_no_lan_fields(hass: HomeAssistant):
    entry = _entry()
    result = await _open_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
            CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY,
        CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
    }
