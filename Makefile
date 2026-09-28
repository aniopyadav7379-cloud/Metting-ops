# Meeting-Ops Recording Appliance - Makefile
# Part of the Unicorn Commander Suite

COMPOSE := docker compose -f docker-compose-full-stack.yml
BACKEND_PORT := 9050
FRONTEND_PORT := 7777
DATABASE_URL := postgresql://meetingops:meetingops123@localhost:5434/meeting_sessions

.PHONY: help up down backend frontend test test-backend test-frontend logs health status install backup clean

help:
	@echo "Meeting-Ops Recording Appliance"
	@echo ""
	@echo "Services:"
	@echo "  make up        - Start Docker services (PostgreSQL, Redis, Qdrant, llama.cpp)"
	@echo "  make down      - Stop Docker services"
	@echo "  make backend   - Start backend on port $(BACKEND_PORT)"
	@echo "  make frontend  - Start frontend on port $(FRONTEND_PORT)"
	@echo ""
	@echo "Testing:"
	@echo "  make test          - Run backend + frontend tests"
	@echo "  make test-backend  - Run backend tests only"
	@echo "  make test-frontend - Run frontend tests only"
	@echo ""
	@echo "Operations:"
	@echo "  make logs      - Tail Docker service logs"
	@echo "  make health    - Check backend health endpoint"
	@echo "  make status    - Show running services and ports"
	@echo "  make install   - Run install-meeting-ops.sh"
	@echo "  make backup    - Run backup script"
	@echo "  make clean     - Stop everything and remove Docker volumes"

up:
	$(COMPOSE) up -d
	@echo "Waiting for PostgreSQL..."
	@for i in $$(seq 1 30); do \
		docker exec meetingops-postgres pg_isready -U meetingops >/dev/null 2>&1 && break; \
		sleep 1; \
	done
	@echo "Services started. llama.cpp may still be loading the model (~60s for GPT-OSS 20B)."

down:
	$(COMPOSE) down

backend:
	cd backend && DATABASE_URL="$(DATABASE_URL)" python3 -m uvicorn main:app --host 0.0.0.0 --port $(BACKEND_PORT)

frontend:
	cd frontend && npm run dev -- --host 0.0.0.0

test: test-backend test-frontend

test-backend:
	cd backend && DATABASE_URL="$(DATABASE_URL)" python3 -m pytest tests/ -v

test-frontend:
	cd frontend && npm run build && npx vitest run

logs:
	$(COMPOSE) logs -f

health:
	@curl -sf http://localhost:$(BACKEND_PORT)/health | python3 -m json.tool 2>/dev/null || echo "Backend not responding on port $(BACKEND_PORT)"

status:
	@echo "=== Docker Services ==="
	@docker ps --filter "name=meetingops-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not running"
	@echo ""
	@echo "=== Backend ==="
	@curl -sf http://localhost:$(BACKEND_PORT)/health >/dev/null 2>&1 && echo "Backend: running on port $(BACKEND_PORT)" || echo "Backend: not running"
	@echo ""
	@echo "=== Frontend ==="
	@curl -sf http://localhost:$(FRONTEND_PORT)/ >/dev/null 2>&1 && echo "Frontend: running on port $(FRONTEND_PORT)" || echo "Frontend: not running"
	@echo ""
	@echo "=== llama.cpp ==="
	@curl -sf http://localhost:11437/health >/dev/null 2>&1 && echo "llama.cpp: ready" || echo "llama.cpp: not ready (may still be loading)"

install:
	bash install-meeting-ops.sh

backup:
	bash scripts/backup.sh

clean:
	$(COMPOSE) down -v
	@echo "Docker volumes removed."
