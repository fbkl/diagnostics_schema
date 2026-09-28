#!/usr/bin/env python3
"""Can this camera be calibrated against the room AR bundle RIGHT NOW?

Run one per camera, inside the camera's namespace (next to its ar_track_alvar),
so `ar_pose_marker` resolves to /<camera>/ar_pose_marker.

ar_track_alvar (findMarkerBundlesNoKinect) publishes an AlvarMarkers message for
EVERY image it processes, and the list is EMPTY when no marker of the bundle was
detected. So three different failures, which used to look identical from the
outside ("I pressed calibrate and nothing changed"), are separable here:

  ERROR  no ar_pose_marker messages at all -> camera stream or alvar is down
  WARN   messages arrive, but empty        -> marker not visible (clipped, blurred,
                                              too far, bad light). THIS is the one
                                              that looks fine by eye.
  OK     the bundle was detected recently

Every freshness decision uses LOCAL ARRIVAL time, never header.stamp. The stamp
comes from the camera's clock on another machine, so judging freshness by it
would turn a clock offset into a fake "marker lost" -- or hide a real one.
The stamp lag (now - stamp) is still reported, separately, and warns on its
own: fresh arrivals with a large stamp lag is a CLOCK problem (see the Clocks
panel), not a marker problem. That split is the whole point: stale stamps with
a visible marker used to get misread as "chrony again".
"""
import rospy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from ar_track_alvar_msgs.msg import AlvarMarkers

OK, WARN, ERROR = DiagnosticStatus.OK, DiagnosticStatus.WARN, DiagnosticStatus.ERROR


def evaluate_ar_visibility(now, last_msg_arrival, last_seen_arrival, last_seen_stamp,
                           stream_timeout=2.0, seen_timeout=1.0, max_stamp_lag=0.5):
    """Pure verdict. All args are float seconds, or None for "never".

    now / *_arrival are LOCAL clock; last_seen_stamp is the camera's clock.
    Returns (level, message, stamp_lag_or_None).
    """
    stamp_lag = None
    if last_seen_stamp is not None and last_seen_arrival is not None:
        # lag measured at arrival, so it does not grow just because the marker left view
        stamp_lag = last_seen_arrival - last_seen_stamp

    if last_msg_arrival is None or now - last_msg_arrival > stream_timeout:
        return ERROR, "CANNOT calibrate: no ar_pose_marker messages (camera stream or ar_track_alvar down)", stamp_lag
    if last_seen_arrival is None:
        return WARN, "CANNOT calibrate: room marker never seen (alvar is running, marker not detected)", stamp_lag
    if now - last_seen_arrival > seen_timeout:
        return WARN, "CANNOT calibrate: room marker not visible for %.1f s (clipped? blurred? too far?)" % (now - last_seen_arrival), stamp_lag
    if stamp_lag is not None and abs(stamp_lag) > max_stamp_lag:
        return WARN, "marker visible, but image stamps are %.2f s off -- CLOCK problem, check the Clocks panel" % stamp_lag, stamp_lag
    return OK, "can calibrate: room marker visible", stamp_lag


class ArVisibility:
    def __init__(self):
        self.camera = rospy.get_param("~camera", rospy.get_namespace().strip("/") or "camera")
        self.stream_timeout = float(rospy.get_param("~stream_timeout", 2.0))
        self.seen_timeout = float(rospy.get_param("~seen_timeout", 1.0))
        self.max_stamp_lag = float(rospy.get_param("~max_stamp_lag", 0.5))
        self.last_msg_arrival = None
        self.last_seen_arrival = None
        self.last_seen_stamp = None
        self.last_ids = []
        self.n_msgs = 0
        self.n_seen = 0
        self.pub = rospy.Publisher("/diagnostics", DiagnosticArray, queue_size=1)
        rospy.Subscriber("ar_pose_marker", AlvarMarkers, self.callback, queue_size=1)

    def callback(self, msg):
        now = rospy.get_time()
        self.last_msg_arrival = now
        self.n_msgs += 1
        if msg.markers:
            self.last_seen_arrival = now
            self.last_seen_stamp = msg.markers[0].header.stamp.to_sec()
            self.last_ids = [m.id for m in msg.markers]
            self.n_seen += 1

    def publish(self, _event=None):
        now = rospy.get_time()
        level, message, stamp_lag = evaluate_ar_visibility(
            now, self.last_msg_arrival, self.last_seen_arrival, self.last_seen_stamp,
            self.stream_timeout, self.seen_timeout, self.max_stamp_lag)

        def age(t):
            return "never" if t is None else "%.2f s" % (now - t)

        st = DiagnosticStatus(level=level, name="AR %s room marker" % self.camera,
                              hardware_id=self.camera, message=message)
        st.values = [
            KeyValue("last ar_pose_marker message", age(self.last_msg_arrival)),
            KeyValue("last time marker seen", age(self.last_seen_arrival)),
            KeyValue("stamp lag (arrival - stamp)", "n/a" if stamp_lag is None else "%.3f s" % stamp_lag),
            KeyValue("bundle ids seen last", str(self.last_ids)),
            KeyValue("frames with marker / frames", "%d / %d (since the last diag tick)" % (self.n_seen, self.n_msgs)),
        ]
        self.n_msgs = self.n_seen = 0

        arr = DiagnosticArray()
        arr.header.stamp = rospy.Time.now()
        arr.status = [st]
        self.pub.publish(arr)


if __name__ == "__main__":
    rospy.init_node("ar_visibility")
    node = ArVisibility()
    rospy.Timer(rospy.Duration(float(rospy.get_param("~period", 1.0))), node.publish)
    rospy.spin()
