# chrony templates for the rig

Captured from the live hosts on 2026-09-04, after the clock work in
[../clock_checks.md](../clock_checks.md). If a node is reimaged, start here
instead of rediscovering all of it.

## Roles

| host | role | files |
|---|---|---|
| `raspberrypi` 192.168.1.5 | **master** — the rig's time root | `master-backpack.conf`, `master-backpack-step.conf`, `master-upstream.sources` |
| `rpi5-ubuntu` 192.168.1.3 | leaf | `leaf-backpack.conf`, `leaf-backpack-step.conf`, `leaf-master.sources`, `leaf-upstream.sources` |
| `rpi5-silver-ubuntu` 192.168.1.4 | leaf | same |
| `frkle-Predator-PT515-52` 192.168.2.39 | visualiser | `visualiser-chrony.conf` |

`.conf` files go in `/etc/chrony/conf.d/`, `.sources` files in
`/etc/chrony/sources.d/`. The visualiser is the exception — see below.

Replace `<THIS_HOST_LAN_IP>` with that machine's own 192.168.x address.

## Applying

```bash
sudo cp leaf-backpack.conf /etc/chrony/conf.d/backpack.conf
sudo $EDITOR /etc/chrony/conf.d/backpack.conf     # fill in <THIS_HOST_LAN_IP>
sudo chronyd -p                                   # parse check, does NOT touch the daemon
sudo chronyd -p -f /path/to/one-file.conf         # or check a single file first
sudo systemctl restart chrony
chronyc tracking && chronyc -n sources
```

## Four traps, all of which cost us a day each

**1. Suffixes are load-bearing.** `confdir` reads `*.conf` only; `sourcedir`
reads `*.sources` only. Anything else in those directories is silently ignored,
with no warning and nothing in the logs. Useful for parking an old file by
renaming it — and a great way to fool yourself. `sudo chronyd -p` prints what is
actually parsed, drop-ins included. **chrony 4+ only** — the predator is on 3.2
and has no parse check; restart it and read `journalctl -u chrony` instead.

**2. Never bind the command port to a wifi address.** chronyd binds once at
startup; if the address is not up yet the bind is skipped silently and the
command port simply is not there. `bindcmdaddress 192.168.2.221` on raspberrypi
did exactly this every time eduroam dropped, and presented as
`506 Cannot talk to daemon` from everywhere. Bind the **wired** address only.
Check with `sudo ss -lunp | grep 323` — the config being right proves nothing.

**3. `allow` and `cmdallow` are different ports.** `allow` is NTP (123),
`cmdallow` is monitoring (323). You need both, and a probe that "pings but does
not answer NTP" is almost always a missing `allow` for the subnet you are
*arriving from*, not a dead daemon.

**4. Do not put a campus address in `bindcmdaddress`.** `rpi5-*` and the
predator also hold `158.37.186.x`. `0.0.0.0` would publish the command port to
the university network.

## Checking a running node

```bash
sudo chronyd -p                      # config as parsed, drop-ins included (chrony 4+)
sudo ss -lunp | grep 323             # what it is REALLY listening on
chronyc tracking                     # this node's own estimate
chronyc -n sources                   # who it is actually following
chronyc -h <ip> tracking             # any node, from anywhere allowed, no ssh
```

## Around a recording

`clock-hold` / `clock-release` in `../../scripts/`. Hold steps the clock once,
verifies it landed, then disarms stepping so nothing jumps mid-take.

All four `.conf` templates here were parse-checked with `chronyd -p -f` against
chrony 4.3 on 2026-09-04.
