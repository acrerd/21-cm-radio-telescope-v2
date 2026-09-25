#!/usr/bin/env python3
"""Export a pulsar-mode recording as a SIGPROC filterbank (.fil) for PRESTO.

The HDF5 the receiver writes (PulsarRecorder) already holds what a filterbank
holds - rows of channel power at a fixed sample time - so this is a
re-packaging, with three things to get right:

- **Channel order.** SIGPROC puts the highest frequency first with a negative
  channel step (`fch1`, `foff < 0`). Our axis is ascending, so every row is
  written reversed.
- **Gaps.** A .fil has no timestamps after the first: sample i is at
  tstart + i x tsamp. Our recording can have overflow gaps, which the radio's
  `time_marks` (row, device time) locate exactly. Each gap is filled with that
  many rows at the channels' median level, so every real row sits at the time
  PRESTO will assume for it. A zero fill would put a step into every channel.
- **The site.** A SIGPROC header carries a telescope id, not a position, and
  PRESTO maps ids to names and observatory codes in tables compiled into it.
  Stock PRESTO has no Acre Road, so we build prepfold with
  tools/presto_acre_road.patch: id 82 is "Acre Road SRT", code AR, at our
  surveyed ITRF position. Stock PRESTO reads id 82 as "Unknown" and folds the
  same; either way we fold with -topo at the topocentric period
  (pulsar_fold.run_topocentric_period).

Streams the rows in chunks: four hours at 1 ms and 16 channels is 900 MB,
which does not need to be in memory at once.

    python sigproc_export.py data/observations/20260926_010000_pulsar.h5
"""
import os
import struct

import numpy as np

TELESCOPE_ID = 82         # "Acre Road SRT" in our patched PRESTO (tools/presto_acre_road.patch)
MACHINE_ID = 0
CHUNK_ROWS = 200_000


def _str(key, value=None):
    """A SIGPROC header string: int32 length then the bytes."""
    b = key.encode("ascii")
    out = struct.pack("<i", len(b)) + b
    if value is not None:
        v = str(value).encode("ascii", "replace")
        out += struct.pack("<i", len(v)) + v
    return out


def _int(key, value):
    return _str(key) + struct.pack("<i", int(value))


def _dbl(key, value):
    return _str(key) + struct.pack("<d", float(value))


def _sexagesimal(value_deg, hours):
    """SIGPROC's packed angle: hhmmss.s (or ddmmss.s) as a float."""
    v = value_deg / 15.0 if hours else value_deg
    sign = -1.0 if v < 0 else 1.0
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60.0) * 3600.0
    return sign * (d * 10000 + m * 100 + s)


def header(source_name, ra_deg, dec_deg, tstart_mjd, tsamp_s, fch1_mhz, foff_mhz,
           nchans, rawdatafile="", telescope_id=TELESCOPE_ID, nbits=32):
    return b"".join([
        _str("HEADER_START"),
        _int("telescope_id", telescope_id), _int("machine_id", MACHINE_ID),
        _int("data_type", 1), _str("rawdatafile", rawdatafile), _str("source_name", source_name),
        _int("barycentric", 0), _int("pulsarcentric", 0),
        _dbl("az_start", 0.0), _dbl("za_start", 0.0),
        _dbl("src_raj", _sexagesimal(ra_deg, True)), _dbl("src_dej", _sexagesimal(dec_deg, False)),
        _dbl("tstart", tstart_mjd), _dbl("tsamp", tsamp_s), _int("nbits", nbits),
        _dbl("fch1", fch1_mhz), _dbl("foff", foff_mhz), _int("nchans", nchans), _int("nifs", 1),
        _str("HEADER_END"),
    ])


def read_header(path):
    """(dict, header_length_bytes) - the inverse of `header`, for checking."""
    ints = {"telescope_id", "machine_id", "data_type", "barycentric", "pulsarcentric",
            "nbits", "nchans", "nifs"}
    strs = {"rawdatafile", "source_name"}
    out = {}
    with open(path, "rb") as fh:
        def s():
            n = struct.unpack("<i", fh.read(4))[0]
            return fh.read(n).decode("ascii", "replace")
        if s() != "HEADER_START":
            raise ValueError("not a SIGPROC filterbank")
        while True:
            key = s()
            if key == "HEADER_END":
                return out, fh.tell()
            if key in ints:
                out[key] = struct.unpack("<i", fh.read(4))[0]
            elif key in strs:
                out[key] = s()
            else:
                out[key] = struct.unpack("<d", fh.read(8))[0]


def gap_plan(n_rows, dt_s, time_marks):
    """[(first_row, last_row_exclusive, fill_rows_before)] - the recording's
    stretches between radio time marks, with how many rows of fill each one
    needs in front of it to land at the time the file will imply. A mark
    that says time ran backwards or by less than a row is taken as no gap."""
    marks = np.asarray(time_marks, float).reshape(-1, 2) if time_marks is not None else np.empty((0, 2))
    marks = marks[np.argsort(marks[:, 0])] if len(marks) else marks
    if not len(marks):
        return [(0, n_rows, 0)]
    rows = [int(r) for r in marks[:, 0]]
    times = list(marks[:, 1])
    plan = []
    t_start = times[0] - dt_s * rows[0]
    written = 0                                   # rows emitted so far, fill included
    for k, r in enumerate(rows):
        lo = 0 if k == 0 else r
        hi = rows[k + 1] if k + 1 < len(rows) else n_rows
        want = int(round((times[k] + dt_s * (lo - r) - t_start) / dt_s))
        fill = max(0, want - written)
        plan.append((lo, hi, fill))
        written += fill + (hi - lo)
    return plan


def export(path, out_path=None, chunk_rows=CHUNK_ROWS):
    """Write `<recording>.fil`. Returns (out_path, summary dict)."""
    import h5py
    import pulsar_fold
    out_path = out_path or os.path.splitext(path)[0] + ".fil"
    with h5py.File(path, "r", swmr=True) as hf:
        a = dict(hf.attrs)
        if "power" not in hf:
            raise ValueError("%s is not a pulsar-mode recording (no `power`)" % os.path.basename(path))
        power = hf["power"]
        n_rows, nchan = power.shape
        freq = np.asarray(hf["frequency_hz"][:], float)
        marks = np.asarray(hf["time_marks"][:], float) if "time_marks" in hf else None
        dt = float(a.get("dt_s", pulsar_fold.DT_S))
        t0 = float(marks[0, 1] - dt * marks[0, 0]) if marks is not None and len(marks) else float(a["t0_unix"])
        psr = pulsar_fold.lookup(a.get("pulsar_name") or a.get("object_name"))
        name = psr["psrj"] if psr else str(a.get("pulsar_name", "unknown"))
        ra = psr["ra_deg"] if psr else float(a.get("pulsar_ra_deg", 0.0))
        dec = psr["dec_deg"] if psr else float(a.get("pulsar_dec_deg", 0.0))
        order = np.argsort(freq)[::-1]                     # highest first
        chan_w = float(np.median(np.diff(np.sort(freq)))) / 1e6
        # the fill level: each channel's median over a sample of the run
        sample = power[:: max(1, n_rows // 20000)]
        fill_row = np.median(sample, axis=0)[order].astype(np.float32)
        plan = gap_plan(n_rows, dt, marks if marks is not None and len(marks) else None)
        hdr = header(name, ra, dec, t0 / 86400.0 + 40587.0, dt, freq[order[0]] / 1e6, -chan_w, nchan,
                     rawdatafile=os.path.basename(path))
        filled = 0
        with open(out_path, "wb") as fh:
            fh.write(hdr)
            for lo, hi, fill in plan:
                if fill:
                    block = np.broadcast_to(fill_row, (min(fill, chunk_rows), nchan))
                    left = fill
                    while left:
                        k = min(left, chunk_rows)
                        fh.write(np.ascontiguousarray(block[:k], dtype="<f4").tobytes())
                        left -= k
                    filled += fill
                for c0 in range(lo, hi, chunk_rows):
                    rows = np.asarray(power[c0:min(hi, c0 + chunk_rows)], dtype=np.float32)[:, order]
                    fh.write(np.ascontiguousarray(rows, dtype="<f4").tobytes())
    return out_path, {"rows": n_rows, "fill_rows": filled, "samples": n_rows + filled,
                      "nchans": nchan, "tsamp_s": dt, "fch1_mhz": freq[order[0]] / 1e6,
                      "foff_mhz": -chan_w, "tstart_mjd": t0 / 86400.0 + 40587.0, "source": name,
                      "gaps": sum(1 for p in plan if p[2])}


def main():
    import argparse
    import json
    p = argparse.ArgumentParser(description="pulsar-mode HDF5 to SIGPROC .fil, for PRESTO")
    p.add_argument("recording")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    out, summary = export(args.recording, args.out)
    print(json.dumps(summary, indent=1))
    print("written:", out)
    print("fold with, e.g.:  prepfold -nobary -p <topocentric period> -dm 26.76 %s" % os.path.basename(out))


if __name__ == "__main__":
    main()
