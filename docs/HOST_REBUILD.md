# Rebuilding the observatory computer

This document takes a blank replacement machine to a working Acre Road SRT workstation. It is written for whoever does it, very likely an agent such as Claude, and so it gives exact paths, versions and commands. Where something is already documented elsewhere, this document points to it instead of repeating it.

**Last checked against the running machine: 2026-09-27** (host `ettus3`).

**Keep it current.** Any change to the host goes in here in the same commit: a package, a udev rule, a service, a path, a group, a cron job, a file outside git that the telescope depends on. The environment snapshots in `docs/host/` are regenerated with the commands in §11. A rebuild that fails because this document is stale is this document's fault.

---

## 1. The machine as it is

| | |
|---|---|
| Hostname | `ettus3` |
| OS | Ubuntu 24.04.4 LTS (Noble), kernel 7.0 generic |
| CPU / RAM / disk | Intel i7-4790, 8 threads; 8 GB RAM; 1.8 TB (`/dev/sda2`, 5 % used) |
| User | `astro` (uid 1000). Groups: `adm dialout cdrom sudo dip video plugdev users lpadmin` |
| Repository | `/home/astro/21-cm-radio-telescope-v2`, cloned from `https://github.com/acrerd/21-cm-radio-telescope-v2.git` |
| Python (observing) | radioconda at `/home/astro/radioconda`: Python 3.12.9, GNU Radio 3.10.12.0, UHD 4.8.0 |
| Python (timing) | conda env `pint`: PINT 1.1.7, Python 3.11 |
| Python (PRESTO) | conda env `presto` (conda-forge `presto-pulsar`), plus a patched `prepfold` in `/home/astro/opt/presto-acre/bin/` |
| Firmware toolchain | PlatformIO at `~/.platformio/penv` |
| Time | `systemd-timesyncd` (NTP, ntp.ubuntu.com). No chrony |

**Paths are hard-coded.** `/home/astro/...` appears throughout: the launch scripts, `receiver_python_path`, `PINT_PY`, `PRESTO_BIN`, `PREPFOLD_ACRE`, and Claude's memory path. Keep the user `astro` and these locations. Moving them means editing all of those.

**Memory is tight.** 8 GB was enough once the pulsar fold was made to stream (§6 of `docs/PULSAR_PROCESSING.tex`). A replacement should have 16 GB or more.

---

## 2. What plugs into it

| Connection | Where | Notes |
|---|---|---|
| USRP **B200** (one RX channel, AD9364) | a **USB 3** port | `lsusb` calls it "USRP B210": the USB ID 2500:0020 is shared by both models. UHD says `Detected Device: B200`. It must be on USB 3 (5000M in `lsusb -t`). |
| B200 RX2 | the receive chain: feed → SAWbird+ H1 → cable | |
| B200 TX/RX | 30 dB pad → the vertex dipole | the pilot and the test transmitter; off by default |
| B200 REF IN | Thunderbolt GPSDO **10 MHz** | selected and checked by `select_clock_source` through the `ref_locked` sensor |
| B200 PPS IN | Thunderbolt **1 PPS** | in use: the 2026-09-28 pulsar run recorded `time_source` pps. The input takes 1.8–5 V, so the Thunderbolt's TTL is fine. Picked up automatically (`H1_TIME_SOURCE=auto`). |
| Thunderbolt serial (DB9) | USB, through an **RS-232** adapter (FTDI FT232R, serial `A9DZ2ZSW`) | **connected 2026-09-30**, decoded first time: locked, 8 satellites, osc ±0.14 ppb, PPS ~1 ns, DAC 2.111 V, 43.5 °C. TSIP at 9600 8-N-1, read by `thunderbolt.py`. Until the udev rule of §7 exists, `scheduler_config.json` sets `thunderbolt_device` to `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A9DZ2ZSW-if00-port0`. Unit settings (in its EEPROM, not on this computer): loop time constant **100 s**, damping 1.0; **cable-delay compensation −50.5 ns** (PPS offset, negative advances: 10 m of RG-58 at velocity factor 0.66 to a Leo Bodnar **LBE-1304-10-BX1** antenna; the antenna's own delay is not included); **surveyed antenna position 55.9025269, −4.3074782, 108.75 m above the WGS-84 ellipsoid** (self-survey of 2000 fixes, 2026-09-30, 3.05 m east and 1.8 m below the previous stored position; 26.6 m from the dish at bearing 65°). All set 2026-09-30 through the scheduler (`/api/clock/survey`, `/api/clock/cable-delay`); read them with the monitor or TSIP 0x8E-A8 / 0x8E-4A, and set a replacement unit the same. RS-232 levels: a 3.3 V TTL adapter will not read it. **Plugging it in reset the B200** (it dropped off USB 4 s later and the running receiver aborted): plug USB devices in between observations. |
| Controller (WT32-ETH01) | the second network card, TP-Link TG-3468 (`enp5s0`, `r8169`) | a private link: see `docs/OBSERVATORY_HOST_SETUP.md` |
| Campus network | the motherboard network card | internet, NTP, GitHub, remote access |
| Safety camera | USB (`/dev/video0`) | needs the `video` group (§7) |

---

## 3. Order of work

1. Install the OS and create the user (§4).
2. Build the controller link (§5, by following `docs/OBSERVATORY_HOST_SETUP.md`).
3. Install radioconda and its additions (§6).
4. Set up UHD: the udev rule and the FPGA images (§7).
5. Clone the repository, authenticate git, **restore the files git does not hold** (§8).
6. Create the PINT and PRESTO environments and build the patched prepfold (§9).
7. Install PlatformIO (only needed for firmware work) (§10).
8. Set up the launchers, remote access and Claude's memory (§10).
9. Run the whole check (§12).

---

## 4. OS and user

```
# Ubuntu 24.04 LTS desktop, user astro (uid 1000)
sudo apt update && sudo apt install -y git gh curl wmctrl openssh-server network-manager \
     pipewire wireplumber gstreamer1.0-tools gstreamer1.0-plugins-good   # v4l2src, jpegenc: the camera stream
sudo usermod -aG video,dialout,plugdev astro      # camera, serial, USB
# log out and back in: a new group reaches only processes started from a new login
```

- **Desktop applications the launcher opens:** Firefox (the snap). Stellarium is installed but started by hand (§10).
- **ssh:** the observatory is normally worked over ssh. Copy `~/.ssh/authorized_keys` from the old machine, or add keys afresh; see `docs/SSH_ACCESS.txt`.

---

## 5. The controller link

Follow `docs/OBSERVATORY_HOST_SETUP.md` in full: the second card, the NetworkManager connection `srt-link` (`ipv4.method shared`, host 192.168.50.1/24), the controller's address pinned by MAC in `/etc/NetworkManager/dnsmasq-shared.d/srt.conf`, the firewall, and the checks. The controller keeps its own settings and its pointing model in its own flash, so nothing on the controller changes when the host is replaced.

---

## 6. radioconda (the observing Python)

1. Install **radioconda 2025.03.14** (linux-64) to `/home/astro/radioconda` with its installer from https://github.com/ryanvolz/radioconda/releases. A newer release is fine only if GNU Radio 3.10 and UHD 4.x remain. Then rerun the test suite, because behaviour has been verified on GNU Radio 3.10.12 and UHD 4.8.0. One specific: through `integrate_ff`, GNU Radio 3.10.12 rounds a tag's offset to the nearest output item, and `_TagTap` relies on reading tags before that block.
2. Add what was installed by hand after the installer, with pip into radioconda:
   ```
   /home/astro/radioconda/bin/python -m pip install astropy==8.0.1 pytest==9.1.1 esprima ipykernel psutil
   ```
   - `astropy` brings `pyerfa` and `astropy-iers-data`.
   - `esprima` is used by `tests/test_page_structure.py` to parse the page's JavaScript.
   - `ipykernel` is for the notebooks.
3. The full package list at the time of writing is `docs/host/radioconda-base-packages.txt` (conda) and `docs/host/radioconda-pip-freeze.txt` (pip). Use them to find anything missing.

Do not install anything else into radioconda: PINT and PRESTO have their own environments (§9). The scheduler re-executes itself under `/home/astro/radioconda/bin/python`.

---

## 7. UHD: USB permissions and firmware images

```
sudo cp /home/astro/radioconda/lib/uhd/utils/uhd-usrp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
/home/astro/radioconda/bin/uhd_images_downloader -t b2xx     # into radioconda/share/uhd/images
/home/astro/radioconda/bin/uhd_find_devices                   # must list the B200
```

- The images needed are `usrp_b200_fw.hex` and `usrp_b200_fpga.bin`. The current machine has the full set in `/home/astro/radioconda/share/uhd/images/`.
- If the rule file is somewhere else in the radioconda tree, find it with `find /home/astro/radioconda -name uhd-usrp.rules`.

**Thunderbolt serial adapter** (connected 2026-09-30, serial `A9DZ2ZSW`; the rule below is not yet installed - it needs sudo - so the config uses the by-id path instead). A fixed name, so the scheduler's `thunderbolt_device` (`/dev/thunderbolt`) survives other USB-serial devices appearing first. Fill in the adapter's own serial number from `udevadm info -a -n /dev/ttyUSB0 | grep '{serial}'`; the vendor and product are FTDI's FT232R.

```
sudo tee /etc/udev/rules.d/99-thunderbolt.rules <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="A9DZ2ZSW", SYMLINK+="thunderbolt", GROUP="dialout", MODE="0660"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/thunderbolt                                        # -> ttyUSBn
/home/astro/radioconda/bin/python receiver_scheduler/thunderbolt.py /dev/thunderbolt --seconds 5
```

The last line should print an `AB` and an `AC` line each second. `astro` needs the `dialout` group (§4). No scheduler restart is needed: it looks for the device every 30 s.

**Camera.** The camera needs the scheduler process itself to hold the `video` group. A scheduler started from a shell that predates `usermod` lacks it until the next login. The launch line used from such a shell is `setsid nohup sg video -c "bash start_scheduler.sh"`, and after a fresh login a plain start is enough. Check with `grep Groups /proc/<scheduler pid>/status`, which must list 44.

---

## 8. The repository, and the files git does not hold

```
cd /home/astro && git clone https://github.com/acrerd/21-cm-radio-telescope-v2.git
gh auth login          # GitHub, HTTPS, "Login with a web browser" or a token
gh auth setup-git      # git then uses gh's token over HTTPS
```

In git already: the code, the calibrations in force (`gain_calibration.json`, `bandpass_template*.json`, `beam_calibration.json` and `beam_calibrations/`, `horizon_profiles/`, `pointing_data*.json`, `scallop_reference.json`), the pulsar timing products (`receiver_scheduler/pulsar_timing/`) and templates.

**Not in git: copy these from the old machine or its backup.** `receiver_scheduler/` is abbreviated `rs/` below.

| File | What it is | If lost |
|---|---|---|
| `rs/scheduler_config.json` | the live configuration: controller URL, `pulsar_*` band, `receiver_pilot_enabled: false`, `sun_monitor`, banner, any `receiver_*` instrument overrides | the scheduler starts on code defaults. **Check the pilot and instrument keys** before observing. |
| `rs/h1_schedule.json` | the bookings | future bookings are gone |
| `rs/pointing_model.json` | the scheduler's copy of the model in force, used by the scallop reduction and drift parking | the controller still holds the model and `/pointing` serves it; files fall back to the model embedded in each recording |
| `rs/last_observation.json` | pointer to the last run | cosmetic |
| `rs/data/` (about 3 GB now) | every recording (`data/observations/`), `solar_reference_history.json`, PRESTO exports, diagnostics | recordings are "archived on the sky", but a year of them is not re-observable in practice. **Copy it.** |
| `rs/scheduler.log*` | the operational record | history only |
| `~/.claude/projects/-home-astro-21-cm-radio-telescope-v2/memory/` | Claude's working memory for this project | Claude loses the lessons learned. Copy the folder to the same path. |
| `~/.config/gh/hosts.yml` | the GitHub token | re-run `gh auth login` rather than copy a token |
| `/etc/NetworkManager/...`, `/etc/udev/rules.d/uhd-usrp.rules` | system configuration | rebuilt by §5 and §7 |

---

## 9. Timing and PRESTO environments

```
C=/home/astro/radioconda/bin/conda
$C env create -f /home/astro/21-cm-radio-telescope-v2/docs/host/env-pint.yml     # PINT 1.1.7, Python 3.11
$C env create -f /home/astro/21-cm-radio-telescope-v2/docs/host/env-presto.yml   # presto-pulsar and build tools
```

The exact package lists at the time of writing are `docs/host/env-pint-packages.txt` and `docs/host/env-presto-packages.txt`.

**PINT** needs the JPL ephemeris DE421 on first use and fetches it itself into `~/.cache/astropy`, so it needs internet once. `pulsar_toa.py` calls PINT at `/home/astro/radioconda/envs/pint/bin/python` (`PINT_PY`).

**Patched prepfold**, so that PRESTO knows the site. Instructions are at the top of `tools/presto_acre_road.patch`:

```
mkdir -p /home/astro/src && cd /home/astro/src
git clone https://github.com/scottransom/presto.git && cd presto
git checkout 06083ad                                  # the commit it was built from on 2026-09-25
git apply /home/astro/21-cm-radio-telescope-v2/tools/presto_acre_road.patch
# in the presto env:
meson setup build-acre --prefix=/home/astro/opt/presto-acre && meson compile -C build-acre prepfold
mkdir -p /home/astro/opt/presto-acre/bin
cp build-acre/src/prepfold build-acre/src/libpresto.so /home/astro/opt/presto-acre/bin/
```

`libpresto.so` must sit next to `prepfold`, because its rpath puts `$ORIGIN` first. If the patched binary is missing, `pulsar_fold.prepfold_path()` falls back to the stock one, which folds the same but calls the site "Unknown".

---

## 10. Launchers, firmware tools, remote access, Claude

- **Desktop launcher:** `~/Desktop/Start SRT Sofware.desktop` (the spelling is as found) runs `receiver_scheduler/start_srt_software.sh`. That starts the scheduler (below) and opens Firefox on the controller. VS Code and Stellarium were taken out of it on 2026-10-01 at the operator's request.
  ```
  [Desktop Entry]
  Type=Application
  Name=Start SRT Sofware
  Exec=/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/start_srt_software.sh
  Path=/home/astro/21-cm-radio-telescope-v2
  Terminal=false
  Icon=applications-science
  ```
- **The scheduler:** the launcher runs `receiver_scheduler/start_scheduler.sh` detached (`setsid`, console output in `/tmp/srt-scheduler-console.log`); the VS Code workspace `receiver_scheduler/SRT Software.code-workspace` still runs the same script on folder-open if it is opened by hand. It starts `h1_web_scheduler.py` on 127.0.0.1:5000 only if one is not already running. To restart it over ssh: check `/api/status` is idle, `kill` the scheduler's PID, then `setsid nohup receiver_scheduler/start_scheduler.sh > /tmp/srt-scheduler-console.log 2>&1 < /dev/null &`. A restart clears the monitors' hold-off and the Thunderbolt's in-memory history.
- **Stellarium** (by hand, not from the launcher): `~/.stellarium/modules/TelescopeControl/telescopes.json` holds the telescope, `"host_name": "192.168.50.120"`, `"tcp_port": 10001`, `"connection": "remote"`, `"equinox": "J2000"`, `"connect_at_startup": true`. Until 2026-10-01 it still named the pre-link address 192.168.106.120 and tried to connect there over the campus network. A goto from Stellarium reaches the controller without the scheduler seeing it.
  - It is **deliberately not a systemd service**, and must not come up unattended.
  - It binds to loopback on purpose: it has no authentication.
- **PlatformIO** (only for ESP32 or Due firmware): `python3 -m venv ~/.platformio/penv && ~/.platformio/penv/bin/pip install platformio`. Builds per CLAUDE.md, using `~/.platformio/penv/bin/pio`.
- **Remote access:** ssh with port forwarding for the web UIs, and waypipe for applications. See `docs/SSH_ACCESS.txt`, `docs/ettus3-tunnel.bat` and `docs/OBSERVATORY_HOST_SETUP.md` §10. A new host key means updating `known_hosts` on the remote machines.
- **Claude:** install Claude Code. Copy the project memory folder (§8). `CLAUDE.md` in the repository carries the rest.

---

## 11. Refreshing the snapshots in `docs/host/`

Run after any change to an environment, and commit the result with the change:

```
C=/home/astro/radioconda/bin/conda; D=/home/astro/21-cm-radio-telescope-v2/docs/host
$C list -n base --export > $D/radioconda-base-packages.txt
/home/astro/radioconda/bin/python -m pip list --format=freeze > $D/radioconda-pip-freeze.txt
$C env export -n pint   --from-history | grep -v '^prefix:' > $D/env-pint.yml
$C env export -n presto --from-history | grep -v '^prefix:' > $D/env-presto.yml
$C list -n pint --export > $D/env-pint-packages.txt; $C list -n presto --export > $D/env-presto-packages.txt
```

---

## 12. Checking the rebuilt machine

Do these in order; each depends on the ones before.

1. `uhd_find_devices` lists a **B200**. `lsusb -t` shows it at 5000M.
2. A 60 s spectral recording with the mount parked:
   ```
   H1_OUTPUT_FILE=/tmp/check.h5 timeout -s INT 60 /home/astro/radioconda/bin/python receiver_scheduler/b210_h1_receiver.py --headless --sdr b210
   ```
   The log must say `Clock: EXTERNAL 10 MHz reference, locked`, and the file's `overflows` must sum to 0.
3. The controller: `curl http://192.168.50.120/status` answers. Check its NTP sync as `docs/OBSERVATORY_HOST_SETUP.md` §8 describes.
4. The scheduler: start it from the desktop launcher, then check that `curl http://127.0.0.1:5000/api/status` answers and the page loads.
5. The camera: `curl -o /tmp/s.jpg -w '%{http_code}' http://127.0.0.1:5000/api/camera/snapshot` returns 200. The snapshot alone does not prove the live view, since it falls back to a one-shot read of the device: `curl -s -m 5 'http://127.0.0.1:5000/api/camera/stream?fps=5' | grep -a -c srt-camera` should count frames, and must do so with nobody logged in at the console.
6. The tests: `cd receiver_scheduler && /home/astro/radioconda/bin/python -m pytest`.
   - About 700 should pass.
   - `TestFlaskAPI::test_post_config` fails on the observatory host by design, because it reaches the live controller.
   - **Run the tests only while nothing is recording.**
   - `tests/test_pulsar_toa.py::test_pint_agrees_with_our_phase_and_reads_our_tim` exercises the PINT environment and our site registration.
7. PRESTO: on the Observe tab, select a pulsar recording and press **PRESTO fold**. The plot must name the telescope "Acre Road SRT", which proves the patched binary is in use.
8. The whole chain: book a short tracked spectrum, let it run, and plot it.

---

## 13. Worth doing while the old machine still exists

- Copy §8's list to the new machine, then compare checksums of `data/observations/`.
- Keep the old disk untouched until the new machine has observed and plotted a full night.
- There is no scheduled backup of §8's files today. A nightly copy of `receiver_scheduler/data/` and the untracked JSON files to other storage would make a hardware failure an inconvenience rather than a loss.
