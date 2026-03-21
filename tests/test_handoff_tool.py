"""Tests for tools/handoff_tool.py — contract validation, output parsing, prompt building."""

import json

import pytest

from tools.handoff_tool import (
    _build_handoff_prompt,
    _extract_json_from_output,
    _validate_contract,
    _validate_output_against_schema,
)


# ── _validate_contract ────────────────────────────────────────────────────────

class TestValidateContract:
    def test_empty_contract_is_valid(self):
        assert _validate_contract({}) is None

    def test_valid_standard_scope(self):
        assert _validate_contract({"policy_scope": "standard"}) is None

    def test_valid_restricted_scope(self):
        assert _validate_contract({"policy_scope": "restricted"}) is None

    def test_valid_read_only_scope(self):
        assert _validate_contract({"policy_scope": "read_only"}) is None

    def test_invalid_policy_scope(self):
        err = _validate_contract({"policy_scope": "admin"})
        assert err is not None
        assert "policy_scope" in err

    def test_non_dict_contract(self):
        err = _validate_contract("not a dict")
        assert err is not None
        assert "JSON object" in err

    def test_schema_must_be_dict(self):
        err = _validate_contract({"expected_output_schema": ["not", "a", "dict"]})
        assert err is not None
        assert "JSON object" in err

    def test_schema_dict_is_valid(self):
        contract = {
            "expected_output_schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            }
        }
        assert _validate_contract(contract) is None

    def test_toolsets_must_be_list(self):
        err = _validate_contract({"allowed_toolsets": "web"})
        assert err is not None
        assert "list" in err

    def test_toolsets_list_is_valid(self):
        assert _validate_contract({"allowed_toolsets": ["web", "file"]}) is None

    def test_none_toolsets_is_valid(self):
        assert _validate_contract({"allowed_toolsets": None}) is None


# ── _validate_output_against_schema ──────────────────────────────────────────

class TestValidateOutputAgainstSchema:
    def test_none_schema_skips_validation(self):
        assert _validate_output_against_schema({"anything": True}, None) is None

    def test_valid_output(self):
        schema = {
            "type": "object",
            "required": ["summary", "score"],
            "properties": {
                "summary": {"type": "string"},
                "score": {"type": "number"},
            },
        }
        output = {"summary": "All good", "score": 0.9}
        assert _validate_output_against_schema(output, schema) is None

    def test_missing_required_field(self):
        schema = {
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        }
        err = _validate_output_against_schema({}, schema)
        assert err is not None
        assert "summary" in err

    def test_wrong_type_string(self):
        schema = {
            "properties": {"count": {"type": "integer"}},
        }
        err = _validate_output_against_schema({"count": "five"}, schema)
        assert err is not None
        assert "count" in err

    def test_wrong_type_array(self):
        schema = {
            "properties": {"items": {"type": "array"}},
        }
        err = _validate_output_against_schema({"items": "not-a-list"}, schema)
        assert err is not None

    def test_correct_type_boolean(self):
        schema = {
            "properties": {"ok": {"type": "boolean"}},
        }
        assert _validate_output_against_schema({"ok": True}, schema) is None

    def test_output_not_dict(self):
        schema = {"type": "object"}
        err = _validate_output_against_schema(["a", "list"], schema)
        assert err is not None
        assert "object" in err

    def test_optional_field_absent_is_ok(self):
        schema = {
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "optional_field": {"type": "string"},
            },
        }
        assert _validate_output_against_schema({"name": "test"}, schema) is None


# ── _extract_json_from_output ─────────────────────────────────────────────────

class TestExtractJsonFromOutput:
    def test_json_code_block(self):
        output = '```json\n{"status": "ok", "count": 3}\n```'
        result = _extract_json_from_output(output)
        assert result == {"status": "ok", "count": 3}

    def test_plain_code_block(self):
        output = '```\n{"key": "value"}\n```'
        result = _extract_json_from_output(output)
        assert result == {"key": "value"}

    def test_raw_json_in_text(self):
        output = 'Here is the answer: {"result": "done"} as requested.'
        result = _extract_json_from_output(output)
        assert result is not None
        assert result.get("result") == "done"

    def test_whole_output_is_json(self):
        output = '{"summary": "completed", "score": 0.9}'
        result = _extract_json_from_output(output)
        assert result == {"summary": "completed", "score": 0.9}

    def test_no_json_returns_none(self):
        output = "The task is complete. Everything looks good."
        result = _extract_json_from_output(output)
        assert result is None

    def test_empty_output_returns_none(self):
        assert _extract_json_from_output("") is None
        assert _extract_json_from_output(None) is None

    def test_malformed_json_returns_none(self):
        output = '```json\n{broken json\n```'
        # Falls back to next strategies, all fail for truly malformed
        # We just verify no exception is raised
        result = _extract_json_from_output(output)
        # May be None or may partially parse — just must not raise
        assert result is None or isinstance(result, dict)


# ── _build_handoff_prompt ─────────────────────────────────────────────────────

class TestBuildHandoffPrompt:
    # Signature: _build_handoff_prompt(goal, structured_input, contract)

    def test_goal_included(self):
        prompt = _build_handoff_prompt("Analyze the logs", None, {})
        assert "Analyze the logs" in prompt

    def test_structured_input_included(self):
        data = {"file": "/var/log/app.log", "lines": 100}
        prompt = _build_handoff_prompt("Read the file", data, {})
        assert "INPUT DATA" in prompt
        assert "/var/log/app.log" in prompt

    def test_no_structured_input(self):
        prompt = _build_handoff_prompt("Simple task", None, {})
        assert "INPUT DATA" not in prompt

    def test_restricted_policy_mentioned(self):
        prompt = _build_handoff_prompt("Task", None, {"policy_scope": "restricted"})
        assert "RESTRICTED" in prompt

    def test_read_only_policy_mentioned(self):
        prompt = _build_handoff_prompt("Task", None, {"policy_scope": "read_only"})
        assert "READ-ONLY" in prompt

    def test_standard_policy_no_restriction_text(self):
        prompt = _build_handoff_prompt("Task", None, {"policy_scope": "standard"})
        assert "RESTRICTED" not in prompt
        assert "READ-ONLY" not in prompt

    def test_contract_description_included(self):
        contract = {"description": "Research agent checking market data"}
        prompt = _build_handoff_prompt("Gather data", None, contract)
        assert "Research agent checking market data" in prompt

    def test_output_schema_requirement_included(self):
        contract = {
            "expected_output_schema": {
                "required": ["summary", "confidence"],
                "properties": {
                    "summary": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            }
        }
        prompt = _build_handoff_prompt("Analyze", None, contract)
        assert "OUTPUT REQUIREMENT" in prompt
        assert "summary" in prompt
        assert "confidence" in prompt
        assert "json" in prompt.lower()

    def test_no_schema_has_generic_instruction(self):
        prompt = _build_handoff_prompt("Task", None, {})
        assert "OUTPUT REQUIREMENT" not in prompt
        assert "summary" in prompt.lower() or "result" in prompt.lower() or "complete" in prompt.lower()
