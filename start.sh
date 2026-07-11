#!/bin/bash
# Start API server in background, scheduler in foreground
gunicorn services.api_server:app --bind 0.0.0.0:8080 --workers 2 --timeout 120 &
exec python cli.py scheduler
