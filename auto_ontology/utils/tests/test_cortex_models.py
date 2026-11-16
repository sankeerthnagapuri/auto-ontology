# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Snowflake Cortex routing for chat, embedding, and rerank."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from auto_ontology.utils import embedding, llm_invoke, rerank
from auto_ontology.utils.embedding import EmbedParams, embed_texts

_ACCOUNT = "https://acct.snowflakecomputing.com"


def test_cortex_chat_uses_openai_client_and_strips_prefix() -> None:
    from langchain_openai import ChatOpenAI

    client = llm_invoke._build_client(
        model="cortex/claude-sonnet-4-5",
        api_key="pat",
        base_url=f"{_ACCOUNT}/api/v2/cortex/v1",
        temperature=0.0,
        max_tokens=100,
    )
    assert isinstance(client, ChatOpenAI)
    assert client.model_name == "claude-sonnet-4-5"
    assert llm_invoke._is_claude(client)


def test_cortex_claude_forces_tool_choice() -> None:
    cortex = llm_invoke._build_client(
        model="cortex/claude-haiku-4-5",
        api_key="pat",
        base_url=f"{_ACCOUNT}/api/v2/cortex/v1",
        temperature=0.0,
        max_tokens=100,
    )
    gateway = llm_invoke._build_client(
        model="aws/anthropic/claude-opus",
        api_key="key",
        base_url="https://gateway.example.com/v1",
        temperature=0.0,
        max_tokens=100,
    )
    assert llm_invoke._structured_output_kwargs(cortex) == {
        "method": "function_calling"
    }
    assert llm_invoke._structured_output_kwargs(gateway) == {
        "method": "function_calling",
        "tool_choice": None,
    }


def _fake_post(calls: list[dict[str, Any]], status: int = 200):
    def post(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        body = {
            "data": [
                {"index": i, "embedding": [[float(i), 1.0]]}
                for i in range(len(json["text"]))
            ]
        }
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    return post


def test_cortex_embed_request_and_unwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(embedding.httpx, "post", _fake_post(calls))
    params = EmbedParams(
        model_name="cortex/snowflake-arctic-embed-l-v2.0",
        embed_invoke_url=_ACCOUNT,
        api_key="pat",
    )

    vectors = embed_texts(["a", "", "b"], params, "query")

    assert vectors == [[0.0, 1.0], None, [1.0, 1.0]]
    assert calls[0]["url"] == f"{_ACCOUNT}/api/v2/cortex/inference:embed"
    assert calls[0]["json"] == {
        "model": "snowflake-arctic-embed-l-v2.0",
        "text": ["query: a", "query: b"],
    }
    assert calls[0]["headers"]["Authorization"] == "Bearer pat"


def test_cortex_embed_passage_has_no_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(embedding.httpx, "post", _fake_post(calls))
    params = EmbedParams(
        model_name="cortex/snowflake-arctic-embed-l-v2.0",
        embed_invoke_url=f"{_ACCOUNT}/api/v2/cortex/inference:embed",
        api_key="pat",
    )

    embed_texts(["a"], params, "passage")

    assert calls[0]["json"]["text"] == ["a"]


def test_cortex_embed_client_error_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding.httpx, "post", _fake_post([], status=403))
    params = EmbedParams(
        model_name="cortex/e5-base-v2", embed_invoke_url=_ACCOUNT, api_key="pat"
    )

    with pytest.raises(httpx.HTTPStatusError) as info:
        embed_texts(["a"], params, "passage", retries=0)
    assert embedding._is_fatal(info.value)


def test_rerank_disabled_raises() -> None:
    with pytest.raises(RuntimeError):
        rerank.rerank_hits(
            "q", [{"text": "x"}], rerank_invoke_url="disabled", model_name=""
        )


def _token_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, token: str) -> Any:
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN_FILE", str(path))
    return path


def test_cortex_embed_uses_the_oauth_token_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _token_file(tmp_path, monkeypatch, "oauth-1\n")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(embedding.httpx, "post", _fake_post(calls))
    params = EmbedParams(
        model_name="cortex/e5-base-v2", embed_invoke_url=_ACCOUNT, api_key="oauth"
    )

    embed_texts(["a"], params, "passage")
    path.write_text("oauth-2")
    embed_texts(["b"], params, "passage")

    assert calls[0]["headers"]["Authorization"] == "Bearer oauth-1"
    assert calls[0]["headers"]["X-Snowflake-Authorization-Token-Type"] == "OAUTH"
    # A refreshed file is picked up on the next request, with no client rebuild.
    assert calls[1]["headers"]["Authorization"] == "Bearer oauth-2"


def test_cortex_embed_without_token_file_keeps_the_static_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SNOWFLAKE_OAUTH_TOKEN_FILE", raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(embedding.httpx, "post", _fake_post(calls))
    params = EmbedParams(
        model_name="cortex/e5-base-v2", embed_invoke_url=_ACCOUNT, api_key="pat"
    )

    embed_texts(["a"], params, "passage")

    assert calls[0]["headers"]["Authorization"] == "Bearer pat"
    assert "X-Snowflake-Authorization-Token-Type" not in calls[0]["headers"]


def test_cortex_chat_auth_reads_the_token_file_per_request(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _token_file(tmp_path, monkeypatch, "oauth-1")
    client = llm_invoke._build_client(
        model="cortex/claude-haiku-4-5",
        api_key="oauth",
        base_url=f"{_ACCOUNT}/api/v2/cortex/v1",
        temperature=0.0,
        max_tokens=100,
    )
    auth = client.http_client.auth
    assert isinstance(auth, llm_invoke._CortexAuth)

    def sent(request: httpx.Request) -> httpx.Request:
        return next(auth.auth_flow(request))

    first = sent(httpx.Request("POST", f"{_ACCOUNT}/api/v2/cortex/v1/chat/completions"))
    path.write_text("oauth-2")
    second = sent(
        httpx.Request("POST", f"{_ACCOUNT}/api/v2/cortex/v1/chat/completions")
    )

    assert first.headers["Authorization"] == "Bearer oauth-1"
    assert first.headers["X-Snowflake-Authorization-Token-Type"] == "OAUTH"
    assert second.headers["Authorization"] == "Bearer oauth-2"


def test_empty_token_file_is_an_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_ontology.utils.model_config import cortex_bearer

    _token_file(tmp_path, monkeypatch, "  \n")

    with pytest.raises(RuntimeError, match="empty"):
        cortex_bearer("pat")
