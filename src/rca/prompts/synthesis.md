Analyze AAP job $job_id using the existing artifacts.

Use the exact string `"$job_id"` for `job_id`. Follow the full JSON Schema below
for both the saved file and your final structured output. The required
top-level fields are `job_id`, `root_cause`, `correlation`, and `evidence`.
Submit the complete object itself, without a `findings`, `level`, or other
wrapper. Omit unavailable optional fields when their schema does not allow
`null`.

Write the complete summary to `$summary_path`, then read it back and check it
against the schema. Submit that same JSON object as your final structured
output. If validation reports a problem, correct the saved file before
resubmitting it.

JSON Schema:

```json
$summary_schema
```
