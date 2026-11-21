"""get_rule_source must hand back the whole YAML file, not a JSON blob that
clients such as Microsoft 365 Copilot cut to its first lines (Teams validation
ticket #5971696, item 6)."""
import json

import pytest

from ai_rete_rag_mcp import server as srv

@pytest.fixture
def anyio_backend():
    return "asyncio"


LONG_YAML = "\n".join(
    f"- name: \"Rule {i}\"\n  salience: {i}\n  conditions:\n"
    f"    - {{type: loan, field: f{i}, op: \">=\", value: {i}}}\n"
    f"  action: {{verdict: \"V{i}\", reason: \"r{i}\"}}"
    for i in range(60)
) + "\n"


def _payload(**over):
    body = {"domain": "loan", "rules_yaml": LONG_YAML, "editable": True, "is_demo": True,
            "exists": True, "rules": [{}] * 60, "overlaps": [], "edges": []}
    body.update(over)
    return json.dumps(body, indent=2)


def test_returns_the_complete_yaml_unescaped():
    out = srv.format_rule_source(_payload())
    assert LONG_YAML.rstrip("\n") in out
    assert out.startswith("Complete rule set for domain `loan`: 60 rules, 300 lines")
    assert "```yaml\n- name: \"Rule 0\"" in out
    assert "\n```\n\n" in out  # the fence closes after the last line of YAML
    assert "Rule 59" in out


def test_ends_with_a_line_naming_where_the_file_ends():
    # A bare closing fence as the last line gave Copilot no end marker to check
    # its copy against; it sometimes stopped a few lines in.
    out = srv.format_rule_source(_payload())
    assert out.endswith("End of the `loan` rule set: 60 rules, 300 lines.")
    assert "Show all 300 lines." in out.splitlines()[0]


def test_drops_the_redundant_parsed_views():
    out = srv.format_rule_source(_payload(overlaps=[{"a": "x"}], edges=[{"src": "a"}]))
    assert '"rules_yaml"' not in out and '"overlaps"' not in out


def test_missing_rule_set_is_explained():
    out = srv.format_rule_source(_payload(exists=False, rules_yaml="", rules=[]))
    assert "No rule set has been authored for domain `loan`" in out
    assert "put_rules" in out


def test_errors_pass_through():
    assert srv.format_rule_source("Error 403: nope") == "Error 403: nope"
    assert srv.format_rule_source('{"detail": "x"}') == '{"detail": "x"}'


def test_yaml_containing_a_fence_gets_a_longer_fence():
    out = srv.format_rule_source(_payload(rules_yaml="# ```\n- name: a\n"))
    assert "````yaml\n# ```\n- name: a\n````" in out


@pytest.mark.anyio
async def test_tool_formats_the_api_response(monkeypatch):
    async def fake_request(method, path, **kwargs):
        assert (method, path) == ("GET", "/domains/loan/rules")
        return _payload()
    monkeypatch.setattr(srv, "_request", fake_request)
    out = await srv.get_rule_source("loan")
    assert "Rule 59" in out and out.startswith("Complete rule set")
