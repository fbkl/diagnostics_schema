#!/usr/bin/env python3

import rospy
from abc import ABC
from abc import abstractmethod
import subprocess
import os
import ipaddress
import shlex
import socket
import struct
import time

from colorama import init as colorama_init
from colorama import Fore
from colorama import Style

colorama_init()

import cv2

CRITICAL_REQUIREMENT = 'Critical: '
REQUIREMENT          = 'Failed: '
OPTIONAL_REQUIREMENT = 'Optional: '



class ATest(ABC):
    def __init__(self, criticality  = REQUIREMENT, tips=[]):
        super().__init__()
        self.criticality = criticality
        self.tips= []
        self.hostreturn = None
        if tips:
            self.tips = tips


    @abstractmethod
    def run(self):
        pass
    
    @abstractmethod
    def troubleshootingmsg(self):
        return []

    @abstractmethod
    def testname(self):
        return ""

class X11Host(ATest):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = None
    def run(self):
        try:
            self.display = os.getenv("DISPLAY")
            if self.display:
                return 'OK'
            else:
                return self.criticality

        except Exception as e:
            return self.criticality+repr(e)#+self.hostreturn.stderr

    def troubleshootingmsg(self):
        return ["The DISPLAY environment variable is empty!! this means that the docker was started in an ssh or other type of session where the DISPLAY variable was not defined. ", "Try reruning the prediags in a session connected with:\n\tssh user@machine -X\n"]

    def testname(self):
        return "Checks if X11 forwarding is running."


class CheckRemote(ATest):
    def __init__(self, username, hostname, command, **kwargs):
        super().__init__(**kwargs)
        self.user = username
        self.host = hostname
        self.command = command

    def run(self):
        # NOTE: the subprocess timeout must be LONGER than ssh's ConnectTimeout,
        # otherwise a host that is merely slow to answer gets killed by python
        # and reported as a TimeoutExpired instead of the real ssh error.
        #
        # shlex.quote is NOT decoration. ssh does not preserve argv boundaries:
        # it joins everything after the host into one string with spaces and the
        # REMOTE LOGIN SHELL re-parses it. So ["bash", "-c", "a && b || c"] went
        # over the wire as
        #     bash -c a && b || c
        # and the remote shell handed bash only "a", ran `b` and `c` itself, and
        # every $(...) inside got expanded in the wrong shell at the wrong time.
        # A plain command like ["chronyc", "tracking"] survives that unharmed,
        # which is exactly why it went unnoticed until something had an operator
        # in it. Quoting each element makes the remote shell hand the script to
        # bash as a single word, the way the list form implies.
        try:
            remote = " ".join(shlex.quote(c) for c in self.command)
            self.hostreturn = subprocess.check_output(["ssh","-q", "-o","BatchMode=yes",
                "-o","ConnectTimeout=3",
                f"{self.user}@{self.host}", remote], timeout=6).decode()
        except Exception as e:
            return self.criticality+repr(e)#+self.hostreturn.stderr
        # Subclasses do `err = super().run(); if err: return err` -- returning
        # None here is what says "the command ran, go parse self.hostreturn".
        return None


    def troubleshootingmsg(self):
        return ["Is SSH running on the host?", "Are the keys properly setup?",f"Is {self.command} a valid command?"]

    def testname(self):
        return f"Checks to see if {self.command} runs in the host [{Style.BRIGHT}{self.host}{Style.NORMAL}]."


class CheckRemoteRealsense(CheckRemote):
    def __init__(self, username, hostname, **kwargs):
        super().__init__(username, hostname, ["lsusb"],**kwargs)
        self.realsense_cameras = []

    def run(self):
        try:
            err = super().run()
            if err:
                return err
            for line in self.hostreturn.splitlines():
                if 'RealSense' in line:
                    self.realsense_cameras.append(line)
            if not self.realsense_cameras:
                ret = self.criticality+ "No RealSense camera found!"
                return str(ret)

            return 'OK'
        except Exception as e:
            return self.criticality+repr(e)#+self.hostreturn.stderr


    def troubleshootingmsg(self):
        return [*super().troubleshootingmsg(), *["Is the realSense Camera plugged in?"]]

    def testname(self):
        msg = ""
        if self.realsense_cameras:
            for camera in self.realsense_cameras:
                msg += f"Camera(s)[{Style.BRIGHT}{camera}{Style.NORMAL}] found running on [{Style.BRIGHT}{self.host}{Style.NORMAL}]."
        else:
            msg = f"Looking for RealSense cameras in [{Style.BRIGHT}{self.host}{Style.NORMAL}]." 
        return msg

# ---------------------------------------------------------------------------
# Clock checks.
#
# 2026-08-26: rpi5-silver-ubuntu spent a night 54641 s (15 h 10 m) behind while
# `chronyc tracking` reported "Leap status: Normal", an RMS offset of 10 us and
# a perfectly valid Reference ID -- so the old version of this test passed it.
#
# Three INDEPENDENT things have to be true before a recording is worth keeping,
# and the old test checked none of them properly:
#
#   1. IS THE TIME REAL?      -> Reference ID and Stratum.
#      2026-08-27: the orphan mesh is GONE. The topology now is a designated
#      master -- raspberrypi (192.168.1.5) -- carrying `local stratum 5`, with
#      the two Ubuntu Pis as plain clients of it (`server 192.168.1.5 prefer
#      minpoll 2 maxpoll 4`, their own pool lines commented out and the campus
#      servers marked `noselect`, so the master is their ONLY selectable
#      source). Cut the master off from upstream and it does not stop serving:
#      `local` kicks in, it hands out its own free-running clock at stratum 5,
#      and the whole backpack agrees on a fabricated time. Leap status stays
#      "Normal" throughout. Two different things give it away, and you need
#      both, because the check runs on both roles:
#        - ON THE MASTER: Reference ID flips to 7F7F0101, the local-clock refid.
#        - ON A CLIENT:   Reference ID still says raspberrypi and looks perfect.
#                         Only the stratum moves, from <= 5 to 6.
#      Hence LOCAL_FALLBACK_STRATUM below, and hence `>` and not `>=`.
#
#   2. IS MY CLOCK THERE YET? -> "System time: X seconds slow of NTP time".
#      This is NOT the measurement residual. It is the correction chrony still
#      intends to apply. While it is non-zero the clock is both wrong AND
#      running at the wrong rate, because chrony is slewing it. "Last offset"
#      and "RMS offset" stay at microseconds the whole time, because chrony
#      subtracts the pending correction from its own predictions -- so the
#      fields that look like error indicators are exactly the ones that stay
#      green. "System time" is the field that would have caught it, and the old
#      parser extracted it into a local variable and then never looked at it.
#
#   3. DO WE ACTUALLY AGREE?  -> NtpOffsetToHost, below.
#      Neither 1 nor 2 compares two machines. They are both self-reports.
# ---------------------------------------------------------------------------

# Must match `local stratum N` in /etc/chrony/conf.d/*.conf on raspberrypi, the
# designated master. If you change it there, change it here.
#
# A client sits at master+1, so healthy is <= LOCAL_FALLBACK_STRATUM and fallen
# back is LOCAL_FALLBACK_STRATUM + 1. That margin is exactly one stratum: the
# master's real upstreams are at 2-3 today (so the master is 3-4 and the clients
# are 4-5). If an upstream at stratum 4 ever won selection, healthy clients would
# land on 6 and this check would cry wolf. Raising `local stratum` on raspberrypi
# to 8 costs nothing -- `local` only activates while the master is unsynchronised,
# so the number never affects source selection -- and buys the margin back.
# Change both places together.
LOCAL_FALLBACK_STRATUM = 5

# At 400 Hz one IMU sample is 2.5 ms. 5 ms is "two samples out, stop and look";
# 50 ms is "the AR marker and the limb it is stuck to are in different frames
# of the recording".
WARN_OFFSET_S = 0.005
FAIL_OFFSET_S = 0.050


def parse_chrony_tracking(text):
    fields = {}
    for line in text.splitlines():
        if ':' in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def _seconds(value):
    """'0.020670651 seconds slow of NTP time' -> 0.020670651"""
    return float(value.split()[0])


def chrony_summary(text):
    """One-line human summary, for the passing case."""
    f = parse_chrony_tracking(text)
    ref = f.get("Reference ID", "?")
    try:
        off = abs(_seconds(f.get("System time", "")))
        off_str = f"{off * 1000:.2f} ms to go"
    except (ValueError, IndexError):
        off_str = "offset unreadable"
    return f"stratum {f.get('Stratum', '?')} via {ref}, {off_str}"


def evaluate_chrony_tracking(text, warn_offset=WARN_OFFSET_S,
                             fail_offset=FAIL_OFFSET_S,
                             local_stratum=LOCAL_FALLBACK_STRATUM):
    """Return (fatal, message) describing a problem, or None if the clock is fine.

    fatal=True  -> report at the test's own criticality (Critical/Failed)
    fatal=False -> downgrade to a warning; the clock is converging, just wait
    """
    f = parse_chrony_tracking(text)
    if not f:
        return (True, "chronyc tracking printed nothing parseable. Is chronyd actually running?")

    leap = f.get("Leap status", "")
    refid = f.get("Reference ID", "")

    if "Not synchronised" in leap or refid.startswith("0000"):
        return (True, f"chronyd is up but has never synchronised (Leap status: {leap!r}, Reference ID: {refid or 'none'}).")

    if leap != "Normal":
        return (True, f"Leap status is {leap!r}. A leap second is pending, so timestamps across it will not be monotonic. Do not record now.")

    # 127.127.x.x is the classic local-clock reference id. A node serving its own
    # undisciplined clock reports this, usually at a flatteringly low stratum.
    if refid.upper().startswith("7F7F"):
        return (True, f"this host is its OWN time reference (refid {refid}): it has fallen back to its `local stratum` line and is handing out a free-running clock as if it were authoritative. On raspberrypi that means the master lost every upstream at once -- check the wifi and `chronyc -n sources` there. On any other machine it means a `local` line is in a config where it does not belong.")

    try:
        stratum = int(f.get("Stratum", ""))
    except ValueError:
        return (True, "could not read Stratum out of chronyc tracking.")

    if stratum > local_stratum:
        return (True, f"stratum {stratum} is deeper than the master's own fallback stratum ({local_stratum}), so we are following raspberrypi while raspberrypi is following nothing: it lost upstream, fell back to `local`, and is serving its own free-running clock. Reference ID here still names the master and looks perfectly healthy. Every node in the backpack will agree with this time and all of it is invented.")

    raw = f.get("System time", "")
    try:
        sys_time = _seconds(raw)
    except (ValueError, IndexError):
        return (True, "could not read 'System time' out of chronyc tracking.")
    direction = "slow" if "slow" in raw else "fast"

    if sys_time > fail_offset:
        return (True, f"the system clock is {sys_time:.6f} s {direction} of NTP time and chrony is SLEWING it, not stepping. At the default rate that takes hours to days, and while it corrects, the clock also runs at the wrong RATE. Leap status and RMS offset stay perfectly healthy the whole time. Nothing recorded now is usable.")
    if sys_time > warn_offset:
        return (False, f"the system clock is {sys_time:.6f} s {direction} and still converging. Give it a moment before you record.")

    return None


CLOCK_TROUBLESHOOTING = [
    "Fix it now, with ROS DOWN (a step mid-run corrupts every timestamp in the recording):\n\tssh <host> sudo chronyc makestep",
    "See who the host is really listening to:\n\tssh <host> chronyc sources -v\n\tssh <host> chronyc tracking",
    "If it keeps coming back, the stock 'makestep 1 3' is the cause. The clients poll the master at minpoll 2, so raspberrypi answers in ~4 s while its own upstream needs a minute: the whole three-step budget is spent agreeing with the master's not-yet-corrected time before real time ever shows up, and after that chrony can only slew. Every node should already carry 'makestep 1 -1'; if you find one that does not, put this in /etc/chrony/conf.d/backpack-step.conf there:\n\tmakestep 1 -1\n\tmaxchange 100 10 3",
    "A client above stratum 5 means raspberrypi is the one that is orphaned, not the client. Go look at the master, not at the machine that reported this:\n\tssh raspberrypi chronyc -n sources\nDo NOT 'fix' a client by giving it its own upstream: the Ubuntu nodes are deliberately single-master (pool lines commented out, campus servers 'noselect'), and handing one its own source puts two independent time bases inside one recording.",
    "The actual fix is hardware: a coin cell on the RPi5 RTC header (J5). Then the Pis never boot on fake-hwclock time and never lock onto a bogus reference in the first place.",
]

# Returned instead of a verdict when the client binary is missing. Nothing was
# opened and nothing was measured -- worth saying out loud, because the bare
# FileNotFoundError this replaces read like a missing data file.
NO_CHRONYC = (
    "could not run the check: there is no `chronyc` binary in this container, so "
    "NOTHING WAS MEASURED. This is not a statement about the clock. The container "
    "runs --network host and shares the host kernel clock, so a chronyc in here "
    "would reach the host's chronyd on 127.0.0.1:323 and report the host's clock, "
    "which is exactly the thing we want to check. Add `chrony` to the apt list in "
    "ros.Dockerfile and rebuild. Until then this machine's own clock is only "
    "covered indirectly, by the NtpOffsetToHost comparisons."
)


class CheckRemoteChrony(CheckRemote):
    def __init__(self, username, hostname,
                 warn_offset=WARN_OFFSET_S, fail_offset=FAIL_OFFSET_S,
                 local_stratum=LOCAL_FALLBACK_STRATUM, **kwargs):
        super().__init__(username, hostname, ["chronyc", "tracking"], **kwargs)
        self.warn_offset = warn_offset
        self.fail_offset = fail_offset
        self.local_stratum = local_stratum
        self.summary = ""

    def run(self):
        try:
            err = super().run()
            if err:
                return err
            self.summary = chrony_summary(self.hostreturn)
            verdict = evaluate_chrony_tracking(self.hostreturn, self.warn_offset,
                                               self.fail_offset, self.local_stratum)
            if verdict is None:
                return 'OK'
            fatal, msg = verdict
            return (self.criticality if fatal else OPTIONAL_REQUIREMENT) + msg
        except Exception as e:
            return self.criticality + repr(e)

    def troubleshootingmsg(self):
        return [*super().troubleshootingmsg(), *CLOCK_TROUBLESHOOTING]

    def testname(self):
        host = f"{Style.BRIGHT}{self.host}{Style.NORMAL}"
        if self.summary:
            return f"Clock on [{host}]: {self.summary}."
        return f"Checks that the clock on [{host}] is synchronised, disciplined and not orphaned."


class CheckLocalChrony(ATest):
    """The same three checks, for the machine this node is running on.

    Worth having separately from CheckRemoteChrony even when the local host is
    also in the remote list: this one cannot be confused by an ssh problem, and
    it still works when the network is the thing that is broken.
    """

    def __init__(self, warn_offset=WARN_OFFSET_S, fail_offset=FAIL_OFFSET_S,
                 local_stratum=LOCAL_FALLBACK_STRATUM, **kwargs):
        super().__init__(**kwargs)
        self.warn_offset = warn_offset
        self.fail_offset = fail_offset
        self.local_stratum = local_stratum
        self.host = socket.gethostname()
        self.summary = ""

    def run(self):
        try:
            self.hostreturn = subprocess.check_output(["chronyc", "tracking"],
                                                      timeout=6).decode()
        except FileNotFoundError:
            # Deliberately NOT self.criticality. This test is registered as
            # CRITICAL and do() exits(1) on critical, so reporting a missing
            # client binary at the test's own criticality kills the whole
            # pre-flight over a packaging detail instead of over a clock.
            self.summary = "chronyc is not installed in this container"
            return OPTIONAL_REQUIREMENT + NO_CHRONYC
        except Exception as e:
            return self.criticality + repr(e)
        try:
            self.summary = chrony_summary(self.hostreturn)
            verdict = evaluate_chrony_tracking(self.hostreturn, self.warn_offset,
                                               self.fail_offset, self.local_stratum)
            if verdict is None:
                return 'OK'
            fatal, msg = verdict
            return (self.criticality if fatal else OPTIONAL_REQUIREMENT) + msg
        except Exception as e:
            return self.criticality + repr(e)

    def troubleshootingmsg(self):
        return ["Is chronyd running here?\n\tsystemctl status chrony",
                "Careful inside a container: it shares the HOST kernel clock, so this reports the host's clock, not something the container owns.",
                "'chronyc: command not found' is a missing package, not a missing file and not a broken clock:\n\tapt install chrony   (or add it to the apt list in ros.Dockerfile and rebuild)",
                *CLOCK_TROUBLESHOOTING]

    def testname(self):
        host = f"{Style.BRIGHT}{self.host}{Style.NORMAL}"
        if self.summary:
            return f"Clock on this machine [{host}]: {self.summary}."
        return f"Checks that the clock on this machine [{host}] is synchronised and disciplined."


class NtpOffsetToHost(ATest):
    """Measure how far THIS clock is from THAT clock, over NTP.

    This is the only check here that compares two machines. `chronyc tracking`
    is a self-report: each node grades itself against its own idea of upstream.
    Two nodes can both look immaculate and still disagree with each other.

    Doing it over NTP rather than `ssh <host> date` matters. An NTP exchange
    carries four timestamps, so the network delay divides out and the residual
    is sub-millisecond. ssh round-trip latency is 10-100x the quantity we are
    trying to measure, so `ssh date` cannot see anything smaller than itself.

    Read this together with the stratum check, not instead of it: a backpack
    following a master that has fallen back to `local` agrees with itself
    beautifully. This says "consistent", not "correct".
    """

    NTP_EPOCH = 2208988800  # seconds between 1900-01-01 and 1970-01-01
    LOCAL_REFID = 0x7F7F0101  # what chronyd puts on the wire while on `local`

    def __init__(self, hostname, hostip=None, warn_offset=WARN_OFFSET_S,
                 fail_offset=FAIL_OFFSET_S, samples=3, **kwargs):
        super().__init__(**kwargs)
        if hostip is not None:
            ipaddress.ip_address(hostip)
        self.host = hostname
        self.hostip = hostip or hostname
        self.warn_offset = warn_offset
        self.fail_offset = fail_offset
        self.samples = samples
        self.offset = None
        self.rtt = None
        self.stratum = None
        self.refid = None

    def _probe(self):
        """One SNTP exchange. Returns (offset, round_trip_delay, stratum, refid)."""
        packet = b'\x1b' + 47 * b'\0'          # LI=0 VN=3 Mode=3 (client)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            t1 = time.time()
            s.sendto(packet, (self.hostip, 123))
            data, _ = s.recvfrom(1024)
            t4 = time.time()
        finally:
            s.close()
        if len(data) < 48:
            raise ValueError("short NTP reply")
        u = struct.unpack('!12I', data[:48])
        t2 = u[8] + u[9] / 2**32 - self.NTP_EPOCH     # server receive
        t3 = u[10] + u[11] / 2**32 - self.NTP_EPOCH   # server transmit
        offset = ((t2 - t1) + (t3 - t4)) / 2
        delay = (t4 - t1) - (t3 - t2)
        stratum = (u[0] >> 16) & 0xff
        # u[3] is the reference identifier, and we need it because stratum alone
        # can no longer tell us whether the MASTER is inventing time: on `local`
        # fallback raspberrypi serves LOCAL_FALLBACK_STRATUM, which is the exact
        # number a perfectly healthy client serves. The refid is unambiguous.
        refid = u[3]
        return offset, delay, stratum, refid

    def run(self):
        try:
            # Keep the sample with the smallest round trip. It is the one least
            # distorted by a packet that sat in a queue on the wifi.
            best = None
            for _ in range(self.samples):
                try:
                    r = self._probe()
                except socket.timeout:
                    continue
                if best is None or r[1] < best[1]:
                    best = r
            if best is None:
                return self.criticality + f"{self.host} did not answer NTP on port 123."
            self.offset, self.rtt, self.stratum, self.refid = best

            if self.stratum == 0:
                return self.criticality + f"{self.host} answers NTP but reports stratum 0, i.e. unsynchronised or a kiss-o'-death. It is not serving real time."
            if self.refid == self.LOCAL_REFID:
                return self.criticality + f"{self.host} answers NTP, but its reference id on the wire is 7F7F0101 -- the local-clock refid. It has fallen back to its `local stratum` line and is serving its own free-running clock at a respectable-looking stratum {self.stratum}. Everything downstream of it is following an invention."
            if self.stratum > LOCAL_FALLBACK_STRATUM:
                return self.criticality + f"{self.host} answers NTP at stratum {self.stratum}, deeper than the master's own fallback stratum ({LOCAL_FALLBACK_STRATUM}), so it is sitting downstream of a raspberrypi that has itself lost upstream."
            if abs(self.offset) > self.fail_offset:
                return self.criticality + f"our clock and {self.host}'s are {abs(self.offset) * 1000:.1f} ms apart (limit {self.fail_offset * 1000:.0f} ms). Stamped messages crossing between these two machines will not line up."
            if abs(self.offset) > self.warn_offset:
                return OPTIONAL_REQUIREMENT + f"our clock and {self.host}'s are {abs(self.offset) * 1000:.1f} ms apart."
            return 'OK'
        except Exception as e:
            return self.criticality + repr(e)

    def troubleshootingmsg(self):
        return [f"Can we even reach it?\n\tping {self.hostip}",
                f"chronyd on {self.host} has to accept us as a client: it needs an 'allow' line covering OUR subnet. Check /etc/chrony/conf.d/ on {self.host}. Note 'allow' is for the NTP port and is not the same thing as 'cmdallow'.",
                "If both machines individually claim to be synchronised but disagree with each other, they are synchronised to different things. Compare 'Reference ID' in chronyc tracking on both.",
                *CLOCK_TROUBLESHOOTING]

    def testname(self):
        host = f"{Style.BRIGHT}{self.host}{Style.NORMAL}"
        if self.offset is not None:
            return f"Clock offset to [{host}]: {self.offset * 1000:+.2f} ms (rtt {self.rtt * 1000:.1f} ms, stratum {self.stratum})."
        return f"Measures the clock offset between this machine and [{host}] over NTP."


class CheckRemoteMount(CheckRemote):
    # Three distinguishable outcomes, because "not mounted, or mounted but
    # empty" sends you to look at the wrong half of the problem half the time.
    #
    # The `ls` is the real test, not `mountpoint`. /mnt/osim is an
    # x-systemd.automount entry, so autofs is mounted there permanently and
    # `mountpoint -q` answers yes whether or not the NFS underneath it is alive.
    # Touching the directory is what triggers the automount and proves the
    # server actually answers, which is the thing we care about.
    PROBE = ("if ! mountpoint -q {mp}; then echo NOT_A_MOUNTPOINT; "
             "elif [ -z \"$(ls -A {mp} 2>/dev/null)\" ]; then echo MOUNTED_BUT_EMPTY; "
             "else echo MOUNTED_AND_POPULATED; fi")

    def __init__(self, username, hostname, mountpoint, nfs_source="frkle-Predator-PT515-52:/home/frkle/shared/osim", **kwargs):
        command = ["bash", "-c", self.PROBE.format(mp=mountpoint)]
        super().__init__(username, hostname, command, **kwargs)
        self.mountpoint = mountpoint
        self.nfs_source = nfs_source

    def run(self):
        try:
            err = super().run()
            if err:
                return err
            out = (self.hostreturn or "").strip()
            if "MOUNTED_AND_POPULATED" in out:
                return 'OK'
            if "MOUNTED_BUT_EMPTY" in out:
                return self.criticality + f"{self.mountpoint} on {self.host} IS a mountpoint but reads back empty. The automount trigger is there and the NFS server behind it is not answering, or is exporting an empty directory."
            if "NOT_A_MOUNTPOINT" in out:
                return self.criticality + f"nothing is mounted at {self.mountpoint} on {self.host} -- not even the autofs trigger, so the fstab entry is missing or systemd never picked it up."
            return self.criticality + f"the mount probe on {self.host} answered {out!r}, which is not one of the three things it can say. That is a bug in the probe, not a verdict about {self.mountpoint}."
        except Exception as e:
            return self.criticality+repr(e)#+self.hostreturn.stderr

    def troubleshootingmsg(self):
        return [*super().troubleshootingmsg(),
            "Is the NFS server running on this PC? Check with:\n\tsudo systemctl status nfs-kernel-server\n\tsudo exportfs -v",
            f"Does {self.host} have a persistent /etc/fstab entry for this mount? Sometimes it was only mounted manually and doesn't survive a reboot. Check with:\n\tssh {self.user}@{self.host} cat /etc/fstab",
            f"To mount it right now on {self.host}, run:\n\tsudo mount -t nfs {self.nfs_source} {self.mountpoint}",
            f"To make it survive reboots, add this line to /etc/fstab on {self.host}:\n\t{self.nfs_source} {self.mountpoint} nfs ro,_netdev,auto 0 0",
            f"If it's mounted but still empty, the docker stack on {self.host} was probably started before the NFS mount landed — a bind mount only captures what's at {self.mountpoint} at container start. Restart the stack on {self.host} so it picks up the content."]

    def testname(self):
        return f"Checks to see if [{Style.BRIGHT}{self.mountpoint}{Style.NORMAL}] (NFS share) is mounted and populated on the host [{Style.BRIGHT}{self.host}{Style.NORMAL}]."


class CheckOwnHost(ATest):
    def __init__(self, own_ip="192.168.1.100", criticality = 'Critical: '):
        super().__init__(criticality)
        self.is_hotspot = False
        try:
            if os.environ['USE_HOTSPOT'] == "true":
                own_ip = "192.168.1.1"
                self.is_hotspot = True
        except:
            rospy.logwarn("USE_HOTSPOT variable not set. I will assume I am not a hotspot!")
        ipaddress.ip_address(own_ip)
        self.own_ip = own_ip

    def run(self):
        self.hostreturn = subprocess.run(["hostname","-I"], capture_output=True, text = True)
        try:
            self.hostreturn.check_returncode()
            if self.own_ip in self.hostreturn.stdout:
                return 'OK'
            else:
                return self.criticality+self.hostreturn.stderr
        except:
            return self.criticality+self.hostreturn.stderr


    def troubleshootingmsg(self):
        if self.is_hotspot:
            return [f"??? This pc is set as a hotspot, however its own ip is not set to 192.168.1.1, which is weird. If you know what you are doing and you changed the network configurations, please fix this node {__file__} as well! "]
        return [f"Check if this pc's cable is connected to the router via ethernet cable", "Make sure that the cable is connected and both ends and is in the correct port on the router.", "Check if router is on.", "Check if router's power supply is connected.","Check if this system is somehow running on AP mode"]

    def testname(self):
        return f"Check if this pc has ip [{Style.BRIGHT}{self.own_ip}{Style.NORMAL}]."


class PingHost(ATest):
    def __init__(self, hostname, hostip, **kwargs):
        super().__init__(**kwargs)
        ipaddress.ip_address(hostip)
        self.host = hostname
        self.hostip = hostip

    def run(self):
        self.hostreturn = subprocess.run(["ping","-W","1","-c","1",self.hostip], capture_output=True, text = True)
        try:
            self.hostreturn.check_returncode()
            return 'OK'
        except:
            return self.criticality+self.hostreturn.stderr


    def troubleshootingmsg(self):
        return [f"Make sure host [{self.host}] is on", f"Make sure that the IP address of the host [{self.host}] is set to {self.hostip}", f"Make sure that {self.host} is connected to the network:\n\t-if {self.host} is using wifi connection, check if it is set to the correct AP,\n\t-if {self.host} is linked via ethernet cable, make sure that the cable is connected and both ends and is in the correct port on the router."]

    def testname(self):
        return f"Check if host [{Style.BRIGHT}{self.host}{Style.NORMAL}] with ip [{Style.BRIGHT}{self.hostip}{Style.NORMAL}] is alive"


class Sound(ATest):
    def __init__(self, soundfilename, **kwargs):
        super().__init__(**kwargs)
        if not os.path.exists(soundfilename):
            raise Exception("invalid sound filename")
        self.soundfile = soundfilename

    def run(self):
        self.soundreturn = subprocess.run(["paplay",self.soundfile], capture_output=True,text = True)
        try:
            self.soundreturn.check_returncode()
            return 'OK'
        except:
            return self.criticality+self.soundreturn.stderr

    def troubleshootingmsg(self):
        return ["Close anything that could be using the sound card (like Firefox, Vlc, Totem [debian/ubuntu just calls it Videos now], ffmpeg, etc.)","You probably want to check the sharing options of the docker, there is likely some mistake there.","Maybe the file doesn't exist?"]
    def testname(self):
        return f"Check if alsa can play sound file: {self.soundfile}"

class Video(ATest):
    def __init__(self, videodevname, **kwargs):
        super().__init__(**kwargs)
        if "/dev/video" not in videodevname:
            raise Exception(f"invalid device name {videodevname}")
        self.videodev = videodevname
        try:
            self.dev = int(self.videodev.split("/dev/video")[1])
        except:
            raise Exception("invalid device name {videodevname}")

    def run(self):
        if not os.path.exists(self.videodev):
            return f"{self.criticality} device does not exist {self.videodev}"
        cam = cv2.VideoCapture(self.dev)
        try:
            ret, frame = cam.read()

            if not ret:
                return f'{self.criticality} Could not read from capture device {self.dev}'
            cam.release()

            return 'OK'
        except:
                return f'{self.criticality} Could not read from capture device {self.dev}'

    def troubleshootingmsg(self):
        return ["You need to connect the external USB camera before starting the docker.","Maybe you don't want the camera?"]
    def testname(self):
        return f"Check if opencv can read from device: [{Style.BRIGHT}{self.videodev}{Style.NORMAL}]"

def do(tests):
    fail_bin = []
    for test in tests:
        ret = test.run() 
        if REQUIREMENT in ret:
            rospy.logerr(f"\t[{Style.BRIGHT}{Style.NORMAL}] {test.testname()}")
            fail_bin.append(test.testname())
            msg =" ".join(ret.split(REQUIREMENT)[1:]) 
            if msg:
                rospy.logerr("\t"+msg)
            for sugg in test.troubleshootingmsg():
                rospy.logwarn(f"\t\t[{Style.BRIGHT}{Style.NORMAL}] {sugg}")
        elif OPTIONAL_REQUIREMENT in ret:
            rospy.logwarn(f"\t[{Style.BRIGHT}{Style.NORMAL}] {test.testname()}")
            fail_bin.append(test.testname())
            # was split(REQUIREMENT), which never matched here, so the whole
            # string including the "Optional: " prefix got printed as an error.
            msg =" ".join(ret.split(OPTIONAL_REQUIREMENT)[1:]) 
            if msg:
                rospy.logerr("\t"+msg)
            for sugg in test.troubleshootingmsg():
                rospy.logwarn(f"\t\t[{Style.BRIGHT}{Style.NORMAL}] {sugg}")
        elif CRITICAL_REQUIREMENT in ret:
            rospy.logfatal(f"\t{Style.BRIGHT}{Style.NORMAL} {test.testname()}")
            msg =" ".join(ret.split(CRITICAL_REQUIREMENT)[1:]) 
            if msg:
                rospy.logerr("\t"+msg)
            for sugg in test.troubleshootingmsg():
                rospy.logwarn(f"\t\t[{Style.BRIGHT}{Style.NORMAL}] {sugg}")
            exit(1)
        else:
            rospy.loginfo(f"\t[{Style.BRIGHT}{Fore.GREEN}{Fore.WHITE}{Style.NORMAL}] "+test.testname())
            if len(test.tips)>0:
                for tip in test.tips:
                    rospy.loginfo(f"\t\t[{Style.BRIGHT}{Style.NORMAL}] {tip}")
    if len(fail_bin) > 0:
        rospy.logwarn("You have some warnings you may want to solve before testing, but system is usable.")

    rospy.spin()
