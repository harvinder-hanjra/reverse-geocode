/**
 * main.js — Atlas.
 *
 * Offline world map. No tile server. ADM2 GeoJSON is the entire basemap.
 * Hover over any land area for instant administrative hierarchy lookup.
 */

import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

import { loadZ0 } from './z0.js';
import { UI } from './ui.js';

// ── Service Worker ────────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(() => {
    const poll = () => navigator.serviceWorker.controller
      ? ui.setOffline(true) : setTimeout(poll, 700);
    setTimeout(poll, 900);
  }).catch(() => {});
}

// ── Map — blank style, #000 ocean, no external tile source ───────────────────
const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {},
    layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#000' } }],
  },
  center: [10, 20],
  zoom: 2,
  maxPitch: 0,
  attributionControl: false,
});

map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

// ── State ─────────────────────────────────────────────────────────────────────
const ui       = new UI();
let z0         = null;
let adminMap   = null;   // Map<admin_id, { country, adm1, adm2 }>
let lastHlId   = null;
let mapReady   = false;
let dataReady  = false;
let geojson    = null;
let rafPending = false;
let pendingX   = 0;
let pendingY   = 0;

// ── Load data in parallel with map boot ───────────────────────────────────────
Promise.all([
  fetch('/data/adm2_render.geojson').then(r => r.json()),
  loadZ0('/data/z0_geo.bin'),
]).then(([gj, z0inst]) => {
  // Stamp per-country fill color as a feature property (computed once here,
  // not per-render). MapLibre copies this to its worker thread.
  for (const f of gj.features) f.properties._c = countryFill(f.properties.country);

  geojson  = gj;
  z0       = z0inst;
  adminMap = new Map(gj.features.map(f => [f.id, f.properties]));

  dataReady = true;
  if (mapReady) attach();
});

map.on('load', () => { mapReady = true; if (dataReady) attach(); });

// ── Attach GeoJSON layers ─────────────────────────────────────────────────────
function attach() {
  map.addSource('adm2', { type: 'geojson', data: geojson, generateId: false });

  // Land — per-country dark fill
  map.addLayer({
    id: 'land',
    type: 'fill',
    source: 'adm2',
    paint: { 'fill-color': ['get', '_c'], 'fill-opacity': 1 },
  });

  // ADM2 dividing lines (barely visible; just enough to read region at close zoom)
  map.addLayer({
    id: 'dividers',
    type: 'line',
    source: 'adm2',
    paint: { 'line-color': '#000', 'line-width': 0.4, 'line-opacity': 0.7 },
  });

  // Highlight — white wash on hover, GPU feature-state only
  map.addLayer({
    id: 'hl',
    type: 'fill',
    source: 'adm2',
    paint: {
      'fill-color': '#fff',
      'fill-opacity': [
        'case', ['boolean', ['feature-state', 'on'], false], 0.12, 0,
      ],
    },
  });

  // Highlight border — sharp white outline
  map.addLayer({
    id: 'hl-border',
    type: 'line',
    source: 'adm2',
    paint: {
      'line-color': '#fff',
      'line-width':   ['case', ['boolean', ['feature-state', 'on'], false], 1, 0],
      'line-opacity': ['case', ['boolean', ['feature-state', 'on'], false], 0.55, 0],
    },
  });

  const loading = document.getElementById('loading');
  loading.classList.add('done');
  loading.addEventListener('transitionend', () => loading.remove(), { once: true });
}

// ── Interaction ───────────────────────────────────────────────────────────────
const canvas = map.getCanvas();

canvas.addEventListener('mousemove', e => {
  pendingX = e.clientX;
  pendingY = e.clientY;
  if (!rafPending) { rafPending = true; requestAnimationFrame(frame); }
}, { passive: true });

canvas.addEventListener('mouseleave', () => { ui.hide(); clearHl(); }, { passive: true });

// Touch tap
canvas.addEventListener('touchend', e => {
  if (e.changedTouches.length !== 1) return;
  if (e.timeStamp - (map._ts || 0) > 280) return;
  e.preventDefault();
  const t = e.changedTouches[0];
  runQuery(t.clientX, t.clientY);
}, { passive: false });

canvas.addEventListener('touchstart', e => { map._ts = e.timeStamp; }, { passive: true });

function frame() {
  rafPending = false;
  if (z0 && mapReady) runQuery(pendingX, pendingY);
}

function runQuery(cx, cy) {
  const t0     = performance.now();
  const rect   = canvas.getBoundingClientRect();
  const ll     = map.unproject([cx - rect.left, cy - rect.top]);
  const { lat, lng } = ll;
  const id     = z0.lookup(lat, lng);
  const ms     = performance.now() - t0;
  const coords = `${fmtCoord(lat, 'N', 'S')}  ${fmtCoord(lng, 'E', 'W')}`;

  setHl(id);

  if (id !== null) {
    const p = adminMap.get(id);
    if (p) {
      ui.update(
        { flag: toFlag(p.country), country: p.country, adm1: p.adm1, adm2: p.adm2, ms, coords },
        cx, cy, coords,
      );
      return;
    }
  }
  ui.update(null, cx, cy, coords);

}

// ── Highlight ─────────────────────────────────────────────────────────────────
function setHl(id) {
  if (lastHlId === id) return;
  clearHl();
  if (id !== null) { map.setFeatureState({ source: 'adm2', id }, { on: true }); lastHlId = id; }
}

function clearHl() {
  if (lastHlId !== null) {
    map.setFeatureState({ source: 'adm2', id: lastHlId }, { on: false });
    lastHlId = null;
  }
}

// ── Per-country fill: hash alpha-3 → narrow dark hue band ────────────────────
function countryFill(a3) {
  let h = 0;
  for (let i = 0; i < (a3?.length ?? 0); i++) h = (Math.imul(h, 31) + a3.charCodeAt(i)) | 0;
  const hue  = ((h >>> 0) % 40) + 195;     // 195–235° : blue-indigo only
  const sat  = 20 + ((h >>> 8)  & 0xF);    // 20–35%
  const lite = 12 + ((h >>> 12) & 0x7);    // 12–19%  — all very dark
  return `hsl(${hue},${sat}%,${lite}%)`;
}

// ── Flag emoji ─────────────────────────────────────────────────────────────────
const A3A2 = {
  AFG:'AF',ALB:'AL',DZA:'DZ',AND:'AD',AGO:'AO',ATG:'AG',ARG:'AR',ARM:'AM',
  AUS:'AU',AUT:'AT',AZE:'AZ',BHS:'BS',BHR:'BH',BGD:'BD',BRB:'BB',BLR:'BY',
  BEL:'BE',BLZ:'BZ',BEN:'BJ',BTN:'BT',BOL:'BO',BIH:'BA',BWA:'BW',BRA:'BR',
  BRN:'BN',BGR:'BG',BFA:'BF',BDI:'BI',CPV:'CV',KHM:'KH',CMR:'CM',CAN:'CA',
  CAF:'CF',TCD:'TD',CHL:'CL',CHN:'CN',COL:'CO',COM:'KM',COD:'CD',COG:'CG',
  CRI:'CR',CIV:'CI',HRV:'HR',CUB:'CU',CYP:'CY',CZE:'CZ',DNK:'DK',DJI:'DJ',
  DOM:'DO',ECU:'EC',EGY:'EG',SLV:'SV',GNQ:'GQ',ERI:'ER',EST:'EE',SWZ:'SZ',
  ETH:'ET',FJI:'FJ',FIN:'FI',FRA:'FR',GAB:'GA',GMB:'GM',GEO:'GE',DEU:'DE',
  GHA:'GH',GRC:'GR',GTM:'GT',GIN:'GN',GNB:'GW',GUY:'GY',HTI:'HT',HND:'HN',
  HUN:'HU',ISL:'IS',IND:'IN',IDN:'ID',IRN:'IR',IRQ:'IQ',IRL:'IE',ISR:'IL',
  ITA:'IT',JAM:'JM',JPN:'JP',JOR:'JO',KAZ:'KZ',KEN:'KE',PRK:'KP',KOR:'KR',
  KWT:'KW',KGZ:'KG',LAO:'LA',LVA:'LV',LBN:'LB',LSO:'LS',LBR:'LR',LBY:'LY',
  LIE:'LI',LTU:'LT',LUX:'LU',MDG:'MG',MWI:'MW',MYS:'MY',MDV:'MV',MLI:'ML',
  MLT:'MT',MRT:'MR',MUS:'MU',MEX:'MX',MDA:'MD',MCO:'MC',MNG:'MN',MNE:'ME',
  MAR:'MA',MOZ:'MZ',MMR:'MM',NAM:'NA',NPL:'NP',NLD:'NL',NZL:'NZ',NIC:'NI',
  NER:'NE',NGA:'NG',MKD:'MK',NOR:'NO',OMN:'OM',PAK:'PK',PAN:'PA',PNG:'PG',
  PRY:'PY',PER:'PE',PHL:'PH',POL:'PL',PRT:'PT',QAT:'QA',ROU:'RO',RUS:'RU',
  RWA:'RW',KNA:'KN',LCA:'LC',VCT:'VC',WSM:'WS',STP:'ST',SAU:'SA',SEN:'SN',
  SRB:'RS',SLE:'SL',SGP:'SG',SVK:'SK',SVN:'SI',SLB:'SB',SOM:'SO',ZAF:'ZA',
  SSD:'SS',ESP:'ES',LKA:'LK',SDN:'SD',SUR:'SR',SWE:'SE',CHE:'CH',SYR:'SY',
  TWN:'TW',TJK:'TJ',TZA:'TZ',THA:'TH',TLS:'TL',TGO:'TG',TON:'TO',TTO:'TT',
  TUN:'TN',TUR:'TR',TKM:'TM',TUV:'TV',UGA:'UG',UKR:'UA',ARE:'AE',GBR:'GB',
  USA:'US',URY:'UY',UZB:'UZ',VUT:'VU',VEN:'VE',VNM:'VN',YEM:'YE',ZMB:'ZM',
  ZWE:'ZW',XKX:'XK',
};

function toFlag(a3) {
  const a2 = A3A2[a3?.toUpperCase()];
  if (!a2) return '';
  const a = a2.charCodeAt(0) - 65, b = a2.charCodeAt(1) - 65;
  return (a < 0 || a > 25 || b < 0 || b > 25)
    ? '' : String.fromCodePoint(0x1F1E6 + a, 0x1F1E6 + b);
}

// ── Coordinate formatting ─────────────────────────────────────────────────────
function fmtCoord(v, pos, neg) {
  return `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;
}
