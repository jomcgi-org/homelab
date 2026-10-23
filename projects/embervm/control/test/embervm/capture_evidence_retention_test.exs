defmodule Embervm.CaptureEvidenceRetentionTest do
  use ExUnit.Case, async: true
  alias Embervm.CaptureEvidenceRetention, as: Retention
  @now 1_750_000_000_000
  @identity %{
    session_id: "s-capture",
    lineage_id: "lineage",
    workload: "claude-runtime",
    principal: "factory"
  }

  test "new captures get a three-day default with separate twelve-hour lease ceiling" do
    {:ok, policy} = Retention.new(@identity, @now)
    assert policy["expires_at"] == @now + 3 * 86_400_000
    assert policy["hard_expires_at"] == policy["expires_at"] + 43_200_000
    assert :ok == Retention.validate(policy)
    assert {:error, _} = Retention.eligible?(policy, @identity, @now, policy["expires_at"] - 1)
    assert :ok == Retention.eligible?(policy, @identity, @now, policy["expires_at"])
  end

  test "review lease is bounded and cannot revive expired evidence" do
    {:ok, policy} = Retention.new(@identity, @now)
    near_expiry = policy["expires_at"] - 1

    {:ok, leased} =
      Retention.renew(policy, %{@identity | session_id: "s-review"}, near_expiry, 43_200_000)

    assert leased["lease_until"] <= policy["hard_expires_at"]
    assert {:error, _} = Retention.renew(leased, @identity, policy["expires_at"] + 1, 43_200_000)
    assert {:error, _} = Retention.eligible?(leased, @identity, @now, policy["expires_at"])
    assert {:error, _} = Retention.renew(leased, @identity, policy["hard_expires_at"], 1)
    assert {:error, _} = Retention.renew(policy, @identity, policy["expires_at"], 1)
  end

  test "archive predating opt-in and wrong lineage are never eligible" do
    {:ok, policy} = Retention.new(@identity, @now)

    assert {:error, _} =
             Retention.eligible?(policy, @identity, @now - 1, policy["hard_expires_at"])

    assert {:error, _} =
             Retention.eligible?(
               policy,
               %{@identity | lineage_id: "other"},
               @now,
               policy["hard_expires_at"]
             )
  end

  test "configuration bounds and malformed policy fail closed" do
    for ttl <- [0, 3_599_999, 7 * 86_400_000 + 1, "3days"] do
      assert {:error, _} = Retention.new(@identity, @now, ttl)
    end

    {:ok, policy} = Retention.new(@identity, @now, 3_600_000)
    assert :ok == Retention.validate(policy)

    assert {:error, _} =
             Retention.validate(Map.put(policy, "hard_expires_at", @now + 100 * 86_400_000))

    assert {:error, _} = Retention.new(%{@identity | lineage_id: "../bad"}, @now)
  end
end
