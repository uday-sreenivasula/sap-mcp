"""Every conversation pays for the tool list: keep it compact and complete."""
import json

import sap_mcp.server as srv
from sap_mcp import tool_text


def _tools():
    return list(srv.mcp._tool_manager._tools.values())


def test_every_core_tool_has_a_compact_text():
    missing = [t.name for t in _tools() if t.name not in tool_text.SHORT]
    assert not missing, f"add these to tool_text.SHORT: {missing}"
    assert all(t.description == tool_text.SHORT[t.name] for t in _tools())


def test_tool_list_stays_small():
    """~3.7k tokens with 25 tools (was ~6.4k with 22). Raise the bar only for a good reason,
    e.g. a new tool, and say so in the commit."""
    size = sum(len(json.dumps({"name": t.name, "description": t.description,
                               "inputSchema": t.parameters})) for t in _tools())
    assert size + len(srv._INSTRUCTIONS) < 16000, size


def test_schemas_have_no_noise_but_keep_meaning():
    for t in _tools():
        text = json.dumps(t.parameters)
        assert '"title"' not in text and '"null"' not in text, t.name
    run = srv.mcp._tool_manager._tools["run_sap_report"].parameters
    assert run["required"] == ["report"]
    assert run["properties"]["selections"]["type"] == "object"


def test_compact_schema_keeps_a_parameter_named_title():
    schema = {"title": "X", "properties": {"title": {"type": "string", "title": "Title"},
                                           "n": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                                                 "default": None}}}
    assert tool_text.compact_schema(schema) == {
        "properties": {"title": {"type": "string"}, "n": {"type": "integer", "default": None}}}
