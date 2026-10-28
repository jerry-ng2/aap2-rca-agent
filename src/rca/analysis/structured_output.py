"""Generate final JSON with the Messages API's constrained output format."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rca.config import Config

logger = logging.getLogger(__name__)

_MAX_OUTPUT_TOKENS = 16384
_VERTEX_MODEL_ALIASES = {
    "claude-haiku-4-5": "claude-haiku-4-5@20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-5@20250929",
    "claude-opus-4-5": "claude-opus-4-5@20251101",
}


@dataclass(frozen=True)
class StructuredOutputResult:
    data: Any
    usage: dict[str, Any]
    model: str


def _load_anthropic() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Native structured output requires the anthropic[vertex] runtime dependency"
        ) from exc
    return anthropic


def _normalize_schema_types(value: Any) -> Any:
    """Expand type arrays before the SDK transforms the copied wire schema."""
    if isinstance(value, list):
        return [_normalize_schema_types(item) for item in value]
    if not isinstance(value, dict):
        return value
    schema = {key: _normalize_schema_types(item) for key, item in value.items()}
    types = schema.get("type")
    if isinstance(types, list):
        # Retain each branch's properties and constraints. In particular, a
        # nullable object must keep its nested shape in the object branch.
        return {"anyOf": [{**schema, "type": item} for item in types]}
    return schema


def _wire_schema(anthropic: Any, schema: dict[str, Any]) -> dict[str, Any]:
    transform = getattr(anthropic, "transform_schema", None)
    if transform is None:
        raise RuntimeError(
            "Installed anthropic SDK lacks native schema transformation; "
            "install the project's current runtime dependencies"
        )
    return transform(_normalize_schema_types(copy.deepcopy(schema)))


def _enabled(environment: Mapping[str, str], name: str) -> bool:
    return environment.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _vertex_model(model: str) -> str:
    model = _VERTEX_MODEL_ALIASES.get(model, model)
    return re.sub(r"^(claude-.+)-(\d{8})$", r"\1@\2", model)


def _load_vertex_credentials(path: str) -> tuple[Any, str | None]:
    try:
        import google.auth
    except ImportError as exc:
        raise RuntimeError(
            "Vertex native structured output requires the anthropic[vertex] dependency"
        ) from exc
    return google.auth.load_credentials_from_file(
        str(Path(path).expanduser()), scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


async def _create_client(anthropic: Any, config: Config) -> tuple[Any, str, str]:
    environment = config.environment
    model = environment.get("CLAUDE_MODEL", "").strip()
    if not model:
        raise ValueError("CLAUDE_MODEL is required for native structured output")
    if _enabled(environment, "CLAUDE_CODE_USE_VERTEX"):
        region = environment.get("CLOUD_ML_REGION", "").strip()
        if not region:
            raise ValueError("CLOUD_ML_REGION is required for Vertex native structured output")
        kwargs: dict[str, Any] = {"region": region}
        project = environment.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
        if project:
            kwargs["project_id"] = project
        base_url = environment.get("ANTHROPIC_VERTEX_BASE_URL", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        credentials_path = environment.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if credentials_path:
            credentials, credentials_project = await asyncio.to_thread(
                _load_vertex_credentials, credentials_path
            )
            kwargs["credentials"] = credentials
            if not project and credentials_project:
                kwargs["project_id"] = credentials_project
        return anthropic.AsyncAnthropicVertex(**kwargs), _vertex_model(model), "vertex"

    if any(
        _enabled(environment, name)
        for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_FOUNDRY")
    ):
        raise ValueError("Native structured output currently supports Vertex and Anthropic endpoints")
    api_key = environment.get("ANTHROPIC_API_KEY") or None
    auth_token = environment.get("ANTHROPIC_AUTH_TOKEN") or None
    if not api_key and not auth_token:
        raise ValueError(
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for native structured output"
        )
    # Pass merged configuration explicitly: .env/settings values are not
    # exported to os.environ, and concurrent jobs must not mutate it.
    kwargs = {
        "api_key": api_key,
        "auth_token": None if api_key else auth_token,
        "base_url": environment.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
    }
    return anthropic.AsyncAnthropic(**kwargs), model, "anthropic"


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-JSON numeric constant: {value}")


async def generate_structured_output(
    prompt: str,
    config: Config,
    *,
    schema: dict[str, Any],
    system_prompt: str,
) -> StructuredOutputResult:
    """Call native constrained decoding; never retry with an unconstrained request."""
    anthropic = _load_anthropic()
    wire_schema = _wire_schema(anthropic, schema)
    client, model, backend = await _create_client(anthropic, config)
    logger.info(
        "Native structured output: backend=%s model=%s output_config.format=json_schema",
        backend,
        model,
    )
    async with client:
        response = await client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": wire_schema}},
        )
    if response.stop_reason != "end_turn":
        raise ValueError(
            "Native structured output did not complete "
            f"(stop_reason={response.stop_reason!r}); no summary was accepted"
        )
    if not response.content or any(block.type != "text" for block in response.content):
        raise ValueError("Native structured output returned no complete text-only JSON response")
    text = "".join(block.text for block in response.content)
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Native structured output returned invalid JSON") from exc
    return StructuredOutputResult(
        data=data,
        usage=response.usage.model_dump(exclude_none=True),
        model=str(response.model or model),
    )
