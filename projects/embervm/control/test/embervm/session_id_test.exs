defmodule Embervm.SessionIdTest do
  @moduledoc """
  Session id shape + the token mint/verify capability: constant-time verification,
  a token authenticates exactly one hash, a tampered or wrong token is rejected,
  and the stored form is a hash (never the plaintext).
  """
  use ExUnit.Case, async: true

  alias Embervm.SessionId

  test "new/1 produces an s-<26-char ULID> id whose time prefix orders lexicographically" do
    a = SessionId.new(1_000_000)
    b = SessionId.new(2_000_000)

    assert String.starts_with?(a, "s-")
    assert String.length(a) == 28
    # 26 base32 chars after the s- prefix.
    assert String.length(String.replace_prefix(a, "s-", "")) == 26
    # A later timestamp sorts after an earlier one (ULID time prefix).
    assert a < b
  end

  test "ids are unique across many mints at the same timestamp (random tail)" do
    ids = for _ <- 1..1000, do: SessionId.new(1_000_000)
    assert length(Enum.uniq(ids)) == 1000
  end

  test "mint_token returns a url-safe token and its lowercase-hex sha256" do
    {token, sha} = SessionId.mint_token()

    # 32 random bytes, base64url no padding -> 43 chars, url-safe alphabet only.
    assert String.match?(token, ~r/^[A-Za-z0-9_-]+$/)
    assert sha == SessionId.token_sha256(token)
    # sha256 hex is 64 lowercase hex chars.
    assert String.match?(sha, ~r/^[0-9a-f]{64}$/)
    # The plaintext token is never equal to its stored hash.
    refute token == sha
  end

  test "verify_token accepts the right token and rejects a wrong or tampered one" do
    {token, sha} = SessionId.mint_token()

    assert SessionId.verify_token(token, sha)

    # A different token (another session's) does not verify against this hash.
    {other, _other_sha} = SessionId.mint_token()
    refute SessionId.verify_token(other, sha)

    # A tampered token (one byte flipped) does not verify.
    tampered = flip_last_char(token)
    refute SessionId.verify_token(tampered, sha)
  end

  test "verify_token rejects a nil or malformed stored hash (a hashless row grants nothing)" do
    {token, _sha} = SessionId.mint_token()
    refute SessionId.verify_token(token, nil)
    refute SessionId.verify_token(token, "not-hex")
    refute SessionId.verify_token(token, "ab")
  end

  defp flip_last_char(token) do
    {head, <<last::utf8>>} = String.split_at(token, String.length(token) - 1)
    replacement = if last == ?a, do: ?b, else: ?a
    head <> <<replacement::utf8>>
  end
end
