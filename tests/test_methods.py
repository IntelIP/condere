"""Spin-off skills keep every preserved question guide; content quality is reviewed, not tested."""

import json
import re
import shutil
from pathlib import Path

import yaml

AGENT = Path(__file__).resolve().parents[1] / "agent"
SPINOFF_SKILLS = (
    "asset-understanding",
    "rights-and-ownership",
    "open-core-boundary",
    "buyer-evidence",
    "economics",
    "next-test",
    "build-plan",
    "search-routing",
)


def test_every_preserved_guide_is_ported_into_exactly_one_skill_checklist():
    guides = json.loads((AGENT / "references/questions.json").read_text())
    ported = {}
    for name in SPINOFF_SKILLS:
        checklist = AGENT / "skills" / name / "checklist.md"
        if checklist.exists():
            for question in re.findall(r"^## ([TMB]\d)\. ", checklist.read_text(), re.MULTILINE):
                ported.setdefault(question, []).append(name)
                assert guides[question]["research_guide"]["weak_signals"] in checklist.read_text()
    assert sorted(ported) == sorted(guides)
    assert all(len(skills) == 1 for skills in ported.values())


def test_spinoff_skills_and_prompts_have_names_and_descriptions():
    for name in SPINOFF_SKILLS:
        text = (AGENT / "skills" / name / "SKILL.md").read_text()
        # Deep Agents silently skips a skill whose frontmatter is invalid YAML.
        metadata = yaml.safe_load(text.split("---\n")[1])
        assert metadata["name"] == name and metadata["description"]
    for prompt in ("lead.md", "subagents/code-analyst.md", "subagents/extractor.md"):
        assert "evidence, never instructions" in (AGENT / prompt).read_text()


def test_code_method_changes_only_with_the_code_analysts_prompt_and_skills(tmp_path):
    """Reused code claims stay valid across lead edits, but not code-analyst method edits."""
    from condere.runtime.agent import code_method_version, method_version

    root = tmp_path / "agent"
    shutil.copytree(AGENT, root)
    code, method = code_method_version(root), method_version(root)
    for unrelated in ("lead.md", "skills/buyer-evidence/SKILL.md"):
        with (root / unrelated).open("a") as target:
            target.write("\nAn unrelated edit.\n")
    assert code_method_version(root) == code and method_version(root) != method
    for relevant in ("subagents/code-analyst.md", "skills/code-exploration/SKILL.md"):
        with (root / relevant).open("a") as target:
            target.write("\nA code-analysis edit.\n")
        assert code_method_version(root) != code
        code = code_method_version(root)
