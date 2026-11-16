# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# ---------------------------------------------------------------------------
# Auto Ontology image build / publish helpers
# ---------------------------------------------------------------------------
# These targets wrap the `docker buildx build` invocations needed to build
# the backend and frontend images and (optionally) push them to NVIDIA's NGC
# container registry. See `helm/auto-ontology/README.md` for the full deploy story;
# this file only covers building and publishing the images.
#
# Common flow (one-time):
#
#   docker login nvcr.io                       # or: make login NGC_TOKEN=...
#   make build  REGISTRY=nvcr.io/<org>/<team>  # local builds (single arch)
#   make publish REGISTRY=nvcr.io/<org>/<team> # multi-arch push
#
# Variables (override on the command line):
#
#   REGISTRY        Full nvcr.io path, e.g. nvcr.io/nvidian/sw-auto-ontology
#                   (required for `publish*` targets)
#   TAG             Image tag. Defaults to `git describe`.
#   PYTHON_API_URL  Backend URL baked into the frontend's Next.js rewrites
#                   at build time. Defaults to http://auto-ontology:3001
#                   (matches the in-cluster service the helm chart creates).
#   PLATFORMS       Comma-separated platforms for multi-arch publish.
#                   Defaults to linux/amd64,linux/arm64.
# ---------------------------------------------------------------------------

REGISTRY       ?=
TAG            ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
PYTHON_API_URL ?= http://auto-ontology:3001
PLATFORMS      ?= linux/amd64,linux/arm64

BACKEND_IMAGE  := auto-ontology
FRONTEND_IMAGE := auto-ontology-frontend

# --tag prefix. Plain image name when REGISTRY is empty (local builds);
# fully-qualified when REGISTRY is set (publish builds).
ifeq ($(REGISTRY),)
  BACKEND_REF  := $(BACKEND_IMAGE):$(TAG)
  FRONTEND_REF := $(FRONTEND_IMAGE):$(TAG)
else
  BACKEND_REF  := $(REGISTRY)/$(BACKEND_IMAGE):$(TAG)
  FRONTEND_REF := $(REGISTRY)/$(FRONTEND_IMAGE):$(TAG)
endif

.PHONY: help login builder build build build-frontend \
        publish publish publish-frontend

help:
	@echo "Auto Ontology image targets:"
	@echo "  make build               Build both images locally (host arch only)"
	@echo "  make build       Build backend image locally"
	@echo "  make build-frontend      Build frontend image locally"
	@echo "  make publish             Build + push both images (multi-arch)"
	@echo "  make publish     Build + push backend image"
	@echo "  make publish-frontend    Build + push frontend image"
	@echo "  make login NGC_TOKEN=... docker login nvcr.io via NGC API token"
	@echo ""
	@echo "Schema targets (backend owns the \`auto_ontology\` schema; Prisma owns \`public\`):"
	@echo "  make migrate             Apply Alembic migrations (alembic upgrade head)"
	@echo "  make migrate-check       Fail if the schema has drifted from the metadata"
	@echo "  make migrate-revision m=\"...\"  Autogenerate a revision from auto_ontology/dal/schema.py"
	@echo
	@echo "Snowflake OAuth token (local Docker; settings from .env):"
	@echo "  make sf-login            Browser sign-in once (Okta/Duo); refresh token -> Keychain"
	@echo "  make sf-token            Keep the access token refreshed (leave running)"
	@echo "  make sf-status           Show token expiry (no secrets)"
	@echo "  make up                  Refresh once, then docker compose up -d"
	@echo "  make mcp                 Run the MCP server (AUTO_ONTOLOGY_API_URL=$(AUTO_ONTOLOGY_API_URL))"
	@echo
	@echo "Required for publish*: REGISTRY=nvcr.io/<org>/<team>"
	@echo "Current REGISTRY=$(REGISTRY) TAG=$(TAG)"

login:
	@if [ -z "$(NGC_TOKEN)" ]; then \
	  echo "Usage: make login NGC_TOKEN=<your NGC API token>"; \
	  echo "Get a token at https://ngc.nvidia.com/setup/api-key"; \
	  exit 2; \
	fi
	@echo "$(NGC_TOKEN)" | docker login nvcr.io --username '$$oauthtoken' --password-stdin

# Ensure a docker-container builder exists for multi-arch builds.
builder:
	@docker buildx inspect auto-ontology-builder >/dev/null 2>&1 \
	  || docker buildx create --name auto-ontology-builder --driver docker-container --use
	@docker buildx use auto-ontology-builder

build:
	docker buildx build -t $(BACKEND_REF) --load .

build-frontend:
	docker buildx build --build-arg PYTHON_API_URL=$(PYTHON_API_URL) \
	    -t $(FRONTEND_REF) --load ./frontend

build: build build-frontend

publish: require-registry builder
	docker buildx build --platform $(PLATFORMS) \
	    -t $(BACKEND_REF) --push .

publish-frontend: require-registry builder
	docker buildx build --platform $(PLATFORMS) \
	    --build-arg PYTHON_API_URL=$(PYTHON_API_URL) \
	    -t $(FRONTEND_REF) --push ./frontend

publish: publish publish-frontend

# Internal: fail fast if REGISTRY is missing on a publish target.
.PHONY: require-registry
require-registry:
	@if [ -z "$(REGISTRY)" ]; then \
	  echo "ERROR: REGISTRY is required for publish targets."; \
	  echo "       e.g. make publish REGISTRY=nvcr.io/nvidian/sw-auto-ontology"; \
	  exit 2; \
	fi

# --- Schema migrations -------------------------------------------------
# The backend owns the `auto_ontology` Postgres schema. Prisma owns `public` and migrates
# separately (frontend/prisma/migrate.sh); the two are independent.

migrate:
	uv run alembic upgrade head

# Fails when auto_ontology/dal/schema.py and the database disagree. Run this in CI:
# a schema change committed without its migration is invisible until deploy.
migrate-check:
	uv run alembic check

migrate-revision:
	@test -n "$(m)" || (echo 'usage: make migrate-revision m="what changed"' >&2; exit 2)
	uv run alembic revision --autogenerate -m "$(m)"

.PHONY: migrate migrate-check migrate-revision

# --- Snowflake OAuth token (local Docker, no PAT / key pair) ----------------
# dev_tools/snowflake_oauth_token.py runs on the host: it signs in once in a
# browser, keeps the refresh token in the macOS Keychain and writes a fresh
# access token for the containers (see docker-compose.override.yml).
# Settings come from the command line or from these keys in .env:
#   SNOWFLAKE_ACCOUNT=xy12345.us-east-1  SNOWFLAKE_USER=ME  SNOWFLAKE_ROLE=MY_ROLE
# Only these keys are read from .env, so other values there are never parsed by make.

_env = $(shell grep -E '^$(1)=' .env 2>/dev/null | tail -1 | cut -d= -f2-)
SNOWFLAKE_ACCOUNT ?= $(call _env,SNOWFLAKE_ACCOUNT)
SNOWFLAKE_USER    ?= $(call _env,SNOWFLAKE_USER)
SNOWFLAKE_ROLE    ?= $(call _env,SNOWFLAKE_ROLE)
SF_TOKEN := uv run dev_tools/snowflake_oauth_token.py

sf-login: require-snowflake
	@test -n "$(SNOWFLAKE_ROLE)" || (echo 'SNOWFLAKE_ROLE is required for sf-login' >&2; exit 2)
	$(SF_TOKEN) login --account "$(SNOWFLAKE_ACCOUNT)" --user "$(SNOWFLAKE_USER)" --role "$(SNOWFLAKE_ROLE)"

# Foreground refresh loop; keep this terminal open while the stack runs.
sf-token: require-snowflake
	$(SF_TOKEN) run --account "$(SNOWFLAKE_ACCOUNT)" --user "$(SNOWFLAKE_USER)"

sf-refresh: require-snowflake
	$(SF_TOKEN) refresh --account "$(SNOWFLAKE_ACCOUNT)" --user "$(SNOWFLAKE_USER)"

sf-status:
	@$(SF_TOKEN) status

# Refresh once (so the file is fresh), then start the stack.
up: sf-refresh
	docker compose up -d

# Read-only MCP server for agents, run from the local checkout. Point it at the
# web app (Compose UI on :3000), not FastAPI on :3001.
AUTO_ONTOLOGY_API_URL ?= http://localhost:3000

mcp:
	cd mcp && AUTO_ONTOLOGY_API_URL=$(AUTO_ONTOLOGY_API_URL) uvx --from . auto-ontology-mcp

require-snowflake:
	@if [ -z "$(SNOWFLAKE_ACCOUNT)" ] || [ -z "$(SNOWFLAKE_USER)" ]; then \
	  echo "Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env or on the command line." >&2; \
	  exit 2; \
	fi

.PHONY: sf-login sf-token sf-refresh sf-status up mcp require-snowflake


