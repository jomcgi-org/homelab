defmodule Embervm.KeyServiceTest do
  use ExUnit.Case, async: true

  alias Embervm.KeyService
  alias Embervm.KeyService.Envelope
  alias Embervm.OpLog.SQLite

  @root :binary.copy(<<0>>, 32)
  @principal "system:serviceaccount:embervm:test"

  defmodule RecordingOpLog do
    def start(mode \\ :ok), do: Agent.start_link(fn -> %{mode: mode, seq: 0, ops: []} end)

    def set_mode(agent, mode), do: Agent.update(agent, fn state -> %{state | mode: mode} end)

    def load_key_epochs(_agent), do: {:ok, []}

    def append(agent, op) do
      case Agent.get(agent, & &1.mode) do
        :fail ->
          {:error, :disk_full}

        {:block, waiter} ->
          send(waiter, {:append_blocked, self(), op})

          receive do
            :continue -> record(agent, op)
          end

        :ok ->
          record(agent, op)
      end
    end

    defp record(agent, op) do
      seq =
        Agent.get_and_update(agent, fn state ->
          next = state.seq + 1
          {next, %{state | seq: next, ops: [op | state.ops]}}
        end)

      {:ok, seq}
    end
  end

  defmodule FakeCustomerKMS do
    @behaviour Embervm.CustomerKMS

    def issue(config, principal, artifact) do
      Agent.get_and_update(config.agent, fn state ->
        call = {:issue, principal, artifact, config.key_ref}
        {reply(state, {:ok, state.data_key, state.wrapped_key}), add_call(state, call)}
      end)
    end

    def wrap(config, principal, data_key, artifact) do
      Agent.get_and_update(config.agent, fn state ->
        call = {:wrap, principal, data_key, artifact, config.key_ref}
        {reply(state, {:ok, state.wrapped_key}), add_call(state, call)}
      end)
    end

    def unwrap(config, principal, key_ref, wrapped_key) do
      Agent.get_and_update(config.agent, fn state ->
        call = {:unwrap, principal, key_ref, wrapped_key}
        {reply(state, {:ok, state.data_key}), add_call(state, call)}
      end)
    end

    defp reply(%{reply: reply}, _default) when not is_nil(reply), do: reply
    defp reply(_state, default), do: default
    defp add_call(state, call), do: Map.update(state, :calls, [call], &[call | &1])
  end

  defp start_service(opts \\ []) do
    root = Keyword.get(opts, :root, @root)
    root_generation = Keyword.get(opts, :root_generation, 1)
    {:ok, op_log} = RecordingOpLog.start(Keyword.get(opts, :mode, :ok))

    {:ok, service} =
      KeyService.start_link(
        name: nil,
        root: root,
        roots: Keyword.get(opts, :roots, if(root, do: %{root_generation => root}, else: %{})),
        root_generation: root_generation,
        op_log: op_log,
        op_log_mod: RecordingOpLog,
        customer_kms: Keyword.get(opts, :customer_kms, %{}),
        tenant: "test",
        clock: fn -> 1_000 end
      )

    {service, op_log}
  end

  test "derivation is deterministic and separated by principal and epoch" do
    {service, _op_log} = start_service()

    assert {:ok, key} = KeyService.derive_kek(service, @principal, 1)
    assert {:ok, ^key} = KeyService.derive_kek(service, @principal, 1)
    assert {:ok, other_principal_key} = KeyService.derive_kek(service, @principal <> "-other", 1)
    assert {:ok, other_epoch_key} = KeyService.derive_kek(service, @principal, 2)

    refute key == other_principal_key
    refute key == other_epoch_key
    refute other_principal_key == other_epoch_key
  end

  test "HKDF parameters match an independent RFC 5869 extract and expand" do
    {service, _op_log} = start_service()
    assert {:ok, derived} = KeyService.derive_kek(service, @principal, 1)

    salt = "embervm-kek-v1"
    info = <<byte_size(@principal)::unsigned-32, @principal::binary, 1::unsigned-64>>
    pseudorandom_key = :crypto.mac(:hmac, :sha256, salt, @root)
    expected = :crypto.mac(:hmac, :sha256, pseudorandom_key, info <> <<1>>)

    assert Base.encode16(derived, case: :lower) == Base.encode16(expected, case: :lower)
  end

  test "invalid principals are refused" do
    {service, _op_log} = start_service()

    assert {:error, :no_principal} = KeyService.derive_kek(service, nil, 0)
    assert {:error, :no_principal} = KeyService.derive_kek(service, "", 0)
  end

  test "a server without a root starts and refuses every cryptographic operation" do
    {service, _op_log} = start_service(root: nil)

    envelope = %Envelope{
      principal: @principal,
      epoch: 0,
      nonce: :binary.copy(<<0>>, 12),
      tag: :binary.copy(<<0>>, 16),
      wrapped_key: :binary.copy(<<0>>, 32)
    }

    assert {:error, :no_root} = KeyService.derive_kek(service, @principal, 0)
    assert {:error, :no_root} = KeyService.wrap(service, @principal, @root)
    assert {:error, :no_root} = KeyService.unwrap(service, envelope)
  end

  test "the root is redacted from state inspection" do
    root = :binary.copy("secret", 6)
    {service, _op_log} = start_service(root: root)

    inspected = service |> :sys.get_state() |> inspect()
    refute inspected =~ root
    assert inspected =~ "redacted"
  end

  test "epoch zero is the valid default and set_epoch requires a strict increase" do
    {service, _op_log} = start_service()

    assert {:ok, 0} = KeyService.current_epoch(service, @principal)
    assert {:error, :epoch_not_increased} = KeyService.set_epoch(service, @principal, 0, "noop")
    assert {:ok, 1} = KeyService.set_epoch(service, @principal, 1, "rotate")
    assert {:ok, 1} = KeyService.current_epoch(service, @principal)
  end

  test "a failed append leaves the current epoch unchanged" do
    {service, _op_log} = start_service(mode: :fail)

    assert {:error, :disk_full} = KeyService.set_epoch(service, @principal, 1, "rotate")
    assert {:ok, 0} = KeyService.current_epoch(service, @principal)
  end

  test "the ETS epoch changes only after a blocked append returns" do
    {service, _op_log} = start_service(mode: {:block, self()})
    state = :sys.get_state(service)
    task = Task.async(fn -> KeyService.set_epoch(service, @principal, 1, "rotate") end)

    assert_receive {:append_blocked, appender, %{kind: :key_epoch_set}}
    assert :ets.lookup(state.epochs, @principal) == []

    send(appender, :continue)
    assert {:ok, 1} = Task.await(task)
    assert :ets.lookup(state.epochs, @principal) == [{@principal, 1, 0}]
  end

  test "the ETS floor changes only after a blocked append returns" do
    {service, op_log} = start_service()
    assert {:ok, 1} = KeyService.set_epoch(service, @principal, 1, "rotate")

    :ok = RecordingOpLog.set_mode(op_log, {:block, self()})
    state = :sys.get_state(service)
    task = Task.async(fn -> KeyService.raise_min_epoch(service, @principal, 1, "revoke") end)

    assert_receive {:append_blocked, appender, %{kind: :key_min_epoch_raised}}
    assert :ets.lookup(state.epochs, @principal) == [{@principal, 1, 0}]

    send(appender, :continue)
    assert {:ok, 1} = Task.await(task)
    assert :ets.lookup(state.epochs, @principal) == [{@principal, 1, 1}]
  end

  test "raising the floor refuses derivation below it" do
    {service, _op_log} = start_service()

    assert {:ok, 2} = KeyService.set_epoch(service, @principal, 2, "rotate")
    assert {:ok, 1} = KeyService.raise_min_epoch(service, @principal, 1, "revoke")
    assert {:error, :below_floor} = KeyService.derive_kek(service, @principal, 0)
    assert {:ok, _key} = KeyService.derive_kek(service, @principal, 1)
  end

  test "envelopes round trip and authenticate principal, epoch, and tag" do
    {service, _op_log} = start_service()
    data_key = :crypto.strong_rand_bytes(32)

    assert {:ok, envelope} = KeyService.wrap(service, @principal, data_key)
    assert {:ok, ^data_key} = KeyService.unwrap(service, envelope)

    <<first, rest::binary>> = envelope.tag

    assert {:error, :auth_failed} =
             KeyService.unwrap(service, %{
               envelope
               | tag: <<Bitwise.bxor(first, 1), rest::binary>>
             })

    assert {:error, :auth_failed} =
             KeyService.unwrap(service, %{envelope | principal: @principal <> "-other"})

    assert {:error, :auth_failed} =
             KeyService.unwrap(service, %{envelope | epoch: envelope.epoch + 1})

    encoded = Envelope.encode(envelope)
    assert {:ok, ^envelope} = Envelope.decode(encoded)
    assert {:error, :bad_envelope} = Envelope.decode("junk")
  end

  test "root rotation emits version 3 and retains generation 1 only while configured" do
    old_root = :binary.copy(<<1>>, 32)
    new_root = :binary.copy(<<2>>, 32)
    data_key = :binary.copy(<<3>>, 32)

    {old_service, _op_log} = start_service(root: old_root, roots: %{1 => old_root})
    assert {:ok, old_envelope} = KeyService.wrap(old_service, @principal, data_key)
    assert old_envelope.version == 1

    {rotating_service, _op_log} =
      start_service(
        root: new_root,
        roots: %{1 => old_root, 2 => new_root},
        root_generation: 2
      )

    assert {:ok, ^data_key} = KeyService.unwrap(rotating_service, old_envelope)
    assert {:ok, new_envelope} = KeyService.wrap(rotating_service, @principal, data_key)

    assert %Envelope{version: 3, root_generation: 2} = new_envelope
    assert {:ok, ^data_key} = KeyService.unwrap(rotating_service, new_envelope)
    assert {:ok, ^new_envelope} = new_envelope |> Envelope.encode() |> Envelope.decode()

    assert {:error, :auth_failed} =
             KeyService.unwrap(rotating_service, %{new_envelope | root_generation: 1})

    {retired_service, _op_log} =
      start_service(root: new_root, roots: %{2 => new_root}, root_generation: 2)

    assert {:error, :root_generation_unavailable} =
             KeyService.unwrap(retired_service, old_envelope)

    inspected = rotating_service |> :sys.get_state() |> inspect()
    refute inspected =~ inspect(old_root)
    refute inspected =~ inspect(new_root)
    assert inspected =~ "roots: :redacted"
    assert inspected =~ "root_generation: 2"
  end

  test "customer KMS issues and unwraps a version 2 envelope without a platform root" do
    data_key = :binary.copy(<<7>>, 32)
    wrapped_key = "opaque-customer-ciphertext"

    {:ok, kms} =
      Agent.start_link(fn ->
        %{data_key: data_key, wrapped_key: wrapped_key, reply: nil, calls: []}
      end)

    config = %{
      adapter: FakeCustomerKMS,
      endpoint: "https://kms.example",
      key_ref: "alice-key",
      bearer_token: "secret-grant",
      agent: kms
    }

    {service, _op_log} = start_service(root: nil, customer_kms: %{@principal => config})
    artifact = %{"kind" => "volume", "workload" => "pg", "ref" => "pg"}

    assert {:ok, ^data_key, envelope} =
             KeyService.issue_data_key(service, @principal, artifact)

    assert %Envelope{
             version: 2,
             principal: @principal,
             key_ref: "alice-key",
             wrapped_key: ^wrapped_key
           } = envelope

    encoded = Envelope.encode(envelope)
    assert {:ok, ^envelope} = Envelope.decode(encoded)
    assert {:ok, ^data_key} = KeyService.unwrap(service, envelope)
    assert {:error, :customer_managed} = KeyService.derive_kek(service, @principal, 1)

    calls = Agent.get(kms, & &1.calls)
    assert Enum.any?(calls, &match?({:issue, @principal, ^artifact, "alice-key"}, &1))
    assert Enum.any?(calls, &match?({:unwrap, @principal, "alice-key", ^wrapped_key}, &1))

    inspected = service |> :sys.get_state() |> inspect()
    refute inspected =~ "secret-grant"
    assert inspected =~ "customer_kms: :redacted"
  end

  test "customer revocation refuses unwrap and custody modes never fall through" do
    {platform_service, _op_log} = start_service()
    assert {:ok, platform_envelope} = KeyService.wrap(platform_service, @principal, @root)

    {:ok, kms} =
      Agent.start_link(fn ->
        %{
          data_key: @root,
          wrapped_key: "revoked-ciphertext",
          reply: {:error, :kms_refused},
          calls: []
        }
      end)

    config = %{
      adapter: FakeCustomerKMS,
      endpoint: "https://kms.example",
      key_ref: "revoked-key",
      bearer_token: "revoked-grant",
      agent: kms
    }

    {customer_service, _op_log} =
      start_service(customer_kms: %{@principal => config})

    customer_envelope = %Envelope{
      version: 2,
      principal: @principal,
      key_ref: "revoked-key",
      wrapped_key: "revoked-ciphertext"
    }

    assert {:error, :kms_refused} = KeyService.unwrap(customer_service, customer_envelope)
    assert {:error, :custody_mismatch} =
             KeyService.unwrap(customer_service, platform_envelope)

    assert {:error, :customer_kms_not_configured} =
             KeyService.unwrap(platform_service, customer_envelope)
  end

  test "an envelope below a newly raised floor is revoked" do
    {service, _op_log} = start_service()
    assert {:ok, envelope} = KeyService.wrap(service, @principal, @root)
    assert {:ok, 1} = KeyService.set_epoch(service, @principal, 1, "rotate")
    assert {:ok, 1} = KeyService.raise_min_epoch(service, @principal, 1, "revoke")

    assert {:error, :below_floor} = KeyService.unwrap(service, envelope)
  end

  test "telemetry counts derivations, floor raises, and below-floor refusals" do
    handler = "key-service-test-#{System.unique_integer([:positive])}"
    test_pid = self()

    events =
      for event <- [:derivation, :floor_raise, :refused_below_floor] do
        [:embervm, :key_service, event]
      end

    :ok =
      :telemetry.attach_many(
        handler,
        events,
        fn event, measurements, metadata, _config ->
          send(test_pid, {:telemetry, event, measurements, metadata})
        end,
        nil
      )

    on_exit(fn -> :telemetry.detach(handler) end)
    {service, _op_log} = start_service()

    assert {:ok, _key} = KeyService.derive_kek(service, @principal, 0)
    assert {:ok, 1} = KeyService.set_epoch(service, @principal, 1, "rotate")
    assert {:ok, 1} = KeyService.raise_min_epoch(service, @principal, 1, "revoke")
    assert {:error, :below_floor} = KeyService.derive_kek(service, @principal, 0)

    assert_receive {:telemetry, [:embervm, :key_service, :derivation], %{count: 1}, %{epoch: 0}}

    assert_receive {:telemetry, [:embervm, :key_service, :floor_raise], %{count: 1},
                    %{min_epoch: 1}}

    assert_receive {:telemetry, [:embervm, :key_service, :refused_below_floor], %{count: 1},
                    %{epoch: 0, min_epoch: 1}}
  end

  test "a fresh service recovers epochs and floors from SQLite" do
    path =
      Path.join(System.tmp_dir!(), "embervm_key_service_#{System.unique_integer([:positive])}.db")

    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)

    {:ok, service} =
      KeyService.start_link(
        name: nil,
        root: @root,
        op_log: op_log,
        op_log_mod: SQLite,
        tenant: "test"
      )

    assert {:ok, 3} = KeyService.set_epoch(service, @principal, 3, "rotate")
    assert {:ok, 2} = KeyService.raise_min_epoch(service, @principal, 2, "revoke")
    :ok = GenServer.stop(service)

    {:ok, recovered} =
      KeyService.start_link(
        name: nil,
        root: @root,
        op_log: op_log,
        op_log_mod: SQLite,
        tenant: "test"
      )

    assert {:ok, 3} = KeyService.current_epoch(recovered, @principal)
    assert {:error, :below_floor} = KeyService.derive_kek(recovered, @principal, 1)
    assert {:ok, _key} = KeyService.derive_kek(recovered, @principal, 2)
  end
end
