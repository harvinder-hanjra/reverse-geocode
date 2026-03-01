/**
 * ui.js — Tooltip and offline dot.
 *
 * Uber design language: solid black, precise weight hierarchy,
 * no blur, no glass, no decorative border. Information only.
 */

export class UI {
  constructor() {
    this._tip     = null;
    this._dot     = null;
    this._lastId  = null;
    this._build();
  }

  /**
   * @param {object} p  — { flag, country, adm1, adm2, lat, lng, ms }
   *                       adm1/adm2 may be empty. lat/lng are numbers.
   *                       Pass null to show ocean state.
   */
  update(p, cx, cy, coords) {
    const el = this._tip;

    if (!p) {
      el.innerHTML =
        `<span class="t-ocean">Ocean</span>` +
        `<span class="t-meta"><span class="t-coords">${esc(coords)}</span></span>`;
    } else {
      const region = [p.adm1, p.adm2].filter(Boolean).join(' · ');
      el.innerHTML =
        `<span class="t-flag">${p.flag}</span>` +
        `<span class="t-country">${esc(p.country)}</span>` +
        (region ? `<span class="t-region">${esc(region)}</span>` : '') +
        `<span class="t-meta">` +
        `<span class="t-coords">${esc(coords)}</span>` +
        `<span class="t-ms">${fmtMs(p.ms)}</span></span>`;
    }

    const m  = 12;
    const tw = el.offsetWidth  || 210;
    const th = el.offsetHeight || 72;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let tx = cx + 18, ty = cy - 8;
    if (tx + tw > vw - m) tx = cx - tw - 18;
    if (ty + th > vh - m) ty = cy - th - 8;
    ty = Math.max(m, ty);
    tx = Math.max(m, tx);

    el.style.transform  = `translate(${tx}px,${ty}px)`;
    el.style.opacity    = '1';
    el.style.visibility = 'visible';
  }

  hide() {
    this._tip.style.opacity    = '0';
    this._tip.style.visibility = 'hidden';
  }


  setOffline(ready) {
    this._dot.dataset.s = ready ? '1' : '0';
  }

  _build() {
    const tip = document.createElement('div');
    tip.id = 'tip';
    document.body.appendChild(tip);
    this._tip = tip;

    const dot = document.createElement('div');
    dot.id = 'offline-dot';
    dot.dataset.s = '0';
    document.body.appendChild(dot);
    this._dot = dot;
  }
}

function fmtMs(ms) {
  return ms < 0.1 ? `${(ms * 1000).toFixed(0)}µs` : `${ms.toFixed(2)}ms`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const css = `
  #tip {
    position: fixed;
    top: 0; left: 0;
    pointer-events: none;
    z-index: 100;
    opacity: 0;
    visibility: hidden;
    will-change: transform;
    transition: opacity 0.06s linear;

    background: #000;
    color: #fff;
    padding: 12px 14px 10px;
    min-width: 160px;
    max-width: 240px;

    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  #tip .t-flag    { font-size: 18px; line-height: 1; margin-bottom: 4px; }
  #tip .t-country { font-size: 15px; font-weight: 600; letter-spacing: -0.3px;
                    line-height: 1.2; color: #fff; }
  #tip .t-region  { font-size: 12px; color: #888; line-height: 1.3; }
  #tip .t-ocean   { font-size: 13px; color: #555; font-style: italic; }
  #tip .t-meta    { display: flex; justify-content: space-between; align-items: baseline;
                    margin-top: 6px; padding-top: 6px;
                    border-top: 1px solid #222; }
  #tip .t-coords  { font-size: 10px; color: #666; font-family: 'SF Mono', 'Menlo', monospace; }
  #tip .t-ms      { font-size: 10px; color: #1db954; font-family: 'SF Mono', 'Menlo', monospace; }

  /* Offline indicator dot — bottom-left, unobtrusive */
  #offline-dot {
    position: fixed;
    bottom: 28px;
    left: 14px;
    z-index: 100;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    transition: background 0.4s;
  }
  #offline-dot[data-s="0"] { background: #333; }
  #offline-dot[data-s="1"] { background: #1db954; }

  .maplibregl-ctrl-logo  { display: none !important; }
  .maplibregl-ctrl-attrib { opacity: 0.3 !important; }
`;

const s = document.createElement('style');
s.textContent = css;
document.head.appendChild(s);
