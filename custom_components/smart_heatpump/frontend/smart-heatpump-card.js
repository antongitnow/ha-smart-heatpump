/*
 * Smart Heatpump Controller — threshold tuner card.
 *
 * Shows a day of grid power (5-min averages) with the three solar thresholds
 * as draggable lines, and replays the controller's decisions for that day so
 * you can see what a threshold change would have done before saving it.
 *
 * Served and auto-loaded by the integration (see __init__.async_setup).
 * Dashboard YAML:  type: custom:smart-heatpump-card
 *
 * decideSolar() mirrors decision.decide_solar() — keep the two in sync;
 * tests/test_card_replay.py checks them against each other.
 */

const DOMAIN = "smart_heatpump";
const AVG_WINDOW_MS = 5 * 60 * 1000; // coordinator._AVG_WINDOW_MINUTES
const MIN_READINGS = 3; // coordinator._MIN_READINGS_FOR_AVG
const HISTORY_DAYS = 10; // recorder default purge_keep_days

// translation_key → name used in the card
const NUMBER_KEYS = {
  temp_ideal: "ideal",
  solar_surplus_threshold: "S",
  solar_release_threshold_low: "L",
  solar_release_threshold_high: "H",
  solar_step_delta: "step",
  solar_min_boost_minutes: "minBoost",
  solar_max_boost_temp: "maxBoost",
  evaluation_interval_min: "interval",
};
// Measured daily figures (sensor translation_key → card name)
const STAT_KEYS = {
  savings_today: "savings",
  solar_energy_today: "solar",
  heat_solar_today: "heat",
  heatpump_grid_boost_today: "grid",
  boost_time_today: "boostMin",
  export_unused_today: "unused",
  boosts_today: "boosts",
  resets_today: "resets",
};
const DEFAULTS = {
  ideal: 21, S: 300, L: 300, H: 700, step: 0.5, minBoost: 20, maxBoost: 25, interval: 5,
};
const THRESHOLDS = [
  { key: "S", label: "Surplus", sign: -1, color: "var(--shp-surplus)", hint: "Export needed to start or raise a boost" },
  { key: "L", label: "Release low", sign: 1, color: "var(--shp-low)", hint: "Import that steps a boost down" },
  { key: "H", label: "Release high", sign: 1, color: "var(--shp-high)", hint: "Import that ends a boost at once" },
];

// ---------------------------------------------------------------------------
// Decision logic — port of decision.decide_solar() (active-month check left
// to the caller). Returns [target, rule, newBoostActive].
// ---------------------------------------------------------------------------

const snapHalf = (v) => Math.floor(v * 2) / 2;

export function decideSolar(i) {
  const {
    active, avgExport, avgImport, temp = null, setpoint = null, ideal,
    S, L, H, step, boostSeconds = 0, minBoostMin = 0, maxBoost = 25, exportNow = 0,
  } = i;
  const hold = setpoint !== null ? setpoint : ideal;
  const clamp = (t) => snapHalf(Math.min(Math.max(t, ideal), maxBoost));

  if (!active) {
    if (avgExport > S) {
      let t = temp !== null ? temp + step : ideal + step;
      if (setpoint !== null) t = Math.max(t, setpoint);
      return [clamp(t), "solar_incremental", true];
    }
    return [hold, "no_solar_action", false];
  }

  if (minBoostMin > 0 && boostSeconds < minBoostMin * 60) {
    let t;
    if (setpoint !== null) t = setpoint;
    else if (temp !== null) t = temp + step;
    else t = ideal + step;
    if (temp !== null) {
      t = Math.max(t, temp + step);
      t = Math.min(t, temp + 1.0);
    }
    if (setpoint !== null) t = avgImport > 0 ? setpoint : Math.max(t, setpoint);
    return [clamp(t), "solar_min_run", true];
  }

  if (avgImport > H) return [ideal, "solar_reset", false];

  if (exportNow > 0 && avgImport > L) return [hold, "solar_boost_holding", true];

  if (avgImport > L) {
    const t = clamp(setpoint !== null ? setpoint - step : ideal);
    if (t <= ideal) return [ideal, "solar_boost_deactivated", false];
    return [t, "solar_step_down", true];
  }

  if (avgImport > 0 || avgExport <= S) return [hold, "solar_boost_holding", true];

  let t;
  if (setpoint !== null) t = setpoint + step;
  else if (temp !== null) t = temp + step;
  else t = ideal + step;
  if (temp !== null) {
    t = Math.min(t, temp + 1.0);
    t = Math.max(t, temp + step);
  }
  if (setpoint !== null) t = Math.max(t, setpoint);
  return [clamp(t), "solar_incremental", true];
}

// ---------------------------------------------------------------------------
// Replay a day: evaluate every `interval` minutes on the 5-min rolling average
// of the P1 readings, exactly like the coordinator does.
//   readings: [[ms, watts]] sorted, negative = export
//   temps:    [[ms, °C]] sorted (may be empty)
// ---------------------------------------------------------------------------

function firstAfter(arr, t) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid][0] <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// Time-weighted mean of a sensor over (a, b] — each reading holds until the
// next. Returns null when the sensor has no reading by b.
function timeMean(series, a, b) {
  let i = firstAfter(series, a) - 1;
  if (i < 0) {
    if (!series.length || series[0][0] > b) return null;
    i = 0;
    a = series[0][0];
  }
  let sum = 0, t = a, v = series[i][1];
  for (let j = i + 1; j < series.length && series[j][0] <= b; j++) {
    sum += v * (series[j][0] - t);
    t = series[j][0];
    v = series[j][1];
  }
  sum += v * (b - t);
  return b > a ? sum / (b - a) : v;
}

// hp (optional): heat pump power [[ms, watts]], used to count only the grid
// import the heat pump itself caused while boosting.
export function replay(readings, temps, p, hp = null) {
  const iv = p.interval * 60000;
  const prefix = [0];
  for (const r of readings) prefix.push(prefix[prefix.length - 1] + r[1]);
  const mean = (a, b) => {
    const i = firstAfter(readings, a), j = firstAfter(readings, b);
    return j - i > 0 ? { n: j - i, v: (prefix[j] - prefix[i]) / (j - i), last: readings[j - 1][1] } : { n: 0 };
  };

  const ticks = [];
  let active = false, sp = null, activatedAt = null;
  for (let T = p.start + iv; T <= p.end; T += iv) {
    const bar = mean(T - iv, T);
    const win = mean(T - AVG_WINDOW_MS, T);
    if (win.n < MIN_READINGS) {
      ticks.push({ t: T, bar: bar.n ? bar.v : null, hp: hp ? timeMean(hp, T - iv, T) : null, active, sp: sp ?? p.ideal, rule: "waiting_for_data", temp: null });
      continue;
    }
    const ti = firstAfter(temps, T) - 1;
    const temp = ti >= 0 ? temps[ti][1] : null;
    const [target, rule, next] = decideSolar({
      active,
      avgExport: Math.max(0, -win.v),
      avgImport: Math.max(0, win.v),
      temp,
      setpoint: sp,
      ideal: p.ideal, S: p.S, L: p.L, H: p.H, step: p.step,
      boostSeconds: active && activatedAt !== null ? (T - activatedAt) / 1000 : 0,
      minBoostMin: p.minBoost,
      maxBoost: p.maxBoost,
      exportNow: Math.max(0, -win.last),
    });
    if (next && !active) activatedAt = T;
    else if (!next) activatedAt = null;
    active = next;
    sp = target;
    ticks.push({ t: T, bar: bar.n ? bar.v : null, hp: hp ? timeMean(hp, T - iv, T) : null, active, sp, rule, temp });
  }
  return { ticks, stats: summarise(ticks, p.interval, Boolean(hp)) };
}

function summarise(ticks, interval, hpKnown) {
  const h = interval / 60;
  const s = { boostH: 0, exportBoost: 0, importBoost: 0, exportUnused: 0, starts: 0, resets: 0, peak: null, hpKnown };
  let was = false;
  ticks.forEach((k, i) => {
    if (k.active && !was) s.starts++;
    if (k.rule === "solar_reset") s.resets++;
    was = k.active;
    if (k.active) s.peak = Math.max(s.peak ?? k.sp, k.sp);
    // The state decided at tick i applies until tick i+1, whose bar covers that span
    const next = ticks[i + 1];
    if (!next || next.bar === null) return;
    const exp = Math.max(0, -next.bar) * h / 1000;
    // With a heat pump meter, other loads get the solar first and the heat
    // pump's share of the import is what it drew beyond that: min(import, draw)
    const gridW = Math.max(0, next.bar);
    const imp = (hpKnown && next.hp !== null ? Math.min(gridW, Math.max(0, next.hp)) : gridW) * h / 1000;
    if (k.active) {
      s.boostH += h;
      s.exportBoost += exp;
      s.importBoost += imp;
    } else {
      s.exportUnused += exp;
    }
  });
  return s;
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

const esc = (v) => String(v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtW = (w) => `${Math.round(Math.abs(w)).toLocaleString()} W`;
const fmtHM = (min) => `${Math.floor(Math.round(min) / 60)}:${String(Math.round(min) % 60).padStart(2, "0")} h`;
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function niceCeil(v) {
  const step = v <= 1000 ? 250 : v <= 3000 ? 500 : 1000;
  return Math.ceil(v / step) * step;
}

const STYLE = `
  :host {
    --shp-import: var(--energy-grid-consumption-color, #488fc2);
    --shp-export: var(--energy-grid-return-color, #8353d1);
    --shp-boost: var(--energy-solar-color, #ff9800);
    --shp-surplus: var(--success-color, #43a047);
    --shp-low: var(--warning-color, #ffa600);
    --shp-high: var(--error-color, #db4437);
    --shp-setpoint: var(--state-climate-heat-color, #ff8100);
  }
  ha-card { padding: 16px; }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
  .title { font-size: 1.25em; font-weight: 400; color: var(--primary-text-color); margin: 0; }
  .chip { font-size: 12px; padding: 4px 10px; border-radius: 12px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .chip.on { background: color-mix(in srgb, var(--shp-boost) 20%, transparent); color: var(--primary-text-color); }
  .live { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin: 14px 0; }
  .tile { background: var(--secondary-background-color); border-radius: 8px; padding: 8px 10px; }
  .tile .l, .kpi .l { font-size: 12px; color: var(--secondary-text-color); }
  .tile .v { font-size: 18px; color: var(--primary-text-color); margin-top: 2px; }
  .tile.warn { box-shadow: inset 0 0 0 1px var(--warning-color, #ffa600); }
  .tile .w { font-size: 12px; color: var(--warning-color, #ffa600); margin-top: 2px; }
  .bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
  .nav { display: flex; align-items: center; gap: 4px; }
  .nav .d { min-width: 110px; text-align: center; font-size: 14px; color: var(--primary-text-color); }
  button { font: inherit; font-size: 13px; color: var(--primary-text-color); background: none; border: 1px solid var(--divider-color); border-radius: 8px; padding: 5px 10px; cursor: pointer; }
  button:hover { background: var(--secondary-background-color); }
  button.icon { border: none; padding: 4px 8px; font-size: 16px; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .hover { font-size: 12px; color: var(--secondary-text-color); min-height: 16px; }
  .chart { position: relative; width: 100%; }
  svg { display: block; width: 100%; touch-action: pan-y; user-select: none; -webkit-user-select: none; }
  svg text { font-family: inherit; }
  .handle { cursor: ns-resize; outline: none; }
  .handle:focus-visible rect { stroke-width: 2.5; }
  .hit { cursor: ns-resize; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--secondary-text-color); margin: 4px 0 12px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  .sect { font-size: 14px; color: var(--primary-text-color); margin: 4px 0 8px; }
  .sect span { font-size: 12px; color: var(--secondary-text-color); margin-left: 6px; }
  #today { margin-bottom: 16px; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
  .kpi { border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 10px; }
  .kpi .v { font-size: 18px; color: var(--primary-text-color); margin: 2px 0; }
  .kpi .d { font-size: 12px; color: var(--secondary-text-color); }
  .kpi .d.good { color: var(--success-color, #43a047); }
  .kpi .d.bad { color: var(--error-color, #db4437); }
  .actions { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  .actions .note { font-size: 12px; color: var(--secondary-text-color); margin-right: auto; }
  details { margin: 0 0 12px; border: 1px solid var(--divider-color); border-radius: 8px; padding: 0 12px; }
  summary { cursor: pointer; padding: 10px 0; font-size: 14px; color: var(--primary-text-color); }
  .ex { display: grid; grid-template-columns: 18px 1fr; gap: 6px 10px; padding: 2px 0 12px; font-size: 13px; line-height: 1.5; color: var(--secondary-text-color); }
  .ex .sw { width: 18px; height: 0; border-top: 2px dashed; margin-top: 9px; }
  .ex b { font-weight: 500; color: var(--primary-text-color); }
  .ex .foot { grid-column: 1 / -1; border-top: 1px solid var(--divider-color); padding-top: 8px; margin-top: 2px; }
  .msg { padding: 24px 0; text-align: center; color: var(--secondary-text-color); }
`;

class SmartHeatpumpCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._day = startOfDay(new Date());
    this._draft = {}; // unsaved threshold edits
    this._data = null; // { day, readings, temps }
    this._width = 0;
    this._drag = null;
    this._hoverIdx = null;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { start_hour: 6, end_hour: 20, title: "Solar boost tuner", ...config };
  }

  getCardSize() {
    return 9;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._entities) this._resolveEntities();
    if (first) {
      this._build();
      this._load();
      return;
    }
    if (!this._entities) return;
    if (!this._data && !this._loading) this._load();
    // hass is set on every state change in HA — only redraw when ours change
    const rule = hass.states[this._entities.rule];
    if (rule !== this._lastRule) {
      this._lastRule = rule;
      this._renderLive();
    }
    const todaySig = Object.values(this._entities.stats).map((id) => hass.states[id]?.state).join("|");
    if (todaySig !== this._lastToday) {
      this._lastToday = todaySig;
      this._renderToday();
    }
    const sig = JSON.stringify(this._saved());
    if (sig !== this._lastSaved) {
      this._lastSaved = sig;
      this._renderLive(); // the setpoint warning depends on the ideal temperature
      if (!this._drag) this._renderChart();
    }
  }

  connectedCallback() {
    this._ro = new ResizeObserver(() => {
      const w = this.shadowRoot.querySelector(".chart")?.clientWidth || 0;
      if (w && Math.abs(w - this._width) > 1) {
        this._width = w;
        this._renderChart();
      }
    });
    if (this.shadowRoot.querySelector(".chart")) this._ro.observe(this.shadowRoot.querySelector(".chart"));
    // Keep today's view fresh — the controller evaluates every few minutes
    this._timer = setInterval(() => {
      if (this._day === startOfDay(new Date())) this._load();
    }, 5 * 60 * 1000);
  }

  disconnectedCallback() {
    this._ro?.disconnect();
    clearInterval(this._timer);
  }

  // ---- entity discovery ---------------------------------------------------

  _resolveEntities() {
    const hass = this._hass;
    const regs = Object.values(hass.entities || {}).filter((e) => e.platform === DOMAIN);
    const rule =
      this._config.entity ||
      regs.find((e) => e.translation_key === "active_rule")?.entity_id ||
      (hass.states["sensor.smart_heatpump_controller_active_rule"] && "sensor.smart_heatpump_controller_active_rule");
    if (!rule) return;
    const device = hass.entities?.[rule]?.device_id;
    const numbers = {};
    const stats = {};
    for (const e of regs) {
      if (device && e.device_id !== device) continue;
      if (e.entity_id.startsWith("number.") && NUMBER_KEYS[e.translation_key]) {
        numbers[NUMBER_KEYS[e.translation_key]] = e.entity_id;
      } else if (e.entity_id.startsWith("sensor.") && STAT_KEYS[e.translation_key]) {
        stats[STAT_KEYS[e.translation_key]] = e.entity_id;
      }
    }
    this._entities = { rule, numbers, stats };
  }

  _saved() {
    const out = { ...DEFAULTS };
    for (const [k, id] of Object.entries(this._entities?.numbers || {})) {
      const v = parseFloat(this._hass.states[id]?.state);
      if (!Number.isNaN(v)) out[k] = v;
    }
    return out;
  }

  _current() {
    return { ...this._saved(), ...this._draft };
  }

  // ---- data ---------------------------------------------------------------

  async _load() {
    if (!this._entities) {
      this._message("Smart Heatpump Controller not found. Set `entity:` to its Active rule sensor.");
      return;
    }
    const attrs = this._hass.states[this._entities.rule]?.attributes || {};
    const power = this._config.power_entity || attrs.power_entity;
    const tempEntity = this._config.indoor_temp_entity || attrs.indoor_temp_entity;
    const hpEntity = this._config.heatpump_power_entity || attrs.heatpump_power_entity;
    if (!power) {
      this._message("No power sensor known yet. Update the integration, or set `power_entity:` on the card.");
      return;
    }
    const day = this._day;
    const end = Math.min(day + 86400000, Date.now());
    this._loading = true;
    const iso = (ms) => new Date(ms).toISOString();
    try {
      const [p, t, h] = await Promise.all([
        this._hass.callWS({
          type: "history/history_during_period",
          start_time: iso(day - AVG_WINDOW_MS),
          end_time: iso(end),
          entity_ids: [power],
          minimal_response: true,
          no_attributes: true,
          significant_changes_only: false,
        }),
        tempEntity
          ? this._hass.callWS({
              type: "history/history_during_period",
              start_time: iso(day),
              end_time: iso(end),
              entity_ids: [tempEntity],
              minimal_response: false,
              no_attributes: !tempEntity.startsWith("climate."),
              significant_changes_only: false,
            })
          : Promise.resolve({}),
        hpEntity
          ? this._hass.callWS({
              type: "history/history_during_period",
              start_time: iso(day),
              end_time: iso(end),
              entity_ids: [hpEntity],
              minimal_response: true,
              no_attributes: true,
              significant_changes_only: false,
            })
          : Promise.resolve({}),
      ]);
      const readings = this._series(p[power], (s) => s.s);
      const temps = tempEntity
        ? this._series(t[tempEntity], (s) => (tempEntity.startsWith("climate.") ? s.a?.current_temperature : s.s))
        : [];
      let hp = null;
      if (hpEntity) {
        const kw = this._hass.states[hpEntity]?.attributes?.unit_of_measurement === "kW";
        hp = this._series(h[hpEntity], (s) => s.s).map(([ts, v]) => [ts, kw ? v * 1000 : v]);
      }
      if (day !== this._day) return; // user moved on while loading
      this._data = { day, end, readings, temps, hp };
      this._renderChart();
    } catch (err) {
      this._message(`Couldn't load history: ${esc(err.message || err)}`);
    } finally {
      this._loading = false;
    }
  }

  _series(states = [], pick) {
    const out = [];
    for (const s of states) {
      const v = parseFloat(pick(s));
      const t = (s.lu ?? s.lc) * 1000;
      if (!Number.isNaN(v) && t) out.push([t, v]);
    }
    return out.sort((a, b) => a[0] - b[0]);
  }

  // ---- rendering ----------------------------------------------------------

  _build() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <p class="title">${esc(this._config.title)}</p>
          <span class="chip" id="rule"></span>
        </div>
        <div class="live" id="live"></div>
        <div class="sect">Today so far <span>measured</span></div>
        <div class="kpis" id="today"></div>
        <div class="bar">
          <div class="nav">
            <button class="icon" id="prev" aria-label="Previous day">&#8249;</button>
            <span class="d" id="day"></span>
            <button class="icon" id="next" aria-label="Next day">&#8250;</button>
          </div>
          <span class="hover" id="hover"></span>
        </div>
        <div class="chart" id="chart"><div class="msg">Loading history…</div></div>
        <div class="legend">
          <span><i style="background:var(--shp-import)"></i>Grid import</span>
          <span><i style="background:var(--shp-export)"></i>Export</span>
          <span><i style="background:var(--shp-boost);opacity:.35"></i>Boost active (replayed)</span>
          <span><i style="background:var(--shp-setpoint)"></i>Setpoint</span>
          <span><i style="background:var(--secondary-text-color)"></i>Room</span>
        </div>
        <details id="explain">
          <summary>What do the three lines do?</summary>
          <div class="ex" id="ex"></div>
        </details>
        <div class="sect" id="replay-head">Replay</div>
        <div class="kpis" id="kpis"></div>
        <div class="actions">
          <span class="note" id="note"></span>
          <button id="reset">Back to saved</button>
          <button class="primary" id="save">Save thresholds</button>
        </div>
      </ha-card>`;
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("prev").onclick = () => this._shiftDay(-1);
    $("next").onclick = () => this._shiftDay(1);
    $("reset").onclick = () => {
      this._draft = {};
      this._renderChart();
    };
    $("save").onclick = () => this._save();
    // Remember whether the explanation is open (per browser; default open)
    try {
      $("explain").open = localStorage.getItem("smart-heatpump-card-explain") !== "closed";
    } catch {
      $("explain").open = true;
    }
    $("explain").addEventListener("toggle", (e) => {
      try {
        localStorage.setItem("smart-heatpump-card-explain", e.target.open ? "open" : "closed");
      } catch {
        // storage blocked — the panel just opens again next time
      }
    });
    const chart = $("chart");
    chart.addEventListener("pointerdown", (e) => this._onDown(e));
    chart.addEventListener("pointermove", (e) => this._onMove(e));
    chart.addEventListener("pointerup", (e) => this._onUp(e));
    chart.addEventListener("pointercancel", (e) => this._onUp(e));
    chart.addEventListener("pointerleave", () => this._setHover(null));
    chart.addEventListener("keydown", (e) => this._onKey(e));
    this._ro?.observe(chart);
    this._width = chart.clientWidth;
    this._renderLive();
    this._renderToday();
  }

  _message(text) {
    const chart = this.shadowRoot.getElementById("chart");
    if (chart) chart.innerHTML = `<div class="msg">${text}</div>`;
  }

  _shiftDay(n) {
    const today = startOfDay(new Date());
    const d = new Date(this._day);
    d.setDate(d.getDate() + n);
    const next = startOfDay(d);
    if (next > today || next < today - HISTORY_DAYS * 86400000) return;
    this._day = next;
    this._data = null;
    this._message("Loading history…");
    this._renderDay();
    this._load();
  }

  _renderDay() {
    const today = startOfDay(new Date());
    const el = this.shadowRoot.getElementById("day");
    el.textContent =
      this._day === today
        ? "Today"
        : new Date(this._day).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
    this.shadowRoot.getElementById("next").style.visibility = this._day === today ? "hidden" : "visible";
  }

  _renderLive() {
    const root = this.shadowRoot;
    if (!root.getElementById("live") || !this._entities) return;
    const st = this._hass.states[this._entities.rule];
    if (!st) return;
    const a = st.attributes;
    const chip = root.getElementById("rule");
    chip.textContent = `${a.description || st.state}${a.mode === "dry_run" ? " · dry run" : ""}`;
    chip.className = `chip${a.solar_boost_active ? " on" : ""}`;
    const deg = (v) => (v === undefined || v === null ? "—" : `${Number(v).toFixed(1)} °C`);
    // Outside a boost this is the thermostat's own setpoint — flag it when
    // something else (schedule, app, automation) set it below ideal
    const ideal = this._saved().ideal;
    const sp = a.computed_setpoint;
    const low = sp !== undefined && sp !== null && Number(sp) < ideal;
    const tiles = [
      ["Grid now", a.current_power ? a.current_power.replace(/(\d)W$/, "$1 W") : "—"],
      ["Room", deg(a.indoor_temp)],
      ["Thermostat setpoint", deg(sp), low ? `below ideal ${deg(ideal)}` : ""],
      ["Outdoor", deg(a.outdoor_temp)],
    ];
    root.getElementById("live").innerHTML = tiles
      .map(
        ([l, v, warn]) =>
          `<div class="tile${warn ? " warn" : ""}"><div class="l">${l}</div><div class="v">${esc(v)}</div>` +
          `${warn ? `<div class="w">${esc(warn)}</div>` : ""}</div>`
      )
      .join("");
    this._renderDay();
  }

  _renderToday() {
    const el = this.shadowRoot.getElementById("today");
    if (!el || !this._entities) return;
    const ids = this._entities.stats;
    const st = (k) => this._hass.states[ids[k]];
    const num = (k) => {
      const v = parseFloat(st(k)?.state);
      return Number.isNaN(v) ? null : v;
    };
    const kwh = (k) => (num(k) === null ? "—" : `${num(k).toFixed(2)} kWh`);
    const yday = (k, fmt) => {
      const v = st(k)?.attributes?.yesterday;
      return v === undefined ? "" : `yesterday ${fmt(Number(v))}`;
    };
    const fmtKwh = (v) => `${v.toFixed(2)} kWh`;
    const noHp = "needs the heat pump power sensor";
    const tiles = [
      // With a heat output sensor the headline is the heat; else the electricity
      num("heat") !== null
        ? [
            "Heat from solar",
            `${num("heat").toFixed(2)} kWh`,
            `${(num("solar") ?? 0).toFixed(2)} kWh electricity · € ${(num("savings") ?? 0).toFixed(2)} saved`,
          ]
        : [
            "Heat pump on solar",
            kwh("solar"),
            num("savings") === null ? noHp : `€ ${num("savings").toFixed(2)} saved`,
          ],
      ["Heat pump grid import while boosting", kwh("grid"), num("grid") === null ? noHp : yday("grid", fmtKwh)],
      ["Boost time", num("boostMin") === null ? "—" : fmtHM(num("boostMin")), yday("boostMin", fmtHM)],
      ["Export not used", kwh("unused"), yday("unused", fmtKwh)],
      [
        "Boosts / resets",
        num("boosts") === null ? "—" : `${num("boosts")} / ${num("resets") ?? 0}`,
        st("boosts") ? `yesterday ${st("boosts").attributes.yesterday ?? 0} / ${st("resets")?.attributes.yesterday ?? 0}` : "",
      ],
    ];
    el.innerHTML = tiles
      .map(([l, v, d]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`)
      .join("");
  }

  _layout() {
    const W = Math.max(300, this._width || 600);
    const narrow = W < 500;
    const g = { W, left: 44, right: narrow ? 8 : 136, top: 10, plotH: narrow ? 200 : 250, gap: 26, stripH: 64, axisH: 20 };
    g.plotB = g.top + g.plotH;
    g.stripT = g.plotB + g.gap;
    g.stripB = g.stripT + g.stripH;
    g.H = g.stripB + g.axisH;
    g.narrow = narrow;
    return g;
  }

  _renderChart() {
    const chart = this.shadowRoot.getElementById("chart");
    if (!chart || !this._data) return;
    const { day, end, readings, temps, hp } = this._data;
    if (!readings.length) {
      this._message("No power readings recorded for this day.");
      return;
    }
    const saved = this._saved();
    const cur = this._current();
    const base = { start: day, end, ideal: saved.ideal, step: saved.step, minBoost: saved.minBoost, maxBoost: saved.maxBoost, interval: saved.interval };
    // Replays are cached: hovering redraws the chart but changes no inputs
    const key = JSON.stringify([day, end, readings.length, temps.length, saved, cur]);
    if (this._replayKey !== key) {
      this._replayKey = key;
      this._replays = [
        replay(readings, temps, { ...base, S: cur.S, L: cur.L, H: cur.H }, hp),
        replay(readings, temps, { ...base, S: saved.S, L: saved.L, H: saved.H }, hp),
      ];
    }
    const [run, ref] = this._replays;

    const g = this._layout();
    const x0 = day + this._config.start_hour * 3600000;
    const x1 = day + this._config.end_hour * 3600000;
    const X = (t) => g.left + ((t - x0) / (x1 - x0)) * (g.W - g.left - g.right);
    const iv = saved.interval * 60000;
    const shown = run.ticks.filter((k) => k.t > x0 && k.t - iv < x1);

    // Y scale — fixed per day so lines don't jump while dragging
    if (!this._scale || this._scale.day !== day || this._scale.end !== end) {
      let imp = 0, exp = 0;
      for (const k of shown) if (k.bar !== null) { imp = Math.max(imp, k.bar); exp = Math.max(exp, -k.bar); }
      const ymax = niceCeil(Math.max(1000, saved.H * 1.5, Math.min(imp, Math.max(2500, saved.H * 2))));
      const ymin = -niceCeil(Math.max(1000, saved.S * 1.5, Math.min(exp, 6000)));
      this._scale = { day, end, ymax, ymin };
    }
    const { ymax, ymin } = this._scale;
    const Y = (w) => g.top + ((ymax - Math.max(ymin, Math.min(ymax, w))) / (ymax - ymin)) * g.plotH;
    this._geom = { g, X, Y, x0, x1, ymax, ymin, iv, shown };

    const pr = [];
    const pR = g.W - g.right;
    // threshold zones
    pr.push(`<rect x="${g.left}" y="${Y(-cur.S)}" width="${pR - g.left}" height="${Y(ymin) - Y(-cur.S)}" fill="var(--shp-surplus)" opacity=".05"/>`);
    pr.push(`<rect x="${g.left}" y="${Y(cur.H)}" width="${pR - g.left}" height="${Y(cur.L) - Y(cur.H)}" fill="var(--shp-low)" opacity=".05"/>`);
    pr.push(`<rect x="${g.left}" y="${Y(ymax)}" width="${pR - g.left}" height="${Y(cur.H) - Y(ymax)}" fill="var(--shp-high)" opacity=".05"/>`);
    // grid
    const stepY = (ymax - ymin) <= 3000 ? 500 : 1000;
    for (let v = Math.ceil(ymin / stepY) * stepY; v <= ymax; v += stepY) {
      pr.push(`<line x1="${g.left}" x2="${pR}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--divider-color)" ${v ? 'stroke-dasharray="2 4"' : ""}/>`);
      pr.push(`<text x="${g.left - 6}" y="${Y(v) + 4}" font-size="11" text-anchor="end" fill="var(--secondary-text-color)">${v === 0 ? "0" : (v / 1000).toLocaleString() + "k"}</text>`);
    }
    // boost shading
    let runStart = null;
    const shade = (a, b) => pr.push(`<rect x="${X(a)}" y="${g.top}" width="${Math.max(0, X(b) - X(a))}" height="${g.stripB - g.top}" fill="var(--shp-boost)" opacity=".16"/>`);
    run.ticks.forEach((k, i) => {
      if (k.active && runStart === null) runStart = k.t;
      const nextT = run.ticks[i + 1]?.t ?? k.t + iv;
      if (runStart !== null && (!run.ticks[i + 1]?.active)) {
        shade(Math.max(runStart, x0), Math.min(nextT, x1));
        runStart = null;
      }
    });
    // bars
    const bw = Math.max(1, X(x0 + iv) - X(x0) - 1);
    for (const k of shown) {
      if (k.bar === null) continue;
      const y0 = Y(0), y1 = Y(k.bar);
      const xb = X(k.t - iv) + 0.5;
      pr.push(`<rect x="${xb}" y="${Math.min(y0, y1)}" width="${bw}" height="${Math.max(1, Math.abs(y1 - y0))}" rx="1" fill="${k.bar < 0 ? "var(--shp-export)" : "var(--shp-import)"}"/>`);
      if (k.bar > ymax || k.bar < ymin) pr.push(`<rect x="${xb}" y="${k.bar > ymax ? g.top : g.plotB - 2}" width="${bw}" height="2" fill="var(--primary-text-color)" opacity=".5"/>`);
    }
    // now marker
    if (Date.now() < day + 86400000 && Date.now() > x0 && Date.now() < x1) {
      pr.push(`<line x1="${X(Date.now())}" x2="${X(Date.now())}" y1="${g.top}" y2="${g.stripB}" stroke="var(--primary-text-color)" stroke-width="1" opacity=".4"/>`);
    }
    // setpoint strip
    const tShown = temps.filter((t) => t[0] >= x0 && t[0] <= x1).map((t) => t[1]);
    const spVals = shown.map((k) => k.sp);
    const sLo = Math.floor(Math.min(saved.ideal, ...tShown, ...spVals) * 2) / 2;
    const sHi = Math.max(sLo + 1, Math.ceil(Math.max(saved.ideal + 1, ...tShown, ...spVals) * 2) / 2);
    const SY = (c) => g.stripB - ((c - sLo) / (sHi - sLo)) * g.stripH;
    pr.push(`<line x1="${g.left}" x2="${pR}" y1="${g.stripB}" y2="${g.stripB}" stroke="var(--divider-color)"/>`);
    pr.push(`<text x="${g.left - 6}" y="${SY(sHi) + 9}" font-size="11" text-anchor="end" fill="var(--secondary-text-color)">${sHi}°</text>`);
    pr.push(`<text x="${g.left - 6}" y="${g.stripB}" font-size="11" text-anchor="end" fill="var(--secondary-text-color)">${sLo}°</text>`);
    pr.push(`<text x="${g.left + 4}" y="${g.stripT - 8}" font-size="11" fill="var(--secondary-text-color)">Replayed setpoint and room temperature</text>`);
    if (tShown.length) {
      let d = "";
      let lastV = null;
      for (const [t, v] of temps) {
        if (t > x1) break;
        if (t < x0) { lastV = v; continue; }
        if (!d && lastV !== null) d = `M${X(x0)} ${SY(lastV)}`;
        d += `${d ? "L" : "M"}${X(t)} ${SY(v)}`;
      }
      pr.push(`<path d="${d}" fill="none" stroke="var(--secondary-text-color)" stroke-width="1.5" stroke-dasharray="3 3"/>`);
    }
    let dsp = "";
    for (const k of shown) {
      const xa = X(Math.max(k.t, x0)), xb2 = X(Math.min(k.t + iv, x1));
      dsp += `${dsp ? "L" : "M"}${xa} ${SY(k.sp)}L${xb2} ${SY(k.sp)}`;
    }
    pr.push(`<path d="${dsp}" fill="none" stroke="var(--shp-setpoint)" stroke-width="2"/>`);
    // time axis
    const span = (x1 - x0) / 3600000;
    const every = g.narrow ? (span > 10 ? 3 : 2) : span > 14 ? 2 : 1;
    for (let h = Math.ceil(this._config.start_hour); h <= this._config.end_hour; h += every) {
      const t = day + h * 3600000;
      pr.push(`<text x="${X(t)}" y="${g.H - 4}" font-size="11" text-anchor="middle" fill="var(--secondary-text-color)">${String(h).padStart(2, "0")}:00</text>`);
    }
    // hover crosshair
    if (this._hoverIdx !== null && shown[this._hoverIdx]) {
      const k = shown[this._hoverIdx];
      const xc = X(k.t - iv / 2);
      pr.push(`<line x1="${xc}" x2="${xc}" y1="${g.top}" y2="${g.stripB}" stroke="var(--primary-text-color)" opacity=".35"/>`);
    }
    // saved ghost lines + threshold lines
    for (const th of THRESHOLDS) {
      if (cur[th.key] !== saved[th.key]) {
        pr.push(`<line x1="${g.left}" x2="${pR}" y1="${Y(th.sign * saved[th.key])}" y2="${Y(th.sign * saved[th.key])}" stroke="${th.color}" opacity=".35" stroke-dasharray="2 3"/>`);
      }
    }
    for (const th of THRESHOLDS) {
      const yy = Y(th.sign * cur[th.key]);
      const short = th.label.split(" ").pop();
      const label = `${g.narrow ? short[0].toUpperCase() + short.slice(1) : th.label} ${cur[th.key]} W`;
      pr.push(`<line x1="${g.left}" x2="${pR}" y1="${yy}" y2="${yy}" stroke="${th.color}" stroke-width="2" stroke-dasharray="7 4"/>`);
      pr.push(`<line class="hit" data-k="${th.key}" x1="${g.left}" x2="${pR}" y1="${yy}" y2="${yy}" stroke="transparent" stroke-width="16"/>`);
      const hw = label.length * 6.2 + 18;
      const hx = g.narrow ? pR - hw - 2 : pR + 6;
      pr.push(`<g class="handle" data-k="${th.key}" tabindex="0" role="slider" aria-label="${th.label} threshold" aria-valuenow="${cur[th.key]}" aria-valuemin="0" aria-valuemax="5000">
        <title>${esc(th.hint)} — drag to change</title>
        <rect x="${hx}" y="${yy - 11}" width="${hw}" height="22" rx="11" fill="var(--card-background-color, #fff)" stroke="${th.color}" stroke-width="1.5"/>
        <text x="${hx + hw / 2}" y="${yy + 4}" font-size="11" text-anchor="middle" fill="var(--primary-text-color)">${esc(label)}</text></g>`);
    }

    const focused = this.shadowRoot.activeElement?.dataset?.k;
    chart.innerHTML = `<svg width="${g.W}" height="${g.H}" viewBox="0 0 ${g.W} ${g.H}" role="img" aria-label="Grid power with solar boost thresholds">${pr.join("")}</svg>`;
    if (focused) chart.querySelector(`.handle[data-k="${focused}"]`)?.focus();
    this._renderKpis(run.stats, ref.stats, cur, saved);
    this._renderExplain(cur, saved);
  }

  _renderExplain(cur, saved) {
    const deg = (v) => `${Number(v).toFixed(1)} °C`;
    const rows = [
      [
        "var(--shp-surplus)",
        `Surplus — ${cur.S} W export`,
        `Starts a boost. When you've exported more than ${cur.S} W on average over the last 5 minutes, ` +
          `the setpoint is set ${saved.step} °C above room temperature, so the heat pump turns free solar power into heat in the floor. ` +
          `While the surplus lasts it steps up another ${saved.step} °C each evaluation, to at most 1 °C above room temperature and ${deg(saved.maxBoost)}. ` +
          `Lower it to boost on smaller surpluses; raise it to boost only on clear sunshine.`,
      ],
      [
        "var(--shp-low)",
        `Release low — ${cur.L} W import`,
        `Steps a boost down. While boosting, importing more than ${cur.L} W on average lowers the setpoint by ${saved.step} °C ` +
          `each evaluation, until it's back at your ideal ${deg(saved.ideal)} and the boost ends. ` +
          `It waits while you're still exporting at that moment, so a passing cloud doesn't step it down.`,
      ],
      [
        "var(--shp-high)",
        `Release high — ${cur.H} W import`,
        `Ends a boost at once. Importing more than ${cur.H} W on average puts the setpoint straight back to ${deg(saved.ideal)}. ` +
          `Keep it above the heat pump's own start-up draw, or the boost switches itself off as soon as the heat pump starts.`,
      ],
    ];
    const foot =
      `Between the surplus and release-low lines the setpoint holds. ` +
      `For the first ${saved.minBoost} minutes of a boost neither release line applies (minimum boost duration). ` +
      `Every value is a 5-minute average, so a kettle or a short spike doesn't trigger anything.`;
    this.shadowRoot.getElementById("ex").innerHTML =
      rows
        .map(([c, h, t]) => `<span class="sw" style="border-color:${c}"></span><div><b>${esc(h)}</b><br>${esc(t)}</div>`)
        .join("") + `<div class="foot">${esc(foot)}</div>`;
  }

  _renderKpis(s, r, cur, saved) {
    const changed = THRESHOLDS.some((t) => cur[t.key] !== saved[t.key]);
    const delta = (a, b, unit, lowerIsBetter, digits = 1) => {
      const d = Math.round((a - b) * 10 ** digits) / 10 ** digits;
      if (!changed) return `<div class="d">with saved settings</div>`;
      if (!d) return `<div class="d">same as saved</div>`;
      const good = lowerIsBetter ? d < 0 : d > 0;
      return `<div class="d ${good ? "good" : "bad"}">${d > 0 ? "+" : ""}${d.toFixed(digits)}${unit} vs saved</div>`;
    };
    const kpis = [
      ["Export used while boosting", `${s.exportBoost.toFixed(2)} kWh`, delta(s.exportBoost, r.exportBoost, " kWh", false, 2)],
      [s.hpKnown ? "Heat pump grid import while boosting" : "Grid import while boosting (whole house)", `${s.importBoost.toFixed(2)} kWh`, delta(s.importBoost, r.importBoost, " kWh", true, 2)],
      ["Boost time", fmtHM(s.boostH * 60), delta(s.boostH * 60, r.boostH * 60, " min", false, 0)],
      ["Export not used", `${s.exportUnused.toFixed(2)} kWh`, delta(s.exportUnused, r.exportUnused, " kWh", true, 2)],
      ["Boosts / resets", `${s.starts} / ${s.resets}`, delta(s.starts + s.resets, r.starts + r.resets, "", true, 0)],
    ];
    const root = this.shadowRoot;
    root.getElementById("kpis").innerHTML = kpis
      .map(([l, v, d]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div>${d}</div>`)
      .join("");
    const dayName = this._day === startOfDay(new Date()) ? "today" : new Date(this._day).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
    root.getElementById("replay-head").innerHTML = `Replay of ${esc(dayName)} <span>${changed ? "with your new thresholds" : "simulated with your saved thresholds"}</span>`;
    root.getElementById("save").style.display = changed ? "" : "none";
    root.getElementById("reset").style.display = changed ? "" : "none";
    root.getElementById("note").textContent = changed
      ? "Replay of this day with your new thresholds. Save to apply them."
      : `Replay of this day with your saved thresholds${s.peak !== null ? `, peak setpoint ${s.peak.toFixed(1)} °C` : ""}. Drag a line to try other values.`;
  }

  // ---- interaction --------------------------------------------------------

  _valueAt(clientY, key) {
    const svg = this.shadowRoot.querySelector("svg");
    const { g, ymax, ymin } = this._geom;
    const yPx = clientY - svg.getBoundingClientRect().top;
    const w = ymax - ((yPx - g.top) / g.plotH) * (ymax - ymin);
    const th = THRESHOLDS.find((t) => t.key === key);
    return this._constrain(key, Math.round((th.sign * w) / 50) * 50);
  }

  _constrain(key, v) {
    const cur = this._current();
    const { ymax, ymin } = this._geom;
    if (key === "S") return Math.max(0, Math.min(-ymin, 5000, v));
    if (key === "L") return Math.max(0, Math.min(cur.H, v));
    return Math.max(cur.L, Math.min(ymax, 5000, v));
  }

  _onDown(e) {
    const k = e.target.closest?.("[data-k]")?.dataset.k;
    if (!k) return;
    e.preventDefault();
    this._drag = k;
    // Capture on the container: the line itself is re-rendered while dragging
    this.shadowRoot.getElementById("chart").setPointerCapture?.(e.pointerId);
  }

  _onMove(e) {
    if (!this._geom) return;
    if (this._drag) {
      const v = this._valueAt(e.clientY, this._drag);
      if (v !== this._current()[this._drag]) {
        this._draft = { ...this._draft, [this._drag]: v };
        this._schedule();
      }
      return;
    }
    const svg = this.shadowRoot.querySelector("svg");
    if (!svg) return;
    const { g, x0, x1, shown, iv } = this._geom;
    const xPx = e.clientX - svg.getBoundingClientRect().left;
    if (xPx < g.left || xPx > g.W - g.right) return this._setHover(null);
    const t = x0 + ((xPx - g.left) / (g.W - g.left - g.right)) * (x1 - x0);
    const idx = shown.findIndex((k) => t > k.t - iv && t <= k.t);
    this._setHover(idx >= 0 ? idx : null);
  }

  _onUp() {
    if (!this._drag) return;
    this._drag = null;
    this._renderChart();
  }

  _onKey(e) {
    const k = e.target.dataset?.k;
    if (!k || !["ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const th = THRESHOLDS.find((t) => t.key === k);
    const dir = (e.key === "ArrowUp" ? 1 : -1) * th.sign;
    this._draft = { ...this._draft, [k]: this._constrain(k, this._current()[k] + dir * 50) };
    this._renderChart();
  }

  _setHover(idx) {
    if (idx === this._hoverIdx) return;
    this._hoverIdx = idx;
    const el = this.shadowRoot.getElementById("hover");
    const k = idx !== null ? this._geom?.shown[idx] : null;
    if (!k) {
      el.textContent = "";
    } else {
      let power = k.bar === null ? "no data" : `${k.bar < 0 ? "export" : "import"} ${fmtW(k.bar)}`;
      if (k.hp !== null && k.hp !== undefined) power += ` · heat pump ${fmtW(k.hp)}`;
      const rule = k.rule.replace(/_/g, " ");
      el.textContent = `${hhmm(k.t - this._geom.iv)}–${hhmm(k.t)} · ${power} · ${rule} · setpoint ${k.sp.toFixed(1)} °C`;
    }
    this._schedule();
  }

  _schedule() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = null;
      this._renderChart();
    });
  }

  async _save() {
    const saved = this._saved();
    const calls = Object.entries(this._draft)
      .filter(([k, v]) => v !== saved[k] && this._entities.numbers[k])
      .map(([k, v]) => this._hass.callService("number", "set_value", { entity_id: this._entities.numbers[k], value: v }));
    try {
      await Promise.all(calls);
      this._draft = {};
    } catch (err) {
      this.shadowRoot.getElementById("note").textContent = `Couldn't save: ${err.message || err}`;
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("smart-heatpump-card")) {
  customElements.define("smart-heatpump-card", SmartHeatpumpCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "smart-heatpump-card",
    name: "Smart Heatpump solar boost tuner",
    description: "Grid power with draggable solar thresholds and a replay of the controller's decisions.",
    preview: false,
  });
}
