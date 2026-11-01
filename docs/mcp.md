# Use Condere over MCP

Start the API as described in [self-hosting](self-hosting.md). The local MCP transport
is stdio. Configure your coding client's MCP server with executable `uv` and arguments:

```json
["run", "--directory", "/absolute/path/to/condere", "--env-file", "/absolute/path/to/condere/.env", "python", "scripts/run_local.py", "mcp"]
```

The launcher's child process receives only `CONDERE_API_URL`, `CONDERE_API_TOKEN`,
`CONDERE_EXPORT_DIRECTORY` and a small set of OS bindings. Keep the environment file
private. For a remote API, use HTTPS; local HTTP is limited to loopback.

Start with `capabilities` and `list_assessments`. Use `assessment_status` to inspect
progress, then `read_report` for the compact JSON or Markdown. Fetch linked detail
with `read_artifact`, and follow pagination offsets. `read_source` and `read_original`
retrieve cited evidence; `export_assessment` writes a verified local bundle.

`admit_assessment` starts research within an existing operator-authorized campaign.
It needs the campaign ID, an idempotent request key and an intake brief. It does
not create spending authority. Cancellation and continuation also require an
explicit user action. Reuse saved reports when the task is retrieval.

Claude Code and Codex can launch this stdio process through their MCP configuration.
This guide covers the local stdio interface. Hosted transport and client-specific
installation flows are outside this guide. See [preview status](status.md) for
verification limits.
