# Report format

Condere returns a compact `condere.spinoff.v1` JSON report, defined by
[`SpinoffResult`](../src/condere/runtime/spinoff.py). Read it first, then retrieve
linked artifacts when you need supporting detail.

## Report contents

| Field | Contents |
|---|---|
| `schema_version` | The report contract, currently `condere.spinoff.v1` |
| `assessment_id`, `created_at` | Assessment identity and creation time |
| `input` | The admitted prompt, repositories and reference URLs |
| `decision` | Verdict, option, route, probabilities, gates and code adjustments |
| `reasoning` | Thesis, summary, arguments for and against, and the full reasoning link |
| `analysis` | Asset, rights and buyer summaries, each linked to detail |
| `next_test` | The next experiment and its success/failure conditions |
| `build_plan` | Proposed work packages, when applicable; may be null |
| `open_questions` | What remains unresolved and who can answer it |
| `claims`, `sources` | Counts and links to the underlying records |
| `artifacts` | Supporting files with content hashes and retrieval references |

Unknowns and a `stop` decision are valid outcomes. Model probabilities are estimates,
not guarantees. A build plan does not authorize executing the proposed work.

## Follow the evidence

Each artifact link carries a `ref`, a `condere://` URI, SHA-256 hash, byte count,
media type and summary. Over MCP:

1. Call `read_report` with the assessment ID.
2. Use `read_artifact` with a linked reference to retrieve detailed reasoning or claims.
3. Use `read_source` and `read_original` to inspect cited evidence.
4. Follow pagination offsets until you have the requested content.
5. Use `export_assessment` for a portable local bundle.

These retrieval operations read saved results; they do not start research. Quotes
are checked against retained source text. A valid quote establishes what a source
said, not whether its claims are true or commercially meaningful.

The [renderer](../src/condere/runtime/bundle.py) produces the supporting artifacts.
A measured public example is still pending; see [preview status](status.md).
