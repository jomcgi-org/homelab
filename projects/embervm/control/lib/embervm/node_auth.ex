defmodule Embervm.NodeAuth do
  @moduledoc """
  Builds the connection options shared by every control-plane dial to noded.

  The bearer token is loaded into application environment at boot. Keeping its
  transport representation here gives the three channel owners one mechanism
  to change if noded authentication evolves.

  ## SPIFFE mTLS dial (phase 2b of #5706, #5758)

  When `EMBERVM_NODED_DIAL_TLS` is on, the dial also carries a
  `GRPC.Credential` whose `:ssl` options point at the X.509-SVID files a
  spiffe-helper sidecar writes beside the control plane (`svid.pem`,
  `svid_key.pem`, `svid_bundle.pem`). Only the file PATHS live in application
  environment: OTP's `:ssl` reads the PEMs on each `:ssl.connect`, through its
  own PEM cache that re-checks file mtimes, so a rotated SVID is consumed by
  the next dial and this module never holds certificate bytes. Established
  channels are cached by `Embervm.NodeChannel`; noded bounds their age with
  its gRPC MaxConnectionAge (noded/cmd/spiffe.go), the server closes the
  connection, the cached channel is invalidated on its first transport error,
  and the re-dial handshakes with whatever files are on disk at that moment.

  Peer verification is fail-closed: `verify: :verify_peer` against the bundle
  file, no partial-chain trust, SNI disabled (an SVID carries a URI SAN and no
  DNS SAN, so OTP's hostname check must not run against the pod IP), and a
  `verify_fun` that requires the peer's URI SAN to be exactly the configured
  noded SPIFFE ID. The bearer header keeps being sent during the dual window
  so noded's plaintext and mTLS listeners see the same request shape.
  """

  require Record

  Record.defrecordp(
    :otp_certificate,
    :OTPCertificate,
    Record.extract(:OTPCertificate, from_lib: "public_key/include/OTP-PUB-KEY.hrl")
  )

  Record.defrecordp(
    :otp_tbs_certificate,
    :OTPTBSCertificate,
    Record.extract(:OTPTBSCertificate, from_lib: "public_key/include/OTP-PUB-KEY.hrl")
  )

  Record.defrecordp(
    :extension,
    :Extension,
    Record.extract(:Extension, from_lib: "public_key/include/OTP-PUB-KEY.hrl")
  )

  @connect_timeout_ms 3_000

  # id-ce-subjectAltName
  @subject_alt_name_oid {2, 5, 29, 17}

  @typedoc "File-based mTLS dial configuration: paths only, never PEM bytes."
  @type tls_config :: %{
          spiffe_id: String.t(),
          tls_port: pos_integer(),
          cert_file: String.t(),
          key_file: String.t(),
          bundle_file: String.t()
        }

  @doc "The maximum time a control-plane dial may spend establishing its TCP connection."
  @spec connect_timeout_ms() :: pos_integer()
  def connect_timeout_ms, do: @connect_timeout_ms

  @spec connect_opts() :: keyword()
  def connect_opts do
    [adapter_opts: [transport_opts: [timeout: @connect_timeout_ms]]] ++
      cred_opts(tls_config()) ++ auth_opts()
  end

  @doc """
  The address a control-plane dial opens for a registry `address`.

  Registrations advertise `"<pod_ip>:<plaintext_port>"`. With the mTLS dial on,
  the same host is dialed on the configured noded TLS port instead, while the
  plaintext listener keeps serving other callers during the dual window. With
  the dial off, or for an address that is not a plain `host:port` pair, the
  address is returned unchanged.
  """
  @spec dial_address(String.t()) :: String.t()
  def dial_address(address) when is_binary(address) do
    case tls_config() do
      nil ->
        address

      %{tls_port: port} ->
        case String.split(address, ":") do
          [host, plaintext_port] when host != "" and plaintext_port != "" ->
            case Integer.parse(plaintext_port) do
              {_, ""} -> "#{host}:#{port}"
              _ -> address
            end

          _ ->
            address
        end
    end
  end

  @doc """
  The active mTLS dial configuration, or nil when the plaintext bearer dial is
  in use. Read at dial time so a test or a redeploy flips every subsequent dial.
  """
  @spec tls_config() :: tls_config() | nil
  def tls_config do
    case Application.get_env(:embervm, :noded_tls) do
      %{spiffe_id: id, tls_port: port, cert_file: c, key_file: k, bundle_file: b} = config
      when is_binary(id) and is_integer(port) and is_binary(c) and is_binary(k) and
             is_binary(b) ->
        config

      _ ->
        nil
    end
  end

  @doc """
  Parses the mTLS dial configuration from the process environment. Returns nil
  when `EMBERVM_NODED_DIAL_TLS` is off. When it is on every other value is
  required and a missing or malformed one raises, so a half-configured control
  plane fails at boot instead of quietly dialing plaintext.
  """
  @spec tls_config_from_env!((String.t() -> String.t() | nil)) :: tls_config() | nil
  def tls_config_from_env!(get_env \\ &System.get_env/1) do
    if trimmed(get_env.("EMBERVM_NODED_DIAL_TLS")) in ["1", "true", "TRUE", "True"] do
      %{
        spiffe_id: required_spiffe_id!(get_env, "EMBERVM_NODED_SPIFFE_ID"),
        tls_port: required_port!(get_env, "EMBERVM_NODED_TLS_PORT"),
        cert_file: required!(get_env, "EMBERVM_NODED_SVID_CERT"),
        key_file: required!(get_env, "EMBERVM_NODED_SVID_KEY"),
        bundle_file: required!(get_env, "EMBERVM_NODED_SVID_BUNDLE")
      }
    else
      nil
    end
  end

  @doc """
  The `:ssl` options for one mTLS dial, built from file paths. Exposed so a
  test can hand them straight to `:ssl.connect/4` against a local listener.
  """
  @spec ssl_opts(tls_config()) :: [:ssl.tls_client_option()]
  def ssl_opts(%{spiffe_id: spiffe_id} = config) do
    [
      certfile: String.to_charlist(config.cert_file),
      keyfile: String.to_charlist(config.key_file),
      cacertfile: String.to_charlist(config.bundle_file),
      verify: :verify_peer,
      # An X.509-SVID has a URI SAN and no DNS SAN. With SNI disabled OTP skips
      # its hostname check entirely; identity is checked by verify_fun instead.
      server_name_indication: :disable,
      # Never shorten the chain to an intermediate the peer sent: the chain must
      # end at a root in the bundle file. Setting this explicitly also stops the
      # Mint adapter installing its own partial_chain, which would read and
      # cache the bundle bytes in persistent_term for the life of the VM.
      partial_chain: &__MODULE__.reject_partial_chain/1,
      verify_fun: {&__MODULE__.verify_peer_spiffe_id/3, spiffe_id}
    ]
  end

  @doc """
  `:ssl` `verify_fun` callback: accepts the peer certificate only when its URI
  SAN is exactly the expected SPIFFE ID. Chain certificates and OTP's own
  path-validation failures pass through unchanged.
  """
  @spec verify_peer_spiffe_id(term(), term(), String.t()) ::
          {:valid, String.t()} | {:fail, term()} | {:unknown, String.t()}
  def verify_peer_spiffe_id(_cert, {:bad_cert, _} = reason, _expected), do: {:fail, reason}
  def verify_peer_spiffe_id(_cert, {:extension, _}, expected), do: {:unknown, expected}
  def verify_peer_spiffe_id(_cert, :valid, expected), do: {:valid, expected}

  def verify_peer_spiffe_id(cert, :valid_peer, expected) do
    case uri_sans(cert) do
      [^expected] -> {:valid, expected}
      presented -> {:fail, {:bad_cert, {:spiffe_id_mismatch, presented}}}
    end
  end

  @doc false
  def reject_partial_chain(_chain), do: :unknown_ca

  @doc "The URI SAN entries of an OTP-decoded certificate, as binaries."
  @spec uri_sans(term()) :: [String.t()]
  def uri_sans(otp_certificate(tbsCertificate: tbs)) do
    case otp_tbs_certificate(tbs, :extensions) do
      extensions when is_list(extensions) ->
        for extension(extnID: @subject_alt_name_oid, extnValue: names) <- extensions,
            is_list(names),
            {:uniformResourceIdentifier, uri} <- names,
            do: IO.chardata_to_string(uri)

      _ ->
        []
    end
  end

  def uri_sans(_other), do: []

  defp cred_opts(nil), do: []
  defp cred_opts(config), do: [cred: GRPC.Credential.new(ssl: ssl_opts(config))]

  defp auth_opts do
    with token when is_binary(token) <- Application.get_env(:embervm, :noded_bearer_token, ""),
         trimmed when trimmed != "" <- String.trim(token) do
      [headers: [{"authorization", "Bearer " <> trimmed}]]
    else
      _ -> []
    end
  end

  defp required!(get_env, name) do
    case trimmed(get_env.(name)) do
      "" -> raise ArgumentError, "#{name} is required when EMBERVM_NODED_DIAL_TLS is on"
      value -> value
    end
  end

  defp required_spiffe_id!(get_env, name) do
    value = required!(get_env, name)

    if String.starts_with?(value, "spiffe://") do
      value
    else
      raise ArgumentError, "#{name} must be a spiffe:// URI, got #{inspect(value)}"
    end
  end

  defp required_port!(get_env, name) do
    value = required!(get_env, name)

    case Integer.parse(value) do
      {port, ""} when port in 1..65_535 -> port
      _ -> raise ArgumentError, "#{name} must be a TCP port, got #{inspect(value)}"
    end
  end

  defp trimmed(nil), do: ""
  defp trimmed(value) when is_binary(value), do: String.trim(value)
end
