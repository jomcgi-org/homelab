// Package shotter holds Go-visible artifacts colocated with this guest
// image's build (apko.yaml, etc/). Today that is only the baked egress
// destination policy, embedded here so a Go test in the guest-init package
// can assert the checked-in file actually parses and validates, rather than
// only ever exercising a synthetic fixture with the same shape.
package shotter

import _ "embed"

// BakedEgressConfigJSON is the exact contents of etc/shotter-egress.json,
// the destination policy BUILD bakes into the guest image at
// /etc/shotter-egress.json (see proxy.go's shotterEgressConfigPath). A
// malformed or invalid real file would otherwise be green in CI forever:
// proxy_test.go's own fixtures are synthetic JSON it writes itself, and
// never touch this one.
//
//go:embed etc/shotter-egress.json
var BakedEgressConfigJSON []byte
