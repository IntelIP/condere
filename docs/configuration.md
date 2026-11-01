# Configuration

Use `uv run --env-file .env python scripts/run_local.py api` or `mcp`.
The launcher passes only component bindings to the child. `--doppler` explicitly
reads the Doppler CLI's configured project. Environment mode requires no Doppler.
The runtime has no dotenv parser. `.env.example` contains every server binding,
followed by MCP, local storage and Modal credential bindings.

The API token must contain at least 32 non-whitespace characters; use the identity
setup helper to generate it. Database URLs select separate application/checkpoint
roles and databases. Evidence settings select a provisioned S3-compatible bucket.
Artifact and export directories are operator-controlled local paths.

`CONDERE_MODEL_PROVIDER` is `chatgpt` (default) or `openrouter`. `CONDERE_SANDBOX`
is `smol` (default) or `modal`. Smol requires a verified archive or immutable image
digest. `CONDERE_EXECUTOR` is `inprocess` (default) or `cloud_run`; Cloud Run needs
a job resource name. A transaction-mode database pooler requires a direct database
URL for session locks. The Cloud ChatGPT secret is an optional Secret Manager
resource, not a plaintext token.

LangSmith is disabled without its API key. When the key is present, choose either
`LANGSMITH_PROJECT` or both `LANGSMITH_AGENT_ID` and `LANGSMITH_AGENT_ENVIRONMENT`.
Invalid destinations fail configuration rather than silently losing configured traces.
The launcher disables automatic global LangChain tracing; explicit worker tracing
uses the configured destination. Apify and GitHub tokens enable optional providers.

## REQUIRED

- `CONDERE_API_URL`
- `CONDERE_API_TOKEN`
- `CONDERE_OWNER_ID`
- `CONDERE_DATABASE_URL`
- `CONDERE_CHECKPOINT_URL`
- `CONDERE_EVIDENCE_ENDPOINT`
- `CONDERE_EVIDENCE_REGION`
- `CONDERE_EVIDENCE_ACCESS_KEY`
- `CONDERE_EVIDENCE_SECRET_KEY`
- `CONDERE_EVIDENCE_BUCKET`
- `CONDERE_ARTIFACT_DIRECTORY`
- `EXA_API_KEY`
- `CONDERE_OPENROUTER_API_KEY`

## PROVIDER

- `CONDERE_MODEL_PROVIDER`
- `CONDERE_CHATGPT_HOME`
- `APIFY_TOKEN`
- `GITHUB_TOKEN`

## CHATGPT_CLOUD

- `CONDERE_CHATGPT_SECRET`

## SANDBOX

- `CONDERE_SANDBOX`
- `CONDERE_SMOL_IMAGE`

## DATABASE

- `CONDERE_DATABASE_DIRECT_URL`

## EXECUTOR

- `CONDERE_EXECUTOR`
- `CONDERE_CLOUD_RUN_JOB`

## TRACING

- `LANGSMITH_API_KEY`
- `LANGSMITH_PROJECT`
- `LANGSMITH_AGENT_ID`
- `LANGSMITH_AGENT_ENVIRONMENT`
- `LANGSMITH_ENDPOINT`
- `LANGSMITH_WORKSPACE_ID`
