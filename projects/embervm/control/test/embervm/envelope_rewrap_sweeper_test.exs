defmodule Embervm.EnvelopeRewrapSweeperTest do
  use ExUnit.Case, async: true

  alias Embervm.EnvelopeRewrapSweeper
  alias Embervm.KeyService.Envelope

  defp envelope(principal, epoch \\ 0) do
    %Envelope{
      principal: principal,
      epoch: epoch,
      nonce: :binary.copy(<<epoch + 1>>, 12),
      tag: :binary.copy(<<epoch + 2>>, 16),
      wrapped_key: :binary.copy(<<epoch + 3>>, 32)
    }
  end

  defp meta(envelope, extra \\ %{}) do
    %{
      "files" => %{"memfile" => %{"size" => 7, "sha256" => "abc"}},
      "generation" => 41,
      "createdAtUnixMs" => 1_750_000_000_000,
      "cpuVendor" => "amd",
      "envelope" => envelope |> Envelope.encode() |> Base.encode64()
    }
    |> Map.merge(extra)
    |> :json.encode()
    |> IO.iodata_to_binary()
  end

  defp plaintext_meta do
    :json.encode(%{
      "files" => %{"memfile" => %{"size" => 7, "sha256" => "abc"}},
      "generation" => 0,
      "createdAtUnixMs" => 1_750_000_000_000
    })
    |> IO.iodata_to_binary()
  end

  defp new_s3(objects, opts \\ []) do
    conflict_keys = MapSet.new(Keyword.get(opts, :conflict_keys, []))
    list_error = Keyword.get(opts, :list_error)

    initial =
      Map.new(objects, fn {key, body} ->
        {key, %{body: body, etag: "\"#{:erlang.phash2(body)}\""}}
      end)

    {:ok, agent} = Agent.start_link(fn -> %{objects: initial, puts: []} end)

    s3 = %{
      list: fn prefix ->
        if list_error == prefix do
          {:error, :unavailable}
        else
          entries =
            Agent.get(agent, & &1.objects)
            |> Map.keys()
            |> Enum.filter(&String.starts_with?(&1, prefix))
            |> Enum.map(&%{key: &1, size: 1, last_modified_ms: 1})

          {:ok, entries}
        end
      end,
      get_with_etag: fn key ->
        case Agent.get(agent, &Map.get(&1.objects, key)) do
          nil -> {:error, :not_found}
          object -> {:ok, object.body, object.etag}
        end
      end,
      put_if_match: fn key, body, etag ->
        Agent.get_and_update(agent, fn state ->
          object = Map.get(state.objects, key)

          cond do
            MapSet.member?(conflict_keys, key) ->
              {{:error, :precondition_failed}, state}

            is_nil(object) or object.etag != etag ->
              {{:error, :precondition_failed}, state}

            true ->
              replacement = %{body: body, etag: "\"#{:erlang.phash2(body)}\""}

              {:ok,
               %{
                 state
                 | objects: Map.put(state.objects, key, replacement),
                   puts: [{key, etag} | state.puts]
               }}
          end
        end)
      end
    }

    {agent, s3}
  end

  defp start_sweeper(s3, rewrap, opts \\ []) do
    start_supervised!(
      {EnvelopeRewrapSweeper,
       [
         name: nil,
         enabled: true,
         s3: s3,
         rewrap: rewrap,
         sweep_interval_ms: 0,
         max_artifacts: Keyword.get(opts, :max_artifacts, 100),
         concurrency: Keyword.get(opts, :concurrency, 4)
       ]}
    )
  end

  test "rewrap changes only the envelope under the ETag it read" do
    stale_key = "stateful/amd/demo/snap-1/meta.json"
    current_key = "volume/demo/meta.json"
    plain_key = "session/intel/demo/snap-plain/meta.json"
    old = envelope("acct:demo", 0)
    current = envelope("acct:current", 4)

    {agent, s3} =
      new_s3(%{
        stale_key => meta(old, %{"futureField" => %{"preserved" => true}}),
        current_key => meta(current),
        plain_key => plaintext_meta()
      })

    rewrap = fn
      "acct:demo", wrapped, artifact ->
        assert artifact == %{"kind" => "stateful", "workload" => "demo", "ref" => "snap-1"}
        {:ok, %{wrapped | epoch: 1}, :rewrapped}

      "acct:current", wrapped, artifact ->
        assert artifact == %{"kind" => "volume", "workload" => "demo", "ref" => ""}
        {:ok, wrapped, :unchanged}
    end

    sweeper = start_sweeper(s3, rewrap)

    assert {:ok,
            %{
              complete: true,
              discovered: 3,
              scanned: 3,
              rewrapped: 1,
              current: 1,
              plaintext: 1,
              conflicts: 0
            }} = EnvelopeRewrapSweeper.sweep_now(sweeper)

    state = Agent.get(agent, & &1)
    assert [{^stale_key, original_etag}] = state.puts
    assert original_etag != ""

    rewritten = state.objects[stale_key].body |> :json.decode()
    assert rewritten["files"] == %{"memfile" => %{"size" => 7, "sha256" => "abc"}}
    assert rewritten["generation"] == 41
    assert rewritten["createdAtUnixMs"] == 1_750_000_000_000
    assert rewritten["cpuVendor"] == "amd"
    assert rewritten["futureField"] == %{"preserved" => true}

    assert {:ok, replacement} =
             rewritten["envelope"] |> Base.decode64!() |> Envelope.decode()

    assert replacement.principal == "acct:demo"
    assert replacement.epoch == 1
  end

  test "a concurrent metadata replacement becomes a conflict and is never overwritten" do
    key = "session/amd/demo/snap-1/meta.json"
    original = meta(envelope("acct:demo"))
    {agent, s3} = new_s3(%{key => original}, conflict_keys: [key])

    rewrap = fn _principal, wrapped, _artifact ->
      {:ok, %{wrapped | epoch: 1}, :rewrapped}
    end

    sweeper = start_sweeper(s3, rewrap)

    assert {:ok, %{complete: false, conflicts: 1, rewrapped: 0}} =
             EnvelopeRewrapSweeper.sweep_now(sweeper)

    state = Agent.get(agent, & &1)
    assert state.puts == []
    assert state.objects[key].body == original
  end

  test "one refused customer rewrap does not prevent an unrelated artifact from progressing" do
    refused_key = "session/amd/refused/snap-1/meta.json"
    healthy_key = "serving/intel/healthy/snap-2/meta.json"

    {agent, s3} =
      new_s3(%{
        refused_key => meta(envelope("acct:refused")),
        healthy_key => meta(envelope("acct:healthy"))
      })

    rewrap = fn
      "acct:refused", _wrapped, _artifact -> {:error, :kms_refused}
      "acct:healthy", wrapped, _artifact -> {:ok, %{wrapped | epoch: 1}, :rewrapped}
    end

    sweeper = start_sweeper(s3, rewrap, concurrency: 2)

    assert {:ok, %{complete: false, refused: 1, rewrapped: 1, errors: 0}} =
             EnvelopeRewrapSweeper.sweep_now(sweeper)

    assert [{^healthy_key, _etag}] = Agent.get(agent, & &1.puts)
  end

  test "a bounded partial pass cannot report transition completion" do
    objects = %{
      "session/amd/a/one/meta.json" => meta(envelope("acct:a")),
      "session/amd/b/two/meta.json" => meta(envelope("acct:b"))
    }

    {agent, s3} = new_s3(objects)

    rewrap = fn _principal, wrapped, _artifact ->
      {:ok, %{wrapped | epoch: wrapped.epoch + 1}, :rewrapped}
    end

    sweeper = start_sweeper(s3, rewrap, max_artifacts: 1)

    assert {:ok,
            %{complete: false, discovered: 2, scanned: 1, capped: 1, rewrapped: 1}} =
             EnvelopeRewrapSweeper.sweep_now(sweeper)

    assert {:ok,
            %{complete: false, discovered: 2, scanned: 1, capped: 1, rewrapped: 1}} =
             EnvelopeRewrapSweeper.sweep_now(sweeper)

    assert agent |> Agent.get(& &1.puts) |> Enum.map(&elem(&1, 0)) |> Enum.sort() ==
             Map.keys(objects) |> Enum.sort()
  end

  test "listing failures abort without presenting a partial result" do
    {_agent, s3} = new_s3(%{}, list_error: "serving/")
    sweeper = start_sweeper(s3, fn _, wrapped, _ -> {:ok, wrapped, :unchanged} end)

    assert {:error, {:list_failed, "serving/", :unavailable}} =
             EnvelopeRewrapSweeper.sweep_now(sweeper)
  end

  test "artifact identity parsing covers portable, vendored, and legacy layouts" do
    assert {:ok, %{"kind" => "volume", "workload" => "demo", "ref" => ""}} =
             EnvelopeRewrapSweeper.artifact_from_meta_key("volume/demo/meta.json")

    assert {:ok,
            %{
              "kind" => "session-workspace",
              "workload" => "demo",
              "ref" => "lineage-1"
            }} =
             EnvelopeRewrapSweeper.artifact_from_meta_key(
               "session-workspace/demo/lineage-1/meta.json"
             )

    assert {:ok, %{"kind" => "group_set", "workload" => "group-1", "ref" => "set-1"}} =
             EnvelopeRewrapSweeper.artifact_from_meta_key(
               "group_set/amd/group-1/set-1/meta.json"
             )

    assert {:ok, %{"kind" => "stateful", "workload" => "legacy", "ref" => "snap-1"}} =
             EnvelopeRewrapSweeper.artifact_from_meta_key(
               "stateful/legacy/snap-1/meta.json"
             )

    assert {:error, {:invalid_artifact_key, _}} =
             EnvelopeRewrapSweeper.artifact_from_meta_key(
               "stateful/amd/snap-ambiguous/meta.json"
             )
  end
end
