"""Prompt template loading: variables, versioning, provider overrides.

Every prompt an agent sends to an LLM is stored as a file under `prompts/`
at the repo root — `prompts/<agent>/<name>/v<N>.yaml` — rather than a
Python string constant, so wording can be tuned, versioned, or given a
different phrasing for a different LLM provider without a code change.
`prompts/README.md` documents the convention for anyone adding a new one.

File layout for one prompt "slot" (an agent + a name, e.g.
`manager`/`workflow_decision_system`):

    prompts/manager/workflow_decision_system/v1.yaml          # default
    prompts/manager/workflow_decision_system/v1.claude.yaml   # Claude-specific override
    prompts/manager/workflow_decision_system/v2.yaml          # a newer version

`PromptLoader.get(agent, name, version="latest", provider=None)` resolves:

1. The version — either the literal version string given, or the highest
   `vN` present on disk when `version="latest"` (compared numerically, so
   `v10` sorts after `v9`).
2. The provider override — `v{version}.{provider}.yaml` if `provider` is
   given and that file exists, else the plain `v{version}.yaml`. Asking
   for a provider with no override file is not an error: it just means
   that provider gets the default wording.

Each YAML file has one required key, `template` — a Jinja2 template
string (`{{ variable }}`, `{% if %}`/`{% for %}` for optional/repeated
content) — plus optional `metadata` for anything descriptive (owner,
notes) that has no runtime effect. `PromptTemplate.render(**variables)`
renders it; a variable the template references but the caller didn't
pass raises `PromptRenderError` immediately rather than silently
rendering "None" or an empty string into a prompt sent to an LLM.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Template
from jinja2.exceptions import TemplateError

_VERSION_PREFIX = "v"


class PromptNotFoundError(RuntimeError):
    """No template file exists for the requested agent/name/version, or
    the prompt "slot" (agent/name directory) doesn't exist at all.
    """


class PromptRenderError(RuntimeError):
    """The template file was found but rendering it failed — almost
    always a variable the template references that the caller didn't
    pass, or a malformed YAML file missing the `template` key.
    """


@dataclass(frozen=True)
class PromptTemplate:
    agent: str
    name: str
    version: str
    #: The provider whose override was actually used, or `None` if the
    #: default (provider-agnostic) file was used — either because no
    #: `provider` was requested, or because none was requested and it had
    #: no override file to fall back from.
    provider: str | None
    source_path: Path
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def render(self, **variables: Any) -> str:
        try:
            template = Template(self.body, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
            return template.render(**variables)
        except TemplateError as exc:
            raise PromptRenderError(
                f"failed to render {self.agent}/{self.name} {self.version} "
                f"({self.source_path}): {exc}"
            ) from exc


class PromptLoader:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def get(
        self,
        agent: str,
        name: str,
        *,
        version: str = "latest",
        provider: str | None = None,
    ) -> PromptTemplate:
        prompt_dir = self._root / agent / name
        if not prompt_dir.is_dir():
            raise PromptNotFoundError(
                f"no prompts registered for {agent!r}/{name!r} (looked in {prompt_dir})"
            )

        resolved_version = (
            self._latest_version(prompt_dir, agent, name) if version == "latest" else version
        )

        used_provider: str | None = None
        path: Path | None = None
        if provider:
            candidate = prompt_dir / f"{resolved_version}.{provider}.yaml"
            if candidate.is_file():
                path = candidate
                used_provider = provider

        if path is None:
            candidate = prompt_dir / f"{resolved_version}.yaml"
            if candidate.is_file():
                path = candidate

        if path is None:
            looked_for = f"{resolved_version}.yaml"
            if provider:
                looked_for = f"{resolved_version}.{provider}.yaml or {looked_for}"
            raise PromptNotFoundError(
                f"no template file for {agent!r}/{name!r} version {resolved_version!r} "
                f"(looked for {looked_for} in {prompt_dir})"
            )

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict) or "template" not in raw:
            raise PromptRenderError(f"{path} must be a mapping with a 'template' key")

        return PromptTemplate(
            agent=agent,
            name=name,
            version=resolved_version,
            provider=used_provider,
            source_path=path,
            body=raw["template"],
            metadata=raw.get("metadata") or {},
        )

    @staticmethod
    def _latest_version(prompt_dir: Path, agent: str, name: str) -> str:
        versions: set[str] = set()
        for candidate in prompt_dir.iterdir():
            if not candidate.is_file() or not candidate.name.endswith(".yaml"):
                continue
            # "v1.yaml" -> "v1"; "v1.claude.yaml" -> "v1" too — only the
            # part before the first dot is ever a version.
            version_part = candidate.name.split(".", 1)[0]
            if version_part.startswith(_VERSION_PREFIX) and version_part[len(_VERSION_PREFIX):].isdigit():
                versions.add(version_part)
        if not versions:
            raise PromptNotFoundError(
                f"no versioned template files (v1.yaml, v2.yaml, ...) found for "
                f"{agent!r}/{name!r} in {prompt_dir}"
            )
        return max(versions, key=lambda v: int(v[len(_VERSION_PREFIX):]))
