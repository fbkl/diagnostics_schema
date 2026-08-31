#!/usr/bin/env python3
import rospy
import socket
import subprocess
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus,  KeyValue

# SHORTCUT, and it should be called one: this package has no setup.py and
# catkin_python_setup() is commented out in CMakeLists.txt, so nothing in
# src/diagnostics_schema/ is importable by package name. pre_setup_diags_node.py
# gets away with `import pre_setup_diags` only because python puts the running
# script's OWN directory on sys.path and it happens to live in there. This file
# does not, so it has to say where to look. test/test_clock_checks.py already
# does the same thing. The real fix is a setup.py + catkin_python_setup(), which
# is a build change and wants doing deliberately, not as a side effect of this.
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src", "diagnostics_schema"))

# Reuse the probe from pre_setup_diags rather than growing a second, subtly
# different one. It speaks SNTP directly, so it needs no ssh -- which is the
# whole reason it works against the visualiser. See doc/clock_checks.md.
from pre_setup_diags import (NtpOffsetToHost, WARN_OFFSET_S, FAIL_OFFSET_S,
                             LOCAL_FALLBACK_STRATUM)

rospy.init_node("host_alive", anonymous=True)

hostname = rospy.get_param("~hostname","")
hostip = rospy.get_param("~hostip","")
waittime = rospy.get_param("~waittime",0.1)
hostlist = rospy.get_param("~hostlist", {})
name_prefix = rospy.get_param("~name_prefix","")

print(hostlist)
poolingtime = float(rospy.get_param("~pooling_time", "1.0"))

# Clock checking is OPT-IN per host, and deliberately not driven off `hostlist`:
# that list has tablets, dongles and a router in it, none of which serve NTP, and
# flagging them every cycle would train you to ignore this panel.
clock_hosts = rospy.get_param("~clock_hosts", {})
# The machine THIS node runs on. A chronyd that hit `maxchange` has EXITED, and
# nothing else in the stack would notice -- the clock just free-runs from there.
check_local_chronyd = rospy.get_param("~check_local_chronyd", True)
me = socket.gethostname()


def clock_status(name, hostname, hostip):
    """One DiagnosticStatus for one remote clock, via SNTP. No ssh."""
    d = DiagnosticStatus()
    d.name = f"{name} Clock"
    t = NtpOffsetToHost(hostname, hostip, samples=2)
    ret = t.run()
    d.values.append(KeyValue(key="IP", value=str(hostip)))
    if t.offset is None:
        d.level = d.ERROR
        d.message = f"{hostname} did not answer NTP on port 123."
        return d
    d.values.append(KeyValue(key="Offset ms", value=f"{t.offset * 1000:+.3f}"))
    d.values.append(KeyValue(key="RTT ms", value=f"{t.rtt * 1000:.1f}"))
    d.values.append(KeyValue(key="Stratum", value=str(t.stratum)))
    d.values.append(KeyValue(key="Refid", value=f"0x{t.refid:08X}"))
    if ret == "OK":
        d.level = d.OK
        d.message = f"{abs(t.offset) * 1000:.2f} ms from us, stratum {t.stratum}."
    elif t.refid == t.LOCAL_REFID or t.stratum > LOCAL_FALLBACK_STRATUM or t.stratum == 0:
        d.level = d.ERROR
        d.message = f"{hostname} is serving invented time (stratum {t.stratum}, refid 0x{t.refid:08X})."
    elif abs(t.offset) > FAIL_OFFSET_S:
        d.level = d.ERROR
        d.message = f"{abs(t.offset) * 1000:.1f} ms apart (limit {FAIL_OFFSET_S * 1000:.0f} ms). Do not record."
    else:
        d.level = d.WARN
        d.message = f"{abs(t.offset) * 1000:.1f} ms apart (warn over {WARN_OFFSET_S * 1000:.0f} ms)."
    return d


def chronyd_status():
    """Is chronyd on THIS machine still alive?

    This is the `maxchange` detector. maxchange makes chronyd EXIT rather than
    apply an absurd correction, which is the safe outcome -- the clock keeps
    free-running at its last frequency instead of lurching mid-take -- but
    nothing announces it. Liveness is the whole check; the forensic detail is in
    /var/log/chrony/tracking.log, which is what the recording sidecar keeps.
    """
    d = DiagnosticStatus()
    d.name = f"{name_prefix}/{me} chronyd"
    try:
        out = subprocess.check_output(["chronyc", "tracking"], timeout=5).decode()
    except FileNotFoundError:
        # Nothing was measured. Not the same as a bad clock -- say so.
        d.level = d.WARN
        d.message = "chronyc is not installed here, so chronyd liveness is UNKNOWN."
        return d
    except Exception as e:
        d.level = d.ERROR
        d.message = ("chronyd is not answering. If it was running earlier this is what "
                     "a maxchange abort looks like: the clock is now free-running. "
                     f"({e!r})")
        return d
    fields = dict(l.split(":", 1) for l in out.splitlines() if ":" in l)
    for k in ("Reference ID", "Stratum", "System time", "Leap status"):
        if k.strip() in fields:
            d.values.append(KeyValue(key=k, value=fields[k].strip()))
    d.level = d.OK
    d.message = f"chronyd up, stratum {fields.get('Stratum', '?').strip()}."
    return d

if poolingtime <= waittime:
    rospy.logfatal("you cannot wait the same or greater amount of time as the pooling time, or you won't ever get a result ")

if not hostlist:
    hostlist = {hostname:hostip}


pub = rospy.Publisher("/diagnostics", DiagnosticArray, queue_size=1)

while not rospy.is_shutdown():
    try:
        da = DiagnosticArray()
        da.header.stamp = rospy.Time.now()
        da.header.frame_id = "map"
        s_list = []
        this_time = rospy.Time.now()
        for hostname, hostip in hostlist.items(): 
            HOST_UP = None
            command =["ping","-c","1","-W",str(waittime),hostip] 
            #rospy.loginfo(command)
            s = subprocess.Popen(command,stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            s_list.append( s)
        finished_list = []
        while len(finished_list) < len(s_list):
            
            #print(finished_list)
            for i, (s, (hostname, hostip)) in enumerate(zip(s_list, hostlist.items() )):
                if i in finished_list:
                    continue
                s.poll()
                HOST_UP = False
                #rospy.loginfo(s.returncode)
                if not s.returncode:
                    if rospy.Time.now() < this_time + rospy.Duration(waittime):
                        continue
                if s.returncode == 0:
                    HOST_UP = True
                else:
                    HOST_UP = False
                d_msg = DiagnosticStatus()
                d_msg.name = name_prefix+"/"+hostname
                ip_kv = KeyValue()
                ip_kv.key = "IP"
                ip_kv.value = hostip
                d_msg.values.append(ip_kv)
                alive_kv = KeyValue()
                alive_kv.key = "Status"
                alive_kv.value = str(HOST_UP)
                d_msg.values.append(alive_kv)
                if HOST_UP:
                    d_msg.level = d_msg.OK
                    d_msg.message = f"Host {hostname} with IP {hostip} is up."
                else:
                    output, err = s.communicate()

                    d_msg.level = d_msg.ERROR
                    comb_out = output
                    if err:
                        comb_out += "\nERROR:" + err
                    d_msg.message = f"Host {hostname} with IP {hostip} is down."
                    rospy.logwarn(d_msg.message)
                    rospy.logdebug(f"{hostname}:{comb_out}")
                da.status.append(d_msg)
                finished_list.append(i)

            rospy.sleep(rospy.Duration(waittime))

        for hostname, hostip in clock_hosts.items():
            if hostname == me:
                continue   # the offset to ourselves is zero by construction
            da.status.append(clock_status(name_prefix + "/" + hostname, hostname, hostip))
        if check_local_chronyd:
            da.status.append(chronyd_status())

        pub.publish(da)
        rospy.sleep(rospy.Duration(poolingtime))
    except rospy.ROSInterruptException:
        rospy.signal_shutdown("something has to work")
        break
    except KeyboardInterrupt:
        rospy.signal_shutdown("something has to work")
        exit()

