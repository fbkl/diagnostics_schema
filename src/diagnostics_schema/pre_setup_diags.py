#!/usr/bin/env python3

import rospy
from abc import ABC
from abc import abstractmethod
import subprocess
import os
import ipaddress
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
        try:
            self.hostreturn = subprocess.check_output(["ssh","-q", "-o","BatchMode=yes",
                "-o","ConnectTimeout=3",
                f"{self.user}@{self.host}", *self.command], timeout=6).decode()
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
#   1. IS THE TIME REAL?      -> Stratum.
#      Every backpack node carries `local stratum 10 orphan` so the mesh stays
#      self-consistent when it is cut off from the world. That is deliberate and
#      good, but it means an island with no upstream elects a leader and all
#      three agree on a completely fabricated time. Leap status stays "Normal"
#      throughout. Stratum >= 10 is the only thing that gives it away.
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

# Must match `local stratum N orphan` in /etc/chrony/conf.d/backpack.conf on
# every node. If you change it there, change it here.
ORPHAN_STRATUM = 10

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
                             orphan_stratum=ORPHAN_STRATUM):
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
        return (True, f"this host is its OWN time reference (refid {refid}). It is handing out an undisciplined clock as if it were authoritative. Look for a `local stratum 1` line, and check whether its upstream server actually resolves.")

    try:
        stratum = int(f.get("Stratum", ""))
    except ValueError:
        return (True, "could not read Stratum out of chronyc tracking.")

    if stratum >= orphan_stratum:
        return (True, f"stratum {stratum}: this is the orphan mesh talking to itself. Every backpack node will agree with this time and all of it is invented -- nothing upstream is reachable from anywhere in the cluster.")

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
    "If it keeps coming back, the stock 'makestep 1 3' is the cause. The orphan mesh answers in ~4 s (minpoll 2) while upstream needs a minute, so the whole step budget is spent on ISLAND time before real time ever shows up -- and after that chrony can only slew. Put this in /etc/chrony/conf.d/backpack-step.conf on every node:\n\tmakestep 1 -1\n\tmaxchange 100 10 3",
    "Stratum >= 10 means nobody in the cluster has upstream. Check wifi. Note the Ubuntu nodes have every 'pool' line commented out in /etc/chrony/chrony.conf, so the campus servers are their ONLY upstream -- add 'pool 2.debian.pool.ntp.org iburst' to /etc/chrony/sources.d/upstream.sources so they have a path that does not depend on being on the campus network.",
    "The actual fix is hardware: a coin cell on the RPi5 RTC header (J5). Then the Pis never boot on fake-hwclock time and never lock onto a bogus reference in the first place.",
]


class CheckRemoteChrony(CheckRemote):
    def __init__(self, username, hostname,
                 warn_offset=WARN_OFFSET_S, fail_offset=FAIL_OFFSET_S,
                 orphan_stratum=ORPHAN_STRATUM, **kwargs):
        super().__init__(username, hostname, ["chronyc", "tracking"], **kwargs)
        self.warn_offset = warn_offset
        self.fail_offset = fail_offset
        self.orphan_stratum = orphan_stratum
        self.summary = ""

    def run(self):
        try:
            err = super().run()
            if err:
                return err
            self.summary = chrony_summary(self.hostreturn)
            verdict = evaluate_chrony_tracking(self.hostreturn, self.warn_offset,
                                               self.fail_offset, self.orphan_stratum)
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
                 orphan_stratum=ORPHAN_STRATUM, **kwargs):
        super().__init__(**kwargs)
        self.warn_offset = warn_offset
        self.fail_offset = fail_offset
        self.orphan_stratum = orphan_stratum
        self.host = socket.gethostname()
        self.summary = ""

    def run(self):
        try:
            self.hostreturn = subprocess.check_output(["chronyc", "tracking"],
                                                      timeout=6).decode()
        except Exception as e:
            return self.criticality + repr(e)
        try:
            self.summary = chrony_summary(self.hostreturn)
            verdict = evaluate_chrony_tracking(self.hostreturn, self.warn_offset,
                                               self.fail_offset, self.orphan_stratum)
            if verdict is None:
                return 'OK'
            fatal, msg = verdict
            return (self.criticality if fatal else OPTIONAL_REQUIREMENT) + msg
        except Exception as e:
            return self.criticality + repr(e)

    def troubleshootingmsg(self):
        return ["Is chronyd running here?\n\tsystemctl status chrony",
                "Careful inside a container: it shares the HOST kernel clock, so this reports the host's clock, not something the container owns.",
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

    Read this together with the stratum check, not instead of it: an orphaned
    mesh agrees with itself beautifully. This says "consistent", not "correct".
    """

    NTP_EPOCH = 2208988800  # seconds between 1900-01-01 and 1970-01-01

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

    def _probe(self):
        """One SNTP exchange. Returns (offset, round_trip_delay, stratum)."""
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
        return offset, delay, stratum

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
            self.offset, self.rtt, self.stratum = best

            if self.stratum == 0 or self.stratum >= ORPHAN_STRATUM:
                return self.criticality + f"{self.host} answers NTP but reports stratum {self.stratum}, so it is not serving real time."
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
    def __init__(self, username, hostname, mountpoint, nfs_source="frkle-Predator-PT515-52:/home/frkle/shared/osim", **kwargs):
        command = ["bash", "-c",
            f"mountpoint -q {mountpoint} && [ -n \"$(ls -A {mountpoint} 2>/dev/null)\" ] && echo MOUNTED_AND_POPULATED || echo NOT_OK"]
        super().__init__(username, hostname, command, **kwargs)
        self.mountpoint = mountpoint
        self.nfs_source = nfs_source

    def run(self):
        try:
            err = super().run()
            if err:
                return err
            if self.hostreturn and "MOUNTED_AND_POPULATED" in self.hostreturn:
                return 'OK'
            return self.criticality + f"{self.mountpoint} on {self.host} is not mounted, or is mounted but empty!"
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
