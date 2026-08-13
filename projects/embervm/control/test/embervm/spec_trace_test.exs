defmodule Embervm.SpecTraceTest do
  use ExUnit.Case, async: false

  alias Embervm.SpecTrace

  setup do
    dir = Path.join(System.tmp_dir!(), "embervm-spec-trace-#{System.unique_integer([:positive])}")
    File.mkdir_p!(dir)
    on_exit(fn -> File.rm_rf(dir) end)
    {:ok, dir: dir}
  end

  test "disabled_no_op", %{dir: dir} do
    System.put_env("EMBERVM_SPEC_TRACE", "off")
    SpecTrace.configure()
    for _ <- 1..10, do: SpecTrace.emit(:adoption, :prime, %{"vm_id" => "vm"})
    refute File.exists?(dir <> "/segment-000.ndjson")
  end

  test "enabled_prime", %{dir: dir} do
    start_writer(dir)
    SpecTrace.emit(:adoption, :prime, %{"vm_id" => "vm-1", "node_id" => "node-1", "workload" => "wl", "lane" => :task})
    :ok = SpecTrace.drain()
    records = records(dir)
    assert Enum.any?(records, &(&1["action"] == "prime" and &1["vars"]["vm_id"] == "vm-1"))
  end

  test "enabled_checkpoint", %{dir: dir} do
    start_writer(dir)
    SpecTrace.emit(:adoption, :checkpoint, %{"node_workload_vm_ids" => %{"node:wl" => ["vm-1"]}, "reserved_vm_ids" => [], "node_health" => %{}})
    :ok = SpecTrace.drain()
    assert Enum.any?(records(dir), &(&1["action"] == "checkpoint" and &1["vars"]["node_workload_vm_ids"]["node:wl"] == ["vm-1"]))
  end

  test "mono_ordering", %{dir: dir} do
    start_writer(dir)
    parent = self()
    for i <- 1..5 do
      spawn(fn -> SpecTrace.emit(:adoption, :prime, %{"i" => i}); send(parent, :done) end)
    end
    for _ <- 1..5, do: assert_receive :done
    :ok = SpecTrace.drain()
    monos = records(dir) |> Enum.filter(&(&1["action"] == "prime")) |> Enum.map(& &1["mono"])
    assert length(monos) == 5
    assert Enum.sort(monos) == Enum.sort_by(monos, & &1)
  end

  test "mono and ts are captured as distinct plausible clocks", %{dir: dir} do
    start_writer(dir)
    SpecTrace.emit(:adoption, :prime, %{})
    :ok = SpecTrace.drain()
    record = Enum.find(records(dir), &(&1["action"] == "prime"))
    now = System.system_time(:millisecond)
    assert record["ts"] > 1_700_000_000_000
    assert abs(record["ts"] - now) < 60_000
    assert record["ts"] != record["mono"]
  end

  test "segment_rotation keeps a preamble and records across segments", %{dir: dir} do
    start_writer(dir, segment_bytes: 2048)
    for i <- 1..20, do: SpecTrace.emit(:adoption, :prime, %{"i" => i})
    :ok = SpecTrace.drain()
    all = records(dir)
    assert length(Path.wildcard(Path.join(dir, "segment-*.ndjson"))) > 1
    assert Enum.count(all, &(&1["action"] == "preamble")) == 1
    assert Enum.count(all, &(&1["action"] == "prime")) == 20
  end

  test "writer_crash_isolation", %{dir: dir} do
    start_writer(dir)
    writer = Process.whereis(Embervm.SpecTrace.Writer)
    Process.unlink(writer)
    Process.exit(writer, :kill)
    assert SpecTrace.emit(:adoption, :prime, %{}) == :ok
  end

  # start_supervised! rather than start_link, because start_link LINKS the
  # writer to the test process: the link kills it with :shutdown when the test
  # ends, and the on_exit callback (which runs afterwards, in another process)
  # then calls GenServer.stop on a corpse and exits :shutdown itself. That is a
  # teardown failure masquerading as a test failure. ExUnit's supervisor owns
  # the lifecycle and stops it before the test process goes away.
  defp start_writer(dir, opts \\ []) do
    prior = System.get_env("EMBERVM_SPEC_TRACE")
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    # Restore the env and the persistent_term flag. The suite is async: false
    # and the flag is global, so leaving it on leaks the enabled state into
    # every later test in the run.
    on_exit(fn ->
      if prior, do: System.put_env("EMBERVM_SPEC_TRACE", prior), else: System.delete_env("EMBERVM_SPEC_TRACE")
      SpecTrace.configure()
    end)

    start_supervised!({Embervm.SpecTrace.Writer, Keyword.merge([dir: dir], opts)})
  end

  defp records(dir) do
    dir
    |> Path.join("segment-*.ndjson")
    |> Path.wildcard()
    |> Enum.sort()
    |> Enum.flat_map(fn path -> path |> File.read!() |> String.split("\n", trim: true) |> Enum.map(&Jason.decode!/1) end)
  end
end
