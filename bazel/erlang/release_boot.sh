#!/usr/bin/env bash
# Smoke test: prove the built OTP release actually BOOTS and serves /healthz on
# the executor, using EXTERNAL erlang exactly like the deployed pod does
# (include_erts:false, RELEASE_DISTRIBUTION=none, RELEASE_TMP writable). This is
# the last runtime unknown the earlier smokes did not cover (they proved compile,
# ExUnit, and release BUILD; this proves the release RUNS and answers HTTP).
#
# Args: $1 OTP Install script, $2 release tar (opt/embervm/...), $3 output marker.
set -euo pipefail

install_script="$1"
release_tar="$2"
out="$3"

# Absolutize inputs/outputs before any cd.
case "$out" in /*) ;; *) out="$(pwd)/$out" ;; esac
case "$release_tar" in /*) ;; *) release_tar="$(pwd)/$release_tar" ;; esac

otp_src="$(cd "$(dirname "$install_script")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# External erlang, matching the image's Wolfi erlang-27 (same OTP version).
cp -RL "$otp_src"/. "$work/otp/"
"$work/otp/Install" -minimal "$work/otp" >/dev/null

# Extract the release (tar is rooted at opt/embervm/).
mkdir -p "$work/app"
tar -xf "$release_tar" -C "$work/app"

export PATH="$work/otp/bin:$PATH"
export HOME="$work"
export RELEASE_TMP="$work/reltmp"
export RELEASE_DISTRIBUTION=none
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export EMBERVM_HTTP_PORT=8080
mkdir -p "$RELEASE_TMP"

# `start` runs the release in the foreground (Elixir convention); background it
# with the shell so we can probe, then tear it down.
"$work/app/opt/embervm/bin/embervm" start &
app_pid=$!

# Poll /healthz over a raw bash /dev/tcp socket (no curl dependency on the
# executor). 200 => the supervision tree booted and the listener is serving.
code=""
for _ in $(seq 1 30); do
	if resp="$( (exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /healthz HTTP/1.0\r\nhost: localhost\r\n\r\n' >&3 && cat <&3) 2>/dev/null)"; then
		code="$(printf '%s' "$resp" | head -1 | awk '{print $2}')"
		[ "$code" = "200" ] && break
	fi
	sleep 1
done

kill "$app_pid" 2>/dev/null || true
"$work/app/opt/embervm/bin/embervm" stop 2>/dev/null || true

printf 'healthz_status=%s\n' "${code:-none}" | tee "$out"
if [ "$code" != "200" ]; then
	echo "RELEASE BOOT FAILED: /healthz did not return 200 (got '${code:-none}')" >&2
	exit 1
fi
echo "RELEASE BOOT OK: release booted and /healthz returned 200 on the executor" >&2
