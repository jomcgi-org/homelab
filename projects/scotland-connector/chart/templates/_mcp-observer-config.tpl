{{- define "scotland-connector.observerConfig" -}}
admin:
  address:
    socket_address: {address: 127.0.0.1, port_value: 9901}
static_resources:
  listeners:
    - name: mcp
      address:
        socket_address: {address: 0.0.0.0, port_value: 8080}
      per_connection_buffer_limit_bytes: 65536
      filter_chains:
        - filters:
            - name: envoy.filters.network.connection_limit
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.connection_limit.v3.ConnectionLimit
                stat_prefix: scotland_mcp_connections
                max_connections: 64
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: scotland_mcp
                codec_type: HTTP1
                stream_idle_timeout: 10s
                request_timeout: 10s
                request_headers_timeout: 5s
                common_http_protocol_options:
                  idle_timeout: 30s
                  max_headers_count: 100
                max_request_headers_kb: 32
                route_config:
                  name: scotland_only
                  virtual_hosts:
                    - name: scotland
                      domains: ["*"]
                      routes:
                        - match: {path: /mcp}
                          route:
                            cluster: scotland
                            timeout: 9s
                        - match: {prefix: /}
                          direct_response: {status: 404}
                access_log:
                  - name: envoy.access_loggers.stdout
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog
                      log_format:
                        json_format:
                          duration_ms: "%DURATION%"
                          status: "%RESPONSE_CODE%"
                          flags: "%RESPONSE_FLAGS%"
                          method: "%DYNAMIC_METADATA(envoy.filters.http.mcp:method):64%"
                          tool: "%DYNAMIC_METADATA(envoy.filters.http.mcp:params:name):64%"
                          parse_status: "%DYNAMIC_METADATA(envoy.filters.http.mcp:status):64%"
                          exceeds_limit: "%DYNAMIC_METADATA(envoy.filters.http.mcp:is_exceeding_limit)%"
                http_filters:
                  - name: envoy.filters.http.mcp
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.mcp.v3.Mcp
                      traffic_mode: PASS_THROUGH
                      attribute_source: BODY
                      max_request_body_size: 65536
                      clear_route_cache: false
                      early_terminate_when_routable: false
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  clusters:
    - name: scotland
      type: STRICT_DNS
      connect_timeout: 1s
      dns_lookup_family: V4_ONLY
      per_connection_buffer_limit_bytes: 65536
      circuit_breakers:
        thresholds:
          - priority: DEFAULT
            max_connections: 32
            max_pending_requests: 32
            max_requests: 32
            max_retries: 0
      load_assignment:
        cluster_name: scotland
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address:
                      address: {{ include "scotland-connector.fullname" . }}.{{ .Release.Namespace }}.svc
                      port_value: {{ .Values.service.port }}
{{- end -}}
