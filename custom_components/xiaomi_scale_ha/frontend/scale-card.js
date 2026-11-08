// Xiaomi Scale dashboard. No external dependencies; identity is verified by HA.
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};
const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: digits});
const date = value => value ? new Date(value).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';

class XiaomiScaleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:'open'});
    this._expanded = new Set();
    this._busy = false;
    this._charts = new Map();
  }
  setConfig(config) {
    if (!config.pending_entity || !config.history_entity || !Array.isArray(config.people)) throw new Error('Configure pending_entity, history_entity and people');
    this._config = config;
    this._renderKey = null;
    this._render();
  }
  set hass(hass) {
    this._hass = hass;
    for (const chart of this._charts.values()) chart.hass = hass;
    const c = this._config;
    if (!c) return;
    const entities = [c.pending_entity,c.history_entity,...c.people.flatMap(p => [p.weight_entity,p.body_fat_entity])];
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
  _metric(label, value, unit, subtitle) {
    const box = node('div',undefined,'metric');
    const labelRow = node('div',undefined,'label');
    const icon = node('ha-icon'); icon.setAttribute('icon',label === 'Weight' ? 'mdi:scale-bathroom' : 'mdi:water-percent');
    labelRow.append(icon,node('span',label));box.append(labelRow);
    const amount = node('div',number(value),'amount'); amount.append(node('span',unit,'unit')); box.append(amount);
    if (subtitle) box.append(node('div',subtitle,'muted'));
    return box;
  }
  _person(person, own, history) {
    const weight = this._hass.states[person.weight_entity];
    const fat = this._hass.states[person.body_fat_entity];
    const rows = [...(history[person.name]?.measurements || [])].reverse();
    const card = node('ha-card',undefined,own ? 'personal' : 'person');
    const heading = node('div',undefined,'section-head');
    heading.append(node('h2',own ? 'Your measurements' : person.name));
    if (own) heading.append(node('span',person.name,'pill'));
    card.append(heading);
    const metrics = node('div',undefined,'metrics');
    const w = weight && !['unknown','unavailable'].includes(weight.state) ? Number(weight.state) : null;
    const f = fat && !['unknown','unavailable'].includes(fat.state) ? Number(fat.state) : null;
    const latest = rows[0];
    metrics.append(this._metric('Weight', w, 'kg', latest ? date(latest.timestamp) : 'No measurements yet'));
    const fatHint = f != null ? date(fat.attributes.measured_at) : latest ? (latest.impedance ? 'Add profile details' : 'No estimate') : '';
    metrics.append(this._metric('Body fat', f, '%',fatHint));
    card.append(metrics);
    if (rows.length > 1) {
      const delta = Number(rows[0].weight) - Number(rows[1].weight);
      card.append(node('div',`${delta > 0 ? '+' : ''}${number(delta)} kg since previous reading`,'muted change'));
    }
    if (own && rows.length) {
      let chart = this._charts.get(person.name);
      if (chart) {
        const hasFat = rows.some(r=>r.body_fat != null);
        if (chart._scaleHasFat !== hasFat) {
          chart.setConfig({type:'history-graph',title:'Trends · 14 days',hours_to_show:336,fit_y_data:true,
            entities:[{entity:person.weight_entity,name:'Weight'},...(hasFat ? [{entity:person.body_fat_entity,name:'Body fat'}] : [])]});
          chart._scaleHasFat = hasFat;
        }
        chart.hass = this._hass; card.append(chart);
      }
      else if (window.loadCardHelpers && !this._loadingCharts) {
        this._loadingCharts = true;
        window.loadCardHelpers().then(helpers => {
          const native = helpers.createCardElement({type:'history-graph', title:'Trends · 14 days', hours_to_show:336, fit_y_data:true,
            entities:[{entity:person.weight_entity,name:'Weight'},...(rows.some(r=>r.body_fat != null) ? [{entity:person.body_fat_entity,name:'Body fat'}] : [])]});
          native.hass = this._hass;
          native.className = 'trend';
          native._scaleHasFat = rows.some(r=>r.body_fat != null);
          this._charts.set(person.name,native);
          this._loadingCharts = false;
          this._render();
        }).catch(() => { this._loadingCharts = false; });
      }
    }
    if (rows.length) {
      const table = node('table');
      const caption = node('caption', own ? 'Recent readings' : 'Recent readings'); table.append(caption);
      const head = node('tr'); ['Date','Weight','Body fat'].forEach(t => head.append(node('th',t))); table.append(head);
      for (const row of rows) {
        const tr = node('tr');
        [date(row.timestamp),`${number(row.weight)} kg`,row.body_fat == null ? '—' : `${number(row.body_fat,1)}%`].forEach(t => tr.append(node('td',t)));
        table.append(tr);
      }
      const scroll = node('div',undefined,'table-wrap'); scroll.append(table);card.append(scroll);
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
      header{justify-content:flex-end;margin-bottom:12px}h2{font-size:20px;margin:0;font-weight:400}a{color:var(--primary-color);text-decoration:none;font-size:14px}
      ha-card{display:block;margin-bottom:16px;padding:16px;border:1px solid var(--ha-card-border-color,var(--divider-color));border-radius:12px;box-shadow:var(--ha-card-box-shadow,none)}
      .section-head{margin-bottom:20px}.pill{font-size:14px;color:var(--secondary-text-color)}
      .reading+.reading{border-top:1px solid var(--divider-color);margin-top:16px;padding-top:16px}.reading-weight{font-size:28px;font-weight:400}
      .unit{font-size:16px;font-weight:400;color:var(--secondary-text-color);margin-left:5px}.muted{font-size:13px;color:var(--secondary-text-color);line-height:1.5;margin-top:4px}
      .actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}ha-button{min-height:40px}select{font:inherit;border-radius:4px;min-height:40px;padding:8px 12px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color)}
      a:focus-visible,summary:focus-visible{outline:2px solid var(--primary-color);outline-offset:4px}
      .metrics{display:grid;grid-template-columns:1fr 1fr;gap:16px}.label{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--secondary-text-color)}ha-icon{--mdc-icon-size:20px;color:var(--state-icon-color)}.amount{font-size:28px;font-weight:400;margin:8px 0 0 28px}.metric>.muted{margin-left:28px}.change{margin:16px 0 0 28px}
      .trend{--chart-max-height:220px;display:block;margin:16px -16px 0;--ha-card-border-width:0;--ha-card-box-shadow:none;--ha-card-border-radius:0}
      .table-wrap{overflow-x:auto;margin-top:24px}table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}caption{text-align:left;font-size:16px;font-weight:400;margin-bottom:12px}th{text-align:right;font-size:12px;font-weight:500;color:var(--secondary-text-color)}td,th{padding:12px 8px;border-bottom:1px solid var(--divider-color)}td{text-align:right}td:first-child,th:first-child{text-align:left;padding-left:0}tr:last-child td{border-bottom:0}
      details.others>summary{cursor:pointer;padding:8px 0 16px;font-size:16px;font-weight:400;color:var(--primary-text-color)}.notice{padding:0 0 12px;color:var(--primary-color)}.error{padding:0 0 12px;color:var(--error-color)}.empty{display:flex;align-items:center;gap:12px;font-size:14px;color:var(--secondary-text-color)}.empty .check{color:var(--success-color,#4caf50);font-size:20px}
      details.admin{margin-top:16px}details.admin summary{font-size:13px;color:var(--secondary-text-color);cursor:pointer}.admin .actions{margin-top:8px}select{max-width:100%}
      @media(max-width:600px){.wrap{padding:8px}h2{font-size:20px}.metrics{gap:8px}.amount{font-size:26px}td,th{font-size:12px;padding:10px 6px}.row-head{align-items:flex-start}.reading-weight{font-size:26px}}
    `;
    const wrap = node('main',undefined,'wrap');
    const header = node('header');
    if (this._hass.user?.is_admin) { const link=node('a','Profile settings');link.href='/config/integrations/integration/xiaomi_scale_ha';header.append(link); }
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
  }
}
if (!customElements.get('xiaomi-scale-card')) customElements.define('xiaomi-scale-card',XiaomiScaleCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'xiaomi-scale-card',name:'Xiaomi Scale',description:'Pending readings, your measurements, then your household.'});
