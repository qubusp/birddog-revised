#!/bin/bash
# docker-stacks start hook (sourced by start.sh before the server starts):
# put the Birddog example notebooks into the user's persistent work volume.
# -n: a user's edited copy is never overwritten. Delete it to get a fresh one.
mkdir -p /home/jovyan/work/examples
cp -rn /opt/birddog-examples/. /home/jovyan/work/examples/ 2>/dev/null || true
