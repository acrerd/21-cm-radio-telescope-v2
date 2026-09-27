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
| B200 PPS IN | Thunderbolt **1 PPS** | **pending**. The input takes 1.8–5 V, so the Thunderbolt's TTL is fine. Picked up automatically (`H1_TIME_SOURCE=auto`). |
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
     pipewire wireplumber gstreamer1.0-tools
sudo usermod -aG video,dialout,plugdev astro      # camera, serial, USB
# log out and back in: a new group reaches only processes started from a new login
```

- **Desktop applications the launcher opens:** VS Code (`code`), Firefox (the snap), Stellarium (optional).
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

- **Desktop launcher:** `~/Desktop/Start SRT Sofware.desktop` (the spelling is as found) runs `receiver_scheduler/start_srt_software.sh`. That opens the VS Code workspace `receiver_scheduler/SRT Software.code-workspace`, Firefox on the scheduler and the controller, and Stellarium, and tiles them with `wmctrl`.
  ```
  [Desktop Entry]
  Type=Application
  Name=Start SRT Sofware
  Exec=/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/start_srt_software.sh
  Path=/home/astro/21-cm-radio-telescope-v2
  Terminal=false
  Icon=applications-science
  ```
- **The scheduler:** the workspace's folder-open task runs `receiver_scheduler/start_scheduler.sh`. It starts `h1_web_scheduler.py` on 127.0.0.1:5000 only if one is not already running.
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
5. The camera: `curl -o /tmp/s.jpg -w '%{http_code}' http://127.0.0.1:5000/api/camera/snapshot` returns 200.
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
