# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Ordinary mapping-reader compatibility with synthetic names and data."""

from __future__ import annotations

import builtins
import datetime as dt
from pathlib import Path
import sys
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _oacp_yaml import YAMLMappingError, load_yaml_mapping  # noqa: E402
import agent_profile  # noqa: E402
import oacp_inbox  # noqa: E402
import oacp_watch  # noqa: E402
import validate_agent_card  # noqa: E402
import validate_message  # noqa: E402


@pytest.fixture
def without_pyyaml(monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("synthetic missing PyYAML")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)


def test_native_and_string_scalar_modes():
    text = "count: 7\nflag: true\nempty: null\ndate: 2026-10-16\n"
    assert load_yaml_mapping(text) == {
        "count": 7, "flag": True, "empty": None, "date": dt.date(2026, 10, 16),
    }
    assert load_yaml_mapping(text, string_scalars=True) == {
        "count": "7", "flag": "true", "empty": "null", "date": "2026-10-16",
    }


@pytest.mark.parametrize("text", ["", "# empty document\n", "null\n"])
def test_empty_policy_is_explicit(text):
    assert load_yaml_mapping(text, allow_empty=True) == {}
    with pytest.raises(YAMLMappingError, match="top-level YAML must be a mapping"):
        load_yaml_mapping(text)
    assert load_yaml_mapping("{}") == {}


@pytest.mark.parametrize("text", ["[alice, bob]", "alice", "7"])
@pytest.mark.parametrize("string_scalars", [False, True])
def test_non_mapping_roots_are_rejected(text, string_scalars):
    fallback = mock.Mock(return_value={})
    with pytest.raises(YAMLMappingError):
        load_yaml_mapping(text, string_scalars=string_scalars, fallback=fallback)
    fallback.assert_not_called()


def test_malformed_yaml_never_uses_fallback():
    fallback = mock.Mock(return_value={})
    with pytest.raises(yaml.YAMLError):
        load_yaml_mapping("field: [broken", fallback=fallback)
    fallback.assert_not_called()
    # Even an ImportError from the parser is not a missing dependency.
    with mock.patch.object(yaml, "load", side_effect=ImportError("parser failure")):
        with pytest.raises(ImportError, match="parser failure"):
            load_yaml_mapping("field: value", fallback=fallback)
    fallback.assert_not_called()


def test_fallback_only_when_pyyaml_import_is_missing(without_pyyaml):
    text = "field: value"
    fallback = mock.Mock(return_value={"field": "value"})
    assert load_yaml_mapping(text, fallback=fallback) == {"field": "value"}
    fallback.assert_called_once_with(text)
    with pytest.raises(ImportError, match="synthetic missing PyYAML"):
        load_yaml_mapping(text)
    with pytest.raises(YAMLMappingError):
        load_yaml_mapping(text, fallback=lambda raw: [])
    with pytest.raises(ValueError, match="^bounded parser failure$"):
        load_yaml_mapping(
            text, fallback=mock.Mock(side_effect=ValueError("bounded parser failure")),
            parse_error=lambda exc: RuntimeError(f"native parser: {exc}"),
        )


def test_snapshot_wrappers_use_exact_supplied_bytes_and_string_scalars(monkeypatch):
    def no_read(*args, **kwargs):
        raise AssertionError("snapshot parser must not reread a path")

    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(Path, "read_bytes", no_read)
    for module in (oacp_inbox, oacp_watch):
        assert module._parse_yaml_mapping_bytes(b"flag: true\r\ncount: 7\r\n", Path("unused")) == {
            "flag": "true", "count": "7",
        }
        assert module._parse_yaml_mapping_bytes(b"", Path("unused")) == {}
        with pytest.raises(ValueError, match="^top-level YAML must be a mapping: unused$"):
            module._parse_yaml_mapping_bytes(b"[alice]", Path("unused"))
        with pytest.raises(yaml.YAMLError):
            module._parse_yaml_mapping_bytes(b"field: [broken", Path("unused"))


def test_validation_and_profile_wrappers_keep_native_modes_and_errors(tmp_path):
    path = tmp_path / "sample.yaml"
    path.write_text("flag: true\ncount: 7\n")
    assert validate_message._load_message(path.read_text()) == {"flag": "true", "count": "7"}
    assert validate_agent_card._load_yaml(path) == {"flag": True, "count": 7}
    assert agent_profile._load_yaml(path) == {"flag": True, "count": 7}
    path.write_text("")
    assert validate_message._load_message("") == {}
    assert validate_agent_card._load_yaml(path) == {}
    assert agent_profile._load_yaml(path) == {}
    path.write_text("[alice]")
    with pytest.raises(validate_message.MessageValidationError, match="^top-level YAML must be a mapping/object$"):
        validate_message._load_message(path.read_text())
    with pytest.raises(ValueError, match="^top-level YAML must be a mapping$"):
        validate_agent_card._load_yaml(path)
    with pytest.raises(ValueError, match="^top-level YAML must be a mapping: "):
        agent_profile._load_yaml(path)
    path.write_text("field: [broken")
    with pytest.raises(validate_message.MessageValidationError, match="^YAML parse error: "):
        validate_message._load_message(path.read_text())
    with pytest.raises(ValueError, match="^YAML parse error: "):
        validate_agent_card._load_yaml(path)
    with pytest.raises(ValueError, match="^invalid YAML in "):
        agent_profile._load_yaml(path)


def test_wrapper_fallbacks_and_required_profile_dependency(tmp_path, without_pyyaml, monkeypatch):
    text = "name: alice\nbody: sample\n"
    path = tmp_path / "sample.yaml"
    path.write_text(text)
    assert validate_message._load_message(text) == {"name": "alice", "body": "sample"}
    assert validate_agent_card._load_yaml(path) == {"name": "alice", "body": "sample"}
    for module in (oacp_inbox, oacp_watch):
        assert module._parse_yaml_mapping_bytes(text.encode(), path) == {"name": "alice", "body": "sample"}
    with pytest.raises(validate_message.MessageValidationError, match="^line 1: expected key: value$"):
        validate_message._load_message("not a mapping")
    monkeypatch.setattr(validate_agent_card, "_parse_simple_yaml", mock.Mock(side_effect=ValueError("bounded parser failure")))
    with pytest.raises(ValueError, match="^bounded parser failure$"):
        validate_agent_card._load_yaml(path)
    monkeypatch.setattr(agent_profile, "yaml", None)
    with pytest.raises(RuntimeError, match="^PyYAML is required: pip install pyyaml$"):
        agent_profile._load_yaml(path)
