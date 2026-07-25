# Prompt Management System

Every prompt this system sends to an LLM lives here as a versioned
template file, loaded at runtime through `libs/prompts`
(`get_prompt_loader()`) — never embedded as a Python string constant in
agent code. This is what lets wording be tuned, rolled back, or given a
different phrasing for a different LLM provider without touching code.

## Layout

```
prompts/<agent>/<name>/v<N>.yaml            # default (provider-agnostic) template
prompts/<agent>/<name>/v<N>.<provider>.yaml # optional provider-specific override
```

- `<agent>` — which agent owns this prompt: `manager`, `research`,
  `script`, `video`, `qa`, and so on as future agents are added.
- `<name>` — a short, stable identifier for what the prompt is *for*
  within that agent (an agent can own several, e.g. the Manager has both
  `workflow_decision_system` and `workflow_decision_user`).
- `v<N>` — an integer version, starting at `v1`. Loading with
  `version="latest"` (the default) resolves to the highest `vN` present;
  pin a specific version to roll back a wording change without a code
  change.
- `.<provider>.yaml` — an optional override for one LLM provider (e.g.
  `v1.claude.yaml`). Requesting a provider with no override file is not an
  error — the loader silently falls back to the plain `v<N>.yaml`, so most
  prompts never need one at all.

## File format

```yaml
template: |
  Your prompt text goes here, with {{ jinja2_variables }} and
  {% if optional_context %}optional blocks{% endif %} as needed.
metadata:
  description: what this prompt is for and any non-obvious context
  owner: which agent/module renders it
```

`template` is a [Jinja2](https://jinja.palletsprojects.com/) template
string — variables (`{{ name }}`), conditionals (`{% if %}`), loops
(`{% for %}`), and filters (`{{ list | join(", ") }}`) all work.
Rendering uses `StrictUndefined`: a variable the template references that
the caller didn't pass raises `PromptRenderError` immediately, rather than
silently sending "None" or an empty string to an LLM. `metadata` is
freeform and has no runtime effect — it's for humans reading the file.

## Usage

```python
from libs.prompts import get_prompt_loader

loader = get_prompt_loader()
template = loader.get("manager", "workflow_decision_system", provider="claude")
system_prompt = template.render()  # no variables needed here

template = loader.get("manager", "workflow_decision_user")
user_prompt = template.render(phase="script", stage="scripting", job_status="succeeded", ...)
```

See `libs/prompts/loader.py` for the exact resolution rules and error
types.

## What's here today

- `manager/` — the Manager Agent's reasoning engine
  (`services/orchestrator/app/manager/reasoning.py`). `workflow_decision_system`
  has a Claude-specific override (`v1.claude.yaml`); `workflow_decision_user`
  demonstrates variables and conditionals.
- `research/` — the Research Agent's two real Claude integrations:
  - `generate_ideas_system`/`generate_ideas_user`
    (`services/agent_research/app/idea_generator.py`) — picks/scores a
    topic. `generate_ideas_system` has a Claude-specific override the same
    way the Manager's does; `generate_ideas_user` demonstrates loops and
    filters (`{% for %}`, `| join(", ")`) on top of variables and
    conditionals.
  - `build_knowledge_package_system`/`build_knowledge_package_user`
    (`services/agent_research/app/knowledge_builder.py`) — deep-researches
    a topic already chosen, using Claude's server-side `web_search`/`web_fetch`
    tools. The one prompt pair in this whole system where the Claude
    override matters functionally, not just stylistically: it documents
    that `tool_choice` is deliberately *not* forced for this call (every
    other Claude call here forces it), since Claude needs room to search
    before answering.
- `script/`, `video/`, `qa/` — draft `v1` templates for each agent's
  future LLM call, grounded in
  `docs/architecture/03-agent-responsibilities.md`. Not wired into any
  real code yet: those agents' `run()` methods still raise
  `NotImplementedError` (see `docs/architecture/06-roadmap.md` for when
  each lands). The templates exist now so there's a stable prompt
  interface to build against, the same way `libs/providers`' stub
  providers exist before any real vendor integration does.

Add a new prompt by creating `prompts/<agent>/<name>/v1.yaml` — no code
change is needed for the loader to find it.
