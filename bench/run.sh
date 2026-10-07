#!/bin/sh
# compares the latency of two microsocks binaries in the trading setup:
#
#   gateway --(GW_RTT_MS)--> microsocks --(MKT_RTT_MS)--> market
#
# runs in a private network namespace (unshare -rn, no root needed) where
# netem adds the delays on loopback, so it does not touch the host network.
# needs: python3, tc (iproute-tc) and the sch_netem kernel module.
#
# usage: bench/run.sh OLD_BINARY NEW_BINARY [extra bench.py options]
#
# e.g. against the upstream v1.0.5:
#   git worktree add /tmp/ms-v1.0.5 v1.0.5 && make -C /tmp/ms-v1.0.5
#   make && bench/run.sh /tmp/ms-v1.0.5/microsocks ./microsocks
#
# environment (defaults): GW_RTT_MS=10 MKT_RTT_MS=0.1 REPEAT=3
set -e

if [ -z "$BENCH_IN_NETNS" ]; then
	[ $# -ge 2 ] || { sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 1; }
	old=$(readlink -f "$1"); new=$(readlink -f "$2"); shift 2
	exec env BENCH_IN_NETNS=1 unshare -rn "$(readlink -f "$0")" "$old" "$new" "$@"
fi

old=$1; new=$2; shift 2
dir=$(dirname "$0")
: "${GW_RTT_MS:=10}" "${MKT_RTT_MS:=0.1}" "${REPEAT:=3}"
half() { awk "BEGIN { printf \"%dus\", $1 * 1000 / 2 }"; }

ip link set lo up
# netem on lo delays every packet once, so one way delay = RTT/2.
# band 1:1 market leg (port 9000), band 1:3 gateway leg (ports 1080-1081).
tc qdisc add dev lo root handle 1: prio
tc qdisc add dev lo parent 1:1 handle 10: netem delay "$(half "$MKT_RTT_MS")"
tc qdisc add dev lo parent 1:3 handle 30: netem delay "$(half "$GW_RTT_MS")"
for d in sport dport; do
	tc filter add dev lo parent 1:0 protocol ip prio 1 u32 match ip $d 9000 0xffff flowid 1:1
	tc filter add dev lo parent 1:0 protocol ip prio 1 u32 match ip $d 1080 0xfffe flowid 1:3
done

"$old" -q -i 127.0.0.1 -p 1080 & p1=$!
"$new" -q -i 127.0.0.1 -p 1081 & p2=$!
trap 'kill $p1 $p2 2>/dev/null' EXIT
sleep 0.3

echo "gateway<->proxy RTT ${GW_RTT_MS}ms, proxy<->market RTT ${MKT_RTT_MS}ms"
hdr=--header
i=0
while [ $i -lt "$REPEAT" ]; do
	python3 -I "$dir/bench.py" --proxy 127.0.0.1:1080 --label old $hdr "$@"
	python3 -I "$dir/bench.py" --proxy 127.0.0.1:1081 --label new "$@"
	hdr=
	i=$((i+1))
done
