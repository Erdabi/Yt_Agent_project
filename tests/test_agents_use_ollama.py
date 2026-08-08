"""Proves the migrated call sites (Research's IdeaGenerator/
KnowledgeBuilder, the Script Agent's ScriptGenerator/ScriptReviewer, the
Manager's ReasoningEngine, the Thumbnail Agent's
ThumbnailConceptGenerator) genuinely work end-to-end through the local
`ollama` provider — real prompt rendering (against the real template
files under prompts/), real request-building/response-parsing code, only
the HTTP layer to Ollama mocked (json.chat -> `format`-constrained JSON).
No `anthropic` import is exercised anywhere in this file.
"""

import json
from unittest import mock

import pytest


def _installed_comfyui_nodes() -> set[str]:
    """Every node class the shipped ComfyUI templates reference, so a
    faked `/object_info` reports an instance that can run them."""
    from libs.providers._comfyui_common import load_workflow_template, workflow_node_types

    nodes: set[str] = set()
    for path in ("config/comfyui/text_to_image.json", "config/comfyui/text_to_video.json"):
        workflow, _ = load_workflow_template(path)
        nodes |= workflow_node_types(workflow)
    return nodes


_INSTALLED_COMFYUI_NODES = _installed_comfyui_nodes()


def _fake_ollama_chat(tool_input: dict):
    """A `requests.request` stand-in returning Ollama's `/api/chat`
    shape for whatever `tool_input` dict the caller wants back.
    """

    def _fake(method, url, *, json=None, timeout=None, params=None):
        # The Script Agent now asks the provider registry which asset
        # types are fulfillable before validating a script, and the
        # ComfyUI providers answer that by querying /object_info. That is
        # a different call than the one these tests are about, so it is
        # answered plausibly rather than asserted on — reporting every
        # node class installed, so nothing is judged unavailable here.
        if url.endswith("/object_info"):
            return _Resp(200, {name: {} for name in _INSTALLED_COMFYUI_NODES})
        assert url.endswith("/api/chat")
        return _Resp(200, {
            "message": {"role": "assistant", "content": json_lib.dumps(tool_input)},
            "prompt_eval_count": 100,
            "eval_count": 50,
        })

    return _fake


import json as json_lib  # noqa: E402 - used inside _fake_ollama_chat above


class _Resp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


# --- Usage-tracking attributes the correct provider -------------------------


def test_track_llm_call_records_actual_provider_not_hardcoded_anthropic():
    """Regression test: `track_llm_call`'s `provider` kwarg defaults to
    "anthropic" (libs/llm_usage/tracker.py) — every migrated call site
    must pass the real provider explicitly, or the usage log silently
    misattributes every local Ollama call as "anthropic" (caught via a
    real end-to-end run: LLMUsageLog.provider read back "anthropic" even
    though Ollama served every request, since none of these call sites
    used to pass `provider=` at all).
    """
    from services.orchestrator.app.manager import reasoning as reasoning_module

    captured = {}

    def fake_record_llm_usage(**kwargs):
        captured["provider"] = kwargs["provider"]

    tool_input = {"action": "advance", "reasoning": "ok"}
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(tool_input)), \
         mock.patch.object(reasoning_module, "track_llm_call", wraps=reasoning_module.track_llm_call):
        with mock.patch("libs.llm_usage.tracker.record_llm_usage", side_effect=fake_record_llm_usage):
            reasoning_module.ReasoningEngine().decide(
                project_id="11111111-1111-1111-1111-111111111111",
                phase="video", stage="script", job_status="succeeded",
                error=None, retry_count=0, max_retries=3, next_stage="video",
            )
    assert captured["provider"] == "OllamaLLMProvider"


# --- Manager's ReasoningEngine ----------------------------------------------


def test_reasoning_engine_decides_via_ollama():
    from services.orchestrator.app.manager.reasoning import ReasoningEngine, WorkflowAction

    engine = ReasoningEngine()
    tool_input = {"action": WorkflowAction.ADVANCE, "reasoning": "Script stage succeeded cleanly."}
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(tool_input)):
        decision = engine.decide(
            project_id="11111111-1111-1111-1111-111111111111",
            phase="video",
            stage="script",
            job_status="succeeded",
            error=None,
            retry_count=0,
            max_retries=3,
            next_stage="video",
        )
    assert decision.action == WorkflowAction.ADVANCE
    assert "succeeded" in decision.reasoning


def test_reasoning_engine_falls_back_when_ollama_unreachable():
    import requests

    from services.orchestrator.app.manager.reasoning import ReasoningEngine

    def fake_unreachable(method, url, *, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")

    engine = ReasoningEngine()
    with mock.patch("requests.request", side_effect=fake_unreachable):
        decision = engine.decide(
            project_id="11111111-1111-1111-1111-111111111111",
            phase="video",
            stage="script",
            job_status="failed",
            error="boom",
            retry_count=0,
            max_retries=3,
            next_stage="video",
        )
    # Ollama's own default max_attempts (3) retries before this returns —
    # the point is it degrades to the deterministic rule, never raises.
    assert decision.action == "retry"
    assert "Deterministic fallback used" in decision.reasoning


# --- Research Agent: IdeaGenerator ------------------------------------------


def test_idea_generator_produces_ideas_via_ollama():
    from services.agent_research.app.idea_generator import IdeaGenerator

    tool_input = {
        "ideas": [
            {
                "topic": "How Car Engines Actually Work",
                "target_audience": "curious adults",
                "why_people_would_watch": "demystifies something people use daily",
                "keywords": ["engine", "combustion"],
                "suggested_angle": "myth-busting",
                "competition_level": "medium",
                "score": 80,
                "suggested_length_sec": 240,
                "research_notes": "seed topic",
            }
        ]
    }
    generator = IdeaGenerator()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(tool_input)):
        ideas = generator.generate(
            project_id=None,
            channel_niche="automotive explainers",
            channel_persona="curious, myth-busting",
            banned_topics=None,
            existing_titles=None,
            trend_signals=None,
            goal=None,
            count=1,
        )
    assert len(ideas) == 1
    assert ideas[0].topic == "How Car Engines Actually Work"


# --- Research Agent: KnowledgeBuilder (enable_web_research path) -----------


def test_knowledge_builder_caveats_unverified_package_under_ollama():
    from services.agent_research.app.knowledge_builder import (
        _NO_WEB_RESEARCH_CAVEAT,
        KnowledgeBuilder,
    )

    tool_input = {
        "summary": "Engines convert fuel into motion via four strokes.",
        "verified_facts": [],
        "timeline": [],
        "entities": [],
        "keywords": ["engine"],
        "related_topics": ["diesel"],
        "hooks": ["Most people can't name all four strokes."],
        "supporting_notes": "Built from general knowledge.",
    }
    builder = KnowledgeBuilder()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(tool_input)):
        package = builder.build(
            project_id="p1",
            topic="How car engines work",
            target_audience="curious adults",
            channel_niche="automotive",
            channel_persona="myth-busting",
            banned_topics=None,
            existing_keywords=None,
        )
    # Ollama has no server-side web search — the package must be honestly
    # labeled as not-actually-verified, not silently presented as if it were.
    assert _NO_WEB_RESEARCH_CAVEAT in package.supporting_notes


# --- Thumbnail Agent: ThumbnailConceptGenerator -----------------------------


def test_thumbnail_concept_generator_via_ollama():
    from services.agent_video.app.thumbnail_concept_generator import ThumbnailConceptGenerator

    tool_input = {
        "concepts": [
            {
                "concept_name": "Cutaway engine close-up",
                "visual_description": "A cutaway engine model mid-stroke",
                "image_prompt": "cutaway 4-cylinder engine, dramatic lighting",
                "overlay_text": "THE REAL STORY",
                "rationale": "curiosity + visual clarity",
            }
        ]
    }
    generator = ThumbnailConceptGenerator()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(tool_input)):
        concepts = generator.generate(
            project_id="p1",
            video_title="How Car Engines Actually Work",
            channel_niche="automotive",
            channel_persona="myth-busting",
            style_guide_summary="bold, high-contrast",
            banned_topics=None,
            target_audience="curious adults",
            suggested_angle="myth-busting",
            emphasis_words=None,
            script_text="engines are more complex than people think",
            concept_count=1,
        )
    assert len(concepts) == 1
    assert concepts[0].concept_name == "Cutaway engine close-up"


# --- Script Agent: ScriptGenerator / ScriptReviewer -------------------------


def _fake_beat(voiceover_text: str | None = None, wpm: int = 150) -> dict:
    return {
        # Each beat needs genuinely different narration: `script_from_dict`
        # rejects a script whose beats repeat (script_schema.py's
        # `_validate_beat_distinctness`), so reusing one line for every
        # beat would fail parsing before these tests ever reached the
        # Ollama plumbing they exist to exercise.
        "voiceover_text": voiceover_text or "Engines are more complex than most people realize.",
        "scene_description": "Cutaway engine model, dramatic lighting.",
        "visual_suggestions": "A cutaway engine model in dramatic lighting.",
        "production_metadata": {
            "camera_framing": "medium shot",
            "asset_requirements": [{"asset_type": "ai_video", "description": "cutaway engine model"}],
            "transition_type": "cut",
            "pacing": "medium",
            "narration_emotion": "curious",
            "emphasis_words": [],
            "estimated_speech_wpm": wpm,
        },
    }


def _fake_script_dict() -> dict:
    return {
        "structure_notes": "problem -> explanation -> takeaway",
        "retention_notes": "open loop in the hook",
        "hook": _fake_beat(),
        "introduction": _fake_beat(
            "Today we follow a single spark from the coil all the way to the driveshaft."
        ),
        "main_sections": [
            {
                "heading": "The four strokes",
                **_fake_beat(
                    "Intake pulls the mixture down, compression squeezes it into a tight pocket."
                ),
            }
        ],
        "ending": _fake_beat(
            "Four simple motions, repeated thousands of times a minute, move a two ton car."
        ),
        "call_to_action": _fake_beat("Subscribe and we will take apart a gearbox next week."),
    }


def _fake_project_context():
    from datetime import UTC, datetime

    from libs.context.schema import (
        ChannelProfile,
        ManagerSettings,
        ProjectContext,
        ProjectMetadata,
        ResearchSummary,
    )
    from libs.models.enums import ProjectStage, ProjectStatus

    return ProjectContext(
        built_at=datetime.now(UTC),
        project=ProjectMetadata(
            project_id="11111111-1111-1111-1111-111111111111",
            channel_id="22222222-2222-2222-2222-222222222222",
            idea_id="33333333-3333-3333-3333-333333333333",
            current_stage=ProjectStage.SCRIPTING,
            status=ProjectStatus.IN_PROGRESS,
            retry_count=0,
            priority=0,
        ),
        channel=ChannelProfile(
            channel_id="22222222-2222-2222-2222-222222222222",
            name="Test Channel",
            niche="automotive",
            persona="myth-busting",
        ),
        research=ResearchSummary(topic="How car engines work", suggested_length_sec=240),
        knowledge_package=None,
        prompt_version="v1",
        manager=ManagerSettings(job_max_retries=3, anthropic_model="unused", anthropic_effort="unused"),
    )


def test_script_generator_via_ollama():
    from services.agent_scriptwriter.app.script_generator import ScriptGenerator

    generator = ScriptGenerator()
    context = _fake_project_context()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(_fake_script_dict())):
        script = generator.generate(context)
    assert script.hook.voiceover_text == "Engines are more complex than most people realize."
    assert len(script.main_sections) == 1


def test_script_reviewer_via_ollama():
    from services.agent_scriptwriter.app.script_generator import ScriptGenerator
    from services.agent_scriptwriter.app.script_reviewer import ScriptReviewer

    context = _fake_project_context()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(_fake_script_dict())):
        draft = ScriptGenerator().generate(context)

    reviewed_dict = {**_fake_script_dict(), "review_notes": "Checked pacing and retention; no changes needed."}
    reviewer = ScriptReviewer()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(reviewed_dict)):
        reviewed = reviewer.review(context, draft)
    assert reviewed.review_notes == "Checked pacing and retention; no changes needed."


def test_script_reviewer_skips_gracefully_on_ollama_failure():
    import requests

    from services.agent_scriptwriter.app.script_generator import ScriptGenerator
    from services.agent_scriptwriter.app.script_reviewer import ScriptReviewer

    context = _fake_project_context()
    with mock.patch("requests.request", side_effect=_fake_ollama_chat(_fake_script_dict())):
        draft = ScriptGenerator().generate(context)

    def fake_unreachable(method, url, *, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")

    reviewer = ScriptReviewer()
    with mock.patch("requests.request", side_effect=fake_unreachable):
        reviewed = reviewer.review(context, draft)
    assert reviewed.hook.voiceover_text == draft.hook.voiceover_text  # unchanged draft returned
    assert "Self-review skipped" in reviewed.review_notes


@pytest.mark.parametrize(
    "module_name",
    [
        "services.orchestrator.app.manager.reasoning",
        "services.agent_research.app.idea_generator",
        "services.agent_research.app.knowledge_builder",
        "services.agent_scriptwriter.app.script_generator",
        "services.agent_scriptwriter.app.script_reviewer",
        "services.agent_video.app.thumbnail_concept_generator",
    ],
)
def test_module_does_not_import_anthropic(module_name):
    import importlib
    import sys

    module = importlib.import_module(module_name)
    assert "anthropic" not in sys.modules or not hasattr(module, "anthropic")
    assert not hasattr(module, "anthropic")
