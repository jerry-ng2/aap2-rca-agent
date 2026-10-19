# Issue 20: Remove the Skill indirection

## Goal

Remove the on-disk `root-cause-analysis` Skill from the runtime path and supply the same RCA guidance directly to the Agent SDK as a packaged system prompt.

## Current state

- `src/rca/batch/orchestrator.py` constructs `ClaudeAgentOptions` with
  `setting_sources=["user", "project"]`.
- The per-job path enables `skills=["root-cause-analysis"]`.
- `deploy/helm/templates/cronjob.yaml` rsyncs the Skill into
  `/workspace/.claude/skills/`.
- `deploy/run-eval.sh` clones the repository and invokes `claude -p` directly
  instead of exercising the installed Python entry point.

## Implementation design

### 1. Package the RCA instructions

- Add `src/rca/prompts/root_cause_analysis.md`.
- Load it with `importlib.resources.files("rca")` and expose it as
  `RCA_INSTRUCTIONS`.
- Add `prompts/*.md` to setuptools package data.
- Keep the prompt scoped to per-job Step 5 synthesis:
  - required input artifacts,
  - output filename,
  - non-interactive behavior,
  - analysis guidelines,
  - summary requirements,
  - explicit prohibition on rerunning Steps 1–4 or uploading.

### 2. Update Agent SDK options

Replace the `include_skill` boolean with an explicit `system_prompt` parameter:

```python
ClaudeAgentOptions(
    model=...,
    cwd=...,
    permission_mode="dontAsk",
    setting_sources=[],
    skills=[],
    env=...,
    system_prompt=RCA_INSTRUCTIONS,
    allowed_tools=[
        "Bash",
        "Read",
        "Write",
        "mcp__github__search_code",
        "mcp__get_file_contents",
    ],
)
```

- `setting_sources=[]` disables user, project, and local filesystem discovery.
- `skills=[]` explicitly suppresses skill discovery.
- `system_prompt` receives the packaged instructions only on the per-job path.
- The semantic aggregation query keeps `tools=[]` and no system prompt.
- `Skill` is not added to `allowed_tools`.

### 3. Replace the per-job prompt

- Rename `_skill_prompt` to `_synthesis_prompt`.
- Remove all Skill invocation language.
- Keep the direct job ID, artifact directory, and output filename in the user
  prompt.
- Preserve the existing `step5_analysis_summary.json` contract.

### 4. Remove deployment skill artifacts

- Delete `skills/root-cause-analysis/SKILL.md`.
- Remove the Skill rsync and installation log from
  `deploy/helm/templates/cronjob.yaml`.
- Keep `.claude/settings.json` and its secret because `Config` still loads
  runtime configuration from that path.
- Update the Dockerfile comment that still mentions the on-disk Skill.
- Rewrite `deploy/run-eval.sh` to invoke `rca-analyze analyze --job-id ...`
  inside the existing eval pod, matching the CronJob’s installed-package path.

### 5. Tests

Update `tests/test_batch_orchestrator.py` to:

- load `RCA_INSTRUCTIONS` from package data,
- assert `setting_sources == []`,
- assert `skills == []`,
- assert `system_prompt == RCA_INSTRUCTIONS` on the per-job path,
- assert `Skill` is absent from `allowed_tools`,
- confirm the semantic path still disables tools,
- confirm the per-job prompt no longer references Skill invocation.

Run:

```bash
.venv/bin/pytest -m "not integration"
helm lint deploy/helm -f deploy/helm/values.example.yaml
```

## Compatibility

- Existing JSON schemas remain unchanged.
- Public `rca-batch` and `rca-analyze` command-line interfaces remain unchanged.
- Report fields and stage names remain unchanged.
- `.claude/settings.json` remains the runtime configuration source.

## Acceptance criteria

- The batch path no longer depends on `.claude/skills/` or `SKILL.md`.
- The CronJob init container no longer copies a Skill into the workspace.
- The per-job Agent SDK query receives packaged `RCA_INSTRUCTIONS` directly.
- `deploy/run-eval.sh` exercises the installed Python entry point.
- Unit tests cover prompt loading and the new SDK options.
- The existing schemas, CLI commands, and deployment interfaces remain backward compatible.
