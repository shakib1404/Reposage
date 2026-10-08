#!/bin/sh
# Starts as root for ONE reason: a platform volume (Railway, a Docker named
# volume) is mounted over /data owned by root, which hides the chown done at
# image build time and leaves the unprivileged `reposage` user unable to write
# the corpus, RAG indexes or job outputs. Fix the ownership, then drop to
# `reposage` before anything runs — the app, and every audited repository's
# code, still executes unprivileged, exactly as before.
set -e

DATA="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA/outputs/taskexec" "$DATA/corpus"
    # Non-recursive on purpose: anything the app created earlier is already
    # owned by reposage, and recursing over a large volume slows every start.
    chown reposage:reposage "$DATA" "$DATA/outputs" "$DATA/outputs/taskexec" \
        "$DATA/corpus"
    export HOME=/home/reposage
    exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi

# Already unprivileged (docker-compose with a user: directive, local runs).
exec "$@"
