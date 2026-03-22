#!/bin/bash
cd "$(dirname "$0")"
echo "Starting ONCALL Dashboard Server..."
echo "Dashboard URL: http://$(hostname -f):8050"
echo "Press Ctrl+C to stop"
echo ""
python3.9 server.py
