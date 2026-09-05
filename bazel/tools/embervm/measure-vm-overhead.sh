#!/usr/bin/env bash
# Measure Firecracker host RSS overhead from EmberVM brick pods.
# Usage: measure-vm-overhead.sh [options]
# Defaults target the homelab hub context and the embervm namespace.
# With no sampling flags, one sample is written as JSON Lines to stdout.
# Use --out PATH to replace PATH with the JSON Lines result.
# Use --interval SECONDS --count N to collect N samples and summaries.
# Each guest line reports total RSS, guest-mapping RSS, and their difference.
# Guest memory is the sum of every */memfile mapping (a guest above 3 GiB maps
# its memfile twice, around the PCI hole); with no memfile, the largest
# anonymous mapping by VMA size is used instead.
# Declared memory is read from CLI/config when exposed by the process.
# Current noded uses its API, so class plus declared_memory_source marks proxy use.
# A successful exec with no Firecracker children emits a guests:0 line.
# Exec failures are diagnostics on stderr and do not stop other bricks.
# Cluster access is read-only: pod listing and cat-only kubectl exec calls.
# Run --self-test to check the smaps parser without kubectl access.
# Requires bash, kubectl, awk, sed, sort, tr, mktemp, and standard core tools.

set -uo pipefail

readonly DEFAULT_CONTEXT="gke_h0melab_europe-west2-a_homelab-hub"
readonly DEFAULT_NAMESPACE="embervm"
readonly FIRECRACKER_COMM="firecracker"

CONTEXT="$DEFAULT_CONTEXT"
NAMESPACE="$DEFAULT_NAMESPACE"
OUT_PATH="-"
INTERVAL=""
COUNT=1
COUNT_SET=0
ONCE_SET=0
SELF_TEST=0
TMP_ROOT=""
METRICS_FILE=""
CLASSES_FILE=""

usage() {
	cat <<'EOF'
Usage: measure-vm-overhead.sh [options]

Options:
  --context CONTEXT    Kubernetes context (default: homelab hub)
  --namespace NS       Kubernetes namespace (default: embervm)
  --out PATH           Write JSON Lines to PATH (default: stdout; - is stdout)
  --once               Take one sample (default)
  --interval SECONDS   Pause SECONDS between repeated samples
  --count N            Take N samples; requires --interval
  --self-test          Run the inline smaps parser test and exit
  -h, --help           Show this help
EOF
}

die_usage() {
	printf 'ERROR: %s\n' "$*" >&2
	usage >&2
	exit 2
}

while (($# > 0)); do
	case "$1" in
	--context)
		(($# >= 2)) || die_usage "--context requires a value"
		CONTEXT="$2"
		shift 2
		;;
	--namespace)
		(($# >= 2)) || die_usage "--namespace requires a value"
		NAMESPACE="$2"
		shift 2
		;;
	--out)
		(($# >= 2)) || die_usage "--out requires a value"
		OUT_PATH="$2"
		shift 2
		;;
	--once)
		ONCE_SET=1
		shift
		;;
	--interval)
		(($# >= 2)) || die_usage "--interval requires a value"
		INTERVAL="$2"
		shift 2
		;;
	--count)
		(($# >= 2)) || die_usage "--count requires a value"
		COUNT="$2"
		COUNT_SET=1
		shift 2
		;;
	--self-test)
		SELF_TEST=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		die_usage "unknown argument: $1"
		;;
	esac
done

if ((COUNT_SET == 1)) && [[ -z "$INTERVAL" ]]; then
	die_usage "--count requires --interval"
fi
if [[ -n "$INTERVAL" ]] && ((COUNT_SET == 0)); then
	die_usage "--interval requires --count"
fi
if [[ -n "$INTERVAL" ]] && ((ONCE_SET == 1)); then
	die_usage "--once cannot be combined with --interval"
fi
if [[ -n "$INTERVAL" && ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
	die_usage "--interval must be a positive integer"
fi
if [[ ! "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
	die_usage "--count must be a positive integer"
fi

# Print total RSS KiB, guest RSS KiB, guest VMA size KiB, and a path or "-".
# Guest memory is the sum over every */memfile mapping; when the process has no
# memfile mapping, the largest anonymous mapping by VMA size stands in for it.
parse_smaps() {
	awk '
		function finish_mapping() {
			if (memfile) {
				memfile_size += map_size
				memfile_rss += map_rss
				if (memfile_path == "") memfile_path = path
			} else if (anonymous && (map_size > anon_size ||
			    (map_size == anon_size && map_rss > anon_rss))) {
				anon_size = map_size
				anon_rss = map_rss
			}
		}
		/^[[:xdigit:]]+-[[:xdigit:]]+[[:space:]]/ {
			finish_mapping()
			path = ""
			if (NF >= 6) {
				path = $6
				for (i = 7; i <= NF; i++) path = path " " $i
			}
			anonymous = ($4 == "00:00" && $5 == "0")
			memfile = (path ~ /(^|\/)memfile([[:space:]]+\(deleted\))?$/)
			map_size = 0
			map_rss = 0
			next
		}
		/^Size:[[:space:]]/ { map_size = $2; next }
		/^Rss:[[:space:]]/ {
			map_rss = $2
			total_rss += $2
			next
		}
		END {
			finish_mapping()
			if (memfile_size > 0) {
				guest_size = memfile_size
				guest_rss = memfile_rss
				guest_path = memfile_path
			} else {
				guest_size = anon_size
				guest_rss = anon_rss
				guest_path = "-"
			}
			printf "%.0f\t%.0f\t%.0f\t%s\n", total_rss, guest_rss, guest_size, guest_path
		}
	'
}

self_test() {
	local parsed total guest_rss guest_size overhead
	parsed=$(
		parse_smaps <<'EOF'
00400000-00420000 r-xp 00000000 08:01 10 /opt/fc/firecracker
Size:                128 kB
Rss:                3000 kB
7f000000-80000000 rw-p 00000000 07:00 655624 /var/lib/embervm/scratch/snapshots/bases/wl__abc/memfile
Size:             262144 kB
Rss:              250000 kB
80000000-a0000000 rw-p c0000000 07:00 655624 /var/lib/embervm/scratch/snapshots/bases/wl__abc/memfile
Size:             524288 kB
Rss:              100000 kB
a0000000-a0001000 rw-p 00000000 00:00 0 [heap]
Size:               4096 kB
Rss:                1000 kB
EOF
	)
	IFS=$'\t' read -r total guest_rss guest_size _guest_path <<<"$parsed"
	overhead=$((total - guest_rss))
	if [[ "$total" != "354000" || "$guest_rss" != "350000" ||
		"$guest_size" != "786432" || "$overhead" != "4000" ]]; then
		printf 'self-test failed: total=%s guest_rss=%s guest_size=%s overhead=%s\n' \
			"$total" "$guest_rss" "$guest_size" "$overhead" >&2
		return 1
	fi
	printf 'self-test passed: total=354000 guest_rss=350000 (two memfile maps) overhead=4000 KiB\n'
}

if ((SELF_TEST == 1)); then
	self_test
	exit $?
fi

cleanup() {
	if [[ -n "$TMP_ROOT" && -d "$TMP_ROOT" ]]; then
		rm -rf -- "$TMP_ROOT"
	fi
}
trap cleanup EXIT

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/measure-vm-overhead.XXXXXX") || {
	printf 'ERROR: could not create a temporary directory\n' >&2
	exit 2
}
METRICS_FILE="$TMP_ROOT/metrics.tsv"
CLASSES_FILE="$TMP_ROOT/classes"
: >"$METRICS_FILE"
: >"$CLASSES_FILE"

if [[ "$OUT_PATH" != "-" ]]; then
	: >"$OUT_PATH" || {
		printf 'ERROR: cannot write output path %s\n' "$OUT_PATH" >&2
		exit 2
	}
fi

emit_line() {
	if [[ "$OUT_PATH" == "-" ]]; then
		printf '%s\n' "$1"
	else
		printf '%s\n' "$1" >>"$OUT_PATH"
	fi
}

kube() {
	kubectl --context "$CONTEXT" --namespace "$NAMESPACE" "$@"
}

report_exec_failure() {
	local pod="$1" operation="$2" error_file="$3" line
	printf 'WARNING: pod %s: kubectl exec failed while %s; skipping\n' \
		"$pod" "$operation" >&2
	while IFS= read -r line; do
		printf '  %s\n' "$line" >&2
	done <"$error_file"
}

class_from_pod() {
	local pod="$1" suffix
	suffix=${pod#*noded-brick-}
	if [[ "$suffix" =~ ^([0-9]+gi)(-|$) ]]; then
		printf '%s\n' "${BASH_REMATCH[1]}"
	else
		printf '%s\n' "${suffix%%-*}"
	fi
}

DECLARED_JSON="null"
DECLARED_SOURCE="pod_class_proxy"

declared_memory_for_pid() {
	local pod="$1" pid="$2" work_dir="$3" guest_path="$4"
	local cmdline_file config_file config_path="" sidecar_path=""
	local arg="" expect_path=0 value=""
	DECLARED_JSON="null"
	DECLARED_SOURCE="pod_class_proxy"
	if [[ "$guest_path" == *" (deleted)" ]]; then
		guest_path=${guest_path% \(deleted\)}
	fi
	case "$guest_path" in
	*/jailer/firecracker/*/root/snapshot/memfile)
		sidecar_path="${guest_path%%/jailer/firecracker/*}/mem_mib"
		;;
	*/memfile)
		sidecar_path="${guest_path%/memfile}/mem_mib"
		;;
	esac
	if [[ -n "$sidecar_path" ]]; then
		config_file="$work_dir/mem_mib"
		if kube exec "$pod" -c noded -- cat "$sidecar_path" \
			>"$config_file" 2>/dev/null; then
			read -r value <"$config_file"
			if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
				DECLARED_JSON="$value"
				DECLARED_SOURCE="mem_mib_sidecar"
				return 0
			fi
		fi
	fi
	cmdline_file="$work_dir/cmdline"
	if ! kube exec "$pod" -c noded -- cat "/proc/$pid/cmdline" \
		>"$cmdline_file" 2>/dev/null; then
		return 0
	fi
	while IFS= read -r arg; do
		if ((expect_path == 1)); then
			config_path="$arg"
			expect_path=0
			continue
		fi
		case "$arg" in
		--mem-size-mib=*)
			value=${arg#*=}
			;;
		--mem-size-mib)
			expect_path=2
			;;
		--config-file=*)
			config_path=${arg#*=}
			;;
		--config-file)
			expect_path=1
			;;
		*)
			if ((expect_path == 2)); then
				value="$arg"
				expect_path=0
			fi
			;;
		esac
	done < <(tr '\000' '\n' <"$cmdline_file")
	if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
		DECLARED_JSON="$value"
		DECLARED_SOURCE="command_line"
		return 0
	fi
	[[ -n "$config_path" ]] || return 0
	config_file="$work_dir/config.json"
	if [[ "$config_path" == /* ]]; then
		config_path="/proc/$pid/root$config_path"
	else
		config_path="/proc/$pid/cwd/$config_path"
	fi
	if ! kube exec "$pod" -c noded -- cat "$config_path" \
		>"$config_file" 2>/dev/null; then
		return 0
	fi
	value=$(tr -d '\n' <"$config_file" |
		sed -n 's/.*"mem_size_mib"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' |
		sed -n '1p')
	if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
		DECLARED_JSON="$value"
		DECLARED_SOURCE="config_file"
	fi
}

emit_guest() {
	local ts="$1" pod="$2" class="$3" pid="$4" comm="$5"
	local total_kib="$6" guest_kib="$7" overhead_kib="$8" line
	line=$(awk \
		-v ts="$ts" -v pod="$pod" -v class="$class" -v pid="$pid" \
		-v declared="$DECLARED_JSON" -v source="$DECLARED_SOURCE" \
		-v total="$total_kib" -v guest="$guest_kib" -v overhead="$overhead_kib" \
		-v comm="$comm" 'BEGIN {
			printf "{\"ts\":\"%s\",\"pod\":\"%s\",\"class\":\"%s\",", ts, pod, class
			printf "\"pid\":%d,\"declared_mib_or_null\":%s,", pid, declared
			printf "\"declared_memory_source\":\"%s\",", source
			printf "\"rss_total_mib\":%.3f,\"guest_mapping_mib\":%.3f,", total / 1024, guest / 1024
			printf "\"overhead_mib\":%.3f,\"comm\":\"%s\"}", overhead / 1024, comm
		}')
	emit_line "$line"
}

sample_once() {
	local sample_number="$1" ts pods_file get_error pod class pod_dir
	local discovery_file exec_error pid comm smaps_file parsed
	local total_kib guest_kib guest_size_kib guest_path overhead_kib discovered emitted
	ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
	pods_file="$TMP_ROOT/pods.$sample_number"
	get_error="$TMP_ROOT/get.$sample_number.err"
	if ! kube get pods -o name >"$pods_file" 2>"$get_error"; then
		while IFS= read -r line; do
			printf '%s\n' "$line" >&2
		done <"$get_error"
		return 1
	fi
	while IFS= read -r pod; do
		pod=${pod#pod/}
		[[ "$pod" == *noded-brick-* ]] || continue
		class=$(class_from_pod "$pod")
		printf '%s\n' "$class" >>"$CLASSES_FILE"
		pod_dir="$TMP_ROOT/$sample_number.$pod"
		mkdir -p "$pod_dir"
		discovery_file="$pod_dir/children"
		exec_error="$pod_dir/exec.err"
		# The variables below belong to the remote shell.
		# shellcheck disable=SC2016
		if ! kube exec "$pod" -c noded -- sh -c '
for pid in $(cat /proc/1/task/*/children 2>/dev/null); do
    comm=$(cat "/proc/$pid/comm" 2>/dev/null) || continue
    if [ "$comm" = "firecracker" ]; then
        printf "%s\t%s\n" "$pid" "$comm"
    fi
done
' >"$discovery_file" 2>"$exec_error"; then
			report_exec_failure "$pod" "listing Firecracker children" "$exec_error"
			continue
		fi
		discovered=0
		emitted=0
		while IFS=$'\t' read -r pid comm; do
			[[ "$pid" =~ ^[0-9]+$ && "$comm" == "$FIRECRACKER_COMM" ]] || continue
			discovered=$((discovered + 1))
			smaps_file="$pod_dir/$pid.smaps"
			exec_error="$pod_dir/$pid.smaps.err"
			if ! kube exec "$pod" -c noded -- cat "/proc/$pid/smaps" \
				>"$smaps_file" 2>"$exec_error"; then
				report_exec_failure "$pod" "reading /proc/$pid/smaps" "$exec_error"
				continue
			fi
			parsed=$(parse_smaps <"$smaps_file")
			IFS=$'\t' read -r total_kib guest_kib guest_size_kib guest_path <<<"$parsed"
			if [[ ! "$total_kib" =~ ^[0-9]+$ || ! "$guest_kib" =~ ^[0-9]+$ ||
				! "$guest_size_kib" =~ ^[1-9][0-9]*$ ]]; then
				printf 'WARNING: pod %s pid %s: no guest mapping found in smaps; skipping\n' \
					"$pod" "$pid" >&2
				continue
			fi
			overhead_kib=$((total_kib - guest_kib))
			declared_memory_for_pid "$pod" "$pid" "$pod_dir" "$guest_path"
			emit_guest "$ts" "$pod" "$class" "$pid" "$comm" \
				"$total_kib" "$guest_kib" "$overhead_kib"
			printf '%s\t%s\n' "$class" "$overhead_kib" >>"$METRICS_FILE"
			emitted=$((emitted + 1))
		done <"$discovery_file"
		if ((discovered == 0)); then
			emit_line "{\"ts\":\"$ts\",\"pod\":\"$pod\",\"class\":\"$class\",\"guests\":0}"
		elif ((emitted == 0)); then
			printf 'WARNING: pod %s: no discovered guest could be sampled\n' "$pod" >&2
		fi
	done <"$pods_file"
}

emit_summaries() {
	local classes_sorted class values_file line
	[[ -s "$CLASSES_FILE" ]] || return 0
	classes_sorted="$TMP_ROOT/classes.sorted"
	LC_ALL=C sort -u "$CLASSES_FILE" >"$classes_sorted"
	while IFS= read -r class; do
		values_file="$TMP_ROOT/values.$class"
		awk -F '\t' -v wanted="$class" '$1 == wanted { print $2 }' \
			"$METRICS_FILE" | LC_ALL=C sort -n >"$values_file"
		if [[ ! -s "$values_file" ]]; then
			emit_line "{\"type\":\"summary\",\"class\":\"$class\",\"sample_count\":0,\"median_overhead_mib\":null,\"p90_overhead_mib\":null,\"suggested_noded.vmOverheadMib\":null}"
			continue
		fi
		line=$(awk -v class="$class" '
			{ values[++count] = $1 }
			END {
				if (count % 2 == 1) {
					median = values[(count + 1) / 2]
				} else {
					median = (values[count / 2] + values[count / 2 + 1]) / 2
				}
				rank = int((9 * count + 9) / 10)
				p90 = values[rank]
				suggested = int((p90 + 16383) / 16384) * 16
				printf "{\"type\":\"summary\",\"class\":\"%s\",", class
				printf "\"sample_count\":%d,\"median_overhead_mib\":%.3f,", count, median / 1024
				printf "\"p90_overhead_mib\":%.3f,", p90 / 1024
				printf "\"suggested_noded.vmOverheadMib\":%d}", suggested
			}
		' "$values_file")
		emit_line "$line"
	done <"$classes_sorted"
}

sample=1
while ((sample <= COUNT)); do
	if ! sample_once "$sample"; then
		exit 1
	fi
	if ((sample < COUNT)); then
		sleep "$INTERVAL"
	fi
	sample=$((sample + 1))
done

if ((COUNT_SET == 1)); then
	emit_summaries
fi
