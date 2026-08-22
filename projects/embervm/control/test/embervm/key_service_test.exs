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

  defp start_service(opts \\ []) do
    root = Keyword.get(opts, :root, @root)
    {:ok, op_log} = RecordingOpLog.start(Keyword.get(opts, :mode, :ok))

    {:ok, service} =
      KeyService.start_link(
        name: nil,
        root: root,
        op_log: op_log,
        op_log_mod: RecordingOpLog,
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
