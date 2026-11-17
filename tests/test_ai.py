"""AI 地名识别（不调用真实 API，只测解析与降级）。"""

from __future__ import annotations

import pytest

from photonmap import ai


def test_parse_plain_json_array():
    assert ai._parse_names('["断桥残雪", "杭州西湖"]') == ["断桥残雪", "杭州西湖"]


def test_parse_json_in_code_fence():
    text = '```json\n["断桥残雪", "杭州西湖"]\n```'
    assert ai._parse_names(text) == ["断桥残雪", "杭州西湖"]


def test_parse_json_with_surrounding_text():
    text = '这张照片可能拍在：["断桥残雪"]，仅供参考。'
    assert ai._parse_names(text) == ["断桥残雪"]


def test_parse_keeps_at_most_three_names():
    text = '["一", "二", "三", "四", "五"]'
    assert ai._parse_names(text) == ["一", "二", "三"]


def test_parse_ignores_non_json():
    assert ai._parse_names("我看不出来") == []
    assert ai._parse_names("") == []


def test_parse_ignores_non_string_items():
    assert ai._parse_names('["断桥", 42, null, "西湖"]') == ["断桥", "西湖"]


def test_available_reports_reason_when_sdk_missing(monkeypatch):
    """没装 anthropic 时如实说明原因，不抛异常。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("没有这个库")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, reason = ai.available()
    assert ok is False
    assert "anthropic" in reason


def test_suggest_places_raises_when_unavailable(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: (False, "没有凭据"))
    with pytest.raises(ai.AIUnavailable):
        ai.suggest_places(b"fake-jpeg")
