# Agent context system

Reusable infrastructure and global operating rules, without an existing user's
projects, memories, machines, remotes, credentials, service endpoints or incident history.

Includes the MCP server and tests, hooks and metadata, dispatchers, consent guards,
materializers, operational scripts and tests, skills, commands, worker definitions,
generic supporting docs, stack templates, dependency roles and node-tool definitions.
EXPORT.json records included categories, optional hooks and excluded project tests.

## Install

Install Python 3.14+ and uv. Extract the archive into ~/.agent-context so setup.py
is at ~/.agent-context/setup.py. The directory must be a new store; do not overwrite
an existing installation. Review global/instructions/global-agent-instructions.md
and global/starter-config.json, then run:

```
python3 ~/.agent-context/setup.py --harness claude
```

Use --harness codex, pi, opencode or copilot as needed. Existing harnesses are
also detected and rendered. Setup installs dependencies, creates an owner token
restricted to loopback in ~/.config/agent-context, and renders harness configuration.
Keep that token local; create separate scoped tokens for remote clients.
The first MCP connection starts one local daemon shared by sessions. Agents must
call get_session_context(cwd); global instructions load automatically.

Safety and workflow hooks are enabled through the existing event registry.
Platform/service integrations listed in disabled_hooks are shipped but opt-in.
Remove a hook from that list after configuring its dependencies, then rerun setup
or home-settings-sync. No personal repository receives a guard exemption.
The optional MCP manifest contains local integrations you may add to your active
manifest after installing them. LSP canaries and per-machine dependency assignments
start empty. Set your own AGENT_CONTEXT_VERIFY_HOSTS and AGENT_CONTEXT_FLEET_DOMAIN
before using fleet scripts. Installation renders source; generated harness copies
and runtime state are never included in this archive.

## Context and synchronization

Use MCP tools to register projects/workspaces/machines and add your own context.
Docs are served through MCP rather than projected into every harness.
Git sync and self-deployment start disabled. Configure your own signing identity,
repository and remotes before enabling sync. Existing consent and deployment
guards remain included. Instructions describe policy; hooks enforce supported
events, whose availability differs between harnesses.

For direct stdio use start.py; for shared local sessions use start.py --shared.
For remote service use start.py --http with your own scoped credentials and TLS
reverse proxy. Configure AGENT_CONTEXT_ALLOWED_HOSTS for the proxy hostname.
The remote relay machinery is included; provision its endpoint and tokens yourself.
No existing owner's fleet or external services are inherited.

No license grant is implied; choose a license before publishing the distribution.
