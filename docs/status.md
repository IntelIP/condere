# Preview status

Condere is a developer preview. The documentation describes the current implementation
and the intended local-to-cloud deployment path.

| Area | Current state |
|---|---|
| Configuration | Environment-first setup; optional Doppler. Configuration smoke checks passed without starting services |
| Tracing | Optional LangSmith; keyless configuration and cleanup changes have focused test coverage |
| Local object storage | Existing MinIO integration. Garage is the intended replacement; provenance, atomic conditional-write compatibility, migration and Compose integration are pending |
| Platforms | macOS on Apple silicon is the tested local platform. Windows and Linux are untested and outside the release verification scope. Clean-install verification of the prepared macOS release candidate remains pending |
| Live demonstration | No live release demonstration has been completed with this candidate |
| Examples | No measured public example report is included yet. Test fixtures are not product demonstrations |
| CI | [GitHub Actions](https://github.com/IntelIP/condere/actions/workflows/ci.yml) runs lint, provider-free tests, private-path protection, dead-code checks and full-history secret scanning. The first test run passed 81 tests; 99 opt-in integration tests were skipped |

No completion-time or cost-per-assessment estimate is published for this candidate.
Local storage can avoid cloud storage charges, but research and model usage may be
billable. Provider-free tests do not establish research quality or live integration.
