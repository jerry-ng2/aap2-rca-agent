"""Tests for SDK results and native summary validation before persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rca.analysis.pipeline import AnalysisArtifacts
from rca.batch import orchestrator
from rca.config import Config


def _config(tmp_path: Path) -> Config:
    return Config.from_env(
        environment={"RCA_STATE_DIR": str(tmp_path), "RCA_MAX_PARALLEL_JOBS": "1"},
        env_file=tmp_path / "missing.env",
    )


def _summary(job_id: str = "101") -> dict:
    return {
        "job_id": job_id,
        "root_cause": {
            "summary": "The job failed",
            "category": "unknown",
            "confidence": "low",
        },
        "correlation": {"method": "none", "identifiers": {}},
        "evidence": [
            {
                "source": "aap_job",
                "timestamp": "2026-10-28T13:15:00Z",
                "message": "The task failed",
            }
        ],
    }


def _result(
    output: object,
    *,
    cost: float = 0.02,
    is_error: bool = False,
    text: str = "summary returned",
) -> SimpleNamespace:
    return SimpleNamespace(
        is_error=is_error,
        total_cost_usd=cost,
        usage={"input_tokens": 20, "cache_creation": {"ephemeral_5m_input_tokens": 2}},
        model_usage={"claude-test": {"inputTokens": 20, "costUSD": cost}},
        result=text,
        structured_output=output,
        errors=["SDK request failed"] if is_error else [],
        session_id="test-session",
    )


class FakeSDK:
    class ResultError(Exception):
        pass

    def __init__(
        self,
        responses: list[SimpleNamespace | None],
        semaphore: asyncio.Semaphore,
        *,
        stream_error: Exception | None = None,
    ):
        self.responses = responses
        self.semaphore = semaphore
        self.stream_error = stream_error
        self.clients = []

    @staticmethod
    def ClaudeAgentOptions(**kwargs: object) -> dict[str, object]:
        return dict(kwargs)

    def ClaudeSDKClient(self, options: dict[str, object]):
        sdk = self

        class Client:
            def __init__(self):
                self.prompts: list[str] = []
                self.entered = 0
                self.exited = 0

            async def __aenter__(self):
                assert sdk.semaphore.locked()
                self.entered += 1
                return self

            async def __aexit__(self, *_args: object):
                assert sdk.semaphore.locked()
                self.exited += 1

            async def query(self, prompt: str):
                assert sdk.semaphore.locked()
                self.prompts.append(prompt)
                await asyncio.sleep(0)

            async def receive_response(self):
                assert sdk.semaphore.locked()
                response = sdk.responses[len(self.prompts) - 1]
                await asyncio.sleep(0)
                if response is not None:
                    yield response
                if sdk.stream_error is not None:
                    raise sdk.stream_error

        client = Client()
        self.clients.append(client)
        return client


async def _query_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    responses: list[SimpleNamespace],
):
    semaphore = asyncio.Semaphore(1)
    sdk = FakeSDK(responses, semaphore)
    monkeypatch.setattr(orchestrator, "_load_agent_sdk", lambda: sdk)
    result = await orchestrator._run_sdk_query(
        "Analyze AAP job 101",
        _config(tmp_path),
        cwd=tmp_path,
        semaphore=semaphore,
    )
    assert not semaphore.locked()
    return result, sdk


def test_generic_queries_do_not_decode_terminal_text_as_a_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, sdk = asyncio.run(
        _query_responses(
            monkeypatch,
            tmp_path,
            [_result(None, text=json.dumps(_summary()))],
        )
    )

    assert result.is_error is False
    assert result.structured_output is None
    assert len(sdk.clients[0].prompts) == 1


@pytest.mark.parametrize("sdk_error", [False, True])
def test_sdk_queries_preserve_structured_output_and_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk_error: bool
) -> None:
    output = {"historical_matches": [], "cross_job_patterns": []}

    async def run():
        semaphore = asyncio.Semaphore(1)
        sdk = FakeSDK([_result(output, is_error=sdk_error)], semaphore)
        monkeypatch.setattr(orchestrator, "_load_agent_sdk", lambda: sdk)
        result = await orchestrator._run_sdk_query(
            "Summarize results",
            _config(tmp_path),
            cwd=tmp_path,
            semaphore=semaphore,
        )
        return result, sdk

    result, sdk = asyncio.run(run())

    assert len(sdk.clients[0].prompts) == 1
    assert result.is_error is sdk_error
    assert result.structured_output == output


def test_missing_terminal_response_fails_and_releases_the_semaphore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run():
        semaphore = asyncio.Semaphore(1)
        sdk = FakeSDK([None], semaphore)
        monkeypatch.setattr(orchestrator, "_load_agent_sdk", lambda: sdk)
        with pytest.raises(RuntimeError, match="terminal result"):
            await orchestrator._run_sdk_query(
                "Analyze AAP job 101",
                _config(tmp_path),
                cwd=tmp_path,
                semaphore=semaphore,
            )
        assert len(sdk.clients[0].prompts) == 1
        assert not semaphore.locked()

    asyncio.run(run())


def test_sdk_stream_exit_preserves_its_terminal_error_without_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run():
        semaphore = asyncio.Semaphore(1)
        sdk = FakeSDK(
            [_result(None, is_error=True)],
            semaphore,
            stream_error=FakeSDK.ResultError("CLI exited nonzero"),
        )
        monkeypatch.setattr(orchestrator, "_load_agent_sdk", lambda: sdk)
        result = await orchestrator._run_sdk_query(
            "Analyze AAP job 101",
            _config(tmp_path),
            cwd=tmp_path,
            semaphore=semaphore,
        )
        assert len(sdk.clients[0].prompts) == 1
        assert not semaphore.locked()
        return result

    result = asyncio.run(run())

    assert result.is_error is True
    assert result.errors == ("SDK request failed",)
    assert result.session_id == "test-session"
    assert result.cost_usd == 0.02
    assert result.usage == {
        "input_tokens": 20,
        "cache_creation": {"ephemeral_5m_input_tokens": 2},
    }
    assert result.model_usage["claude-test"]["costUSD"] == 0.02


def test_python_saves_and_uploads_only_the_validated_structured_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    analysis_dir = config.analysis_dir / "101"
    analysis_dir.mkdir(parents=True)
    path = analysis_dir / "step5_analysis_summary.json"
    stale = _summary()
    stale["root_cause"]["summary"] = "Previous analysis"
    path.write_text(json.dumps(stale), encoding="utf-8")
    artifacts = AnalysisArtifacts("101", analysis_dir, {}, {}, {}, {})
    canonical = _summary()
    canonical["evidence"][0]["github_path"] = None

    async def query(*args: object, **kwargs: object):
        assert not path.exists()
        # A model-written file is not authoritative even when it is schema-valid.
        path.write_text(json.dumps(stale), encoding="utf-8")
        return orchestrator.SDKQueryResult(False, 0.02, {}, "Investigated artifacts")

    def upload(*args: object, **kwargs: object):
        assert json.loads(path.read_text()) == _summary()
        return True

    monkeypatch.setattr(orchestrator, "run_analysis", Mock(return_value=artifacts))
    monkeypatch.setattr(orchestrator, "_run_sdk_query", query)
    monkeypatch.setattr(
        orchestrator,
        "generate_structured_output",
        AsyncMock(return_value=SimpleNamespace(data=canonical, usage={}, model="native")),
    )
    uploader = Mock(side_effect=upload)
    monkeypatch.setattr(orchestrator, "upload_to_jumpbox", uploader)

    execution = asyncio.run(orchestrator._analyze_jobs([101], config, object(), cwd=tmp_path))[0]

    assert execution.status == "completed"
    assert execution.summary == _summary()
    assert json.loads(path.read_text()) == execution.summary
    uploader.assert_called_once()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_summary_rejects_nonfinite_values_even_in_schema_permitted_extra_fields(value: float) -> None:
    summary = _summary()
    summary["extra_context"] = {"value": value}

    with pytest.raises(ValueError):
        orchestrator._validate_step5_summary(summary, "101")


@pytest.mark.parametrize(
    ("problem", "expected_stage", "error_fragment"),
    [
        ("missing_output", "skill_execution", "JSON object"),
        ("wrong_job", "skill_execution", "job_id"),
        ("missing_timestamp", "skill_execution", "timestamp"),
        ("required_null", "skill_execution", "root_cause.summary"),
    ],
)
def test_invalid_structured_output_cannot_create_a_summary_or_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    problem: str,
    expected_stage: str,
    error_fragment: str,
) -> None:
    config = _config(tmp_path)
    analysis_dir = config.analysis_dir / "101"
    analysis_dir.mkdir(parents=True)
    artifacts = AnalysisArtifacts("101", analysis_dir, {}, {}, {}, {})
    summary = _summary()
    if problem == "wrong_job":
        summary["job_id"] = "999"
    elif problem == "missing_timestamp":
        del summary["evidence"][0]["timestamp"]
    elif problem == "required_null":
        summary["root_cause"]["summary"] = None
    else:
        summary = None

    async def investigate(*args: object, **kwargs: object):
        # A plausible model-written file must not bypass validation of its result.
        (analysis_dir / "step5_analysis_summary.json").write_text(
            json.dumps(_summary()), encoding="utf-8"
        )
        return orchestrator.SDKQueryResult(False, 0.02, {}, "Investigated artifacts")

    monkeypatch.setattr(orchestrator, "run_analysis", Mock(return_value=artifacts))
    monkeypatch.setattr(orchestrator, "_run_sdk_query", investigate)
    generator = AsyncMock(return_value=SimpleNamespace(data=summary, usage={}, model="native"))
    monkeypatch.setattr(orchestrator, "generate_structured_output", generator)
    upload = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "upload_to_jumpbox", upload)

    execution = asyncio.run(orchestrator._analyze_jobs([101], config, object(), cwd=tmp_path))[0]

    assert execution.status == "failed"
    assert execution.stage == expected_stage
    assert error_fragment in execution.error
    assert generator.await_count == 3
    assert not (analysis_dir / "step5_analysis_summary.json").exists()
    upload.assert_not_called()
