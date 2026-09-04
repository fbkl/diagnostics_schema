#!/usr/bin/env python3
import rospy
import socket
import subprocess
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus,  KeyValue

# The verdict logic lives in pre_setup_diags and is shared with prediags, so the
# live panel and the pre-flight check can never drift apart on what "bad" means.
# See doc/clock_checks.md.
from diagnostics_schema.pre_setup_diags import (parse_chrony_tracking,
                                                chrony_summary,
                                                evaluate_chrony_tracking)

# Which tracking fields are worth carrying into the diagnostic. The rest are
# chrony internals that would just make the panel harder to read.
CHRONY_KEYS = ("Reference ID", "Stratum", "System time", "Last offset",
               "RMS offset", "Leap status")

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
    """One DiagnosticStatus for one remote clock, asked of that host's own chronyd.

    `chronyc -h` talks to chronyd's command port (UDP 323) directly: no ssh, and
    no measurement of our own. What comes back is chrony's OWN estimate, filtered
    over many samples with outlier rejection, which is a different quality of
    number from the two raw SNTP round trips this used to take. Those two samples
    were largely measuring wifi packet queueing -- the old probe read +1.06 ms
    against LOOPBACK, where the answer is zero by construction -- so the panel
    flapped WARN constantly and was training us to ignore it.

    Note what this is and is not. chronyc tracking is a SELF-REPORT: each host
    grades itself against its own selected source. In this star topology that is
    the number we want anyway, because every node except raspberrypi has
    raspberrypi as its source, so "System time" IS the offset to the master. What
    is genuinely lost is check #3, an independent two-machine comparison. That
    still exists as NtpOffsetToHost in prediags, where it runs once, deliberately,
    before a recording, rather than every cycle over wifi.

    Requires `bindcmdaddress <that host's lan ip>` and a `cmdallow` covering us,
    on the target. Bind to the LAN address: three of these machines also hold
    campus addresses.
    """
    d = DiagnosticStatus()
    d.name = f"{name} Clock"
    d.values.append(KeyValue(key="IP", value=str(hostip)))

    try:
        text = subprocess.check_output(["chronyc", "-h", str(hostip), "tracking"],
                                       stderr=subprocess.STDOUT, timeout=6).decode()
    except FileNotFoundError:
        # Nothing was measured. Deliberately not ERROR: this says nothing at all
        # about that host's clock, and reporting it as a clock fault is a lie.
        d.level = d.WARN
        d.message = "chronyc is not installed here, so nothing was measured."
        return d
    except Exception as e:
        d.level = d.ERROR
        d.message = (f"could not reach chronyd on {hostname} ({hostip}). It needs "
                     f"`bindcmdaddress {hostip}` and a `cmdallow` covering us. {e!r}")
        return d

    fields = parse_chrony_tracking(text)
    for k in CHRONY_KEYS:
        if k in fields:
            d.values.append(KeyValue(key=k, value=fields[k]))

    verdict = evaluate_chrony_tracking(text)
    if verdict is None:
        d.level = d.OK
        d.message = chrony_summary(text)
        return d
    fatal, msg = verdict
    d.level = d.ERROR if fatal else d.WARN
    d.message = msg
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
    # This hand-rolled its own parse and got it wrong: splitting on ":" leaves
    # the padding on the key, so "Stratum         " never matched "Stratum" and
    # every field silently vanished. parse_chrony_tracking already strips both
    # sides. Do not grow a second parser for a format we already parse.
    fields = parse_chrony_tracking(out)
    for k in CHRONY_KEYS:
        if k in fields:
            d.values.append(KeyValue(key=k, value=fields[k]))

    # chronyd being up is the point of this check -- it is the maxchange-abort
    # detector -- but we have the text in hand, so grade the local clock with the
    # same rules as every other host rather than reporting a bare "up".
    verdict = evaluate_chrony_tracking(out)
    if verdict is None:
        d.level = d.OK
        d.message = f"chronyd up, {chrony_summary(out)}"
        return d
    fatal, msg = verdict
    d.level = d.ERROR if fatal else d.WARN
    d.message = f"chronyd up, but: {msg}"
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

