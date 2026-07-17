package main

import (
	"fmt"
	"os"
	"sort"
)

// Role env facts (standing decision 13). The platform injects only generic
// facts; this image owns the k3s knowledge. The k3s images' init maps them here.
const (
	// roleEnv names the member's role: "server" or "agent".
	roleEnv = "EMBER_GROUP_ROLE"
	// secretEnv is the per-group shared secret. It becomes K3S_TOKEN (server and
	// agent) and the static token-auth entry the server exposes for API access.
	secretEnv = "EMBER_GROUP_SECRET"
	// ownIPEnv is this member's own IP on the group subnet.
	ownIPEnv = "EMBER_GROUP_IP"
	// peerServerEnv is EMBER_PEER_SERVER: the server member's IP, injected on
	// agents so they know where to join. EMBER_PEER_<NAME> keys are the platform's
	// peer map (decision 13: expanded names uppercased, `-` mapped to `_`); the
	// server member is named `server`, so agents receive EMBER_PEER_SERVER.
	peerServerEnv = "EMBER_PEER_SERVER"
)

const (
	roleServer = "server"
	roleAgent  = "agent"
)

// k3sServerPort / k3sAgentPort are the health surfaces (plan Task 3): the server
// API on 6443, the agent kubelet on 10250. Named here for the flag mapping and
// so a reader sees the health contract in one place.
const (
	k3sServerPort = 6443
	k3sAgentPort  = 10250
)

// k3sArgv builds the k3s command line from the environment (standing decision
// 13). It is a pure function of an env lookup so it is table-testable without a
// microVM. env mirrors os.Getenv. It returns the argv (starting with the k3s
// binary path) or an error when a required fact is missing (a member that cannot
// form its command must fail the boot loudly, not start a misconfigured node).
//
// Server (role server): `k3s server` with the sqlite datastore (the k3s
// default, so no datastore flag), flannel host-gw (works over the group's flat
// L2 subnet without vxlan kernel modules, Fork 3), the group secret as the
// cluster token, and node-ip pinned to the member's own group IP so the
// advertised API and flannel routes use the stable pinned address. The static
// token-auth API entry is written separately (writeServerTokenAuth) from the
// same secret.
//
// Agent (role agent): `k3s agent` joining the server at
// https://$EMBER_PEER_SERVER:6443 with the same token, node-ip pinned likewise.
func k3sArgv(env func(string) string) ([]string, error) {
	const bin = "/usr/local/bin/k3s"
	role := env(roleEnv)
	secret := env(secretEnv)
	ownIP := env(ownIPEnv)

	switch role {
	case roleServer:
		if secret == "" {
			return nil, fmt.Errorf("%s is server but %s is unset", roleEnv, secretEnv)
		}
		argv := []string{
			bin, "server",
			"--flannel-backend=host-gw",
			"--token", secret,
			// Static token-auth entry derived from the same secret (decision 13):
			// the consumer's kubeconfig authenticates with EMBER_GROUP_SECRET as a
			// bearer token. The file is written by the supervisor before exec
			// (writeServerTokenAuth) from serverTokenAuthCSV(secret).
			"--kube-apiserver-arg=token-auth-file=" + tokenAuthPath,
		}
		if ownIP != "" {
			argv = append(argv, "--node-ip", ownIP, "--advertise-address", ownIP)
		}
		return argv, nil

	case roleAgent:
		if secret == "" {
			return nil, fmt.Errorf("%s is agent but %s is unset", roleEnv, secretEnv)
		}
		server := env(peerServerEnv)
		if server == "" {
			return nil, fmt.Errorf("%s is agent but %s (the server peer IP) is unset", roleEnv, peerServerEnv)
		}
		argv := []string{
			bin, "agent",
			"--server", fmt.Sprintf("https://%s:%d", server, k3sServerPort),
			"--token", secret,
		}
		if ownIP != "" {
			argv = append(argv, "--node-ip", ownIP)
		}
		return argv, nil

	case "":
		return nil, fmt.Errorf("%s is unset (expected %q or %q)", roleEnv, roleServer, roleAgent)
	default:
		return nil, fmt.Errorf("%s=%q is not a known role (expected %q or %q)", roleEnv, role, roleServer, roleAgent)
	}
}

// serverTokenAuthCSV renders the static token-auth entry the k3s server exposes
// for API access (standing decision 13: "a static token-auth entry derived from
// the same secret"). k3s reads --token-auth-file as CSV: token,user,uid,groups.
// The consumer's kubeconfig authenticates with the same EMBER_GROUP_SECRET as a
// bearer token, mapped to a cluster-admin-grouped user. It is a pure function so
// it is table-testable. Returns "" when secret is empty (no auth entry written).
func serverTokenAuthCSV(secret string) string {
	if secret == "" {
		return ""
	}
	// user "ember", uid "ember", group "system:masters" (cluster-admin). The
	// group grant is the scratch-tier in-cluster posture documented in the
	// consumer wiring (plan Task 10); the token dies with the group instance.
	return fmt.Sprintf("%s,ember,ember,system:masters\n", secret)
}

// tokenAuthEnv / tokenAuthPath: the server writes the token-auth CSV to a tmpfs
// path and passes it via K3S extra args through the supervisor. Kept as
// constants so the writer and the argv assembler agree.
const tokenAuthPath = "/run/ember/token-auth.csv"

// peerFactsForLog returns the sorted EMBER_PEER_* keys present in the
// environment, for a non-secret startup log line that shows the peer map the
// init resolved (values omitted: a peer IP is not secret, but keeping the log to
// keys matches the secret-safe posture of setMmdsEnv). Pure over os.Environ-like
// input for testability.
func peerFactsForLog(environ []string) []string {
	var keys []string
	for _, kv := range environ {
		if len(kv) >= len("EMBER_PEER_") && kv[:len("EMBER_PEER_")] == "EMBER_PEER_" {
			// kv is KEY=VALUE; take KEY only.
			for i := 0; i < len(kv); i++ {
				if kv[i] == '=' {
					keys = append(keys, kv[:i])
					break
				}
			}
		}
	}
	sort.Strings(keys)
	return keys
}

// getenv is the production env lookup passed to k3sArgv; a seam so tests inject
// a map without mutating the process environment.
var getenv = os.Getenv
