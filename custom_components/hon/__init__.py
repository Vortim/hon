import asyncio
import importlib
import logging
import pkgutil
import pyhon.appliances
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pathlib import Path
from pyhon import Hon
from typing import Any

from .const import DOMAIN, PLATFORMS, MOBILE_ID, CONF_REFRESH_TOKEN
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)


def _patch_pyhon_custom_scheme_oauth() -> None:
    """Let pyhon finish login when Haier redirects to its custom-scheme callback.

    Haier's Salesforce OAuth flow ends by redirecting to
    ``hon://mobilesdk/detect/oauth/done#access_token=...`` (tokens in the
    fragment). pyhon's ``_manual_redirect`` blindly does ``aiohttp.get()`` on
    every redirect URL; modern aiohttp rejects non-HTTP schemes with
    ``NonHttpUrlClientError``, so login dies before the tokens are read.

    Parse the tokens straight out of that terminal URL instead — exactly what
    pyhon's own ``_introduce`` already does for the same callback (it calls
    ``_parse_token_data`` then raises ``HonNoAuthenticationNeeded``, which
    ``authenticate`` catches as success). Idempotent and best-effort: if pyhon's
    internals differ it leaves the library untouched.
    """
    try:
        from pyhon import exceptions
        from pyhon.connection.auth import HonAuth
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "hon: could not import pyhon auth to apply OAuth patch", exc_info=True
        )
        return
    if getattr(HonAuth, "_custom_scheme_patched", False):
        return
    _original_manual_redirect = HonAuth._manual_redirect

    async def _manual_redirect(self, url):  # type: ignore[no-untyped-def]
        if isinstance(url, str) and "oauth/done#access_token=" in url:
            # Terminal Salesforce OAuth callback with the tokens in the URL
            # fragment. Parse them, then finish auth the same way the full
            # login does next (exchange id_token for the Cognito API token)
            # before signalling completion — otherwise load_appliances() runs
            # without API auth and returns an empty list.
            self._parse_token_data(url)
            await self._api_auth()
            raise exceptions.HonNoAuthenticationNeeded()
        return await _original_manual_redirect(self, url)

    HonAuth._manual_redirect = _manual_redirect  # type: ignore[method-assign]
    setattr(HonAuth, "_custom_scheme_patched", True)
    _LOGGER.debug("hon: applied pyhon custom-scheme OAuth redirect patch")


def _patch_pyhon_appliance_list_endpoint() -> None:
    """Point pyhon's load_appliances at Haier's new appliance-list endpoint.

    Around 2026-06 Haier retired ``GET /commands/v1/appliance`` (it now returns
    an empty list) and moved the device list to
    ``POST /unified-api/v1/view/appliance-list`` with body
    ``{"deviceId": "homeassistant"}``. The returned appliance objects keep the
    same field names (nickName, macAddress, applianceTypeName, ...), so the rest
    of pyhon is unchanged. Best-effort and idempotent.
    """
    try:
        from pyhon import const
        from pyhon.connection.api import HonAPI
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "hon: could not import pyhon api to apply endpoint patch", exc_info=True
        )
        return
    if getattr(HonAPI, "_unified_appliance_list_patched", False):
        return

    async def load_appliances(self):  # type: ignore[no-untyped-def]
        url = f"{const.API_URL}/unified-api/v1/view/appliance-list"
        async with self._hon.post(url, json={"deviceId": "homeassistant"}) as response:
            result = await response.json()
        try:
            appliances = result["modules"]["applianceList"]["payload"]["appliances"]
        except (KeyError, TypeError):
            return []
        return appliances or []

    HonAPI.load_appliances = load_appliances  # type: ignore[method-assign]
    setattr(HonAPI, "_unified_appliance_list_patched", True)
    _LOGGER.debug("hon: applied unified-api appliance-list endpoint patch")


def _patch_pyhon_mqtt_watchdog() -> None:
    """Give the MQTT reconnect watchdog exponential backoff and client cleanup.

    pyhon's ``MQTTClient._watchdog`` retries on a fixed 5 s interval: while the
    connection is down it calls ``_start()`` every 5 s, and each ``_start()``
    makes an HTTP ``auth/v1/introspection`` call (``load_aws_token``). A
    sustained failure (cloud outage, or auth that never recovers) therefore
    hammers Haier's API at ~720 req/h with no backoff. It also overwrites
    ``self._client`` without stopping the previous client, leaking AWS mqtt5
    clients that keep retrying on their own.

    Replace it with a version that backs off 5 s → 300 s, resets to 5 s once
    connected, and stops the stale client before rebuilding. Best-effort and
    idempotent; if pyhon's internals differ it leaves the library untouched.
    """
    try:
        from pyhon.connection.mqtt import MQTTClient
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "hon: could not import pyhon mqtt to apply watchdog patch", exc_info=True
        )
        return
    if getattr(MQTTClient, "_watchdog_backoff_patched", False):
        return

    min_delay, max_delay = 5, 300

    async def _watchdog(self):  # type: ignore[no-untyped-def]
        delay = min_delay
        while True:
            await asyncio.sleep(delay)
            if self._connection:
                delay = min_delay
                continue
            _LOGGER.info("Restart mqtt connection (next retry in %ss)", delay)
            old = self._client
            if old is not None:
                try:
                    old.stop()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "hon: failed stopping stale mqtt client", exc_info=True
                    )
            try:
                await self._start()
                self._subscribe_appliances()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("hon: mqtt reconnect attempt failed", exc_info=True)
            delay = min(max_delay, delay * 2)

    MQTTClient._watchdog = _watchdog  # type: ignore[method-assign]
    setattr(MQTTClient, "_watchdog_backoff_patched", True)
    _LOGGER.debug("hon: applied mqtt watchdog backoff patch")


_patch_pyhon_custom_scheme_oauth()
_patch_pyhon_appliance_list_endpoint()
_patch_pyhon_mqtt_watchdog()

HON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema(vol.All(cv.ensure_list, [HON_SCHEMA]))},
    extra=vol.ALLOW_EXTRA,
)


def _preload_pyhon_appliances() -> None:
    for _, name, _ in pkgutil.iter_modules(pyhon.appliances.__path__):
        importlib.import_module(f"pyhon.appliances.{name}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = aiohttp_client.async_get_clientsession(hass)
    if (config_dir := hass.config.config_dir) is None:
        raise ValueError("Missing Config Dir")
    await hass.async_add_executor_job(_preload_pyhon_appliances)
    hon = await Hon(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        mobile_id=MOBILE_ID,
        session=session,
        test_data_path=Path(config_dir),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
    ).create()

    # Save the new refresh token
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REFRESH_TOKEN: hon.api.auth.refresh_token}
    )

    coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
        hass, _LOGGER, name=DOMAIN
    )

    def _notify(data: Any) -> None:
        hass.loop.call_soon_threadsafe(coordinator.async_set_updated_data, data)

    hon.subscribe_updates(_notify)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.unique_id] = {"hon": hon, "coordinator": coordinator}

    async_register_services(hass)

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    refresh_token = hass.data[DOMAIN][entry.unique_id]["hon"].api.auth.refresh_token

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REFRESH_TOKEN: refresh_token}
    )
    unload_ok = bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))
    if unload_ok:
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload_ok
