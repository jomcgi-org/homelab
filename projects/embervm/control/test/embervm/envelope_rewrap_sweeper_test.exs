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

    {:ok, agent} =
      Agent.start_link(fn ->
        %{
          objects: initial,
          get_errors: %{},
          put_errors: %{},
          rewraps: [],
          put_attempts: [],
          puts: []
        }
      end)

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
        Agent.get(agent, fn state ->
          case Map.fetch(state.get_errors, key) do
            {:ok, reason} ->
              {:error, reason}

            :error ->
              case Map.get(state.objects, key) do
                nil -> {:error, :not_found}
                object -> {:ok, object.body, object.etag}
              end
          end
        end)
      end,
      put_if_match: fn key, body, etag ->
        Agent.get_and_update(agent, fn state ->
          state = %{state | put_attempts: [{key, body, etag} | state.put_attempts]}
          object = Map.get(state.objects, key)

          cond do
            Map.has_key?(state.put_errors, key) ->
              {{:error, Map.fetch!(state.put_errors, key)}, state}

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

  defp recorded_rewrap(agent) do
    fn _principal, wrapped, artifact ->
      Agent.update(agent, fn state ->
        %{state | rewraps: [{artifact, wrapped.epoch} | state.rewraps]}
      end)

      if wrapped.epoch == 1,
        do: {:ok, wrapped, :unchanged},
        else: {:ok, %{wrapped | epoch: 1}, :rewrapped}
    end
  end

  defp stored_envelope(object) do
    object.body
    |> :json.decode()
    |> Map.fetch!("envelope")
    |> Base.decode64!()
    |> Envelope.decode()
  end

  for scenario <- [
        :malformed_json,
        :json_array,
        :invalid_base64,
        :invalid_envelope,
        :get_unavailable,
        :get_missing_etag,
        :put_unavailable
      ] do
    test "#{scenario} vetoes completion while a healthy sibling progresses and repair completes" do
      scenario = unquote(scenario)
      faulty_key = "session/amd/faulty/snap-1/meta.json"
      healthy_key = "session/amd/healthy/snap-2/meta.json"
      faulty_artifact = %{"kind" => "session", "workload" => "faulty", "ref" => "snap-1"}
      healthy_artifact = %{"kind" => "session", "workload" => "healthy", "ref" => "snap-2"}
      repaired_body = meta(envelope("acct:faulty"))

      faulty_body =
        case scenario do
          :malformed_json -> "{"
          :json_array -> "[]"
          :invalid_base64 -> meta(envelope("acct:faulty"), %{"envelope" => "!invalid!"})
          :invalid_envelope -> meta(envelope("acct:faulty"), %{"envelope" => Base.encode64(<<0>>)})
          _ -> repaired_body
        end

      {agent, s3} =
        new_s3(%{
          faulty_key => faulty_body,
          healthy_key => meta(envelope("acct:healthy"))
        })

      Agent.update(agent, fn state ->
        case scenario do
          :get_unavailable -> %{state | get_errors: %{faulty_key => :unavailable}}
          :get_missing_etag -> %{state | get_errors: %{faulty_key => :missing_etag}}
          :put_unavailable -> %{state | put_errors: %{faulty_key => :unavailable}}
          _ -> state
        end
      end)

      original = Agent.get(agent, & &1.objects)
      sweeper = start_sweeper(s3, recorded_rewrap(agent), max_artifacts: 3)

      assert {:ok, result} = EnvelopeRewrapSweeper.sweep_now(sweeper)
      assert result.discovered == 2
      assert result.scanned == 2
      assert result.capped == 0
      assert result.conflicts == 0
      assert result.refused == 0
      assert result.plaintext == 0
      assert result.current == 0
      assert result.rewrapped == 1
      refute result.complete

      state = Agent.get(agent, & &1)
      assert state.objects[faulty_key] == original[faulty_key]
      assert [{^healthy_key, healthy_etag}] = state.puts
      assert healthy_etag == original[healthy_key].etag
      assert {:ok, %Envelope{epoch: 1}} = stored_envelope(state.objects[healthy_key])

      if scenario == :put_unavailable do
        assert result.errors == 1
        assert result.invalid == 0

        assert Enum.sort(state.rewraps) ==
                 Enum.sort([{faulty_artifact, 0}, {healthy_artifact, 0}])

        assert Enum.sort(Enum.map(state.put_attempts, &elem(&1, 0))) ==
                 Enum.sort([faulty_key, healthy_key])

        assert [{^faulty_key, attempted_body, faulty_etag}] =
                 Enum.filter(state.put_attempts, &(elem(&1, 0) == faulty_key))

        assert faulty_etag == original[faulty_key].etag
        assert {:ok, %Envelope{epoch: 1}} = stored_envelope(%{body: attempted_body})
      else
        if scenario in [:get_unavailable, :get_missing_etag] do
          assert result.errors == 1
          assert result.invalid == 0
        else
          assert result.invalid == 1
          assert result.errors == 0
        end

        assert state.rewraps == [{healthy_artifact, 0}]
        assert [{^healthy_key, _body, ^healthy_etag}] = state.put_attempts
      end

      Agent.update(agent, fn state ->
        repaired = %{body: repaired_body, etag: "\"#{:erlang.phash2(repaired_body)}\""}

        %{
          state
          | objects: Map.put(state.objects, faulty_key, repaired),
            get_errors: %{},
            put_errors: %{},
            rewraps: [],
            put_attempts: [],
            puts: []
        }
      end)

      assert {:ok,
              %{
                discovered: 2,
                scanned: 2,
                capped: 0,
                conflicts: 0,
                refused: 0,
                invalid: 0,
                errors: 0,
                plaintext: 0,
                current: 1,
                rewrapped: 1,
                complete: true
              }} = EnvelopeRewrapSweeper.sweep_now(sweeper)

      repaired_state = Agent.get(agent, & &1)

      assert Enum.sort(repaired_state.rewraps) ==
               Enum.sort([{faulty_artifact, 0}, {healthy_artifact, 1}])

      assert [{^faulty_key, _body, repaired_etag}] = repaired_state.put_attempts
      assert repaired_state.puts == [{faulty_key, repaired_etag}]
      assert repaired_state.objects[healthy_key] == state.objects[healthy_key]
      assert {:ok, %Envelope{epoch: 1}} = stored_envelope(repaired_state.objects[faulty_key])
    end
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
