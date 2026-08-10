# ruleid: no-dockerfile
FROM golang:1.25@sha256:fe5d57d3b718e7a4986bae156c2d73f44973bfd313073aed08a4de6692bb6161 AS builder
WORKDIR /workspace
COPY go.mod go.mod
# ruleid: no-dockerfile
FROM gcr.io/distroless/static:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6
COPY --from=builder /workspace/manager .
