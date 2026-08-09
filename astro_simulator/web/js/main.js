// Bootstrap: fetch + gunzip the data bundles, build the engine, wire
// the UI.  URL parameters replace the desktop CLI flags:
//   ?site=Name&lat=..&lon=..&height=..&controller=http://...

import { SkyData } from "./skydata.js";
import { SkyMap } from "./map.js";
import { Plot } from "./plot.js";
import { setupUI } from "./ui.js";
import { jdFromDate, decimalYear } from "./coordinates.js";
import { continuumSources } from "./ephemeris.js";

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
    const controller = params.get("controller") || meta.controller;

    status.textContent = "decoding...";
    await new Promise((r) => setTimeout(r));      // let the text paint
    const sky = new SkyData(cubeBuf, contBuf, meta);
    const jd = jdFromDate(new Date());
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
      realiseBtn: document.getElementById("btn-realise"),
      homeBtn: document.getElementById("btn-home"),
      saveBtn: document.getElementById("btn-save"),
      targetsMenu: document.getElementById("targets-menu"),
      readout: document.getElementById("readout"),
      console: document.getElementById("console"),
      siteName: document.getElementById("p-site"),
      siteLat: document.getElementById("p-slat"),
      siteLon: document.getElementById("p-slon"),
    };
    els.sd.disabled = true;                       // until continuum mode

    status.textContent = "building map...";
    await new Promise((r) => setTimeout(r));
    map.resize();
    map.draw();
    plot.message("click the map");

    const ui = setupUI({ sky, map, plot, els, site, controller });
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

    document.getElementById("loading").style.display = "none";
    document.getElementById("app").style.visibility = "visible";
  } catch (err) {
    status.textContent = `failed to start: ${err.message}`;
    console.error(err);
  }
}

boot();
