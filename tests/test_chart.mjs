import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

// Test calculation helpers without a browser, Home Assistant or real readings.
globalThis.HTMLElement=class {};
globalThis.customElements={get:()=>true};
globalThis.window={};
const source=await readFile(new URL('../custom_components/xiaomi_scale_ha/frontend/scale-card.js',import.meta.url),'utf8');
const {normalizeReadings,trendData,chartBounds}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const DAY=86400000, now=Date.parse('2026-06-20T12:00:00Z');
const row=(days,weight,extra={})=>({measurement_id:String(days),timestamp:new Date(now-days*DAY).toISOString(),unit:'kg',weight,...extra});

test('a single historical fat reading survives later weight-only readings',()=>{
  const result=trendData([row(4,75),row(2,74,{body_fat:22}),row(0,74.5)],'body_fat',30,now);
  assert.equal(result.points.length,1);
  assert.equal(result.points[0].value,22);
  assert.equal(result.points[0].time,now-2*DAY);
  assert.equal(result.change,null);
  assert.deepEqual(result.average,[]);
  const bounds=chartBounds(result.points);
  assert.ok(bounds.start<result.points[0].time && bounds.end>result.points[0].time);
  assert.ok(bounds.low<22 && bounds.high>22);
});
test('missing estimates break lines and never become zero',()=>{
  const result=trendData([row(4,75,{body_fat:22}),row(3,74,{body_fat:null}),row(2,74.5,{body_fat:21}),row(0,74,{body_fat:'unknown'})],'body_fat',30,now);
  assert.deepEqual(result.points.map(p=>p.value),[22,21]);
  assert.deepEqual(result.segments.map(s=>s.length),[1,1]);
});
test('seven-day trend averages daily means, without overweighting repeated weigh-ins',()=>{
  const rows=[row(6,72),row(6,76),row(1,80),row(0,82)];
  const result=trendData(rows,'weight',30,now);
  assert.equal(result.average[0].value,77);
  assert.equal(result.average[1].value,(74+80+82)/3);
  const later=trendData([...rows,row(-1,84)],'weight',30,now+DAY);
  assert.equal(later.average.at(-1).value,82); // The day at -6 is now outside seven days.
});
test('period filtering retains warm-up data for the rolling average',()=>{
  const rows=[row(20,70),row(6,72),row(1,74),row(0,76),row(-1,90)];
  const result=trendData(rows,'weight',2,now);
  assert.deepEqual(result.points.map(p=>p.value),[74,76]);
  assert.equal(result.average[0].value,73);
  assert.equal(result.change,2);
  assert.equal(trendData(rows,'weight',0,now).points.length,4);
});
test('BMI uses stored values and records sort by their original timestamp',()=>{
  const result=trendData([row(0,74,{bmi:23.1}),row(4,75,{bmi:23.4})],'bmi',30,now);
  assert.deepEqual(result.points.map(p=>p.value),[23.4,23.1]);
  assert.ok(Math.abs(result.change+0.3)<0.0001);
});
test('empty, invalid and flat data have safe chart bounds',()=>{
  assert.deepEqual(normalizeReadings([{timestamp:'bad',weight:75}]),[]);
  assert.equal(chartBounds([]),null);
  const bounds=chartBounds(trendData([row(1,75),row(0,75)],'weight',30,now).points);
  assert.ok(bounds.high>bounds.low);
  assert.ok(bounds.end>bounds.start);
});
test('weight units are normalized once while estimates remain unchanged',()=>{
  const rows=normalizeReadings([row(0,150,{unit:'jin',bmi:23.4}),row(1,165,{unit:'lbs'})]);
  assert.equal(rows[1].weight,75);
  assert.equal(rows[0].weight,165*0.4536);
  assert.equal(normalizeReadings(rows)[1].weight,75);
  assert.equal(rows[1].bmi,23.4);
});
