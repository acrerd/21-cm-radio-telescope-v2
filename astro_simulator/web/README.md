# SRT dish simulator — browser version

An install-free web port of `../astro_simulator.py`, restricted by
design to the **compact datasets**: the whole sky ships as ~36 MB of
data bundles in `data/`, no downloads or full-cube fallbacks exist.
Design and validation strategy are in [PLAN.md](PLAN.md).

## Running it

The page is static but uses ES modules and `fetch`, so it needs an
HTTP server (not `file://`). From this folder:

```
python -m http.server 8000
```

then open <http://localhost:8000/>. Any static file host works the
same way. First load fetches ~36 MB (cached by the browser after
that) and decodes it to ~80 MB in memory; a modern browser on a
laptop or tablet is fine. Requires native gzip decompression
(`DecompressionStream`): Chrome 80+, Edge 80+, Firefox 113+,
Safari 16.4+.

URL parameters replace the desktop CLI flags:

| parameter | meaning | default |
|---|---|---|
| `?site=&lat=&lon=&height=` | observer site | Glasgow 55.87, −4.29 |
| `?l=&b=` | point the dish at startup (deep link) | — |
| `?mode=cont` | start in continuum/drift mode | H I |

## What it does

Feature parity with the desktop app on compact data: click the
Mollweide map for a beam-weighted H I spectrum (stepped channels,
per-channel radiometer noise including source self-noise, T_sys 0 =
ideal receiver / empty = noise off, channels box pre-filled with the
band's native count); LSR/SSB/topocentric frames; the 18 H I targets
plus the five analytic continuum sources with Cas A's secular fade;
site visibility loops and the live horizon; the 1420 MHz continuum
map with drift scans (τ-per-sample noise); display maps blurred to
the current beam, like the desktop; observer-site entry boxes (name,
lat, lon — default Glasgow) that move the horizon, visibility loops,
Moon and topocentric frame together; wheel-zoom, `s`/Save for PNG +
txt export.

Known divergences from the desktop app, all deliberate:
- compact data only — the minimum beam is the compact floor (~1.54°),
  and there is no `--full` upgrade path;
- Moon position is Meeus + topocentric parallax (~0.3°) and the
  topocentric velocity frame is good to ~40 m/s (~1/30 channel),
  versus astropy on the desktop;
- **Realise works only when the scheduler is serving this page**, which
  in practice means the Simulator tab of the scheduler UI on the
  observatory host. Opened any other way — the published copy below, a
  static file host, `file://` — the button is not shown at all, because
  the page asks `/api/telescope` at startup and only reveals it if a
  scheduler answers. There is nothing to configure and nothing that can
  half-work.

  The reason it is arranged that way is worth keeping in view, because
  the obvious alternative is the wrong one. Three things stood between
  a *hosted* page and the controller, and the address was the least of
  them: the published copy is served over **HTTPS** while the controller
  speaks plain HTTP, which browsers block as mixed content; the
  controller sends **no `Access-Control-Allow-Origin` header** and 404s
  the preflight, so a cross-origin page could not read a reply even if
  it arrived; and the controller lives on a **private link** that only
  the observatory host can reach at all. The middle one is a protection
  rather than a gap: same-origin policy is what stops any page visited
  on the observatory host from driving an unauthenticated telescope API,
  and `Access-Control-Allow-Origin: *` would remove it for every page at
  once. There is a sharp edge behind it too — through an ssh tunnel to
  `http://localhost`, the GET is a simple request, so the browser sends
  it and the dish moves, then blocks the response and reports a network
  error. The button appeared to fail while commanding the telescope.

  Serving the page from the scheduler removes the cross-origin request
  instead of permitting it: no preflight, no CORS header, no mixed
  content, and the controller stays unreachable from anywhere but the
  host. The page never talks to the controller — it POSTs `l`, `b`, the
  map mode and the scan length to `/api/simulator/realise`, and the
  scheduler makes the request. That endpoint exposes the two commands
  Realise means and validates their arguments; it is deliberately not a
  general proxy, which would hand this page the whole controller API.
  The drift-scan geometry is computed there as well, so where the dish
  parks depends on the configured observatory position and not on the
  site boxes in this page, which are free text and settable from the URL.

  `astro_simulator.py --controller ...` still has the desktop button,
  which makes the request server-side with no browser in the way.

## Published copy (GitHub Pages)

The simulator is publicly hosted at
<https://acrerd.github.io/srt-dish-simulator/>, served from the
separate public repo `acrerd/srt-dish-simulator` so this repository
can stay private. To publish an update, push the current contents of
this folder over the public repo's history:

```bash
cd $(mktemp -d)
git clone --depth 1 git@github.com:acrerd/srt-dish-simulator.git pub
rm -rf pub/* && cp -r /path/to/astro_simulator/web/. pub/
touch pub/.nojekyll
cd pub && git add -A && git commit -m "Update simulator" && git push
```

Pages redeploys automatically a minute or so after the push.

## Regenerating the data bundles

After changing the compact datasets in the parent folder:

```
python make_web_data.py     # writes data/*.bin.gz + data/meta.json
```

## Validation

`gen_golden.py` exports golden vectors (spectra, continuum, drift
scans, frames, coordinates) from the desktop simulator at a fixed
epoch; the node harness checks the JS engine against them:

```
python gen_golden.py        # refresh test/golden.json (radioconda)
node test/run_golden.mjs    # node >= 18
```

Current status: all green — spectra match to <1 mK, continuum to
<0.5 %, drift scans to <2 mK, frame offsets to <40 m/s.
