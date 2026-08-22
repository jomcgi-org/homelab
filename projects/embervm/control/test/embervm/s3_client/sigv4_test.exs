defmodule Embervm.S3Client.SigV4Test do
  use ExUnit.Case, async: true

  alias Embervm.S3Client.SigV4

  test "AWS documented GET object known answer" do
    headers = [
      {"range", "bytes=0-9"},
      {"x-amz-content-sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
      {"x-amz-date", "20130524T000000Z"}
    ]

    signed_headers = ["host", "range", "x-amz-content-sha256", "x-amz-date"]
    body_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    canonical =
      SigV4.canonical_request(
        :get,
        "https://examplebucket.s3.amazonaws.com/test.txt",
        headers,
        signed_headers,
        body_sha
      )

    scope = "20130524/us-east-1/s3/aws4_request"
    canonical_hash = :crypto.hash(:sha256, canonical) |> Base.encode16(case: :lower)
    string_to_sign = "AWS4-HMAC-SHA256\n20130524T000000Z\n#{scope}\n#{canonical_hash}"

    assert SigV4.signature(
             "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
             "20130524",
             "us-east-1",
             "s3",
             string_to_sign
           ) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
  end

  test "credentials add SigV4 headers and empty credentials are unchanged" do
    now = ~U[2026-08-21 12:34:56Z]
    url = "http://seaweedfs/embervm/?prefix=base%2Famd%2F&list-type=2"
    creds = %{access_key_id: "embervm", secret_access_key: "secret"}
    headers = SigV4.sign(:get, url, [], SigV4.unsigned_payload(), creds, now)

    authorization = headers |> Map.new() |> Map.fetch!("authorization")
    assert authorization =~ ~r/^AWS4-HMAC-SHA256 Credential=embervm\/20260821\/us-east-1\/s3\/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$/
    assert Map.new(headers)["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert SigV4.sign(:get, url, [], SigV4.unsigned_payload(), %{}, now) == []
  end

  test "canonical query encodes slashes in keys and values the way S3 recomputes them" do
    # SeaweedFS canonicalises the raw query with the AWS unreserved set, so a raw
    # `delimiter=/` must sign as `delimiter=%2F` or the signature never matches.
    canonical = SigV4.canonical_request(:get, "http://seaweedfs/embervm/?prefix=a/b&delimiter=/", [], ["host"], "UNSIGNED-PAYLOAD")
    [_method, _uri, query | _] = String.split(canonical, "\n")
    assert query == "delimiter=%2F&prefix=a%2Fb"
  end
end
