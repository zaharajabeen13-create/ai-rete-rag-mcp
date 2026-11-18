"""
Directory-policy guard for tool descriptions.

Anthropic's Connectors Directory requires that tool descriptions "contain no
instructions about model behavior, other tools, or external instruction
sources". A description says what a tool does and returns; workflow guidance
(which tool to call next, what to tell the user) lives once, in the server's
`instructions`. These tests keep a new or edited tool from drifting back.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from ai_rete_rag_mcp import server as srv

TOOLS = asyncio.run(srv.mcp.list_tools())
TOOL_NAMES = {t.name for t in TOOLS}

# Phrases that address the model rather than describe the tool.
DIRECTIVE_PATTERNS = [
    r"\btell the user\b",
    r"\bshow (?:them|it|both|the drafts)? ?to the user\b",
    r"\bpass (?:that|this|it) on\b",
    r"\bask (?:the user|for those)\b",
    r"\buse this (?:before|after|to)\b",
    r"\bbefore calling\b",
    r"\bthen (?:call|persist|save)\b",
    r"\balways set\b",
    r"\bIMPORTANT\b",
    r"\byou must\b",
    r"\bignore (?:previous|prior|other)\b",
]


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_has_no_model_directives(tool):
    text = tool.description or ""
    hits = [p for p in DIRECTIVE_PATTERNS if re.search(p, text, re.IGNORECASE)]
    assert not hits, f"{tool.name}: directive language {hits}"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_does_not_reference_other_tools(tool):
    text = tool.description or ""
    others = [n for n in TOOL_NAMES - {tool.name} if f"`{n}`" in text]
    assert not others, f"{tool.name}: mentions other tools {others}"


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_description_has_no_hidden_text(tool):
    text = tool.description or ""
    assert text.isascii() or not re.search(r"[​-‏⁠-⁯﻿]", text)
    assert "<!--" not in text


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_tool_has_title_and_annotations(tool):
    assert tool.title, f"{tool.name}: missing title"
    # Wire format (camelCase), which is what the directory reviewer sees.
    ann = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
    # Every hint must be an explicit boolean: the ChatGPT directory rejects
    # tools that leave any of them to the protocol default.
    for hint in ("readOnlyHint", "destructiveHint", "openWorldHint"):
        assert isinstance(ann.get(hint), bool), f"{tool.name}: {hint} must be explicit"
    if ann["readOnlyHint"]:
        assert ann["destructiveHint"] is False, f"{tool.name}: read-only tool marked destructive"
    # Only our own API is ever called.
    assert ann["openWorldHint"] is False, f"{tool.name}: unexpected openWorldHint"


def test_workflow_guidance_lives_in_server_instructions():
    instructions = srv.mcp.instructions or ""
    for needle in ("get_parameters", "missing_parameters", "import_policy_rules",
                   "put_rules", "citation", "next_step"):
        assert needle in instructions, f"server instructions lost guidance on {needle}"
