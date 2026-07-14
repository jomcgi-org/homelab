defmodule Embervm.UsageTest do
  @moduledoc "The metering formula (vCPU-seconds, GB-seconds) from raw stats."
  use ExUnit.Case, async: true

  alias Embervm.Usage

  test "vcpu_seconds is cpu_ms / 1000" do
    assert Usage.vcpu_seconds(0) == 0.0
    assert Usage.vcpu_seconds(1000) == 1.0
    assert Usage.vcpu_seconds(2500) == 2.5
  end

  test "gb_seconds is (peak_rss_mib / 1024) * (wall_ms / 1000)" do
    # 1024 MiB for 1s = 1 GB-second.
    assert Usage.gb_seconds(1024, 1000) == 1.0
    # 512 MiB for 2s = 0.5 * 2 = 1 GB-second.
    assert Usage.gb_seconds(512, 2000) == 1.0
    assert Usage.gb_seconds(0, 5000) == 0.0
  end

  test "billed derives both quantities from scripted stats" do
    stats = %{cpu_ms: 2000, peak_rss_mib: 1024, wall_ms: 4000}
    assert %{vcpu_seconds: 2.0, gb_seconds: 4.0} = Usage.billed(stats)
  end

  test "from_proto normalizes a UsageStats-shaped map, defaulting missing fields to 0" do
    assert Usage.from_proto(%{cpu_ms: 5, peak_rss_mib: 6, wall_ms: 7}) ==
             %{cpu_ms: 5, peak_rss_mib: 6, wall_ms: 7}

    # A struct with nil/absent fields (proto default): coerced to 0.
    assert Usage.from_proto(%{cpu_ms: nil, peak_rss_mib: 6, wall_ms: nil}) ==
             %{cpu_ms: 0, peak_rss_mib: 6, wall_ms: 0}
  end

  test "from_proto(nil) is nil (no usage reported charges nothing)" do
    assert Usage.from_proto(nil) == nil
  end

  test "all_zero? flags a daemon that never populated UsageStats" do
    assert Usage.all_zero?(%{cpu_ms: 0, peak_rss_mib: 0, wall_ms: 0})
    refute Usage.all_zero?(%{cpu_ms: 1, peak_rss_mib: 0, wall_ms: 0})
  end
end
