# Preview status

The public documentation describes the current implementation and the intended
local-to-cloud deployment path. It does not establish a completed public release.

| Area | Current state |
|---|---|
| Configuration | Environment-first setup; optional Doppler. Configuration smoke checks passed without starting services |
| Tracing | Optional LangSmith; keyless configuration and cleanup changes have focused test coverage |
| Local object storage | Existing MinIO integration. Garage is the intended replacement; provenance, atomic conditional-write compatibility, migration and Compose integration are pending |
| Platforms | macOS on Apple silicon is the tested local platform. Windows and Linux are untested and outside the release verification scope. Clean-install verification of the prepared macOS release candidate remains pending |
| Live demonstration | No live release demonstration has been completed with this candidate |
| Examples | No measured public example report is included yet. Test fixtures are not product demonstrations |
| CI | Public workflow is defined; it has not yet run on the new public repository |

No completion-time or cost-per-assessment estimate is published for this candidate.
Local storage can avoid cloud storage charges, but research and model usage may be
billable. Provider-free tests do not establish research quality or live integration.
