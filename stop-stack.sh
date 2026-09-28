#!/bin/bash
# Stop the Meeting-Ops stack

echo "Stopping Meeting-Ops stack..."

# Stop processes
pkill -f "uvicorn.*9050" || true
pkill -f "vite.*7777" || true
pkill -f "no_auth_backend" || true

# Stop Docker services
docker compose -f docker-compose-services.yml down

echo "✅ Meeting-Ops stack stopped"