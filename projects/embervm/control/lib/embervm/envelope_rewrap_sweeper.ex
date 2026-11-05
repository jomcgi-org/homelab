defmodule Embervm.EnvelopeRewrapSweeper do
  @moduledoc """
  Bounded background reconciler for stale artifact data-key envelopes.

  The worker lists only EmberVM's mutable principal-artifact namespaces, reads
  each `meta.json` with its object-store ETag, asks `Embervm.KeyService` whether
  the envelope needs rewrapping, and conditionally replaces only the metadata
  object. Artifact payload objects are never read or rewritten.

  The ETag compare-and-swap is the correctness boundary. An export can replace
  `meta.json` with a newer generation and file manifest while a rewrap is in
  flight. An unconditional PUT would restore the stale manifest and make the
  newer payload invisible. A 412 is therefore counted as a conflict and left
  for the next sweep, never retried against the stale body.

  A result is `complete: true` only when the full listing fit within the bound
  and every discovered marker was current, plaintext, or successfully
  rewrapped. Operators may use that clean result as evidence before separately
  raising an epoch floor or retiring a previous root or source-custody grant.
  The worker never performs those retirement actions itself.
  """

  use GenServer
  require Logger

  alias Embervm.KeyService
  alias Embervm.KeyService.Envelope
  alias Embervm.S3Client

  @prefixes ["stateful/", "session/", "serving/", "session-workspace/", "group_set/", "volume/"]
  @vendored_kinds ["stateful", "session", "serving", "group_set"]
  @vendors ["amd", "intel"]
  @sweep_interval_ms 3_600_000
  @max_artifacts 100
  @concurrency 8
  @artifact_timeout_ms 60_000

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc "Run one complete bounded pass synchronously."
  @spec sweep_now(GenServer.server()) :: {:ok, map()} | {:error, term()}
  def sweep_now(server \\ __MODULE__), do: GenServer.call(server, :sweep_now, :infinity)

  @impl true
  def init(opts) do
    endpoint = Keyword.get(opts, :endpoint, "")
    bucket = Keyword.get(opts, :bucket, "embervm")
    access_key_id = Keyword.get(opts, :access_key_id, "") || ""
    secret_access_key = Keyword.get(opts, :secret_access_key, "") || ""

    credential_opts = credentials(access_key_id, secret_access_key)
    client = S3Client.new(endpoint, bucket, credential_opts)

    s3 =
      Keyword.get_lazy(opts, :s3, fn ->
        if client do
          %{
            list: fn prefix -> S3Client.list_all(client, prefix) end,
            get_with_etag: fn key -> S3Client.get_with_etag(client, key) end,
            put_if_match: fn key, body, etag ->
              S3Client.put_if_match(client, key, body, etag)
            end
          }
        end
      end)

    key_service = Keyword.get(opts, :key_service, KeyService)

    rewrap =
      Keyword.get(opts, :rewrap, fn principal, envelope, artifact ->
        KeyService.rewrap(key_service, principal, envelope, artifact)
      end)

    state = %{
      enabled: Keyword.get(opts, :enabled, false),
      s3: s3,
      rewrap: rewrap,
      vendors: Keyword.get(opts, :vendors, @vendors),
      max_artifacts: Keyword.get(opts, :max_artifacts, @max_artifacts),
      concurrency: Keyword.get(opts, :concurrency, @concurrency),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms),
      cursor: nil
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_call(:sweep_now, _from, state) do
    {reply, next_state} = run_sweep(state)
    {:reply, reply, next_state}
  end

  @impl true
  def handle_info(:sweep, state) do
    {_reply, next_state} = run_sweep(state)
    schedule_sweep(next_state)
    {:noreply, next_state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  defp schedule_sweep(%{sweep_interval_ms: interval}) when interval > 0 do
    Process.send_after(self(), :sweep, interval)
    :ok
  end

  defp schedule_sweep(_state), do: :ok

  defp run_sweep(%{enabled: false} = state), do: {{:error, :disabled}, state}
  defp run_sweep(%{s3: nil} = state), do: {{:error, :store_disabled}, state}

  defp run_sweep(state) do
    with {:ok, keys} <- list_meta_keys(state) do
      candidates = keys_after_cursor(keys, state.cursor)
      {selected, remaining} = Enum.split(candidates, max(state.max_artifacts, 0))
      capped = length(keys) - length(selected)

      result =
        selected
        |> Task.async_stream(&reconcile_one(state, &1),
          max_concurrency: max(state.concurrency, 1),
          ordered: false,
          timeout: @artifact_timeout_ms,
          on_timeout: :kill_task
        )
        |> Enum.reduce(empty_result(length(keys), length(selected), capped), &record_outcome/2)
        |> finish_result()

      log_result(result)
      emit_result(result)

      next_cursor =
        if remaining == [] do
          nil
        else
          List.last(selected) || state.cursor
        end

      {{:ok, result}, %{state | cursor: next_cursor}}
    else
      error ->
        Logger.warning("embervm envelope rewrap: sweep aborted reason=#{inspect(error)}")
        :telemetry.execute([:embervm, :envelope_rewrap, :sweep_error], %{count: 1}, %{reason: error})
        {error, state}
    end
  end

  # A per-pass cap must not starve keys after the first sorted batch. Continue
  # strictly after the last attempted marker and reset at the end of the listing.
  # Each capped result still reports complete=false; an operator must raise the
  # bound high enough for one full clean pass before retiring old key material.
  defp keys_after_cursor(keys, nil), do: keys

  defp keys_after_cursor(keys, cursor) do
    case Enum.drop_while(keys, &(&1 <= cursor)) do
      [] -> keys
      remaining -> remaining
    end
  end

  defp list_meta_keys(state) do
    Enum.reduce_while(@prefixes, {:ok, []}, fn prefix, {:ok, acc} ->
      case state.s3.list.(prefix) do
        {:ok, entries} when is_list(entries) ->
          if Enum.all?(entries, &listed_under?(&1, prefix)) do
            meta_keys =
              entries
              |> Enum.map(& &1.key)
              |> Enum.filter(&String.ends_with?(&1, "/meta.json"))

            {:cont, {:ok, meta_keys ++ acc}}
          else
            {:halt, {:error, {:allowlist_violation, prefix}}}
          end

        {:error, reason} ->
          {:halt, {:error, {:list_failed, prefix, reason}}}

        other ->
          {:halt, {:error, {:bad_list_response, prefix, other}}}
      end
    end)
    |> case do
      {:ok, keys} -> {:ok, keys |> Enum.uniq() |> Enum.sort()}
      error -> error
    end
  end

  defp listed_under?(%{key: key}, prefix) when is_binary(key),
    do: String.starts_with?(key, prefix)

  defp listed_under?(_entry, _prefix), do: false

  defp reconcile_one(state, key) do
    with {:ok, artifact} <- artifact_from_meta_key(key, state.vendors),
         {:ok, body, etag} <- state.s3.get_with_etag.(key),
         {:ok, meta} <- decode_meta(body),
         {:ok, envelope} <- decode_envelope(meta) do
      case envelope do
        nil ->
          {:plaintext, key, nil}

        %Envelope{principal: principal} ->
          persist_rewrap(state, key, etag, meta, principal, envelope, artifact)
      end
    else
      {:error, :not_found} -> {:conflicts, key, :metadata_disappeared}
      {:error, {:invalid_artifact_key, _key} = reason} -> {:invalid, key, reason}
      {:error, reason} -> classify_read_error(key, reason)
    end
  rescue
    error -> {:errors, key, {:raised, error}}
  catch
    kind, reason -> {:errors, key, {kind, reason}}
  end

  defp persist_rewrap(state, key, etag, meta, principal, envelope, artifact) do
    case state.rewrap.(principal, envelope, artifact) do
      {:ok, _envelope, :unchanged} ->
        {:current, key, nil}

      {:ok, %Envelope{} = replacement, :rewrapped} ->
        body =
          meta
          |> Map.put("envelope", replacement |> Envelope.encode() |> Base.encode64())
          |> :json.encode()
          |> IO.iodata_to_binary()

        case state.s3.put_if_match.(key, body, etag) do
          :ok -> {:rewrapped, key, nil}
          {:error, :precondition_failed} -> {:conflicts, key, :etag_changed}
          {:error, reason} -> {:errors, key, {:put_failed, reason}}
          other -> {:errors, key, {:bad_put_response, other}}
        end

      {:error, reason} ->
        {:refused, key, reason}

      other ->
        {:errors, key, {:bad_rewrap_response, other}}
    end
  end

  defp classify_read_error(key, reason)
       when reason in [:invalid_json, :invalid_meta, :invalid_envelope_encoding, :bad_envelope],
       do: {:invalid, key, reason}

  defp classify_read_error(key, reason), do: {:errors, key, {:get_failed, reason}}

  defp decode_meta(body) when is_binary(body) do
    case :json.decode(body) do
      meta when is_map(meta) -> {:ok, meta}
      _ -> {:error, :invalid_meta}
    end
  rescue
    _ -> {:error, :invalid_json}
  catch
    _, _ -> {:error, :invalid_json}
  end

  defp decode_envelope(meta) do
    case Map.get(meta, "envelope") do
      value when value in [nil, ""] ->
        {:ok, nil}

      encoded when is_binary(encoded) ->
        with {:ok, binary} <- Base.decode64(encoded),
             {:ok, envelope} <- Envelope.decode(binary) do
          {:ok, envelope}
        else
          :error -> {:error, :invalid_envelope_encoding}
          {:error, :bad_envelope} -> {:error, :bad_envelope}
        end

      _ ->
        {:error, :invalid_envelope_encoding}
    end
  end

  @doc false
  @spec artifact_from_meta_key(String.t(), [String.t()]) ::
          {:ok, map()} | {:error, {:invalid_artifact_key, String.t()}}
  def artifact_from_meta_key(key, vendors \\ @vendors)

  def artifact_from_meta_key(key, vendors) when is_binary(key) do
    artifact =
      case String.split(key, "/") do
        ["volume", workload, "meta.json"] ->
          artifact("volume", workload, "")

        ["volume", workload, ref, "meta.json"] ->
          artifact("volume", workload, ref)

        ["session-workspace", workload, ref, "meta.json"] ->
          artifact("session-workspace", workload, ref)

        [kind, vendor, workload, ref, "meta.json"] when kind in @vendored_kinds ->
          if vendor in vendors and workload not in vendors,
            do: artifact(kind, workload, ref),
            else: nil

        [kind, workload, ref, "meta.json"] when kind in @vendored_kinds ->
          if workload in vendors, do: nil, else: artifact(kind, workload, ref)

        _ ->
          nil
      end

    case artifact do
      %{"workload" => workload, "ref" => ref} = parsed
      when workload != "" and is_binary(ref) and ref != "meta.json" ->
        {:ok, parsed}

      _ ->
        {:error, {:invalid_artifact_key, key}}
    end
  end

  def artifact_from_meta_key(key, _vendors),
    do: {:error, {:invalid_artifact_key, inspect(key)}}

  defp artifact(kind, workload, ref),
    do: %{"kind" => kind, "workload" => workload, "ref" => ref}

  defp empty_result(discovered, scanned, capped) do
    %{
      discovered: discovered,
      scanned: scanned,
      plaintext: 0,
      current: 0,
      rewrapped: 0,
      conflicts: 0,
      refused: 0,
      invalid: 0,
      errors: 0,
      capped: capped
    }
  end

  defp record_outcome({:ok, {outcome, _key, _reason}}, result)
       when outcome in [:plaintext, :current, :rewrapped],
       do: Map.update!(result, outcome, &(&1 + 1))

  defp record_outcome({:ok, {outcome, key, reason}}, result)
       when outcome in [:conflicts, :refused, :invalid, :errors] do
    Logger.warning(
      "embervm envelope rewrap: #{outcome} key=#{key} reason=#{inspect(reason)}"
    )

    Map.update!(result, outcome, &(&1 + 1))
  end

  defp record_outcome({:exit, reason}, result) do
    Logger.warning("embervm envelope rewrap: task exited reason=#{inspect(reason)}")
    Map.update!(result, :errors, &(&1 + 1))
  end

  defp finish_result(result) do
    complete =
      result.capped == 0 and result.conflicts == 0 and result.refused == 0 and
        result.invalid == 0 and result.errors == 0

    Map.put(result, :complete, complete)
  end

  defp log_result(result) do
    Logger.info(
      "embervm envelope rewrap: sweep complete=#{result.complete} " <>
        "discovered=#{result.discovered} scanned=#{result.scanned} plaintext=#{result.plaintext} " <>
        "current=#{result.current} rewrapped=#{result.rewrapped} conflicts=#{result.conflicts} " <>
        "refused=#{result.refused} invalid=#{result.invalid} errors=#{result.errors} capped=#{result.capped}"
    )
  end

  defp emit_result(result) do
    measurements = Map.drop(result, [:complete])
    :telemetry.execute([:embervm, :envelope_rewrap, :sweep], measurements, %{complete: result.complete})
  end

  defp credentials("", ""), do: []

  defp credentials("", _secret) do
    Logger.error(
      "embervm envelope rewrap: both access key ID and secret access key are required; using anonymous requests"
    )

    []
  end

  defp credentials(_access_key_id, "") do
    Logger.error(
      "embervm envelope rewrap: both access key ID and secret access key are required; using anonymous requests"
    )

    []
  end

  defp credentials(access_key_id, secret_access_key),
    do: [access_key_id: access_key_id, secret_access_key: secret_access_key]
end
