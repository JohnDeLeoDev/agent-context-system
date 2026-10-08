'Every visible nested Markdown reference can be filtered in Obsidian.'

from pathlib import Path


def test_mobileapp_skill_references_have_area():
    root = Path(__file__).resolve().parents[2]
    skills = root / "projects" / "example-app" / "skills"
    references = [p for p in skills.rglob("*.md") if "references" in p.parts]
    assert references
    missing = [str(p.relative_to(root)) for p in references
               if not p.read_text().startswith(
                   '---\narea: "example-workspace"\nscope: "project:example-app"\n---\n')]
    assert missing == []
