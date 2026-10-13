#!/usr/bin/env bash
# Stop every local SuperLink, SuperNode and SuperExec process.
pkill -f "[f]lower-supernode" || true
pkill -f "[f]lower-superexec" || true
pkill -f "[f]lower-superlink" || true
echo "stopped"
