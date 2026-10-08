# Project workflow architecture

Read this file when scaffolding a new project (running `init-project.py`), modifying stack templates, or auditing the cross-project layering. It is **not** auto-loaded at session start.

> **Canonical onboarding playbook:** the authoritative source-of-truth model is agent-context — see `get_doc("onboarding/project-setup-prompt.md")`. That doc governs *what content lives where*: all instructions, memory, docs, skills, commands, and hooks live in `~/.agent-context`; the filesystem `.claude/` holds only what the Claude Code harness physically reads from disk. **This** doc describes the complementary *disk-bootstrap layer* — the stack templates and `init-project.py` / `sync-project-config.py` scaffolding that materialize those harness-required files. Read both together when setting up a project.
