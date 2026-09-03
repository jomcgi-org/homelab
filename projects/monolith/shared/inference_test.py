from shared.inference import (
    META_SPARK_API_KEY_ENV,
    auth_headers,
    structured_output,
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
