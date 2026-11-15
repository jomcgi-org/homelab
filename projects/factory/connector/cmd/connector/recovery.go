package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const recoveryGuidance = "Confirmation is an operator attestation, not a verified kill. Fence every possible execution copy, including copies under historical principals, and quiesce retired or in-flight dispatchers as well as runtimes. Keep old executions and dispatchers fenced; consumption does not authorize resuming them."

// recoveryRequest is parsed completely before opening the operator database.
type recoveryRequest struct {
	issue, fingerprint, action, by, evidence string
	id                                       int64
}

func parseRecoveryArgs(args []string) (recoveryRequest, error) {
	var req recoveryRequest
	if len(args) == 2 && args[0] == "inspect" && strings.TrimSpace(args[1]) != "" {
		req.issue = strings.TrimSpace(args[1])
		return req, nil
	}
	if len(args) < 2 || args[0] != "confirm-fenced" {
		return req, errors.New("usage: connector recovery inspect ISSUE | confirm-fenced HOLD_ID --fingerprint HASH --action retry|cancel --by NAME --evidence TEXT --confirm-all-copies-fenced")
	}
	var err error
	if req.id, err = strconv.ParseInt(args[1], 10, 64); err != nil || req.id <= 0 {
		return req, errors.New("recovery hold ID must be a positive integer")
	}
	flags := flag.NewFlagSet("recovery confirm-fenced", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&req.fingerprint, "fingerprint", "", "exact fingerprint from inspect")
	flags.StringVar(&req.action, "action", "", "retry or cancel")
	flags.StringVar(&req.by, "by", "", "operator identity")
	flags.StringVar(&req.evidence, "evidence", "", "fencing evidence")
	confirmed := flags.Bool("confirm-all-copies-fenced", false, "attest all copies and dispatchers are fenced")
	if err := flags.Parse(args[2:]); err != nil {
		return req, err
	}
	digest, digestErr := hex.DecodeString(req.fingerprint)
	if flags.NArg() != 0 || !*confirmed || digestErr != nil || len(digest) != 32 || req.fingerprint != strings.ToLower(req.fingerprint) ||
		(req.action != "retry" && req.action != "cancel") || strings.TrimSpace(req.by) == "" || strings.TrimSpace(req.evidence) == "" {
		return req, errors.New("confirmation requires the exact fingerprint, retry|cancel action, operator identity, fencing evidence, and --confirm-all-copies-fenced")
	}
	return req, nil
}

type recoveryStore interface {
	InspectRecoveries(context.Context, string) ([]*store.Recovery, error)
	AttestRecovery(context.Context, int64, string, string, string, string) error
}

type recoveryOpener func() (recoveryStore, func(), error)

func recoveryCommand(ctx context.Context, args []string, out io.Writer, open recoveryOpener) error {
	req, err := parseRecoveryArgs(args)
	if err != nil {
		return err
	}
	st, close, err := open()
	if err != nil {
		return err
	}
	defer close()
	if req.issue != "" {
		holds, err := st.InspectRecoveries(ctx, req.issue)
		if err != nil {
			return err
		}
		if holds == nil {
			holds = []*store.Recovery{}
		}
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		return encoder.Encode(struct {
			Guidance string            `json:"guidance"`
			Holds    []*store.Recovery `json:"holds"`
		}{recoveryGuidance, holds})
	}
	if err := st.AttestRecovery(ctx, req.id, req.fingerprint, req.action, req.by, req.evidence); err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "Recorded operator fencing attestation for hold %d. This command executed no recovery action; the connector must revalidate the attestation before consuming it.\n", req.id)
	return err
}

func recoveryCLI(args []string) error {
	return recoveryCommand(context.Background(), args, os.Stdout, func() (recoveryStore, func(), error) {
		st, err := operatorStore()
		if err != nil {
			return nil, nil, err
		}
		return st, func() { _ = st.DB.Close() }, nil
	})
}
