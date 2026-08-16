package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	proxyListenAddress      = "127.0.0.1:1024"
	maxProxyHeadBytes       = 64 << 10
	egressConnectTimeout    = 5 * time.Second
	egressConnectAttempts   = 3
	egressConnectBackoff    = 200 * time.Millisecond
	proxyReadBufferSize     = 4096
	proxyHeaderEnd          = "\r\n\r\n"
	proxyConnectEstablished = "HTTP/1.1 200 Connection Established\r\n\r\n"
)

var errProxyConfigDisabled = errors.New("proxy configuration is unavailable")

var errProxyDestinationRefused = errors.New("proxy destination is not allowlisted")

// ProxyConfig is the complete destination policy for the in-guest proxy.
// Nil maps or slices disable all forwarding, which makes the zero value safe.
type ProxyConfig struct {
	HostMapping map[string]string
	Allowlist   []string
}

// LoadProxyConfig reads the destination policy from the guest environment.
// Any absent, malformed, or invalid value returns a zero config that refuses
// every request.
func LoadProxyConfig() (ProxyConfig, error) {
	mappingJSON := os.Getenv("SHOTTER_HOST_MAPPING")
	allowlistJSON := os.Getenv("SHOTTER_ALLOWLIST")
	if mappingJSON == "" || allowlistJSON == "" {
		return ProxyConfig{}, fmt.Errorf("%w: SHOTTER_HOST_MAPPING and SHOTTER_ALLOWLIST are required", errProxyConfigDisabled)
	}

	var config ProxyConfig
	if err := json.Unmarshal([]byte(mappingJSON), &config.HostMapping); err != nil {
		return ProxyConfig{}, fmt.Errorf("%w: decode SHOTTER_HOST_MAPPING: %v", errProxyConfigDisabled, err)
	}
	if err := json.Unmarshal([]byte(allowlistJSON), &config.Allowlist); err != nil {
		return ProxyConfig{}, fmt.Errorf("%w: decode SHOTTER_ALLOWLIST: %v", errProxyConfigDisabled, err)
	}
	if err := config.validate(); err != nil {
		return ProxyConfig{}, fmt.Errorf("%w: %v", errProxyConfigDisabled, err)
	}
	return config, nil
}

func (c ProxyConfig) validate() error {
	if c.HostMapping == nil || c.Allowlist == nil {
		return errors.New("mapping and allowlist must be JSON objects and arrays")
	}
	seenHosts := make(map[string]struct{}, len(c.HostMapping))
	for publicHost, mappedDestination := range c.HostMapping {
		if !validProxyHost(publicHost) || strings.Contains(publicHost, ":") {
			return fmt.Errorf("invalid mapped hostname %q", publicHost)
		}
		foldedHost := strings.ToLower(publicHost)
		if _, exists := seenHosts[foldedHost]; exists {
			return fmt.Errorf("duplicate mapped hostname %q", publicHost)
		}
		seenHosts[foldedHost] = struct{}{}
		if _, err := parseDestination(mappedDestination, false); err != nil {
			return fmt.Errorf("invalid destination for %q: %w", publicHost, err)
		}
	}
	for _, allowedDestination := range c.Allowlist {
		if _, err := parseDestination(allowedDestination, false); err != nil {
			return fmt.Errorf("invalid allowlist destination %q: %w", allowedDestination, err)
		}
	}
	return nil
}

type proxyDestination struct {
	host string
	port string
}

func parseDestination(value string, defaultHTTPPort bool) (proxyDestination, error) {
	if value == "" || strings.TrimSpace(value) != value {
		return proxyDestination{}, errors.New("destination is empty or contains surrounding whitespace")
	}

	host, port, err := net.SplitHostPort(value)
	if err != nil && defaultHTTPPort {
		switch {
		case !strings.Contains(value, ":"):
			host, port, err = value, "80", nil
		case strings.HasPrefix(value, "[") && strings.HasSuffix(value, "]"):
			host, port, err = net.SplitHostPort(value + ":80")
		}
	}
	if err != nil || host == "" || port == "" {
		return proxyDestination{}, fmt.Errorf("want host:port, got %q", value)
	}
	if !validProxyHost(host) {
		return proxyDestination{}, fmt.Errorf("invalid hostname %q", host)
	}
	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		return proxyDestination{}, fmt.Errorf("invalid port %q", port)
	}
	return proxyDestination{host: host, port: port}, nil
}

func validProxyHost(host string) bool {
	if host == "" || strings.TrimSpace(host) != host {
		return false
	}
	if net.ParseIP(host) != nil {
		return true
	}
	for _, character := range host {
		if character >= 'a' && character <= 'z' ||
			character >= 'A' && character <= 'Z' ||
			character >= '0' && character <= '9' ||
			character == '.' || character == '-' {
			continue
		}
		return false
	}
	return true
}

func destinationsEqual(left, right proxyDestination) bool {
	return strings.EqualFold(left.host, right.host) && left.port == right.port
}

func (c ProxyConfig) resolve(requested string) (string, bool) {
	if err := c.validate(); err != nil {
		return "", false
	}
	requestedDestination, err := parseDestination(requested, false)
	if err != nil {
		return "", false
	}

	resolved := requested
	for publicHost, mappedDestination := range c.HostMapping {
		if strings.EqualFold(requestedDestination.host, publicHost) {
			resolved = mappedDestination
			break
		}
	}
	resolvedDestination, err := parseDestination(resolved, false)
	if err != nil {
		return "", false
	}
	for _, allowed := range c.Allowlist {
		allowedDestination, err := parseDestination(allowed, false)
		if err == nil && destinationsEqual(resolvedDestination, allowedDestination) {
			return resolved, true
		}
	}
	return "", false
}

type proxyDialer func(context.Context, uint32, uint32) (net.Conn, error)

// ProxyServer accepts Chromium proxy connections and forwards only resolved,
// allowlisted destinations to the host egress lane.
type ProxyServer struct {
	config ProxyConfig
	logger *slog.Logger
	dial   proxyDialer
}

func newProxyServer(config ProxyConfig, logger *slog.Logger, dial proxyDialer) *ProxyServer {
	if logger == nil {
		logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if dial == nil {
		dial = dialVsock
	}
	return &ProxyServer{config: config, logger: logger, dial: dial}
}

func startProxyServer(ctx context.Context, config ProxyConfig, logger *slog.Logger) (<-chan error, error) {
	listener, err := net.Listen("tcp", proxyListenAddress)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", proxyListenAddress, err)
	}
	logger.Info("ember-shotter-init: egress proxy listening", "address", proxyListenAddress)

	server := newProxyServer(config, logger, nil)
	serveErr := make(chan error, 1)
	go func() {
		serveErr <- server.serve(ctx, listener)
		close(serveErr)
	}()
	return serveErr, nil
}

func (s *ProxyServer) serve(ctx context.Context, listener net.Listener) error {
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("accept proxy connection: %w", err)
		}
		go s.handleConnection(conn)
	}
}

func (s *ProxyServer) handleConnection(conn net.Conn) {
	defer conn.Close()

	destination, pending, isConnect, err := readProxyRequest(conn)
	if err != nil {
		s.logger.Warn("ember-shotter-init: refusing malformed proxy request", "err", err)
		return
	}
	if err := s.forward(conn, destination, pending, isConnect); err != nil {
		if errors.Is(err, errProxyDestinationRefused) {
			s.logger.Warn("ember-shotter-init: refusing proxy destination", "destination", destination)
			return
		}
		s.logger.Warn("ember-shotter-init: proxy forwarding stopped", "destination", destination, "err", err)
	}
}

func readProxyRequest(conn net.Conn) (string, []byte, bool, error) {
	buffer := make([]byte, 0, proxyReadBufferSize)
	chunk := make([]byte, proxyReadBufferSize)
	for {
		if headerIndex := bytes.Index(buffer, []byte(proxyHeaderEnd)); headerIndex >= 0 {
			headerEnd := headerIndex + len(proxyHeaderEnd)
			if headerEnd > maxProxyHeadBytes {
				return "", nil, false, errors.New("proxy request head exceeds limit")
			}
			return parseProxyHead(buffer[:headerIndex], buffer[headerEnd:])
		}
		if len(buffer) > maxProxyHeadBytes {
			return "", nil, false, errors.New("proxy request head exceeds limit")
		}

		readSize := len(chunk)
		if remaining := maxProxyHeadBytes + 1 - len(buffer); remaining < readSize {
			readSize = remaining
		}
		n, err := conn.Read(chunk[:readSize])
		if n > 0 {
			buffer = append(buffer, chunk[:n]...)
			continue
		}
		if err != nil {
			return "", nil, false, fmt.Errorf("read proxy request head: %w", err)
		}
		return "", nil, false, io.ErrNoProgress
	}
}

func parseProxyHead(rawHead, leftover []byte) (string, []byte, bool, error) {
	lines := bytes.Split(rawHead, []byte("\r\n"))
	if len(lines) == 0 {
		return "", nil, false, errors.New("proxy request has no request line")
	}
	parts := strings.Split(string(lines[0]), " ")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || !strings.HasPrefix(parts[2], "HTTP/") {
		return "", nil, false, errors.New("malformed proxy request line")
	}
	if strings.EqualFold(parts[0], "CONNECT") {
		if _, err := parseDestination(parts[1], false); err != nil {
			return "", nil, false, fmt.Errorf("invalid CONNECT destination: %w", err)
		}
		return parts[1], append([]byte(nil), leftover...), true, nil
	}

	target, err := url.ParseRequestURI(parts[1])
	if err != nil || !target.IsAbs() || target.Host == "" || (!strings.EqualFold(target.Scheme, "http") && !strings.EqualFold(target.Scheme, "https")) {
		return "", nil, false, errors.New("proxy request target is not an absolute HTTP URI")
	}
	var hostHeader string
	for _, line := range lines[1:] {
		name, value, found := bytes.Cut(line, []byte(":"))
		if !found || !strings.EqualFold(strings.TrimSpace(string(name)), "Host") {
			continue
		}
		if hostHeader != "" {
			return "", nil, false, errors.New("proxy request has multiple Host headers")
		}
		hostHeader = strings.TrimSpace(string(value))
	}
	if hostHeader == "" {
		return "", nil, false, errors.New("proxy request has no Host header")
	}
	host, err := parseDestination(hostHeader, true)
	if err != nil {
		return "", nil, false, fmt.Errorf("invalid Host header: %w", err)
	}
	destination := net.JoinHostPort(host.host, host.port)
	pending := make([]byte, 0, len(rawHead)+len(proxyHeaderEnd)+len(leftover))
	pending = append(pending, rawHead...)
	pending = append(pending, proxyHeaderEnd...)
	pending = append(pending, leftover...)
	return destination, pending, false, nil
}

func (s *ProxyServer) forward(client net.Conn, destination string, pending []byte, isConnect bool) error {
	resolved, allowed := s.config.resolve(destination)
	if !allowed {
		return fmt.Errorf("%w: %s", errProxyDestinationRefused, destination)
	}

	var upstream net.Conn
	var lastErr error
	for attempt := 0; attempt < egressConnectAttempts; attempt++ {
		attemptContext, cancel := context.WithTimeout(context.Background(), egressConnectTimeout)
		upstream, lastErr = s.dial(attemptContext, vsockproto.HostCID, vsockproto.EgressPort)
		cancel()
		if lastErr == nil && upstream != nil {
			break
		}
		if lastErr == nil {
			lastErr = errors.New("vsock dialer returned a nil connection")
		}
		if attempt+1 < egressConnectAttempts {
			time.Sleep(egressConnectBackoff * time.Duration(1<<attempt))
		}
	}
	if upstream == nil {
		return fmt.Errorf("vsock connect failed after %d attempts: %w", egressConnectAttempts, lastErr)
	}
	defer upstream.Close()

	preamble, err := encodeLatin1(resolved + "\n")
	if err != nil {
		return fmt.Errorf("encode egress preamble: %w", err)
	}
	if err := writeAll(upstream, preamble); err != nil {
		return fmt.Errorf("write egress preamble: %w", err)
	}
	if isConnect {
		if err := writeAll(client, []byte(proxyConnectEstablished)); err != nil {
			return fmt.Errorf("write CONNECT response: %w", err)
		}
	}
	if len(pending) > 0 {
		if err := writeAll(upstream, pending); err != nil {
			return fmt.Errorf("write pending proxy bytes: %w", err)
		}
	}

	copyErr := make(chan error, 2)
	go func() {
		_, err := io.Copy(upstream, client)
		copyErr <- err
	}()
	go func() {
		_, err := io.Copy(client, upstream)
		copyErr <- err
	}()
	firstErr := <-copyErr
	_ = client.Close()
	_ = upstream.Close()
	secondErr := <-copyErr
	if firstErr != nil && !errors.Is(firstErr, net.ErrClosed) {
		return firstErr
	}
	if secondErr != nil && !errors.Is(secondErr, net.ErrClosed) {
		return secondErr
	}
	return nil
}

func encodeLatin1(value string) ([]byte, error) {
	encoded := make([]byte, 0, len(value))
	for _, character := range value {
		if character > 255 {
			return nil, fmt.Errorf("character %U is outside Latin-1", character)
		}
		encoded = append(encoded, byte(character))
	}
	return encoded, nil
}

func writeAll(writer io.Writer, data []byte) error {
	for len(data) > 0 {
		written, err := writer.Write(data)
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		data = data[written:]
	}
	return nil
}
