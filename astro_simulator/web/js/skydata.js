// Sky data + spectrum engine: a line-faithful port of the desktop
// DishSimulator restricted to the compact datasets (see PLAN.md §4).
// Bundle format is produced by make_web_data.py.

import { D2R, sepDeg, galToEq, eqToGal } from "./coordinates.js";

export const C_LIGHT = 299792458.0;
export const F_HI = 1420405751.768;
const K_B = 1.380649e-23;
// Beam FWHM as a multiple of lambda/D, measured on the 3 m dish: see
// ../../instrument.py, which is the source of meta.defaults.fwhm and carries
// the reasoning. Emphatically not 1.22, the Airy first-null radius of a
// uniformly illuminated aperture, which this used to be. Only reached if a
// meta.json predating that fix is served.
const BEAM_FWHM_COEFF = 1.28;

// ---- bundle parsing -------------------------------------------------
const DTYPES = { int16: Int16Array, uint16: Uint16Array,
                 uint32: Uint32Array, float32: Float32Array,
                 float64: Float64Array };

export function parseBundle(buf, magic) {
  const u8 = new Uint8Array(buf);
  const tag = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (tag !== magic) throw new Error(`bad bundle magic ${tag}`);
  const hlen = new DataView(buf).getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(
      new Uint8Array(buf, 8, hlen)));
  let ofs = 8 + hlen;
  const sections = {};
  for (const s of header.sections) {
    const T = DTYPES[s.dtype];
    if (!T) throw new Error(`unknown dtype ${s.dtype}`);
    // typed arrays need aligned offsets; copy if the header length
    // left us unaligned (cheap relative to the data)
    let arr;
    if (ofs % T.BYTES_PER_ELEMENT === 0) {
      arr = new T(buf, ofs, s.count);
    } else {
      arr = new T(buf.slice(ofs, ofs + s.count * T.BYTES_PER_ELEMENT));
    }
    sections[s.name] = arr;
    ofs += s.count * T.BYTES_PER_ELEMENT;
  }
  return { header, sections };
}

// ---- seedable RNG (mulberry32 + Box-Muller) -------------------------
export function makeRng(seed) {
  let a = seed >>> 0;
  const uni = () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  let spare = null;
  return {
    uniform: uni,
    normal() {
      if (spare !== null) { const v = spare; spare = null; return v; }
      let u1 = 0;
      while (u1 === 0) u1 = uni();
      const u2 = uni();
      const r = Math.sqrt(-2 * Math.log(u1));
      spare = r * Math.sin(2 * Math.PI * u2);
      return r * Math.cos(2 * Math.PI * u2);
    },
  };
}

function medianAbsDiff(a) {
  if (a.length < 2) return 6.1e3;
  const d = [];
  for (let i = 1; i < a.length; i++) d.push(Math.abs(a[i] - a[i - 1]));
  d.sort((x, y) => x - y);
  const m = d.length >> 1;
  return d.length % 2 ? d[m] : 0.5 * (d[m - 1] + d[m]);
}

// np.interp clone: xq ascending not required, xp strictly ascending
function interp(xq, xp, fp, left, right) {
  const out = new Float64Array(xq.length);
  for (let i = 0; i < xq.length; i++) {
    const x = xq[i];
    if (x <= xp[0]) { out[i] = x < xp[0] ? left : fp[0]; continue; }
    const last = xp.length - 1;
    if (x >= xp[last]) { out[i] = x > xp[last] ? right : fp[last]; continue; }
    let lo = 0, hi = last;               // binary search: xp[lo]<=x<xp[hi]
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (xp[mid] <= x) lo = mid; else hi = mid;
    }
    out[i] = fp[lo] + (fp[lo + 1] - fp[lo]) * (x - xp[lo])
                    / (xp[lo + 1] - xp[lo]);
  }
  return out;
}

// ---- the engine -----------------------------------------------------
export class SkyData {
  constructor(cubeBuf, contBuf, meta) {
    const cube = parseBundle(cubeBuf, "HI4W");
    const cont = parseBundle(contBuf, "CONW");
    const h = cube.header;
    [this.nv, this.nlat, this.nlon] = h.shape;
    this.scale = h.scale;
    this.dataFwhm = h.fwhm;
    this.pixOff = cube.sections.pix_off;
    this.runs = cube.sections.runs;
    this.vals = cube.sections.vals;
    this.v_all = cube.sections.v;
    this.lon = cube.sections.lon;
    this.lat = cube.sections.lat;
    // cumulative value offset of each run (values are stored in run order)
    const nruns = this.runs.length / 2;
    this.runVal = new Uint32Array(nruns + 1);
    for (let r = 0; r < nruns; r++)
      this.runVal[r + 1] = this.runVal[r] + this.runs[2 * r + 1];
    this.f_all = new Float64Array(this.nv);
    for (let k = 0; k < this.nv; k++)
      this.f_all[k] = F_HI * (1.0 - this.v_all[k] / C_LIGHT);

    this.cmap = {
      t: cont.sections.t, nhi: cont.sections.nhi,
      lon: cont.sections.lon, lat: cont.sections.lat,
      fwhm: cont.header.fwhm, tZero: cont.header.t_zero,
      nlat: cont.header.shape[0], nlon: cont.header.shape[1],
    };

    const pix = Math.abs(this.lat[1] - this.lat[0]);
    this.minFwhm = Math.round(Math.sqrt(this.dataFwhm ** 2
                   + (2.355 * pix) ** 2) * 100) / 100;
    this.pix = pix;

    this.eta = meta.defaults.eta;
    this.npol = meta.defaults.npol;
    this.tsys = meta.defaults.tsys;
    this.tint = meta.defaults.tint;
    // Common-mode instability of band-integrated power per sample, from
    // instrument.py via meta.json; the fallback is the 2026-08-25 measurement,
    // for a stale meta.json predating the field. Drift scans use it - the
    // band integral sits ~3x above its radiometer floor while per-channel
    // noise stays thermal.
    this.gainSig = meta.defaults.gain_sigma ?? 2.3e-4;
    this.nchan = null;
    this.sources = [];                    // set via setSources()
    this.rng = makeRng((Math.random() * 2 ** 32) >>> 0);
    this.setBeam(meta.defaults.fwhm ??
        (BEAM_FWHM_COEFF * (C_LIGHT / F_HI) / meta.defaults.dish_m) / D2R);
    this.setBand(meta.defaults.bw_mhz * 1e6, F_HI);
  }

  setSources(list) { this.sources = list; }

  setBeam(fwhmDeg) {
    if (fwhmDeg < this.minFwhm) fwhmDeg = this.minFwhm;
    this.fwhm = fwhmDeg;
    const eff = Math.sqrt(Math.max(fwhmDeg ** 2 - this.dataFwhm ** 2,
                                   1e-12));
    this.sigma = eff / (2 * Math.sqrt(2 * Math.log(2)));
    this.rmax = 1.5 * eff;
  }

  setBand(bwHz, fcHz) {
    this.bwHz = bwHz;
    this.fc = fcHz;
    let k0 = -1, k1 = 0;
    for (let k = 0; k < this.nv; k++) {
      if (Math.abs(this.f_all[k] - fcHz) <= bwHz / 2) {
        if (k0 < 0) k0 = k;
        k1 = k + 1;
      }
    }
    if (k0 < 0) { this.k0 = this.k1 = 0; return false; }
    this.k0 = k0; this.k1 = k1;
    return true;
  }

  bandV() { return this.v_all.subarray(this.k0, this.k1); }
  bandF() { return this.f_all.subarray(this.k0, this.k1); }

  // beam-weighted continuum T_A: analytic sources + diffuse map
  continuum(glon, glat) {
    const lam2 = (C_LIGHT / F_HI) ** 2;
    const aE = lam2 * this.eta / (1.133 * (this.fwhm * D2R) ** 2);
    const sigmaB = this.fwhm / (2 * Math.sqrt(2 * Math.log(2)));
    let total = 0.0;
    for (const s of this.sources) {
      const th = sepDeg(s.l, s.b, glon, glat);
      total += s.jy * 1e-26 * aE / (2 * K_B)
             * Math.exp(-0.5 * (th / sigmaB) ** 2);
    }
    total += this.mapContinuum(glon, glat);
    return total;
  }

  mapContinuum(glon, glat) {
    const c = this.cmap;
    const pix = Math.abs(c.lat[1] - c.lat[0]);
    const sig = Math.max(
        Math.sqrt(Math.max(this.fwhm ** 2 - c.fwhm ** 2, 0.0)) / 2.355,
        0.3 * pix);
    const rmax = Math.max(3 * sig, 1.5 * pix);
    const cosb = Math.max(0.05, Math.cos(
        Math.min(89.0, Math.abs(glat) + rmax) * D2R));
    let sw = 0.0, swt = 0.0;
    for (let iy = 0; iy < c.nlat; iy++) {
      if (Math.abs(c.lat[iy] - glat) > rmax) continue;
      const wlat = Math.cos(c.lat[iy] * D2R);
      for (let ix = 0; ix < c.nlon; ix++) {
        let dl = ((c.lon[ix] - glon + 180.0) % 360.0 + 360.0) % 360.0
               - 180.0;
        if (Math.abs(dl) * cosb > rmax) continue;
        const sep = sepDeg(c.lon[ix], c.lat[iy], glon, glat);
        const w = Math.exp(-0.5 * (sep / sig) ** 2) * wlat;
        sw += w;
        swt += w * c.t[iy * c.nlon + ix];
      }
    }
    return sw > 0 ? this.eta * swt / sw : 0.0;
  }

  // the desktop spectrum(): returns {v, t, sigma, tcont} — v in m/s,
  // t in K (with continuum and, if tsys!=null, noise), sigma per
  // channel or null.  Throws if the footprint holds no map pixels.
  spectrum(glon, glat) {
    glon = ((glon % 360) + 360) % 360;
    if (this.k1 <= this.k0) {
      const dfNat = medianAbsDiff(this.f_all);
      const n = this.nchan || Math.max(2, Math.round(this.bwHz / dfNat));
      const fOut = new Float64Array(n);
      for (let i = 0; i < n; i++)
        fOut[i] = this.fc - this.bwHz / 2
                + (this.bwHz / n) * (i + 0.5);
      const vOut = new Float64Array(n);
      for (let i = 0; i < n; i++)
        vOut[i] = C_LIGHT * (F_HI - fOut[i]) / F_HI;
      return this._finish(vOut, new Float64Array(n), this.bwHz / n,
                          glon, glat);
    }

    // footprint rows (lat is uniform ascending => contiguous)
    let y0 = -1, y1 = 0;
    for (let iy = 0; iy < this.nlat; iy++) {
      if (Math.abs(this.lat[iy] - glat) <= this.rmax) {
        if (y0 < 0) y0 = iy;
        y1 = iy + 1;
      }
    }
    const cosb = Math.max(0.05, Math.cos(
        Math.min(89.0, Math.abs(glat) + this.rmax) * D2R));
    const colmask = new Uint8Array(this.nlon);
    let anyCol = false;
    for (let ix = 0; ix < this.nlon; ix++) {
      let dl = ((this.lon[ix] - glon + 180.0) % 360.0 + 360.0) % 360.0
             - 180.0;
      if (Math.abs(dl) * cosb <= this.rmax) { colmask[ix] = 1; anyCol = true; }
    }
    if (y0 < 0 || !anyCol)
      throw new Error(
        `beam footprint (${this.fwhm} deg FWHM) contains no map ` +
        `pixels at l=${glon.toFixed(2)}, b=${glat.toFixed(2)}`);

    const step = Math.max(1, Math.trunc(this.fwhm / (15.0 * this.pix)));

    // contiguous column runs, stride restarting at each run start
    // (mirrors the desktop's r[0]:r[-1]+1:step slicing)
    const cols = [];
    for (let ix = 0; ix < this.nlon; ix++) {
      if (!colmask[ix]) continue;
      let end = ix;
      while (end + 1 < this.nlon && colmask[end + 1]) end++;
      for (let x = ix; x <= end; x += step) cols.push(x);
      ix = end;
    }

    const nk = this.k1 - this.k0;
    const acc = new Float64Array(nk);
    let W = 0.0;
    for (let iy = y0; iy < y1; iy += step) {
      const wlat = Math.cos(this.lat[iy] * D2R);
      for (const ix of cols) {
        const sep = sepDeg(this.lon[ix], this.lat[iy], glon, glat);
        if (sep > this.rmax) continue;
        const w = Math.exp(-0.5 * (sep / this.sigma) ** 2) * wlat;
        W += w;
        const p = iy * this.nlon + ix;
        const r1 = this.pixOff[p + 1];
        for (let r = this.pixOff[p]; r < r1; r++) {
          const rk0 = this.runs[2 * r], rlen = this.runs[2 * r + 1];
          const a = Math.max(rk0, this.k0);
          const b = Math.min(rk0 + rlen, this.k1);
          if (a >= b) continue;
          const vo = this.runVal[r] - rk0;
          for (let k = a; k < b; k++)
            acc[k - this.k0] += w * this.vals[vo + k];
        }
      }
    }
    let tOut = new Float64Array(nk);
    const norm = W > 0 ? this.eta * this.scale / W : NaN;
    for (let k = 0; k < nk; k++) tOut[k] = acc[k] * norm;

    let vOut = Float64Array.from(this.bandV());
    let df = medianAbsDiff(this.bandF());
    if (this.nchan) {
      const n = this.nchan;
      const fOut = new Float64Array(n);
      for (let i = 0; i < n; i++)
        fOut[i] = this.fc - this.bwHz / 2 + (this.bwHz / n) * (i + 0.5);
      df = this.bwHz / n;
      // band f is descending with v ascending: reverse for interp
      const f = this.bandF();
      const fs = new Float64Array(nk), ts = new Float64Array(nk);
      for (let i = 0; i < nk; i++) {
        fs[i] = f[nk - 1 - i];
        ts[i] = tOut[nk - 1 - i];
      }
      tOut = interp(fOut, fs, ts, 0.0, 0.0);
      vOut = new Float64Array(n);
      for (let i = 0; i < n; i++)
        vOut[i] = C_LIGHT * (F_HI - fOut[i]) / F_HI;
    }
    return this._finish(vOut, tOut, df, glon, glat);
  }

  _finish(vOut, tOut, df, glon, glat) {
    const tcont = this.continuum(glon, glat);
    for (let i = 0; i < tOut.length; i++) tOut[i] += tcont;
    let sigma = null;
    if (this.tsys !== null && this.tsys !== undefined) {
      sigma = new Float64Array(tOut.length);
      const den = Math.sqrt(this.npol * df * this.tint);
      for (let i = 0; i < tOut.length; i++) {
        sigma[i] = (this.tsys + Math.max(tOut[i], 0.0)) / den;
        tOut[i] += this.rng.normal() * sigma[i];
      }
    }
    return { v: vOut, t: tOut, sigma, tcont };
  }

  // drift-scan track + band-averaged T_A (noise-free), desktop parity:
  // n=151 points, RA advance 15.041 deg/h at constant declination
  driftScan(glon, glat, durMin, n = 151) {
    const eq = galToEq(glon, glat);
    const cosd = Math.max(0.02, Math.cos(eq.dec * D2R));
    const dtH = new Float64Array(n);
    const trackL = new Float64Array(n), trackB = new Float64Array(n);
    const tbar = new Float64Array(n);
    const keepT = this.tsys;
    this.tsys = null;
    try {
      for (let i = 0; i < n; i++) {
        dtH[i] = -durMin / 120.0 + (durMin / 60.0) * i / (n - 1);
        const g = eqToGal(eq.ra + 15.041 * dtH[i], eq.dec);
        trackL[i] = g.l; trackB[i] = g.b;
        const s = this.spectrum(g.l, g.b);
        let sum = 0, m = 0;
        for (const x of s.t) if (Number.isFinite(x)) { sum += x; m++; }
        tbar[i] = m ? sum / m : NaN;
      }
    } finally {
      this.tsys = keepT;
    }
    return { dtH, tbar, trackL, trackB, ra0: eq.ra, dec: eq.dec, cosd };
  }
}
