"""Regression tests for the household Xiaomi Scale HA fork.

Pure parser/ledger tests run locally. HA runtime/config tests also run inside
HA's Python environment, using fake storage/notifications and no real readings.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch, PropertyMock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components/xiaomi_scale_ha'
pkg = types.ModuleType('scale_under_test')
pkg.__path__ = [str(COMPONENT)]
sys.modules[pkg.__name__] = pkg
ledger_module = importlib.import_module('scale_under_test.ledger')
parser = importlib.import_module('scale_under_test.parser')
Ledger = ledger_module.Ledger
USERS = [{'NAME':'A','GT':70,'LT':90,'HA_USER_ID':'a'}, {'NAME':'B','GT':80,'LT':100,'HA_USER_ID':'b'}]
BASE = datetime.now(timezone.utc)

def payload(seconds=0, weight=85, packet='stable', **changes):
    return {'weight':weight, 'unit':'kg', 'timestamp':(BASE+timedelta(seconds=seconds)).isoformat(),
            'stabilized':True, 'impedance':None, 'load_removed':False,
            'packet_id':packet, 'source_address':'AA:BB:CC:DD:EE:FF', 'rssi':-85, **changes}

class LedgerTests(unittest.TestCase):
    def pending(self):
        ledger = Ledger()
        record,_ = ledger.observe(payload())
        ledger.finalize(record['measurement_id'], USERS)
        return ledger, record

    def test_overlap_pending_then_first_claim_wins(self):
        ledger,r = self.pending()
        self.assertEqual(r['candidates'],['A','B'])
        self.assertTrue(ledger.resolve(r['measurement_id'],'A','claim',payload(10)['timestamp']))
        self.assertIsNone(ledger.resolve(r['measurement_id'],'B','claim',payload(11)['timestamp']))
        self.assertEqual(r['user_name'],'A')

    def test_decline_does_not_assign_other_person(self):
        ledger,r = self.pending()
        ledger.resolve(r['measurement_id'],'A','decline',payload(10)['timestamp'])
        self.assertEqual(r['status'],'pending')
        self.assertIsNone(ledger.resolve(r['measurement_id'],'A','claim',payload(11)['timestamp']))
        ledger.resolve(r['measurement_id'],'B','decline',payload(12)['timestamp'])
        self.assertEqual(r['status'],'pending')

    def test_later_identical_weight_is_separate(self):
        ledger,r = self.pending()
        ledger.observe(payload(5,packet='off',load_removed=True))
        later,new = ledger.observe(payload(20,packet='new_stable'))
        self.assertTrue(new)
        self.assertNotEqual(r['measurement_id'],later['measurement_id'])
        self.assertEqual(len(ledger.records),2)

    def test_impedance_and_step_off_merge_one_weigh_in(self):
        ledger,r = self.pending()
        updated,new = ledger.observe(payload(4,packet='impedance',impedance=500))
        self.assertFalse(new)
        self.assertEqual(updated['measurement_id'],r['measurement_id'])
        ledger.observe(payload(7,packet='off',load_removed=True))
        self.assertEqual(len(ledger.records),1)
        self.assertEqual(r['impedance'],500)

    def test_packet_replay_ignored_after_restart(self):
        ledger,r = self.pending()
        restarted = Ledger(deepcopy(ledger.records))
        self.assertEqual(restarted.observe(payload(100)),(None,False))
        self.assertEqual(restarted.records[0]['status'],'pending')

    def test_old_notification_cannot_claim_new_record(self):
        ledger,r = self.pending()
        ledger.resolve(r['measurement_id'],'A','claim',payload(10)['timestamp'])
        newer,_ = ledger.observe(payload(120,packet='new'))
        ledger.finalize(newer['measurement_id'],USERS)
        self.assertIsNone(ledger.resolve(r['measurement_id'],'B','claim',payload(121)['timestamp']))
        self.assertEqual(newer['status'],'pending')

    def test_expired_button_requires_manual_resolution(self):
        ledger,r = self.pending()
        self.assertIsNone(ledger.resolve(r['measurement_id'],'A','claim',payload(86401)['timestamp']))
        self.assertTrue(ledger.resolve(r['measurement_id'],'A','claim',payload(86401)['timestamp'],manual=True))

    def test_no_users_keeps_reading_and_admin_can_assign_later(self):
        ledger = Ledger(); r,_ = ledger.observe(payload())
        ledger.finalize(r['measurement_id'],[])
        self.assertEqual(r['status'],'pending')
        self.assertTrue(ledger.resolve(r['measurement_id'],'A','claim',payload(10)['timestamp'],manual=True))

    def test_matching_learns_confirmed_readings_and_detects_overlap(self):
        users = [{'NAME':'A'}, {'NAME':'B'}]
        ledger = Ledger()
        first,_ = ledger.observe(payload(weight=75))
        ledger.finalize(first['measurement_id'], users)
        self.assertEqual(first['candidates'], ['A','B'])
        ledger.resolve(first['measurement_id'], 'A', 'claim', payload(1)['timestamp'])
        second,_ = ledger.observe(payload(120,weight=76,packet='second'))
        ledger.finalize(second['measurement_id'], users)
        self.assertEqual(second['status'], 'assigned')
        self.assertEqual(second['user_name'], 'A')
        third,_ = ledger.observe(payload(240,weight=78,packet='third'))
        ledger.finalize(third['measurement_id'], users)
        self.assertEqual(third['status'], 'pending')
        ledger.resolve(third['measurement_id'], 'B', 'claim', payload(241)['timestamp'])
        overlap,_ = ledger.observe(payload(360,weight=76.5,packet='overlap'))
        ledger.finalize(overlap['measurement_id'], users)
        self.assertEqual(overlap['status'], 'pending')
        self.assertEqual(overlap['candidates'], ['A','B'])

    def test_single_match_assigns_and_outside_ranges_requires_confirmation(self):
        for weight,status in [(75,'assigned'),(105,'pending')]:
            ledger = Ledger();r,_=ledger.observe(payload(weight=weight))
            ledger.finalize(r['measurement_id'],USERS)
            self.assertEqual(r['status'],status)

class ParserTests(unittest.TestCase):
    def parse(self, raw):
        return parser.parse_service_info(types.SimpleNamespace(address='AA:BB:CC:DD:EE:FF',rssi=-85,
            service_data={parser.MISCALE_V2_UUID:bytes.fromhex(raw)}))
    def test_synthetic_packet_and_unset_clock(self):
        r = self.parse('02 24 b2 07 03 04 17 37 23 00 00 98 3a')
        self.assertEqual(r.weight,75.0)
        self.assertTrue(r.stabilized)
        self.assertIsNone(r.impedance)
        self.assertGreaterEqual(datetime.fromisoformat(r.timestamp).year,2026)
    def test_step_off_and_invalid_impedance(self):
        r = self.parse('02 a6 b2 07 03 04 17 37 28 fd ff 98 3a')
        self.assertTrue(r.load_removed)
        self.assertIsNone(r.impedance)
    def test_short_packet_ignored(self):
        self.assertIsNone(self.parse('02 24'))
    def test_valid_impedance(self):
        self.assertEqual(self.parse('02 26 b2 07 03 04 17 37 23 f4 01 98 3a').impedance,500)

HAS_HA = importlib.util.find_spec('homeassistant') is not None
@unittest.skipUnless(HAS_HA,'run inside HA Python for runtime and config checks')
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mod = importlib.import_module('scale_under_test.runtime')
        self.storage = types.SimpleNamespace(async_load=AsyncMock(return_value=None),async_save=AsyncMock())
        self.patches = [patch.object(self.mod,'Store',return_value=self.storage),patch.object(self.mod,'async_dispatcher_send')]
        for p in self.patches:p.start()
        self.hass=types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock()),bus=types.SimpleNamespace(async_fire=Mock()),async_create_task=asyncio.create_task)
        entry=types.SimpleNamespace(entry_id='entry',data={'name':'Scale','mac':'AA:BB:CC:DD:EE:FF','users':deepcopy(USERS)},options={})
        self.runtime=self.mod.XiaomiScaleRuntime(self.hass,entry)
        await self.runtime.async_setup()
        r,_=self.runtime.ledger.observe(payload());self.runtime.ledger.finalize(r['measurement_id'],USERS)
        self.mid=r['measurement_id']
    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
    async def test_simultaneous_claims(self):
        outcomes=await asyncio.gather(self.runtime._resolve(self.mid,'A','claim'),self.runtime._resolve(self.mid,'B','claim'))
        self.assertEqual(outcomes.count(True),1)
        self.assertEqual(len(self.runtime.history.data),1)
    async def test_wrong_phone_cannot_claim(self):
        action=f'xiaomi_scale_ha:entry:{self.mid}:claim:0'
        self.assertFalse(await self.runtime.async_handle_mobile_action(action,user_id='b'))
        self.assertTrue(await self.runtime.async_handle_mobile_action(action,user_id='a'))
    async def test_dashboard_response_uses_authenticated_identity(self):
        with self.assertRaises(self.mod.HomeAssistantError):
            await self.runtime.async_respond_pending(self.mid, 'claim', None)
        with self.assertRaises(self.mod.HomeAssistantError):
            await self.runtime.async_respond_pending(self.mid, 'claim', 'unknown')
        await self.runtime.async_respond_pending(self.mid, 'claim', 'b')
        self.assertEqual(self.runtime.ledger.get(self.mid)['user_name'], 'B')
        with self.assertRaises(self.mod.HomeAssistantError):
            await self.runtime.async_respond_pending(self.mid, 'claim', 'a')

    async def test_dashboard_decline_and_expired_claim(self):
        await self.runtime.async_respond_pending(self.mid, 'decline', 'a')
        self.assertEqual(self.runtime.ledger.get(self.mid)['status'], 'pending')
        with self.assertRaises(self.mod.HomeAssistantError):
            await self.runtime.async_respond_pending(self.mid, 'claim', 'a')
        self.runtime.ledger.get(self.mid)['timestamp'] = payload(-86401)['timestamp']
        with self.assertRaises(self.mod.HomeAssistantError):
            await self.runtime.async_respond_pending(self.mid, 'claim', 'b')

    async def test_storage_failure_rolls_back_ownership(self):
        self.storage.async_save.side_effect=OSError('disk failure')
        with self.assertRaises(OSError):await self.runtime._resolve(self.mid,'A','claim')
        self.assertEqual(self.runtime.ledger.get(self.mid)['status'],'pending')
        self.hass.bus.async_fire.assert_not_called()
    async def test_profile_parser_supports_weight_only_and_empty(self):
        flow=importlib.import_module('scale_under_test.config_flow')
        self.assertEqual(flow._parse_users('[]'),[])
        self.assertEqual(flow._parse_users('[{"NAME":"A","GT":70,"LT":90}]')[0]['NAME'],'A')
        with self.assertRaises(ValueError):flow._parse_users('[{"NAME":"A","GT":90,"LT":70}]')
        with self.assertRaises(ValueError):flow._parse_users('[{"NAME":"A"},{"NAME":"A"}]')
    async def test_profile_birthdate_roundtrip_and_numeric_wait(self):
        flowmod=importlib.import_module('scale_under_test.config_flow')
        flow=flowmod.XiaomiScaleHaOptionsFlow()
        flow._profile_name='A'
        entry=types.SimpleNamespace(options={'users':[{'NAME':'A','DOB':'1985-01-02','HEIGHT':180,'SEX':'male'}]},data={})
        with patch.object(type(flow),'config_entry',new_callable=PropertyMock,return_value=entry):
            form=await flow.async_step_profile()
            defaults={key.schema:key.default() for key in form['data_schema'].schema if key.default is not __import__('voluptuous').UNDEFINED}
            self.assertEqual(defaults['birth_year'],1985)
            self.assertEqual(defaults['birth_month'],'1')
            self.assertEqual(defaults['birth_day'],2)
            result=await flow.async_step_profile({'manual_range':False,'birth_year':1984,'birth_month':'2','birth_day':29,'HEIGHT':175,'SEX':'male'})
            self.assertEqual(result['data']['users'][0]['DOB'],'1984-02-29')
            invalid=await flow.async_step_profile({'manual_range':False,'birth_year':1985,'birth_month':'2','birth_day':29})
            self.assertEqual(invalid['errors']['base'],'invalid_profile')
            settings=await flow.async_step_settings()
            selector=next(value for key,value in settings['data_schema'].schema.items() if key.schema=='impedance_wait_seconds')
            self.assertEqual(selector.config['mode'],'box')

    async def test_personal_profile_changes_only_authenticated_person(self):
        dashboard=importlib.import_module('scale_under_test.dashboard')
        self.hass.config_entries=types.SimpleNamespace(async_update_entry=Mock())
        with self.assertRaises(self.mod.HomeAssistantError):
            await dashboard.update_personal_profile(self.runtime,'unknown',{'HEIGHT':175})
        with self.assertRaises(__import__('voluptuous').Invalid):
            await dashboard.update_personal_profile(self.runtime,'a',{'HA_USER_ID':'b'})
        with self.assertRaises(ValueError):
            await dashboard.update_personal_profile(self.runtime,'a',{'DOB':'1985-02-29'})
        self.hass.config_entries.async_update_entry.assert_not_called()
        result=await dashboard.update_personal_profile(self.runtime,'a',{'HEIGHT':175,'DOB':'1984-02-29','SEX':'male'})
        self.assertEqual(result,{'NAME':'A','HEIGHT':175,'DOB':'1984-02-29','SEX':'male'})
        users=self.hass.config_entries.async_update_entry.call_args.kwargs['options']['users']
        self.assertEqual(users[0]['HA_USER_ID'],'a')
        self.assertEqual(users[1],USERS[1])

    async def test_dashboard_uses_registered_entity_ids_and_profile_bindings(self):
        dashboard=importlib.import_module('scale_under_test.dashboard')
        registry=types.SimpleNamespace(async_get_entity_id=Mock(side_effect=lambda domain,platform,unique:'sensor.renamed_'+unique))
        with patch.object(dashboard.er,'async_get',return_value=registry):
            view=await dashboard.ScaleDashboard(self.runtime,'xiaomi-scale').async_load()
        card=view['views'][0]['cards'][0]
        self.assertEqual(card['pending_entity'],'sensor.renamed_entry_pending')
        self.assertEqual(card['people'][0]['weight_entity'],'sensor.renamed_entry_a_weight')
        self.assertEqual(card['people'][0]['user_id'],'a')
        self.assertNotIn('DOB',card['people'][0])

    async def test_impedance_backfills_but_weight_only_has_no_body_fat(self):
        record=self.runtime.ledger.get(self.mid)
        record.update(status='assigned',user_name='A',impedance=500,has_impedance=True)
        self.runtime._refresh()
        self.assertNotIn('body_fat',self.runtime.last_metrics_by_user['A'])
        self.runtime.users[0].update(HEIGHT=180,DOB='1985-01-01',SEX='male')
        await self.runtime._mutate(lambda:[self.runtime._metrics(r) for r in self.runtime.ledger.records])
        fat=self.runtime.last_metrics_by_user['A']['body_fat']
        self.assertGreater(fat,0)
        self.assertLess(fat,100)
        self.assertEqual(record['impedance'],500)
        later,_=self.runtime.ledger.observe(payload(120,weight=86,packet='socks'))
        self.runtime.ledger.finalize(later['measurement_id'],USERS)
        await self.runtime._resolve(later['measurement_id'],'A','claim',manual=True)
        self.assertEqual(self.runtime.latest_by_user['A']['weight'],86)
        self.assertNotIn('body_fat',self.runtime.last_metrics_by_user['A'])
        self.assertNotIn(('A','body_fat'),self.runtime.last_metric_records)
        self.assertEqual(self.runtime.ledger.get(self.mid)['body_fat'],fat)
        self.assertEqual(self.runtime.ledger.get(self.mid)['impedance'],500)
        self.assertIsNone(self.runtime.ledger.get(later['measurement_id'])['impedance'])
        saved=deepcopy(self.storage.async_save.call_args.args[0]['records'])
        self.storage.async_load.return_value = {'records':saved}
        await self.runtime.async_setup()
        self.assertNotIn('body_fat',self.runtime.last_metrics_by_user['A'])
        self.assertEqual(saved[0]['impedance'],500)
        self.assertIsNone(saved[1]['impedance'])

if __name__=='__main__':unittest.main()
