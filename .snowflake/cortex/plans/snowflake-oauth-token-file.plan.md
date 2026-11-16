---
name: "snowflake oauth token file"
created: "2026-11-15T19:58:57.393Z"
status: pending
---

# Plan: Snowflake OAuth token file (no PAT, no RSA)

## Goal

Run Auto-Ontology in Docker against `ffa68025` using Snowflake's built-in local-app OAuth (`SNOWFLAKE$LOCAL_APPLICATION`), which we verified on your account:

- Access token: 600 s (10 min). Refresh token: 7,776,000 s (90 days), reusable (not single-use).
- Token works for SQL, Cortex chat (`/api/v2/cortex/v1`) and Cortex embed (`/inference:embed`).
- Scope: `refresh_token session:role:FR_PRJ_PRINCIPAL_ENGINEER_RO`.

## How it fits together

```
Mac (host)                                         Docker
dev_tools/snowflake_oauth_token.py                 auto-ontology, ingestion-service
  login  : browser -> Okta + Duo (once / 90 days)    SNOWFLAKE_OAUTH_TOKEN_FILE=/run/snowflake-oauth/token
  refresh: every ~8 min, silent                      - Snowflake driver: authenticator=oauth, token_file_path
  keeps refresh token in macOS Keychain              - Cortex chat + embed: Bearer read from file per request
  writes access token -> ~/.auto-ontology/snowflake-oauth/token (0600)
                                  mounted read-only (directory) --------^
```

## Changes

### 1. Token helper (new): `dev_tools/snowflake_oauth_token.py`

- Runs on the Mac only, via `uv run --with keyring dev_tools/snowflake_oauth_token.py login|run|status` (inline deps, so `pyproject.toml` / `uv.lock` are not changed).
- `login`: OAuth authorization-code flow with PKCE against `https://<account>.snowflakecomputing.com/oauth/authorize` and `/oauth/token-request`, `client_id=LOCAL_APPLICATION`, redirect to `http://127.0.0.1:<port>`, scope `refresh_token session:role:<ROLE>`. Opens your browser once.
- Stores the refresh token in the macOS Keychain via `keyring` (never on disk, never printed).
- `run`: loop that refreshes \~2 minutes before `expires_in`, writes the access token atomically (temp file + rename, mode 0600). On `invalid_grant` it stops with a clear "run login again" message.
- `status`: shows token expiry and refresh-token age; no secrets.
- Settings via flags/env: account, user, role (default `FR_PRJ_PRINCIPAL_ENGINEER_RO`), output dir.

### 2. Snowflake connector

- `auto_ontology/connectors/snowflake.py`: accept `authenticator=oauth` in the URL. Then set `authenticator="oauth"` and `token_file_path` from env `SNOWFLAKE_OAUTH_TOKEN_FILE`; no password or key required. The path comes only from the environment, never from the URL/UI, so a connection cannot be used to read arbitrary files. Raise a clear error if the env var is unset or file missing.
- No `role` is passed: the token's scope fixes the role (passing a different one would fail).
- The connector already opens a new connection per query, so each connect reads the fresh token.
- `auto_ontology/connectors/connection_string_factory.py`: new boolean field `oauth_token_file`; when true, build `snowflake://USER@ACCOUNT?warehouse=..&database=..&authenticator=oauth` (checked before key/password, so stale stored credentials cannot override it).

### 3. Cortex model calls

- `auto_ontology/utils/model_config.py`: helper `cortex_bearer(static_key)` returns the token-file contents when `SNOWFLAKE_OAUTH_TOKEN_FILE` is set, else the static key.
- `auto_ontology/utils/llm_invoke.py` (`cortex/` branch): give `ChatOpenAI` an `httpx` client with an `httpx.Auth` hook that sets `Authorization: Bearer <file>` on every request, plus header `X-Snowflake-Authorization-Token-Type: OAUTH`. Long-lived clients therefore never hold a stale token.
- `auto_ontology/utils/embedding.py` (`_cortex_embed_batch`): read the bearer per request the same way.
- `.env`: `*_API_KEY` become a non-secret placeholder (`oauth`) so existing "is a key set" checks (e.g. `semantic_fk._build_retriever`) keep working.

### 4. Frontend

- `frontend/enums/connection.ts`, `frontend/types/connection.ts`: Snowflake checkbox "Use Snowflake OAuth (token file)", added to `CONNECTION_EITHER_FIELDS` so password/key become optional when it is ticked. Hint text explains the helper must be running.

### 5. Docker Compose (local only)

- New `docker-compose.override.yml` (gitignored; Compose loads it automatically) that, for `auto-ontology` and `ingestion-service`, mounts `${HOME}/.auto-ontology/snowflake-oauth` to `/run/snowflake-oauth` read-only and sets `SNOWFLAKE_OAUTH_TOKEN_FILE=/run/snowflake-oauth/token`.
- Mount the directory, not the file: the helper replaces the file atomically, and a single-file bind mount would keep pointing at the old file.

### 6. Tests and checks

- Unit tests: factory builds the OAuth URL and ignores stale credentials; connector parses `authenticator=oauth` into `token_file_path` and errors without the env var; Cortex chat/embed send the token from the file and pick up a changed file without rebuilding the client.
- `uv run ruff check/format`, `uv run pytest auto_ontology/utils auto_ontology/connectors`, `npx prettier --check` + `pnpm lint` on the two frontend files.
- Rebuild images, run helper `login` + `run`, recreate the connection in the UI with the checkbox, then verify: ingest one small table, a chat question, and a token refresh mid-run (file changes, calls keep working).

## Out of scope / risks

- Security sign-off: Snowflake documents local-app OAuth for apps on your computer; here the login and refresh stay on your Mac and Docker only receives the 10-minute token. Worth a quick OK.
- Admins can change `SNOWFLAKE$LOCAL_APPLICATION` (lifetimes, single-use refresh, disable) at any time.
- Laptop asleep > 10 min: calls fail until the helper refreshes on wake (self-recovers).
- Docker Desktop file sharing must let the container user read a 0600 file owned by you; verified in the end-to-end step, and the mode is relaxed to 0640 only if needed.
- The old PAT-based connection in Postgres is deleted and recreated with the new checkbox.
- `ingestion-service` and `auto-ontology` are recreated, which interrupts any running compilation.
