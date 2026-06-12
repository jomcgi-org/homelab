#!/bin/bash
# SigNoz MCP Server Wrapper with kubectl port-forward
# This script starts kubectl port-forward and then launches the SigNoz MCP server

set -e

# Configuration
SIGNOZ_NAMESPACE="signoz"
SIGNOZ_SERVICE="signoz"
SIGNOZ_PORT="8080"
LOCAL_PORT="38080"
SIGNOZ_MCP_SERVER="${SIGNOZ_MCP_SERVER_PATH}"

# Cleanup function
cleanup() {
	echo "Cleaning up..."
	if [ ! -z "$PORT_FORWARD_PID" ]; then
		kill $PORT_FORWARD_PID 2>/dev/null || true
	fi
}

trap cleanup EXIT

# Check if kubectl is available
if ! command -v kubectl &>/dev/null; then
	echo "Error: kubectl is not installed or not in PATH"
	exit 1
fi

# Check if SigNoz MCP server binary exists
if [ ! -f "$SIGNOZ_MCP_SERVER" ]; then
	echo "Error: SigNoz MCP server binary not found at: $SIGNOZ_MCP_SERVER"
	echo "Please set SIGNOZ_MCP_SERVER_PATH environment variable or install to default location"
	exit 1
fi

# Check if we can access the cluster
if ! kubectl get namespace $SIGNOZ_NAMESPACE &>/dev/null; then
	echo "Error: Cannot access namespace '$SIGNOZ_NAMESPACE'. Check your kubectl context."
	exit 1
fi

# Start kubectl port-forward in the background
echo "Starting kubectl port-forward: $SIGNOZ_SERVICE.$SIGNOZ_NAMESPACE:$SIGNOZ_PORT -> localhost:$LOCAL_PORT"
kubectl port-forward -n $SIGNOZ_NAMESPACE svc/$SIGNOZ_SERVICE $LOCAL_PORT:$SIGNOZ_PORT &
PORT_FORWARD_PID=$!

# Wait for port-forward to be ready
echo "Waiting for port-forward to be ready..."
for i in {1..10}; do
	if nc -z localhost $LOCAL_PORT 2>/dev/null; then
		echo "Port-forward is ready"
		break
	fi
	if [ $i -eq 10 ]; then
		echo "Error: Port-forward failed to start"
		exit 1
	fi
	sleep 1
done

# Set environment variables for MCP server
export SIGNOZ_URL="http://localhost:$LOCAL_PORT/app/signoz"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# SIGNOZ_API_KEY should be set in the environment or MCP client config
if [ -z "$SIGNOZ_API_KEY" ]; then
	echo "Warning: SIGNOZ_API_KEY is not set. MCP server may fail to authenticate."
fi

echo "Starting SigNoz MCP server..."
echo "  SIGNOZ_URL: $SIGNOZ_URL"
echo "  LOG_LEVEL: $LOG_LEVEL"

# Start the MCP server (this will run in foreground)
exec "$SIGNOZ_MCP_SERVER"
