"""Config flow for the iLetComfort integration."""

from __future__ import annotations

import logging
from typing import Any, cast

import requests
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ApiError, AuthError, ILetComfortClient
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_CONNECTION_MODE,
    CONF_C3_BASIC_CONTROL,
    CONF_ENABLE_MQTT_PUSH,
    CONF_LOCAL_HOST,
    CONF_LOCAL_KEY,
    CONF_LOCAL_TOKEN,
    CONF_OPERATION_MODE,
    CONF_REGION,
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_C3_BASIC_CONTROL,
    DEFAULT_ENABLE_MQTT_PUSH,
    LOCAL_PORT,
    DEFAULT_OPERATION_MODE,
    DEFAULT_REGION,
    DOMAIN,
    OPERATION_MODE_HA_PRIMARY,
    OPERATION_MODE_PHONE_APP,
    REGION_EU,
    REGION_URLS,
    REGION_US,
)
from .local import async_validate_local
from .model_profiles import ModelProfile, resolve_profile

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(
            SelectSelectorConfig(
                options=[REGION_US, REGION_EU],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
        ),
    }
)


def _appliance_label(appliance: dict[str, Any]) -> str:
    """Build a user-facing label for an appliance entry."""
    for key in ("applianceName", "nickname", "name", "applianceCode"):
        value = appliance.get(key)
        if value:
            return str(value)
    return "Unknown device"


class ILetComfortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iLetComfort."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ILetComfortOptionsFlow:
        """Return the operation-mode options flow (issue #55)."""
        return ILetComfortOptionsFlow()

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._region: str | None = None
        self._appliances: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the credentials + region step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            region = user_input[CONF_REGION]
            api_base = REGION_URLS.get(region, REGION_URLS[DEFAULT_REGION])

            client = ILetComfortClient(api_base=api_base)
            appliances: list[dict[str, Any]] | None = None

            try:
                await self.hass.async_add_executor_job(
                    client.login, email, password,
                )
            except AuthError as err:
                _LOGGER.warning("Auth error during login: %s", err)
                errors["base"] = "invalid_auth"
            except requests.exceptions.RequestException as err:
                _LOGGER.warning("Network error during login: %s", err)
                errors["base"] = "cannot_connect"
            except ApiError as err:
                _LOGGER.warning("API error during login: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"

            if not errors:
                try:
                    appliances = await self.hass.async_add_executor_job(
                        client.list_appliances,
                    )
                except requests.exceptions.RequestException as err:
                    _LOGGER.warning("Network error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except ApiError as err:
                    _LOGGER.warning("API error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during device discovery")
                    errors["base"] = "unknown"

            if not errors:
                if not appliances:
                    errors["base"] = "no_devices"
                else:
                    self._email = email
                    self._password = password
                    self._region = region
                    self._appliances = appliances
                    return await self.async_step_device()

        # HA 2026.10+ types data_schema as probatio.Schema, while this
        # integration supports HA 2024.1+ where voluptuous.Schema is required.
        return self.async_show_form(
            step_id="user",
            data_schema=cast(Any, STEP_USER_DATA_SCHEMA),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the device-picker step."""
        errors: dict[str, str] = {}

        options = {
            str(a.get("applianceCode", "")): _appliance_label(a)
            for a in self._appliances
            if a.get("applianceCode")
        }

        if user_input is not None:
            appliance_code = user_input[CONF_APPLIANCE_CODE]

            await self.async_set_unique_id(
                f"{(self._email or '').lower()}:{appliance_code}"
            )
            self._abort_if_unique_id_configured()

            label = options.get(appliance_code, appliance_code)
            return self.async_create_entry(
                title=f"iLetComfort ({label})",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_REGION: self._region,
                    CONF_APPLIANCE_CODE: appliance_code,
                },
            )

        schema = vol.Schema(
            {vol.Required(CONF_APPLIANCE_CODE): vol.In(options)}
        )
        return self.async_show_form(
            step_id="device",
            data_schema=cast(Any, schema),
            errors=errors,
        )


def _clean_hex(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return bool(value)


class ILetComfortOptionsFlow(OptionsFlow):
    """Options for an iLetComfort entry.

    Select whether HA owns the account session or coexists read-only with the
    phone app over certificate-authenticated MQTT push (issue #55), and whether
    C3 frames go via the cloud or straight to the heat pump over the LAN.
    Changing the options reloads the entry (see the update listener in
    ``__init__``).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            options = self.config_entry.options
            if not self._offers_c3_basic_control() and CONF_C3_BASIC_CONTROL in options:
                # Keep an earlier choice when the field is not shown (e.g.
                # the model code is not known yet).
                data[CONF_C3_BASIC_CONTROL] = options[CONF_C3_BASIC_CONTROL]
            if data.get(CONF_CONNECTION_MODE) == CONNECTION_MODE_LOCAL:
                errors = await self._async_validate_local(data)
            else:
                # Cloud mode: keep LAN settings so switching back is one click,
                # but only if they were provided.
                data = {k: v for k, v in data.items() if v not in (None, "")}
            if not errors:
                return self.async_create_entry(data=data)

        return self.async_show_form(
            step_id="init",
            data_schema=cast(Any, self._schema(user_input)),
            errors=errors,
        )

    async def _async_validate_local(self, data: dict[str, Any]) -> dict[str, str]:
        """Normalize LAN fields in ``data`` and test them against the device."""
        host = str(data.get(CONF_LOCAL_HOST) or "").strip()
        token = _clean_hex(data.get(CONF_LOCAL_TOKEN))
        key = _clean_hex(data.get(CONF_LOCAL_KEY))
        if not host or not token or not key:
            return {"base": "local_missing"}
        if not _is_hex(token) or not _is_hex(key):
            return {"base": "local_invalid_hex"}
        data[CONF_LOCAL_HOST] = host
        data[CONF_LOCAL_TOKEN] = token
        data[CONF_LOCAL_KEY] = key

        appliance_code = str(self.config_entry.data.get(CONF_APPLIANCE_CODE, ""))
        if not appliance_code.isdigit():
            return {"base": "local_unsupported_device"}

        try:
            await async_validate_local(
                self.hass.loop, host, LOCAL_PORT, int(appliance_code), token, key,
            )
        except ApiError as err:
            from msmart.lan import AuthenticationError

            _LOGGER.warning("Local connection test to %s failed: %s", host, err)
            if isinstance(err.__cause__, AuthenticationError):
                return {"base": "local_auth_failed"}
            return {"base": "local_cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error testing local connection")
            return {"base": "unknown"}
        return {}

    def _schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        options = {**self.config_entry.options, **(user_input or {})}
        current = options.get(CONF_OPERATION_MODE)
        if current is None:
            current = (
                OPERATION_MODE_PHONE_APP
                if options.get(
                    CONF_ENABLE_MQTT_PUSH, DEFAULT_ENABLE_MQTT_PUSH
                )
                else DEFAULT_OPERATION_MODE
            )
        connection = options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
        secret = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_OPERATION_MODE, default=current,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        OPERATION_MODE_HA_PRIMARY,
                        OPERATION_MODE_PHONE_APP,
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="operation_mode",
                )
            ),
            vol.Required(
                CONF_CONNECTION_MODE, default=connection,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[CONNECTION_MODE_CLOUD, CONNECTION_MODE_LOCAL],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="connection_mode",
                )
            ),
            vol.Optional(
                CONF_LOCAL_HOST,
                description={"suggested_value": options.get(CONF_LOCAL_HOST)},
            ): TextSelector(),
            vol.Optional(
                CONF_LOCAL_TOKEN,
                description={"suggested_value": options.get(CONF_LOCAL_TOKEN)},
            ): secret,
            vol.Optional(
                CONF_LOCAL_KEY,
                description={"suggested_value": options.get(CONF_LOCAL_KEY)},
            ): secret,
        }
        if self._offers_c3_basic_control():
            fields[
                vol.Required(
                    CONF_C3_BASIC_CONTROL,
                    default=options.get(
                        CONF_C3_BASIC_CONTROL, DEFAULT_C3_BASIC_CONTROL
                    ),
                )
            ] = bool
        return vol.Schema(fields)

    def _offers_c3_basic_control(self) -> bool:
        """Offer C3 basic control only for loaded sn8 171H120F (ATW) units."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        sn8 = getattr(coordinator, "sn8", None)
        return resolve_profile(sn8) is ModelProfile.ATW
