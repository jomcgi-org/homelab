defmodule Embervm.CaptureEvidenceRetention do
  @moduledoc """
  Retention for explicitly opted-in, newly created browser-capture lineages.

  A small policy lives beside the existing workspace archive, not inside the
  guest disk. The S3 owner must create it before meta.json exists. Unmarked
  archives keep their existing retention. Expiration makes inactive evidence
  eligible for the existing fenced GC; it never overrides live-session or
  node-reported holds. Small turn results/provenance remain in the factory DB.
  """

  @hour 3_600_000
  @day 24 * @hour
  @default_ttl 3 * @day
  @max_ttl 7 * @day
  @max_review_lease 12 * @hour
  @keys ~w(version kind session_id lineage_id workload principal created_at expires_at hard_expires_at lease_until)

  def default_ttl_ms, do: @default_ttl
  def max_review_lease_ms, do: @max_review_lease
  def filename, do: "evidence-retention.json"

  def new(identity, now, ttl_ms \\ @default_ttl) do
    with :ok <- identity_valid(identity),
         true <- is_integer(now) and now > 0,
         true <- is_integer(ttl_ms) and ttl_ms >= @hour and ttl_ms <= @max_ttl do
      {:ok,
       Map.merge(Map.new(identity, fn {k, v} -> {to_string(k), v} end), %{
         "version" => 1,
         "kind" => "browser-capture",
         "created_at" => now,
         "expires_at" => now + ttl_ms,
         "hard_expires_at" => now + ttl_ms + @max_review_lease,
         "lease_until" => now
       })}
    else
      _ -> {:error, :invalid_capture_retention}
    end
  end

  def validate(policy) when is_map(policy) do
    identity = Map.take(policy, ~w(session_id lineage_id workload principal))

    with true <- Enum.sort(Map.keys(policy)) == Enum.sort(@keys),
         :ok <- identity_valid(identity),
         true <- policy["version"] == 1 and policy["kind"] == "browser-capture",
         created when is_integer(created) and created > 0 <- policy["created_at"],
         expires when is_integer(expires) <- policy["expires_at"],
         hard when is_integer(hard) <- policy["hard_expires_at"],
         lease when is_integer(lease) <- policy["lease_until"],
         true <- expires - created >= @hour and expires - created <= @max_ttl,
         true <- hard == expires + @max_review_lease,
         true <- lease >= created and lease <= hard do
      :ok
    else
      _ -> {:error, :invalid_capture_retention}
    end
  end

  def validate(_), do: {:error, :invalid_capture_retention}

  def renew(policy, identity, now, lease_ms) do
    with :ok <- validate(policy),
         :ok <- identity_valid(identity),
         true <- same_lineage?(policy, identity),
         true <- is_integer(now) and now < deadline(policy),
         true <- is_integer(lease_ms) and lease_ms > 0 and lease_ms <= @max_review_lease,
         true <- now + lease_ms <= policy["hard_expires_at"] do
      {:ok,
       Map.put(
         policy,
         "lease_until",
         max(policy["lease_until"], now + lease_ms)
       )}
    else
      _ -> {:error, :capture_retention_expired_or_mismatched}
    end
  end

  def same_lineage?(policy, identity) do
    Enum.all?(~w(lineage_id workload principal), fn key ->
      policy[key] == Map.get(identity, key, Map.get(identity, String.to_existing_atom(key)))
    end)
  end

  def deadline(policy), do: max(policy["expires_at"], policy["lease_until"])

  def eligible?(policy, identity, archive_created_at, now) do
    with :ok <- validate(policy),
         true <- same_lineage?(policy, identity),
         true <- is_integer(archive_created_at) and archive_created_at >= policy["created_at"],
         true <- is_integer(now) and now >= deadline(policy) do
      :ok
    else
      _ -> {:error, :capture_retention_held}
    end
  end

  defp identity_valid(identity) when is_map(identity) do
    values =
      Enum.map(~w(session_id lineage_id workload principal), fn key ->
        Map.get(identity, key, Map.get(identity, String.to_existing_atom(key)))
      end)

    if map_size(identity) == 4 and Enum.all?(values, &valid_component?/1),
      do: :ok,
      else: {:error, :invalid_capture_identity}
  end

  defp identity_valid(_), do: {:error, :invalid_capture_identity}

  defp valid_component?(value) when is_binary(value),
    do:
      byte_size(value) in 1..512 and not String.contains?(value, ["/", "\\", "\0"]) and
        String.trim(value) == value

  defp valid_component?(_), do: false
end
