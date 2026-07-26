"""A "no compositor configured" placeholder.

Unlike every other capability's stub_provider.py, this one doesn't stand
in for a missing paid vendor integration — a real, working provider for
this capability already exists (ffmpeg_provider.py) and is what
config/providers.yaml's `editor.active` points at. This file exists only
for structural symmetry with the rest of libs/providers, so an
environment without ffmpeg available could still point `editor.active`
at something that fails loudly and immediately instead of at a provider
that would crash mid-render.
"""

from .base import EditorProvider, EditResult, EditSpec


class StubEditorProvider(EditorProvider):
    def render(self, spec: EditSpec) -> EditResult:
        raise NotImplementedError(
            "No compositor is configured. Set editor.active to 'ffmpeg' in "
            "config/providers.yaml (libs.providers.editor.ffmpeg_provider."
            "FFmpegEditorProvider) to actually render video."
        )
