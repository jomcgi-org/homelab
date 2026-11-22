package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"os"
	"path/filepath"

	"github.com/lib/pq"
)

// The ATE Postgres actor database. The pod certificate signer issues the
// client certificate; the projected volume rotates it in place.
const (
	actorDB  = "postgresql://factory_reaper@postgres.ate-system.svc:5432/atepg?sslmode=verify-full"
	ateCerts = "/var/run/ate"
)

// pemConnector reads the CA bundle and the client credential bundle on each
// new connection and gives them to lib/pq inline. lib/pq refuses a key file
// that is not 0600 unless root owns it. With a pod fsGroup, kubelet adds
// group read to the projected key (0640, owned by the pod user), so a file
// path DSN fails before it connects. The key stays in the pod's own
// read-only volume; only the file-mode check is skipped.
type pemConnector struct {
	dsn string
	dir string
}

func (c pemConnector) Connect(ctx context.Context) (driver.Conn, error) {
	cfg, err := pq.NewConfig(c.dsn)
	if err != nil {
		return nil, err
	}
	ca, err := os.ReadFile(filepath.Join(c.dir, "ca.pem"))
	if err != nil {
		return nil, err
	}
	bundle, err := os.ReadFile(filepath.Join(c.dir, "client.pem"))
	if err != nil {
		return nil, err
	}
	// The credential bundle holds the key and the certificate chain;
	// tls.X509KeyPair takes the CERTIFICATE blocks from one and the key
	// block from the other.
	cfg.SSLInline = true
	cfg.SSLRootCert, cfg.SSLCert, cfg.SSLKey = string(ca), string(bundle), string(bundle)
	conn, err := pq.NewConnectorConfig(cfg)
	if err != nil {
		return nil, err
	}
	return conn.Connect(ctx)
}

func (pemConnector) Driver() driver.Driver { return &pq.Driver{} }

// openActorDB opens the ATE actor database with the pod certificate.
func openActorDB() *sql.DB {
	return sql.OpenDB(pemConnector{dsn: actorDB, dir: ateCerts})
}
