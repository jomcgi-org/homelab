from knowledge.redact import PATTERNS, Redactor, redact_text, redact_text_counts


PLANTED = (
    "AKIA1234567890ABCDEF",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ghp_abcdefghijklmnopqrstuvwxyz",
    "sk-openai_key_abcdefghijklmnopqrstu",
    "sk-ant-abcdefghijklmnopqrstuvwxyz",
    "xoxb-1234567890-abcdefghij",
    "eyJabcdefghijkl.abcdefghijkl.abcdefghijkl",
    "ZmFrZS1wcml2YXRlLWtleQ==",
    "-----BEGIN PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
    "Bearer abcdefghijklmnopqrstuvwxyz",
    "user:password123",
    "password123",
    "supersecretvalue",
    "CF_Authorization=abcdefghijklmnopqrstuvwxyz123456",
    "ops_abcdefghijklmnopqrstuvwxyz",
    "export BUILDBUDDY_API_KEY=abc123def456ghi789",
    "PGPASSWORD=hunter2secretvalue psql",
    "CLIENT_SECRET=0123456789abcdefghij",
    "MYSQL_ROOT_PASSWORD=letmeinplease123",
    "AWS_SESSION_TOKEN=FwoGZXIvYXdzEP//////////wEaDL0123456789abcdef",
    '{"token": "s3cr3tvaluehere123"}',
    "Authorization: token abcdef0123456789abcdef",
)

PLANTED_VALUES = (
    "abc123def456ghi789",
    "hunter2secretvalue",
    "0123456789abcdefghij",
    "letmeinplease123",
    "FwoGZXIvYXdzEP//////////wEaDL0123456789abcdef",
    "s3cr3tvaluehere123",
    "abcdef0123456789abcdef",
)


def test_every_planted_secret_is_replaced_and_counted_by_class():
    text = " ".join(
        (
            "AKIA1234567890ABCDEF",
            "aws_secret_access_key=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "sk-openai_key_abcdefghijklmnopqrstu",
            "sk-ant-abcdefghijklmnopqrstuvwxyz",
            "xoxb-1234567890-abcdefghij",
            "eyJabcdefghijkl.abcdefghijkl.abcdefghijkl",
            "https://user:password123@example.test",
            "password=supersecretvalue",
            "CF_Authorization=abcdefghijklmnopqrstuvwxyz123456",
            "ops_abcdefghijklmnopqrstuvwxyz",
            "export BUILDBUDDY_API_KEY=abc123def456ghi789",
            "PGPASSWORD=hunter2secretvalue psql",
            "CLIENT_SECRET=0123456789abcdefghij",
            "MYSQL_ROOT_PASSWORD=letmeinplease123",
            "AWS_SESSION_TOKEN=FwoGZXIvYXdzEP//////////wEaDL0123456789abcdef",
            '{"token": "s3cr3tvaluehere123"}',
            "Authorization: token abcdef0123456789abcdef",
            "Bearer abcdefghijklmnopqrstuvwxyz",
            "-----BEGIN PRIVATE KEY-----\n"
            "ZmFrZS1wcml2YXRlLWtleQ==\n"
            "-----END PRIVATE KEY-----",
        )
    )

    redacted, counts = redact_text_counts(text)

    for secret in PLANTED + PLANTED_VALUES:
        assert secret not in redacted
    assert counts == {
        "aws_access_key": 1,
        "aws_secret": 1,
        "github_token": 1,
        "anthropic_key": 1,
        "openai_key": 1,
        "slack_token": 1,
        "jwt": 1,
        "pem_block": 1,
        "basic_auth_url": 1,
        "cf_cookie": 1,
        "kv_secret": 8,
        "bearer_header": 1,
        "onepassword": 1,
    }
    for name, _pattern in PATTERNS:
        assert counts[name] >= 1
        assert f"[REDACTED:{name}]" in redacted


def test_redact_text_returns_total_count_and_leaves_normal_token_sentence():
    text = "A normal sentence can use the word token without containing a secret."

    assert redact_text(text) == (text, 0)
    assert redact_text_counts(text) == (text, {})


def test_env_dump_tool_result_is_replaced_and_counted():
    redactor = Redactor()

    redacted = redactor.tool_result(
        "PATH=/bin\nHOME=/tmp\nSHELL=/bin/zsh\nSECRET_VALUE=not-for-output"
    )

    assert redacted == "[REDACTED:env_dump]"
    assert redactor.counts["env_dump"] == 1
    assert "not-for-output" not in redacted
