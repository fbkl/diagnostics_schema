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
"""
import argparse
import os
import socket
import struct
import sys
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
    ("orphan island, nothing upstream anywhere", 'fail', """
Reference ID    : C0A80103 (rpi5-ubuntu)
Stratum         : 11
System time     : 0.000004000 seconds fast of NTP time
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


def offline():
    failures = 0
    width = max(len(n) for n, _, _ in CASES)
    for name, expected, text in CASES:
        verdict = p.evaluate_chrony_tracking(text)
        got = None if verdict is None else ('fail' if verdict[0] else 'warn')
        ok = (got == expected)
        failures += not ok
        mark = f"{GREEN}ok{OFF}" if ok else f"{RED}BROKEN{OFF}"
        shown = {None: 'pass', 'warn': 'warn', 'fail': 'fail'}[got]
        print(f"  [{mark}] {name:<{width}}  -> {shown}"
              + ("" if ok else f"   {RED}(expected {expected or 'pass'}){OFF}"))
        if verdict is not None:
            print(f"        {DIM}{verdict[1][:150]}{OFF}")
    return failures


def live():
    failures = 0
    me = socket.gethostname()
    print(f"  {DIM}probing from {me}{OFF}\n")
    for name, ip in HOSTS:
        t = p.NtpOffsetToHost(name, ip, criticality=p.REQUIREMENT)
        r = t.run()
        if r == 'OK':
            print(f"  [{GREEN}ok{OFF}] {t.testname()}")
        elif p.OPTIONAL_REQUIREMENT in r:
            print(f"  [{YELLOW}warn{OFF}] {t.testname()}\n        {r}")
        else:
            failures += 1
            print(f"  [{RED}FAIL{OFF}] {name} ({ip})\n        {r}")
            print(f"        {DIM}pings but no NTP reply => chronyd there is refusing us;"
                  f" it needs an `allow` line covering our subnet.{OFF}")
    print()
    t = p.CheckLocalChrony(criticality=p.REQUIREMENT)
    r = t.run()
    if r == 'OK':
        print(f"  [{GREEN}ok{OFF}] {t.testname()}")
    else:
        failures += 1
        print(f"  [{RED}FAIL{OFF}] {t.testname()}\n        {r}")
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
