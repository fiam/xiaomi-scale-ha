// Xiaomi Scale dashboard. No external dependencies; identity is verified by HA.
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};
const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: digits});
const date = value => value ? new Date(value).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
const DAY = 86400000;
const METRICS = {
  weight: {label:'Weight', unit:'kg', icon:'mdi:scale-bathroom'},
  body_fat: {label:'Body fat', unit:'%', icon:'mdi:water-percent'},
  bmi: {label:'BMI', unit:'', icon:'mdi:human'},
};
const finite = value => typeof value === 'number' && Number.isFinite(value);
const svgNode = (tag, attributes = {}, text) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
  if (text !== undefined) el.textContent = text;
  return el;
};

export function normalizeReadings(records) {
  return records.filter(r => Number.isFinite(Date.parse(r.timestamp))).map(r => ({...r,
    time:Date.parse(r.timestamp),
    weight:finite(r.weight) ? r.weight * (r.unit === 'lbs' ? 0.4536 : r.unit === 'jin' ? 0.5 : 1) : null,
    unit:'kg',
  })).sort((a,b) => a.time - b.time);
}

export function trendData(records, metric, days, now = Date.now()) {
  const all = normalizeReadings(records).filter(r => r.time <= now);
  const cutoff = days ? now - days * DAY : -Infinity;
  const rows = all.filter(r => r.time >= cutoff);
  const points = rows.filter(r => finite(r[metric])).map(r => ({...r, value:r[metric]}));
  // Missing body-fat values break the reading line; they never become zeroes.
  const segments = []; let segment = [];
  for (const row of rows) {
    if (finite(row[metric])) segment.push({time:row.time, value:row[metric]});
    else if (segment.length) { segments.push(segment); segment = []; }
  }
  if (segment.length) segments.push(segment);
  // Average daily means, so multiple weigh-ins in a day don't skew the trend.
  const daily = new Map();
  for (const row of all) {
    if (!finite(row[metric])) continue;
    const day = Math.floor(row.time / DAY);
    const bucket = daily.get(day) || {day, values:[]};
    bucket.values.push(row[metric]); bucket.time = row.time; daily.set(day,bucket);
  }
  const means = [...daily.values()].map(d => ({...d, value:d.values.reduce((a,b)=>a+b,0)/d.values.length}));
  const average = []; let start = 0, sum = 0;
  means.forEach((day,index) => {
    sum += day.value;
    while (means[start].day < day.day-6) sum -= means[start++].value;
    const count = index-start+1;
    if (day.time >= cutoff && count >= 2) average.push({time:day.time,value:sum/count});
  });
  const averageSegments = [];
  for (const point of average) {
    const last = averageSegments.at(-1);
    if (!last || point.time - last.at(-1).time >= 7*DAY) averageSegments.push([point]);
    else last.push(point);
  }
  return {points,segments,average,averageSegments,
    change:points.length > 1 ? points.at(-1).value-points[0].value : null};
}

export function chartBounds(points, average = []) {
  const times = points.map(p=>p.time);
  const values = [...points,...average].map(p=>p.value);
  if (!times.length) return null;
  const first = Math.min(...times), last = Math.max(...times);
  const timePadding = Math.max((last-first)*0.04, DAY/4);
  const low = Math.min(...values), high = Math.max(...values);
  const valuePadding = Math.max((high-low)*0.2,0.5);
  return {start:first-timePadding, end:last+timePadding, low:low-valuePadding, high:high+valuePadding};
}

class XiaomiScaleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:'open'});
    this._expanded = new Set();
    this._busy = false;
    this._historyCache = new Map();
    this._trendMetric = 'weight';
    this._trendDays = 30;
    this._plotWidth = 720;
  }
  connectedCallback() {
    this._resizeObserver = new ResizeObserver(entries=>{
      const available=entries[0].contentRect.width;
      const width=Math.max(280,Math.round(Math.min(900,available)-(available<=600?48:64)));
      if (this._plotWidth!==width) {this._plotWidth=width;this._render();}
    });
    this._resizeObserver.observe(this);
  }
  disconnectedCallback() {this._resizeObserver?.disconnect();}
  setConfig(config) {
    if (!config.pending_entity || !config.history_entity || !Array.isArray(config.people)) throw new Error('Configure pending_entity, history_entity and people');
    this._config = config;
    this._historyCache.clear();
    this._renderKey = null;
    this._render();
  }
  set hass(hass) {
    if (this._hass?.user?.id !== hass.user?.id) this._historyCache.clear();
    this._hass = hass;
    const c = this._config;
    if (!c) return;
    const entities = [c.pending_entity,c.history_entity,...c.people.flatMap(p => [p.weight_entity,p.body_fat_entity,p.bmi_entity])];
    const key = JSON.stringify([hass.user?.id,...entities.map(id => hass.states[id])]);
    if (key !== this._renderKey) { this._renderKey = key; this._render(); }
  }
  getCardSize() { return 12; }
  getGridOptions() { return {columns:12, rows:'auto'}; }
  _button(label, action, primary = false) {
    const button = node('ha-button',label);
    button.setAttribute('appearance', primary ? 'filled' : 'plain');
    button.type = 'button';
    button.disabled = this._busy;
    button.addEventListener('click', action);
    return button;
  }
  async _respond(record, decision, person) {
    this._busy = true; this._error = ''; this._notice = ''; this._render();
    try {
      const data = {measurement_id:record.measurement_id};
      if (this._config.entry_id) data.entry_id = this._config.entry_id;
      if (person) data.user_name = person;
      else data.decision = decision;
      await this._hass.callService('xiaomi_scale_ha', person ? 'assign_pending' : 'respond_pending', data);
      this._notice = decision === 'claim' ? 'Measurement saved.' : 'Marked as not yours.';
    } catch (error) { this._error = error.message || String(error); }
    finally { this._busy = false; this._render(); }
  }
  async _openProfile() {
    try {
      const profile = await this._hass.callWS({type:'xiaomi_scale_ha/profile',entry_id:this._config.entry_id});
      const host = node('div'); const shadow = host.attachShadow({mode:'open'});
      const style = node('style');
      style.textContent = `
        dialog{box-sizing:border-box;width:min(500px,calc(100vw - 32px));max-height:calc(100dvh - 32px);overflow:auto;border:1px solid var(--divider-color);border-radius:var(--ha-dialog-border-radius,28px);padding:24px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#212121);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
        dialog::backdrop{background:rgba(0,0,0,.5)}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}h2{font-size:24px;font-weight:400;margin:0}label{display:flex;flex-direction:column;gap:8px;margin-bottom:20px;font-size:14px}input,select{box-sizing:border-box;width:100%;min-width:0;height:48px;font:inherit;font-size:16px;padding:8px 12px;background:var(--secondary-background-color,#eee);border:1px solid var(--divider-color,#ccc);border-radius:4px;color:inherit}fieldset{padding:0;border:0;margin:0}legend{margin-bottom:12px;font-size:14px}.date{display:grid;grid-template-columns:1fr 1.6fr 1.2fr;gap:12px}footer{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}button{font:inherit;cursor:pointer;border:0;border-radius:24px;background:none;color:var(--primary-color,#03a9f4);padding:10px 20px;min-height:44px}.save{background:var(--primary-color,#03a9f4);color:var(--text-primary-color,#fff)}.close{font-size:26px;padding:0 8px}.error{font-size:14px;color:var(--error-color,#db4437);margin-bottom:12px}:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}
      `;
      const dialog = node('dialog');dialog.setAttribute('aria-label',`${profile.NAME}’s profile`);
      const heading = node('header');heading.append(node('h2',`${profile.NAME}’s profile`));
      const close = node('button','×','close');close.type='button';close.setAttribute('aria-label','Close profile');heading.append(close);dialog.append(heading);
      const form = node('form');
      const field = (title,type,value,min,max) => { const label=node('label',title);const input=node('input');input.type=type;input.value=value ?? '';if(min!=null)input.min=min;if(max!=null)input.max=max;label.append(input);return [label,input]; };
      const [heightLabel,height]=field('Height (cm)','number',profile.HEIGHT,30,250);height.step='1';form.append(heightLabel);
      const birth=node('fieldset');birth.append(node('legend','Date of birth'));const dateRow=node('div',undefined,'date');
      const parts=(profile.DOB || '').split('-');
      const [dayLabel,day]=field('Day','number',parts[2],1,31);
      const monthLabel=node('label','Month');const month=node('select');monthLabel.append(month);
      ['', 'January','February','March','April','May','June','July','August','September','October','November','December'].forEach((name,i)=>{const opt=node('option',name||'—');opt.value=i ? String(i) : '';month.append(opt);});month.value=parts[1] ? String(Number(parts[1])) : '';
      const [yearLabel,year]=field('Year','number',parts[0],1900,new Date().getFullYear());
      dateRow.append(dayLabel,monthLabel,yearLabel);birth.append(dateRow);form.append(birth);
      const sexLabel=node('label','Sex used by the body-fat formula');const sex=node('select');
      [['','Not set'],['male','Male'],['female','Female']].forEach(([value,label])=>{const opt=node('option',label);opt.value=value;sex.append(opt);});sex.value=profile.SEX || '';sexLabel.append(sex);form.append(sexLabel);
      const error=node('div',undefined,'error');error.setAttribute('role','alert');form.append(error);
      const footer=node('footer');const cancel=node('button','Cancel');cancel.type='button';const save=node('button','Save','save');save.type='submit';footer.append(cancel,save);form.append(footer);dialog.append(form);
      const cleanup=()=>{dialog.close();host.remove();};close.onclick=cleanup;cancel.onclick=cleanup;dialog.addEventListener('cancel',()=>host.remove());
      form.addEventListener('submit',async event=>{
        event.preventDefault();error.textContent='';
        const hasDate = day.value || month.value || year.value;
        if (hasDate && !(day.value && month.value && year.value)) {error.textContent='Enter the day, month and year.';return;}
        const dob=hasDate ? `${year.value.padStart(4,'0')}-${month.value.padStart(2,'0')}-${day.value.padStart(2,'0')}` : null;
        save.disabled=true;
        try {
          await this._hass.callWS({type:'xiaomi_scale_ha/profile',entry_id:this._config.entry_id,
            changes:{HEIGHT:height.value ? Number(height.value) : null,DOB:dob,SEX:sex.value || null}});
          cleanup();this._notice='Profile saved.';this._render();
        } catch(err) {error.textContent=err.message || String(err);save.disabled=false;}
      });
      shadow.append(style,dialog);document.body.append(host);dialog.showModal();
    } catch(error) {this._error=error.message || String(error);this._render();}
  }
  _rowsFor(person, history, load) {
    const recent = history[person.name]?.measurements || [];
    if (!this._config.entry_id || !load) return normalizeReadings(recent);
    const key = JSON.stringify(history[person.name] || null);
    let cached = this._historyCache.get(person.name);
    if (!cached || cached.key !== key) {
      const request = {key, rows:cached?.rows, loading:true};
      this._historyCache.set(person.name,request); cached = request;
      this._hass.callWS({type:'xiaomi_scale_ha/history',entry_id:this._config.entry_id,user_name:person.name})
        .then(result => {
          if (this._historyCache.get(person.name) !== request) return;
          request.rows = normalizeReadings(result.measurements); request.loading = false; this._render();
        }).catch(error => {
          if (this._historyCache.get(person.name) !== request) return;
          request.error = error.message || 'Could not load history.'; request.loading = false; this._render();
        });
    }
    return cached.rows || normalizeReadings(recent);
  }
  _metric(key, value, subtitle) {
    const {label,unit,icon:iconName} = METRICS[key];
    const box = node('div',undefined,'metric');
    const labelRow = node('div',undefined,'label');
    const icon = node('ha-icon'); icon.setAttribute('icon',iconName);
    labelRow.append(icon,node('span',label));box.append(labelRow);
    const amount = node('div',number(value),'amount');
    if (unit && value != null) amount.append(node('span',unit,'unit'));
    box.append(amount);
    if (subtitle) box.append(node('div',subtitle,'muted'));
    return box;
  }
  _trend(person, records) {
    const section = node('section',undefined,'trends');
    section.setAttribute('aria-label','Measurement trends');
    const heading = node('div',undefined,'trend-heading');heading.append(node('h3','Trends'));
    const rangeLabel = node('label',undefined,'range');rangeLabel.append(node('span','Period'));
    const select = node('select'); select.setAttribute('aria-label','Trend period');
    for (const [value,label] of [[7,'7 days'],[30,'30 days'],[90,'90 days'],[0,'All time']]) {
      const option = node('option',label);option.value=String(value);select.append(option);
    }
    select.value=String(this._trendDays);
    select.addEventListener('change',()=>{this._trendDays=Number(select.value);this._selectedPoint=null;this._focusControl='period';this._render();});
    rangeLabel.append(select);heading.append(rangeLabel);section.append(heading);
    const choices = node('div',undefined,'metric-choices');choices.setAttribute('role','group');choices.setAttribute('aria-label','Chart metric');
    for (const [key,metric] of Object.entries(METRICS)) {
      const button = this._button(metric.label,()=>{this._trendMetric=key;this._selectedPoint=null;this._focusControl='metric';this._render();});
      button.className='metric-choice';button.setAttribute('aria-pressed',String(this._trendMetric===key));choices.append(button);
    }
    section.append(choices);
    const metric = METRICS[this._trendMetric];
    const data = trendData(records,this._trendMetric,this._trendDays);
    const cached = this._historyCache.get(person.name);
    if (cached?.loading) section.append(node('p','Loading history…','muted'));
    if (cached?.error) {
      const error=node('div',undefined,'history-error');error.setAttribute('role','alert');
      error.append(node('span','Full history could not be loaded.'),this._button('Retry',()=>{this._historyCache.delete(person.name);this._render();}));section.append(error);
    }
    const summary = node('div',undefined,'trend-summary');
    summary.append(node('span',`${data.points.length} ${data.points.length===1?'reading':'readings'}`,'muted'));
    if (data.change != null) summary.append(node('span',`${data.change>0?'+':''}${number(data.change)}${metric.unit ? ' '+(this._trendMetric==='body_fat'?'percentage points':metric.unit):''} over period`,'muted'));
    section.append(summary);
    if (!data.points.length) {
      section.append(node('div',`No ${metric.label.toLowerCase()} readings in this period.`,'chart-empty'));
      return section;
    }
    const bounds = chartBounds(data.points,data.average);
    const width=this._plotWidth, height=250, left=45, right=15, top=18, bottom=38;
    const x=t=>left+(t-bounds.start)/(bounds.end-bounds.start)*(width-left-right);
    const y=v=>top+(bounds.high-v)/(bounds.high-bounds.low)*(height-top-bottom);
    const chart = svgNode('svg',{viewBox:`0 0 ${width} ${height}`,class:'measurement-chart',role:'group','aria-label':`${metric.label} readings. Use arrow keys to select a reading.`});
    for (let i=0;i<=3;i++) {
      const value=bounds.low+(bounds.high-bounds.low)*i/3, cy=y(value);
      chart.append(svgNode('line',{x1:left,x2:width-right,y1:cy,y2:cy,class:'grid-line'}));
      chart.append(svgNode('text',{x:left-10,y:cy+4,'text-anchor':'end',class:'axis-label'},number(value,1)));
    }
    const shortSpan = bounds.end-bounds.start < DAY*2;
    const ticks=width<460?1:2;
    for (let i=0;i<=ticks;i++) {
      const time=bounds.start+(bounds.end-bounds.start)*i/ticks;
      const label=new Date(time).toLocaleString(undefined,shortSpan ? {hour:'2-digit',minute:'2-digit'} : {month:'short',day:'numeric'});
      chart.append(svgNode('text',{x:x(time),y:height-12,'text-anchor':i===0?'start':i===ticks?'end':'middle',class:'axis-label'},label));
    }
    const drawSegments = (segments,cls) => {
      for (const points of segments) {
        if (points.length<2) continue;
        chart.append(svgNode('polyline',{points:points.map(p=>`${x(p.time)},${y(p.value)}`).join(' '),class:cls}));
      }
    };
    drawSegments(data.segments,'reading-line');drawSegments(data.averageSegments,'average-line');
    for (const point of data.average) chart.append(svgNode('circle',{cx:x(point.time),cy:y(point.value),r:3,class:'average-dot'}));
    const details = node('p',undefined,'point-detail');details.setAttribute('aria-live','polite');
    const groups=[];
    const showPoint = index => {
      const point=data.points[index];this._selectedPoint=point.measurement_id;
      details.replaceChildren(node('span',date(point.timestamp)),node('strong',`${number(point.value)}${metric.unit?' '+metric.unit:''}`));
      groups.forEach((group,i)=>{group.setAttribute('tabindex',i===index?'0':'-1');group.classList.toggle('selected',i===index);});
    };
    data.points.forEach((point,index)=>{
      const label=`${date(point.timestamp)}: ${number(point.value)} ${metric.unit}`;
      const group=svgNode('g',{class:'reading-point',role:'button',tabindex:'-1','aria-label':label});
      group.append(svgNode('title',{},label),svgNode('circle',{cx:x(point.time),cy:y(point.value),r:16,class:'point-target'}),svgNode('circle',{cx:x(point.time),cy:y(point.value),r:4.5,class:'point-dot'}));
      group.addEventListener('click',()=>showPoint(index));group.addEventListener('focus',()=>showPoint(index));
      group.addEventListener('keydown',event=>{
        let next=index;
        if (event.key==='ArrowRight') next=Math.min(index+1,groups.length-1);
        else if (event.key==='ArrowLeft') next=Math.max(index-1,0);
        else if (event.key==='Home') next=0;
        else if (event.key==='End') next=groups.length-1;
        else if (event.key!=='Enter' && event.key!==' ') return;
        event.preventDefault();showPoint(next);groups[next].focus();
      });
      groups.push(group);chart.append(group);
    });
    const selected=data.points.findIndex(p=>p.measurement_id===this._selectedPoint);
    showPoint(selected<0 ? data.points.length-1 : selected);
    section.append(chart);
    const legend=node('div',undefined,'chart-legend');legend.append(node('span','Readings','reading-key'));
    if (data.average.length) legend.append(node('span','7-day average','average-key'));
    section.append(legend,details);
    if (data.points.length===1) section.append(node('p','More readings will show a trend.','muted single-reading'));
    return section;
  }
  _person(person, own, history) {
    const stateValue = entity => {
      const state = this._hass.states[entity]?.state;
      return state != null && state !== '' && Number.isFinite(Number(state)) ? Number(state) : null;
    };
    const records = this._rowsFor(person,history,own || this._expanded.has('others'));
    const rows = [...records].reverse(), latest = rows[0];
    const card = node('ha-card',undefined,own ? 'personal' : 'person');
    const heading = node('div',undefined,'section-head');
    heading.append(node('h2',own ? 'Your measurements' : person.name));
    if (own) heading.append(node('span',person.name,'pill'));
    card.append(heading);
    const metrics = node('div',undefined,'metrics');
    const weight = latest?.weight ?? stateValue(person.weight_entity);
    // A latest weight-only reading must never show a preceding body-fat estimate.
    const fat = latest ? latest.body_fat ?? null : stateValue(person.body_fat_entity);
    const bmi = latest ? latest.bmi ?? null : stateValue(person.bmi_entity);
    metrics.append(this._metric('weight',weight,latest ? date(latest.timestamp) : 'No measurements yet'));
    metrics.append(this._metric('body_fat',fat,fat != null ? 'Estimated' : latest ? ((latest.has_impedance || latest.impedance) ? 'Complete your profile' : 'No estimate for this reading') : ''));
    metrics.append(this._metric('bmi',bmi,bmi == null ? 'Add your height in Profile settings' : ''));
    card.append(metrics);
    if (rows.length>1) {
      const delta=rows[0].weight-rows[1].weight;
      card.append(node('div',`${delta>0?'+':''}${number(delta)} kg since previous reading`,'muted change'));
    }
    if (own) card.append(this._trend(person,records));
    if (latest) {
      const detailKey=`composition:${person.name}`;
      const more=node('details',undefined,'composition');more.open=this._expanded.has(detailKey);
      more.append(node('summary','Body composition'));
      more.addEventListener('toggle',()=>{if(more.isConnected) more.open?this._expanded.add(detailKey):this._expanded.delete(detailKey);});
      more.append(node('p',`Latest reading: ${date(latest.timestamp)}`,'muted'));
      const fields=[['water','Water','%'],['muscle_mass','Muscle mass','kg'],['lean_body_mass','Lean body mass','kg'],['bone_mass','Bone mass','kg'],['visceral_fat','Visceral fat score',''],['basal_metabolism','Basal metabolism','kcal/day'],['protein','Protein','%'],['metabolic_age','Metabolic age','years']];
      if (fields.some(([key])=>finite(latest[key]))) {
        const list=node('dl');
        for (const [key,label,unit] of fields) {list.append(node('dt',label),node('dd',finite(latest[key])?`${number(latest[key])}${unit?' '+unit:''}`:'—'));}
        more.append(list,node('p','Calculated estimates.','muted'));
      } else more.append(node('p','No body-composition estimate for this reading.','muted'));
      card.append(more);
    }
    if (rows.length) {
      const table = node('table');table.append(node('caption','Recent readings'));
      const head=node('tr');['Date','Weight','Body fat','BMI'].forEach(t=>{const th=node('th',t);th.scope='col';head.append(th);});
      const thead=node('thead');thead.append(head);table.append(thead);const tbody=node('tbody');
      for (const row of rows.slice(0,10)) {
        const tr=node('tr');[date(row.timestamp),`${number(row.weight)} kg`,finite(row.body_fat)?`${number(row.body_fat,1)}%`:'—',number(row.bmi)].forEach(t=>tr.append(node('td',t)));tbody.append(tr);
      }
      table.append(tbody);const scroll=node('div',undefined,'table-wrap');scroll.append(table);card.append(scroll);
    }
    return card;
  }
  _render() {
    if (!this._hass || !this._config) return;
    const c = this._config;
    const me = c.people.find(p => p.user_id === this._hass.user?.id);
    const pendingState = this._hass.states[c.pending_entity];
    const pending = pendingState?.attributes.measurements || [];
    const history = this._hass.states[c.history_entity]?.attributes || {};
    const style = node('style');
    style.textContent = `
      :host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,inherit)}
      *{box-sizing:border-box}.wrap{max-width:900px;margin:0 auto;padding:16px}
      header,.section-head,.row-head{display:flex;align-items:center;justify-content:space-between;gap:16px}
      header{justify-content:flex-end;margin-bottom:12px}h2,h3{font-size:20px;margin:0;font-weight:400}a{color:var(--primary-color);text-decoration:none;font-size:14px}
      ha-card{display:block;margin-bottom:16px;padding:16px;border:1px solid var(--ha-card-border-color,var(--divider-color));border-radius:12px;box-shadow:var(--ha-card-box-shadow,none)}
      .section-head{margin-bottom:20px}.pill{font-size:14px;color:var(--secondary-text-color)}
      .reading+.reading{border-top:1px solid var(--divider-color);margin-top:16px;padding-top:16px}.reading-weight{font-size:28px;font-weight:400}
      .unit{font-size:16px;font-weight:400;color:var(--secondary-text-color);margin-left:5px}.muted{font-size:13px;color:var(--secondary-text-color);line-height:1.5;margin-top:4px}
      .actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}ha-button{min-height:40px}select{font:inherit;border-radius:4px;min-height:40px;padding:8px 12px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color)}
      a:focus-visible,summary:focus-visible{outline:2px solid var(--primary-color);outline-offset:4px}
      .metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.label{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--secondary-text-color)}ha-icon{--mdc-icon-size:20px;color:var(--state-icon-color)}.amount{font-size:28px;font-weight:400;margin:8px 0 0 28px;font-variant-numeric:tabular-nums}.metric>.muted{margin-left:28px}.change{margin:16px 0 0 28px}
      .trends{margin-top:28px;padding-top:24px;border-top:1px solid var(--divider-color)}.trend-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.range{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--secondary-text-color)}.range select{font-size:14px}.metric-choices{display:flex;gap:8px;margin:16px 0 8px}.metric-choice{border:1px solid var(--divider-color);border-radius:20px}.metric-choice[aria-pressed=true]{background:var(--secondary-background-color);border-color:var(--primary-color);box-shadow:inset 0 -2px var(--primary-color)}.trend-summary{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:4px}
      .measurement-chart{display:block;width:100%;overflow:visible}.grid-line{stroke:var(--divider-color);stroke-width:1}.axis-label{fill:var(--secondary-text-color);font-size:12px}.reading-line{fill:none;stroke:var(--primary-color,#03a9f4);stroke-width:1.7;opacity:.6}.average-line{fill:none;stroke:var(--accent-color,#ff9800);stroke-width:2;stroke-dasharray:6 4}.average-dot{fill:var(--accent-color,#ff9800)}.point-target{fill:transparent;cursor:pointer}.point-dot{fill:var(--primary-color,#03a9f4);stroke:var(--card-background-color);stroke-width:2;pointer-events:none}.reading-point.selected .point-dot,.reading-point:focus .point-dot{r:6;stroke:var(--primary-text-color);stroke-width:1.5}.reading-point:focus{outline:none}
      .chart-legend{display:flex;justify-content:center;gap:24px;font-size:12px;color:var(--secondary-text-color);margin-top:8px}.chart-legend span{display:flex;align-items:center;gap:6px}.chart-legend span:before{content:'';width:20px;border-top:2px solid var(--primary-color,#03a9f4)}.chart-legend .average-key:before{border-top:2px dashed var(--accent-color,#ff9800)}.point-detail{min-height:24px;display:flex;justify-content:space-between;align-items:baseline;gap:12px;font-size:14px;margin:20px 0 0;color:var(--secondary-text-color)}.point-detail strong{color:var(--primary-text-color);font-weight:500}.single-reading{margin-top:8px}.chart-empty{padding:32px 0;font-size:14px;color:var(--secondary-text-color)}.history-error{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--error-color)}
      .composition{margin-top:24px;border-top:1px solid var(--divider-color);padding-top:16px}.composition summary{font-size:14px;cursor:pointer}.composition dl{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;margin:20px 0;font-size:14px}.composition dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
      .table-wrap{overflow-x:auto;margin-top:24px}table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}caption{text-align:left;font-size:16px;font-weight:400;margin-bottom:12px}th{text-align:right;font-size:12px;font-weight:500;color:var(--secondary-text-color)}td,th{padding:12px 8px;border-bottom:1px solid var(--divider-color)}td{text-align:right}td:first-child,th:first-child{text-align:left;padding-left:0}tr:last-child td{border-bottom:0}
      details.others>summary{cursor:pointer;padding:8px 0 16px;font-size:16px;font-weight:400;color:var(--primary-text-color)}.notice{padding:0 0 12px;color:var(--primary-color)}.error{padding:0 0 12px;color:var(--error-color)}.empty{display:flex;align-items:center;gap:12px;font-size:14px;color:var(--secondary-text-color)}.empty .check{color:var(--success-color,#4caf50);font-size:20px}
      details.admin{margin-top:16px}details.admin summary{font-size:13px;color:var(--secondary-text-color);cursor:pointer}.admin .actions{margin-top:8px}select{max-width:100%}
      @media(max-width:600px){.wrap{padding:8px}.metrics{gap:8px}.label{gap:4px;font-size:13px}.label ha-icon{--mdc-icon-size:16px}.amount{font-size:24px;margin-left:0}.metric>.muted{margin-left:0;font-size:12px}.unit{font-size:12px;margin-left:3px}.change{margin-left:0}.range>span{display:none}.metric-choices{gap:4px}.metric-choice{flex:1;--ha-button-font-size:13px}td,th{font-size:12px;padding:10px 8px}.row-head{align-items:flex-start}.reading-weight{font-size:26px}.point-detail{font-size:13px}.trend-summary{font-size:12px}}
    `;
    const wrap = node('main',undefined,'wrap');
    const header = node('header');
    if (me) header.append(this._button('Profile settings',()=>this._openProfile()));
    wrap.append(header);
    if (this._notice || this._error) { const message=node('div',this._error || this._notice,this._error ? 'error' : 'notice');message.setAttribute('role',this._error ? 'alert' : 'status');wrap.append(message); }
    const pendingCard=node('ha-card',undefined,pending.length ? 'pending' : '');
    if (!pendingState || pendingState.state === 'unavailable') pendingCard.append(node('div','Scale is unavailable.','muted'));
    else if (!pending.length) { const empty=node('div',undefined,'empty');empty.append(node('span','✓','check'),node('span','All caught up'));pendingCard.append(empty); }
    else {
      const heading=node('div',undefined,'section-head');heading.append(node('h2','Confirm a reading'),node('span',String(pending.length),'pill'));pendingCard.append(heading);
      for (const record of pending) {
        const reading=node('div',undefined,'reading');const row=node('div',undefined,'row-head');const value=node('div',number(record.weight),'reading-weight');value.append(node('span','kg','unit'));row.append(value,node('span',date(record.timestamp),'muted'));reading.append(row);
        const eligible=me && record.candidates?.includes(me.name) && !record.declined?.includes(me.name);
        const expired=Date.now()-new Date(record.timestamp).getTime()>86400000;
        if (eligible && !expired) { const actions=node('div',undefined,'actions');actions.append(this._button("That’s me",()=>this._respond(record,'claim'),true),this._button('Not me',()=>this._respond(record,'decline')));reading.append(actions); }
        else reading.append(node('div',!me ? 'Your HA account has no scale profile.' : expired ? 'An administrator can assign this older reading.' : record.declined?.includes(me.name) ? 'You marked this as not yours.' : 'Waiting for confirmation from another person.','muted'));
        if (this._hass.user?.is_admin) {
          const admin=node('details',undefined,'admin');admin.append(node('summary','Assign to someone…'));
          const actions=node('div',undefined,'actions');const select=node('select');select.setAttribute('aria-label','Assign reading to');
          for (const person of c.people) { const option=node('option',person.name);option.value=person.name;select.append(option); }
          actions.append(select,this._button('Assign',()=>this._respond(record,'claim',select.value)));admin.append(actions);reading.append(admin);
        }
        pendingCard.append(reading);
      }
    }
    wrap.append(pendingCard);
    if (me) wrap.append(this._person(me,true,history));
    const others=c.people.filter(p=>p!==me);
    if (others.length) { const details=node('details',undefined,'others');details.open=this._expanded.has('others');details.append(node('summary',`Other household members · ${others.length}`));details.addEventListener('toggle',()=>details.open ? this._expanded.add('others') : this._expanded.delete('others'));for(const person of others) details.append(this._person(person,false,history));wrap.append(details); }
    this.shadowRoot.replaceChildren(style,wrap);
    if (this._focusControl) {
      const selector=this._focusControl==='period'?'[aria-label="Trend period"]':'.metric-choice[aria-pressed="true"]';
      this.shadowRoot.querySelector(selector)?.focus();this._focusControl=null;
    }
  }
}
if (!customElements.get('xiaomi-scale-card')) customElements.define('xiaomi-scale-card',XiaomiScaleCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'xiaomi-scale-card',name:'Xiaomi Scale',description:'Pending readings, your measurements, then your household.'});
