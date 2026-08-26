// UI wiring: parameter row (with the desktop's clamp/write-back and
// f_c display-precision rules), targets menu, frame cycling, map mode,
// drift scans and save.  Port of the desktop main() closures.

import { jdFromDate, decimalYear, galToEq, raDecToAltAz, sepDeg }
  from "./coordinates.js";
import { frameOffset, continuumSources } from "./ephemeris.js";
import { C_LIGHT, F_HI } from "./skydata.js";
import { simDate, setFixedTime, isFixed } from "./clock.js";

const TARGETS = [
  ["Galactic centre wings", 0.0, 0.0, 3.0, "broad velocity wings"],
  ["Inner Galaxy (l=30)", 30.0, 0.0, 2.0, "terminal velocities"],
  ["Vulpecula rift (l=60)", 60.0, 0.0, 2.0, "local + Sagittarius arm"],
  ["Cygnus X (l=80)", 80.0, 0.0, 2.0, "star-forming complex"],
  ["Outer Arm (l=110)", 110.0, 0.0, 2.0, "distant spiral arm"],
  ["Perseus arm (l=134)", 134.0, -1.0, 2.0, "double-peaked line"],
  ["Anticentre (l=180)", 180.0, 0.0, 2.0, "zero-velocity direction"],
  ["Rosette (l=206)", 206.0, -2.0, 2.0, "3rd quadrant plane"],
  ["Third quadrant (l=220)", 220.0, 0.0, 2.0, "negative velocities"],
  ["M31 (Andromeda)", 121.17, -21.57, 5.0, "H I at -300 km/s"],
  ["M33 (Triangulum)", 133.6, -31.3, 3.0, "H I at -180 km/s"],
  ["LMC", 280.5, -32.9, 5.0, "H I at +280 km/s"],
  ["SMC", 302.8, -44.3, 3.0, "H I at +160 km/s"],
  ["HVC Complex A", 150.0, 35.0, 3.0, "infalling, -180 km/s"],
  ["HVC Complex C", 100.0, 45.0, 3.0, "infalling, -120 km/s"],
  ["Smith Cloud", 39.0, -13.0, 2.0, "infalling, +100 km/s"],
  ["Lockman Hole", 150.0, 53.0, 2.0, "minimum H I, off-position"],
  ["Celestial pole", 122.9, 27.1, 2.0, "zero drift rate"],
];

const FRAME_NAMES = { lsr: "LSR", ssb: "SSB", topo: "Topo" };

export function setupUI(cfg) {
  const { sky, map, plot, els, site } = cfg;
  // The site the page opened with, for Reset: the site boxes edit `site`
  // in place, so the defaults have to be remembered here.
  const siteDefault = { name: site.name, lat: site.lat, lon: site.lon };
  const state = { last: null, frame: "lsr", params: null, mode: "hi" };
  const messages = [];

  function message(text) {
    messages.push(text);
    while (messages.length > 4) messages.shift();
    els.console.textContent = messages.join("\n");
    console.log(text);
  }

  // ---- parameter row ------------------------------------------------
  const boxes = {
    l: els.l, b: els.b, fw: els.fw,
    nc: els.nc, ts: els.ts, ti: els.ti, sd: els.sd,
  };

  // The channel count the instrument itself records for the band in force:
  // the H I product's fine channels, or the continuum product's coarse ones.
  // Falls back to the compact data's own grid when there is no instrument.
  function nativeChannels() {
    const inst = sky.instrument;
    if (inst) {
      const width = inst.h1_channel_hz;
      if (state.mode !== "cont" && width) return Math.round(sky.bwHz / width);
      if (state.mode === "cont" && inst.wide_channel_hz)
        return Math.round(sky.bwHz / inst.wide_channel_hz);
    }
    return sky.k1 > sky.k0 + 1 ? sky.k1 - sky.k0
         : Math.max(2, Math.round(sky.bwHz / 6.1e3));
  }

  function initBoxes() {
    boxes.l.value = "132.0";
    boxes.b.value = "-1.0";
    boxes.fw.value = sky.fwhm.toFixed(2);
    boxes.nc.value = `${nativeChannels()}`;
    boxes.ts.value = `${sky.tsys}`;
    boxes.ti.value = `${sky.tint}`;
    boxes.sd.value = "240";
  }

  // The band read-out beside the map toggle: what the instrument records
  // for this map type, and which of the two per-mode boxes is offered.
  function showBand() {
    const cont = state.mode === "cont";
    els.bandLabel.textContent = cont ? "continuum band" : "H I band";
    // Centre and width, nothing else: the channel count has its own box.
    els.bandText.textContent =
      `${(sky.fc / 1e6).toFixed(3)} MHz, BW ${(sky.bwHz / 1e6).toFixed(1)} MHz`;
    els.ncGroup.hidden = cont;
    els.sdGroup.hidden = !cont;
  }

  function writeBack(box, val, label) {
    if (box.value.trim() && Math.abs(parseFloat(box.value) -
        parseFloat(val)) > 1e-9) {
      box.value = val;
      message(`Clamped ${label} to ${val}`);
    }
  }

  function applyParams() {
    const num = (box) => parseFloat(box.value);
    let glon = num(boxes.l), glat = num(boxes.b), fwhm = num(boxes.fw);
    let tint = num(boxes.ti);
    const tsText = boxes.ts.value.trim();
    // empty box = no noise model; 0 is a valid (ideal) receiver
    let tsys = tsText ? parseFloat(tsText) : null;
    const ncText = boxes.nc.value.trim();
    let nchan = ncText ? Math.trunc(parseFloat(ncText)) : null;
    if (![glon, glat, fwhm, tint].every(Number.isFinite)
        || (tsText && !Number.isFinite(tsys))
        || (ncText && !Number.isFinite(nchan))) {
      message("Could not parse the parameter boxes.");
      return null;
    }
    glon = ((glon % 360) + 360) % 360;
    glat = Math.min(90, Math.max(-90, glat));
    fwhm = Math.abs(fwhm);
    tint = Math.min(1e7, Math.max(1e-3, Math.abs(tint)));
    if (tsys !== null) tsys = Math.min(1e6, Math.max(0.0, tsys));
    if (nchan !== null) nchan = Math.min(65536, Math.max(2, nchan));
    if (fwhm > 0 && fwhm < sky.minFwhm)
      message(`Beam ${fwhm.toFixed(2)}° is finer than the compact ` +
              `dataset supports; using ${sky.minFwhm.toFixed(2)}°.`);
    fwhm = fwhm > 0 ? Math.min(90.0, Math.max(sky.minFwhm, fwhm))
                    : sky.fwhm;
    writeBack(boxes.b, `${glat}`, "b (°)");
    writeBack(boxes.fw, `${fwhm}`, "beam (°)");
    writeBack(boxes.ti, `${tint}`, "τ (s)");
    if (ncText) writeBack(boxes.nc, `${nchan}`, "channels");
    if (Math.abs(fwhm - sky.fwhm) > 1e-6) sky.setBeam(fwhm);
    map.setDisplayBeam(sky.fwhm);
    updateMapTitle();
    sky.tsys = tsys;
    sky.tint = tint;
    // The band is the instrument's for the mode (applyModeBand); only the
    // channel count is the observer's, and only for a spectrum.
    sky.nchan = state.mode === "cont" ? null : nchan;
    return { glon, glat };
  }

  // map title, opposite the pointing readout — desktop parity
  function updateMapTitle() {
    els.mapTitle.textContent = state.mode === "cont"
      ? "1420 MHz continuum (Stockert/Villa-Elisa) — click to point " +
        `the dish (beam ${sky.fwhm.toFixed(1)}°)`
      : "HI4PI N_HI — click to point the dish " +
        `(beam ${sky.fwhm.toFixed(1)}°)`;
  }

  // ---- lower panel --------------------------------------------------
  function velLabel(glon, glat) {
    const jd = jdFromDate(simDate());
    const shift = frameOffset(glon, glat, state.frame, jd, site);
    const names = {
      lsr: "LSR radial velocity",
      ssb: "SSB (barycentric) radial velocity",
      topo: `topocentric radial velocity (${site.name}, `
            + (isFixed() ? "pinned time)" : "now)"),
    };
    return { frame: FRAME_NAMES[state.frame] === "Topo"
                    ? `Topo (${site.name})` : FRAME_NAMES[state.frame],
             text: names[state.frame] + "  (km/s)", shift };
  }

  function render() {
    const { glon, glat, v, t, sigma, tcont } = state.last;
    const lbl = velLabel(glon, glat);
    const f = new Float64Array(v.length);
    for (let i = 0; i < v.length; i++)
      f[i] = F_HI * (1.0 - (v[i] + lbl.shift) / C_LIGHT) / 1e6;
    let peak = -Infinity;
    for (const x of t) if (Number.isFinite(x)) peak = Math.max(peak, x);
    let title = `l=${glon.toFixed(1)}°, b=${glat.toFixed(1)}°   ` +
                `peak T_A=${peak.toFixed(1)} K`;
    if (sigma) {
      let s0 = Infinity;
      for (const s of sigma) s0 = Math.min(s0, s);
      title += `,  σ₀=${(s0 * 1e3).toFixed(0)} mK`;
    }
    if (tcont > 0.005) title += `,  continuum ${tcont.toFixed(2)} K`;
    if (state.frame !== "lsr")
      title += `,  shift ${(lbl.shift / 1e3).toFixed(1)} km/s`;
    plot.showSpectrum(f, t, title, lbl);
  }

  function renderDrift() {
    const { glon, glat } = state.last;
    let dur = parseFloat(boxes.sd.value);
    if (!Number.isFinite(dur)) dur = 240.0;
    const durC = Math.min(1435.0, Math.max(2.0, dur));
    if (Math.abs(durC - dur) > 1e-9) {
      boxes.sd.value = `${durC}`;
      message(`Clamped scan duration to ${durC}`);
    }
    const d = sky.driftScan(glon, glat, durC);
    map.setTrack(Array.from(d.trackL), Array.from(d.trackB));
    const mins = Array.from(d.dtH, (h) => h * 60);
    // per-sample noise: tau per sample, duration just sets the count
    const vAx = state.last.v;
    let bwUse = sky.bwHz;
    if (vAx.length > 1) {
      const df = Math.abs(F_HI * (vAx[1] - vAx[0]) / C_LIGHT);
      bwUse = vAx.length * df;
    }
    let smp = null, noiseTxt = "";
    const tau = sky.tint;
    if (sky.tsys !== null && durC * 60 >= tau) {
      let n = Math.trunc(durC * 60 / tau);
      if (n > 20000) {
        message(`Drift scan: showing 20000 of ${n} samples`);
        n = 20000;
      }
      const tmins = [], tvals = [], sigs = [];
      for (let i = 0; i < n; i++) {
        const m = -durC / 2 + durC * i / (n - 1 || 1);
        // linear interp of tbar
        const x = (m - mins[0]) / (mins[mins.length - 1] - mins[0])
                * (mins.length - 1);
        const i0 = Math.max(0, Math.min(mins.length - 2, Math.floor(x)));
        const tb = d.tbar[i0] + (d.tbar[i0 + 1] - d.tbar[i0]) * (x - i0);
        // Radiometer floor plus the measured common-mode instability of
        // band-integrated power, in quadrature - per channel the receiver is
        // thermal, but the band integral sits ~3x above its floor. See
        // GAIN_INSTABILITY in instrument.py for the measurement.
        const sigRad = (sky.tsys + tb) / Math.sqrt(sky.npol * bwUse * tau);
        const sigG = sky.gainSig * (sky.tsys + tb);
        const sig = Math.hypot(sigRad, sigG);
        tmins.push(m);
        tvals.push(tb + sky.rng.normal() * sig);
        sigs.push(sig);
      }
      smp = { mins: tmins, t: tvals };
      const sMed = sigs.slice().sort((a, b) => a - b)[sigs.length >> 1];
      const gMed = sky.gainSig * (sky.tsys + d.tbar[d.tbar.length >> 1]);
      noiseTxt = `,  τ/sample ${tau} s, σ≈${(sMed * 1e3).toFixed(0)} mK` +
                 ` (gain noise ${(gMed * 1e3).toFixed(0)})`;
    }
    const half = (sky.fwhm / 2) / (15.041 * d.cosd) * 60.0;
    const title = `drift scan  l=${glon.toFixed(1)}°, ` +
        `b=${glat.toFixed(1)}° (dec ${d.dec >= 0 ? "+" : ""}` +
        `${d.dec.toFixed(1)}°)   BW ${(bwUse / 1e6).toPrecision(2)} MHz` +
        ` at ${(sky.fc / 1e6).toFixed(1)} MHz${noiseTxt}`;
    plot.showDrift(mins, Array.from(d.tbar), smp, null, half, title);
  }

  function point(glon, glat) {
    let s;
    try {
      s = sky.spectrum(glon, glat);
    } catch (err) {
      message(`No spectrum: ${err.message}`);
      return;
    }
    state.last = { glon, glat, ...s };
    state.params = snapshot(glon, glat);
    map.setBeamMarker(glon, glat, sky.fwhm);
    if (state.mode === "cont") renderDrift();
    else { map.setTrack(null); render(); }
    map.draw();
    updateInfo();
  }

  const snapshot = (l, b) =>
      JSON.stringify([l, b, sky.fwhm, sky.tsys, sky.tint, sky.bwHz,
                      sky.fc, sky.nchan]);

  function onGo() {
    const p = applyParams();
    if (!p) return;
    if (snapshot(p.glon, p.glat) !== state.params) point(p.glon, p.glat);
  }

  // ---- controls -----------------------------------------------------
  for (const box of [boxes.l, boxes.b, boxes.fw,
                     boxes.nc, boxes.ts, boxes.ti]) {
    box.addEventListener("change", onGo);
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { box.blur(); }
      e.stopPropagation();
    });
  }
  boxes.sd.addEventListener("change", () => {
    if (state.mode === "cont" && state.last) renderDrift();
  });
  boxes.sd.addEventListener("keydown", (e) => e.stopPropagation());

  els.frameBtn.addEventListener("click", () => {
    const order = ["lsr", "ssb", "topo"];
    state.frame = order[(order.indexOf(state.frame) + 1) % 3];
    els.frameBtn.textContent = "Frame: " + FRAME_NAMES[state.frame];
    if (state.last && state.mode !== "cont") render();
  });

  // The band follows the mode, from the fixed instrument (scheduler issue
  // #27): the H I sub-band for a spectrum, the continuum band - which holds
  // no hydrogen - for a drift scan. So the simulated drift scan is the
  // continuum product a scheduled one will record, line excluded.
  function bandForMode(mode) {
    const inst = sky.instrument;
    if (!inst) return null;
    const band = mode === "cont" ? inst.continuum_band_hz : inst.h1_band_hz;
    if (!band) return null;
    return { bwHz: band[1] - band[0], fcHz: 0.5 * (band[0] + band[1]) };
  }

  function applyModeBand(mode) {
    const b = bandForMode(mode);
    if (b && (Math.abs(b.bwHz - sky.bwHz) > 1 || Math.abs(b.fcHz - sky.fc) > 1)) {
      sky.setBand(b.bwHz, b.fcHz);
      // A new band has a new native channel count; a typed count is kept.
      if (!boxes.nc.value.trim() || boxes.nc.dataset.native === boxes.nc.value.trim()) {
        boxes.nc.value = `${nativeChannels()}`;
      }
    }
    boxes.nc.dataset.native = `${nativeChannels()}`;
    showBand();
  }

  els.mapBtn.addEventListener("click", () => {
    state.mode = state.mode === "hi" ? "cont" : "hi";
    const cont = state.mode === "cont";
    els.mapBtn.textContent = cont ? "Map: continuum" : "Map: H I";
    els.frameBtn.disabled = cont;
    boxes.sd.disabled = !cont;
    map.setMode(cont ? "cont" : "hi");
    applyModeBand(state.mode);
    updateMapTitle();
    if (state.last) {
      if (cont) renderDrift();
      else { map.setTrack(null); render(); }
      map.draw();
    }
  });

  // targets menu (rebuilt when the site changes: never-rises flags)
  const menu = els.targetsMenu;
  function buildTargetsMenu() {
    menu.innerHTML = "";
    for (let i = 0; i < TARGETS.length + sky.sources.length; i++) {
      const row = document.createElement("div");
      row.className = "target-row";
      let name, l, b, desc;
      if (i < TARGETS.length) {
        [name, l, b, , desc] = TARGETS[i];
      } else {
        const s = sky.sources[i - TARGETS.length];
        name = s.name; l = s.l; b = s.b;
        desc = { "Cyg A": "radio galaxy", "Cas A": "supernova remnant",
                 "Tau A": "Crab nebula", "Sun": "launch position",
                 "Moon": "launch position" }[s.name] + "  [continuum]";
      }
      const dec = galToEq(l, b).dec;
      const gone = site.lat >= 0 ? dec < site.lat - 90
                                 : dec > site.lat + 90;
      row.innerHTML =
        `<span${gone ? ' class="never"' : ""}>${name} (${desc})` +
        `${gone ? "   [never rises]" : ""}</span>` +
        `<span class="coords">(${l.toFixed(1)}°, ${b >= 0 ? "+" : ""}` +
        `${b.toFixed(1)}°)</span>`;
      row.addEventListener("click", () => {
        boxes.l.value = l.toFixed(2);
        boxes.b.value = b.toFixed(2);
        // The band is the instrument's; a target whose profile wants more
        // of it than the H I sub-band holds is said so, not widened.
        const reqBw = i < TARGETS.length ? TARGETS[i][3] : 2.0;
        if (sky.bwHz < reqBw * 1e6 - 1)
          message(`${name}: its profile spans ${reqBw} MHz, wider than the ` +
                  `instrument's ${(sky.bwHz / 1e6).toFixed(1)} MHz H I band.`);
        menu.style.display = "none";
        message(`Target: ${name}`);
        const p = applyParams();
        if (p) point(p.glon, p.glat);
      });
      menu.appendChild(row);
    }
  }
  buildTargetsMenu();

  // observer site: name/lat/lon boxes; the Moon's parallax, horizon,
  // visibility loops and the topocentric frame all follow
  function initSiteBoxes() {
    els.siteName.value = site.name;
    els.siteLat.value = `${site.lat}`;
    els.siteLon.value = `${site.lon}`;
  }
  function onSiteChange() {
    let lat = parseFloat(els.siteLat.value);
    let lon = parseFloat(els.siteLon.value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      message("Could not parse the site coordinates.");
      initSiteBoxes();
      return;
    }
    lat = Math.min(90, Math.max(-90, lat));
    lon = ((lon + 180) % 360 + 360) % 360 - 180;
    site.name = els.siteName.value.trim() || "site";
    site.lat = lat;
    site.lon = lon;
    initSiteBoxes();
    const jd = jdFromDate(simDate());
    sky.setSources(continuumSources(jd, decimalYear(jd), site));
    map.sources = sky.sources;
    buildTargetsMenu();
    map.draw();
    updateInfo();
    message(`Site: ${site.name} (${lat.toFixed(2)}°, ${lon.toFixed(2)}°E)`);
    if (state.last && state.mode !== "cont" && state.frame !== "lsr")
      render();                       // topocentric axis follows the site
  }
  for (const box of [els.siteName, els.siteLat, els.siteLon]) {
    box.addEventListener("change", onSiteChange);
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter") box.blur();
      e.stopPropagation();
    });
  }
  initSiteBoxes();

  // ---- the clock: pin the sky to a moment, or run live --------------
  // Everything epoch-dependent re-derives from the pinned time - the sun
  // and moon, the horizon line, alt/az readouts, and the velocity-frame
  // shift (the barycentric term moves ~2 km/s in a week, so a spectrum
  // "for next month" genuinely differs from today's).
  function onEpochChange() {
    const jd = jdFromDate(simDate());
    sky.setSources(continuumSources(jd, decimalYear(jd), site));
    map.sources = sky.sources;
    buildTargetsMenu();               // the sun and moon rows move
    map.draw();
    updateInfo();
    if (state.last) {
      if (state.mode === "cont") renderDrift(); else render();
    }
  }
  els.timeBox.addEventListener("change", () => {
    const v = els.timeBox.value;
    if (!v) {
      setFixedTime(null);
      onEpochChange();
      message("Clock: live.");
      return;
    }
    // A datetime-local value has no zone; the box is labelled UTC and the
    // observatory works in UTC everywhere, so that is the reading.
    const d = new Date(v + (v.length === 16 ? ":00" : "") + "Z");
    if (isNaN(d)) { message("Could not parse the time."); return; }
    setFixedTime(d);
    onEpochChange();
    message(`Clock pinned to ${d.toISOString().slice(0, 16).replace("T", " ")} UTC.`);
  });
  els.nowBtn.addEventListener("click", () => {
    els.timeBox.value = "";
    setFixedTime(null);
    onEpochChange();
    message("Clock: live.");
  });

  els.targetsBtn.addEventListener("click", () => {
    menu.style.display = menu.style.display === "none" ? "block" : "none";
  });

  els.rotateBtn.addEventListener("click", () => {
    map.rotate(90);
    map.draw();
    message(`Map centred on l=${map.l0}°` + (map.l0 === 180 ? " (galactic centre at the edge)" : ""));
  });

  // Reset: every parameter back to its default - the instrument boxes, the
  // site, the clock (live), map mode (H I), frame (LSR), rotation and the
  // horizon overlay. The pointing is kept: it is the thing being looked at,
  // not a setting, and it is re-rendered with the defaults. Replaced "Home",
  // which reset the boxes only, and the Save button (2026-08-26).
  els.homeBtn.addEventListener("click", () => {
    const keep = state.last ? { glon: state.last.glon, glat: state.last.glat } : null;
    // Instrument boxes; the band itself is the fixed instrument's for the
    // mode, set once the mode is back to H I below.
    initBoxes();
    if (keep) { boxes.l.value = keep.glon.toFixed(3); boxes.b.value = keep.glat.toFixed(3); }
    // Site, as the page was opened with (the site boxes edit `site` in
    // place, so the defaults are the snapshot taken at setup).
    els.siteName.value = siteDefault.name;
    els.siteLat.value = `${siteDefault.lat}`;
    els.siteLon.value = `${siteDefault.lon}`;
    onSiteChange();
    // Clock: live.
    els.timeBox.value = "";
    setFixedTime(null);
    // Frame and map mode.
    state.frame = "lsr";
    els.frameBtn.textContent = "Frame: LSR";
    if (state.mode === "cont") els.mapBtn.click();
    applyModeBand("hi");
    // Rotation and the horizon overlay.
    if (map.l0 !== 0) map.rotate(-map.l0);
    if (map.horizon && els.horizonBtn && !map.showHorizon) els.horizonBtn.click();
    onEpochChange();
    message("Reset: all parameters at their defaults, clock live.");
    const p = applyParams();
    if (p && keep) point(p.glon, p.glat);
    map.draw();
  });

  // ---- realise: hand the simulated observation to the telescope -----
  // Only reachable when this page is served by the scheduler, which is what
  // makes it same origin with the API; main.js unhides the button after
  // confirming that. The scheduler owns the astronomy for the drift case: the
  // parking position depends on the real site, and the site boxes here are
  // free text and settable from the URL, so a page parameter must not be what
  // decides where a telescope points.
  async function scheduleEntry() {
    const p = applyParams();
    if (!p) return;
    const drift = state.mode === "cont";
    const scan = parseFloat(boxes.sd.value);
    els.scheduleBtn.disabled = true;
    message(drift
      ? `Schedule: booking a drift scan of l=${p.glon.toFixed(2)}°, ` +
        `b=${p.glat.toFixed(2)}°...`
      : `Schedule: booking a tracked spectrum of l=${p.glon.toFixed(2)}°, ` +
        `b=${p.glat.toFixed(2)}°...`);
    try {
      const resp = await fetch("/api/simulator/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          l: p.glon, b: p.glat, mode: drift ? "cont" : "hi",
          scan_minutes: Number.isFinite(scan) ? scan : 240,
          // The page's clock, pinned or live: a spectrum starts then, a
          // drift scan is centred on the next transit after it.
          epoch_utc: simDate().toISOString(),
          // Read from sky rather than from the boxes: applyParams has just
          // clamped, snapped f_c to the rest frequency where the display
          // precision says it should, and resolved an empty channels box to
          // the band's native count, so these are the numbers the spectrum
          // on screen was actually computed with.
          center_freq_mhz: sky.fc / 1e6,
          bandwidth_mhz: sky.bwHz / 1e6,
          channels: sky.nchan ?? nativeChannels(),
          integration_time_s: sky.tint,
        }),
      });
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok || !d.success) {
        message(`Schedule failed: ${d.error || "HTTP " + resp.status}`);
        return;
      }
      const e = d.entry;
      message(drift
        ? `  scheduled "${e.name}": transit ${e.drift_time} local, ` +
          `${e.start_date} ${e.start_time} for ${e.duration_minutes} min`
        : `  scheduled "${e.name}": ${e.start_date} ${e.start_time} local ` +
          `for ${e.duration_minutes} min`);
      for (const n of d.horizon_notes || []) message(`  local horizon: ${n}`);
      // The schedule list lives in the page that embeds this one; ask it to
      // reload so the new entry appears without a tab round-trip.
      try {
        if (window.parent && window.parent !== window
            && typeof window.parent.loadSchedule === "function")
          window.parent.loadSchedule();
      } catch (_) { /* cross-origin or not embedded: nothing to refresh */ }
    } catch (e) {
      message(`Schedule failed: ${e}`);
    } finally {
      els.scheduleBtn.disabled = false;
    }
  }
  if (els.scheduleBtn) els.scheduleBtn.addEventListener("click", scheduleEntry);

  // pointing readout
  function updateInfo() {
    const lv = parseFloat(boxes.l.value), bv = parseFloat(boxes.b.value);
    if (!Number.isFinite(lv) || !Number.isFinite(bv)) return;
    const eq = galToEq(lv, bv);
    const jd = jdFromDate(simDate());
    const aa = raDecToAltAz(eq.ra, eq.dec, site.lat, site.lon, jd);
    const raH = eq.ra / 15;
    const h = Math.trunc(raH), m = Math.round((raH - h) * 60);
    els.readout.textContent =
      `RA ${h}h${`${m}`.padStart(2, "0")}m  ` +
      `Dec ${eq.dec >= 0 ? "+" : ""}${eq.dec.toFixed(1)}°   |   ` +
      `Alt ${aa.alt.toFixed(1)}°  Az ${aa.az.toFixed(1)}°` +
      (aa.alt < 0 ? "   (below horizon)" : "");
  }
  setInterval(updateInfo, 1000);

  // map click
  map.onPoint = (l, b) => {
    boxes.l.value = l.toFixed(2);
    boxes.b.value = b.toFixed(2);
    const p = applyParams();
    if (p) point(p.glon, p.glat);
  };

  initBoxes();
  // The band read-out and the per-mode boxes, for the mode the page opens in.
  applyModeBand(state.mode);
  updateMapTitle();
  updateInfo();
  return { message, point, applyParams };
}
