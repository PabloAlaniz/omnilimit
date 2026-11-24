#!/bin/sh
# A Redis Cluster of one node that holds every slot, for the store tests. It is the command of
# the `redis-cluster` container in compose.test.yml and in CI.
#
# One node is enough for what the tests prove: a cluster refuses a script that touches a key it
# did not declare, so every script here takes exactly the key it is given.
#
# PORT is the port clients reach the node on. The node announces it, because a cluster client
# connects to the address the node gives, not to the one it was configured with.
set -eu
PORT="${PORT:-6379}"
redis-server --port "$PORT" --cluster-enabled yes --cluster-announce-ip 127.0.0.1 --save '' --appendonly no "$@" &
until redis-cli -p "$PORT" ping >/dev/null 2>&1; do sleep 0.1; done
redis-cli -p "$PORT" cluster addslotsrange 0 16383 >/dev/null
until redis-cli -p "$PORT" cluster info | grep -q 'cluster_state:ok'; do sleep 0.1; done
echo "cluster ready on port $PORT"
wait
