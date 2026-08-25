// Bootstrap: fetch + gunzip the data bundles, build the engine, wire
// the UI.  URL parameters replace the desktop CLI flags:
//   ?site=Name&lat=..&lon=..&height=..

import { SkyData } from "./skydata.js";
import { SkyMap } from "./map.js";
import { Plot } from "./plot.js";
import { setupUI } from "./ui.js";
import { jdFromDate, decimalYear } from "./coordinates.js";
import { continuumSources } from "./ephemeris.js";
import { simDate } from "./clock.js";

const status = document.getElementById("load-status");

async function fetchGunzip(url, onProgress) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  const total = +resp.headers.get("Content-Length") || 0;
  let seen = 0;
  const counted = new TransformStream({
    transform(chunk, ctl) {
      seen += chunk.length;
      onProgress?.(seen, total);
      ctl.enqueue(chunk);
    },
  });
  const stream = resp.body.pipeThrough(counted)
      .pipeThrough(new DecompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  return buf;
}

async function boot() {
  try {
    const params = new URLSearchParams(location.search);
    status.textContent = "loading sky data...";

    const [meta, contBuf, cubeBuf] = await Promise.all([
      fetch("data/meta.json").then((r) => r.json()),
      fetchGunzip("data/continuum_web.bin.gz"),
      fetchGunzip("data/hi4pi_web.bin.gz", (seen, total) => {
        status.textContent = total
          ? `loading H I cube... ${(seen / 1e6).toFixed(0)} / ` +
            `${(total / 1e6).toFixed(0)} MB`
          : `loading H I cube... ${(seen / 1e6).toFixed(0)} MB`;
      }),
    ]);

    const site = {
      name: params.get("site") || meta.site.name,
      lat: parseFloat(params.get("lat") ?? meta.site.lat),
      lon: parseFloat(params.get("lon") ?? meta.site.lon),
      height: parseFloat(params.get("height") ?? meta.site.height),
    };
    status.textContent = "decoding...";
    await new Promise((r) => setTimeout(r));      // let the text paint
    const sky = new SkyData(cubeBuf, contBuf, meta);
    const jd = jdFromDate(simDate());
    sky.setSources(continuumSources(jd, decimalYear(jd), site));

    const map = new SkyMap(document.getElementById("map"), sky);
    map.site = site;
    map.sources = sky.sources;
    const plot = new Plot(document.getElementById("spec"));

    const els = {
      l: document.getElementById("p-l"),
      b: document.getElementById("p-b"),
      fw: document.getElementById("p-fw"),
      bw: document.getElementById("p-bw"),
      fc: document.getElementById("p-fc"),
      nc: document.getElementById("p-nc"),
      ts: document.getElementById("p-ts"),
      ti: document.getElementById("p-ti"),
      sd: document.getElementById("p-sd"),
      frameBtn: document.getElementById("btn-frame"),
      mapBtn: document.getElementById("btn-map"),
      targetsBtn: document.getElementById("btn-targets"),
      homeBtn: document.getElementById("btn-home"),
      saveBtn: document.getElementById("btn-save"),
      realiseBtn: document.getElementById("btn-realise"),
      targetsMenu: document.getElementById("targets-menu"),
      readout: document.getElementById("readout"),
      console: document.getElementById("console"),
      mapTitle: document.getElementById("map-title"),
      siteName: document.getElementById("p-site"),
      siteLat: document.getElementById("p-slat"),
      siteLon: document.getElementById("p-slon"),
      timeBox: document.getElementById("p-time"),
      nowBtn: document.getElementById("btn-now"),
    };
    els.sd.disabled = true;                       // until continuum mode

    status.textContent = "building map...";
    await new Promise((r) => setTimeout(r));
    map.resize();
    map.draw();
    plot.message("click the map");

    const ui = setupUI({ sky, map, plot, els, site });
    setInterval(() => map.draw(), 60000);         // horizon drifts

    // deep link: ?l=..&b=..[&beam=..][&mode=cont] points at startup
    if (params.has("beam")) els.fw.value = params.get("beam");
    if (params.has("l") && params.has("b")) {
      els.l.value = params.get("l");
      els.b.value = params.get("b");
      if (params.get("mode") === "cont") els.mapBtn.click();
      const p = ui.applyParams();
      if (p) ui.point(p.glon, p.glat);
    }

    // Realise exists exactly when it can work. Served from the scheduler, this
    // page is same origin with its API and the button commands the telescope
    // through it; opened any other way - a static host, file:// - there is no
    // API to reach and the button stays hidden rather than failing on click.
    // Asking is also the check that matters: it is the scheduler's presence at
    // this origin, not the URL, that decides.
    fetch("/api/telescope", { method: "GET" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && "configured" in d && els.realiseBtn) els.realiseBtn.hidden = false;
      })
      .catch(() => {});

    // The measured horizon, for the same reason and on the same terms: it is
    // the scheduler that holds it, so the button exists exactly where the data
    // does. What arrives is whichever profile is *in force* - the scheduler
    // decides that, on its Horizon tab, and this page simply draws the choice.
    const horizonBtn = document.getElementById("btn-horizon");
    fetch("/api/horizon/profile")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d || !d.success || !d.floors || !d.floors.length || !horizonBtn) return;
        map.horizon = {
          floors: d.floors,
          date: d.date || "measured",
          demo: d.sdr_type === "demo",
        };
        map.showHorizon = true;
        horizonBtn.hidden = false;
        horizonBtn.onclick = () => {
          map.showHorizon = !map.showHorizon;
          horizonBtn.textContent = map.showHorizon ? "Horizon: measured"
                                                   : "Horizon: none";
          map.draw();
        };
        if (map.horizon.demo) {
          // A demo profile describes a synthetic horizon. Saying so here is
          // the whole defence against it being read as the observatory's.
          horizonBtn.title = "SIMULATED horizon - not the observatory";
          map.horizon.date += " SIMULATED";
        }
        map.draw();
      })
      .catch(() => {});

    document.getElementById("loading").style.display = "none";
    document.getElementById("app").style.visibility = "visible";
  } catch (err) {
    status.textContent = `failed to start: ${err.message}`;
    console.error(err);
  }
}

boot();
