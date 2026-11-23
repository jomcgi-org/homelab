#!/usr/bin/env bash
# Regenerate ateapi.pb.go and ateapi_grpc.pb.go from ateapi.proto.
# Needs protoc (25 or later), protoc-gen-go v1.36.12 and protoc-gen-go-grpc
# v1.5.1 on PATH: both plugins build from the versions in the repo go.mod
# (go build google.golang.org/protobuf/cmd/protoc-gen-go and
# google.golang.org/grpc/cmd/protoc-gen-go-grpc).
set -euo pipefail
cd "$(dirname "$0")"
protoc \
	--go_out=. --go_opt=paths=source_relative \
	--go-grpc_out=. --go-grpc_opt=paths=source_relative \
	ateapi.proto
