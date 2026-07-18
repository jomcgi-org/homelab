package main

import (
	"fmt"
	"os"
	"sort"
)

// Role env facts (standing decision 13). The platform injects only generic
// facts; this image owns the k3s knowledge. The k3s images' init maps them here.
const (
	// memberEnv names this member's expanded name (e.g. "server", "agent-0"). The
	// platform sets it for EVERY composite member (server and agent) and never for
	// a base build or the stateful-postgres lane, so its presence is the reliable
	// marker of an R5 composite member runtime boot. It is preferred over roleEnv
	// as the discriminator because the CR's role field can be empty (roleEnv then
	// defaults to server in k3sArgv), whereas the expanded name is always set.
	memberEnv = "EMBER_GROUP_MEMBER"
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
// so a reader sees the health contract in one place. The actual TCP health-gate
// is done by noded on the port from the CR (finishStatefulStart); these are the
// values that CR should carry per role, surfaced by roleHealthPort for logging.
const (
	k3sServerPort = 6443
	k3sAgentPort  = 10250
)

// roleHealthPort returns the TCP port a member of the given role serves its
// health surface on (server API 6443, agent kubelet 10250), so the startup log
// records the port noded is expected to health-gate. A pure helper; it keeps the
// two port constants live and documents the role/port contract in one place.
func roleHealthPort(role string) int {
	switch role {
	case roleAgent:
		return k3sAgentPort
	default: // server, and the factless single-server default
		return k3sServerPort
	}
}

// k3sArgv builds the k3s command line from the environment (standing decision
// 13). It is a pure function of an env lookup so it is table-testable without a
// microVM. env mirrors os.Getenv. It returns the argv (starting with the k3s
// binary path) or an error only for a genuinely malformed request (an unknown
// role, or an agent missing its required multi-node facts).
//
// A BARE, FACTLESS boot runs a single k3s server. When EMBER_GROUP_ROLE is
// unset the member defaults to the server role: this is the Task 3 single-VM
// spike, which boots factlessly and proves one k3s server reaches Ready with no
// injected env. The peer map and shared secret are a MULTI-NODE concern injected
// later by Task 5's StartGroupMember FRESH env seam, NOT by this spike, so a
// missing role must NOT kill PID 1.
//
// Server (role server, or the factless default): `k3s server` with the sqlite
// datastore (the k3s default, so no datastore flag), flannel host-gw (works over
// the group's flat L2 subnet without vxlan kernel modules, Fork 3), and node-ip
// pinned to the member's own group IP when supplied. The cluster token is the
// group secret WHEN PRESENT; when EMBER_GROUP_SECRET is unset (the factless
// spike) k3s AUTO-GENERATES its own token, which is correct for a single-server
// cluster with no peers to join it. The static token-auth API entry is likewise
// written only when a secret is present (writeServerTokenAuth); a factless spike
// has no external consumer kubeconfig to authenticate.
//
// Agent (role agent): `k3s agent` joining the server at
// https://$EMBER_PEER_SERVER:6443 with the shared token, node-ip pinned
// likewise. Agents are inherently multi-node: they REQUIRE the secret and the
// server peer IP (a missing fact IS a malformed agent request, so it errors).
func k3sArgv(env func(string) string) ([]string, error) {
	const bin = "/usr/local/bin/k3s"
	role := env(roleEnv)
	if role == "" {
		// Factless boot: the single-server spike. Default to the server role.
		role = roleServer
	}
	secret := env(secretEnv)
	ownIP := env(ownIPEnv)

	switch role {
	case roleServer:
		argv := []string{
			bin, "server",
			"--flannel-backend=host-gw",
			// Non-default cluster/service CIDRs: the OUTER homelab cluster is also
			// k3s with the default 10.42.0.0/16 pods + 10.43.0.0/16 services, so an
			// inner cluster left on the defaults shadows the entire outer pod
			// network. Once flannel host-gw installs the inner per-node 10.42.x/24
			// routes, the guest routes replies to any outer-pod client INTO its own
			// pod network instead of back out the default gateway - the entry DNAT
			// blackhole behind the R6 Gate-1 EOF (traffic in, SYN-ACK swallowed).
			"--cluster-cidr=10.52.0.0/16",
			"--service-cidr=10.53.0.0/16",
			"--cluster-dns=10.53.0.10",
		}
		if secret != "" {
			// A supplied secret becomes the cluster token AND the static token-auth
			// API entry (decision 13): the consumer's kubeconfig authenticates with
			// EMBER_GROUP_SECRET as a bearer token. Absent (the factless spike) k3s
			// auto-generates its own token and no token-auth file is written.
			argv = append(argv,
				"--token", secret,
				"--kube-apiserver-arg=token-auth-file="+tokenAuthPath,
			)
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
