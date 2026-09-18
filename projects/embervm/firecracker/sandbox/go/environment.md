# go sandbox guest environment

Zero-egress Go execution sandbox (ADR agents/057). One-shot: each request runs
in a fresh microVM restore and nothing persists. No network access at all. Code
runs as uid 65532 with a hard wall-clock timeout that covers COMPILATION as well
as execution; stdout, stderr, and files created in the working directory are
returned to the caller. Save files with a plain relative filename (e.g.
chart.png), never an absolute path or /tmp, or they are not collected.

Your code is written to main.go and run with `go run .`. A minimal go.mod
(module sandbox) is generated for you unless you supply your own, so write a
normal `package main` with a `func main()`.

The standard library is all you get. There is no module fetching: the guest has
no network, and GOPROXY is off and GOTOOLCHAIN is local so an import of a
third-party package fails fast at compile time rather than hanging. Do not
write code that imports anything outside the standard library.

The compiler cache is pre-warmed for the common standard library packages
(bufio, encoding/json, fmt, os, sort, strconv, strings, time), so programs using
those compile fastest. An import outside that set still works, it just pays its
own compile.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| busybox | 1.38.0-r1 |
| ca-certificates-bundle | 20260413-r1 |
| glibc-2.43 | 2.43-r15 |
| glibc-2.43-locale-posix | 2.43-r15 |
| go-1.26 | 1.26.7-r0 |
| ld-linux-2.43 | 2.43-r15 |
| libcrypt1-2.43 | 2.43-r15 |
| libgcc | 16.2.0-r0 |
| libxcrypt | 4.5.2-r4 |
| wolfi-baselayout | 20230201-r29 |
