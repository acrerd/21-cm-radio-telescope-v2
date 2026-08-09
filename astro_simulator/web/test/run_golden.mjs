// Golden validation of the JS engine against the Python simulator.
// Run from astro_simulator/web:  node test/run_golden.mjs
// Requires node >= 18 (plain fs/zlib, no dependencies).

import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { galToEq, raDecToAltAz, sepDeg, julianDate }
  from "../js/coordinates.js";
import { sunGalactic, moonGalacticTopo, ssbOffset, frameOffset }
  from "../js/ephemeris.js";
import { SkyData } from "../js/skydata.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const golden = JSON.parse(readFileSync(join(HERE, "golden.json")));
const meta = JSON.parse(readFileSync(join(HERE, "..", "data", "meta.json")));

function toArrayBuffer(buf) {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}
const cubeBuf = toArrayBuffer(gunzipSync(
    readFileSync(join(HERE, "..", "data", "hi4pi_web.bin.gz"))));
const contBuf = toArrayBuffer(gunzipSync(
    readFileSync(join(HERE, "..", "data", "continuum_web.bin.gz"))));

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.error(`FAIL ${name}: ${detail}`); }
  else console.log(`ok   ${name}`);
}

const jd0 = golden.t0_jd;
const site = golden.site;

// ---- coordinates ----------------------------------------------------
{
  let worst = 0;
  for (const g of golden.coords.gal2icrs) {
    const eq = galToEq(g.l, g.b);
    worst = Math.max(worst, sepDeg(eq.ra, eq.dec, g.ra, g.dec));
  }
  check("gal->icrs", worst < 0.01, `worst sep ${worst.toFixed(5)} deg`);
}
{
  let worst = 0;
  for (const g of golden.coords.altaz) {
    const aa = raDecToAltAz(g.ra, g.dec, site.lat, site.lon, jd0);
    worst = Math.max(worst,
        Math.hypot(aa.alt - g.alt,
                   ((aa.az - g.az + 540) % 360 - 180)
                     * Math.cos(g.alt * Math.PI / 180)));
  }
  check("altaz", worst < 0.05, `worst err ${worst.toFixed(4)} deg`);
}
{
  const s = sunGalactic(jd0);
  const d = sepDeg(s.l, s.b, golden.coords.sun_gal.l, golden.coords.sun_gal.b);
  check("sun", d < 0.05, `sep ${d.toFixed(4)} deg`);
  const m = moonGalacticTopo(jd0, site);
  const dm = sepDeg(m.l, m.b, golden.coords.moon_gal.l,
                    golden.coords.moon_gal.b);
  check("moon (topocentric)", dm < 0.5, `sep ${dm.toFixed(4)} deg`);
}

// ---- velocity frames ------------------------------------------------
{
  let worstS = 0, worstT = 0;
  for (const g of golden.frames) {
    worstS = Math.max(worstS, Math.abs(ssbOffset(g.l, g.b) - g.ssb));
    worstT = Math.max(worstT,
        Math.abs(frameOffset(g.l, g.b, "topo", jd0, site) - g.topo));
  }
  check("ssb offset", worstS < 2.0, `worst ${worstS.toFixed(2)} m/s`);
  check("topo offset", worstT < 40.0, `worst ${worstT.toFixed(2)} m/s`);
}

// ---- engine ---------------------------------------------------------
const sky = new SkyData(cubeBuf, contBuf, meta);
sky.setSources(golden.sources.map(s => ({ ...s })));
sky.tsys = null;
sky.npol = golden.npol;
sky.eta = golden.eta;

{
  let worst = 0, worstAt = "";
  for (const g of golden.spectra) {
    sky.setBeam(g.fwhm);
    sky.setBand(g.bw_hz, g.fc_hz);
    sky.nchan = g.nchan;
    const s = sky.spectrum(g.l, g.b);
    if (s.t.length !== g.t.length) {
      check(`spectrum l=${g.l} b=${g.b}`, false,
            `length ${s.t.length} != ${g.t.length}`);
      continue;
    }
    for (let i = 0; i < s.t.length; i++) {
      const d = Math.abs(s.t[i] - g.t[i]);
      if (d > worst) { worst = d; worstAt = `l=${g.l} b=${g.b} ch${i}`; }
    }
    const dv = Math.abs(s.v[0] - g.v[0]);
    if (dv > 1e-6) check("v axis", false, `dv ${dv}`);
  }
  check("spectra (13 combos)", worst < 1e-3,
        `worst |dT| ${(worst * 1e3).toFixed(4)} mK at ${worstAt}`);
}
{
  let worst = 0;
  sky.setBeam(4.93);
  for (const g of golden.continuum) {
    const t = sky.continuum(g.l, g.b);
    const rel = Math.abs(t - g.t) / Math.max(Math.abs(g.t), 1e-6);
    worst = Math.max(worst, rel);
  }
  check("continuum (8 pts)", worst < 0.005,
        `worst rel err ${(worst * 100).toFixed(3)} %`);
}
{
  let worst = 0;
  for (const g of golden.drift) {
    sky.setBeam(g.fwhm);
    sky.setBand(2.0e6, 1420405751.768);
    sky.nchan = null;
    const d = sky.driftScan(g.l, g.b, g.dur_min);
    for (let i = 0; i < d.tbar.length; i++)
      worst = Math.max(worst, Math.abs(d.tbar[i] - g.tbar[i]));
  }
  check("drift scans", worst < 2e-3,
        `worst |dT| ${(worst * 1e3).toFixed(4)} mK`);
}
{
  // noise statistics: seeded RNG, sigma matches the radiometer formula
  sky.setBeam(4.93);
  sky.setBand(2.0e6, 1420405751.768);
  sky.nchan = null;
  sky.tsys = 100.0;
  sky.tint = 60.0;
  const s = sky.spectrum(150.0, 53.0);
  const expected = (100.0 + s.tcont)
      / Math.sqrt(1 * 1288.2149691241211 / 299792458.0
                  * 1420405751.768 * 60.0);
  const rel = Math.abs(s.sigma[0] - expected) / expected;
  check("noise sigma", rel < 0.02, `rel err ${(rel * 100).toFixed(2)} %`);
  sky.tsys = null;
}

console.log(failures ? `\n${failures} FAILURE(S)` : "\nall green");
process.exit(failures ? 1 : 0);
