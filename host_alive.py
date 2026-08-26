#!/usr/bin/env python3
import rospy
import subprocess
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus,  KeyValue
rospy.init_node("host_alive", anonymous=True)

hostname = rospy.get_param("~hostname","")
hostip = rospy.get_param("~hostip","")
waittime = rospy.get_param("~waittime",0.1)
hostlist = rospy.get_param("~hostlist", {})
name_prefix = rospy.get_param("~name_prefix","")

print(hostlist)
poolingtime = float(rospy.get_param("~pooling_time", "1.0"))

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
        pub.publish(da)
        rospy.sleep(rospy.Duration(poolingtime))
    except rospy.ROSInterruptException:
        rospy.signal_shutdown("something has to work")
        break
    except KeyboardInterrupt:
        rospy.signal_shutdown("something has to work")
        exit()

