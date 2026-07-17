package main

import (
	"reflect"
	"strings"
	"testing"
)

// envFunc builds an env lookup from a map for k3sArgv's table tests.
func envFunc(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestK3sArgv(t *testing.T) {
	cases := []struct {
		name    string
		env     map[string]string
		want    []string
		wantErr string
	}{
		{
			name: "server with a secret maps to k3s server with host-gw, token, node-ip, and token-auth",
			env: map[string]string{
				"EMBER_GROUP_ROLE":   "server",
				"EMBER_GROUP_SECRET": "s3cr3t",
				"EMBER_GROUP_IP":     "10.101.0.10",
			},
			want: []string{
				"/usr/local/bin/k3s", "server",
				"--flannel-backend=host-gw",
				"--token", "s3cr3t",
				"--kube-apiserver-arg=token-auth-file=/run/ember/token-auth.csv",
				"--node-ip", "10.101.0.10",
				"--advertise-address", "10.101.0.10",
			},
		},
		{
			// The Task 3 single-VM spike: a bare, factless boot defaults to a
			// single k3s server with NO token flag (k3s auto-generates one) and NO
			// token-auth file (no external consumer kubeconfig).
			name: "factless boot (no env) defaults to a single k3s server, auto-token",
			env:  map[string]string{},
			want: []string{
				"/usr/local/bin/k3s", "server",
				"--flannel-backend=host-gw",
			},
		},
		{
			name: "explicit server role without a secret is also factless (auto-token)",
			env: map[string]string{
				"EMBER_GROUP_ROLE": "server",
				"EMBER_GROUP_IP":   "10.101.0.10",
			},
			want: []string{
				"/usr/local/bin/k3s", "server",
				"--flannel-backend=host-gw",
				"--node-ip", "10.101.0.10",
				"--advertise-address", "10.101.0.10",
			},
		},
		{
			name: "agent joins the server peer with the shared token and node-ip",
			env: map[string]string{
				"EMBER_GROUP_ROLE":   "agent",
				"EMBER_GROUP_SECRET": "s3cr3t",
				"EMBER_GROUP_IP":     "10.101.0.11",
				"EMBER_PEER_SERVER":  "10.101.0.10",
			},
			want: []string{
				"/usr/local/bin/k3s", "agent",
				"--server", "https://10.101.0.10:6443",
				"--token", "s3cr3t",
				"--node-ip", "10.101.0.11",
			},
		},
		{
			name: "agent without a secret is rejected (agents are multi-node)",
			env: map[string]string{
				"EMBER_GROUP_ROLE": "agent",
			},
			wantErr: "EMBER_GROUP_SECRET is unset",
		},
		{
			name: "agent without the server peer is rejected",
			env: map[string]string{
				"EMBER_GROUP_ROLE":   "agent",
				"EMBER_GROUP_SECRET": "s3cr3t",
			},
			wantErr: "EMBER_PEER_SERVER",
		},
		{
			name: "unknown role is rejected",
			env: map[string]string{
				"EMBER_GROUP_ROLE": "controller",
			},
			wantErr: "is not a known role",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := k3sArgv(envFunc(tc.env))
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected err: %v", err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("argv mismatch:\n got %v\nwant %v", got, tc.want)
			}
		})
	}
}

func TestServerTokenAuthCSV(t *testing.T) {
	if got := serverTokenAuthCSV(""); got != "" {
		t.Fatalf("empty secret must yield no CSV, got %q", got)
	}
	got := serverTokenAuthCSV("abc123")
	want := "abc123,ember,ember,system:masters\n"
	if got != want {
		t.Fatalf("CSV = %q, want %q", got, want)
	}
}

func TestRedactArgv(t *testing.T) {
	in := []string{"/usr/local/bin/k3s", "server", "--token", "SECRET", "--node-ip", "10.0.0.1"}
	got := redactArgv(in)
	for _, tok := range got {
		if tok == "SECRET" {
			t.Fatalf("redactArgv leaked the token: %v", got)
		}
	}
	// The IP (non-secret) survives so a failed boot is debuggable.
	if !reflect.DeepEqual(got, []string{"/usr/local/bin/k3s", "server", "--token", "<redacted>", "--node-ip", "10.0.0.1"}) {
		t.Fatalf("unexpected redaction: %v", got)
	}
}

func TestRoleHealthPort(t *testing.T) {
	if got := roleHealthPort("server"); got != 6443 {
		t.Fatalf("server health port = %d, want 6443", got)
	}
	if got := roleHealthPort("agent"); got != 10250 {
		t.Fatalf("agent health port = %d, want 10250", got)
	}
	// The factless default (empty role) is a server.
	if got := roleHealthPort(""); got != 6443 {
		t.Fatalf("factless health port = %d, want 6443 (server default)", got)
	}
}

func TestPeerFactsForLog(t *testing.T) {
	environ := []string{
		"PATH=/bin",
		"EMBER_PEER_SERVER=10.101.0.10",
		"EMBER_GROUP_SECRET=nope",
		"EMBER_PEER_AGENT_0=10.101.0.11",
	}
	got := peerFactsForLog(environ)
	want := []string{"EMBER_PEER_AGENT_0", "EMBER_PEER_SERVER"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("peer facts = %v, want %v (sorted, keys only, no secret)", got, want)
	}
}
