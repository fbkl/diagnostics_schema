#!/usr/bin/env python3
"""Offline tests for the AR visibility verdict. No ROS master needed.

    ./test_ar_visibility.py

The case that motivated this: the room marker was in view by eye but partly
clipped, so alvar detected nothing, calibration silently did not happen, and
the stale timestamps got misread as a chrony problem. `clipped_marker` and
`clock_skew_not_marker` below are the two halves of that confusion, and they
must keep producing DIFFERENT messages.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ar_visibility import evaluate_ar_visibility, OK, WARN, ERROR  # noqa: E402

NOW = 1000.0
CASES = {
    # name: (args after `now`, expected level, substring the message must contain)
    "healthy":               ((NOW - 0.1, NOW - 0.1, NOW - 0.15), OK, "can calibrate"),
    "nothing_running":       ((None, None, None), ERROR, "down"),
    "stream_died":           ((NOW - 5.0, NOW - 5.0, NOW - 5.0), ERROR, "down"),
    "never_seen":            ((NOW - 0.1, None, None), WARN, "never seen"),
    "clipped_marker":        ((NOW - 0.1, NOW - 30.0, NOW - 30.0), WARN, "not visible"),
    "clock_skew_not_marker": ((NOW - 0.1, NOW - 0.1, NOW - 3.1), WARN, "CLOCK"),
    "camera_clock_ahead":    ((NOW - 0.1, NOW - 0.1, NOW + 2.0), WARN, "CLOCK"),
    # a lost marker must be reported as lost, not as a clock problem, even with skew
    "lost_beats_skew":       ((NOW - 0.1, NOW - 30.0, NOW - 40.0), WARN, "not visible"),
}


def main():
    failed = 0
    for name, (args, level, needle) in CASES.items():
        got_level, msg, _lag = evaluate_ar_visibility(NOW, *args)
        ok = got_level == level and needle in msg
        failed += not ok
        print("%s  %-22s level=%d  %s" % ("PASS" if ok else "FAIL", name, got_level, msg))
    print("%d/%d passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
