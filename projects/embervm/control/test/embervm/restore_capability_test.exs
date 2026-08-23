defmodule Embervm.RestoreCapabilityTest do
  use ExUnit.Case, async: true

  alias Embervm.KeyService.Envelope
  alias Embervm.RestoreCapability

  @golden "01000001b8dac5b47b0020000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f00a07b227072696e636970616c223a22616363743a616c696365222c226c696e65616765223a226c696e656167652d3432222c226e6f6465223a226e6f64652d61222c22706f645f756964223a227569642d61222c22776f726b6c6f6164223a2273616e64626f782d73657373696f6e222c22726566223a22736573732d3432222c226b696e64223a2273657373696f6e222c2267656e65726174696f6e223a377d856a4e670db0f0fdd70b88aafbe5ab9b8411b21cf0c19cc82c8ffe3ffaf9adc8"

  defmodule FakeS3 do
    def get(agent, key) do
      Agent.get_and_update(agent, fn state ->
        reply = Map.get(state, :reply, {:error, :not_found})
        {reply, Map.update(state, :calls, [key], &[key | &1])}
      end)
    end
  end

  defmodule FakeKeyService do
    def current_epoch(agent, _principal), do: Agent.get(agent, &{:ok, Map.get(&1, :epoch, 0)})

    def set_epoch(agent, _principal, epoch, _reason) do
      Agent.update(agent, &Map.put(&1, :epoch, epoch))
      {:ok, epoch}
    end

    def unwrap(agent, _envelope),
      do: Agent.get(agent, &Map.get(&1, :unwrap, {:error, :auth_failed}))
  end

  setup do
    {:ok, s3} = Agent.start_link(fn -> %{calls: [], reply: {:error, :not_found}} end)
    {:ok, keys} = Agent.start_link(fn -> %{epoch: 0, unwrap: {:ok, :binary.copy(<<7>>, 32)}} end)

    envelope =
      Envelope.encode(%Envelope{
        principal: "acct:alice",
        epoch: 0,
        nonce: :binary.copy(<<1>>, 12),
        tag: :binary.copy(<<2>>, 16),
        wrapped_key: :binary.copy(<<3>>, 32)
      })

    req = %{
      artifact: %{
        kind: :ARTIFACT_KIND_SESSION,
        workload: "sandbox-session",
        ref: "sess-42"
      },
      vendor: "intel",
      capability: ""
    }

    opts = [
      enabled?: true,
      mac_key: "shared-bearer",
      s3_client: {FakeS3, s3},
      key_service: {FakeKeyService, keys},
      clock: fn -> 1_000 end
    ]

    %{s3: s3, keys: keys, envelope: envelope, req: req, opts: opts}
  end

  test "mint reproduces noded's golden vector byte for byte" do
    data_key = 0..31 |> Enum.to_list() |> :binary.list_to_bin()

    scope = %{
      principal: "acct:alice",
      lineage: "lineage-42",
      node: "node-a",
      pod_uid: "uid-a",
      workload: "sandbox-session",
      ref: "sess-42",
      kind: "session",
      generation: 7
    }

    capability =
      RestoreCapability.mint("golden-shared-bearer", data_key, 1_893_456_000_123, scope)

    assert Base.encode16(capability, case: :lower) == @golden
  end

  test "flag off is a no-op without an S3 call", %{s3: s3, req: req, opts: opts} do
    assert {:ok, ^req} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{},
               Keyword.put(opts, :enabled?, false)
             )

    assert Agent.get(s3, & &1.calls) == []
  end

  test "plaintext meta passes through unchanged", %{s3: s3, req: req, opts: opts} do
    Agent.update(s3, &Map.put(&1, :reply, {:ok, ~s({"generation":7})}))

    assert {:ok, ^req} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{principal: "acct:alice", lineage: "lineage-42", generation: 7},
               opts
             )
  end

  test "enveloped meta mints a brick-scoped five-minute capability", %{
    s3: s3,
    envelope: envelope,
    req: req,
    opts: opts
  } do
    meta = :json.encode(%{envelope: Base.encode64(envelope)}) |> IO.iodata_to_binary()
    Agent.update(s3, &Map.put(&1, :reply, {:ok, meta}))

    assert {:ok, %{capability: capability}} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{principal: "acct:alice", lineage: "lineage-42", generation: 7},
               opts
             )

    <<1, expiry::unsigned-64, 32::unsigned-16, _key::binary-32, tuple_len::unsigned-16,
      tuple::binary-size(tuple_len), _mac::binary-32>> = capability

    assert expiry == 301_000

    assert :json.decode(tuple) == %{
             "principal" => "acct:alice",
             "lineage" => "lineage-42",
             "node" => "node-a",
             "pod_uid" => "uid-a",
             "workload" => "sandbox-session",
             "ref" => "sess-42",
             "kind" => "session",
             "generation" => 7
           }
  end

  test "below-floor envelopes are refused", %{
    s3: s3,
    keys: keys,
    envelope: envelope,
    req: req,
    opts: opts
  } do
    meta = :json.encode(%{envelope: Base.encode64(envelope)}) |> IO.iodata_to_binary()
    Agent.update(s3, &Map.put(&1, :reply, {:ok, meta}))
    Agent.update(keys, &Map.put(&1, :unwrap, {:error, :below_floor}))

    assert {:error, :capability_refused} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{principal: "acct:alice", lineage: "lineage-42", generation: 7},
               opts
             )
  end

  test "customer KMS revocation refuses the capability and skips platform epochs", %{
    s3: s3,
    keys: keys,
    req: req,
    opts: opts
  } do
    envelope =
      Envelope.encode(%Envelope{
        version: 2,
        principal: "acct:alice",
        key_ref: "customer-key",
        wrapped_key: "opaque-ciphertext"
      })

    meta = :json.encode(%{envelope: Base.encode64(envelope)}) |> IO.iodata_to_binary()
    Agent.update(s3, &Map.put(&1, :reply, {:ok, meta}))
    Agent.update(keys, &Map.put(&1, :unwrap, {:error, :kms_refused}))

    assert {:error, :capability_refused} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{principal: "acct:alice", lineage: "lineage-42", generation: 7},
               opts
             )

    assert Agent.get(keys, & &1.epoch) == 0
  end

  test "an empty bearer refuses capability minting", %{req: req, opts: opts} do
    assert {:error, :no_capability_key} =
             RestoreCapability.stamp(
               req,
               %{node_id: "node-a", pod_uid: "uid-a"},
               %{principal: "acct:alice", lineage: "lineage-42", generation: 7},
               Keyword.put(opts, :mac_key, "")
             )
  end
end
