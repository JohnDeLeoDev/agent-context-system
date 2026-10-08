# Inter-agent requests

Use this for **cross-project or cross-machine hand-offs**. It is not for self-notes
(use memory) or context-system findings (use audit observations). To reach a session
that is live now, send it a message: `get_doc("inter-agent-messaging.md")`.

## Model

## Request doc format

Body opens with a frontmatter block:

Then these sections:
- **Request** — what you want, in one short paragraph.
- **Why** — rationale and impact (why it matters, what breaks without it).
- **Acceptance criteria** — bullet list of what "done" looks like.
- **Links** — files, PRs, related memories/docs.

## Lifecycle

## Conventions

- One request per doc; split unrelated asks.
- Address a project by its **registered display name** (`list_entities("project")`); address a
  machine by its **machine_id** (`list_machines`).
- Keep the frontmatter accurate — status drives what agents surface.
