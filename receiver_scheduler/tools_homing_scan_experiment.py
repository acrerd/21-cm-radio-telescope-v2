"""Home from the Sun's position, then Sun scan; repeated. 2026-09-08.

The calibration day homes before every raster and its scans fit one model to
0.015 deg rms, so homing from a moderate Sun position is repeatable. Today's
two pointing measurements disagreed by ~a pulse per axis, with homings from
the stow and from home itself between them. This repeats the calibration-day
sequence and records whether the scan result holds still. Detached from any
terminal; appends to data/homing_scan_experiment_20260908.jsonl.
"""
import json, time, datetime, urllib.request, sys
C = 'http://192.168.50.120'
S = 'http://127.0.0.1:5000'
OUT = '/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/data/homing_scan_experiment_20260908.jsonl'
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

def get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

def post(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def record(**kw):
    kw['utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    with open(OUT, 'a') as f:
        f.write(json.dumps(kw) + '\n')
    print(kw, flush=True)

def wait_homing(issued_after):
    """Wait for the homing to finish, collecting every "Homing:" line the Due
    prints on the way (the controller's /serial/log is a ~15 s ring buffer,
    so it is polled at 0.4 s). Returns (status, lines) or (None, lines)."""
    seen = set()
    lines = []
    for _ in range(600):
        try:
            for e in get(C + '/serial/log', timeout=3):
                key = (e.get('time'), e.get('msg'))
                m = str(e.get('msg', ''))
                if key not in seen and 'Homing' in m and 'Alt:' not in m:
                    seen.add(key)
                    lines.append(m)
            d = get(C + '/status')
            h = d.get('last_homing') or {}
            keys = ('az_error_first_deg', 'alt_error_first_deg', 'az_error_second_deg', 'alt_error_second_deg')
            if h.get('utc', 0) >= issued_after and all(h.get(k) is not None for k in keys) and d.get('status') == 'Ready':
                return d, lines
        except Exception as e:
            record(event='status_error', error=str(e))
        time.sleep(0.4)
    return None, lines

def wait_scan():
    for _ in range(360):
        try:
            d = get(S + '/api/sunscan/status')
            if not d.get('running'):
                return d
        except Exception as e:
            record(event='scan_status_error', error=str(e))
        time.sleep(2)
    return None

record(event='start', repeats=REPEATS, position=get(C + '/status').get('raw'))
for i in range(1, REPEATS + 1):
    before = get(C + '/status')
    issued = int(time.time()) - 2
    get(C + '/home')
    record(event='home_issued', cycle=i, from_alt=before.get('alt'), from_az=before.get('az'))
    d, lines = wait_homing(issued)
    if d is None:
        record(event='home_timeout', cycle=i, lines=lines); break
    record(event='home_done', cycle=i, last_homing=d.get('last_homing'), alt=d.get('alt'), az=d.get('az'),
           lines=lines)
    r = None
    for attempt in range(8):
        try:
            r = post(S + '/api/sunscan/start', {})
            break
        except Exception as e:
            # 409: the previous scan's state has not cleared yet - wait and retry
            if '409' in str(e) and attempt < 7:
                time.sleep(5)
                continue
            record(event='scan_start_error', cycle=i, error=str(e)); r = None; break
    if r is None:
        break
    record(event='scan_started', cycle=i, response=r)
    d = wait_scan()
    if d is None:
        record(event='scan_timeout', cycle=i); break
    res = d.get('result') or {}
    record(event='scan_done', cycle=i, error=d.get('error'),
           alt_error_deg=res.get('alt_error_deg'), az_error_deg=res.get('az_error_deg'),
           az_error_sky_deg=res.get('az_error_sky_deg'), beam_fwhm_deg=res.get('beam_fwhm_deg'),
           sun_alt_deg=res.get('sun_alt_deg'), sun_az_deg=res.get('sun_az_deg'),
           fit_errors=(res.get('fit') or {}).get('fit_errors'), r_squared=(res.get('fit') or {}).get('r_squared'),
           image=res.get('image_path'))
    if d.get('error'):
        break
record(event='finished')
