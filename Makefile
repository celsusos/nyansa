SHELL := /bin/bash

HOST ?= 127.0.0.1
PORT ?= 8080
UI_PORT ?= 5173

# Dev and production both run on Postgres so the builds match.
DATABASE_URL ?= postgresql://nyansa:nyansa@localhost:5432/nyansa
PG_CONTAINER ?= nyansa-postgres

# Only used by root-api (bootstrap through a running server).
ROOT_TOKEN ?= dev-root-token-change-me-0001
BOOTSTRAP_SECRET ?= dev-bootstrap-secret-change-me
ORG_NAME ?= Dev Grid

# make join URL=...       control plane to join
# make superuser USER_ID=...  additional platform superuser
URL ?=
USER_ID ?=

NVM_SH ?= $(HOME)/.nvm/nvm.sh
NVM_BUN = source "$(NVM_SH)" && nvm use 22 >/dev/null && bun

# Source .env (if present) into the recipe shell. Used by any target that runs the
# CLI / server / admin tools so they pick up DATABASE_URL, tokens, and secrets.
LOAD_ENV = set -a; [ -f .env ] && . ./.env || true; set +a

.PHONY: help install ui-install ui-build vscode-check vscode-package vscode-install test test-postgres \
	typecheck build dev-setup init-dev join db-up db-down db-logs root-api superuser \
	cli run agent run-api run-ui dev prod-check clean-dev docker-up docker-up-tls \
	docker-down docker-logs

help:
	@echo "Nyansa operations"
	@echo ""
	@echo "Setup:"
	@echo "  make install          Install Python and UI dependencies"
	@echo "  make dev-setup        Install deps, run the installer, build UI"
	@echo "  make init-dev         Run the installer (Docker, Postgres, org, root account)"
	@echo "  make join URL=...     Connect this machine to an existing control plane"
	@echo "  make db-up            Start the local Postgres dev container"
	@echo "  make db-down          Stop and remove the local Postgres dev container"
	@echo "  make db-logs          Tail the Postgres container logs"
	@echo "  make root-api ROOT_TOKEN=... BOOTSTRAP_SECRET=...  Bootstrap through running API"
	@echo "  make superuser USER_ID=...  Create an additional platform superuser"
	@echo ""
	@echo "Run:"
	@echo "  make run              Run the agent CLI (alias: make cli / make agent)"
	@echo "  make run-api          Run FastAPI control plane on $(HOST):$(PORT)"
	@echo "  make run-ui           Run Vite UI dev server on $(UI_PORT)"
	@echo "  make dev              Run API and UI dev servers together"
	@echo "  make docker-up        Build and run the production-like stack (Postgres + control plane)"
	@echo "  make docker-up-tls DOMAIN=...  Same, with the Caddy HTTPS proxy"
	@echo "  make docker-logs      Tail the Docker Compose logs"
	@echo "  make docker-down      Stop the Docker Compose stack"
	@echo ""
	@echo "Validate:"
	@echo "  make test             Run Python tests"
	@echo "  make test-postgres    Run tests against a real Postgres (the production store)"
	@echo "  make typecheck        Run UI typecheck"
	@echo "  make build            Build production UI"
	@echo "  make vscode-check     Validate VS Code extension"
	@echo "  make vscode-package   Package VS Code extension"
	@echo "  make vscode-install   Install the latest packaged VS Code extension locally"
	@echo "  make prod-check       Tests (incl. Postgres) + UI build + extension check"
	@echo ""
	@echo "Clean:"
	@echo "  make clean-dev        Remove local dev key material under .dev"

install: ui-install
	uv sync

ui-install:
	cd agent-ui && $(NVM_BUN) install

ui-build:
	cd agent-ui && $(NVM_BUN) run build

typecheck:
	cd agent-ui && $(NVM_BUN) run typecheck

vscode-check:
	cd extensions/vscode-nyansa && node --check src/extension.js

vscode-package:
	cd extensions/vscode-nyansa && npm install && npx vsce package --no-dependencies

vscode-install: vscode-package
	code --install-extension "$$(ls -t extensions/vscode-nyansa/*.vsix | head -n1)" --force

test:
	uv run python -m unittest discover -s tests

# Exercises the Postgres-backed store (the production datastore), which the plain
# `make test` run skips. Spins up the local container and points the suite at it.
test-postgres: db-up
	TEST_DATABASE_URL="$(DATABASE_URL)" uv run python -m unittest discover -s tests

build: ui-build

db-up:
	@if [ -n "$$(docker ps -q -f name=^/$(PG_CONTAINER)$$)" ]; then \
	  echo "Postgres $(PG_CONTAINER) already running."; \
	elif [ -n "$$(docker ps -aq -f name=^/$(PG_CONTAINER)$$)" ]; then \
	  docker start $(PG_CONTAINER); \
	else \
	  docker run -d --name $(PG_CONTAINER) \
	    -e POSTGRES_USER=nyansa -e POSTGRES_PASSWORD=nyansa -e POSTGRES_DB=nyansa \
	    -e PGDATA=/var/lib/postgresql/18/docker \
	    -p 5432:5432 mirror.gcr.io/library/postgres:18; \
	fi
	@echo "Postgres $(PG_CONTAINER) is up on localhost:5432"

db-down:
	-docker rm -f $(PG_CONTAINER)

db-logs:
	docker logs -f $(PG_CONTAINER)

# One-command interactive first-run setup: model, secrets, org, root account, CLI config.
init-dev:
	uv run nyansa-init --quick

# Connect this machine to a control plane an admin already runs (teammate onboarding).
join:
	@if [ -z "$(URL)" ]; then echo "Usage: make join URL=https://control-plane.example.com"; exit 1; fi
	uv run nyansa-init --join "$(URL)"

dev-setup: install init-dev ui-build
	@echo ""
	@echo "Setup complete."
	@echo "Open UI/API: http://$(HOST):$(PORT)"
	@echo "Run CLI: make run"

cli run agent:
	@$(LOAD_ENV); \
	uv run nyansa

root-api:
	curl -X POST "http://$(HOST):$(PORT)/bootstrap" \
	  -H 'content-type: application/json' \
	  -H "x-bootstrap-secret: $(BOOTSTRAP_SECRET)" \
	  -d '{"organization_name":"$(ORG_NAME)","root_user_id":"root","root_display_name":"Root User","root_token":"$(ROOT_TOKEN)"}'

# Create an additional platform superuser (root/platform_owner). Prints the token.
superuser:
	@if [ -z "$(USER_ID)" ]; then echo "Usage: make superuser USER_ID=alice"; exit 1; fi
	@$(LOAD_ENV); \
	uv run nyansa-control-admin create-superuser "$(USER_ID)"

run-api:
	@$(LOAD_ENV); \
	export NYANSA_DATABASE_URL="$${NYANSA_DATABASE_URL:-$(DATABASE_URL)}"; \
	NYANSA_PRODUCTION=false uv run uvicorn control_plane.api:app --host "$(HOST)" --port "$(PORT)"

run-ui:
	cd agent-ui && VITE_CONTROL_PLANE_URL="http://$(HOST):$(PORT)" $(NVM_BUN) run dev -- --port "$(UI_PORT)"

dev:
	@set -euo pipefail; \
	trap 'kill 0' EXIT; \
	$(LOAD_ENV); \
	export NYANSA_DATABASE_URL="$${NYANSA_DATABASE_URL:-$(DATABASE_URL)}"; \
	NYANSA_PRODUCTION=false uv run uvicorn control_plane.api:app --host "$(HOST)" --port "$(PORT)" & \
	cd agent-ui && VITE_CONTROL_PLANE_URL="http://$(HOST):$(PORT)" $(NVM_BUN) run dev -- --port "$(UI_PORT)" & \
	wait

prod-check: test-postgres typecheck ui-build vscode-check
	@echo "Production checks passed."

docker-up:
	@if [ ! -f .env ]; then cp .env.example .env; fi
	COMPOSE_BAKE=true docker compose up --build

# Production stack with automatic HTTPS via the bundled Caddy proxy.
docker-up-tls:
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@if [ -z "$(DOMAIN)" ]; then echo "Set DOMAIN, e.g. make docker-up-tls DOMAIN=cp.example.com"; exit 1; fi
	COMPOSE_BAKE=true DOMAIN="$(DOMAIN)" docker compose --profile tls up --build

docker-logs:
	docker compose logs -f

docker-down:
	docker compose down

clean-dev:
	@echo "Removing local dev key material under .dev"
	rm -rf .dev
	@echo "Stop Postgres with: make db-down"
