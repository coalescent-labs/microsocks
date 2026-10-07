#!/bin/sh
# runs tests/regress.py in a private network namespace (no root needed).
# usage: tests/run.sh BINARY [BINARY ...]
set -e
[ $# -ge 1 ] || { echo "usage: $0 BINARY [BINARY ...]"; exit 1; }
dir=$(dirname "$(readlink -f "$0")")
bins=
for b; do bins="$bins $(readlink -f "$b")"; done
exec unshare -rn sh -c "ip link set lo up && exec python3 -I '$dir/regress.py' $bins"
