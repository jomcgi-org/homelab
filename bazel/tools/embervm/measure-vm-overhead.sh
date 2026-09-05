#!/usr/bin/env bash
# Measure Firecracker host RSS and PSS overhead from EmberVM brick pods.
# Usage: measure-vm-overhead.sh [options]
# Defaults target the homelab hub context and the embervm namespace.
# With no sampling flags, one sample is written as JSON Lines to stdout.
# Use --out PATH to replace PATH with the JSON Lines result.
# Use --interval SECONDS --count N to collect N samples and summaries.
# Each guest line reports total RSS and PSS, guest-mapping RSS and PSS, the
# guest mapping's VMA size, and both overhead differences.
# Guest memory is the sum of every */memfile mapping (a guest above 3 GiB maps
# its memfile twice, around the PCI hole); with no memfile, the largest
# anonymous mapping by VMA size is used instead.
# Declared memory is read from mem_mib beside a directly mapped memfile.
# Jailed memfile paths are reported as unsupported. noded.jailer.enabled is false
# today, and the jailed path does not identify the snapshot bundle holding mem_mib.
# A successful exec with no Firecracker children emits a guests:0 line.
# Exec failures are diagnostics on stderr and do not stop other bricks.
# Cluster access is read-only: pod get, a sh -c discovery loop that reads
# /proc/1/task/*/children and comm files, and cat-only kubectl exec reads.
# Run --self-test to check the smaps parser without kubectl access.
# Requires bash, kubectl, awk, sort, mktemp, and standard core tools.

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

# Print total RSS KiB, total PSS KiB, guest RSS KiB, guest PSS KiB, guest VMA
# size KiB, and a path or "-".
# Guest memory is the sum over every */memfile mapping; when the process has no
# memfile mapping, the largest anonymous mapping by VMA size stands in for it.
parse_smaps() {
	awk '
		function finish_mapping() {
			if (memfile) {
				memfile_size += map_size
				memfile_rss += map_rss
				memfile_pss += map_pss
				if (memfile_path == "") memfile_path = path
			} else if (anonymous && (map_size > anon_size ||
			    (map_size == anon_size && map_rss > anon_rss))) {
				anon_size = map_size
				anon_rss = map_rss
				anon_pss = map_pss
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
			map_pss = 0
			next
		}
		/^Size:[[:space:]]/ { map_size = $2; next }
		/^Rss:[[:space:]]/ {
			map_rss = $2
			total_rss += $2
			next
		}
		/^Pss:[[:space:]]/ {
			map_pss = $2
			total_pss += $2
			next
		}
		END {
			finish_mapping()
			if (memfile_size > 0) {
				guest_size = memfile_size
				guest_rss = memfile_rss
				guest_pss = memfile_pss
				guest_path = memfile_path
			} else {
				guest_size = anon_size
				guest_rss = anon_rss
				guest_pss = anon_pss
				guest_path = "-"
			}
			printf "%.0f\t%.0f\t%.0f\t%.0f\t%.0f\t%s\n", \
			    total_rss, total_pss, guest_rss, guest_pss, guest_size, guest_path
		}
	'
}

self_test() {
	local parsed total_rss total_pss guest_rss guest_pss guest_size
	local overhead_rss overhead_pss
	parsed=$(
		parse_smaps <<'EOF'
00400000-00420000 r-xp 00000000 08:01 10 /opt/fc/firecracker
Size:                128 kB
Rss:                3000 kB
Pss:                1000 kB
7f000000-80000000 rw-p 00000000 07:00 655624 /var/lib/embervm/scratch/snapshots/bases/wl__abc/memfile
Size:             262144 kB
Rss:              250000 kB
Pss:              249000 kB
80000000-a0000000 rw-p c0000000 07:00 655624 /var/lib/embervm/scratch/snapshots/bases/wl__abc/memfile
Size:             524288 kB
Rss:              100000 kB
Pss:               99000 kB
a0000000-a0001000 rw-p 00000000 00:00 0 [heap]
Size:               4096 kB
Rss:                1000 kB
Pss:                1000 kB
EOF
	)
	IFS=$'\t' read -r total_rss total_pss guest_rss guest_pss guest_size \
		_guest_path <<<"$parsed"
	overhead_rss=$((total_rss - guest_rss))
	overhead_pss=$((total_pss - guest_pss))
	if [[ "$total_rss" != "354000" || "$total_pss" != "350000" ||
		"$guest_rss" != "350000" || "$guest_pss" != "348000" ||
		"$guest_size" != "786432" || "$overhead_rss" != "4000" ||
		"$overhead_pss" != "2000" ]]; then
		printf 'self-test failed: total_rss=%s total_pss=%s guest_rss=%s guest_pss=%s guest_size=%s overhead_rss=%s overhead_pss=%s\n' \
			"$total_rss" "$total_pss" "$guest_rss" "$guest_pss" \
			"$guest_size" "$overhead_rss" "$overhead_pss" >&2
		return 1
	fi
	printf 'self-test passed: RSS overhead=4000 KiB PSS overhead=2000 KiB (two memfile maps)\n'
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
DECLARED_SOURCE="unavailable"

declared_memory_for_pid() {
	local pod="$1" work_dir="$2" guest_path="$3"
	local sidecar_file sidecar_path="" value=""
	DECLARED_JSON="null"
	DECLARED_SOURCE="unavailable"
	if [[ "$guest_path" == *" (deleted)" ]]; then
		guest_path=${guest_path% \(deleted\)}
	fi
	case "$guest_path" in
	*/jailer/firecracker/*)
		DECLARED_SOURCE="jailed_layout_unsupported"
		return 0
		;;
	*/memfile)
		sidecar_path="${guest_path%/memfile}/mem_mib"
		;;
	esac
	if [[ -n "$sidecar_path" ]]; then
		sidecar_file="$work_dir/mem_mib"
		if kube exec "$pod" -c noded -- cat "$sidecar_path" \
			>"$sidecar_file" 2>/dev/null; then
			read -r value <"$sidecar_file"
			if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
				DECLARED_JSON="$value"
				DECLARED_SOURCE="mem_mib_sidecar"
			fi
		fi
	fi
}

emit_guest() {
	local ts="$1" pod="$2" class="$3" pid="$4" comm="$5"
	local total_rss_kib="$6" total_pss_kib="$7" guest_rss_kib="$8"
	local guest_pss_kib="$9" guest_size_kib="${10}" overhead_rss_kib="${11}"
	local overhead_pss_kib="${12}" line
	line=$(awk \
		-v ts="$ts" -v pod="$pod" -v class="$class" -v pid="$pid" \
		-v declared="$DECLARED_JSON" -v source="$DECLARED_SOURCE" \
		-v total_rss="$total_rss_kib" -v total_pss="$total_pss_kib" \
		-v guest_rss="$guest_rss_kib" -v guest_pss="$guest_pss_kib" \
		-v guest_size="$guest_size_kib" -v overhead_rss="$overhead_rss_kib" \
		-v overhead_pss="$overhead_pss_kib" \
		-v comm="$comm" 'BEGIN {
			printf "{\"ts\":\"%s\",\"pod\":\"%s\",\"class\":\"%s\",", ts, pod, class
			printf "\"pid\":%d,\"declared_mib_or_null\":%s,", pid, declared
			printf "\"declared_memory_source\":\"%s\",", source
			printf "\"rss_total_mib\":%.3f,\"pss_total_mib\":%.3f,", total_rss / 1024, total_pss / 1024
			printf "\"guest_mapping_rss_mib\":%.3f,", guest_rss / 1024
			printf "\"guest_mapping_pss_mib\":%.3f,", guest_pss / 1024
			printf "\"guest_mapping_size_mib\":%.3f,", guest_size / 1024
			printf "\"overhead_rss_mib\":%.3f,", overhead_rss / 1024
			printf "\"overhead_pss_mib\":%.3f,\"comm\":\"%s\"}", overhead_pss / 1024, comm
		}')
	emit_line "$line"
}

sample_once() {
	local sample_number="$1" ts pods_file get_error pod class deletion_timestamp pod_dir
	local discovery_file exec_error pid comm smaps_file parsed
	local total_rss_kib total_pss_kib guest_rss_kib guest_pss_kib guest_size_kib
	local guest_path overhead_rss_kib overhead_pss_kib discovered emitted
	ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
	pods_file="$TMP_ROOT/pods.$sample_number"
	get_error="$TMP_ROOT/get.$sample_number.err"
	if ! kube get pods \
		--selector "app.kubernetes.io/component=noded-brick" \
		--field-selector "status.phase=Running" \
		-o 'jsonpath={range .items[*]}{.metadata.name}{"|"}{.metadata.labels.embervm\.jomcgi\.dev/size-class}{"|"}{.metadata.deletionTimestamp}{"\n"}{end}' \
		>"$pods_file" 2>"$get_error"; then
		while IFS= read -r line; do
			printf '%s\n' "$line" >&2
		done <"$get_error"
		return 1
	fi
	while IFS='|' read -r pod class deletion_timestamp; do
		[[ -n "$pod" && -z "$deletion_timestamp" ]] || continue
		if [[ -z "$class" ]]; then
			class=$(class_from_pod "$pod")
		fi
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
			IFS=$'\t' read -r total_rss_kib total_pss_kib guest_rss_kib \
				guest_pss_kib guest_size_kib guest_path <<<"$parsed"
			if [[ ! "$total_rss_kib" =~ ^[0-9]+$ || ! "$total_pss_kib" =~ ^[0-9]+$ ||
				! "$guest_rss_kib" =~ ^[0-9]+$ || ! "$guest_pss_kib" =~ ^[0-9]+$ ||
				! "$guest_size_kib" =~ ^[1-9][0-9]*$ ]]; then
				printf 'WARNING: pod %s pid %s: no guest mapping found in smaps; skipping\n' \
					"$pod" "$pid" >&2
				continue
			fi
			overhead_rss_kib=$((total_rss_kib - guest_rss_kib))
			overhead_pss_kib=$((total_pss_kib - guest_pss_kib))
			declared_memory_for_pid "$pod" "$pod_dir" "$guest_path"
			emit_guest "$ts" "$pod" "$class" "$pid" "$comm" \
				"$total_rss_kib" "$total_pss_kib" "$guest_rss_kib" \
				"$guest_pss_kib" "$guest_size_kib" "$overhead_rss_kib" \
				"$overhead_pss_kib"
			printf '%s\t%s\n' "$class" "$overhead_pss_kib" >>"$METRICS_FILE"
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
			emit_line "{\"type\":\"summary\",\"class\":\"$class\",\"sample_count\":0,\"median_overhead_pss_mib\":null,\"p90_overhead_pss_mib\":null,\"suggested_noded.vmOverheadMib\":null}"
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
				printf "\"sample_count\":%d,\"median_overhead_pss_mib\":%.3f,", count, median / 1024
				printf "\"p90_overhead_pss_mib\":%.3f,", p90 / 1024
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
