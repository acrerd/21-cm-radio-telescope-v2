// Lower-panel plot: stepped spectrum with a velocity axis on top, or
// a drift scan (a stepped simulated measurement).  Canvas port of the desktop
// panel: wheel zoom about the cursor, double-click to reset.

import { C_LIGHT, F_HI } from "./skydata.js";

const INK = "#333639", ACCENT = "#3b7bbf", GRID = "#eceeef";
const MARGIN = { l: 68, r: 18, t: 64, b: 48 };

function niceTicks(lo, hi, n = 6) {
  const span = hi - lo;
  if (!(span > 0)) return [];
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  let step = mag;
  for (const m of [1, 2, 2.5, 5, 10])
    if (step0 <= m * mag) { step = m * mag; break; }
  const out = [];
  for (let x = Math.ceil(lo / step) * step; x <= hi + 1e-12 * span;
       x += step)
    out.push(Math.abs(x) < 1e-12 * span ? 0 : x);
  return out;
}

export class Plot {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.view = null;                    // {x0,x1,y0,y1} data coords
    this.data = null;
    canvas.addEventListener("wheel", (e) => this._wheel(e),
                            { passive: false });
    canvas.addEventListener("dblclick", () => {
      this.view = null;
      this.render();
    });
  }

  rect() {
    return { x: MARGIN.l, y: MARGIN.t,
             w: this.canvas.width - MARGIN.l - MARGIN.r,
             h: this.canvas.height - MARGIN.t - MARGIN.b };
  }

  toPx(x, y) {
    const r = this.rect(), v = this.view;
    return { px: r.x + (x - v.x0) / (v.x1 - v.x0) * r.w,
             py: r.y + (v.y1 - y) / (v.y1 - v.y0) * r.h };
  }

  _wheel(e) {
    if (!this.view) return;
    e.preventDefault();
    const r = this.rect(), v = this.view;
    const cr = this.canvas.getBoundingClientRect();
    const px = (e.clientX - cr.left) * this.canvas.width / cr.width;
    const py = (e.clientY - cr.top) * this.canvas.height / cr.height;
    const fx = (px - r.x) / r.w, fy = (r.y + r.h - py) / r.h;
    const x = v.x0 + fx * (v.x1 - v.x0);
    const y = v.y0 + fy * (v.y1 - v.y0);
    const f = e.deltaY < 0 ? 1 / 1.3 : 1.3;
    this.view = { x0: x - (x - v.x0) * f, x1: x + (v.x1 - x) * f,
                  y0: y - (y - v.y0) * f, y1: y + (v.y1 - y) * f };
    this.render();
  }

  message(text) {
    const ctx = this.ctx;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.fillStyle = "#9a9da0";
    ctx.font = "18px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(text, this.canvas.width / 2, this.canvas.height / 2);
    ctx.textAlign = "left";
    this.data = null;
  }

  _frame(xlabel, ylabel, title) {
    const ctx = this.ctx, r = this.rect(), v = this.view;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.save();
    ctx.font = "14px sans-serif";
    // grid + x ticks
    for (const x of niceTicks(v.x0, v.x1, 7)) {
      const { px } = this.toPx(x, 0);
      ctx.strokeStyle = GRID; ctx.lineWidth = 0.7;
      ctx.beginPath(); ctx.moveTo(px, r.y); ctx.lineTo(px, r.y + r.h);
      ctx.stroke();
      ctx.fillStyle = INK;
      ctx.textAlign = "center";
      ctx.fillText(this._fmtX(x), px, r.y + r.h + 17);
    }
    for (const y of niceTicks(v.y0, v.y1, 6)) {
      const { py } = this.toPx(v.x0, y);
      ctx.strokeStyle = GRID; ctx.lineWidth = 0.7;
      ctx.beginPath(); ctx.moveTo(r.x, py); ctx.lineTo(r.x + r.w, py);
      ctx.stroke();
      ctx.fillStyle = INK;
      ctx.textAlign = "right";
      ctx.fillText(this._fmtY(y), r.x - 6, py + 4);
    }
    ctx.strokeStyle = "#c7cacd"; ctx.lineWidth = 1;
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.fillStyle = INK;
    ctx.textAlign = "center";
    ctx.font = "15px sans-serif";
    ctx.fillText(xlabel, r.x + r.w / 2, this.canvas.height - 8);
    ctx.save();
    ctx.translate(14, r.y + r.h / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText(ylabel, 0, 0);
    ctx.restore();
    ctx.font = "15px sans-serif";
    ctx.fillText(title, r.x + r.w / 2, 17);
    ctx.restore();
  }

  _clip() {
    const r = this.rect();
    this.ctx.save();
    this.ctx.beginPath();
    this.ctx.rect(r.x, r.y, r.w, r.h);
    this.ctx.clip();
  }

  _fmtX(x) { return this.data.mode === "spec" ? x.toFixed(2) : `${x}`; }
  _fmtY(y) {
    const a = Math.abs(y);
    return a >= 100 ? y.toFixed(0) : a >= 1 ? y.toFixed(1)
         : y.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  }

  // spectrum: fMHz ascending order not required (drawn in given order)
  showSpectrum(fMHz, t, title, velLabel) {
    let lo = Infinity, hi = -Infinity, tlo = Infinity, thi = -Infinity;
    for (let i = 0; i < fMHz.length; i++) {
      lo = Math.min(lo, fMHz[i]); hi = Math.max(hi, fMHz[i]);
      if (Number.isFinite(t[i])) {
        tlo = Math.min(tlo, t[i]); thi = Math.max(thi, t[i]);
      }
    }
    const pad = (thi - tlo || 1) * 0.06;
    this.data = { mode: "spec", fMHz, t, title, velLabel };
    this.view = { x0: lo, x1: hi, y0: Math.min(tlo - pad, -pad),
                  y1: thi + pad };
    this.render();
  }

  showDrift(mins, tbar, smp, sig, halfMin, title) {
    let ylo = Infinity, yhi = -Infinity;
    for (const x of tbar) {
      ylo = Math.min(ylo, x); yhi = Math.max(yhi, x);
    }
    if (smp)
      for (const x of smp.t) {
        ylo = Math.min(ylo, x); yhi = Math.max(yhi, x);
      }
    const pad = (yhi - ylo || 1) * 0.06;
    this.data = { mode: "drift", mins, tbar, smp, halfMin, title };
    this.view = { x0: mins[0], x1: mins[mins.length - 1],
                  y0: ylo - pad, y1: yhi + pad };
    this.render();
  }

  render() {
    if (!this.data) return;
    const d = this.data;
    if (d.mode === "spec") this._renderSpec();
    else this._renderDrift();
  }

  _renderSpec() {
    const d = this.data;
    this._frame("Frequency  (MHz, " + d.velLabel.frame + ")",
                "T_A  (K)", d.title);
    const ctx = this.ctx;
    this._clip();
    // zero line
    const z = this.toPx(this.view.x0, 0);
    ctx.strokeStyle = "#c7cacd"; ctx.lineWidth = 0.8;
    ctx.beginPath();
    ctx.moveTo(this.rect().x, z.py);
    ctx.lineTo(this.rect().x + this.rect().w, z.py);
    ctx.stroke();
    // steps-mid polyline
    ctx.strokeStyle = ACCENT; ctx.lineWidth = 1.3;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < d.fMHz.length; i++) {
      if (!Number.isFinite(d.t[i])) { started = false; continue; }
      const p = this.toPx(d.fMHz[i], d.t[i]);
      const xPrev = i > 0 ? (d.fMHz[i - 1] + d.fMHz[i]) / 2 : d.fMHz[i];
      const xNext = i < d.fMHz.length - 1
                  ? (d.fMHz[i] + d.fMHz[i + 1]) / 2 : d.fMHz[i];
      const p0 = this.toPx(xPrev, d.t[i]), p1 = this.toPx(xNext, d.t[i]);
      if (!started) { ctx.moveTo(p0.px, p0.py); started = true; }
      else ctx.lineTo(p0.px, p0.py);
      ctx.lineTo(p1.px, p1.py);
      void p;                     // steps-mid: p0/p1 straddle the sample
    }
    ctx.stroke();
    ctx.restore();
    this._topVelocityAxis();
  }

  _topVelocityAxis() {
    // secondary axis: v = c (F - f)/F, labels on the plot's top edge
    const ctx = this.ctx, r = this.rect(), v = this.view;
    const vel = (fMHz) => C_LIGHT * (F_HI - fMHz * 1e6) / F_HI / 1e3;
    const dv = this.data.velLabel.shift / 1e3;      // frame shift km/s
    const v0 = vel(v.x1) + dv, v1 = vel(v.x0) + dv; // f desc <-> v asc
    ctx.save();
    ctx.font = "13px sans-serif";
    ctx.fillStyle = INK;
    ctx.textAlign = "center";
    for (const kv of niceTicks(Math.min(v0, v1), Math.max(v0, v1), 7)) {
      const f = F_HI * (1 - (kv - dv) * 1e3 / C_LIGHT) / 1e6;
      if (f < v.x0 || f > v.x1) continue;
      const { px } = this.toPx(f, 0);
      ctx.beginPath();
      ctx.strokeStyle = "#c7cacd";
      ctx.moveTo(px, r.y); ctx.lineTo(px, r.y - 4);
      ctx.stroke();
      ctx.fillText(`${kv}`, px, r.y - 9);
    }
    ctx.fillText(this.data.velLabel.text, r.x + r.w / 2, r.y - 27);
    ctx.restore();
  }

  _renderDrift() {
    const d = this.data;
    this._frame("minutes from beam-centre transit",
                "band-averaged T_A  (K)", d.title);
    const ctx = this.ctx;
    this._clip();
    // The simulated measurement, drawn as the receiver would record it: a
    // staircase, each sample a level held across its own integration - the
    // same convention as the Observe tab's live plot, so the simulation and
    // the real trace can be compared by eye. The underlying smooth mean was
    // once drawn through it, and is deliberately not: it claimed a knowledge
    // of the sky between samples that no measurement has. With the noise
    // model off (empty T_sys box) the samples are simply noiseless.
    const series = d.smp || { mins: d.mins, t: d.tbar };
    ctx.strokeStyle = ACCENT; ctx.lineWidth = 1.5;
    ctx.beginPath();
    const xs = series.mins, ys = series.t;
    for (let i = 0; i < xs.length; i++) {
      const half = i ? (xs[i] - xs[i - 1]) / 2
                     : (xs.length > 1 ? (xs[1] - xs[0]) / 2 : 0.5);
      const halfR = (i < xs.length - 1) ? (xs[i + 1] - xs[i]) / 2 : half;
      const p0 = this.toPx(xs[i] - half, ys[i]);
      const p1 = this.toPx(xs[i] + halfR, ys[i]);
      if (i === 0) ctx.moveTo(p0.px, p0.py); else ctx.lineTo(p0.px, p0.py);
      ctx.lineTo(p1.px, p1.py);
    }
    ctx.stroke();
    ctx.strokeStyle = "#c7cacd"; ctx.lineWidth = 1;
    ctx.setLineDash([6, 4]);
    for (const xx of [-d.halfMin, d.halfMin]) {
      const p = this.toPx(xx, 0);
      ctx.beginPath();
      ctx.moveTo(p.px, this.rect().y);
      ctx.lineTo(p.px, this.rect().y + this.rect().h);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.restore();
  }
}
