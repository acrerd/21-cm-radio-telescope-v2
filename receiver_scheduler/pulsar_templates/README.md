# Pulsar profile templates

`b0329_1410_epn_hx97b.npz` is the template `pulsar_toa.py` times against. It holds B0329+54's full-Stokes profile at 1410 MHz (Effelsberg, 4096 bins), centred on the main peak:

- `t_ms`: time from the peak (ms)
- `I`, `Q`, `U`, `V`: the Stokes profiles, baseline removed

It was made from `hx97b_B0329+54_1410.txt`, the plain-text EPN file as downloaded, with columns sub-integration, channel, bin, I, Q, U, V.

Source: EPN Database of Pulsar Profiles (https://psrweb.jb.man.ac.uk/epndb/), dataset hx97b.
- Reference: von Hoensbroech, A. & Xilouris, K. 1997, A&AS 126, 121, "Effelsberg multifrequency pulsar polarimetry".
- Licence: Creative Commons Attribution 4.0.
