;+
; Read and plot an Acre Road SRT solar flux file, FITS or RSTN3.
;
;   IDL> read_solar_flux, '../fits/acre_srt_1417MHz_20261007.fits'
;   IDL> read_solar_flux, '../fits/acre_srt_1417MHz_20261007.fits', /all   ; flagged rows too
;   IDL> read_solar_flux, '../rstn/07OCT26.ACR'
;
; or call the readers directly:
;
;   IDL> flux = acre_read_fits(file, runs=runs, header=header, jd=jd)
;   IDL> good = where(flux.quality eq 0)
;   IDL> plot, (jd[good] - floor(jd[0] - 0.5d) - 0.5d) * 24, flux[good].flux, psym=3, /ynozero
;
;   IDL> acre_read_rstn, '../rstn/07OCT26.ACR', ut_hours, sfu
;
; FITS needs MRDFITS, HEADFITS and SXPAR from the IDL Astronomy User's
; Library (part of SolarSoft). RSTN3 needs nothing. Direct graphics, so it
; runs in GDL too.
;-

function acre_read_fits, file, runs=runs, header=header, jd=jd
  compile_opt idl2
  ; Extension 1 is FLUX, one row per record; extension 2 is RUNS, one row
  ; per recording. Column names become structure tags.
  header = headfits(file)
  flux = mrdfits(file, 1, fhdr, /silent)
  runs = mrdfits(file, 2, /silent)
  ; TIME is seconds after MJDREF (00:00 UTC of the day), at each record's middle.
  jd = sxpar(fhdr, 'MJDREF') + 2400000.5d + flux.time / 86400d
  return, flux
end


pro acre_read_rstn, file, ut_hours, sfu, freq=freq, date=date
  compile_opt idl2
  ; RSTN3: OBSC YYYYMMDDHHmmSS, then eight 6-character fields at
  ; 245, 410, 610, 1415, 2695, 4995, 8800, 15400 MHz. Blank = no data (NaN).
  if n_elements(freq) eq 0 then freq = 1415
  freqs = [245, 410, 610, 1415, 2695, 4995, 8800, 15400]
  col = (where(freqs eq freq))[0]
  if col lt 0 then message, 'not an RSTN frequency: ' + strtrim(freq, 2)
  n = file_lines(file)
  lines = strarr(n)
  openr, lun, file, /get_lun
  readf, lun, lines
  free_lun, lun
  keep = where(strlen(lines) ge 18, nkeep)
  if nkeep eq 0 then message, 'no records in ' + file
  lines = lines[keep]
  date = strmid(lines[0], 4, 4) + '-' + strmid(lines[0], 8, 2) + '-' + strmid(lines[0], 10, 2)
  ut_hours = fix(strmid(lines, 12, 2)) + fix(strmid(lines, 14, 2)) / 60d $
             + fix(strmid(lines, 16, 2)) / 3600d
  field = strtrim(strmid(lines, 18 + 6 * col, 6), 2)
  sfu = replicate(!values.f_nan, nkeep)
  ok = where(field ne '', nok)
  if nok gt 0 then sfu[ok] = float(field[ok])
end


pro read_solar_flux, file, all=all
  compile_opt idl2
  if stregex(file, '\.(fits|fit|fts)$', /boolean, /fold_case) then begin
    flux = acre_read_fits(file, runs=runs, header=header)
    ut = flux.time / 3600d
    if keyword_set(all) then good = lindgen(n_elements(ut)) $
    else good = where(flux.quality eq 0)
    date = strmid(sxpar(header, 'DATE-OBS'), 0, 10)
    plot, ut[good], flux[good].flux, psym=3, /ynozero, /nan, $
          xtitle = 'UT (hours) on ' + date, ytitle = 'Solar flux (SFU)', $
          title = strtrim(sxpar(header, 'TELESCOP'), 2) + ', ' $
                  + string(sxpar(header, 'FREQ') / 1d6, format='(F7.2)') + ' MHz'
    if keyword_set(all) then begin
      bad = where(flux.quality ne 0, nbad)
      if nbad gt 0 then oplot, ut[bad], flux[bad].flux, psym=7, symsize=0.3
    endif
    ; The RSTN 1415 MHz local-noon values for the day, where the file has them.
    keys = strtrim(strmid(header, 0, 8), 2)
    ref = where(strmid(keys, 0, 2) eq 'R_', nref)
    for i = 0, nref - 1 do begin
      v = sxpar(header, keys[ref[i]])
      oplot, !x.crange, [v, v], linestyle = 2
      xyouts, !x.crange[0], v, ' RSTN ' + strmid(keys[ref[i]], 2), charsize = 0.8
    endfor
    for i = 0, n_elements(runs) - 1 do $
      print, strtrim(runs[i].file, 2), runs[i].nrows, runs[i].meanflux, runs[i].sigma, $
             strtrim(runs[i].scalsol, 2), $
             format = '(A-26, I6, " rows  mean", F7.1, " SFU  sigma", F5.2, "  scallop ", A)'
  endif else begin
    acre_read_rstn, file, ut, sfu, date = date
    plot, ut, sfu, psym=3, /ynozero, /nan, xtitle = 'UT (hours) on ' + date, $
          ytitle = 'Solar flux (SFU)', title = file_basename(file) + ' (RSTN3, 1415 MHz field)'
  endelse
end
