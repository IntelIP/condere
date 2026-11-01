# Security

## Reporting a vulnerability

Do not post credentials, private assessment data or exploit details in public issues.
Use the repository's GitHub private vulnerability reporting channel when available.
If it is unavailable, ask a maintainer for a private reporting channel without
including sensitive details. Private vulnerability reporting must be enabled before
public launch.

Include the affected revision, a minimal reproduction using synthetic data, the
expected security boundary and the observed impact. There is no published response-time
or security-support guarantee for this developer preview.

## Data boundaries

Self-hosting storage does not keep every input on your machine. Configured model and
research providers may receive repository content, analysis context or search queries.
Review their data handling and your rights to submit the material before admission.
Optional tracing can also send assessment context to the configured LangSmith workspace.

Repository inspection executes in isolated sandboxes. Provider credentials and host
mounts must remain outside those sandboxes, and assessment sandboxes run without
network access. Treat repository files and retrieved pages as untrusted evidence.

## Operating safely

- Keep `.env`, sign-in credentials, runtime state and assessment exports out of Git.
- Use separate application/checkpoint database roles, a dedicated evidence bucket,
  and scoped provider credentials.
- Bind a local API to loopback. Use HTTPS for remote access and protect bearer tokens;
  each token grants access as its configured owner.
- Register research authority explicitly. Intake and MCP tools cannot create or
  enlarge a campaign's spending allowance.
- Use dedicated test storage for integration checks; never point them at production.
- Stop services before moving local state, and preserve database and ownership records.

Report uncertain provider outcomes before retrying. A missing trace is not evidence
that no work occurred or that a request was free.
