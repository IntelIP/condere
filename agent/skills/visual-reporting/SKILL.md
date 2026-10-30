---
name: visual-reporting
description: Present accepted Condere research as traceable figures and a human report while retaining complete JSON for coding clients.
---

# Visual reporting

Use after evidence reconciliation. One saved assessment owns the answer. JSON is
the complete machine-readable record; Markdown and HTML explain that same record
to a person. PNG/SVG figures and their data are portable supporting artifacts,
not separate model-generated conclusions.

The trusted report renderer uses accepted records and runs plotting code in an
offline SmolVM. It returns figures, underlying data, captions, source references,
and hashes. It makes no model call. Do not reconstruct numbers from prose or ask
an image generator to draw quantitative charts.

Choose a visual only when it helps a decision:

- Keep measured, vendor-reported, and modelled figures distinct; compare values
  only with the same units, basis, workload, and version conditions.
- Show unknown values as missing, never zero. Label hypotheses and simulation.
- Use the evidence-coverage figure to show what still needs validation, not as a
  score for business quality or probability of success.
- Show validation tasks as an ordered plan without invented dates or durations.
- A repository graph describes observed code relationships; a proposed product
  architecture needs a separate label and explanation.

Every figure needs an informative title, units, a caption describing the takeaway
and limitations, and its accepted-record/source references. Preserve a data table
or JSON beside the image so Codex can inspect values without guessing from pixels.

Lead the human report with the recommendation, strongest reasons, material gaps,
and next proposed test. Keep all 26 Technical, Market and Business records in
the report/JSON. Codex retrieves the JSON and references first, then reads original
evidence or images as needed. Retrieval never starts fresh research.

A separate analyst worker is warranted only if a named analytical task requires
additional investigation. Rendering accepted data does not require another agent,
another provider, or broader permissions.
