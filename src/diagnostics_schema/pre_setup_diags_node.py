#!/usr/bin/env python3

import rospy
import pre_setup_diags
import socket

from pre_setup_diags import (CheckOwnHost, PingHost, Sound, Video, CheckRemoteChrony,
                             CheckLocalChrony, NtpOffsetToHost, CheckRemoteRealsense,
                             CheckRemoteMount, X11Host)


rospy.init_node("pre_setup_tester")

tests = []

#tests.append(CheckOwnHost())
tests.append(X11Host(criticality=pre_setup_diags.CRITICAL_REQUIREMENT))
tests.append(PingHost("Asus 5g router","192.168.2.1"))
#tests.append(PingHost("myself","192.168.1.100", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
#tests.append(PingHost("tablet","192.168.1.101", tips=["You can also use VNC to control the tablet now. Just connect to:\n\n\thttp://192.168.1.101:5800/vnc.html?autoconnect=true&show_dot=true&192.168.1.101&port=5900 \n"]))
#tests.append(PingHost("vicon pc","192.168.1.103", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
tests.append(Sound("/srv/data/calib.wav"))


NORMAL_PC_WITH_WEBCAM = False
if NORMAL_PC_WITH_WEBCAM:
    tests.append(Video("/dev/video0", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    ## I am maybe being a bit pedantic here, but these extra devices are not useless specially since it appears they provide better timestamping information, which we may want
    ## But even if I want to check those, which I don't think I do, the correct type of test is not implemented
    ## source https://unix.stackexchange.com/questions/512759/multiple-dev-video-for-one-physical-device
    ## more info https://linuxtv.org/downloads/v4l-dvb-apis/userspace-api/v4l/dev-meta.html and here: https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=088ead25524583e2200aa99111bea2f66a86545a
    #
    #tests.append(Video("/dev/video1", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    tests.append(Video("/dev/video2", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    ## you can play it with gst-launch-1.0 -v v4l2src device=/dev/video2 ! videoconvert ! autovideosink

    #tests.append(Video("/dev/video3", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))

    tests.append(Video("/dev/video4", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT)) ## this is the main device
    #tests.append(Video("/dev/video5", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    tests.append(Video("/dev/video6", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    ## you can play it with : gst-launch-1.0 -v v4l2src device=/dev/video6 ! videoconvert ! autovideosink
    #tests.append(Video("/dev/video7", criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
else:
    ## check if we have a realsense maybe?

    pass
# The clock of the machine we are running on -- which is raspberrypi, inside the
# container: startup_sequence.bash:71 ssh's to raspberrypi, pre_setup_run.sh
# starts run_docker_image.sh, and that runs prediags.bash. The container shares
# the host kernel clock, so this is raspberrypi's clock, i.e. the master's.
#
# Not redundant with the ssh check below: this one still works when the network
# is the thing that is broken, and it cannot be confused by an ssh problem
# pretending to be a clock problem.
tests.append(CheckLocalChrony(criticality=pre_setup_diags.CRITICAL_REQUIREMENT))

me = socket.gethostname()

##yaml... we can also have different usernames
machines = {"rpi5-silver-ubuntu":"192.168.1.4", "raspberrypi":"192.168.1.5", "rpi5-ubuntu":"192.168.1.3"}
for machine, ip in machines.items():
    tests.append(PingHost(machine, ip)) ## we wont use ip here
    if machine != me:
        tests.append(CheckRemoteChrony("frederico",machine, criticality=pre_setup_diags.CRITICAL_REQUIREMENT))
        # chronyc tracking is a self-report. This is the only test that actually
        # compares two clocks, which is the quantity ROS timestamps depend on.
        tests.append(NtpOffsetToHost(machine, ip, criticality=pre_setup_diags.CRITICAL_REQUIREMENT))
    if not machine == "raspberrypi":
        tests.append(CheckRemoteRealsense("frederico", machine))
    if machine == "raspberrypi":
        tests.append(CheckRemoteMount("frederico", machine, "/mnt/osim", criticality=pre_setup_diags.CRITICAL_REQUIREMENT))

# The visualiser runs rviz and ar_track_alvar, so it publishes TFs into the same
# graph and its clock is part of every recording -- even though alvar stamps its
# markers with the incoming image stamp. tf2's buffer and rviz both resolve "now"
# against the LOCAL clock, so a visualiser that disagrees with the backpack gets
# extrapolation errors and silently starved message_filters, not a clear failure.
#
# Reachability is fine: the visualiser sits on 192.168.2.39/24 and raspberrypi's
# wifi interface is 192.168.2.221, so this is one direct hop, no forwarding.
#
# But we will arrive with source address 192.168.2.221, and the visualiser's
# chrony.conf only has `allow 192.168.1.0/24`. Until it also allows
# 192.168.2.0/24 this test can only report "did not answer NTP on port 123",
# so it stays a warning. Promote it to CRITICAL once that line is in.
CHECK_VISUALISER_CLOCK = True
VISUALISER_HOST = "frkle-Predator-PT515-52"
VISUALISER_IP   = "192.168.2.39"
if CHECK_VISUALISER_CLOCK and VISUALISER_HOST != me:
    tests.append(NtpOffsetToHost(VISUALISER_HOST, VISUALISER_IP,
                                 criticality=pre_setup_diags.OPTIONAL_REQUIREMENT))
    # No CheckRemoteChrony here on purpose: there are deliberately no ssh keys to
    # the visualiser. paramiko could not negotiate the key types, so they were
    # removed rather than fought with. NtpOffsetToHost needs no ssh, which is why
    # it is the right tool for this host specifically.

pre_setup_diags.do(tests)

