from __future__ import annotations

import logging
from typing import Any
from pathlib import Path
from time import monotonic

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_MAC,
    DOMAIN,
    SERVICE_ASSIGN_PENDING,
    SERVICE_CLEAR_HISTORY,
    SERVICE_DELETE_HISTORY_MEASUREMENT,
    SERVICE_DISCARD_PENDING,
    SERVICE_SEND_PENDING_NOTIFICATION,
    SIGNAL_MEASUREMENT,
)
from .parser import parse_service_info
from .runtime import XiaomiScaleRuntime

PLATFORMS = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)

ATTR_ENTRY_ID = "entry_id"
ATTR_USER_NAME = "user_name"
ATTR_REASON = "reason"
ATTR_INDEX = "index"
ATTR_MEASUREMENT_ID = "measurement_id"

ASSIGN_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTRY_ID): str, vol.Required(ATTR_USER_NAME): str, vol.Required(ATTR_MEASUREMENT_ID): str}
)
OPTIONAL_ENTRY_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})
CLEAR_HISTORY_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTRY_ID): str, vol.Optional(ATTR_USER_NAME): str}
)
DELETE_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Required(ATTR_USER_NAME): str,
        vol.Required(ATTR_INDEX): vol.Coerce(int),
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    await hass.http.async_register_static_paths([
        StaticPathConfig('/xiaomi_scale_ha/scale-card.js',
                         str(Path(__file__).parent / 'frontend/scale-card.js'), False)
    ])

    async def _async_respond_pending(call: ServiceCall) -> None:
        runtime = _runtime_from_call(hass, call)
        await runtime.async_respond_pending(
            call.data[ATTR_MEASUREMENT_ID], call.data['decision'], call.context.user_id
        )

    hass.services.async_register(DOMAIN, 'respond_pending', _async_respond_pending,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): str,
                           vol.Required(ATTR_MEASUREMENT_ID): str,
                           vol.Required('decision'): vol.In(['claim', 'decline'])}))
    async def _async_assign_pending(call: ServiceCall) -> None:
        runtime = _runtime_from_call(hass, call)
        await _require_admin(hass, call)
        assigned = await runtime.async_assign_pending(call.data[ATTR_USER_NAME], call.data[ATTR_MEASUREMENT_ID])
        if not assigned:
            _LOGGER.info("No pending Xiaomi Scale HA measurement to assign")

    async def _async_discard_pending(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        await _runtime_from_call(hass, call).async_discard_pending(
            call.data[ATTR_MEASUREMENT_ID], call.data.get(ATTR_REASON, "service")
        )

    async def _async_send_pending_notification(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        await _runtime_from_call(hass, call).async_send_pending_notification()

    async def _async_clear_history(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        await _runtime_from_call(hass, call).async_clear_history(
            call.data.get(ATTR_USER_NAME)
        )

    async def _async_delete_history_measurement(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        await _runtime_from_call(hass, call).async_delete_history_measurement(
            call.data[ATTR_USER_NAME], call.data[ATTR_INDEX]
        )

    @callback
    def _async_mobile_action(event: Event) -> None:
        action = event.data.get("action")
        if isinstance(action, str):
            for runtime in hass.data.get(DOMAIN, {}).values():
                hass.async_create_task(runtime.async_handle_mobile_action(action, event.context.user_id, event.data.get("device_id")))

    hass.services.async_register(DOMAIN, SERVICE_ASSIGN_PENDING, _async_assign_pending, schema=ASSIGN_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCARD_PENDING,
        _async_discard_pending,
        schema=vol.Schema(
            {vol.Optional(ATTR_ENTRY_ID): str, vol.Required(ATTR_MEASUREMENT_ID): str, vol.Optional(ATTR_REASON, default="service"): str}
        ),
    )
    hass.services.async_register(DOMAIN, SERVICE_SEND_PENDING_NOTIFICATION, _async_send_pending_notification, schema=OPTIONAL_ENTRY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_HISTORY, _async_clear_history, schema=CLEAR_HISTORY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_HISTORY_MEASUREMENT, _async_delete_history_measurement, schema=DELETE_HISTORY_SCHEMA)
    hass.bus.async_listen("mobile_app_notification_action", _async_mobile_action)
    return True


async def _require_admin(hass, call):
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None or not user.is_admin:
            raise HomeAssistantError("Administrator access is required for manual scale assignment")


def _runtime_from_call(hass: HomeAssistant, call: ServiceCall) -> XiaomiScaleRuntime:
    runtimes = hass.data.get(DOMAIN, {})
    entry_id = call.data.get(ATTR_ENTRY_ID)
    if entry_id:
        runtime = runtimes.get(entry_id)
        if runtime is None:
            raise HomeAssistantError(f"No Xiaomi Scale HA config entry loaded for {entry_id}")
        return runtime
    if len(runtimes) == 1:
        return next(iter(runtimes.values()))
    raise HomeAssistantError("entry_id is required when multiple Xiaomi Scale HA entries are loaded")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = XiaomiScaleRuntime(hass, entry)
    await runtime.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    started_at = monotonic()

    @callback
    def _async_discovered_device(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if service_info.address.upper() != runtime.mac or getattr(service_info, "time", started_at) < started_at:
            return
        try:
            measurement = parse_service_info(service_info)
        except Exception as err:
            runtime.last_diagnostic = {
                "state": "parse_error",
                "source_address": service_info.address,
                "rssi": getattr(service_info, "rssi", None),
                "error": str(err),
            }
            async_dispatcher_send(hass, SIGNAL_MEASUREMENT, entry.entry_id)
            _LOGGER.warning("Could not parse Xiaomi scale advertisement: %s", err)
            return
        if measurement is None:
            runtime.last_diagnostic = {
                "state": "unsupported_advertisement",
                "source_address": service_info.address,
                "rssi": getattr(service_info, "rssi", None),
            }
            async_dispatcher_send(hass, SIGNAL_MEASUREMENT, entry.entry_id)
            return
        runtime.hass.async_create_task(runtime.async_handle_measurement(measurement))

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass, _async_discovered_device,
            {"address": runtime.mac, "connectable": False},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    runtime.last_diagnostic = {"state": "listening", "mac": runtime.mac}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = hass.data[DOMAIN].pop(entry.entry_id, None)
        if runtime is not None:
            for task in runtime._pending_finalize.values():
                task.cancel()
            runtime._pending_finalize.clear()
    return unload_ok
