Analyze AAP job $job_id using the existing artifacts in `$analysis_dir`.

Investigate the failure and return concise findings grounded in these artifacts.
Include the root cause, confidence, correlation identifiers, evidence, and
actionable recommendations. Include the exact job ID `"$job_id"`.

Every evidence entry requires `source`, `timestamp`, and `message`, including
GitHub evidence. For configuration or code with no event timestamp in the
artifacts, use `"timestamp": ""` to indicate that the time is unavailable;
never invent a timestamp. Include `github_path` for GitHub evidence.

The orchestrator will supply your findings and the artifacts to a separate
Messages API request with a schema-constrained response format. That request
produces the final structured output. Python validates the response and writes
`$summary_path`. Do not write that file yourself or upload the analysis.

Use this summary schema to identify what evidence the final response needs:

```json
$summary_schema
```
