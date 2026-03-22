#!/usr/bin/env bash
# zpilot-http-host.sh — Start zpilot HTTP server and expose via devtunnel.
#
# Usage:
#   zpilot-http-host.sh [--port PORT] [--tunnel-name NAME]
#
# Prerequisites:
#   - zpilot installed (pip install -e .)
#   - devtunnel CLI installed (https://learn.microsoft.com/azure/developer/dev-tunnels/)
#   - devtunnel login completed
#
# The script:
#   1. Starts zpilot serve-http in the background
#   2. Creates/reuses a devtunnel and forwards the port
#   3. Prints the public URL for nodes.toml configuration
#   4. Cleans up on exit (SIGINT/SIGTERM)

set -euo pipefail

PORT="${1:-8222}"
TUNNEL_NAME="${2:-zpilot}"

cleanup() {
    echo ""
    echo "Shutting down..."
    if [[ -n "${ZPILOT_PID:-}" ]]; then
        kill "$ZPILOT_PID" 2>/dev/null || true
        wait "$ZPILOT_PID" 2>/dev/null || true
    fi
    devtunnel delete "$TUNNEL_NAME" 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

# Check prerequisites
command -v zpilot >/dev/null 2>&1 || { echo "Error: zpilot not found. Install with: pip install -e ."; exit 1; }
command -v devtunnel >/dev/null 2>&1 || { echo "Error: devtunnel CLI not found. See: https://learn.microsoft.com/azure/developer/dev-tunnels/"; exit 1; }

echo "=== zpilot HTTP + devtunnel host ==="
echo ""

# Start zpilot HTTP server
echo "Starting zpilot serve-http on port $PORT..."
zpilot serve-http --port "$PORT" &
ZPILOT_PID=$!
sleep 2

# Verify it started
if ! kill -0 "$ZPILOT_PID" 2>/dev/null; then
    echo "Error: zpilot serve-http failed to start"
    exit 1
fi

# Create devtunnel
echo "Creating devtunnel '$TUNNEL_NAME'..."
devtunnel create "$TUNNEL_NAME" --allow-anonymous 2>/dev/null || true
devtunnel port create "$TUNNEL_NAME" --port-number "$PORT" 2>/dev/null || true

# Host the tunnel (this blocks)
echo ""
echo "Hosting devtunnel — your public URL will appear below."
echo "Add this URL to nodes.toml on peer machines."
echo "Press Ctrl+C to stop."
echo ""
devtunnel host "$TUNNEL_NAME"
