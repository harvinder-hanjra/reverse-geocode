/**
 * main.js — H3 Atlas.
 *
 * Offline world map backed by the H3/LKHA0001 reverse geocoder.
 * Hover for instant lookup via h3_geo.bin + h3_names.json.
 */

import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

import { loadH3 } from './h3.js';
import { UI } from './ui.js';

// ── Service Worker ─────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(() => {
    const poll = () => navigator.serviceWorker.controller
      ? ui.setOffline(true) : setTimeout(poll, 700);
    setTimeout(poll, 900);
  }).catch(() => {});
}

// ── Map ────────────────────────────────────────────────────────────────────
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

// ── State ──────────────────────────────────────────────────────────────────
const ui      = new UI();
let h3geo     = null;
let adminMap  = null;
let lastHlId  = null;
let mapReady  = false;
let dataReady = false;
let geojson   = null;
let rafPending = false;
let pendingX  = 0;
let pendingY  = 0;

// ── Data URLs — local in dev, GitHub release in production ─────────────────
const DATA = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
  ? '/data'
  : 'https://pub-4ad65c005bad4ef08b4bca5befc474b8.r2.dev';

// ── Load data ──────────────────────────────────────────────────────────────
Promise.all([
  fetch('/data/adm2_render.geojson').then(r => r.json()),
  loadH3(`${DATA}/h3_geo.bin`, `${DATA}/h3_names.json`),
]).then(([gj, h3inst]) => {
  for (const f of gj.features) f.properties._c = countryFill(f.properties.country);
  geojson  = gj;
  h3geo    = h3inst;
  adminMap = new Map(gj.features.map(f => [f.id, f.properties]));
  dataReady = true;
  if (mapReady) attach();
});

map.on('load', () => { mapReady = true; if (dataReady) attach(); });

// ── Attach GeoJSON layers ──────────────────────────────────────────────────
function attach() {
  map.addSource('adm2', { type: 'geojson', data: geojson, generateId: false });

  map.addLayer({
    id: 'land', type: 'fill', source: 'adm2',
    paint: { 'fill-color': ['get', '_c'], 'fill-opacity': 1 },
  });
  map.addLayer({
    id: 'dividers', type: 'line', source: 'adm2',
    paint: { 'line-color': '#000', 'line-width': 0.4, 'line-opacity': 0.7 },
  });
  map.addLayer({
    id: 'hl', type: 'fill', source: 'adm2',
    paint: {
      'fill-color': '#fff',
      'fill-opacity': ['case', ['boolean', ['feature-state', 'on'], false], 0.12, 0],
    },
  });
  map.addLayer({
    id: 'hl-border', type: 'line', source: 'adm2',
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

// ── Interaction ────────────────────────────────────────────────────────────
const canvas = map.getCanvas();

canvas.addEventListener('mousemove', e => {
  pendingX = e.clientX; pendingY = e.clientY;
  if (!rafPending) { rafPending = true; requestAnimationFrame(frame); }
}, { passive: true });

canvas.addEventListener('mouseleave', () => { ui.hide(); clearHl(); }, { passive: true });

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
  if (h3geo && mapReady) runQuery(pendingX, pendingY);
}

function runQuery(cx, cy) {
  const t0   = performance.now();
  const rect = canvas.getBoundingClientRect();
  const x    = cx - rect.left;
  const y    = cy - rect.top;
  const ll   = map.unproject([x, y]);
  const { lat, lng } = ll;

  const result = h3geo.lookup(lat, lng);
  const ms     = performance.now() - t0;
  const coords = `${fmtCoord(lat, 'N', 'S')}  ${fmtCoord(lng, 'E', 'W')}`;

  // Highlight via rendered feature
  const rendered = map.queryRenderedFeatures([x, y], { layers: ['land'] });
  setHl(rendered[0]?.id ?? null);

  if (result) {
    // H3 binary has flat country structure; fall back to GeoJSON name if available
    const hlFeat = rendered[0];
    const geoP   = hlFeat ? adminMap.get(hlFeat.id) : null;
    const country = geoP?.country || result.country;
    const adm1    = geoP?.adm1    || result.adm1;
    const adm2    = result.adm2   || geoP?.adm2 || '';
    ui.update(
      { flag: toFlag(country), country, adm1, adm2, ms, coords },
      cx, cy, coords,
    );
    return;
  }
  ui.update(null, cx, cy, coords);
}

// ── Highlight ──────────────────────────────────────────────────────────────
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

// ── Per-country fill ───────────────────────────────────────────────────────
function countryFill(a3) {
  let h = 0;
  for (let i = 0; i < (a3?.length ?? 0); i++) h = (Math.imul(h, 31) + a3.charCodeAt(i)) | 0;
  const hue  = ((h >>> 0) % 40) + 195;
  const sat  = 20 + ((h >>> 8)  & 0xF);
  const lite = 12 + ((h >>> 12) & 0x7);
  return `hsl(${hue},${sat}%,${lite}%)`;
}

// ── Flag emoji ─────────────────────────────────────────────────────────────
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

function fmtCoord(v, pos, neg) {
  return `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;
}
