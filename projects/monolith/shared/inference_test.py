"""Tests for shared inference helpers."""

from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import shared.inference
from shared.inference import (
    CHAT_BASE_URL_ENV,
    CHAT_MODEL_ENV,
    CHAT_REASONING_EFFORT_ENV,
    META_SPARK_API_KEY_ENV,
    auth_headers,
    chat_reasoning_effort,
    hosted_chat_provider,
    structured_output,
)


def test_chat_reasoning_effort_defaults_to_minimal(monkeypatch):
    monkeypatch.delenv(CHAT_REASONING_EFFORT_ENV, raising=False)

    assert chat_reasoning_effort() == "minimal"


def test_chat_reasoning_effort_honors_environment(monkeypatch):
    monkeypatch.setenv(CHAT_REASONING_EFFORT_ENV, "low")

    assert chat_reasoning_effort() == "low"


def test_chat_reasoning_effort_treats_whitespace_as_unset(monkeypatch):
    monkeypatch.setenv(CHAT_REASONING_EFFORT_ENV, "  \t ")

    assert chat_reasoning_effort() == "minimal"


def test_hosted_chat_provider_requires_model(monkeypatch):
    monkeypatch.delenv(CHAT_MODEL_ENV, raising=False)
    monkeypatch.setenv(CHAT_BASE_URL_ENV, "https://provider.example/v1")

    assert hosted_chat_provider() is None


def test_hosted_chat_provider_requires_base_url(monkeypatch):
    monkeypatch.setenv(CHAT_MODEL_ENV, "provider/model")
    monkeypatch.delenv(CHAT_BASE_URL_ENV, raising=False)

    assert hosted_chat_provider() is None


@pytest.mark.parametrize(
    ("model", "base_url"),
    [("", ""), ("  ", "\t")],
)
def test_hosted_chat_provider_rejects_empty_values(monkeypatch, model, base_url):
    monkeypatch.setenv(CHAT_MODEL_ENV, model)
    monkeypatch.setenv(CHAT_BASE_URL_ENV, base_url)

    assert hosted_chat_provider() is None


def test_hosted_chat_provider_returns_configured_pair(monkeypatch):
    monkeypatch.setenv(CHAT_MODEL_ENV, "openai/gpt-oss-20b")
    monkeypatch.setenv(CHAT_BASE_URL_ENV, "https://openrouter.ai/api/v1")

    assert hosted_chat_provider() == (
        "https://openrouter.ai/api/v1",
        "openai/gpt-oss-20b",
    )


def test_hosted_chat_provider_uses_environment_values(monkeypatch):
    monkeypatch.setenv(CHAT_MODEL_ENV, "custom/model")
    monkeypatch.setenv(CHAT_BASE_URL_ENV, "https://custom.example/v1")

    assert hosted_chat_provider() == (
        "https://custom.example/v1",
        "custom/model",
    )


def test_auth_headers_adds_bearer_when_meta_spark_key_is_set(monkeypatch):
    monkeypatch.setenv(META_SPARK_API_KEY_ENV, "spark-secret")

    assert auth_headers() == {"Authorization": "Bearer spark-secret"}


def test_auth_headers_omits_authorization_when_meta_spark_key_is_unset(monkeypatch):
    monkeypatch.delenv(META_SPARK_API_KEY_ENV, raising=False)

    assert auth_headers() == {}


def test_auth_headers_omits_authorization_when_meta_spark_key_is_empty(monkeypatch):
    monkeypatch.setenv(META_SPARK_API_KEY_ENV, "")

    assert auth_headers() == {}


def test_auth_headers_does_not_send_meta_key_to_another_host(monkeypatch):
    monkeypatch.setenv(META_SPARK_API_KEY_ENV, "spark-secret")

    assert auth_headers("http://inference.internal:8080") == {}


def test_structured_output_carries_both_dialects_and_same_schema():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    result = structured_output(schema, name="answer")

    assert result["guided_json"] is schema
    assert result["response_format"]["json_schema"]["schema"] is schema
    assert result["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": schema,
        },
    }


def test_record_usage_sets_attributes_on_current_span():
    span = MagicMock()
    span.is_recording.return_value = True
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(usage, "spark", "classifier")

    span.set_attributes.assert_called_once_with(
        {
            "llm.usage.prompt_tokens": 12,
            "llm.usage.completion_tokens": 5,
            "llm.usage.total_tokens": 17,
            "llm.model": "spark",
            "llm.caller": "classifier",
        }
    )


def test_record_usage_includes_reasoning_tokens_when_present():
    span = MagicMock()
    span.is_recording.return_value = True
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "completion_tokens_details": {"reasoning_tokens": 3},
    }

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(usage, "spark", "chat")

    assert span.set_attributes.call_args.args[0]["llm.usage.reasoning_tokens"] == 3


def test_record_usage_includes_cached_tokens_when_present():
    span = MagicMock()
    span.is_recording.return_value = True
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 8},
    }

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(usage, "spark", "chat")

    assert span.set_attributes.call_args.args[0]["llm.usage.cached_tokens"] == 8


@pytest.mark.parametrize(
    "completion_details",
    [None, "not-a-mapping", {}, {"accepted_prediction_tokens": 2}],
)
def test_record_usage_omits_reasoning_tokens_when_unavailable(completion_details):
    span = MagicMock()
    span.is_recording.return_value = True
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }
    if completion_details is not None:
        usage["completion_tokens_details"] = completion_details

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(usage, "spark", "chat")

    attributes = span.set_attributes.call_args.args[0]
    assert attributes["llm.usage.prompt_tokens"] == 12
    assert attributes["llm.usage.completion_tokens"] == 5
    assert attributes["llm.usage.total_tokens"] == 17
    assert "llm.usage.reasoning_tokens" not in attributes


def test_record_usage_returns_without_current_span():
    with patch("shared.inference.trace.get_current_span", return_value=None):
        shared.inference.record_usage(
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "spark",
            "chat",
        )


def test_record_usage_returns_without_recording_context():
    span = MagicMock()
    span.is_recording.return_value = False

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "spark",
            "chat",
        )

    span.set_attributes.assert_not_called()


def test_record_usage_tolerates_none_usage():
    with patch("shared.inference.trace.get_current_span") as get_current_span:
        shared.inference.record_usage(None, "spark", "chat")

    get_current_span.assert_not_called()


def test_record_usage_tolerates_missing_token_fields():
    span = MagicMock()
    span.is_recording.return_value = True

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(
            {"prompt_tokens": 1, "completion_tokens": 2}, "spark", "chat"
        )

    span.set_attributes.assert_not_called()


def test_record_usage_tolerates_scalar_usage():
    with patch("shared.inference.trace.get_current_span") as get_current_span:
        shared.inference.record_usage(12, "spark", "chat")

    get_current_span.assert_not_called()


def test_record_usage_tolerates_otel_errors():
    span = MagicMock()
    span.is_recording.return_value = True
    span.set_attributes.side_effect = RuntimeError("otel unavailable")

    with patch("shared.inference.trace.get_current_span", return_value=span):
        shared.inference.record_usage(
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "spark",
            "chat",
        )


def _real_sdk() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _expected_usage_attributes() -> dict[str, int | str]:
    return {
        "llm.usage.prompt_tokens": 12,
        "llm.usage.completion_tokens": 5,
        "llm.usage.total_tokens": 17,
        "llm.model": "spark",
        "llm.caller": "classifier",
    }


def test_record_usage_exports_span_without_ambient_span():
    provider, exporter = _real_sdk()

    with patch("shared.inference.trace.get_tracer", side_effect=provider.get_tracer):
        shared.inference.record_usage(
            {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            "spark",
            "classifier",
        )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "llm.completion"
    assert spans[0].attributes == _expected_usage_attributes()
    provider.shutdown()


def test_record_usage_exports_span_and_decorates_ambient_span():
    provider, exporter = _real_sdk()
    tracer = provider.get_tracer("test")

    with (
        patch("shared.inference.trace.get_tracer", side_effect=provider.get_tracer),
        tracer.start_as_current_span("request"),
    ):
        shared.inference.record_usage(
            {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            "spark",
            "classifier",
        )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"request", "llm.completion"}
    assert spans["request"].attributes == _expected_usage_attributes()
    assert spans["llm.completion"].attributes == _expected_usage_attributes()
    provider.shutdown()
