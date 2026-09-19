defmodule Embervm.StoreProbeTest do
  use ExUnit.Case, async: true
  import ExUnit.CaptureLog

  alias Embervm.StoreProbe

  defmodule StubS3Client do
    def get(%{results: agent}, "probe/.keep") do
      Agent.get_and_update(agent, fn
        [{:raise, error} | _rest] -> raise error
        [{:throw, reason} | _rest] -> throw(reason)
        [result | rest] -> {result, rest}
      end)
    end
  end

  defmodule CountingS3Client do
    def get(%{calls: calls}, _key), do: Agent.update(calls, &(&1 + 1))
  end

  test "a missing object and an HTTP error both prove the TLS path" do
    for result <- [{:error, :not_found}, {:error, {:unexpected_status, 403}}] do
      probe = start_probe([result])
      assert eventually_status(probe).state == :ok
    end
  end

  test "TLS alerts and transport errors are degraded and observable" do
    for reason <- [
          {:tls_alert, {:unknown_ca, "certificate unknown"}},
          {:transport_error, :closed}
        ] do
      probe = start_probe([{:error, reason}])
      status = eventually_status(probe)

      assert status.state == :degraded
      assert status.reason =~ inspect(reason)
      assert status.last_checked_at
      assert status.last_ok_at == nil
    end
  end

  test "nil and plaintext clients are disabled and never fetched" do
    nil_probe = start_supervised_probe(client: nil, s3_client: StubS3Client)

    assert StoreProbe.status(nil_probe).state == :disabled

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    client = %{endpoint: "http://seaweedfs:8333", calls: calls}
    plaintext_probe =
      start_supervised_probe(client: client, s3_client: CountingS3Client, interval_ms: 10)

    Process.sleep(30)
    assert StoreProbe.status(plaintext_probe).state == :disabled
    assert Agent.get(calls, & &1) == 0
  end

  test "a successful next tick clears a degraded result without a restart" do
    probe = start_probe([{:error, {:tls_alert, :unknown_ca}}, {:error, :not_found}])
    assert eventually_status(probe).state == :degraded

    send(probe, :probe)
    status = eventually_status(probe, :ok)

    assert status.state == :ok
    assert status.reason == nil
    assert status.last_ok_at == status.last_checked_at
  end

  test "a throwing client becomes degraded instead of crashing the probe" do
    probe = start_probe([{:throw, :boom}, {:raise, RuntimeError.exception("bad client")}])
    status = eventually_status(probe)

    assert status.state == :degraded
    assert status.reason =~ "boom"
    assert Process.alive?(probe)

    send(probe, :probe)
    status = eventually_status(probe, fn status -> status.reason =~ "bad client" end)
    assert status.state == :degraded
    assert Process.alive?(probe)
  end

  test "every degraded check warns and a later recovery logs once" do
    log =
      capture_log(fn ->
        probe = start_probe([{:error, {:tls_alert, :unknown_ca}}, {:error, :not_found}])
        assert eventually_status(probe).state == :degraded
        send(probe, :probe)
        assert eventually_status(probe, :ok).state == :ok
      end)

    assert log =~ "embervm store probe: store fetch failed"
    assert log =~ "embervm store probe: store fetch recovered"
  end

  defp start_probe(results) do
    {:ok, agent} = Agent.start_link(fn -> results end)

    start_supervised_probe(
      client: agent,
      endpoint: "https://storage.example",
      s3_client: StubS3Client,
      interval_ms: 60_000
    )
  end

  defp start_supervised_probe(opts) do
    client =
      case Keyword.pop(opts, :endpoint) do
        {nil, opts} -> Keyword.get(opts, :client)
        {endpoint, opts} -> %{endpoint: endpoint, results: Keyword.fetch!(opts, :client)}
      end

    opts = Keyword.put(opts, :client, client)
    spec = Supervisor.child_spec({StoreProbe, Keyword.put(opts, :name, unique_name())}, id: make_ref())
    start_supervised!(spec)
  end

  defp eventually_status(probe), do: eventually_status(probe, fn status -> status.last_checked_at != nil end)
  defp eventually_status(probe, state) when is_atom(state), do: eventually_status(probe, &(&1.state == state))

  defp eventually_status(probe, predicate, attempts \\ 100)
  defp eventually_status(probe, _predicate, 0), do: StoreProbe.status(probe)

  defp eventually_status(probe, predicate, attempts) do
    status = StoreProbe.status(probe)

    if predicate.(status) do
      status
    else
      Process.sleep(10)
      eventually_status(probe, predicate, attempts - 1)
    end
  end

  defp unique_name do
    String.to_atom("store_probe_test_#{System.unique_integer([:positive, :monotonic])}")
  end
end
