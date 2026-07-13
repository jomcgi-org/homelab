#!/usr/bin/env bash
# EmberVM toolchain feasibility probe.
#
# Runs as a `bazel test` action, i.e. ON the BuildBuddy RBE executor (the same
# place a from-source OTP+Elixir build action would run). It reports which
# Erlang/OTP-27 build prerequisites the stock executor already provides, so the
# from-source toolchain (Task 1) is designed against measured facts instead of
# guesses about the executor image.
#
# Required for a from-source OTP build WITH crypto/ssl (the real control plane
# needs sha256 + TLS): C compiler, GNU make, perl, and the OpenSSL + ncurses
# development headers. Optional apps (wx/odbc/jinterface/megaco) are dropped at
# ./configure time, so their deps are not probed.
#
# Green  => the executor can build OTP from source as-is (OCaml-style pattern
#           applies directly). Red => the missing headers must be provisioned
#           (openssl/ncurses staged as hermetic inputs), a complexity jump that
#           feeds back into the Elixir-vs-Go decision.
set -uo pipefail

missing=0

report() {
  # $1 = human label, $2 = "ok"/"MISSING", $3 = detail
  printf '  %-28s %-8s %s\n' "$1" "$2" "$3"
}

check_bin() {
  local bin="$1"
  if command -v "$bin" >/dev/null 2>&1; then
    report "$bin" "ok" "$(command -v "$bin")"
  else
    report "$bin" "MISSING" ""
    missing=$((missing + 1))
  fi
}

check_header() {
  local label="$1"
  shift
  for path in "$@"; do
    if [ -f "$path" ]; then
      report "$label" "ok" "$path"
      return 0
    fi
  done
  report "$label" "MISSING" "searched: $*"
  missing=$((missing + 1))
}

echo "== EmberVM from-source OTP build prerequisite probe =="
echo "-- executor --"
report "uname" "" "$(uname -a)"
if [ -f /etc/os-release ]; then
  report "os" "" "$(. /etc/os-release && echo "$PRETTY_NAME")"
fi

echo "-- required binaries --"
check_bin cc
check_bin gcc
check_bin make
check_bin perl
check_bin m4
check_bin autoconf
check_bin tar

echo "-- required development headers --"
check_header "openssl (opensslv.h)" \
  /usr/include/openssl/opensslv.h \
  /usr/local/include/openssl/opensslv.h
check_header "ncurses (curses.h)" \
  /usr/include/curses.h \
  /usr/include/ncurses.h \
  /usr/include/ncursesw/curses.h
check_header "zlib (zlib.h)" \
  /usr/include/zlib.h

echo "-- summary --"
if [ "$missing" -eq 0 ]; then
  echo "ALL PREREQUISITES PRESENT: from-source OTP build is feasible on the executor as-is."
  exit 0
fi
echo "MISSING $missing prerequisite(s): the from-source toolchain must provision them as hermetic inputs."
exit 1
