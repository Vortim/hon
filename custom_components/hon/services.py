"""Custom ``hon.set_settings`` service.

Every hOn settings entity (climate mode/temperature, fan-direction selects, the
silent-mode / screen-display switches, ...) writes into the SAME bundled
``settings`` command and then calls ``commands["settings"].send()``. Changing
six things via six entity services therefore sends six full-bundle commands ->
six device beeps.

This service stages an arbitrary set of ``settings.*`` parameters and sends the
bundle ONCE -> a single cloud command / single beep. Friendly values are
accepted (``cool``, ``position_2``, ``on``/``off``); anything unrecognised is
passed through verbatim, so it stays usable for any hOn appliance/parameter.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from . import const
from .const import DOMAIN, HON_HVAC_MODE

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_SETTINGS = "set_settings"
_SERVICES_KEY = f"{DOMAIN}_services_registered"

_BOOL_TRUE = {"on", "true", "yes", "1"}
_BOOL_FALSE = {"off", "false", "no", "0"}


def _reverse(mapping: dict[Any, Any]) -> dict[str, str]:
    """friendly label (lowercased) -> raw value. First raw wins on duplicates."""
    out: dict[str, str] = {}
    for raw, label in mapping.items():
        out.setdefault(str(label).lower(), str(raw))
    return out


# Friendly -> raw maps for the enum-style AC settings. Other keys fall through
# to boolean / pass-through handling, keeping the service appliance-agnostic.
_ENUM_MAPS: dict[str, dict[str, str]] = {
    "machMode": _reverse(HON_HVAC_MODE),
    "windDirectionHorizontal": _reverse(const.AC_POSITION_HORIZONTAL),
    "windDirectionVertical": _reverse(const.AC_POSITION_VERTICAL),
}

SET_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("entity_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("area_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("settings"): dict,
    }
)


def _resolve_value(key: str, value: Any) -> str:
    """Translate a friendly value to the raw string hOn expects."""
    if isinstance(value, bool):
        return "1" if value else "0"
    sval = str(value).strip().lower()
    if (enum_map := _ENUM_MAPS.get(key)) is not None and sval in enum_map:
        return enum_map[sval]
    if sval in _BOOL_TRUE:
        return "1"
    if sval in _BOOL_FALSE:
        return "0"
    return str(value)


def async_register_services(hass: HomeAssistant) -> None:
    """Register ``hon.set_settings`` once for the whole integration."""
    if hass.data.get(_SERVICES_KEY):
        return

    async def _async_set_settings(call: ServiceCall) -> None:
        settings: dict[str, Any] = call.data["settings"]
        if not settings:
            raise HomeAssistantError("hon.set_settings: 'settings' must not be empty")

        device_ids: set[str] = set(call.data.get("device_id", []))
        ent_reg = er.async_get(hass)
        for eid in call.data.get("entity_id", []):
            if (ent := ent_reg.async_get(eid)) is not None and ent.device_id:
                device_ids.add(ent.device_id)
        dev_reg = dr.async_get(hass)
        for area in call.data.get("area_id", []):
            for area_dev in dr.async_entries_for_area(dev_reg, area):
                device_ids.add(area_dev.id)

        # appliance.unique_id -> (appliance, coordinator)
        appliances: dict[str, tuple[Any, Any]] = {}
        for store in hass.data.get(DOMAIN, {}).values():
            if not isinstance(store, dict) or (hon := store.get("hon")) is None:
                continue
            for app in hon.appliances:
                appliances[app.unique_id] = (app, store.get("coordinator"))

        targets: list[tuple[Any, Any]] = []
        for did in device_ids:
            if (dev := dev_reg.async_get(did)) is None:
                continue
            for domain, ident in dev.identifiers:
                if domain == DOMAIN and ident in appliances:
                    targets.append(appliances[ident])
                    break

        if not targets:
            raise HomeAssistantError(
                "hon.set_settings: no hOn appliance matched the target"
            )

        for app, coordinator in targets:
            if "settings" not in app.commands:
                _LOGGER.warning(
                    "hon.set_settings: %s has no 'settings' command; skipping",
                    app.nick_name,
                )
                continue
            applied: list[str] = []
            for key, value in settings.items():
                if (setting := app.settings.get(f"settings.{key}")) is None:
                    _LOGGER.warning(
                        "hon.set_settings: %s has no setting '%s'; skipping",
                        app.nick_name,
                        key,
                    )
                    continue
                try:
                    setting.value = _resolve_value(key, value)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "hon.set_settings: %s rejected %s=%s (%s); skipping",
                        app.nick_name,
                        key,
                        value,
                        err,
                    )
                    continue
                applied.append(key)
            if not applied:
                continue
            await app.commands["settings"].send()
            if coordinator is not None:
                coordinator.async_set_updated_data({})
            _LOGGER.debug(
                "hon.set_settings: %s sent %s in one command",
                app.nick_name,
                applied,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SETTINGS, _async_set_settings, schema=SET_SETTINGS_SCHEMA
    )
    hass.data[_SERVICES_KEY] = True
    _LOGGER.debug("hon: registered %s.%s service", DOMAIN, SERVICE_SET_SETTINGS)
