package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestRecoveryArguments(t *testing.T) {
	confirm := recoveryConfirmation("retry")
	for _, args := range [][]string{{"inspect", "HL-1"}, confirm} {
		if _, err := parseRecoveryArgs(args); err != nil {
			t.Fatalf("valid command %v: %v", args, err)
		}
	}
	invalid := map[string][]string{
		"empty":                  nil,
		"unknown":                {"resume", "HL-1"},
		"missing issue":          {"inspect"},
		"empty issue":            {"inspect", " "},
		"extra inspect argument": {"inspect", "HL-1", "extra"},
		"missing ID":             {"confirm-fenced"},
		"missing flags":          {"confirm-fenced", "7"},
		"missing confirmation":   confirm[:len(confirm)-1],
		"extra argument":         append(append([]string{}, confirm...), "extra"),
	}
	for flag, value := range map[string]string{"ID": "0", "--fingerprint": "short", "--action": "resume", "--by": " ", "--evidence": " "} {
		args := append([]string{}, confirm...)
		for i := range args {
			if args[i] == flag {
				args[i+1] = value
			}
		}
		if flag == "ID" {
			args[1] = value
		}
		invalid[flag] = args
	}
	for name, args := range invalid {
		t.Run(name, func(t *testing.T) {
			if _, err := parseRecoveryArgs(args); err == nil {
				t.Fatalf("invalid command accepted: %v", args)
			}
		})
	}
}

func recoveryConfirmation(action string) []string {
	return []string{"confirm-fenced", "7", "--fingerprint", strings.Repeat("a", 64), "--action", action, "--by", "Joe", "--evidence", "node power-off verified", "--confirm-all-copies-fenced"}
}

type recoveryFake struct {
	issue    string
	attested recoveryRequest
	err      error
}

func (f *recoveryFake) InspectRecoveries(_ context.Context, issue string) ([]*store.Recovery, error) {
	f.issue = issue
	return []*store.Recovery{{ID: 7, Fingerprint: strings.Repeat("a", 64), State: "consumed"}}, f.err
}

func (f *recoveryFake) AttestRecovery(_ context.Context, id int64, fingerprint, action, by, evidence string) error {
	f.attested = recoveryRequest{id: id, fingerprint: fingerprint, action: action, by: by, evidence: evidence}
	return f.err
}

func TestRecoveryCommands(t *testing.T) {
	for _, action := range []string{"inspect", "retry", "cancel", "stale", "invalid"} {
		t.Run(action, func(t *testing.T) {
			fake := &recoveryFake{}
			args := recoveryConfirmation(action)
			switch action {
			case "inspect":
				args = []string{"inspect", "HL-1"}
			case "stale":
				args, fake.err = recoveryConfirmation("retry"), errors.New("stale recovery fingerprint")
			case "invalid":
				args = []string{"resume", "HL-1"}
			}
			opened, closed := 0, 0
			var out bytes.Buffer
			err := recoveryCommand(context.Background(), args, &out, func() (recoveryStore, func(), error) {
				opened++
				return fake, func() { closed++ }, nil
			})
			if action == "invalid" {
				if err == nil || opened != 0 {
					t.Fatal("invalid syntax opened the operator database")
				}
				return
			}
			if opened != 1 || closed != 1 {
				t.Fatal("operator database lifetime was not bounded")
			}
			if action == "stale" {
				if !errors.Is(err, fake.err) || out.Len() != 0 {
					t.Fatal("stale attestation was reported successful")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if action == "inspect" {
				var result struct {
					Guidance string
					Holds    []store.Recovery
				}
				if err := json.Unmarshal(out.Bytes(), &result); err != nil {
					t.Fatal(err)
				}
				if fake.issue != "HL-1" || fake.attested.id != 0 || len(result.Holds) != 1 || result.Holds[0].State != "consumed" || !strings.Contains(result.Guidance, "historical principals") || !strings.Contains(result.Guidance, "dispatchers") {
					t.Fatalf("inspection lost history or fencing scope: %s", out.String())
				}
			} else if got := fake.attested; got.id != 7 || got.action != action || got.fingerprint != strings.Repeat("a", 64) || got.by != "Joe" || got.evidence != "node power-off verified" || fake.issue != "" || !strings.Contains(out.String(), "executed no recovery action") {
				t.Fatalf("confirmation did not only attest exact request: %+v output=%s", got, out.String())
			}
		})
	}
}
