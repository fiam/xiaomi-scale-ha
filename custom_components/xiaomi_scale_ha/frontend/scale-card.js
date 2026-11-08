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
  }
  setConfig(config) {
    if (!config.pending_entity || !config.history_entity || !Array.isArray(config.people)) throw new Error('Configure pending_entity, history_entity and people');
    this._config = config;
    this._renderKey = null;
    this._render();
  }
  set hass(hass) {
    this._hass = hass;
    const c = this._config;
    if (!c) return;
    const entities = [c.pending_entity,c.history_entity,...c.people.flatMap(p => [p.weight_entity,p.body_fat_entity])];
    const key = JSON.stringify([hass.user?.id,...entities.map(id => hass.states[id])]);
    if (key !== this._renderKey) { this._renderKey = key; this._render(); }
  }
  getCardSize() { return 12; }
  getGridOptions() { return {columns:12, rows:'auto'}; }
  _button(label, action, primary = false) {
    const button = node('button',label,primary ? 'primary' : '');
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
    box.append(node('div',label,'label'));
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
    const fatHint = f != null ? date(fat.attributes.measured_at) : latest ? (latest.impedance ? 'Add profile details' : 'No impedance recorded') : '';
    metrics.append(this._metric('Body fat', f, '%',fatHint));
    card.append(metrics);
    if (rows.length) {
      const table = node('table');
      const caption = node('caption', own ? 'Recent readings' : 'Recent readings'); table.append(caption);
      const head = node('tr'); ['Date','Weight','Body fat','Impedance'].forEach(t => head.append(node('th',t))); table.append(head);
      for (const row of rows) {
        const tr = node('tr');
        [date(row.timestamp),`${number(row.weight)} kg`,row.body_fat == null ? '—' : `${number(row.body_fat,1)}%`,row.impedance == null ? '—' : `${number(row.impedance,0)} Ω`].forEach(t => tr.append(node('td',t)));
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
      *{box-sizing:border-box} .wrap{max-width:920px;margin:24px auto;padding:0 20px 32px}
      header,.section-head,.row-head{display:flex;align-items:center;justify-content:space-between;gap:16px}
      header{margin:0 0 24px} h1{font-size:28px;letter-spacing:-.5px;margin:0}h2{font-size:19px;margin:0;font-weight:600}
      .subtitle{margin-top:5px;color:var(--secondary-text-color);font-size:14px}a{color:var(--primary-color);text-decoration:none;font-size:14px}
      ha-card{display:block;margin:0 0 20px;padding:24px;border-radius:20px;border:1px solid var(--divider-color);background:var(--ha-card-background,var(--card-background-color));box-shadow:none}
      .pending{border-color:color-mix(in srgb,var(--primary-color) 50%,transparent)}
      .section-head{margin-bottom:20px}.pill{font-size:12px;padding:5px 10px;border-radius:20px;background:var(--secondary-background-color);white-space:nowrap}
      .reading+.reading{border-top:1px solid var(--divider-color);margin-top:20px;padding-top:20px}.reading-weight{font-size:30px;font-weight:600;letter-spacing:-.5px}
      .unit{font-size:16px;font-weight:400;color:var(--secondary-text-color);margin-left:7px}.muted{font-size:13px;color:var(--secondary-text-color);line-height:1.5;margin-top:5px}
      .actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}button,select{font:inherit;border-radius:12px;min-height:44px;padding:10px 18px;border:1px solid var(--divider-color);background:transparent;color:var(--primary-text-color)}
      button{cursor:pointer;font-weight:600}button.primary{background:var(--primary-color);border-color:var(--primary-color);color:var(--text-primary-color,#fff)}button:disabled{opacity:.5;cursor:wait}button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--primary-color);outline-offset:4px}
      .metrics{display:grid;grid-template-columns:1fr 1fr;gap:24px}.label{font-size:14px;color:var(--secondary-text-color)}.amount{font-size:38px;font-weight:600;margin-top:6px;letter-spacing:-1px}
      .table-wrap{overflow-x:auto;margin-top:24px}table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}caption{text-align:left;font-size:14px;font-weight:600;margin-bottom:12px}th{text-align:right;font-size:12px;font-weight:500;color:var(--secondary-text-color)}td,th{padding:12px 8px;border-bottom:1px solid var(--divider-color)}td{text-align:right}td:first-child,th:first-child{text-align:left;padding-left:0}tr:last-child td{border-bottom:0}
      details.others>summary{cursor:pointer;padding:6px 0 20px;font-size:16px;font-weight:600;color:var(--secondary-text-color)}.notice{padding:12px 0;color:var(--primary-color)}.error{padding:12px 0;color:var(--error-color)}.empty{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--secondary-text-color)}.empty .check{color:var(--success-color,#4caf50);font-size:20px}
      details.admin{margin-top:18px}details.admin summary{font-size:13px;color:var(--secondary-text-color);cursor:pointer}.admin .actions{margin-top:10px}select{max-width:100%}option{background:var(--card-background-color)}
      @media(max-width:600px){.wrap{margin:16px auto;padding:0 12px 20px}ha-card{padding:20px;border-radius:16px}h1{font-size:25px}header{align-items:flex-start}.metrics{gap:16px}.amount{font-size:32px}td,th{font-size:12px;padding:10px 6px}.row-head{align-items:flex-start}.reading-weight{font-size:28px}}
    `;
    const wrap = node('main',undefined,'wrap');
    const header = node('header'); const title = node('div');title.append(node('h1','Scale'),node('div','Your household’s measurements','subtitle'));header.append(title);
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
        reading.append(node('div',record.impedance ? `${number(record.impedance,0)} Ω impedance` : 'No impedance recorded','muted'));
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
