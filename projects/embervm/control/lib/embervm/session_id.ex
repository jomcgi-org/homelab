defmodule Embervm.SessionId do
  @moduledoc """
  Session identity and per-session capability tokens (R2, Task 5).

  Two independent secrets are minted per session:

    * the session id, `s-` + a 26-char Crockford-base32 ULID. The ULID's leading
      48 bits are the create timestamp (lexicographic-sortable, like the spec's
      `s-<ULID>`), the trailing 80 bits are random. It is NOT secret: it appears
      in URLs, logs, and the op-log. The random tail is only there so ids are
      unguessable enough that a caller cannot enumerate other sessions by id (the
      token, not the id, is the auth capability; this is defence in depth).
    * the session token, 32 random bytes URL-safe-base64 encoded, returned to the
      caller EXACTLY ONCE at create and never stored in plaintext: only its
      sha256 lives in the store. `verify_token/2` recomputes the hash and compares
      it CONSTANT-TIME against the stored hash, so a timing side channel cannot
      leak how many leading bytes of a guessed token matched.

  The token never enters the guest, MMDS, initEnv, or any snapshot: it exists
  only as a hash in the control plane, so a compromised guest image cannot leak
  it (standing decision 6, and the Task 5 threat model).
  """

  # Crockford base32 alphabet (no I, L, O, U), the ULID canonical encoding.
  @crockford ~c"0123456789ABCDEFGHJKMNPQRSTVWXYZ"

  @doc """
  A fresh session id: `s-` + a 26-char ULID whose 48-bit time prefix is
  `now_ms`. `now_ms` is injected (never read here) so tests are deterministic and
  ordering is testable.
  """
  @spec new(integer()) :: String.t()
  def new(now_ms) when is_integer(now_ms) do
    # 128-bit ULID = 48-bit time (ms since epoch) <> 80-bit randomness, encoded as
    # 26 Crockford-base32 chars (5 bits each, 130 bits, top 2 bits are the padding
    # the canonical ULID encoding drops to 0).
    <<rand::80>> = :crypto.strong_rand_bytes(10)
    int = Bitwise.bsl(now_ms, 80) + rand
    "s-" <> encode_ulid(int)
  end

  @doc """
  Mints a fresh capability token and its sha256. Returns `{token, token_sha256}`;
  the caller returns `token` to the client exactly once and persists only
  `token_sha256`.
  """
  @spec mint_token() :: {String.t(), String.t()}
  def mint_token do
    token = 32 |> :crypto.strong_rand_bytes() |> Base.url_encode64(padding: false)
    {token, token_sha256(token)}
  end

  @doc "The lowercase-hex sha256 of a token, the form stored in `sessions.token_sha256`."
  @spec token_sha256(String.t()) :: String.t()
  def token_sha256(token) when is_binary(token) do
    :crypto.hash(:sha256, token) |> Base.encode16(case: :lower)
  end

  @doc """
  Whether `token` authenticates the session whose stored hash is `stored_sha256`.
  Constant-time over the two hex hashes (`:crypto.hash_equals/2`), so a partial
  match is indistinguishable in time from a total miss. Returns false for a nil
  stored hash (a session that recorded none) so a token can never authenticate a
  hashless row.
  """
  @spec verify_token(String.t(), String.t() | nil) :: boolean()
  def verify_token(token, stored_sha256)
      when is_binary(token) and is_binary(stored_sha256) do
    # Compare the raw sha256 bytes, not the hex text: hash_equals wants equal-length
    # binaries, and the raw digests are always 32 bytes even if a stored value were
    # ever malformed hex (decode failure falls through to false below).
    computed = :crypto.hash(:sha256, token)

    case Base.decode16(stored_sha256, case: :lower) do
      {:ok, stored} when byte_size(stored) == byte_size(computed) ->
        :crypto.hash_equals(computed, stored)

      _ ->
        false
    end
  end

  def verify_token(_token, _stored), do: false

  # -- ULID encoding ---------------------------------------------------------

  defp encode_ulid(int) do
    # 26 base32 chars, most-significant first. Build the char list by shifting 5
    # bits off the top each step; the top 2 of the 130 encoded bits are always 0.
    for shift <- 125..0//-5, into: "" do
      idx = int |> Bitwise.bsr(shift) |> Bitwise.band(0x1F)
      <<Enum.at(@crockford, idx)>>
    end
  end
end
