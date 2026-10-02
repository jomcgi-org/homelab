package guestmem

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
)

func goodSample(nonce string, kills uint64) guestagent.MemoryStatus {
	total, available, supported, unsupported := uint64(1024), uint64(512), true, false
	return guestagent.MemoryStatus{Nonce: nonce, TotalBytes: &total, AvailableBytes: &available, PSISupported: &unsupported, OOMSupported: &supported, OOMKill: &kills}
}

func TestClientEvidence(t *testing.T) {
	for _, tc := range []struct {
		name, handshake, body string
		want                  State
	}{
		{"ok", "OK 12345\n", "good", OK},
		{"old agent", "OK 1\n", `{"clock_realtime_ns":0,"err":"unknown command \"memory_status\""}`, Unsupported},
		{"refused handshake", "ERR\n", "", Unsupported},
		{"unknown handshake", "OKAY 1024\n", "", Unsupported},
		{"bad handshake port", "OK nope\n", "", Unsupported},
		{"bounded handshake", strings.Repeat("x", 256), "", Unsupported},
		{"malformed JSON", "OK 1\n", `{broken`, Error},
		{"absent response", "OK 1\n", `{}`, Error},
		{"null response", "OK 1\n", `null`, Error},
		{"guest error", "OK 1\n", `{"err":"cannot read proc"}`, Error},
		{"unknown response", "OK 1\n", `{"identity":"fake"}`, Error},
		{"missing flags", "OK 1\n", `{"memory_status":{"nonce":"request","total_bytes":1,"available_bytes":0}}`, Error},
		{"wrong nonce", "OK 1\n", "wrong nonce", Error},
		{"oversized frame", "OK 1\n", "oversize", Error},
		{"truncated frame", "OK 1\n", "truncate", Error},
		{"trailing JSON", "OK 1\n", `{} {}`, Error},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := Client{Dial: func(context.Context, string) (net.Conn, error) {
				host, guest := net.Pipe()
				go func() {
					defer guest.Close()
					_ = guest.SetDeadline(time.Now().Add(2 * time.Second))
					br := bufio.NewReader(guest)
					line, _ := br.ReadString('\n')
					if line != "CONNECT 1024\n" {
						return
					}
					if _, err := io.WriteString(guest, tc.handshake); err != nil {
						return
					}
					if !strings.HasPrefix(tc.handshake, "OK ") {
						return
					}
					var hdr [4]byte
					if _, err := io.ReadFull(br, hdr[:]); err != nil {
						return
					}
					b := make([]byte, binary.BigEndian.Uint32(hdr[:]))
					if _, err := io.ReadFull(br, b); err != nil {
						return
					}
					var req struct{ Cmd, Nonce string }
					if json.Unmarshal(b, &req) != nil || req.Cmd != "memory_status" {
						return
					}
					body := []byte(tc.body)
					if tc.body == "good" || tc.body == "wrong nonce" {
						nonce := req.Nonce
						if tc.body == "wrong nonce" {
							nonce = "other"
						}
						body, _ = json.Marshal(map[string]any{"clock_realtime_ns": 0, "memory_status": goodSample(nonce, 0)})
					}
					n := uint32(len(body))
					if tc.body == "oversize" {
						n = maxFrameBytes + 1
					}
					if tc.body == "truncate" {
						n += 10
					}
					binary.BigEndian.PutUint32(hdr[:], n)
					_, _ = guest.Write(hdr[:])
					_, _ = guest.Write(body)
				}()
				return host, nil
			}}
			_, err := c.Fetch(context.Background(), "host-known-uds", "request")
			if tc.want == OK && err != nil {
				t.Fatal(err)
			}
			if tc.want != OK && err == nil {
				t.Fatal("invalid evidence became ok")
			}
			if errors.Is(err, ErrUnsupported) != (tc.want == Unsupported) {
				t.Fatalf("err=%v want=%s", err, tc.want)
			}
			if errors.Is(err, ErrAgentUnavailable) != (tc.want == Unsupported && tc.name != "old agent") {
				t.Fatalf("transport failure incorrectly cached as confirmed capability: %v", err)
			}
		})
	}
}

func TestDialAndTimeoutStates(t *testing.T) {
	for _, tc := range []struct {
		name        string
		err         error
		unsupported bool
	}{
		{"refused", syscall.ECONNREFUSED, true},
		{"missing socket", syscall.ENOENT, false},
		{"permission", syscall.EACCES, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := Client{Dial: func(context.Context, string) (net.Conn, error) { return nil, tc.err }}
			_, err := c.Fetch(context.Background(), "uds", "nonce")
			if err == nil || errors.Is(err, ErrUnsupported) != tc.unsupported {
				t.Fatalf("err=%v", err)
			}
			if errors.Is(err, ErrAgentUnavailable) != (tc.err == syscall.ECONNREFUSED) {
				t.Fatalf("refused listener must remain retryable: %v", err)
			}
		})
	}
	for _, phase := range []string{"handshake", "response"} {
		t.Run(phase+" timeout", func(t *testing.T) {
			c := Client{Dial: func(context.Context, string) (net.Conn, error) {
				host, guest := net.Pipe()
				go func() {
					defer guest.Close()
					br := bufio.NewReader(guest)
					_, _ = br.ReadString('\n')
					if phase == "response" {
						_, _ = io.WriteString(guest, "OK 1\n")
						_, _ = io.Copy(io.Discard, br)
					} else {
						_, _ = io.Copy(io.Discard, br)
					}
				}()
				return host, nil
			}}
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
			defer cancel()
			_, err := c.Fetch(ctx, "uds", "nonce")
			if err == nil || errors.Is(err, ErrUnsupported) {
				t.Fatalf("timeout became supported evidence: %v", err)
			}
		})
	}
}
