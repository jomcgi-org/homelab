#!/usr/bin/env bash
# Run the EmberVM live-Postgres ExUnit module against an ephemeral PostgreSQL
# server on the Linux executor. PostgreSQL is extracted from the same pinned OCI
# fixture used by the monolith integration tests.
#
# Args: $1 mix_test.sh, $2 OTP Install script, $3 Elixir anchor,
#       $4 control mix.exs, $5 output marker, $6 hex.ez, $7 node.pb.ex,
#       $8 rebar3, followed by PostgreSQL fixture files, --hex, then Hex tarballs.
set -euo pipefail

mix_test="$1"
install_script="$2"
elixir_anchor="$3"
mixexs="$4"
out="$5"
hex_ez="$6"
node_pb_ex="$7"
rebar3="$8"
shift 8

postgres_bin=""
pg_isready_bin=""
pg_root=""
hex_tarballs=()
reading_hex=0

for path in "$@"; do
	if [ "$path" = "--hex" ]; then
		reading_hex=1
		continue
	fi

	if [ "$reading_hex" -eq 1 ]; then
		hex_tarballs+=("$path")
		continue
	fi

	case "$path" in
	*/usr/lib/postgresql/16/bin/postgres)
		postgres_bin="$path"
		pg_root="${path%/usr/lib/postgresql/16/bin/postgres}"
		;;
	*/usr/lib/postgresql/16/bin/pg_isready)
		pg_isready_bin="$path"
		;;
	esac
done

if [ -z "$postgres_bin" ] || [ -z "$pg_isready_bin" ] || [ -z "$pg_root" ]; then
	echo "could not locate the PostgreSQL fixture binaries" >&2
	exit 1
fi

case "$pg_root" in
/*) ;;
*) pg_root="$(pwd)/$pg_root" ;;
esac

postgres_bin="$pg_root/usr/lib/postgresql/16/bin/postgres"
pg_isready_bin="$pg_root/usr/lib/postgresql/16/bin/pg_isready"
initdb_bin="${postgres_bin%/postgres}/initdb"
pg_share_src="$pg_root/usr/share/postgresql/16"
pg_lib="$pg_root/usr/lib"
pg_arch_lib="$pg_lib/x86_64-linux-gnu"
pg_internal_lib="$pg_lib/postgresql/16/lib"
dynamic_library_path="$pg_arch_lib:$pg_internal_lib:$pg_lib"

work="$(mktemp -d)"
pg_data="$work/data"
pg_share="$work/share"
pg_log="$work/postgres.log"
port=$((20000 + $$ % 20000))
pg_pid=""

cleanup() {
	if [ -n "$pg_pid" ] && kill -0 "$pg_pid" 2>/dev/null; then
		kill "$pg_pid"
		wait "$pg_pid" || true
	fi
	rm -rf "$work"
}
trap cleanup EXIT

mkdir -p "$pg_data" "$pg_share"
cp -RL "$pg_share_src"/. "$pg_share/"

if [ ! -f "$pg_share/postgresql.conf.sample" ]; then
	printf '%s\n' \
		"listen_addresses = '127.0.0.1'" \
		"max_connections = 100" \
		"shared_buffers = 128MB" \
		"dynamic_shared_memory_type = posix" \
		"log_timezone = 'UTC'" \
		"datestyle = 'iso, mdy'" \
		"timezone = 'UTC'" \
		"lc_messages = 'C'" \
		"lc_monetary = 'C'" \
		"lc_numeric = 'C'" \
		"lc_time = 'C'" \
		"default_text_search_config = 'pg_catalog.english'" \
		>"$pg_share/postgresql.conf.sample"
fi

if [ ! -f "$pg_share/pg_hba.conf.sample" ]; then
	printf '%s\n' \
		"local all all trust" \
		"host all all 127.0.0.1/32 trust" \
		"host all all ::1/128 trust" \
		>"$pg_share/pg_hba.conf.sample"
fi

if [ ! -f "$pg_share/pg_ident.conf.sample" ]; then
	printf '%s\n' "# MAPNAME SYSTEM-USERNAME PG-USERNAME" >"$pg_share/pg_ident.conf.sample"
fi

run_as_postgres_user() {
	if [ "$(id -u)" -eq 0 ]; then
		setpriv --reuid=65534 --regid=65534 --clear-groups "$@"
	else
		"$@"
	fi
}

if [ "$(id -u)" -eq 0 ]; then
	chown -R 65534:65534 "$work"
	chmod -R a+rX "$pg_root"
fi

if ! run_as_postgres_user "$initdb_bin" -D "$pg_data" --no-locale -U test -L "$pg_share" \
	>"$work/initdb.log" 2>&1; then
	echo "PostgreSQL initdb failed" >&2
	cat "$work/initdb.log" >&2
	exit 1
fi

run_as_postgres_user "$postgres_bin" \
	-D "$pg_data" \
	-p "$port" \
	-k "$pg_data" \
	-c "dynamic_library_path=$dynamic_library_path" \
	>"$pg_log" 2>&1 &
pg_pid=$!

ready=0
for _ in $(seq 1 30); do
	if run_as_postgres_user "$pg_isready_bin" -h 127.0.0.1 -p "$port" -U test >/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 0.2
done

if [ "$ready" -ne 1 ]; then
	echo "PostgreSQL did not become ready" >&2
	cat "$pg_log" >&2
	exit 1
fi

export EMBERVM_OPLOG_TEST_DSN="postgres://test:@127.0.0.1:$port/postgres"
export EMBERVM_MIX_TEST_FILE="test/embervm/op_log/postgres_live_test.exs"
export MIX_REBAR3_SRC="$rebar3"

"$mix_test" \
	"$install_script" \
	"$elixir_anchor" \
	"$mixexs" \
	"$out" \
	"$hex_ez" \
	"$node_pb_ex" \
	"${hex_tarballs[@]}"
