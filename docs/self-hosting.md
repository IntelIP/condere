# Self-hosting

This guide covers the current native development setup. A complete Garage-based
Compose setup is not available yet. Clean-install and full sandbox assessment
verification remain pending; see [preview status](status.md).

## Configuration

Install Python 3.12 or 3.13 and uv, then run `uv sync --locked --group dev`.
Copy `.env.example` to `.env`. Fill in the required keys; never commit `.env`.
Use uv's `--env-file` option to supply environment values. Condere does not load
configuration files inside its runtime.

Generate local storage credentials without contacting providers:

```sh
uv run --env-file .env python scripts/configure_storage.py
uv run --env-file .env python scripts/configure_evidence.py
```

These commands fill or update generated bindings in `.env`, preserve unrelated
settings and restrict the file permissions.
To use existing Doppler configuration, add `--doppler` after configuring the Doppler
CLI for your own project. No production project name is built into these helpers.

## Storage

Use separate PostgreSQL application and checkpoint databases with separate roles.
The native development launcher requires `initdb` and `pg_ctl` on PATH:

```sh
uv run --env-file .env python scripts/start_storage.py
```

It listens only on `127.0.0.1:15434`. `--stop` stops this cluster and retains data.
It refuses to adopt a directory without its ownership marker.

Evidence requires an S3-compatible service with conditional object writes. The
existing native MinIO build record is `infra/evidence/minio.json`; its binary hash
is specific to that recorded platform. The upstream community repository is
archived. Garage is the intended long-term self-hosted replacement for local
development. Its provenance and Condere's evidence-storage compatibility, including
atomic write-if-absent behavior, must be verified before migration. Garage is not
yet integrated or tested here; the existing implementation still uses MinIO.
Cloud production uses R2. Keeping storage self-hosted locally avoids requiring a
cloud storage account and cloud storage charges during development.

If you already have the recorded native MinIO binary, start it in a separate terminal:

```sh
uv run --env-file .env python scripts/start_evidence.py
```

The launcher verifies that binary against the platform-specific build record and
retains its data when stopped. It does not download or build MinIO. A fresh clone
without that binary needs an independently provisioned, compatible evidence store;
the planned Garage setup will replace this manual step after verification.

Once the local service is running on `127.0.0.1:19000`, initialize the bucket:

```sh
uv run --env-file .env python scripts/configure_evidence.py --initialize-bucket
```

For an operator-managed remote store, provision its bucket separately and set the
endpoint, region and credentials directly. The local setup helper deliberately
refuses to replace remote storage bindings.

## Sandbox

The tested local platform is macOS on Apple silicon (arm64). Windows and Linux
are untested; they are not required platforms for this release. A clean-install
full assessment of the prepared release candidate remains to be run on macOS.

```sh
uv run python scripts/build_inspection_image.py
uv run --env-file .env python scripts/configure_api.py
```

The build records a verified inspection archive in `infra/smolvm/inspection-image.json`.
Set `CONDERE_SMOL_IMAGE` to that archive. The runtime checks its hash before use.
The builder needs network access to install inspection tools; assessment VMs do not.

Alternatively set `CONDERE_SANDBOX=modal`, supply your own Modal credentials, and
run `uv run --env-file .env python scripts/configure_api.py` to generate the API
identity without a local image build. The existing Modal adapter builds from the pinned inspection base. Cloud creation
and sandbox execution incur external activity; no release-candidate full assessment
has been verified on this path yet.

## Models and startup

The template selects `CONDERE_MODEL_PROVIDER=openrouter`. Set
`CONDERE_OPENROUTER_API_KEY` and `EXA_API_KEY` in `.env`.

For analysis through your own ChatGPT plan, change `CONDERE_MODEL_PROVIDER` to
`chatgpt` and sign in locally:

```sh
uv run --env-file .env python scripts/chatgpt_login.py
```

After storage, sandbox and API identity setup, start the API:

```sh
uv run --env-file .env python scripts/run_local.py api
```

Use your own account. Hosted multi-user plan access is a separate arrangement.
OpenRouter remains required for Jev decisions in both modes. The unresolved release verification
requirements are listed in [preview status](status.md).

## Operator authorization

MCP cannot create or enlarge spending authority. Before admitting an assessment,
an operator registers a campaign using the administrative helper. This explicitly
authorizes future spending; choose the cap and reason yourself:

```sh
uv run --env-file .env python scripts/authorize_campaign.py my-campaign \
  --cap-microusd "$APPROVED_CAP_MICROUSD" --reason "$AUTHORIZATION_REASON"
```

The helper calls `AssessmentStore.authorize_campaign` and starts no research.
Use a unique campaign ID, the configured owner, an explicit cap in micro-USD
(1 USD = 1,000,000 micro-USD), an authorization record and an assessment limit.
The API cannot supply these privileges through intake. The method is in
`src/condere/runtime/state.py`; administrative setup must use the application DB.
Existing authority cannot be overwritten with a larger cap. Connect the coding
client only after that operator step; supply the campaign ID when admitting work.

## Existing local data

New defaults use `~/.local/share/condere`. For an existing installation using
`~/.local/share/condere-mda`, stop its API, workers and native storage first. Move
the entire directory once only when the new destination does not exist, then
update absolute artifact/export paths and `CONDERE_CHATGPT_HOME` in configuration.
Preserve ownership marker contents and all database files. Never merge two state
directories or move a live database. This release preparation did not move existing
data. Set `CONDERE_CHATGPT_HOME` to retain an existing sign-in location meanwhile.

## Tests

Provider-free checks need no credentials:

```sh
uv run ruff check src scripts tests
uv run pytest tests -q
```

On dedicated local test storage, explicitly enable integration checks:

```sh
CONDERE_TEST_POSTGRES=1 CONDERE_TEST_OBJECTS=1 \
  uv run --env-file .env pytest tests/integration/test_state.py \
  tests/integration/test_native_evidence.py -q
```

These tests write fictional records. Do not point them at production. Sandbox tests
have separate opt-in flags in their modules. Live model tests require provider keys
and may spend money; do not enable the entire integration suite indiscriminately.
