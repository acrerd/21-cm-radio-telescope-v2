#!/usr/bin/env python3
"""Read and plot an Acre Road SRT solar flux file, FITS or RSTN3.

    python read_solar_flux.py ../fits/acre_srt_1417MHz_20261007.fits
    python read_solar_flux.py ../rstn/07OCT26.ACR
    python read_solar_flux.py ../fits/acre_srt_1417MHz_20261007.fits --all   # flagged rows too
    python read_solar_flux.py FILE --save flux.png

Needs numpy, matplotlib and (for FITS) astropy. Nothing from the telescope's
own code, so it can be copied anywhere with the data.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import numpy as np


def read_fits(path):
    """(times as UTC datetimes, table of the FLUX extension, table of RUNS, primary header)."""
    from astropy.io import fits
    from astropy.table import Table
    from astropy.time import Time

    with fits.open(path) as hdul:
        header = hdul[0].header
        flux_header = hdul["FLUX"].header
    flux = Table.read(path, hdu="FLUX")
    runs = Table.read(path, hdu="RUNS")
    # TIME is seconds after MJDREF (00:00 UTC of the day), at each record's middle.
    day0 = Time(flux_header["MJDREF"], format="mjd", scale="utc").to_datetime(timezone=timezone.utc)
    times = np.array([day0 + timedelta(seconds=float(t)) for t in flux["TIME"]])
    return times, flux, runs, header


def read_rstn(path, freq_mhz=1415):
    """(times as UTC datetimes, flux in SFU) for one frequency of an RSTN3 file.
    Blank fields come back as NaN."""
    freqs = (245, 410, 610, 1415, 2695, 4995, 8800, 15400)
    col = freqs.index(freq_mhz)
    times, values = [], []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if len(line) < 18:
                continue
            # OBSC YYYYMMDDHHmmSS, then eight 6-character fields
            times.append(datetime.strptime(line[4:18], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc))
            field = line[18 + 6 * col:24 + 6 * col].strip()
            values.append(float(field) if field else np.nan)
    return np.array(times), np.array(values)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("file")
    p.add_argument("--all", action="store_true", help="plot flagged rows too (FITS)")
    p.add_argument("--save", help="write the plot to this file instead of showing it")
    a = p.parse_args(argv)

    import matplotlib
    if a.save:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(11, 5))
    if a.file.lower().endswith((".fits", ".fit", ".fts")):
        times, flux, runs, header = read_fits(a.file)
        good = np.ones(len(flux), bool) if a.all else (flux["QUALITY"] == 0)
        ax.plot(times[good], flux["FLUX"][good], ".", ms=2, label="Acre Road, %.2f MHz"
                % (header["FREQ"] / 1e6))
        if a.all:
            bad = flux["QUALITY"] != 0
            ax.plot(times[bad], flux["FLUX"][bad], "x", ms=3, color="grey", label="flagged")
        # The RSTN 1415 MHz local-noon values for the day, where the file has them.
        for key in header:
            if key.startswith("R_"):
                ax.axhline(header[key], ls="--", lw=0.8, color="C1")
                ax.text(times[good][0] if good.any() else times[0], header[key],
                        " RSTN " + header.comments[key].split(", ")[-1], fontsize=8,
                        color="C1", va="bottom")
        title = "%s %s - %d run%s" % (header["TELESCOP"], header["DATE-OBS"][:10],
                                       len(runs), "" if len(runs) == 1 else "s")
        for r in runs:
            print("%-26s %5d rows  mean %6.1f SFU  sigma %.2f  scallop %s"
                  % (r["FILE"], r["NROWS"], r["MEANFLUX"], r["SIGMA"], r["SCALSOL"]))
        day = times[0]
    else:
        times, values = read_rstn(a.file)
        ax.plot(times, values, ".", ms=2, label="1415 MHz field")
        title = "%s (RSTN3)" % a.file.split("/")[-1]
        day = times[0]
    ax.set_title(title)
    ax.set_xlabel("UTC on %s" % day.strftime("%Y-%m-%d"))
    ax.set_ylabel("Solar flux (SFU)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
    ax.legend(fontsize=8)
    fig.tight_layout()
    if a.save:
        fig.savefig(a.save, dpi=120)
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
