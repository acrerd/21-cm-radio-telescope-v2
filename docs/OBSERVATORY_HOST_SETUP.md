# Observatory Host Setup

**Status:** Implemented (first built 2026-08-19, issue #10)
**Purpose:** Build an observatory computer that owns a private Ethernet link to the SRT controller

---

## 1. What this builds and why

The observatory computer is given a **second Ethernet card**. The controller
(WT32-ETH01) hangs off it on a private point-to-point link that this host owns
completely: it serves the address, resolves names, and routes the link out to the
internet.

The first card stays on the site LAN and is how people reach the computer.

```
   site LAN ──── eno1 ┐
                      │  observatory computer     (NAT + DHCP + DNS)
  controller ─── enp5s0 ┘  192.168.50.1/24
  192.168.50.120
```

**Why bother.** On the site LAN the controller had whatever address the site
handed it, and that address was hard-coded in seven places in the code and
twenty-five in the documentation. Every renumbering was a code change. On a
private link the address is permanently ours.

**No firmware change is needed.** The controller stays on DHCP; its address is
pinned by MAC on the host. Nothing on the controller is configured for this link,
so the whole arrangement reverses by moving one cable back to the site switch.
That reversibility is the point — keep it.

**The one thing the link breaks is time**, and it matters more than it sounds.
The controller syncs its clock from the internet, and pointing is computed from
sidereal time, so a link with no route out mispoints the telescope overnight in a
way that is tedious to diagnose after the fact. Step 4 routes it; step 8 proves
it. Do not skip either.

### Address plan

Must avoid the site LAN, `192.168.4.0/24` (the controller's own WiFi AP), and
`192.168.1.0/24` (common home-router range, and the firmware's old placeholder).
`192.168.50.0/24` is used here; nothing depends on the numbers, but if you change
them, change `DEFAULT_ETH_*` in `esp32_controller_arduino/src/config.h` too.

| Host | Address |
|---|---|
| Observatory computer, second card | `192.168.50.1` |
| Controller | `192.168.50.120` |

---

## 2. Prerequisites

- Ubuntu 24.04 with **NetworkManager** managing the interfaces (this guide was
  written against NM 1.46). If the host uses `systemd-networkd` or plain netplan
  without NM, the `nmcli` steps need translating.
- A free PCIe slot.
- `radioconda` at `/home/astro/radioconda` for the scheduler and receiver.
- PlatformIO at `/home/astro/.platformio/penv/bin/pio` for firmware builds.
- The repository cloned at `/home/astro/21-cm-radio-telescope-v2`.

Confirm NetworkManager is in charge before going further:

```bash
nmcli device status        # the site interface should show a connection name
```

---

## 3. Fit the second card

**TP-Link TG-3468** (~£10). PCIe x1, Realtek RTL8168, driven by the in-kernel
`r8169` module, so Ubuntu brings it up with no driver install. Ships with both
full-height and low-profile brackets.

> **Avoid cheap "Intel I210" listings.** A genuine i210-T1 is the better card in
> the abstract, but real ones are £35-45 from a trade supplier and the £15-20
> marketplace listings are a well-documented counterfeit market. Counterfeits
> frequently carry cloned or duplicated MAC addresses — exactly the failure this
> design depends on *not* happening, since the controller's address is pinned by
> MAC. The link carries a status poll and a coordinate stream, and the
> controller's PHY is 100BASE-TX anyway: buy on driver reliability, not speed.

Shut down, unplug from the mains, fit the card, boot.

```bash
ip -br link                       # a new interface appears, e.g. enp5s0
lspci -k | grep -A3 -i ethernet   # confirm "Kernel driver in use: r8169"
```

Note the interface name — every step below needs it. It comes from the card's bus
position, so unlike a USB adapter it is stable across reboots and does not depend
on the MAC.

If nothing new appears, check `lspci | grep -i ethernet` to see whether the card
is on the bus at all.

---

## 4. Build the link

Do this while the controller is **still on the site switch**. Nothing is at risk
here; the controller stays reachable throughout.

Substitute your interface name for `enp5s0` and the controller's MAC for the one
below — read it from the controller's web UI (`/wifi/status` → `eth_mac`) or from
the site switch's table.

```bash
sudo nmcli connection add type ethernet \
    con-name srt-link \
    ifname enp5s0 \
    ipv4.method shared \
    ipv4.addresses 192.168.50.1/24 \
    ipv4.never-default yes \
    ipv6.method ignore
```

- **`ipv4.method shared` is the important one.** It sets the address, enables IP
  forwarding, installs the NAT rules so the controller can reach the internet,
  and runs a dnsmasq on the interface providing DHCP and a DNS forwarder. All
  four in one setting.
- **`ipv4.never-default yes`** stops the host routing general traffic down a link
  with only a microcontroller on the far end.
- **`ipv6.method ignore`** — nothing here speaks IPv6.

Pin the controller's address by MAC so it can stay on DHCP:

```bash
sudo tee /etc/NetworkManager/dnsmasq-shared.d/srt.conf >/dev/null <<'EOF'
# Controller stays on DHCP; the address is pinned here so the whole
# arrangement is reversible by moving one cable.
dhcp-host=70:4B:CA:58:59:8B,192.168.50.120,srt-controller
EOF

sudo nmcli connection up srt-link
```

`shared` mode leases from `.10` upward, and a `dhcp-host` entry takes precedence
within that range, so `.120` is safe.

If another connection profile might claim the new interface, stop it:

```bash
nmcli -f NAME,DEVICE,AUTOCONNECT connection show      # look for "Wired connection 1"
sudo nmcli connection modify "Wired connection 1" connection.autoconnect no
```

**Activation without a cable is expected to succeed** but the interface stays
`NO-CARRIER` until step 6. That is normal.

Check:

```bash
sysctl net.ipv4.ip_forward                  # must be 1
sudo nft list ruleset | grep -i masquerade  # a rule for 192.168.50.0/24
pgrep -af "dnsmasq.*enp5s0"                 # dnsmasq with --conf-dir set
```

---

## 5. Firewall

Ubuntu ships `ufw`. **Check whether it is actually enforcing, and do not trust
`systemctl`:**

```bash
systemctl is-active ufw     # says "active" even when the firewall is OFF
sudo ufw status             # this is the real answer
```

That distinction has caused real confusion — the unit being active says nothing
about whether rules are applied.

Whether or not you enable it, the rules below are the correct set. Add them
first, enable second, and **do it at the console** — enabling a firewall over ssh
is how people lock themselves out.

```bash
sudo ufw limit 22/tcp                    comment 'ssh (rate limited)'
sudo ufw allow in on enp5s0              comment 'SRT private link (single trusted device)'
sudo ufw route allow in on enp5s0 out on eno1 comment 'SRT link NAT out'

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw default deny routed

sudo ufw enable
```

Two of those are non-obvious and both were learned the hard way:

- **`route allow … out on eno1` is what keeps the controller's clock working.**
  The default forward policy is deny, which silently blocks the NAT. The
  controller then never syncs time again and the mount mispoints overnight.
- **`allow in on enp5s0` is what keeps OTA firmware updates working.** `espota`
  sends its invitation to the controller and then expects the controller to
  connect *back* to the host on an **ephemeral** port — a different one every
  run — so it cannot be allowed by port number. Without this rule the upload
  fails with the unhelpful `No response from device`, because the invitation
  succeeds and only the callback is blocked. Trusting the link interface as a
  whole is reasonable: it carries exactly one device, on a subnet this host owns
  and serves.

Nothing needs an inbound rule on the site interface. In particular **do not open
port 5000** — see section 7.

---

## 6. Move the controller

This is the only moment with any risk, and it is a cable's width of risk.

Move the controller's Ethernet cable from the site switch to the new card. The
controller handles link-down/link-up, re-runs DHCP, and re-arms its time sync on
every `GOT_IP`. Nothing on it is reconfigured.

```bash
journalctl -u NetworkManager -f      # watch for DHCPACK … 192.168.50.120
```

**If anything misbehaves, move the cable back.** That restores the previous state
exactly. Second fallback is the controller's WiFi AP (`SRT_Controller`, at
`192.168.4.1`), which stays up regardless of Ethernet settings.

---

## 7. Software configuration

**Scheduler runtime config** — `receiver_scheduler/scheduler_config.json` is
gitignored and is the runtime source of truth:

```json
"srt_controller_url": "http://192.168.50.120",
"srt_controller_fallback_urls": ["http://srt-controller.local",
                                 "http://192.168.4.1"]
```

Set it through the scheduler's Configuration tab or `POST /api/config` so the
running process and the file agree. The scheduler restricts CORS to these
origins, so if they do not match the address the controller's page is served
from, the scheduler buttons embedded in that page stop working with no error
beyond a browser console message.

**The scheduler binds loopback, and must stay that way.** It has no
authentication of any kind, and its endpoints start observations, take the SDR,
rewrite the schedule, push a pointing model and **flash controller firmware over
OTA**. Bound to `0.0.0.0` that is an unauthenticated telescope-control API
offered to every network the host is attached to. Nothing needs the wildcard: the
controller's own page fetches the scheduler at `127.0.0.1:5000` from a browser
running on this host, and remote use is over waypipe, which forwards the display
rather than the connection.

**The scheduler is deliberately not a service.** It starts from the desktop
**Start SRT Software** launcher, via the VS Code workspace's `folderOpen` task,
which runs `receiver_scheduler/start_scheduler.sh`. That wrapper reuses an
already-serving scheduler instead of starting a second one that would die on the
bound port. Do not add a systemd unit: it would contend with the task for port
5000, and the scheduler should not come up unattended.

**Firmware defaults** — if you changed the subnet, update `DEFAULT_ETH_STATIC_IP`,
`DEFAULT_ETH_GATEWAY`, `DEFAULT_ETH_SUBNET` and `DEFAULT_ETH_DNS` in
`esp32_controller_arduino/src/config.h`. These are only used if someone turns
DHCP off in the web UI, but they are what that form is pre-filled with — left
pointing at another subnet, one tick of that box moves the controller off the
link entirely.

**OTA target** — `esp32_controller_arduino/platformio.ini`, `[env:wt32-eth01-ota]`,
`upload_port = 192.168.50.120`.

---

## 8. Verify

Run from the observatory computer.

```bash
ping -c3 192.168.50.120
curl http://192.168.50.120/ping        # -> ok
curl http://192.168.50.120/network     # eth_ip, and the DNS diagnostics
curl http://192.168.50.120/status      # live alt/az JSON
getent hosts srt-controller.local      # mDNS, needs avahi on the host
```

Then the two that are easy to get wrong.

### Proving the route out actually works

**`/time/status` reporting `"source":"NTP"` does not prove the link routes.** The
firmware carries a numeric NTP fallback behind the pool name, so the clock syncs
whether or not DNS works — it will report `NTP` on a host that cannot resolve
anything. It is a true statement about the clock and a useless one about the
network.

What proves it is a **fresh sync after a reboot**. Reboot the controller (an OTA
upload will do it), then:

```bash
curl http://192.168.50.120/time/status
```

`sync_count` must have **reset to 1** — that confirms you are looking at a real
reboot and not a stale reading — with a small `last_sync_age_s`. That combination
means SNTP packets left the host, reached a time server and came back.

### Proving DNS works

Turn on query logging temporarily:

```bash
echo 'log-queries' | sudo tee -a /etc/NetworkManager/dnsmasq-shared.d/srt.conf
sudo nmcli connection down srt-link && sudo nmcli connection up srt-link
journalctl -u NetworkManager -f | grep -E "query|reply"
```

Reboot the controller and watch for `query[A] pool.ntp.org from 192.168.50.120`
followed by replies. Remove the `log-queries` line afterwards or the journal
fills slowly.

The firmware also reports its own view, which needs no logging at all:

```bash
curl http://192.168.50.120/network
```

| Field | Meaning |
|---|---|
| `dns_after_eth` | resolver right after Ethernet DHCP — should be `192.168.50.1` |
| `dns_after_wifi` | resolver after WiFi startup — **`0.0.0.0` is expected** |
| `lwip_dns0` | the resolver in use now — should hold `192.168.50.1` |
| `dns_restores` | repair count, climbs slowly — **normal, not a fault** |

That `dns_after_wifi` reading is not a bug report: lwIP's resolver list is
process-wide rather than per-interface, and bringing the WiFi AP up clears it,
repeatedly. The firmware caches the address DHCP supplies and puts it back
whenever it finds the list empty (issue #11). The field is sampled *before* the
repair on purpose, so it keeps showing the underlying behaviour. If it ever shows
a real address, the core was fixed upstream.

### Finally

```bash
cd esp32_controller_arduino && ~/.platformio/penv/bin/pio run -e wt32-eth01-ota -t upload
```

OTA is the check that most depends on the firewall being right, so run it once
deliberately rather than discovering it when you need it.

Stellarium: telescope type "External software or remote computer", host
`192.168.50.120`, port `10001`.

---

## 9. Recovery

The controller has two independent safety nets, so a wrong address is not a
bricking risk:

1. **Move the cable back to the site switch.** Because the controller was never
   reconfigured, this restores the previous state exactly.
2. **The WiFi AP stays active** — SSID `SRT_Controller`, web UI at
   `http://192.168.4.1` — regardless of what the Ethernet settings say.
3. Invalid static configuration falls back to DHCP, so a malformed address leaves
   the controller reachable rather than dark.
4. Last resort is the FT232-and-buttons serial flash at the telescope, in
   [WT32_ETH01_MIGRATION.md](WT32_ETH01_MIGRATION.md).

To roll back completely: reach the UI over the AP, confirm Ethernet is on DHCP,
move the cable to the site switch, power-cycle, and put the site address back in
the scheduler config.

---

## 10. Remote access

Neither web UI is reachable from the network, and that is the design: the
controller sits on the private link where only this host can see it, and the
scheduler binds loopback (section 7). So remote access means reaching them
*through* this host rather than exposing them.

### The web UIs: an ssh tunnel

Both are just web pages, so this needs no graphical forwarding at all. From the
remote machine:

```bash
ssh -L 8080:192.168.50.120:80 -L 5000:127.0.0.1:5000 astro@ettus3.astro.gla.ac.uk
```

Then, in a browser on the remote machine:

| | |
|---|---|
| `http://localhost:8080` | controller UI |
| `http://localhost:5000` | scheduler |

This works because the ssh session terminates *on the observatory host*, so the
forwards originate from the one machine that can see both the private link and
the loopback-bound scheduler. Nothing new is exposed: the tunnel is
authenticated by the ssh login, and both services stay unreachable from the
network.

**Forward port 5000 as well, even if you only want the controller.** The
controller's own page fetches the scheduler at `http://127.0.0.1:5000` for its
Scheduler link, Sun Scan and Calibration Day buttons and the firmware update
control. Through a tunnel that resolves to the *remote* machine, so without the
second forward those buttons fail with nothing but a browser console message.

Two mistakes give the same `channel N: open failed` from ssh, and the text after
the colon tells you which:

- `-L 8080:192.168.50.120:8080` — the controller serves on port **80**. The
  remote port is 80; only the local one is 8080.
- Forwarding to the scheduler while it is not running. `connect failed:
  Connection refused` means the far end had nothing listening. Note the
  scheduler does not start at boot by design, so after a reboot there is nothing
  on port 5000 until the launcher has been used.

### Applications: waypipe

For anything that genuinely needs a desktop — Stellarium, VS Code, the receiver
GUI — use waypipe, which is installed on the host:

```bash
waypipe ssh astro@ettus3.astro.gla.ac.uk stellarium
```

The client needs waypipe too, and a Wayland session. **From WSL2 that means
WSLg** (Windows 11, or Windows 10 with a recent WSL): install `waypipe` inside
the WSL distribution and check `echo $WAYLAND_DISPLAY` returns something.
Compare `waypipe --version` at both ends, since mismatched versions can fail to
connect. Expect it to be usable but not fast — waypipe's dmabuf and video
acceleration do not survive the hop into WSLg, so it falls back to software
transfer.

## 11. What this does not do

**Access from other machines on the site network is not set up**, deliberately.
The host NATs *outbound* for the controller; nothing routes inbound. If someone
else needs the controller UI, the host needs a forwarding rule or a reverse
proxy — which partly re-exposes the thing the private link was meant to protect.
Use the ssh tunnel in section 10 instead.

**Nothing here depends on the site interface's address.** The masquerade rule
matches on the link subnet, the ufw rules name interfaces rather than addresses,
and `never-default` keeps the link out of the routing decision. The site
interface can be renumbered, moved to a different socket, or switched between
DHCP and a static address, and the controller will not notice — verified on
2026-08-21 by moving this host from a DHCP 192.168.106.x address to a static
public one, after which the controller's clock still synced on the first reboot.
