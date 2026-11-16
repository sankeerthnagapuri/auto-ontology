# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "keyring>=25"]
# ///
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep a Snowflake OAuth access token on disk for Auto-Ontology containers.

For accounts that allow neither passwords, programmatic access tokens nor key
pairs. It uses Snowflake's built-in OAuth for local applications
(``SNOWFLAKE$LOCAL_APPLICATION``): you sign in once in a browser, and the
refresh token that comes back is kept in the macOS Keychain. ``run`` then swaps
it for a fresh access token before each one expires and writes only that
short-lived token to a file, which Docker Compose mounts read-only into the
containers (see ``SNOWFLAKE_OAUTH_TOKEN_FILE``).

Run it on the host, not in a container, because sign-in opens a browser::

    uv run dev_tools/snowflake_oauth_token.py login --account xy12345.us-east-1 --user ME --role MY_ROLE
    uv run dev_tools/snowflake_oauth_token.py run   --account xy12345.us-east-1 --user ME --role MY_ROLE
    uv run dev_tools/snowflake_oauth_token.py status

Every option can also come from the environment: ``SNOWFLAKE_ACCOUNT``,
``SNOWFLAKE_USER``, ``SNOWFLAKE_ROLE`` and ``SNOWFLAKE_OAUTH_TOKEN_DIR``.
Tokens are never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
import keyring

# Snowflake's built-in local-application client; it is a public client, so the
# id doubles as the secret for HTTP Basic auth.
CLIENT_ID = "LOCAL_APPLICATION"
KEYRING_SERVICE = "auto-ontology-snowflake-oauth"
TOKEN_FILE_NAME = "token"
STATE_FILE_NAME = "status.json"
# Refresh this long before the access token expires, so a request that reads the
# file just before a refresh never carries an expired token.
REFRESH_MARGIN_S = 120
LOGIN_TIMEOUT_S = 300


def _base_url(account: str) -> str:
    return f"https://{account}.snowflakecomputing.com"


def _keyring_user(account: str, user: str) -> str:
    return f"{account.upper()}:{user.upper()}"


def _token_dir(path: str) -> Path:
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _write_atomic(path: Path, content: str, mode: int) -> None:
    """Replace *path* in one step, so a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _token_request(account: str, data: dict[str, str]) -> dict:
    response = httpx.post(
        f"{_base_url(account)}/oauth/token-request",
        data=data,
        auth=(CLIENT_ID, CLIENT_ID),
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        error = body.get("error") or body.get("message") or response.text[:200]
        raise RuntimeError(
            f"token request failed (HTTP {response.status_code}): {error}"
        )
    return body


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result = {key: values[0] for key, values in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Signed in. You can close this tab.")

    def log_message(self, *args: object) -> None:
        return


def login(account: str, user: str, role: str, token_dir: str) -> None:
    """Browser sign-in; stores the refresh token and writes the first access token."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(32)

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = f"{_base_url(account)}/oauth/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": f"refresh_token session:role:{role}",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": user,
        }
    )
    print("Opening your browser to sign in to Snowflake...")
    if not webbrowser.open(url):
        print(f"Open this URL to sign in:\n{url}")

    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while not _CallbackHandler.result and time.monotonic() < deadline:
        time.sleep(0.2)
    server.server_close()
    result = _CallbackHandler.result
    if not result:
        raise SystemExit("Sign-in timed out.")
    if result.get("state") != state:
        raise SystemExit("Sign-in failed: state mismatch.")
    if "code" not in result:
        raise SystemExit(
            f"Sign-in failed: {result.get('error_description') or result.get('error')}"
        )

    body = _token_request(
        account,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    if "refresh_token" not in body:
        raise SystemExit(
            "Snowflake did not issue a refresh token (OAUTH_ISSUE_REFRESH_TOKENS may be off); "
            "background refresh is not possible."
        )
    keyring.set_password(
        KEYRING_SERVICE, _keyring_user(account, user), body["refresh_token"]
    )
    _save(Path(_token_dir(token_dir)), body, refresh_issued=True)
    days = int(body.get("refresh_token_expires_in", 0)) // 86400
    print(
        f"Signed in as {body.get('username', user)}. Refresh token kept in the Keychain (~{days} days)."
    )


def _save(directory: Path, body: dict, *, refresh_issued: bool) -> int:
    """Write the access token and a secret-free status file; returns seconds until expiry."""
    expires_in = int(body.get("expires_in", 600))
    _write_atomic(directory / TOKEN_FILE_NAME, body["access_token"], 0o600)
    status_path = directory / STATE_FILE_NAME
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    now = int(time.time())
    status.update(
        {
            "access_token_expires_at": now + expires_in,
            "scope": body.get("scope", status.get("scope")),
            "username": body.get("username", status.get("username")),
        }
    )
    if refresh_issued:
        status["refresh_token_issued_at"] = now
        status["refresh_token_expires_at"] = now + int(
            body.get("refresh_token_expires_in", 0)
        )
    _write_atomic(status_path, json.dumps(status, indent=2), 0o600)
    return expires_in


def refresh_once(account: str, user: str, token_dir: str) -> int:
    """Swap the stored refresh token for a new access token; returns seconds until expiry."""
    refresh_token = keyring.get_password(KEYRING_SERVICE, _keyring_user(account, user))
    if not refresh_token:
        raise SystemExit("No refresh token in the Keychain. Run `login` first.")
    body = _token_request(
        account, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    # Single-use refresh tokens come back rotated; keep the new one.
    rotated = body.get("refresh_token")
    if rotated:
        keyring.set_password(KEYRING_SERVICE, _keyring_user(account, user), rotated)
    return _save(Path(_token_dir(token_dir)), body, refresh_issued=bool(rotated))


def run(account: str, user: str, token_dir: str) -> None:
    """Refresh forever, a little before each access token expires."""
    print(
        f"Writing access tokens to {Path(token_dir).expanduser() / TOKEN_FILE_NAME}. Ctrl-C to stop."
    )
    failures = 0
    while True:
        try:
            expires_in = refresh_once(account, user, token_dir)
            failures = 0
            print(
                f"{time.strftime('%H:%M:%S')} refreshed; valid for {expires_in}s",
                flush=True,
            )
            time.sleep(max(30, expires_in - REFRESH_MARGIN_S))
        except RuntimeError as exc:
            if "invalid_grant" in str(exc):
                raise SystemExit(
                    f"{exc}\nThe refresh token is no longer valid. Run `login` again."
                )
            failures += 1
            wait = min(300, 15 * 2 ** min(failures, 4))
            print(
                f"{time.strftime('%H:%M:%S')} refresh failed ({exc}); retrying in {wait}s",
                flush=True,
            )
            time.sleep(wait)
        except httpx.HTTPError as exc:
            # Network down or laptop just woke: retry until it comes back.
            failures += 1
            wait = min(300, 15 * 2 ** min(failures, 4))
            print(
                f"{time.strftime('%H:%M:%S')} network error ({type(exc).__name__}); retrying in {wait}s",
                flush=True,
            )
            time.sleep(wait)


def status(token_dir: str) -> None:
    path = Path(token_dir).expanduser() / STATE_FILE_NAME
    if not path.exists():
        raise SystemExit("No status yet. Run `login` first.")
    info = json.loads(path.read_text())
    now = int(time.time())

    def left(key: str) -> str:
        at = info.get(key)
        if not at:
            return "unknown"
        seconds = at - now
        if seconds <= 0:
            return "expired"
        return (
            f"{seconds // 86400}d {seconds % 86400 // 3600}h"
            if seconds > 3600
            else f"{seconds // 60}m"
        )

    print(f"user:          {info.get('username')}")
    print(f"scope:         {info.get('scope')}")
    print(f"access token:  {left('access_token_expires_at')} left")
    print(f"refresh token: {left('refresh_token_expires_at')} left")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["login", "run", "refresh", "status"])
    parser.add_argument("--account", default=os.environ.get("SNOWFLAKE_ACCOUNT"))
    parser.add_argument("--user", default=os.environ.get("SNOWFLAKE_USER"))
    parser.add_argument("--role", default=os.environ.get("SNOWFLAKE_ROLE"))
    parser.add_argument(
        "--token-dir",
        default=os.environ.get(
            "SNOWFLAKE_OAUTH_TOKEN_DIR", "~/.auto-ontology/snowflake-oauth"
        ),
    )
    args = parser.parse_args()

    if args.command == "status":
        status(args.token_dir)
        return
    missing = [name for name in ("account", "user") if not getattr(args, name)]
    if args.command == "login" and not args.role:
        missing.append("role")
    if missing:
        parser.error("missing " + ", ".join(f"--{name}" for name in missing))

    try:
        if args.command == "login":
            login(args.account, args.user, args.role, args.token_dir)
        elif args.command == "refresh":
            print(
                f"refreshed; valid for {refresh_once(args.account, args.user, args.token_dir)}s"
            )
        else:
            run(args.account, args.user, args.token_dir)
    except KeyboardInterrupt:
        sys.exit(0)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
