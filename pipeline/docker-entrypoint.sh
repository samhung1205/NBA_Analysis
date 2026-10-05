#!/bin/sh
set -eu

BOOTSTRAP_DIR="/app/artifacts/production"
ARTIFACT_DIR="${MODEL_ARTIFACT_DIR:-/app/artifacts/production}"

echo "[entrypoint] MODEL_ARTIFACT_DIR=${ARTIFACT_DIR}"

# Railway production uses a persistent volume.
# Bootstrap it only when no production artifact exists yet.
if [ "$ARTIFACT_DIR" != "$BOOTSTRAP_DIR" ]; then
    mkdir -p "$ARTIFACT_DIR"

    if [ ! -f "$ARTIFACT_DIR/CURRENT.json" ]; then
        echo "[entrypoint] No persistent production artifact found; bootstrapping..."
        cp -R "$BOOTSTRAP_DIR/." "$ARTIFACT_DIR/"
        rm -f "$ARTIFACT_DIR/.lock"
        echo "[entrypoint] Bootstrap artifact copied."
    else
        echo "[entrypoint] Persistent production artifact already exists; keeping it."
    fi
fi

exec python scheduler.py
