# Contributing

For setup and service requirements, see [self-hosting](docs/self-hosting.md).
For known limitations, see [preview status](docs/status.md).

Use Python 3.12 or 3.13. Install the locked development environment with
`uv sync --locked --group dev`, then run:

```sh
uv run ruff check src scripts tests
uv run pytest tests -q
bash scripts/check-dead-code.sh
```

Describe the problem, resulting behavior and relevant verification in pull requests.
Keep provider-free tests independent of secrets. Integration tests are opt-in and
must identify real services, simulations and any paid calls accurately.

Do not commit credentials, private assessment data, production configuration or
operator planning documents. Keep untrusted code inside the sandbox and enforce
ownership and spending authority in the trusted runtime. Preserve unknowns rather
than inventing evidence. Public CI checks apply to the cleaned public tree.

Contributions to the public core are under the MIT license. Do not add proprietary
commercial code here without an explicit, clearly separated licensing decision.

The preview validation contract is `tabellio.validation.json`. CI checks the actual
pull-request head and retains JUnit results with its commit identity. These checks
cover provider-free behavior; they do not certify live research or untested platforms.

Please follow the [community code of conduct](CODE_OF_CONDUCT.md).
