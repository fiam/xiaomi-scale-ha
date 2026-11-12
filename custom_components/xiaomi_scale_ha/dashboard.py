"""Integration-owned dashboard and authenticated personal profile editing."""
from copy import deepcopy
import json

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import frontend, websocket_api
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.dashboard import LovelaceConfig, cached_json_fragment
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.core import callback
from .const import DOMAIN, CONF_USERS
from .metrics import METRIC_KEYS


def measurement_history(runtime, user_name):
    """Return chart values from the ledger, including recalculated old readings."""
    fields = METRIC_KEYS | {'measurement_id', 'timestamp', 'weight', 'unit', 'has_impedance'}
    return [{key: value for key, value in record.items() if key in fields}
            for record in runtime.ledger.records
            if record['status'] == 'assigned' and record.get('user_name') == user_name]


@websocket_api.websocket_command({
    vol.Required('type'): 'xiaomi_scale_ha/history',
    vol.Required('entry_id'): str,
    vol.Required('user_name'): str,
})
@websocket_api.async_response
async def websocket_history(hass, connection, msg):
    runtime = hass.data.get(DOMAIN, {}).get(msg['entry_id'])
    if runtime is None:
        connection.send_error(msg['id'], 'not_loaded', 'The scale is unavailable')
        return
    name = msg['user_name']
    if not any(user['NAME'] == name for user in runtime.users):
        connection.send_error(msg['id'], 'not_found', 'The scale profile does not exist')
        return
    entity_id = er.async_get(hass).async_get_entity_id(
        'sensor', DOMAIN, f'{runtime.entry.entry_id}_{name.lower()}_weight')
    if not entity_id or not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        connection.send_error(msg['id'], 'unauthorized', 'You cannot view these readings')
        return
    connection.send_result(msg['id'], {'measurements': measurement_history(runtime, name)})

PROFILE_FIELDS = vol.Schema({
    vol.Optional('HEIGHT'): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=30,max=250))),
    vol.Optional('DOB'): vol.Any(None, str),
    vol.Optional('SEX'): vol.Any(None, vol.In(['male','female'])),
})


def personal_profile(runtime, user_id):
    matches = [u for u in runtime.users if user_id and u.get('HA_USER_ID') == user_id]
    if len(matches) != 1:
        raise HomeAssistantError('Your HA account must be linked to one scale profile')
    return matches[0]


async def update_personal_profile(runtime, user_id, changes):
    from .config_flow import _parse_users
    current = personal_profile(runtime, user_id)
    changes = PROFILE_FIELDS(changes)
    updated = {**current, **changes}
    for key in ('HEIGHT','DOB','SEX'):
        if updated.get(key) is None:
            updated.pop(key, None)
    _parse_users(json.dumps([updated]))
    users = [updated if u['NAME'] == current['NAME'] else deepcopy(u) for u in runtime.users]
    runtime.hass.config_entries.async_update_entry(
        runtime.entry, options={**runtime.entry.options, CONF_USERS:users})
    # The entry's update listener reloads sensors and recalculates stored readings.
    return {k:v for k,v in updated.items() if k in ('NAME','HEIGHT','DOB','SEX')}


@websocket_api.websocket_command({
    vol.Required('type'): 'xiaomi_scale_ha/profile',
    vol.Required('entry_id'): str,
    vol.Optional('changes'): PROFILE_FIELDS,
})
@websocket_api.async_response
async def websocket_profile(hass, connection, msg):
    runtime = hass.data.get(DOMAIN, {}).get(msg['entry_id'])
    if runtime is None:
        connection.send_error(msg['id'], 'not_loaded', 'The scale is unavailable')
        return
    try:
        if 'changes' in msg:
            result = await update_personal_profile(runtime, connection.user.id, msg['changes'])
        else:
            result = {k:v for k,v in personal_profile(runtime, connection.user.id).items()
                      if k in ('NAME','HEIGHT','DOB','SEX')}
    except (HomeAssistantError, ValueError, TypeError, vol.Invalid) as err:
        connection.send_error(msg['id'], 'invalid_profile', str(err))
    else:
        connection.send_result(msg['id'], result)


class ScaleDashboard(LovelaceConfig):
    """Generate config from profiles and entity registry; no copied dashboard JSON."""
    def __init__(self, runtime, path):
        self.runtime = runtime
        super().__init__(runtime.hass, path, {'id':path,'url_path':path,'title':runtime.name,
            'icon':'mdi:scale-bathroom','show_in_sidebar':True,'require_admin':False})

    @property
    def mode(self):
        # HA treats generated dashboards like YAML dashboards: render, don't edit.
        return 'yaml'

    async def async_get_info(self):
        return {'mode':self.mode}

    async def async_load(self, force=False):
        registry = er.async_get(self.hass)
        entry_id = self.runtime.entry.entry_id
        def entity(unique):
            return registry.async_get_entity_id('sensor', DOMAIN, f'{entry_id}_{unique}')
        people = [{'name':u['NAME'],'user_id':u.get('HA_USER_ID'),
                   'weight_entity':entity(u['NAME'].lower()+'_weight'),
                   'bmi_entity':entity(u['NAME'].lower()+'_bmi'),
                   'body_fat_entity':entity(u['NAME'].lower()+'_body_fat')} for u in self.runtime.users]
        return {'title':self.runtime.name,'views':[{'title':'Scale','path':'overview',
            'icon':'mdi:scale-bathroom','type':'panel','cards':[{
                'type':'custom:xiaomi-scale-card','entry_id':entry_id,
                'pending_entity':entity('pending'),'history_entity':entity('history'),'people':people}]}]}

    async def async_json(self, force=False):
        return cached_json_fragment(await self.async_load(force))


async def async_setup_dashboard(runtime):
    hass = runtime.hass
    data = hass.data[LOVELACE_DATA]
    path = runtime.entry.options.get('dashboard_path', 'household-scale')
    existing = data.dashboards.get(path)
    if existing is not None and not (isinstance(existing, ScaleDashboard) and existing.runtime.entry.entry_id == runtime.entry.entry_id):
        # Never replace an unrelated dashboard, including a manual one with this URL.
        path = f'xiaomi-scale-{runtime.entry.entry_id.lower()}'
    dashboard = ScaleDashboard(runtime, path)
    data.dashboards[path] = dashboard
    runtime.dashboard_path = path
    frontend.async_register_built_in_panel(hass, 'lovelace', sidebar_title='Scale',
        sidebar_icon='mdi:scale-bathroom', frontend_url_path=path,
        config={'mode':'yaml'}, update=isinstance(existing, ScaleDashboard))
    dashboard._config_updated()

    @callback
    def registry_updated(event):
        dashboard._config_updated()
    runtime.entry.async_on_unload(hass.bus.async_listen('entity_registry_updated',registry_updated))

    @callback
    def remove():
        if data.dashboards.get(path) is dashboard:
            data.dashboards.pop(path)
            frontend.async_remove_panel(hass,path)
    runtime.entry.async_on_unload(remove)
