"""Serve the card and register it with Lovelace's resource loader."""
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.loader import async_get_integration

from .const import DOMAIN

CARD_URL = '/xiaomi_scale_ha/scale-card.js'


async def async_register_card_resource(hass, version):
    """Keep one versioned module in the resource list, preserving other cards.

    extra_module_url alone misses clients using a cached frontend HTML shell.
    Lovelace fetches this list over its authenticated connection even for those
    clients, and upgrades missing custom elements when the module finishes loading.
    """
    url = f'{CARD_URL}?v={version}'
    resources = hass.data[LOVELACE_DATA].resources
    await resources.async_get_info()  # Load stored resources before inspecting them.
    matches = [item for item in resources.async_items()
               if item['url'].split('?', 1)[0] == CARD_URL]
    if isinstance(resources, ResourceStorageCollection):
        if matches:
            current = matches[0]
            if current['url'] != url or current['type'] != 'module':
                await resources.async_update_item(current['id'],
                    {'url': url, 'res_type': 'module'})
            for duplicate in matches[1:]:
                await resources.async_delete_item(duplicate['id'])
        else:
            await resources.async_create_item({'url': url, 'res_type': 'module'})
    else:
        # YAML resources are in memory; never rewrite the user's YAML file.
        resources.data[:] = [item for item in resources.async_items() if item not in matches]
        resources.data.append({'url': url, 'type': 'module'})
        # Also support a later YAML resource reload that replaces the collection.
        frontend.add_extra_js_url(hass, url)


async def async_setup_frontend(hass):
    await hass.http.async_register_static_paths([
        StaticPathConfig(CARD_URL, str(Path(__file__).parent / 'frontend/scale-card.js'), False)
    ])
    integration = await async_get_integration(hass, DOMAIN)
    await async_register_card_resource(hass, integration.version)
