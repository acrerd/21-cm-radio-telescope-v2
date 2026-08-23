// UI wiring: parameter row (with the desktop's clamp/write-back and
// f_c display-precision rules), targets menu, frame cycling, map mode,
// drift scans and save.  Port of the desktop main() closures.

import { jdFromDate, decimalYear, galToEq, raDecToAltAz, sepDeg }
  from "./coordinates.js";
import { frameOffset, continuumSources } from "./ephemeris.js";
import { C_LIGHT, F_HI } from "./skydata.js";

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
    l: els.l, b: els.b, fw: els.fw, bw: els.bw, fc: els.fc,
    nc: els.nc, ts: els.ts, ti: els.ti, sd: els.sd,
  };
  let fcShown = (F_HI / 1e6).toFixed(2);

  function nativeChannels() {
    return sky.k1 > sky.k0 + 1 ? sky.k1 - sky.k0
         : Math.max(2, Math.round(sky.bwHz / 6.1e3));
  }

  function initBoxes() {
    boxes.l.value = "132.0";
    boxes.b.value = "-1.0";
    boxes.fw.value = sky.fwhm.toFixed(2);
    boxes.bw.value = `${sky.bwHz / 1e6}`;
    boxes.fc.value = fcShown;
    boxes.nc.value = `${nativeChannels()}`;
    boxes.ts.value = `${sky.tsys}`;
    boxes.ti.value = `${sky.tint}`;
    boxes.sd.value = "240";
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
    let tint = num(boxes.ti), bwHz = num(boxes.bw) * 1e6;
    const fcText = boxes.fc.value.trim();
    let fcHz = parseFloat(fcText) * 1e6;
    const tsText = boxes.ts.value.trim();
    // empty box = no noise model; 0 is a valid (ideal) receiver
    let tsys = tsText ? parseFloat(tsText) : null;
    const ncText = boxes.nc.value.trim();
    let nchan = ncText ? Math.trunc(parseFloat(ncText)) : null;
    if (![glon, glat, fwhm, tint, bwHz, fcHz].every(Number.isFinite)
        || (tsText && !Number.isFinite(tsys))
        || (ncText && !Number.isFinite(nchan))) {
      message("Could not parse the parameter boxes.");
      return null;
    }
    glon = ((glon % 360) + 360) % 360;
    glat = Math.min(90, Math.max(-90, glat));
    fwhm = Math.abs(fwhm);
    tint = Math.min(1e7, Math.max(1e-3, Math.abs(tint)));
    bwHz = Math.min(8e6, Math.max(2e4, Math.abs(bwHz)));
    if (tsys !== null) tsys = Math.min(1e6, Math.max(0.0, tsys));
    if (nchan !== null) nchan = Math.min(65536, Math.max(2, nchan));
    // f_c display rule: an unchanged display keeps the exact current
    // value; a typed value that rounds to the rest frequency at its
    // own precision means the exact rest frequency
    if (fcText === fcShown) {
      fcHz = sky.fc;
    } else {
      const dec = fcText.includes(".") ? fcText.split(".")[1].length : 0;
      if (Math.abs(fcHz - F_HI) < 0.5 * Math.pow(10, 6 - Math.min(dec, 6)))
        fcHz = F_HI;
    }
    if (fwhm > 0 && fwhm < sky.minFwhm)
      message(`Beam ${fwhm.toFixed(2)}° is finer than the compact ` +
              `dataset supports; using ${sky.minFwhm.toFixed(2)}°.`);
    fwhm = fwhm > 0 ? Math.min(90.0, Math.max(sky.minFwhm, fwhm))
                    : sky.fwhm;
    writeBack(boxes.b, `${glat}`, "b (°)");
    writeBack(boxes.fw, `${fwhm}`, "beam (°)");
    writeBack(boxes.ti, `${tint}`, "τ (s)");
    writeBack(boxes.bw, `${bwHz / 1e6}`, "BW (MHz)");
    if (ncText) writeBack(boxes.nc, `${nchan}`, "channels");
    if (Math.abs(fwhm - sky.fwhm) > 1e-6) sky.setBeam(fwhm);
    map.setDisplayBeam(sky.fwhm);
    updateMapTitle();
    sky.tsys = tsys;
    sky.tint = tint;
    sky.nchan = nchan;
    if (Math.abs(bwHz - sky.bwHz) > 1 || Math.abs(fcHz - sky.fc) > 1) {
      if (!sky.setBand(bwHz, fcHz))
        message("Band has no H I coverage in the compact dataset: " +
                "the spectrum is continuum + noise only.");
    }
    fcShown = (sky.fc / 1e6).toFixed(2);
    if (boxes.fc.value.trim() !== fcShown) boxes.fc.value = fcShown;
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
    const jd = jdFromDate(new Date());
    const shift = frameOffset(glon, glat, state.frame, jd, site);
    const names = {
      lsr: "LSR radial velocity",
      ssb: "SSB (barycentric) radial velocity",
      topo: `topocentric radial velocity (${site.name}, now)`,
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
        const sig = (sky.tsys + tb) / Math.sqrt(sky.npol * bwUse * tau);
        tmins.push(m);
        tvals.push(tb + sky.rng.normal() * sig);
        sigs.push(sig);
      }
      smp = { mins: tmins, t: tvals };
      const sMed = sigs.slice().sort((a, b) => a - b)[sigs.length >> 1];
      noiseTxt = `,  τ/sample ${tau} s, σ≈${(sMed * 1e3).toFixed(0)} mK`;
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
  for (const box of [boxes.l, boxes.b, boxes.fw, boxes.bw, boxes.fc,
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

  els.mapBtn.addEventListener("click", () => {
    state.mode = state.mode === "hi" ? "cont" : "hi";
    const cont = state.mode === "cont";
    els.mapBtn.textContent = cont ? "Map: 1420" : "Map: H I";
    els.frameBtn.disabled = cont;
    boxes.sd.disabled = !cont;
    map.setMode(cont ? "cont" : "hi");
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
        const reqBw = i < TARGETS.length ? TARGETS[i][3] : 2.0;
        if (sky.bwHz < reqBw * 1e6 - 1) boxes.bw.value = `${reqBw}`;
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
    const jd = jdFromDate(new Date());
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
  els.targetsBtn.addEventListener("click", () => {
    menu.style.display = menu.style.display === "none" ? "block" : "none";
  });

  // home / save
  els.homeBtn.addEventListener("click", () => {
    initBoxes();
    state.frame = "lsr";
    els.frameBtn.textContent = "Frame: LSR";
    fcShown = (F_HI / 1e6).toFixed(2);
    message("Parameters reset to startup values.");
    const p = applyParams();
    if (p && state.last) point(p.glon, p.glat);
  });

  function save() {
    if (!state.last) return;
    const { glon, glat, v, t } = state.last;
    const base = `spectrum_l${glon.toFixed(2).padStart(7, "0")}` +
                 `_b${(glat >= 0 ? "+" : "") + glat.toFixed(2)}`;
    const jd = jdFromDate(new Date());
    const dv = frameOffset(glon, glat, state.frame, jd, site);
    let txt = `# v_${state.frame}_km/s   T_A_K\n`;
    for (let i = 0; i < v.length; i++)
      txt += `${((v[i] + dv) / 1e3).toExponential(6)}   ` +
             `${t[i].toExponential(6)}\n`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([txt], { type: "text/plain" }));
    a.download = base + ".txt";
    a.click();
    plot.canvas.toBlob((blob) => {
      const b = document.createElement("a");
      b.href = URL.createObjectURL(blob);
      b.download = base + ".png";
      b.click();
    });
    message(`Saved ${base}.png and ${base}.txt (${state.frame} frame)`);
  }
  els.saveBtn.addEventListener("click", save);
  document.addEventListener("keydown", (e) => {
    if (e.key === "s" && state.last) save();
  });

  // ---- realise: hand the simulated observation to the telescope -----
  // Only reachable when this page is served by the scheduler, which is what
  // makes it same origin with the API; main.js unhides the button after
  // confirming that. The scheduler owns the astronomy for the drift case: the
  // parking position depends on the real site, and the site boxes here are
  // free text and settable from the URL, so a page parameter must not be what
  // decides where a telescope points.
  async function realise() {
    const p = applyParams();
    if (!p) return;
    const drift = state.mode === "cont";
    const scan = parseFloat(boxes.sd.value);
    els.realiseBtn.disabled = true;
    message(drift
      ? `Realise: parking for a drift scan of l=${p.glon.toFixed(2)}°, ` +
        `b=${p.glat.toFixed(2)}°...`
      : `Realise: asking the SRT to track l=${p.glon.toFixed(2)}°, ` +
        `b=${p.glat.toFixed(2)}°...`);
    try {
      const resp = await fetch("/api/simulator/realise", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          l: p.glon, b: p.glat, mode: drift ? "cont" : "hi",
          scan_minutes: Number.isFinite(scan) ? scan : 240,
          // The receiver settings the simulation is using, for the Observe
          // tab. Read from sky rather than from the boxes: applyParams has
          // just clamped, snapped f_c to the rest frequency where the display
          // precision says it should, and resolved an empty channels box to
          // the band's native count, so these are the numbers the spectrum on
          // screen was actually computed with.
          center_freq_mhz: sky.fc / 1e6,
          bandwidth_mhz: sky.bwHz / 1e6,
          channels: sky.nchan ?? nativeChannels(),
          integration_time_s: sky.tint,
        }),
      });
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok || !d.success) {
        message(`Realise failed: ${d.error || "HTTP " + resp.status}`);
      } else if (d.action === "drift") {
        message(`  parked at alt ${d.alt.toFixed(1)}°, az ${d.az.toFixed(1)}°; ` +
                `transit in ${d.transit_minutes.toFixed(0)} min`);
      } else {
        message(`  tracking l=${d.l.toFixed(2)}°, b=${d.b.toFixed(2)}°`);
      }
      // Reported from the server's own flag, not from whether the pointing
      // succeeded: a target below the horizon right now is refused, and its
      // settings are still exactly what the Observe tab wants in order to book
      // it for a time when it is up.
      if (d.params_copied) {
        message("  receiver settings copied to the scheduler's Observe tab");
      }
    } catch (e) {
      message(`Realise failed: ${e}`);
    } finally {
      els.realiseBtn.disabled = false;
    }
  }
  // Guarded because a deployment is free to drop the button from its own copy
  // of index.html; that should lose Realise, not the whole UI.
  if (els.realiseBtn) els.realiseBtn.addEventListener("click", realise);

  // pointing readout
  function updateInfo() {
    const lv = parseFloat(boxes.l.value), bv = parseFloat(boxes.b.value);
    if (!Number.isFinite(lv) || !Number.isFinite(bv)) return;
    const eq = galToEq(lv, bv);
    const jd = jdFromDate(new Date());
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
  updateMapTitle();
  updateInfo();
  return { message, point, applyParams };
}
