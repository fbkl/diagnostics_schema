#!/usr/bin/env python3
"""Tests for the clock checks in pre_setup_diags.

Two modes, both runnable without a ROS master:

    ./test_clock_checks.py            offline regression against captured
                                      `chronyc tracking` output
    ./test_clock_checks.py --live     probe the real machines over NTP

The offline cases exist because the failure that motivated all of this was
INVISIBLE: on 2026-08-26 rpi5-silver-ubuntu was 15 h 10 m behind while every
field a human instinctively reads -- Leap status, RMS offset, Reference ID --
looked perfect. The old check passed it. The two cases marked (REGRESSION)
below are the exact output that got through; if either ever goes back to
passing, the check has been broken again.

2026-08-27: the cluster moved off the orphan mesh to a designated master, so the
stratum threshold moved from ORPHAN_STRATUM = 10 to LOCAL_FALLBACK_STRATUM = 5.
The two "master on `local` fallback" cases below are that change.

Why any of this is checked the way it is: ../doc/clock_checks.md
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import textwrap
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "diagnostics_schema"))

# Stub the things pre_setup_diags imports but these tests do not need, so this
# runs on a bare python3 with no ROS environment sourced.
for _m in ("rospy", "cv2"):
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
for _lvl in ("loginfo", "logwarn", "logerr", "logfatal", "logdebug"):
    setattr(sys.modules["rospy"], _lvl, lambda *a, **k: None)

import pre_setup_diags as p  # noqa: E402

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# (name, tracking output, expected verdict) where expected is
#   None    -> must pass
#   'fail'  -> must be reported at the test's own criticality
#   'warn'  -> must be downgraded to a warning
CASES = [
    ("(REGRESSION) silver 2026-08-26, 15 h slow, slewing", 'fail', """
Reference ID    : C0A80105 (raspberrypi)
Stratum         : 5
Ref time (UTC)  : Wed Aug 26 07:48:21 2026
System time     : 54641.453125000 seconds slow of NTP time
Last offset     : +0.000003709 seconds
RMS offset      : 0.000010565 seconds
Frequency       : 1.103 ppm fast
Residual freq   : -0.015 ppm
Skew            : 0.446 ppm
Root delay      : 0.020670651 seconds
Root dispersion : 0.000388934 seconds
Update interval : 8.0 seconds
Leap status     : Normal
"""),
    ("(REGRESSION) predator, local stratum 1, zero sources", 'fail', """
Reference ID    : 7F7F0101 ()
Stratum         : 1
System time     : 0.000000000 seconds fast of NTP time
Last offset     : +0.000000000 seconds
RMS offset      : 0.000000000 seconds
Root delay      : 0.000000000 seconds
Leap status     : Normal
"""),
    ("orphan island (old topology; must still fail)", 'fail', """
Reference ID    : C0A80103 (rpi5-ubuntu)
Stratum         : 11
System time     : 0.000004000 seconds fast of NTP time
Leap status     : Normal
"""),
    # The two cases the 2026-08-27 move off orphan created. With the old
    # ORPHAN_STRATUM = 10 threshold the first of these PASSED, which is the
    # whole reason the constant became LOCAL_FALLBACK_STRATUM = 5.
    ("master on `local` fallback: client sees stratum 6", 'fail', """
Reference ID    : C0A80105 (raspberrypi)
Stratum         : 6
System time     : 0.000009000 seconds fast of NTP time
Leap status     : Normal
"""),
    ("master on `local` fallback, seen ON the master", 'fail', """
Reference ID    : 7F7F0101 ()
Stratum         : 5
System time     : 0.000000000 seconds fast of NTP time
Leap status     : Normal
"""),
    ("chronyd up but never synchronised", 'fail', """
Reference ID    : 00000000 ()
Stratum         : 0
System time     : 0.000000000 seconds fast of NTP time
Leap status     : Not synchronised
"""),
    ("leap second pending", 'fail', """
Reference ID    : A29FC87B (time.cloudflare.com)
Stratum         : 4
System time     : 0.000100000 seconds fast of NTP time
Leap status     : Insert second
"""),
    ("chronyd not running / garbage", 'fail', ""),
    ("still converging, 12 ms out", 'warn', """
Reference ID    : A29FC87B (time.cloudflare.com)
Stratum         : 4
System time     : 0.012000000 seconds slow of NTP time
Leap status     : Normal
"""),
    ("raspberrypi 2026-08-26, genuinely fine", None, """
Reference ID    : A29FC87B (time.cloudflare.com)
Stratum         : 4
System time     : 0.000271859 seconds fast of NTP time
Last offset     : +0.000179693 seconds
Root delay      : 0.020455506 seconds
Root dispersion : 0.000481425 seconds
Leap status     : Normal
"""),
    ("rpi5-ubuntu 2026-08-26, following the master", None, """
Reference ID    : C0A80105 (raspberrypi)
Stratum         : 5
System time     : 0.000010886 seconds fast of NTP time
Leap status     : Normal
"""),
]

# name -> ip. Keep in step with pre_setup_diags_node.py.
HOSTS = [
    ("raspberrypi",           "192.168.1.5"),
    ("rpi5-ubuntu",           "192.168.1.3"),
    ("rpi5-silver-ubuntu",    "192.168.1.4"),
    ("frkle-Predator-PT515-52", "192.168.2.39"),
]


def _block(text, indent):
    """Reproduce a captured blob verbatim, indented, so it is obvious what the
    check was actually looking at rather than what we think it was."""
    pad = " " * indent
    body = text.strip("\n")
    if not body.strip():
        return pad + f"{DIM}(empty){OFF}"
    return "\n".join(pad + DIM + line + OFF for line in body.splitlines())


def _para(text, indent):
    pad = " " * indent
    return "\n".join(textwrap.wrap(text, width=100,
                                   initial_indent=pad, subsequent_indent=pad))


def offline():
    failures = 0
    width = max(len(n) for n, _, _ in CASES)
    for name, expected, text in CASES:
        verdict = p.evaluate_chrony_tracking(text)
        got = None if verdict is None else ('fail' if verdict[0] else 'warn')
        ok = (got == expected)
        shown = {None: 'pass', 'warn': 'warn', 'fail': 'fail'}[got]
        if ok:
            print(f"  [{GREEN}ok{OFF}] {name:<{width}}  -> {shown}")
            if verdict is not None:
                print(f"        {DIM}{verdict[1][:150]}{OFF}")
            continue

        # A failing case has to hand over everything needed to fix it without
        # reading the source: what was expected, what came out, what the check
        # was looking at, and what it managed to parse out of it.
        failures += 1
        print(f"  [{RED}BROKEN{OFF}] {name}")
        print(f"        {RED}expected {expected or 'pass'}, got {shown}{OFF}")
        print(f"        {DIM}thresholds in force: warn > {p.WARN_OFFSET_S}s, "
              f"fail > {p.FAIL_OFFSET_S}s, stratum > {p.LOCAL_FALLBACK_STRATUM}{OFF}")
        if verdict is None:
            print(f"        {DIM}the check returned None, i.e. \"this clock is fine\".{OFF}")
        else:
            print(f"        {DIM}the check said:{OFF}")
            print(_para(verdict[1], 10))
        print(f"        {DIM}chronyc output it was given:{OFF}")
        print(_block(text, 10))
        fields = p.parse_chrony_tracking(text)
        print(f"        {DIM}fields it managed to parse: "
              f"{fields if fields else '{} <- nothing; the parser is the problem'}{OFF}")
        print()
    return failures


def _icmp_reachable(ip):
    return subprocess.run(["ping", "-W", "1", "-c", "1", ip],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _source_ip_towards(ip):
    """Which address we will arrive from. This is the whole ball game for the
    `allow` lines: the backpack allows 192.168.1.0/24, and a probe that leaves
    over the 192.168.2 interface gets silently dropped, which looks identical
    to chronyd being down."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 123))
        return s.getsockname()[0]
    except OSError as e:
        return f"no route ({e})"
    finally:
        s.close()


def _explain_ntp_failure(name, ip, t):
    """Say which of the three distinguishable things went wrong, rather than
    asserting the same guess every time."""
    src = _source_ip_towards(ip)
    print(f"        {DIM}probe: {t.samples} x SNTP to {ip}:123/udp, 2 s timeout each, "
          f"leaving from {src}{OFF}")
    if t.offset is not None:
        print(f"        {DIM}it DID answer: offset {t.offset * 1000:+.3f} ms, "
              f"rtt {t.rtt * 1000:.1f} ms, stratum {t.stratum}, "
              f"refid 0x{t.refid:08X}{OFF}")
        print(f"        {DIM}so this is a clock verdict, not a reachability problem. "
              f"Compare both ends:\n"
              f"          ssh {name} chronyc tracking\n"
              f"          chronyc tracking{OFF}")
        return
    if not _icmp_reachable(ip):
        print(f"        {RED}no ICMP reply either{OFF} {DIM}-- {ip} is down, or there is "
              f"no route to it from {src}. The NTP result says nothing about its "
              f"clock; fix reachability first.{OFF}")
        print(f"        {DIM}  ip route get {ip}\n          ping {ip}{OFF}")
        return
    print(f"        {YELLOW}pings but does not answer NTP{OFF} {DIM}-- chronyd there is "
          f"up-but-refusing, or not running at all. We arrive from {src}, so it needs "
          f"an `allow` line covering that address:{OFF}")
    print(f"        {DIM}  ssh {name} systemctl is-active chrony\n"
          f"          ssh {name} grep -rn allow /etc/chrony/\n"
          f"          ssh {name} chronyc clients   # do we even show up?{OFF}")
    print(f"        {DIM}(`allow` is the NTP port. `cmdallow` is a different thing and "
          f"will not help here.){OFF}")


def live():
    failures = 0
    me = socket.gethostname()
    print(f"  {DIM}probing from {me}, thresholds warn > {p.WARN_OFFSET_S * 1000:.0f} ms, "
          f"fail > {p.FAIL_OFFSET_S * 1000:.0f} ms{OFF}\n")
    for name, ip in HOSTS:
        if name == me:
            print(f"  [{DIM}skip{OFF}] {name} ({ip}) is this machine; "
                  f"the offset to ourselves is zero by construction")
            continue
        t = p.NtpOffsetToHost(name, ip, criticality=p.REQUIREMENT)
        r = t.run()
        if r == 'OK':
            print(f"  [{GREEN}ok{OFF}] {t.testname()}")
        elif p.OPTIONAL_REQUIREMENT in r:
            print(f"  [{YELLOW}warn{OFF}] {t.testname()}")
            print(_para(r, 8))
        else:
            failures += 1
            print(f"  [{RED}FAIL{OFF}] {name} ({ip})")
            print(_para(r, 8))
            _explain_ntp_failure(name, ip, t)
        print()

    t = p.CheckLocalChrony(criticality=p.REQUIREMENT)
    r = t.run()
    if r == 'OK':
        print(f"  [{GREEN}ok{OFF}] {t.testname()}")
    elif p.OPTIONAL_REQUIREMENT in r:
        # e.g. no chronyc in the container. Not a clock verdict; do not count it
        # as a failure, or the test cries wolf on every un-rebuilt image.
        print(f"  [{YELLOW}warn{OFF}] {t.testname()}")
        print(_para(r, 8))
    else:
        failures += 1
        print(f"  [{RED}FAIL{OFF}] {t.testname()}")
        print(_para(r, 8))
        print(f"        {DIM}raw `chronyc tracking` output it judged:{OFF}")
        print(_block(t.hostreturn or "", 10))
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="also probe the real machines over NTP")
    args = ap.parse_args()

    print("\noffline regression (captured chronyc tracking output)\n")
    n = offline()
    if args.live:
        print("\nlive NTP probes\n")
        n += live()
    print()
    if n:
        print(f"{RED}{n} check(s) did not behave as expected{OFF}\n")
    else:
        print(f"{GREEN}all good{OFF}\n")
    sys.exit(1 if n else 0)
