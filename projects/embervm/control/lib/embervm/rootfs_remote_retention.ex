defmodule Embervm.RootfsRemoteRetention do
  @moduledoc """
  Default-off retention for identity-qualified baked-rootfs objects.

  The worker reuses `Embervm.S3Client` and runs an hourly dry-run sweep. Current
  chart image refs are protected without an age limit. A non-current prefix is
  eligible only after the configured age horizon, which must remain longer than
  every retained base and warmth migration window. Legacy digest-only layouts,
  malformed keys, incomplete markers, and unreadable listings are held
  fail-closed. The destructive arm is a separate gate and is false by default.

  Every plan is persisted before deletion under `gc-manifests/rootfs-*.json`.
  Enabling deletion therefore requires reviewing real candidates and confirming
  rollback assumptions first. Disabling the gate stops future deletion, but
  cannot restore an object that was already removed.
  """

  use GenServer
  require Logger

  alias Embervm.S3Client

  @sweep_interval_ms 3_600_000
  @max_prefixes 10
  @day_ms 24 * 60 * 60 * 1000
  @marker_name "rootfs.ext4.sha256"
  @digest ~r/^[0-9a-f]{64}$/
  @size ~r/^[1-9][0-9]*[KMGTP]?$/
  @format ~r/^[a-z][a-z0-9-]{0,31}$/
  @payload ~r/^[0-9a-f]{64}\.ext4$/

  def start_link(opts) do
    age_days = Keyword.get(opts, :age_days, 30)

    if is_integer(age_days) and age_days > 0 do
      case Keyword.get(opts, :name, __MODULE__) do
        nil -> GenServer.start_link(__MODULE__, opts)
        name -> GenServer.start_link(__MODULE__, opts, name: name)
      end
    else
      {:error, {:invalid_age_days, age_days}}
    end
  end

  @doc "Run one retention plan synchronously. Used by tests and live dry-run review."
  def sweep_now(server \\ __MODULE__), do: GenServer.call(server, :sweep_now, 120_000)

  @impl true
  def init(opts) do
    endpoint = Keyword.get(opts, :endpoint, "")
    bucket = Keyword.get(opts, :bucket, "embervm")
    access_key_id = Keyword.get(opts, :access_key_id, "") || ""
    secret_access_key = Keyword.get(opts, :secret_access_key, "") || ""

    credentials =
      if access_key_id != "" and secret_access_key != "" do
        [access_key_id: access_key_id, secret_access_key: secret_access_key]
      else
        []
      end

    client = S3Client.new(endpoint, bucket, credentials)

    s3 =
      Keyword.get_lazy(opts, :s3, fn ->
        if client do
          %{
            list: fn prefix -> S3Client.list_all(client, prefix) end,
            get: fn key -> S3Client.get(client, key) end,
            delete: fn key -> S3Client.delete(client, key) end,
            put: fn key, body -> S3Client.put(client, key, body) end
          }
        end
      end)

    state = %{
      s3: s3,
      enabled: Keyword.get(opts, :enabled, false),
      age_ms: Keyword.get(opts, :age_days, 30) * @day_ms,
      current_image_refs:
        opts
        |> Keyword.get(:current_image_refs, [])
        |> Enum.filter(&(is_binary(&1) and &1 != ""))
        |> MapSet.new(),
      wall_clock: Keyword.get(opts, :wall_clock, fn -> System.system_time(:millisecond) end),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms)
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_call(:sweep_now, _from, state), do: {:reply, run_sweep(state), state}

  @impl true
  def handle_info(:sweep, state) do
    run_sweep(state)
    schedule_sweep(state)
    {:noreply, state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  defp schedule_sweep(%{sweep_interval_ms: interval}) when interval > 0 do
    Process.send_after(self(), :sweep, interval)
  end

  defp schedule_sweep(_state), do: :ok

  defp run_sweep(%{s3: nil}), do: {:error, :store_disabled}

  defp run_sweep(%{enabled: true, current_image_refs: refs} = state) do
    if MapSet.size(refs) == 0, do: {:error, :no_current_image_refs}, else: do_run_sweep(state)
  end

  defp run_sweep(state), do: do_run_sweep(state)

  defp do_run_sweep(state) do
    with {:ok, entries} <- state.s3.list.("rootfs/"),
         {:ok, groups, held_keys} <- group_entries(entries),
         {:ok, eligible, held} <- classify_groups(state, groups),
         plan <- eligible |> Enum.sort_by(& &1.created_at_ms) |> Enum.take(@max_prefixes),
         :ok <- persist_manifest(state, plan, eligible -- plan, held, held_keys) do
      log_plan(state, plan, eligible, held, held_keys)
      deleted = if state.enabled, do: apply_deletes(state, plan), else: []
      {:ok, %{plan: Enum.map(plan, & &1.prefix), deleted: deleted}}
    else
      {:error, reason} = error ->
        Logger.error("embervm rootfs remote retention: ABORT #{inspect(reason)}")
        error
    end
  end

  defp group_entries(entries) when is_list(entries) do
    {grouped, held_keys} =
      Enum.reduce(entries, {%{}, []}, fn entry, {groups, held} ->
        case parse_key(entry.key) do
          {:ok, identity, file} ->
            prefix = identity.prefix
            item = %{entry: entry, file: file, identity: identity}
            {Map.update(groups, prefix, [item], &[item | &1]), held}

          :hold ->
            {groups, [entry.key | held]}
        end
      end)

    {:ok, Map.values(grouped), Enum.reverse(held_keys)}
  end

  defp group_entries(_entries), do: {:error, :bad_list_response}

  defp parse_key(key) do
    case String.split(key, "/") do
      ["rootfs", digest, "size-" <> size, "format-" <> format, file]
      when file != "" ->
        if Regex.match?(@digest, digest) and Regex.match?(@size, size) and
             Regex.match?(@format, format) do
          prefix = "rootfs/#{digest}/size-#{size}/format-#{format}"
          {:ok, %{digest: digest, size: size, format: format, prefix: prefix}, file}
        else
          :hold
        end

      # Legacy digest-only objects and every unexpected depth are never deleted.
      _ ->
        :hold
    end
  end

  defp classify_groups(state, groups) do
    Enum.reduce_while(groups, {:ok, [], []}, fn group, {:ok, eligible, held} ->
      case classify_group(state, group) do
        {:eligible, candidate} -> {:cont, {:ok, [candidate | eligible], held}}
        {:held, item} -> {:cont, {:ok, eligible, [item | held]}}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
    |> case do
      {:ok, eligible, held} -> {:ok, Enum.reverse(eligible), Enum.reverse(held)}
      error -> error
    end
  end

  defp classify_group(state, group) do
    first = hd(group)
    prefix = first.identity.prefix
    invalid_files = Enum.reject(group, &valid_file?(&1.file))
    markers = Enum.filter(group, &(&1.file == @marker_name))
    entries = Enum.map(group, & &1.entry)
    bytes = Enum.sum(Enum.map(entries, & &1.size))

    cond do
      invalid_files != [] ->
        {:held, held(prefix, bytes, "unexpected_file")}

      length(markers) > 1 ->
        {:held, held(prefix, bytes, "multiple_markers")}

      markers == [] ->
        # A payload without its completion marker may be a writer still in
        # flight or a failed historical publish. Age cannot distinguish those
        # states, so destructive retention must hold it for manual review.
        {:held, held(prefix, bytes, "incomplete_missing_marker")}

      true ->
        marker_key = hd(markers).entry.key

        case state.s3.get.(marker_key) do
          {:ok, body} -> classify_marker(state, first.identity, entries, bytes, body)
          {:error, reason} -> {:error, {:marker_get_failed, marker_key, reason}}
        end
    end
  end

  defp classify_marker(state, identity, entries, bytes, body) do
    prefix = identity.prefix

    with {:ok, marker} <- decode_marker(body),
         :ok <- validate_marker(marker, identity, entries),
         {:ok, uploaded_at_ms} <- parse_time(marker["uploadedAt"]) do
      image_ref = marker["imageRef"]

      cond do
        MapSet.member?(state.current_image_refs, image_ref) ->
          {:held, held(prefix, bytes, "current_image_ref")}

        state.wall_clock.() - uploaded_at_ms < state.age_ms ->
          {:held, held(prefix, bytes, "younger_than_age_horizon")}

        true ->
          {:eligible,
           candidate(
             prefix,
             bytes,
             uploaded_at_ms,
             entries,
             prefix <> "/" <> @marker_name,
             image_ref,
             identity
           )}
      end
    else
      _ -> {:held, held(prefix, bytes, "invalid_marker")}
    end
  end

  defp decode_marker(body) when is_binary(body) do
    case :json.decode(body) do
      marker when is_map(marker) -> {:ok, marker}
      _ -> {:error, :not_a_map}
    end
  rescue
    _ -> {:error, :invalid_json}
  end

  defp validate_marker(marker, identity, entries) do
    listed = MapSet.new(entries, & &1.key)
    payload_key = marker["payloadKey"]
    checksum = normalize_checksum(marker["sha256"])

    if marker["imageDigest"] == identity.digest and marker["rootfsSize"] == identity.size and
         marker["bakeFormat"] == identity.format and is_binary(marker["imageRef"]) and
         marker["imageRef"] != "" and is_binary(checksum) and is_binary(payload_key) and
         payload_key == identity.prefix <> "/" <> checksum <> ".ext4" and
         MapSet.member?(listed, payload_key) do
      :ok
    else
      {:error, :identity_mismatch}
    end
  end

  defp normalize_checksum(value) when is_binary(value) do
    checksum = value |> String.replace_prefix("sha256:", "") |> String.downcase()
    if Regex.match?(@digest, checksum), do: checksum
  end

  defp normalize_checksum(_value), do: nil

  defp parse_time(value) when is_binary(value) do
    case DateTime.from_iso8601(value) do
      {:ok, datetime, _offset} -> {:ok, DateTime.to_unix(datetime, :millisecond)}
      _ -> {:error, :invalid_time}
    end
  end

  defp parse_time(_value), do: {:error, :invalid_time}

  defp valid_file?(@marker_name), do: true
  defp valid_file?(file), do: Regex.match?(@payload, file)

  defp candidate(prefix, bytes, created_at_ms, entries, marker_key, image_ref, identity) do
    %{
      prefix: prefix,
      bytes: bytes,
      created_at_ms: created_at_ms,
      files: entries,
      marker_key: marker_key,
      image_ref: image_ref,
      identity: identity
    }
  end

  defp held(prefix, bytes, reason), do: %{prefix: prefix, bytes: bytes, reason: reason}

  defp log_plan(state, plan, eligible, held, held_keys) do
    mode = if state.enabled, do: "ARMED", else: "DRY RUN"

    Logger.info(
      "embervm rootfs remote retention: sweep (#{mode}): #{length(plan)}/#{length(eligible)} eligible prefixes, " <>
        "#{length(held)} held prefixes, #{length(held_keys)} legacy or malformed keys held"
    )

    Enum.each(plan, fn candidate ->
      Logger.info(
        "embervm rootfs remote retention: candidate prefix=#{candidate.prefix} bytes=#{candidate.bytes} created_at_ms=#{candidate.created_at_ms}"
      )
    end)
  end

  defp persist_manifest(state, plan, beyond_cap, held, held_keys) do
    now = state.wall_clock.()

    manifest = %{
      "ts_unix_ms" => now,
      "mode" => if(state.enabled, do: "armed", else: "dry_run"),
      "age_ms" => state.age_ms,
      "current_image_refs" => state.current_image_refs |> MapSet.to_list() |> Enum.sort(),
      "plan" => Enum.map(plan, &manifest_candidate/1),
      "eligible_beyond_cap" => Enum.map(beyond_cap, &manifest_candidate/1),
      "held" =>
        Enum.map(held, fn item ->
          %{"prefix" => item.prefix, "bytes" => item.bytes, "reason" => item.reason}
        end),
      "held_legacy_or_malformed_keys" => held_keys
    }

    body = manifest |> :json.encode() |> IO.iodata_to_binary()
    state.s3.put.("gc-manifests/rootfs-#{now}.json", body)
  rescue
    error -> {:error, {:manifest_failed, error}}
  end

  defp manifest_candidate(candidate) do
    %{
      "prefix" => candidate.prefix,
      "bytes" => candidate.bytes,
      "created_at_ms" => candidate.created_at_ms,
      "image_ref" => candidate.image_ref,
      "files" => Enum.map(candidate.files, & &1.key)
    }
  end

  defp apply_deletes(state, plan) do
    Enum.reduce_while(plan, [], fn candidate, deleted ->
      case recheck_candidate(state, candidate) do
        :ok ->
          case delete_candidate(state, candidate) do
            :ok ->
              Logger.info("embervm rootfs remote retention: DELETED prefix=#{candidate.prefix}")
              {:cont, [candidate.prefix | deleted]}

            {:error, reason} ->
              Logger.error(
                "embervm rootfs remote retention: delete failed prefix=#{candidate.prefix} error=#{inspect(reason)}"
              )

              {:halt, deleted}
          end

        {:blocked, reason} ->
          Logger.warning(
            "embervm rootfs remote retention: recheck held prefix=#{candidate.prefix} reason=#{reason}"
          )

          {:cont, deleted}
      end
    end)
    |> Enum.reverse()
  end

  defp recheck_candidate(state, %{marker_key: nil} = candidate) do
    marker_key = candidate.prefix <> "/" <> @marker_name

    case state.s3.get.(marker_key) do
      {:error, :not_found} -> :ok
      {:ok, _body} -> {:blocked, "marker appeared after plan"}
      {:error, _reason} -> {:blocked, "marker state unreadable"}
    end
  end

  defp recheck_candidate(state, candidate) do
    case state.s3.get.(candidate.marker_key) do
      {:ok, body} ->
        with {:ok, marker} <- decode_marker(body),
             :ok <- validate_marker(marker, candidate.identity, candidate.files),
             {:ok, uploaded_at_ms} <- parse_time(marker["uploadedAt"]) do
          cond do
            marker["imageRef"] != candidate.image_ref ->
              {:blocked, "marker changed after plan"}

            uploaded_at_ms != candidate.created_at_ms ->
              {:blocked, "marker age changed after plan"}

            MapSet.member?(state.current_image_refs, marker["imageRef"]) ->
              {:blocked, "image ref became current"}

            true ->
              :ok
          end
        else
          _ -> {:blocked, "marker became invalid"}
        end

      {:error, _reason} ->
        {:blocked, "marker state changed after plan"}
    end
  end

  defp delete_candidate(state, candidate) do
    files =
      Enum.sort_by(candidate.files, fn entry ->
        if entry.key == candidate.marker_key, do: 0, else: 1
      end)

    Enum.reduce_while(files, :ok, fn entry, :ok ->
      if String.starts_with?(entry.key, candidate.prefix <> "/") do
        case state.s3.delete.(entry.key) do
          :ok -> {:cont, :ok}
          {:error, reason} -> {:halt, {:error, {entry.key, reason}}}
        end
      else
        {:halt, {:error, {entry.key, :outside_prefix}}}
      end
    end)
  end
end
