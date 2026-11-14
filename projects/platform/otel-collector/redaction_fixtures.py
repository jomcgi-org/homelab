"""Synthetic credentials and ordinary text shared by redaction tests."""

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAsecretkeymaterial\n"
    "-----END RSA PRIVATE KEY-----"
)


CREDENTIAL_CASES = [
    ("connecting to postgres://app:s3cr3tpw@db:5432/app", "s3cr3tpw"),
    ("redis://:r3dispw@cache:6379/0", "r3dispw"),
    ("ALTER ROLE app WITH LOGIN PASSWORD 'hunter2'", "hunter2"),
    ("CREATE USER x PASSWORD 'it''s-quoted'", "it''s-quoted"),
    ("push with ghp_" + "a" * 36, "ghp_" + "a" * 36),
    ("auth gho_" + "B1" * 18 + " ok", "gho_" + "B1" * 18),
    ("ghs_" + "c" * 36, "ghs_" + "c" * 36),
    ("token github_pat_11ABCDEFG0" + "x" * 40, "github_pat_11ABCDEFG0"),
    ("gitlab glpat-" + "g" * 20, "glpat-" + "g" * 20),
    ("aws key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("session key ASIAIOSFODNN7EXAMPLE used", "ASIAIOSFODNN7EXAMPLE"),
    ("key sk-ant-api03-" + "Z" * 40, "sk-ant-api03-"),
    ("openai sk-proj-" + "q" * 30, "sk-proj-"),
    ("honeycomb hcaik_01" + "k" * 40, "hcaik_01"),
    (
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqb2UifQ.c2lnbmF0dXJlLXBhcnQ",
        "c2lnbmF0dXJlLXBhcnQ",
    ),
    ("loaded key " + PEM, "secretkeymaterial"),
    ("-----BEGIN PRIVATE KEY----- MIIEvQIBADANsecret", "MIIEvQIBADANsecret"),
    ("Cookie: session=abc123; csrftoken=def456", "abc123"),
    ("Set-Cookie: id=a3fWa; Expires=Wed, 21 Oct 2026 07:28:00 GMT", "a3fWa"),
    (
        'headers={"Authorization":["Basic dXNlcjpwYXNz"],"Cookie":["session=abc123"]}',
        "dXNlcjpwYXNz",
    ),
    (
        'headers={"Authorization":["Basic dXNlcjpwYXNz"],"Cookie":["session=abc123"]}',
        "abc123",
    ),
    ("header map[Authorization:[Bearer t0ken.v4lue] Accept:[*/*]]", "t0ken.v4lue"),
    ("X-Api-Key: k3y-v4lue", "k3y-v4lue"),
    ("x-auth-token: aut4-v4lue", "aut4-v4lue"),
    ("Private-Token: pr1v-v4lue", "pr1v-v4lue"),
    ("x-honeycomb-team: hcteam-v4lue", "hcteam-v4lue"),
    ("run psql --password s3cret-flag -h db", "s3cret-flag"),
    ("run cli --api-key=flagk3y", "flagk3y"),
    ("api_key: yamlk3yv4lue", "yamlk3yv4lue"),
    ("  token: yamlt0ken", "yamlt0ken"),
    ('payload {\\"user\\":\\"joe\\",\\"password\\":\\"pa55w0rd\\"}', "pa55w0rd"),
    ('payload {\\"password\\":\\"pa\\\\\\"55tail\\"}', "55tail"),
    ('{"client_secret":"cs-value","user":"joe"}', "cs-value"),
    ('{"password":"pa\\"ss-tail","user":"joe"}', "ss-tail"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Authorization: Bearer abc.DEF-123", "abc.DEF-123"),
    ("GET /cb?code=1&access_token=tok123&x=1", "tok123"),
    ("password=hunter2 user=joe", "hunter2"),
    ("retry with password: pw456", "pw456"),
]


ORDINARY_LINES = [
    "Secret: ns/name not found",
    'secret "monolith/db-credentials" not found',
    "failed to refresh token: context canceled",
    "failed to set cookie: invalid domain example.com",
    "token: refresh failed after 3 attempts",
    "max_tokens=4096 temperature=0",
    "dial tcp 10.0.0.1:5432: connect: connection refused",
    "GET http://argocd-server:8080/api/v1/applications@latest",
    "task-runner-controller-manager restarted",
    "user=joe action=login result=ok",
    "--password-stdin given; reading from stdin",
]
