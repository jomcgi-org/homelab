//go:build linux

package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// postgresUser / postgresUID / postgresGID are the in-guest Postgres account.
// initdb and the postgres server both REFUSE to run as root, so guest-init
// (PID 1, root, which it must be to mkfs+mount the volume) drops to this uid for
// every Postgres child. The apko image creates this account (see apko.yaml).
const (
	postgresUser = "postgres"
	postgresUID  = 70
	postgresGID  = 70
)

// pgPort is the TCP port Postgres listens on inside the VM (the CR's
// stateful.port). noded health-gates a stateful runtime boot by TCP-connecting
// here over the tap; the ready probe below uses the same local port.
const pgPort = 5432

// bootstrapAndLaunchPostgres owns the stateful data path. mountPath is the
// mounted volume (e.g. /data); PGDATA lives at <mountPath>/pgdata so the volume
// root can hold other state without colliding with Postgres's "directory must be
// empty" initdb check. On an EMPTY/uninitialized PGDATA it runs initdb (scram
// auth, the superuser password from $POSTGRES_PASSWORD, listen on *, the
// `scratch` database), then launches `postgres`. On a NON-EMPTY PGDATA (a later
// cold boot against an initialized volume) it SKIPS initdb entirely and just
// launches Postgres; crash/WAL recovery runs automatically. It flips ready once
// Postgres accepts a local TCP connection. Postgres runs as a child (not exec)
// so the vsock ready server keeps serving; this init stays PID 1.
func bootstrapAndLaunchPostgres(ctx context.Context, logger *slog.Logger, mountPath string, ready *atomic.Bool) error {
	pgdata := filepath.Join(mountPath, "pgdata")

	// The volume is owned by root after mkfs+mount; hand the mount point and
	// PGDATA to the postgres uid so initdb/postgres can write.
	if err := os.MkdirAll(pgdata, 0o700); err != nil {
		return fmt.Errorf("mkdir PGDATA %s: %w", pgdata, err)
	}
	if err := chownRecursive(mountPath, postgresUID, postgresGID); err != nil {
		return fmt.Errorf("chown volume %s to postgres: %w", mountPath, err)
	}

	initialized, err := pgDataInitialized(pgdata)
	if err != nil {
		return fmt.Errorf("probe PGDATA %s: %w", pgdata, err)
	}
	if !initialized {
		password := os.Getenv("POSTGRES_PASSWORD")
		if password == "" {
			// Fail loudly: a scratch Postgres with no superuser password would
			// initdb with trust auth, which the pg_hba below forbids anyway. The
			// secretRef seam is the contract; a missing password is a wiring bug.
			return fmt.Errorf("POSTGRES_PASSWORD unset on first boot: cannot initdb (check the CR secretRef and the Secret's POSTGRES_PASSWORD key)")
		}
		logger.Info("stateful postgres: PGDATA empty, running initdb", "pgdata", pgdata)
		if err := runInitdb(logger, pgdata, password); err != nil {
			return fmt.Errorf("initdb: %w", err)
		}
	} else {
		logger.Info("stateful postgres: PGDATA already initialized, skipping initdb (WAL recovery runs on start)", "pgdata", pgdata)
	}

	// Ensure the cluster-internal pg_hba rule on EVERY boot, not just first boot.
	// pgDataInitialized keys on PG_VERSION, which initdb writes BEFORE this policy
	// is appended; a first boot interrupted in that window (an aggressive idle-bank
	// or a destroy during boot) leaves PG_VERSION present but pg_hba.conf without
	// the pod-network rule, and a first-boot-only config would then skip it forever,
	// leaving the volume permanently rejecting every cluster connection ("no
	// pg_hba.conf entry for host ..."). Running it unconditionally and idempotently
	// self-heals such a volume on its next cold boot and closes the race for good.
	// It runs before postgres launches, so the server reads the corrected file at
	// startup with no reload needed. (Relights resume a snapshot taken from an
	// already-serving VM, whose pg_hba was necessarily complete, so they are
	// unaffected; only cold boots re-read the on-disk file.)
	if err := ensureHostAuth(logger, pgdata); err != nil {
		return fmt.Errorf("configure pg_hba: %w", err)
	}

	// Launch the server as a child so PID 1 (this init) keeps the vsock ready
	// server alive. fsync stays ON (the default): the volume IS the durability
	// story, so we never trade it for speed. shared_buffers is small (128MB) to
	// fit the 512Mi guest.
	cmd := postgresCommand(ctx, pgdata)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start postgres: %w", err)
	}
	logger.Info("stateful postgres: launched", "pgdata", pgdata, "port", pgPort)

	// On first boot, create the `scratch` database once Postgres is up. initdb
	// creates only the template/postgres databases; the app database is a
	// post-start step (createdb over the local socket as the superuser).
	go func() {
		if err := waitPostgresReady(ctx, logger); err != nil {
			logger.Warn("stateful postgres: readiness wait failed", "err", err)
			return
		}
		if !initialized {
			if err := ensureScratchDatabase(logger); err != nil {
				logger.Warn("stateful postgres: create scratch database failed", "err", err)
			}
		}
		ready.Store(true)
		logger.Info("stateful postgres: ready (accepting TCP)")
	}()

	// Reap the postgres child. If it exits, that is a hard failure of the
	// stateful guest; return so run surfaces it (noded's TCP probe will already
	// have failed the wake).
	return cmd.Wait()
}

// postgresCommand builds the `postgres` server invocation, dropped to the
// postgres uid, listening on all interfaces (the tap NIC) on pgPort with fsync
// on and a small shared_buffers.
func postgresCommand(ctx context.Context, pgdata string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "postgres",
		"-D", pgdata,
		"-p", strconv.Itoa(pgPort),
		"-c", "listen_addresses=*",
		// Put the unix socket on the writable tmpfs. The rootfs is read-only and
		// the compiled default (/run/postgresql) does not exist there, so postgres
		// would fail to create its socket; /tmp is a tmpfs and matches the PGHOST
		// the bootstrap createdb connects on.
		"-c", "unix_socket_directories=/tmp",
		"-c", "fsync=on",
		// MEMORY COUPLING: these are sized against the workload's memMib (150 MiB
		// for demo-postgres, see chart/templates/workload-demo-postgres.yaml). The
		// old 128MB shared_buffers was Postgres's own default and would consume
		// almost the entire guest on its own, so it must not drift back up without
		// memMib moving with it. Passed as -c so they apply at every start
		// regardless of what initdb wrote into an existing volume's
		// postgresql.conf; there is no reinit needed to pick them up.
		//
		// 32MB of shared buffers is ample for the demo's single small table, and
		// maintenance_work_mem is pulled off its 64MB default so an autovacuum
		// worker cannot claim a third of the guest.
		"-c", "shared_buffers=32MB",
		"-c", "maintenance_work_mem=16MB",
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "PGDATA="+pgdata)
	dropToPostgres(cmd)
	return cmd
}

// runInitdb initializes PGDATA as the postgres user with the superuser password
// from `password`, delivered via a --pwfile so the secret never appears in argv
// (visible in /proc). The pwfile lives on the tmpfs and is removed immediately
// after. TCP auth is scram (the network boundary); the local unix socket is
// trust: the guest is a single-tenant, isolated microVM where the only local
// callers are guest-init's own bootstrap (initdb/createdb), so a password on the
// in-VM socket buys nothing and would only make the bootstrap createdb fail.
func runInitdb(logger *slog.Logger, pgdata, password string) error {
	pwfile := "/tmp/.pgpw"
	if err := os.WriteFile(pwfile, []byte(password), 0o600); err != nil {
		return fmt.Errorf("write pwfile: %w", err)
	}
	if err := os.Chown(pwfile, postgresUID, postgresGID); err != nil {
		return fmt.Errorf("chown pwfile: %w", err)
	}
	defer func() { _ = os.Remove(pwfile) }()

	cmd := exec.Command("initdb",
		"-D", pgdata,
		"-U", postgresUser,
		"--auth-host=scram-sha-256",
		"--auth-local=trust",
		"--pwfile", pwfile,
		"-E", "UTF8",
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "PGDATA="+pgdata)
	dropToPostgres(cmd)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("initdb run: %w", err)
	}
	logger.Info("stateful postgres: initdb complete", "pgdata", pgdata)
	return nil
}

// hbaRuleMarker is the comment that heads the cluster-internal pg_hba block.
// ensureHostAuth uses it as an idempotency key: its presence means the policy is
// already installed, so the block is not appended twice.
const hbaRuleMarker = "# EmberVM scratch-postgres (R4): scram for cluster-internal TCP."

// ensureHostAuth appends the network policy initdb does not set: require scram
// auth for TCP connections from the cluster-internal pod network. The CR is
// cluster-internal, low-stakes scratch tier (D-R4.PR-11.1), so a single scram
// rule for all sources is the policy. IDEMPOTENT: it reads pg_hba.conf first and
// appends the block only when hbaRuleMarker is absent, so it can run on every
// boot (see the call site's rationale) without duplicating the rule on a volume
// that already has it.
func ensureHostAuth(logger *slog.Logger, pgdata string) error {
	hba := filepath.Join(pgdata, "pg_hba.conf")
	existing, err := os.ReadFile(hba)
	if err != nil {
		return fmt.Errorf("read pg_hba.conf: %w", err)
	}
	if strings.Contains(string(existing), hbaRuleMarker) {
		return nil
	}

	// Append: scram over TCP from anywhere (the tap NIC is only reachable from
	// the node Envoy). initdb already wrote a trust local line plus host scram
	// lines for 127.0.0.1/::1; this widens host to all IPv4/IPv6 so the
	// Envoy-forwarded connection (from the pod network) authenticates with scram.
	rule := "\n" + hbaRuleMarker + "\nhost all all 0.0.0.0/0 scram-sha-256\nhost all all ::/0 scram-sha-256\n"
	f, err := os.OpenFile(hba, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("open pg_hba.conf: %w", err)
	}
	defer func() { _ = f.Close() }()
	if _, err := f.WriteString(rule); err != nil {
		return fmt.Errorf("append pg_hba.conf: %w", err)
	}
	logger.Info("stateful postgres: installed cluster-internal pg_hba rule", "pgdata", pgdata)
	return nil
}

// ensureScratchDatabase creates the `scratch` database (idempotent) over the
// local socket as the postgres superuser. Runs once on first boot after the
// server is accepting connections.
func ensureScratchDatabase(logger *slog.Logger) error {
	cmd := exec.Command("createdb", "scratch")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// createdb connects over the trust local socket (in /tmp, matching the
	// server's unix_socket_directories) as the postgres superuser, no password.
	cmd.Env = append(os.Environ(), "PGHOST=/tmp", "PGPORT="+strconv.Itoa(pgPort))
	dropToPostgres(cmd)
	if err := cmd.Run(); err != nil {
		// A pre-existing database (a re-run against an initialized volume where
		// the go-routine still fires) is benign; log and move on.
		logger.Info("stateful postgres: createdb scratch returned (may already exist)", "err", err)
		return nil
	}
	logger.Info("stateful postgres: created scratch database")
	return nil
}

// waitPostgresReady polls a local TCP connect to pgPort until it succeeds or the
// context is cancelled, so the scratch-database creation and the ready flip only
// happen once the server actually accepts connections.
func waitPostgresReady(ctx context.Context, logger *slog.Logger) error {
	deadline := time.Now().Add(55 * time.Second)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(pgPort)), time.Second)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("postgres did not accept TCP within deadline: %w", err)
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// dropToPostgres sets the command's SysProcAttr so it runs as the postgres uid/
// gid rather than root. initdb/postgres both refuse root; this is the drop.
func dropToPostgres(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Credential = &syscall.Credential{Uid: postgresUID, Gid: postgresGID}
}

// pgDataInitialized reports whether PGDATA already holds an initialized cluster.
// PG_VERSION at the PGDATA root is the canonical marker initdb writes last-ish;
// its presence means a prior boot initialized this volume and initdb MUST be
// skipped (running it against a non-empty PGDATA fails and, worse, could wipe
// data). An empty or absent PGDATA means first boot.
func pgDataInitialized(pgdata string) (bool, error) {
	_, err := os.Stat(filepath.Join(pgdata, "PG_VERSION"))
	if err == nil {
		return true, nil
	}
	if os.IsNotExist(err) {
		return false, nil
	}
	return false, err
}

// chownRecursive chowns path and everything under it to uid/gid. Used once to
// hand the freshly mkfs'd volume mount to the postgres uid.
func chownRecursive(path string, uid, gid int) error {
	return filepath.Walk(path, func(name string, _ os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		return os.Chown(name, uid, gid)
	})
}
