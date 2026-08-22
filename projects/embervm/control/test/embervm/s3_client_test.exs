defmodule Embervm.S3ClientTest do
  use ExUnit.Case, async: false

  alias Embervm.S3Client
  import ExUnit.CaptureLog

  defmodule RecordingPlug do
    @behaviour Plug

    @impl true
    def init(test_pid), do: test_pid

    @impl true
    def call(conn, test_pid) do
      send(test_pid, {:s3_request, conn.method, conn.request_path, conn.query_string, Plug.Conn.get_req_header(conn, "authorization")})

      body =
        if conn.method == "GET" and conn.request_path == "/embervm/" do
          "<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
        else
          "ok"
        end

      Plug.Conn.send_resp(conn, 200, body)
    end
  end

  @port 18_092
  @auth_shape ~r/^AWS4-HMAC-SHA256 Credential=embervm\/\d{8}\/us-east-1\/s3\/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$/

  setup do
    start_supervised!({Bandit, plug: {RecordingPlug, self()}, scheme: :http, port: @port})
    :ok
  end

  test "GET, PUT, DELETE, and ListObjectsV2 are signed when credentials are set" do
    client =
      S3Client.new("http://127.0.0.1:#{@port}", "embervm",
        access_key_id: "embervm",
        secret_access_key: "secret"
      )

    assert {:ok, "ok"} = S3Client.get(client, "base/amd/demo/meta.json")
    assert :ok = S3Client.put(client, "manifests/latest.json", "{}")
    assert :ok = S3Client.delete(client, "stateful/amd/old/meta.json")
    assert {:ok, []} = S3Client.list_all(client, "base/amd/")

    for method <- ["GET", "PUT", "DELETE", "GET"] do
      assert_receive {:s3_request, ^method, _path, _query, [authorization]}
      assert authorization =~ @auth_shape
    end
  end

  test "anonymous client sends no Authorization header" do
    client = S3Client.new("http://127.0.0.1:#{@port}", "embervm")
    assert {:ok, "ok"} = S3Client.get(client, "base/amd/demo/meta.json")
    assert_receive {:s3_request, "GET", _path, _query, []}
  end

  test "an incomplete credential pair logs an error and falls back to anonymous" do
    log =
      capture_log(fn ->
        gc =
          start_supervised!(
            {Embervm.S3WarmthGc,
             [
               name: nil,
               endpoint: "http://127.0.0.1:#{@port}",
               bucket: "embervm",
               access_key_id: "embervm",
               secret_access_key: "",
               sweep_interval_ms: 60_000
             ]}
          )

        state = :sys.get_state(gc)
        assert {:ok, "ok"} = state.s3.get.("base/amd/demo/meta.json")
        assert_receive {:s3_request, "GET", _path, _query, []}
      end)

    assert log =~ "both access key ID and secret access key are required"
  end
end
