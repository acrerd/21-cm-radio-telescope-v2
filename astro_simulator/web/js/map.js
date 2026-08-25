// Mollweide all-sky map on a canvas: N_HI / continuum images, site
// overlays, beam circle, drift track, click-to-point.  Visual port of
// the desktop map panel (inferno colormap, same log stretches).

import { D2R, R2D, galToEq, altAzToGal, jdFromDate } from "./coordinates.js";

// matplotlib inferno, 256 RGB triplets, hex-packed
const INFERNO_HEX = "00000401000501010601010802010a02020c02020e03021004031204031405041706041907051b08051d09061f0a07220b07240c08260d08290e092b10092d110a30120a32140b34150b37160b39180c3c190c3e1b0c411c0c431e0c451f0c48210c4a230c4c240c4f260c51280b53290b552b0b572d0b592f0a5b310a5c320a5e340a5f3609613809623909633b09643d09653e0966400a67420a68440a68450a69470b6a490b6a4a0c6b4c0c6b4d0d6c4f0d6c510e6c520e6d540f6d550f6d57106e59106e5a116e5c126e5d126e5f136e61136e62146e64156e65156e67166e69166e6a176e6c186e6d186e6f196e71196e721a6e741a6e751b6e771c6d781c6d7a1d6d7c1d6d7d1e6d7f1e6c801f6c82206c84206b85216b87216b88226a8a226a8c23698d23698f24699025689225689326679526679727669827669a28659b29649d29649f2a63a02a63a22b62a32c61a52c60a62d60a82e5fa92e5eab2f5ead305dae305cb0315bb1325ab3325ab43359b63458b73557b93556ba3655bc3754bd3853bf3952c03a51c13a50c33b4fc43c4ec63d4dc73e4cc83f4bca404acb4149cc4248ce4347cf4446d04545d24644d34743d44842d54a41d74b3fd84c3ed94d3dda4e3cdb503bdd513ade5238df5337e05536e15635e25734e35933e45a31e55c30e65d2fe75e2ee8602de9612bea632aeb6429eb6628ec6726ed6925ee6a24ef6c23ef6e21f06f20f1711ff1731df2741cf3761bf37819f47918f57b17f57d15f67e14f68013f78212f78410f8850ff8870ef8890cf98b0bf98c0af98e09fa9008fa9207fa9407fb9606fb9706fb9906fb9b06fb9d07fc9f07fca108fca309fca50afca60cfca80dfcaa0ffcac11fcae12fcb014fcb216fcb418fbb61afbb81dfbba1ffbbc21fbbe23fac026fac228fac42afac62df9c72ff9c932f9cb35f8cd37f8cf3af7d13df7d340f6d543f6d746f5d949f5db4cf4dd4ff4df53f4e156f3e35af3e55df2e661f2e865f2ea69f1ec6df1ed71f1ef75f1f179f2f27df2f482f3f586f3f68af4f88ef5f992f6fa96f8fb9af9fc9dfafda1fcffa4";
const INFERNO = [];
for (let i = 0; i < 256; i++)
  INFERNO.push([parseInt(INFERNO_HEX.slice(i * 6, i * 6 + 2), 16),
                parseInt(INFERNO_HEX.slice(i * 6 + 2, i * 6 + 4), 16),
                parseInt(INFERNO_HEX.slice(i * 6 + 4, i * 6 + 6), 16)]);

const SQRT2 = Math.SQRT2;

// The lowest clean altitude at an azimuth, from a measured horizon profile.
//
// Deliberately the same rule as horizon_store.horizon_floor on the Python
// side: take the higher of the two bracketing samples rather than
// interpolating between them. An obstruction narrower than the sampling is
// likelier to be missed than double-counted, so between two measured azimuths
// the safe assumption is the worse of them. That is also why the drawn line
// comes out as a castellation - the rule really is piecewise, and a smooth
// curve would claim a precision the scan does not have, on the optimistic
// side. `floors` is the [az, alt] list served by /api/horizon/profile.
export function horizonFloor(floors, azDeg) {
  if (!floors || !floors.length) return 0;
  const az = ((azDeg % 360) + 360) % 360;
  let before = floors.length - 1, after = 0;
  for (let i = 0; i < floors.length; i++) {
    if (floors[i][0] <= az) before = i;
  }
  for (let i = floors.length - 1; i >= 0; i--) {
    if (floors[i][0] >= az) after = i;
  }
  return Math.max(floors[before][1], floors[after][1]);
}

function gaussKernel(sigmaPix, halfOverride) {
  const half = halfOverride || Math.max(1, Math.ceil(4 * sigmaPix));
  const k = new Float64Array(2 * half + 1);
  let sum = 0;
  for (let i = -half; i <= half; i++) {
    const v = Math.exp(-0.5 * (i / sigmaPix) ** 2);
    k[i + half] = v;
    sum += v;
  }
  for (let i = 0; i < k.length; i++) k[i] /= sum;
  return k;
}

// Separable Gaussian smoothing on the CAR grid: longitude kernel widens
// as 1/cos(b) so the blur is ~isotropic on the sky.  Port of the
// desktop smooth_to_beam (our grids have no NaNs, so the mask
// normalisation there reduces to identity and is omitted).
function smoothToBeam(grid, latArr, stepDeg, fwhmDeg, nlat, nlon) {
  const sigmaDeg = fwhmDeg / (2 * Math.sqrt(2 * Math.log(2)));
  const out = new Float64Array(nlat * nlon);
  // along longitude, wrapping, per-row kernel width
  for (let j = 0; j < nlat; j++) {
    const cosb = Math.max(0.05, Math.cos(latArr[j] * D2R));
    const s = Math.min(sigmaDeg / stepDeg / cosb, nlon / 6);
    const k = gaussKernel(s);
    const h = (k.length - 1) / 2;
    const row = j * nlon;
    for (let i = 0; i < nlon; i++) {
      let acc = 0;
      for (let m = -h; m <= h; m++)
        acc += k[m + h] * grid[row + (((i + m) % nlon) + nlon) % nlon];
      out[row + i] = acc;
    }
  }
  // along latitude, edge-extended
  const k = gaussKernel(sigmaDeg / stepDeg);
  const h = (k.length - 1) / 2;
  const res = new Float64Array(nlat * nlon);
  for (let i = 0; i < nlon; i++) {
    for (let j = 0; j < nlat; j++) {
      let acc = 0;
      for (let m = -h; m <= h; m++) {
        const jj = Math.min(nlat - 1, Math.max(0, j + m));
        acc += k[m + h] * out[jj * nlon + i];
      }
      res[j * nlon + i] = acc;
    }
  }
  return res;
}

// forward Mollweide: l,b (deg) -> normalized X in [-1,1], Y in [-1,1]
// with the desktop's flipped-longitude convention (x = -wrapped l)
function project(lDeg, bDeg) {
  const lam = -(((lDeg + 180) % 360 + 360) % 360 - 180) * D2R;
  const bR = bDeg * D2R;
  let th = bR;
  for (let i = 0; i < 8; i++) {
    const f = 2 * th + Math.sin(2 * th) - Math.PI * Math.sin(bR);
    const fp = 2 + 2 * Math.cos(2 * th);
    if (Math.abs(fp) < 1e-9) break;
    th -= f / fp;
  }
  return { x: (lam / Math.PI) * Math.cos(th), y: Math.sin(th) };
}

export class SkyMap {
  constructor(canvas, sky) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.sky = sky;
    this.mode = "hi";                     // 'hi' | 'cont'
    this.site = { name: "Glasgow", lat: 55.87, lon: -4.29, height: 50 };
    this.beam = null;                     // {l, b, fwhm}
    this.track = null;                    // {lArr, bArr}
    this.sources = [];
    this.lut = null;
    this.baseImage = null;
    this.displayFwhm = sky.fwhm;          // beam the display is blurred to
    this._smoothCache = new Map();
    this.onPoint = null;                  // callback(l, b)
    canvas.addEventListener("click", (e) => this._click(e));
    canvas.addEventListener("mousemove", (e) => this._hover(e));
    this.hoverText = "";
  }

  // canvas pixel -> {l, b} or null (outside the ellipse)
  invert(px, py) {
    const W = this.canvas.width, H = this.canvas.height;
    const X = (px - W / 2) / (W / 2);            // [-1, 1]
    const Y = (H / 2 - py) / (H / 2);
    if (X * X + Y * Y > 1.0 && Math.abs(Y) > 1) return null;
    const th = Math.asin(Math.max(-1, Math.min(1, Y)));
    const sinB = (2 * th + Math.sin(2 * th)) / Math.PI;
    if (Math.abs(sinB) > 1) return null;
    const b = Math.asin(sinB);
    const cosT = Math.cos(th);
    if (cosT < 1e-9) return { l: 0, b: b * R2D };
    const lam = Math.PI * X / cosT;
    if (Math.abs(lam) > Math.PI) return null;
    return { l: ((-lam * R2D) % 360 + 360) % 360, b: b * R2D };
  }

  // white label with a dark halo: readable inside the ellipse and
  // on the white page where labels overhang the map edge
  _label(text, x, y) {
    const ctx = this.ctx;
    ctx.save();
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(45,47,50,0.85)";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(text, x, y);
    ctx.restore();
  }

  toCanvas(lDeg, bDeg) {
    const p = project(lDeg, bDeg);
    return { x: this.canvas.width / 2 + p.x * this.canvas.width / 2,
             y: this.canvas.height / 2 - p.y * this.canvas.height / 2 };
  }

  resize() {
    const W = this.canvas.width, H = this.canvas.height;
    const c = this.sky.cmap;
    this.lut = new Int32Array(W * H).fill(-1);
    const lat0 = c.lat[0], dlat = c.lat[1] - c.lat[0];
    for (let py = 0; py < H; py++) {
      for (let px = 0; px < W; px++) {
        const g = this.invert(px, py);
        if (!g) continue;
        let iy = Math.round((g.b - lat0) / dlat);
        iy = Math.max(0, Math.min(c.nlat - 1, iy));
        // continuum-grid lon is ascending 0.25..359.75
        let ix = Math.round((g.l - c.lon[0]) / (c.lon[1] - c.lon[0]));
        ix = ((ix % c.nlon) + c.nlon) % c.nlon;
        this.lut[py * W + px] = iy * c.nlon + ix;
      }
    }
    this.buildImage();
  }

  buildImage() {
    const W = this.canvas.width, H = this.canvas.height;
    const c = this.sky.cmap;
    const img = this.ctx.createImageData(W, H);
    const data = img.data;
    const hi = this.mode === "hi";
    // continuum shows the galactic emission above the uniform zero
    // level, floored at 10 mK — same stretch as the desktop display.
    // Both maps are pre-smoothed to c.fwhm; blur the residual to the
    // current beam so the display shows what the dish resolves.
    const grid = this._displayGrid(hi ? c.nhi : c.t, c);
    const lo = hi ? Math.log10(4e19) : Math.log10(0.05);
    const hiV = hi ? Math.log10(2e22) : Math.log10(60.0);
    const floor = hi ? 0 : 0.01;
    for (let i = 0; i < W * H; i++) {
      const gi = this.lut[i];
      const o = i * 4;
      if (gi < 0) {                              // outside: page bg
        data[o] = 251; data[o + 1] = 252; data[o + 2] = 253;
        data[o + 3] = 255;
        continue;
      }
      const val = Math.max(grid[gi], floor);
      let u = val > 0 ? (Math.log10(val) - lo) / (hiV - lo) : 0;
      u = Math.max(0, Math.min(1, u));
      const rgb = INFERNO[Math.round(u * 255)];
      data[o] = rgb[0]; data[o + 1] = rgb[1]; data[o + 2] = rgb[2];
      data[o + 3] = 255;
    }
    this.baseImage = img;
  }

  _displayGrid(raw, c) {
    const resid = Math.sqrt(Math.max(
        this.displayFwhm ** 2 - c.fwhm ** 2, 0.0));
    if (resid <= 0.3) return raw;
    const key = `${this.mode}:${resid.toFixed(2)}`;
    let sm = this._smoothCache.get(key);
    if (!sm) {
      const step = Math.abs(c.lat[1] - c.lat[0]);
      sm = smoothToBeam(raw, c.lat, step, resid, c.nlat, c.nlon);
      if (this._smoothCache.size > 6) this._smoothCache.clear();
      this._smoothCache.set(key, sm);
    }
    return sm;
  }

  setDisplayBeam(fwhm) {
    if (Math.abs(fwhm - this.displayFwhm) < 0.01) return;
    this.displayFwhm = fwhm;
    this.buildImage();
    this.draw();
  }

  setMode(mode) { this.mode = mode; this.buildImage(); this.draw(); }
  setBeamMarker(l, b, fwhm) { this.beam = { l, b, fwhm }; }
  setTrack(lArr, bArr) { this.track = lArr ? { lArr, bArr } : null; }

  // draw a path in sky coords, splitting at the longitude wrap
  path(lArr, bArr, style) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = style.color;
    ctx.lineWidth = style.lw || 1.4;
    ctx.setLineDash(style.dash || []);
    ctx.beginPath();
    let prev = null;
    for (let i = 0; i < lArr.length; i++) {
      const p = this.toCanvas(lArr[i], bArr[i]);
      if (prev && Math.abs(p.x - prev.x) > this.canvas.width / 4) {
        ctx.moveTo(p.x, p.y);
      } else if (prev) ctx.lineTo(p.x, p.y);
      else ctx.moveTo(p.x, p.y);
      prev = p;
    }
    ctx.stroke();
    ctx.restore();
  }

  _decLoop(altDeg) {
    const dec = this.site.lat >= 0 ? this.site.lat - 90 + altDeg
                                   : this.site.lat + 90 - altDeg;
    const lArr = [], bArr = [];
    for (let i = 0; i <= 720; i++) {
      // constant-dec circle in ICRS -> galactic
      const ra = i * 0.5;
      const g = eqToGalCached(ra, dec);
      lArr.push(g.l); bArr.push(g.b);
    }
    return { lArr, bArr, dec };
  }

  draw(jd) {
    const ctx = this.ctx;
    const W = this.canvas.width, H = this.canvas.height;
    if (!this.baseImage) return;
    ctx.putImageData(this.baseImage, 0, 0);
    jd = jd ?? jdFromDate(new Date());

    // graticule
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 0.6;
    for (let b = -60; b <= 60; b += 30) {
      const lArr = [], bArr = [];
      for (let l = 0; l <= 360; l += 2) { lArr.push(l); bArr.push(b); }
      this.path(lArr, bArr, { color: "rgba(255,255,255,0.22)", lw: 0.6 });
    }
    for (let l = 0; l < 360; l += 60) {
      const lArr = [], bArr = [];
      for (let b = -90; b <= 90; b += 2) { lArr.push(l); bArr.push(b); }
      this.path(lArr, bArr, { color: "rgba(255,255,255,0.22)", lw: 0.6 });
    }
    ctx.restore();
    // meridian labels along the equator
    ctx.save();
    ctx.font = "14px sans-serif";
    for (let l = 60; l < 360; l += 60) {
      const p = this.toCanvas(l, 2);
      this._label(`${l}°`, p.x + 2, p.y - 2);
    }
    ctx.restore();

    // site visibility loops
    const loops = [
      { alt: 0, color: "#7fe07f", label: "never rises" },
      { alt: 20, color: "#ffd166", label: "alt always < 20°" },
    ];
    const legend = [];
    for (const lo of loops) {
      const d = this._decLoop(lo.alt);
      this.path(d.lArr, d.bArr, { color: lo.color, lw: 1.4, dash: [6, 4] });
      const side = this.site.lat >= 0 ? "<" : ">";
      legend.push({ color: lo.color, dash: true,
                    text: `${lo.label} (dec ${side} ${d.dec.toFixed(0)}°)` });
    }

    // live horizon + zenith
    const hL = [], hB = [];
    for (let i = 0; i <= 720; i++) {
      const g = altAzToGal(0, i * 0.5, this.site.lat, this.site.lon, jd);
      hL.push(g.l); hB.push(g.b);
    }
    this.path(hL, hB, { color: "#55585b", lw: 3.2, dash: [8, 4] });
    this.path(hL, hB, { color: "#ffffff", lw: 1.6, dash: [8, 4] });
    const zen = altAzToGal(90, 0, this.site.lat, this.site.lon, jd);
    const zp = this.toCanvas(zen.l, zen.b);
    ctx.save();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    ctx.moveTo(zp.x - 5, zp.y); ctx.lineTo(zp.x + 5, zp.y);
    ctx.moveTo(zp.x, zp.y - 5); ctx.lineTo(zp.x, zp.y + 5);
    ctx.stroke();
    ctx.font = "13px sans-serif";
    this._label("zenith", zp.x + 6, zp.y - 4);
    ctx.restore();
    const hhmm = new Date((jd - 2440587.5) * 86400e3)
        .toISOString().slice(11, 16);
    legend.push({ color: "#ffffff", dash: true,
                  text: `horizon at ${hhmm} UT` });

    // ...and above it, the horizon we actually measured. alt=0 is where the
    // sky would end if the observatory stood on a billiard table; the trees,
    // the roofline and the two dome towers are what really ends it, and the
    // difference reaches 45 deg to the north. Anything between the two lines
    // is up, but not observable.
    if (this.horizon && this.showHorizon) {
      const mL = [], mB = [];
      for (let i = 0; i <= 720; i++) {
        const az = i * 0.5;
        const g = altAzToGal(horizonFloor(this.horizon.floors, az), az,
                             this.site.lat, this.site.lon, jd);
        mL.push(g.l); mB.push(g.b);
      }
      this.path(mL, mB, { color: "#55585b", lw: 3.2 });
      this.path(mL, mB, { color: "#ff9f43", lw: 1.6 });
      legend.push({ color: "#ff9f43", dash: false,
                    text: `measured horizon (${this.horizon.date})` });
    }

    // continuum sources + M31 landmark
    ctx.save();
    ctx.font = "14px sans-serif";
    for (const s of this.sources) {
      const p = this.toCanvas(s.l, s.b);
      ctx.beginPath();
      ctx.fillStyle = s.name === "Sun" ? "#ffe14d"
                    : s.name === "Moon" ? "#d5d8dc" : "#ffffff";
      ctx.strokeStyle = "#333639";
      ctx.arc(p.x, p.y, 4, 0, 2 * Math.PI);
      ctx.fill(); ctx.stroke();
      // flip the label leftward when it would run off the canvas
      const tw = ctx.measureText(s.name).width;
      const tx = p.x + 6 + tw > W - 2 ? p.x - 6 - tw : p.x + 6;
      this._label(s.name, tx, p.y - 4);
    }
    const m31 = this.toCanvas(121.17, -21.57);
    ctx.fillStyle = "#a8e6ff";
    ctx.strokeStyle = "#333639";
    ctx.beginPath();
    ctx.moveTo(m31.x, m31.y - 4); ctx.lineTo(m31.x + 4, m31.y);
    ctx.lineTo(m31.x, m31.y + 4); ctx.lineTo(m31.x - 4, m31.y);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    this._label("M31", m31.x + 6, m31.y - 4);
    ctx.restore();

    // drift-scan track
    if (this.track)
      this.path(this.track.lArr, this.track.bArr,
                { color: "#4dd2ff", lw: 1.2, dash: [5, 4] });

    // beam circle (cos-b widened, wrap-safe via path splitting)
    if (this.beam) {
      const { l, b, fwhm } = this.beam;
      const r = fwhm / 2;
      const lArr = [], bArr = [];
      for (let i = 0; i <= 100; i++) {
        const t = 2 * Math.PI * i / 100;
        const bb = Math.max(-89.9, Math.min(89.9, b + r * Math.cos(t)));
        lArr.push(l + r * Math.sin(t) / Math.cos(bb * D2R));
        bArr.push(bb);
      }
      this.path(lArr, bArr, { color: "#4dd2ff", lw: 1.5 });
    }

    // legend, bottom-right, inside the page margin
    ctx.save();
    ctx.font = "14px sans-serif";
    const lx = W - 240, ly = H - 20 * (legend.length + 1) - 6;
    ctx.fillStyle = "rgba(251,252,253,0.92)";
    ctx.fillRect(lx - 8, ly - 16, 248, 20 * (legend.length + 1) + 14);
    ctx.fillStyle = "#333639";
    ctx.fillText(`from ${this.site.name} (inside loop)`, lx, ly);
    legend.forEach((e, i) => {
      const y = ly + 20 * (i + 1);
      ctx.strokeStyle = e.color === "#ffffff" ? "#55585b" : e.color;
      ctx.lineWidth = 2;
      ctx.setLineDash(e.dash ? [5, 3] : []);
      ctx.beginPath();
      ctx.moveTo(lx, y - 3); ctx.lineTo(lx + 18, y - 3);
      ctx.stroke();
      ctx.fillText(e.text, lx + 24, y);
    });
    ctx.restore();

    // cursor readout
    if (this.hoverText) {
      ctx.save();
      ctx.font = "15px sans-serif";
      ctx.fillStyle = "#333639";
      ctx.fillText(this.hoverText, 8, H - 8);
      ctx.restore();
    }
  }

  _canvasPos(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: (e.clientX - r.left) * this.canvas.width / r.width,
             y: (e.clientY - r.top) * this.canvas.height / r.height };
  }

  _click(e) {
    const p = this._canvasPos(e);
    const g = this.invert(p.x, p.y);
    if (g && this.onPoint) this.onPoint(g.l, g.b);
  }

  _hover(e) {
    const p = this._canvasPos(e);
    const g = this.invert(p.x, p.y);
    this.hoverText = g
      ? `l = ${g.l.toFixed(2)}°,  b = ${g.b.toFixed(2)}°` : "";
    this.draw();
  }
}

// dec-loop points don't move (site-fixed): cache the gal conversions
import { eqToGal } from "./coordinates.js";
const _eqGalCache = new Map();
function eqToGalCached(ra, dec) {
  const key = ra + ":" + dec;
  let v = _eqGalCache.get(key);
  if (!v) { v = eqToGal(ra, dec); _eqGalCache.set(key, v); }
  return v;
}
