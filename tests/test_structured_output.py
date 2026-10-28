"""Native structured-output requests use constrained decoding on each backend."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anthropic
import pytest

from rca.analysis import structured_output
from rca.config import Config


def _config(tmp_path: Path, **environment: str) -> Config:
    return Config.from_env(
        environment={
            "RCA_STATE_DIR": str(tmp_path),
            "CLAUDE_MODEL": "claude-haiku-4-5",
            "ANTHROPIC_API_KEY": "test-api-key",
            **environment,
        },
        env_file=tmp_path / "missing.env",
    )


def _schema() -> dict:
    return json.loads(
        files("rca").joinpath("schemas", "summary.schema.json").read_text(encoding="utf-8")
    )


def _response(*, text: str = '{"job_id":"101"}', stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-haiku-4-5-20251001",
        usage=anthropic.types.Usage(input_tokens=12, output_tokens=5),
    )


def _fake_api(monkeypatch: pytest.MonkeyPatch, response=None):
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response or _response())))

    class Context:
        messages = client.messages

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    api = SimpleNamespace(
        transform_schema=anthropic.transform_schema,
        AsyncAnthropic=Mock(return_value=Context()),
        AsyncAnthropicVertex=Mock(return_value=Context()),
    )
    monkeypatch.setattr(structured_output, "_load_anthropic", lambda: api)
    return api, client.messages.create


def _generate(config: Config, *, schema: dict | None = None):
    return asyncio.run(
        structured_output.generate_structured_output(
            "Private investigation text",
            config,
            schema=_schema() if schema is None else schema,
            system_prompt="Use the supplied evidence",
        )
    )


def test_native_request_uses_output_config_and_preserves_canonical_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    api, create = _fake_api(monkeypatch)
    schema = _schema()
    original = copy.deepcopy(schema)
    caplog.set_level(logging.INFO, logger=structured_output.__name__)

    result = _generate(
        _config(tmp_path, ANTHROPIC_BASE_URL="https://gateway.example.test"), schema=schema
    )

    api.AsyncAnthropic.assert_called_once_with(
        api_key="test-api-key", auth_token=None, base_url="https://gateway.example.test"
    )
    api.AsyncAnthropicVertex.assert_not_called()
    request = create.call_args.kwargs
    assert request["model"] == "claude-haiku-4-5"
    assert request["system"] == "Use the supplied evidence"
    assert request["messages"] == [{"role": "user", "content": "Private investigation text"}]
    assert request["max_tokens"] == 16384
    assert "output_format" not in request
    wire = request["output_config"]["format"]
    assert wire["type"] == "json_schema"
    assert wire["schema"]["required"] == schema["required"]
    assert wire["schema"]["additionalProperties"] is False
    recommendation = wire["schema"]["properties"]["recommendations"]["items"]
    evidence_ref = recommendation["properties"]["evidence_ref"]
    assert evidence_ref["anyOf"][0]["type"] == "integer"
    assert "minimum" not in evidence_ref["anyOf"][0]
    assert "minimum: 0" in evidence_ref["anyOf"][0]["description"]
    fix = recommendation["properties"]["fix"]["anyOf"][0]
    assert fix["type"] == "object"
    assert set(fix["properties"]) == {"status", "base_sha", "diff", "pr_link"}
    assert fix["additionalProperties"] is False
    assert schema == original
    assert result.data == {"job_id": "101"}
    assert result.usage == {"input_tokens": 12, "output_tokens": 5}
    assert result.model == "claude-haiku-4-5-20251001"
    assert "output_config.format=json_schema" in caplog.text
    assert "Private investigation text" not in caplog.text


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-4-5", "claude-haiku-4-5@20251001"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5@20250929"),
        ("claude-opus-4-5", "claude-opus-4-5@20251101"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5@20251001"),
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5@20251001"),
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ],
)
def test_vertex_uses_native_client_and_documented_model_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str, expected: str
) -> None:
    api, create = _fake_api(monkeypatch)

    _generate(
        _config(
            tmp_path,
            CLAUDE_CODE_USE_VERTEX="true",
            CLAUDE_MODEL=model,
            CLOUD_ML_REGION="global",
            ANTHROPIC_VERTEX_PROJECT_ID="test-project",
        )
    )

    api.AsyncAnthropic.assert_not_called()
    api.AsyncAnthropicVertex.assert_called_once_with(region="global", project_id="test-project")
    assert create.call_args.kwargs["model"] == expected
    assert create.call_args.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_vertex_loads_configured_credentials_without_environment_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import google.auth

    api, _ = _fake_api(monkeypatch)
    credentials = object()
    load = Mock(return_value=(credentials, "credentials-project"))
    monkeypatch.setattr(google.auth, "load_credentials_from_file", load)
    before = dict(os.environ)
    credentials_path = tmp_path / "configured-credentials.json"

    _generate(
        _config(
            tmp_path,
            CLAUDE_CODE_USE_VERTEX="1",
            CLOUD_ML_REGION="us-east5",
            GOOGLE_APPLICATION_CREDENTIALS=str(credentials_path),
        )
    )

    load.assert_called_once_with(
        str(credentials_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    api.AsyncAnthropicVertex.assert_called_once_with(
        region="us-east5", project_id="credentials-project", credentials=credentials
    )
    assert dict(os.environ) == before


def test_installed_vertex_sdk_serializes_native_output_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    httpx2 = pytest.importorskip("httpx2")
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5@20251001",
                "content": [{"type": "text", "text": '{"job_id":"101"}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )

    def vertex_client(**kwargs):
        return anthropic.AsyncAnthropicVertex(
            **kwargs,
            access_token="test-token",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        )

    monkeypatch.setattr(
        structured_output,
        "_load_anthropic",
        lambda: SimpleNamespace(
            transform_schema=anthropic.transform_schema, AsyncAnthropicVertex=vertex_client
        ),
    )

    result = _generate(
        _config(
            tmp_path,
            CLAUDE_CODE_USE_VERTEX="1",
            CLOUD_ML_REGION="global",
            ANTHROPIC_VERTEX_PROJECT_ID="test-project",
        )
    )

    assert len(requests) == 1
    assert requests[0].url.path.endswith(
        "/publishers/anthropic/models/claude-haiku-4-5@20251001:rawPredict"
    )
    body = json.loads(requests[0].content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == _schema()["required"]
    assert result.data == {"job_id": "101"}


def test_anthropic_auth_token_and_default_endpoint_are_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api, _ = _fake_api(monkeypatch)

    _generate(_config(tmp_path, ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN="test-auth-token"))

    api.AsyncAnthropic.assert_called_once_with(
        api_key=None, auth_token="test-auth-token", base_url="https://api.anthropic.com"
    )


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "tool_use", "pause_turn"])
def test_incomplete_native_responses_fail_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop_reason: str
) -> None:
    _, create = _fake_api(monkeypatch, _response(stop_reason=stop_reason))

    with pytest.raises(ValueError, match=stop_reason):
        _generate(_config(tmp_path))

    create.assert_awaited_once()


@pytest.mark.parametrize("text", ["not JSON", "```json\n{}\n```", '{"value": NaN}', ""])
def test_invalid_json_is_rejected_without_repair_or_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> None:
    _, create = _fake_api(monkeypatch, _response(text=text))

    with pytest.raises(ValueError, match="invalid JSON"):
        _generate(_config(tmp_path))

    create.assert_awaited_once()


def test_tool_content_is_not_accepted_as_native_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = _response()
    response.content.append(SimpleNamespace(type="tool_use", input={"job_id": "101"}))
    _fake_api(monkeypatch, response)

    with pytest.raises(ValueError, match="text-only JSON"):
        _generate(_config(tmp_path))


def test_provider_error_does_not_trigger_unconstrained_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, create = _fake_api(monkeypatch)
    create.side_effect = RuntimeError("output_config.format is unsupported by gateway")

    with pytest.raises(RuntimeError, match="unsupported by gateway"):
        _generate(_config(tmp_path))

    create.assert_awaited_once()


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"CLAUDE_MODEL": ""}, "CLAUDE_MODEL"),
        ({"CLAUDE_CODE_USE_VERTEX": "1"}, "CLOUD_ML_REGION"),
        ({"ANTHROPIC_API_KEY": ""}, "ANTHROPIC_API_KEY"),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, "supports Vertex and Anthropic"),
    ],
)
def test_unsupported_configuration_fails_before_any_api_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: dict, message: str
) -> None:
    api, create = _fake_api(monkeypatch)

    with pytest.raises(ValueError, match=message):
        _generate(_config(tmp_path, **environment))

    api.AsyncAnthropic.assert_not_called()
    api.AsyncAnthropicVertex.assert_not_called()
    create.assert_not_awaited()
