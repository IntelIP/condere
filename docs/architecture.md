# Architecture

Condere is a trusted Python runtime with HTTP and MCP interfaces. The MCP process
forwards authenticated requests to the API; it receives API bindings, not research
provider or database credentials. Both interfaces read the same saved assessments.

The operator registers campaign authority before admission. Admission binds an
assessment to its owner and campaign. Spending reservations, settled costs,
uncertain provider outcomes and assessment limits live in PostgreSQL. An uncertain
charge is reconciled before retry; a request cannot enlarge campaign authority.

The coordinator runs research with Deep Agents and PostgreSQL checkpoints. The
agent bundle lives under `agent/`: instructions, skills, references and operator
context. Assessment-specific evidence is kept separate from that context.

Repository archives are admitted and checked by the trusted runtime. SmolVM or
Modal executes inspection in an offline sandbox without provider credentials or
host mounts. Provider calls and evidence downloads happen in the trusted runtime.
Repository contents and retrieved pages are evidence, never operator instructions.

Evidence originals and extracted text are stored in an S3-compatible object store.
PostgreSQL stores ownership, hashes and records. The current local store is MinIO;
Garage is the intended self-hosted successor, subject to verification and migration.
The hosted deployment uses Cloudflare R2. Quotes must match saved sources;
artifact hashes and access checks protect later retrieval. An assessment can be
retrieved without repeating research.

A saved `Analysis` feeds the typed Jev decision interface. Code applies decision
gates and records adjustments; a build plan cannot grant implementation authority.
The report renderer creates the compact JSON and linked supporting artifacts.

LangSmith tracing is optional observability. PostgreSQL and evidence storage stay
authoritative when traces are disabled or unavailable. Without `LANGSMITH_API_KEY`,
no tracing client is constructed by the runtime.

The default executor runs inside the API process. Cloud Run Jobs and Secret Manager
adapters are available for operators building their own deployment; deployment
configuration and credentials are not part of this public release.
