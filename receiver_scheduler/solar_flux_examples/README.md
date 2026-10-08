# Acre Road SRT solar flux, 1417 MHz

The Sun's flux density from the 3 m dish at Acre Road Observatory (University of
Glasgow, 55.9024 N, 4.3079 W). The dish tracks the Sun whenever it clears the
local horizon and nothing else is booked. One file per UTC day in each of two
formats. The files are regenerated from the recordings each time a Sun run
ends, so today's file grows through the day. These files are copied in by
`receiver_scheduler/solar_flux_export.py`; edit them in
`receiver_scheduler/solar_flux_examples/`, not here.

```
fits/acre_srt_1417MHz_YYYYMMDD.fits   full record
rstn/DDMONYY.ACR                      RSTN3 archive format
examples/read_solar_flux.py           reader and plot, Python
examples/read_solar_flux.pro          reader and plot, IDL / GDL
examples/read_solar_flux_example.ipynb  worked example: one day, the flags and
                                      corrections, the brightest half hour,
                                      RSTN3 against FITS, every day against RSTN
```

## The measurement

- **Band:** 1415.71-1418.81 MHz, centred 1417.26 MHz. This is the receiver's
  continuum band, with the H I line and the LO spur left out.
- **Polarisation:** single linear. The flux is twice the received
  polarisation, which is right for an unpolarised Sun.
- **Records:** one every 3 s.
- **Calibration:** the bandpass template and the gain fitted on the sky, with
  T_sys subtracted. The antenna temperature is converted with
  S = 2kT_A/A_e, where A_e = lambda^2/Omega and Omega is the main lobe
  measured from a Sun drift (`BEAM_SA`, `AEFF` in the RUNS table).
  The feed was replaced on 2026-09-22. Each recording is reduced with the
  gain and the beam of its own feed: before the change, 21.6 sq deg; after,
  20.96. The flux is corrected
  to above the atmosphere with a zenith optical depth of 0.010 (`TAU_ZEN`).
- **Tracking scallop removed:** the drive moves in 0.5 deg steps, so the
  beam walks up to 0.25 deg either side of the Sun and the gain dips with
  each step, 0.5-1% peak to peak. The dip is fitted from the run itself, or
  taken from the last quiet run, whichever leaves less modulation. It is then
  divided out of the source term, record by record (`SCALLOP` column; the
  RUNS table says which solution was used).
- **Errors:** `ERROR` is the run's own record-to-record scatter, from second
  differences, typically 0.1-0.2 SFU. It holds the radiometer noise and the
  receiver's gain wobble, and nothing systematic. On the flux scale: on
  2026-10-07 this dish read 78 SFU against Learmonth 71 and San Vito 74. The gain and T_sys drift by ~0.3%/h through an evening and are
  not tracked within a run, and A_e is a main-lobe figure, an upper bound on
  the true aperture. The RSTN stations disagree with each other by 10-20 SFU
  on any day; their 1415 MHz noon values are in the FITS header (`R_*`).

## FITS

Extension 1, `FLUX`, has one row per record:

| column | unit | |
|---|---|---|
| TIME | s | record mid-point, seconds after `MJDREF` (00:00 UTC of the day) |
| TIMEDEL | s | integration of the record |
| FLUX | SFU | above the atmosphere, scallop removed |
| ERROR | SFU | statistical, per run |
| TANT | K | antenna temperature as measured (scallop removed, no atmosphere) |
| SCALLOP | | scallop gain divided out (1 = none) |
| TRANSMIS | | atmospheric transmission divided out |
| SUN_ALT, SUN_AZ | deg | true position of the Sun, no refraction |
| QUALITY | | bit flags, below |
| RUN | | row of `RUNS`, from 0 |

QUALITY bits:
- **1:** within 5 min of the receiver starting. It reads 0.3-1.7% high while
  it settles.
- **2:** beam off the Sun: a slew, or the stow at the end of a run.
- **4:** samples dropped by the radio in that record.

Flagged rows are kept in the file. Bursts are never flagged.

Extension 2, `RUNS`, has one row per recording that day: the source file, the
gain and T_sys used and when they were measured, the beam and effective
area used, the scallop solution
(`SCALSOL` fitted / carried / none, amplitudes and phases) and the modulation
before and after.

The time keywords follow the FITS 4.0 standard: `TIMESYS` UTC, `MJDREF`,
`TIMEUNIT` s, `TREFPOS` TOPOCENTER, `TIMEPIXR` 0.5. The observation keywords
follow SolarSoft: `DATE-OBS`, `DATE-END`, `TELESCOP`, `OBSRVTRY`,
`OBSGEO-X/Y/Z`. FLUX's unit is written `10**-22 W m-2 Hz-1`, because FITS has
no "sfu".

## RSTN3

The ASCII format of the RSTN 1-second archive. Each line is 66 characters
plus CRLF: `OBSCYYYYMMDDHHmmSS`, then eight 6-character fields at 245, 410,
610, 1415, 2695, 4995, 8800 and 15400 MHz, in integer SFU, blank where there
is no data. Only the 1415 field is filled, from the 1417.26 MHz band. There is
one line per 3 s record, at the UT second of its mid-point. A flagged record's
line is written blank. `ACRE` is our own code, not one RSTN assigned. Use the
FITS file for anything that needs better than 1 SFU, an error or a flag.

## Reading

```
python examples/read_solar_flux.py fits/acre_srt_1417MHz_20261007.fits
python examples/read_solar_flux.py rstn/07OCT26.ACR
IDL> read_solar_flux, 'fits/acre_srt_1417MHz_20261007.fits'
```

The notebook goes through both formats with the Python reader. Open it from
`examples/`, where it finds the files one folder up.

The Python reader needs numpy, matplotlib and astropy. The IDL reader needs
the IDL Astronomy User's Library (`mrdfits`, `sxpar`, part of SolarSoft) for
FITS, and nothing for RSTN3.
