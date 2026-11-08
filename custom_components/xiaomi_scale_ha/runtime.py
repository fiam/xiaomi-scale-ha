"""Household workflow built on Xiaomi Scale HA's decoder and sensors.

All ownership decisions and received records share one persistent HA Store.
Phone actions identify a measurement and candidate; an asyncio lock serializes claims.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from .const import DOMAIN, CONF_MAC, CONF_USERS, SIGNAL_MEASUREMENT, EVENT_ASSIGNED, EVENT_PENDING, EVENT_DISCARDED
from .ledger import Ledger, seconds_between
from .metrics import build_metrics, METRIC_KEYS

_LOGGER = logging.getLogger(__name__)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def public_record(record):
    return {k:v for k,v in record.items() if k not in ('packet_ids', 'closed', 'last_seen')}

class HistoryView:
    def __init__(self, runtime):
        self.runtime = runtime
    @property
    def data(self):
        users = {}
        for r in self.runtime.ledger.records:
            if r['status'] == 'assigned':
                users.setdefault(r['user_name'], []).append(public_record(r))
        return users
    def summary(self):
        return {name:{'count':len(rows), 'measurements':rows[-10:]} for name,rows in self.data.items()}

class XiaomiScaleRuntime:
    def __init__(self, hass: HomeAssistant, entry):
        self.hass, self.entry = hass, entry
        self.name = entry.data['name']
        self.mac = entry.data[CONF_MAC].upper()
        self.users = entry.options.get(CONF_USERS, entry.data.get(CONF_USERS, []))
        self.store = Store(hass, 1, f'{DOMAIN}_{entry.entry_id}_records')
        self.ledger = Ledger()
        self.history = HistoryView(self)
        self.lock = asyncio.Lock()
        self.latest_by_user = {}
        self.last_metrics_by_user = {}
        self.last_metric_records = {}
        self.last_diagnostic = {'state':'starting'}
        self.last_notification = {}
        self._pending_finalize = {}

    @property
    def pending_records(self):
        return [r for r in self.ledger.records if r['status'] == 'pending']

    @property
    def pending(self):
        records = self.pending_records
        if not records:
            return None
        return {**public_record(records[0]), 'pending_count':len(records),
                'measurements':[public_record(r) for r in records]}

    async def async_setup(self):
        data = await self.store.async_load() or {}
        self.ledger = Ledger(data.get('records', []))
        await self._mutate(lambda:[self._metrics(r) for r in self.ledger.records])
        for r in self.ledger.records:
            if r['status'] == 'collecting':
                self._schedule(r['measurement_id'], 0)

    def _refresh(self):
        self.latest_by_user = {}
        self.last_metrics_by_user = {}
        self.last_metric_records = {}
        for r in self.ledger.records:
            if r['status'] == 'assigned':
                self.latest_by_user[r['user_name']] = public_record(r)
                self.last_metrics_by_user[r['user_name']] = {k:r[k] for k in METRIC_KEYS if k in r}
                for key in METRIC_KEYS:
                    self.last_metric_records.pop((r['user_name'], key), None)
                for k in METRIC_KEYS:
                    if k in r: self.last_metric_records[(r['user_name'],k)] = r
        async_dispatcher_send(self.hass, SIGNAL_MEASUREMENT, self.entry.entry_id)

    async def _mutate(self, operation):
        async with self.lock:
            before = deepcopy(self.ledger.records)
            try:
                result = operation()
                if self.ledger.records != before:
                    await self.store.async_save({'records':self.ledger.records})
            except BaseException:
                self.ledger.records = before
                raise
            self._refresh()
            return deepcopy(result)

    def _metrics(self, record):
        if record and record['status'] == 'assigned':
            user = next((u for u in self.users if u['NAME']==record['user_name']), None)
            if user:
                metrics = build_metrics(record, user)
                old = {k:record[k] for k in METRIC_KEYS | {'metrics_error'} if k in record}
                if old != metrics:
                    for key in METRIC_KEYS | {'metrics_error'}:
                        record.pop(key, None)
                    record.update(metrics)
                    record["metrics_updated_at"] = utcnow()
        return record

    async def async_handle_measurement(self, measurement):
        def observe():
            record, new = self.ledger.observe(asdict(measurement))
            self._metrics(record)
            return record, new
        record, new = await self._mutate(observe)
        if not record:
            return
        self.last_diagnostic = {'state':record['status'], **public_record(record)}
        if new:
            self._schedule(record['measurement_id'], self.entry.options.get('impedance_wait_seconds', 8))
        self._refresh()

    def _schedule(self, measurement_id, delay):
        if measurement_id not in self._pending_finalize:
            self._pending_finalize[measurement_id] = self.hass.async_create_task(self._finalize_later(measurement_id, delay))

    async def _finalize_later(self, measurement_id, delay):
        try:
            await asyncio.sleep(delay)
            record = await self._mutate(lambda:self._metrics(self.ledger.finalize(measurement_id, self.users)))
            if not record:
                return
            self.last_diagnostic = {'state':record['status'], **public_record(record)}
            if record['status'] == 'assigned':
                await self._assigned(record)
            else:
                self.hass.bus.async_fire(EVENT_PENDING, {'entry_id':self.entry.entry_id, **public_record(record)})
                await self._notify_pending(record)
        finally:
            self._pending_finalize.pop(measurement_id, None)

    async def _resolve(self, measurement_id, user_name, decision, manual=False):
        record = await self._mutate(lambda:self._metrics(self.ledger.resolve(measurement_id, user_name, decision, utcnow(), manual)))
        if not record:
            return False
        if decision == 'decline':
            await self._clear_phone(record, self._user(user_name))
            self.last_diagnostic = {'state':'awaiting_confirmation' if set(record['candidates'])-set(record['declined']) else 'manual_review', **public_record(record)}
        else:
            await self._clear_notifications(record)
            if record['status'] == 'assigned':
                await self._assigned(record)
            else:
                self.hass.bus.async_fire(EVENT_DISCARDED, {'entry_id':self.entry.entry_id, **public_record(record)})
        self._refresh()
        return True

    async def async_assign_pending(self, user_name, measurement_id):
        if not self._user(user_name):
            raise HomeAssistantError('Unknown scale user')
        return await self._resolve(measurement_id, user_name, 'claim', manual=True)

    async def async_respond_pending(self, measurement_id, decision, user_id):
        # Derive identity from the authenticated service context, never client data.
        matches = [u for u in self.users if user_id and u.get('HA_USER_ID') == user_id]
        if len(matches) != 1:
            raise HomeAssistantError('Your HA account must be linked to exactly one scale profile')
        if decision not in ('claim', 'decline'):
            raise HomeAssistantError('Invalid scale response')
        if not await self._resolve(measurement_id, matches[0]['NAME'], decision):
            raise HomeAssistantError('This reading is no longer available for your confirmation. Refresh the dashboard; an administrator can resolve older readings.')

    async def async_discard_pending(self, measurement_id, reason='discarded'):
        return await self._resolve(measurement_id, None, 'discard', manual=True)

    async def async_handle_mobile_action(self, action, user_id=None, device_id=None):
        prefix = f'{DOMAIN}:{self.entry.entry_id}:'
        if not action.startswith(prefix):
            return False
        parts = action[len(prefix):].split(':')
        if len(parts) != 3:
            return False
        measurement_id, decision, index = parts
        record = self.ledger.get(measurement_id)
        if not record or decision not in ('claim', 'decline') or not index.isdigit():
            return False
        candidates = record.get('candidates', [])
        if int(index) >= len(candidates):
            return False
        user_name = candidates[int(index)]
        user = self._user(user_name)
        # Webhook events normally carry the HA user. Device binding is a fallback.
        expected = user.get('HA_USER_ID') if user else None
        expected_device = user.get('MOBILE_DEVICE_ID') if user else None
        if not ((expected and user_id == expected) or (not user_id and expected_device and device_id == expected_device)):
            _LOGGER.warning('Ignored scale confirmation from an unexpected phone/user')
            return False
        return await self._resolve(measurement_id, user_name, decision)

    async def async_handle_alexa_action(self, *args):
        return False

    def _user(self, name):
        return next((u for u in self.users if u['NAME']==name), None)

    def _tag(self, record):
        return f'{DOMAIN}_{record["measurement_id"]}'

    async def _notify(self, user, message, title='Xiaomi Scale', data=None):
        service = (user or {}).get('NOTIFY_SERVICE', '')
        if not service.startswith('notify.mobile_app_'):
            return
        try:
            await self.hass.services.async_call('notify', service.split('.',1)[1],
                {'message':message, 'title':title, 'data':data or {}}, blocking=True)
            self.last_notification = {'result':'sent', 'service':service, 'timestamp':utcnow()}
        except Exception as err:
            self.last_notification = {'result':'error', 'service':service, 'error':str(err)}
            _LOGGER.warning('Scale notification failed for %s: %s', service, err)

    async def _notify_pending(self, record):
        if self.entry.options.get('persistent_notification', True):
            await self.hass.services.async_call('persistent_notification','create',
                {'notification_id':self._tag(record), 'title':'Unassigned scale measurement',
                 'message':f'{record["weight"]:.2f} kg, measured at {record["timestamp"]}. Waiting for confirmation. Measurement: {record["measurement_id"]}.'}, blocking=True)
        if seconds_between(record['timestamp'], utcnow()) > 86400:
            return
        for index, name in enumerate(record['candidates']):
            if name in record['declined']:
                continue
            prefix = f'{DOMAIN}:{self.entry.entry_id}:{record["measurement_id"]}'
            await self._notify(self._user(name), f'{record["weight"]:.2f} kg — is this your measurement?',
                data={'tag':self._tag(record), 'group':DOMAIN, 'actions':[
                    {'action':f'{prefix}:claim:{index}', 'title':"That's me", 'authenticationRequired':True},
                    {'action':f'{prefix}:decline:{index}', 'title':'Not me', 'authenticationRequired':True}]})

    async def async_send_pending_notification(self):
        for record in deepcopy(self.pending_records):
            await self._notify_pending(record)

    async def _clear_phone(self, record, user):
        await self._notify(user, 'clear_notification', data={'tag':self._tag(record)})

    async def _clear_notifications(self, record):
        await self.hass.services.async_call('persistent_notification', 'dismiss', {'notification_id':self._tag(record)}, blocking=True)
        for name in record.get('candidates', []):
            await self._clear_phone(record, self._user(name))

    async def _assigned(self, record):
        self.last_diagnostic = {'state':'assigned', **public_record(record)}
        self.hass.bus.async_fire(EVENT_ASSIGNED, {'entry_id':self.entry.entry_id, **public_record(record)})
        if self.entry.options.get('notify_assigned', False):
            await self._notify(self._user(record['user_name']), f'{record["weight"]:.2f} kg registered for {record["user_name"]}.',
                title='Weight registered', data={'tag':self._tag(record)+'_assigned', 'group':DOMAIN})
        self._refresh()

    async def async_clear_history(self, user_name=None):
        def clear():
            self.ledger.records = [r for r in self.ledger.records if r['status'] != 'assigned' or (user_name and r['user_name'] != user_name)]
        await self._mutate(clear)

    async def async_delete_history_measurement(self, user_name, index):
        def delete():
            rows = [r for r in self.ledger.records if r['status']=='assigned' and r['user_name']==user_name]
            try:
                target = rows[index]
            except IndexError:
                return
            self.ledger.records.remove(target)
        await self._mutate(delete)
