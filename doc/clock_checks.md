# Clock checks

Why `pre_setup_diags.py` checks the clock the way it does. The code has short
pointers back here; the reasoning lives in this file so the source stays
readable.

## The incident that caused all of this

On 2026-08-26 `rpi5-silver-ubuntu` spent a night **54641 s (15 h 10 m) behind**
while `chronyc tracking` reported:

```
Leap status  : Normal
RMS offset   : 0.000010565 seconds
Reference ID : C0A80105 (raspberrypi)
```

Every field a human instinctively reads looked perfect, and the old check passed
it. Anything recorded that night would have been unusable, and would have stayed
unusable until the clock finally came right, and nothing said so.

The trap is that chrony **subtracts the pending correction from its own
predictions**. So while it is slewing a 15-hour error away, `Last offset` and
`RMS offset` stay at microseconds — the fields that look like error indicators
are exactly the ones that stay green.

## The three things that must all be true

A recording is only correct, and only worth keeping, if all three hold. They are
independent, and the old check tested none of them properly.

### 1. Is the time real? — Reference ID and Stratum

The topology (as of 2026-08-27, after the move off the orphan mesh):

| host | role | config |
|---|---|---|
| `raspberrypi` (192.168.1.5) | designated master | `local stratum 5`, campus + debian pool upstream |
| `rpi5-ubuntu` (192.168.1.3) | client | `server 192.168.1.5 prefer minpoll 2 maxpoll 4` |
| `rpi5-silver-ubuntu` (192.168.1.4) | client | same |
| `frkle-Predator-PT515-52` (192.168.2.39) | visualiser | client of the master, over the 192.168.2 side |

The Ubuntu nodes are **deliberately single-master**: their `pool` lines are
commented out and the campus servers are marked `noselect`, so raspberrypi is
their only selectable source. Do not "fix" a client by giving it its own
upstream — that puts two independent time bases inside one recording. It might
even come out fine; the problem is that if it does not, the discrepancy shows up
as data that looks wrong rather than time that is wrong, and you go and debug the
wrong thing.

Cut the master off from upstream and it does not stop serving. `local` kicks in,
it hands out its own free-running clock at stratum 5, and the whole backpack
agrees on a fabricated time with `Leap status: Normal` throughout. Two different
signals give it away, and the check needs both because it runs on both roles:

- **On the master:** `Reference ID` flips to `7F7F0101`, the local-clock refid.
- **On a client:** `Reference ID` still says `raspberrypi` and looks perfect.
  Only the stratum moves, from `<= 5` to `6`.

That is why the constant is `LOCAL_FALLBACK_STRATUM = 5` and the test is `>`,
not `>=`.

> This replaced `ORPHAN_STRATUM = 10`. Under the old threshold the
> master-on-fallback case **passed**, because a fallen-back master serves
> stratum 5 and its clients serve 6 — nowhere near 10. Both are regression
> cases in `test/test_clock_checks.py` now.

#### The margin is one stratum, and that is thin

A client sits at master+1, so healthy is `<= 5` and fallen back is `6`. The
master's real upstreams are at stratum 2–3 today, so the master is 3–4 and the
clients are 4–5. **If an upstream at stratum 4 ever won selection, healthy
clients would land on 6 and this check would cry wolf.**

Raising `local stratum` on raspberrypi to 8 costs nothing — `local` only
activates while the master is unsynchronised, so the number never affects source
selection — and buys the margin back. Change it in
`/etc/chrony/conf.d/*.conf` on raspberrypi **and** in `LOCAL_FALLBACK_STRATUM`
together.

### 2. Is my clock there yet? — `System time`

`System time: X seconds slow of NTP time` is **not** the measurement residual.
It is the correction chrony still intends to apply. While it is non-zero the
clock is both wrong *and* running at the wrong rate, because chrony is slewing
it.

This is the field that would have caught the 15-hour bug. The old parser
extracted it into a local variable and then never looked at it.

### 3. Do we actually agree? — `NtpOffsetToHost`

Checks 1 and 2 are **self-reports**: each node grades itself against its own
idea of upstream. Neither compares two machines, which is the quantity ROS
timestamps actually depend on.

`NtpOffsetToHost` speaks SNTP directly (UDP 123, four timestamps, so network
delay divides out and the residual is sub-millisecond). It needs no ssh, which
is why it is the right tool for the visualiser specifically.

It also reads the **reference identifier off the wire** (word 3 of the NTP
packet) and fails on `0x7F7F0101`. Stratum alone cannot catch a master on
fallback, because it serves `LOCAL_FALLBACK_STRATUM` — the exact number a
healthy client serves. The refid is unambiguous.

Read this together with check 1, never instead of it: a backpack following a
fallen-back master agrees with itself beautifully. This says *consistent*, not
*correct*.

## The thresholds

```python
WARN_OFFSET_S = 0.005
FAIL_OFFSET_S = 0.050
```

At 400 Hz one IMU sample is 2.5 ms. 5 ms is "two samples out, stop and look".
50 ms is "the AR marker and the limb it is stuck to are in different frames of
the recording".

## When `chronyc` is missing

`CheckLocalChrony` shells out to `chronyc tracking`, and the binary is not in
every image. That surfaced as a bare `FileNotFoundError(2, 'No such file or
directory')` reported at CRITICAL, which killed the entire pre-flight (`do()`
calls `exit(1)` on critical) over a packaging detail — and read like a missing
data file, when nothing had been opened.

It is now reported at `OPTIONAL` with an explicit "nothing was measured" notice.
The fix is `chrony` in the apt list of `ros.Dockerfile`. The container runs
`--network host`, so `chronyc` inside it reaches the host's `chronyd` on
`127.0.0.1:323` and reports the clock the container actually runs on.

## Chrony config gotchas, learned the hard way

**Suffixes are load-bearing.** Debian's `chrony.conf` has:

```
confdir   /etc/chrony/conf.d      → reads *.conf    ONLY
sourcedir /etc/chrony/sources.d   → reads *.sources ONLY
```

Anything else in those directories is **silently ignored** — no warning, nothing
in the logs. This is genuinely useful: renaming `upstream.sources` to
`upstream_sources_old_idea` disables it while keeping the history on a machine
with no git. It is also a good way to fool yourself, so **ask chronyd, never
grep the files**:

```bash
chronyc -n sources     # what is actually loaded
chronyc tracking       # what it is actually following
sudo chronyc reload sources   # after editing a *.sources file -- needs root,
                              # otherwise you get "501 Not authorised"
```

**None of this applies to the predator**, which is on chrony 3.2 (Ubuntu 18.04).
`sourcedir` and `confdir` did not exist until 4.0, so there is no
`/etc/chrony/conf.d` or `/etc/chrony/sources.d` on that machine at all —
everything is in the single `chrony.conf` — and `chronyc reload sources` answers
`Unrecognized command`. Restart the service instead:

```bash
sudo systemctl restart chrony
```

**`allow` is per-subnet and matches the address we arrive from.** The backpack
allows `192.168.1.0/24`; the visualiser arrives from `192.168.2.39`, so it was
silently dropped and looked exactly like chronyd being down. Both directions
need their own `allow` line. `allow` governs the NTP port; `cmdallow` is a
different thing and will not help.

**`makestep 1 -1` everywhere.** The stock `makestep 1 3` is a trap here: clients
poll the master at `minpoll 2` so raspberrypi answers in ~4 s, while its own
upstream needs a minute. The whole three-step budget gets spent agreeing with
the master's not-yet-corrected time before real time ever shows up, and after
that chrony can only slew.

**The real fix is hardware.** A coin cell on the RPi5 RTC header (J5). Then the
Pis never boot on fake-hwclock time and never lock onto a bogus reference in the
first place. It is on the TODO list — `first_paperino.md:989`, still open.

## The configs themselves

Working templates for all four nodes, captured from the live hosts, are in
[chrony/](chrony/) — one per role, plus the four traps that each cost a day
(silent suffix rules, binding the command port to a wifi address, `allow` vs
`cmdallow`, and campus addresses).

## Testing

```bash
./test/test_clock_checks.py          # offline regression, no ROS master needed
./test/test_clock_checks.py --live   # also probe the real machines over NTP
```

The offline cases are captured `chronyc tracking` output. The ones marked
`(REGRESSION)` are the exact text that got through the old check — if either
ever goes back to passing, the check has been broken again.
