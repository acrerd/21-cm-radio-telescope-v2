#!/usr/bin/env python3
"""
Download the HI4PI survey files from CDS when they are not already here.

The scripts in this directory use two all-sky data products of the HI4PI
survey (HI4PI Collaboration 2016, A&A 594, A116; CDS J/A+A/594/A116):

    hi4pi_allsky_gal_CAR.fits  all-sky galactic plate-carree spectral
                               cube (ALLSKY/GAL/CAR.fits, ~33 GiB)
    hi4pi.fits                 N_HI HEALPix table (NHI_HPX.fits.gz,
                               318 MiB compressed, ~600 MiB unpacked)

ensure_file(path) fetches a missing file automatically.  Interrupted
downloads resume from where they stopped (HTTP Range + a .download
partial file), which matters for the 33 GiB cube.  Run this module
directly to pre-fetch every known file:

    python hi4pi_data.py
"""

import gzip
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

CDS = "https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/"

# local file -> (remote URL, gzipped on the server, size note)
FILES = {
    "hi4pi_allsky_gal_CAR.fits":
        (CDS + "ALLSKY/GAL/CAR.fits", False, "~33 GiB"),
    "hi4pi.fits":
        (CDS + "NHI_HPX.fits.gz", True, "318 MiB, unpacks to ~600 MiB"),
    # Stockert/Villa-Elisa 1420 MHz continuum survey (Reich 1982; Reich &
    # Reich 1986; Reich, Testori & Reich 2001), CADE HEALPix regridding,
    # mirrored at LAMBDA; used by continuum_compress.py
    "stockert_villaelisa_1420MHz_healpix.fits":
        ("https://lambda.gsfc.nasa.gov/data/foregrounds/reich_reich/"
         "STOCKERT+VILLA-ELISA_1420MHz_1_256.fits", False, "3.2 MiB"),
}

CHUNK = 1 << 20                       # 1 MiB read/write blocks
TRIES = 20                            # a 33 GiB pull drops a few times


def _fetch(url, part):
    """Download url into `part`, resuming from its current size."""
    for attempt in range(TRIES):
        pos = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(url)
        if pos:
            req.add_header("Range", f"bytes={pos}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if pos and resp.status != 206:
                    pos = 0           # server ignored Range: start over
                total = pos + int(resp.headers.get("Content-Length", 0))
                t0, pos0 = time.time(), pos
                with open(part, "ab" if pos else "wb") as out:
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        pos += len(block)
                        rate = (pos - pos0) / max(1e-9, time.time() - t0)
                        pct = (f"{100 * pos / total:5.1f}%" if total
                               else f"{pos >> 20} MiB")
                        sys.stderr.write(
                            f"\r  {pct} of {total >> 20} MiB   "
                            f"{rate / 2**20:6.1f} MiB/s   ")
                sys.stderr.write("\n")
                if total and pos < total:
                    raise OSError("connection closed early")
                return
        except urllib.error.HTTPError as err:
            if err.code == 416:       # the .download file is complete
                return
            raise
        except OSError as err:
            sys.stderr.write(f"\n  interrupted ({err}); "
                             f"retrying ({attempt + 1}/{TRIES})...\n")
            time.sleep(min(60, 5 * (attempt + 1)))
    raise OSError(f"download failed after {TRIES} attempts: {url}")


def ensure_file(path):
    """Return `path`, first downloading it from CDS if it is missing and
    is one of the known survey products above."""
    if os.path.exists(path):
        return path
    name = os.path.basename(path)
    if name in FILES:
        url, gzipped, size = FILES[name]
    else:
        return path                   # not a known file; caller reports
    print(f"{path} not found - downloading from CDS ({size}):\n"
          f"  {url}\n"
          f"  (Ctrl-C to stop; a rerun resumes where it left off.)")
    part = path + ".download"
    _fetch(url, part)
    if gzipped:
        print("  unpacking...")
        with gzip.open(part, "rb") as src, open(path + ".tmp", "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        os.replace(path + ".tmp", path)
        os.remove(part)
    else:
        os.replace(part, path)
    print(f"  done: {path}")
    return path


if __name__ == "__main__":
    for _name in FILES:
        ensure_file(_name)
