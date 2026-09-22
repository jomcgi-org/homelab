defmodule Embervm.RootfsRemoteRetentionTest do
  use ExUnit.Case, async: true

  alias Embervm.RootfsRemoteRetention

  @wall 1_750_000_000_000
  @day 86_400_000
  @digest String.duplicate("a", 64)

  defp new_s3(objects) do
    {:ok, agent} = Agent.start_link(fn -> %{objects: objects, deleted: [], puts: []} end)

    s3 = %{
      list: fn prefix ->
        entries =
          Agent.get(agent, & &1.objects)
          |> Enum.filter(fn {key, _value} -> String.starts_with?(key, prefix) end)
          |> Enum.map(fn {key, {modified, body}} ->
            %{key: key, size: byte_size(body), last_modified_ms: modified}
          end)

        {:ok, entries}
      end,
      get: fn key ->
        case Agent.get(agent, &Map.get(&1.objects, key)) do
          nil -> {:error, :not_found}
          {_modified, body} -> {:ok, body}
        end
      end,
      delete: fn key ->
        Agent.update(agent, fn state ->
          %{state | objects: Map.delete(state.objects, key), deleted: state.deleted ++ [key]}
        end)

        :ok
      end,
      put: fn key, body ->
        Agent.update(agent, fn state -> %{state | puts: state.puts ++ [{key, body}]} end)
        :ok
      end
    }

    {agent, s3}
  end

  defp rootfs_objects(image_ref, age_days, format \\ "b2") do
    prefix = "rootfs/#{@digest}/size-4G/format-#{format}"
    payload = prefix <> "/" <> String.duplicate("b", 64) <> ".ext4"
    modified = @wall - age_days * @day
    uploaded_at = modified |> DateTime.from_unix!(:millisecond) |> DateTime.to_iso8601()

    marker =
      :json.encode(%{
        "payloadKey" => payload,
        "sha256" => String.duplicate("b", 64),
        "imageRef" => image_ref,
        "imageDigest" => @digest,
        "rootfsSize" => "4G",
        "bakeFormat" => format,
        "uploadedAt" => uploaded_at
      })
      |> IO.iodata_to_binary()

    {
      prefix,
      %{
        payload => {modified, "payload"},
        (prefix <> "/rootfs.ext4.sha256") => {modified, marker}
      }
    }
  end

  defp start_retention(s3, opts \\ []) do
    defaults = [
      name: nil,
      s3: s3,
      age_days: 30,
      current_image_refs: ["repo/current@sha256:index"],
      wall_clock: fn -> @wall end,
      sweep_interval_ms: 0
    ]

    {:ok, pid} = RootfsRemoteRetention.start_link(Keyword.merge(defaults, opts))
    pid
  end

  test "dry run protects current and young refs and plans only old non-current prefixes" do
    {current_prefix, current} = rootfs_objects("repo/current@sha256:index", 90, "b2")
    {young_prefix, young} = rootfs_objects("repo/old@sha256:index", 29, "b3")
    {old_prefix, old} = rootfs_objects("repo/old@sha256:index", 31, "b4")
    legacy = %{"rootfs/#{@digest}/legacy.ext4" => {@wall - 365 * @day, "legacy"}}
    {agent, s3} = new_s3(Map.merge(Map.merge(Map.merge(current, young), old), legacy))
    pid = start_retention(s3)

    assert {:ok, %{plan: [^old_prefix], deleted: []}} = RootfsRemoteRetention.sweep_now(pid)
    assert Agent.get(agent, & &1.deleted) == []

    [{manifest_key, body}] = Agent.get(agent, & &1.puts)
    assert String.starts_with?(manifest_key, "gc-manifests/rootfs-")
    manifest = :json.decode(body)
    assert manifest["mode"] == "dry_run"

    assert Enum.any?(
             manifest["held"],
             &(&1["reason"] == "current_image_ref" and &1["prefix"] == current_prefix)
           )

    assert Enum.any?(
             manifest["held"],
             &(&1["reason"] == "younger_than_age_horizon" and &1["prefix"] == young_prefix)
           )

    assert manifest["held_legacy_or_malformed_keys"] == ["rootfs/#{@digest}/legacy.ext4"]
  end

  test "armed sweep deletes marker first and only listed keys in an old prefix" do
    {prefix, objects} = rootfs_objects("repo/old@sha256:index", 31)
    {agent, s3} = new_s3(objects)
    pid = start_retention(s3, enabled: true)

    assert {:ok, %{plan: [^prefix], deleted: [^prefix]}} = RootfsRemoteRetention.sweep_now(pid)
    [first | rest] = Agent.get(agent, & &1.deleted)
    assert first == prefix <> "/rootfs.ext4.sha256"
    assert rest == [prefix <> "/" <> String.duplicate("b", 64) <> ".ext4"]
  end

  test "armed sweep refuses an empty current-image inventory" do
    {_prefix, objects} = rootfs_objects("repo/old@sha256:index", 31)
    {agent, s3} = new_s3(objects)
    pid = start_retention(s3, enabled: true, current_image_refs: [])

    assert {:error, :no_current_image_refs} = RootfsRemoteRetention.sweep_now(pid)
    assert Agent.get(agent, & &1.deleted) == []
  end

  test "armed sweep holds an old payload with no completion marker" do
    prefix = "rootfs/#{@digest}/size-4G/format-b2"
    payload = prefix <> "/" <> String.duplicate("b", 64) <> ".ext4"
    objects = %{payload => {@wall - 365 * @day, "payload"}}
    {agent, s3} = new_s3(objects)
    pid = start_retention(s3, enabled: true)

    assert {:ok, %{plan: [], deleted: []}} = RootfsRemoteRetention.sweep_now(pid)
    assert Agent.get(agent, & &1.deleted) == []

    [{_manifest_key, body}] = Agent.get(agent, & &1.puts)
    manifest = :json.decode(body)

    assert manifest["held"] == [
             %{
               "prefix" => prefix,
               "bytes" => byte_size("payload"),
               "reason" => "incomplete_missing_marker"
             }
           ]
  end

  test "invalid age horizon refuses to start" do
    assert {:error, {:invalid_age_days, 0}} =
             RootfsRemoteRetention.start_link(name: nil, age_days: 0)
  end
end
