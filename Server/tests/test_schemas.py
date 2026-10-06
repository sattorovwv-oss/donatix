"""Схемы инструментов: валидность и синхрон с реализациями."""

import json

from jsonschema import Draft202012Validator

from bot.schemas import TOOLS
from bot.tools import HANDLERS


def test_every_schema_has_an_implementation():
    """Схема без реализации — это гарантированная ошибка в разговоре."""
    assert {t["name"] for t in TOOLS} == set(HANDLERS)


def test_schemas_are_valid_json_schema():
    for tool in TOOLS:
        Draft202012Validator.check_schema(tool["input_schema"])


def test_required_fields_exist_in_properties():
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert set(schema.get("required", [])) <= set(schema["properties"]), tool["name"]


def test_every_tool_is_described_for_the_model():
    for tool in TOOLS:
        assert tool["description"].strip(), tool["name"]
        for name, prop in tool["input_schema"]["properties"].items():
            assert "type" in prop, (tool["name"], name)


def test_schemas_serialize_for_the_api():
    assert json.dumps(TOOLS, ensure_ascii=False)
