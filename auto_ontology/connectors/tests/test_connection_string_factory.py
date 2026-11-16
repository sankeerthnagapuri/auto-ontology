# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import base64
from urllib.parse import parse_qs, urlparse

import pytest

from auto_ontology.connectors.connection_string_factory import build_connection_string
from auto_ontology.connectors.snowflake import _parse_connection_string

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzk4OcbXcT\n"
    "-----END PRIVATE KEY-----\n"
)


def _snowflake(**overrides: object) -> dict[str, object]:
    connection = {
        "type": "snowflake",
        "account": "acct",
        "warehouse": "WH",
        "user": "svc_user",
        "database": "DB",
    }
    connection.update(overrides)
    return connection


def test_password_form_is_unchanged() -> None:
    """The old Snowflake account still authenticates with a password."""
    result = build_connection_string(_snowflake(password="p@ss/word"))

    assert result == "snowflake://svc_user:p%40ss%2Fword@acct?warehouse=WH&database=DB"


def test_private_key_omits_the_password_segment() -> None:
    result = build_connection_string(_snowflake(private_key=PEM))

    parsed = urlparse(result)
    assert parsed.password is None
    assert parsed.username == "svc_user"
    assert "private_key" in parse_qs(parsed.query)


def test_private_key_is_base64_encoded_in_the_url() -> None:
    """A raw PEM cannot survive a query string: parse_qs turns ``+`` into a space."""
    result = build_connection_string(_snowflake(private_key=PEM))

    encoded = parse_qs(urlparse(result).query)["private_key"][0]
    assert "BEGIN" not in encoded
    # Surrounding whitespace is trimmed; a PEM parses without a trailing newline.
    assert base64.urlsafe_b64decode(encoded).decode() == PEM.strip()


def test_already_encoded_key_is_not_double_encoded() -> None:
    """Rebuilding a connection string from a stored value must be idempotent."""
    encoded = base64.urlsafe_b64encode(PEM.encode()).decode()

    result = build_connection_string(_snowflake(private_key=encoded))

    assert parse_qs(urlparse(result).query)["private_key"][0] == encoded


def test_passphrase_is_carried_when_present() -> None:
    result = build_connection_string(
        _snowflake(private_key=PEM, private_key_passphrase="s3cret")
    )

    assert parse_qs(urlparse(result).query)["private_key_passphrase"] == ["s3cret"]


def test_passphrase_is_omitted_when_blank() -> None:
    result = build_connection_string(
        _snowflake(private_key=PEM, private_key_passphrase="   ")
    )

    assert "private_key_passphrase" not in parse_qs(urlparse(result).query)


def test_private_key_takes_precedence_over_a_stale_password() -> None:
    """A leftover password must not be sent to an account that rejects passwords."""
    result = build_connection_string(_snowflake(private_key=PEM, password="old"))

    assert urlparse(result).password is None


def test_missing_both_credentials_is_rejected() -> None:
    with pytest.raises(ValueError, match="password"):
        build_connection_string(_snowflake())


@pytest.mark.parametrize("field", ["account", "warehouse", "user", "database"])
def test_required_fields_are_still_enforced_for_key_pair(field: str) -> None:
    connection = _snowflake(private_key=PEM)
    connection[field] = ""

    with pytest.raises(ValueError, match=field):
        build_connection_string(connection)


def test_round_trip_reaches_the_driver_kwargs() -> None:
    """The builder's output must be consumable by the connector that receives it."""
    connection = _snowflake(private_key=PEM, private_key_passphrase="")
    encoded = parse_qs(urlparse(build_connection_string(connection)).query)

    assert encoded["warehouse"] == ["WH"]
    assert encoded["database"] == ["DB"]
    # The PEM above is a stub rather than a real key, so parsing must fail at the
    # key itself -- proving the value arrived intact rather than being mangled.
    with pytest.raises(ValueError, match="could not be parsed"):
        _parse_connection_string(build_connection_string(connection))


@pytest.mark.parametrize("flag", [True, "true"])
def test_oauth_token_file_needs_no_credential(flag: object) -> None:
    result = build_connection_string(_snowflake(oauth_token_file=flag))

    parsed = urlparse(result)
    assert parsed.password is None
    assert parse_qs(parsed.query)["authenticator"] == ["oauth"]


def test_oauth_token_file_overrides_stored_credentials() -> None:
    """Credentials merged back from storage must not override a switch to OAuth."""
    result = build_connection_string(
        _snowflake(oauth_token_file=True, private_key=PEM, password="old")
    )

    query = parse_qs(urlparse(result).query)
    assert urlparse(result).password is None
    assert "private_key" not in query
    assert query["authenticator"] == ["oauth"]


def test_oauth_round_trip_reads_the_token_file_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN_FILE", "/run/snowflake-oauth/token")

    kwargs, _, _ = _parse_connection_string(
        build_connection_string(_snowflake(oauth_token_file=True))
    )

    assert kwargs["authenticator"] == "oauth"
    assert kwargs["token_file_path"] == "/run/snowflake-oauth/token"
    assert "password" not in kwargs
    assert "private_key" not in kwargs


def test_oauth_without_token_file_env_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SNOWFLAKE_OAUTH_TOKEN_FILE", raising=False)

    with pytest.raises(ValueError, match="SNOWFLAKE_OAUTH_TOKEN_FILE"):
        _parse_connection_string(
            build_connection_string(_snowflake(oauth_token_file=True))
        )


def test_oauth_ignores_a_role_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token's scope fixes the role; sending another one would fail login."""
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN_FILE", "/tmp/token")

    kwargs, _, _ = _parse_connection_string(
        "snowflake://u@acct?warehouse=WH&database=DB&authenticator=oauth&role=OTHER"
    )

    assert "role" not in kwargs


def test_unchecked_oauth_still_requires_a_credential() -> None:
    with pytest.raises(ValueError, match="password"):
        build_connection_string(_snowflake(oauth_token_file=False))
