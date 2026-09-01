defmodule Embervm.LogFormatter do
  @moduledoc """
  Structured JSON log formatter (Task 13, logging half): emits ONE JSON object
  per log line to stdout so pod-log pipelines ingest structured fields
  (level, message, and the whitelisted embervm metadata) rather than parsing free
  text. This is the Erlang `:logger` formatter behaviour (`format/2`), wired as
  the default handler's formatter in `config/config.exs`.

  Defensive by construction: it must NEVER raise (a formatter that crashes takes
  down logging), so every branch is guarded and falls back to a plain line. It
  only encodes a fixed whitelist of metadata keys, so an arbitrary un-encodable
  term in the metadata (a pid, ref, or tuple) can never break the JSON encode.
  """

  # Metadata keys the control plane sets on structured log calls (plus a few the
  # runtime always provides). Anything else in metadata is dropped, so the JSON
  # encode only ever sees strings/numbers. A caller adding new metadata MUST add
  # its keys here or they are silently dropped.
  @meta_keys [
    :task_id,
    :workload,
    :principal,
    :ref,
    :node_id,
    :reason,
    :attempt,
    :kind,
    :mfa,
    :error,
    # CapacityObserver metadata.
    :instance_id,
    :vm_id,
    :elapsed_ms,
    :alarm_threshold_ms,
    :escape_threshold_ms,
    :size_class,
    :mem_budget_mib,
    :mem_headroom_mib,
    :mem_reserved_mib,
    :admits_on_reservation,
    :live_vms,
    :max_live_vms,
    :nameplate_mib,
    :total_working_set_mib,
    :guest_free?,
    :cp_reserved_mib,
    # StatefulSweeper pressure-state transitions.
    :brick_id,
    :from,
    :to,
    # Base retention manifest and accounting metadata.
    :path,
    :size_bytes,
    :vendor,
    :age_seconds,
    :reason_unreferenced,
    :candidates,
    :bytes_reclaimable,
    :base_generation,
    :bases_seen_on_disk,
    :bases_in_desired_set,
    :bases_protected_by_refcounts,
    :bases_excluded_as_too_young,
    :bases_selected_as_candidates,
    :total_candidates,
    :shown,
    :hidden
  ]

  @doc """
  The Erlang `:logger` formatter callback. `event` is `%{level, msg, meta}`;
  returns the iodata line written to the handler's device.
  """
  @spec format(:logger.log_event(), :logger.formatter_config()) :: iodata()
  def format(event, config) do
    try do
      do_format(event)
    rescue
      _ -> fallback(event, config)
    catch
      _, _ -> fallback(event, config)
    end
  end

  defp do_format(%{level: level, msg: msg, meta: meta}) do
    base = %{
      "time" => timestamp(meta),
      "level" => to_string(level),
      "message" => message(msg)
    }

    entry = Map.merge(base, whitelisted_meta(meta))
    [:json.encode(entry), ?\n]
  end

  # -- message extraction (the three :logger msg shapes) ---------------------

  defp message({:string, chardata}), do: chardata_to_string(chardata)
  defp message({:report, report}) when is_map(report), do: inspect(report)
  defp message({:report, kw}) when is_list(kw), do: inspect(kw)

  defp message({format, args}) when is_list(args) do
    chardata_to_string(:io_lib.format(format, args))
  end

  defp message(other), do: inspect(other)

  defp chardata_to_string(chardata) do
    IO.iodata_to_binary(chardata)
  rescue
    _ -> inspect(chardata)
  end

  # -- metadata --------------------------------------------------------------

  defp whitelisted_meta(meta) do
    for key <- @meta_keys, Map.has_key?(meta, key), into: %{} do
      {Atom.to_string(key), jsonable(Map.get(meta, key))}
    end
  end

  defp jsonable(v) when is_binary(v), do: v
  defp jsonable(v) when is_integer(v) or is_float(v), do: v
  defp jsonable(v) when is_boolean(v), do: v
  defp jsonable(v) when is_atom(v), do: Atom.to_string(v)
  defp jsonable(v), do: inspect(v)

  # meta.time is system microseconds since epoch (OTP :logger). ISO8601 UTC keeps
  # downstream time parsing trivial; a bad/absent stamp falls back to the raw value.
  defp timestamp(%{time: us}) when is_integer(us) do
    case DateTime.from_unix(us, :microsecond) do
      {:ok, dt} -> DateTime.to_iso8601(dt)
      _ -> Integer.to_string(us)
    end
  end

  defp timestamp(_), do: ""

  # Last-resort plain line if anything above fails, so a log is never lost and the
  # handler never sees an exception from this formatter.
  defp fallback(%{level: level, msg: msg}, _config) do
    text =
      try do
        message(msg)
      rescue
        _ -> "unformattable log message"
      catch
        _, _ -> "unformattable log message"
      end

    [to_string(level), ?\s, text, ?\n]
  end

  defp fallback(_event, _config), do: "log\n"
end
