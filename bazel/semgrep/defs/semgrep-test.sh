#!/usr/bin/env bash
# semgrep-test.sh - Runs semgrep-core Pro directly against source files
#
# Usage: semgrep-test.sh <rule-files...> -- <source-files...> [-- <lockfile-files...>]
#
# The semgrep-core binary is discovered via find(1) in runfiles rather than
# passed as an argument, because Bazel's $(rootpath) can't resolve platform-
# specific select() targets in sh_test args.
#
# Exit code 0 = clean, 1 = findings, 2 = infrastructure failure.
#
# Env: SEMGREP_EXCLUDE_RULES — comma-separated items to skip. Each item is used in two ways:
#      1. Matched against YAML filename (basename without .yaml) to exclude entire config files
#      2. Matched as a suffix against semgrep check_ids to exclude individual rule findings
# Env: SEMGREP_TEST_MODE: unsupported annotation mode fails visibly (status 2).

set -euo pipefail

if [[ $# -lt 2 ]]; then
	echo "Usage: $0 <rule-files...> -- <source-files...>"
	exit 2
fi

# rules_shell places a renamed wrapper in the consuming target's package.
# Shared support stays at its declared runfiles path, not beside that copy.
SEMGREP_SUPPORT="$(dirname "${BASH_SOURCE[0]}")/semgrep-common.sh"
if [[ ! -f "$SEMGREP_SUPPORT" ]]; then
	SEMGREP_SUPPORT="${RUNFILES_DIR:-${TEST_SRCDIR:-.}}/${TEST_WORKSPACE:-_main}/bazel/semgrep/defs/semgrep-common.sh"
fi
if [[ ! -f "$SEMGREP_SUPPORT" ]]; then
	echo "INFRASTRUCTURE: semgrep wrapper support not found in runfiles" >&2
	exit 2
fi
source "$SEMGREP_SUPPORT"
semgrep_setup

# Parse exclude items: filename-based exclusion (EXCLUDE_LIST) and
# rule-ID-based exclusion (EXCLUDE_IDS).
EXCLUDE_LIST=",${SEMGREP_EXCLUDE_RULES:-},"
EXCLUDE_IDS=()
if [[ -n "${SEMGREP_EXCLUDE_RULES:-}" ]]; then
	IFS=',' read -ra _EXCLUDE_ITEMS <<<"$SEMGREP_EXCLUDE_RULES"
	for _item in "${_EXCLUDE_ITEMS[@]}"; do
		_item="${_item## }"
		_item="${_item%% }"
		if [[ -n "$_item" ]]; then
			EXCLUDE_IDS+=("$_item")
		fi
	done
fi

# Collect rule files until we hit the -- separator, skipping excluded rules
RULE_FILES=()
RULE_COUNT=0
while [[ $# -gt 0 && "$1" != "--" ]]; do
	RULE_COUNT=$((RULE_COUNT + 1))
	rule_name="$(basename "$1" .yaml)"
	if [[ "$EXCLUDE_LIST" != *",$rule_name,"* ]]; then
		RULE_FILES+=("$(pwd)/$1")
	fi
	shift
done

if [[ $# -eq 0 ]]; then
	echo "ERROR: missing -- separator between rules and source files"
	exit 2
fi
shift # skip --

if [[ ${#RULE_FILES[@]} -eq 0 ]]; then
	[[ "$RULE_COUNT" -gt 0 ]] || semgrep_error "no rules supplied"
	echo "POLICY: All $RULE_COUNT rule files explicitly excluded; no scan required"
	exit 0
fi

# Collect source files until we hit another -- separator (or end of args)
SOURCE_ARGS=()
while [[ $# -gt 0 && "$1" != "--" ]]; do
	SOURCE_ARGS+=("$1")
	shift
done

# Collect lockfile files after the optional second --
LOCKFILE_ARGS=()
if [[ $# -gt 0 && "$1" == "--" ]]; then
	shift # skip second --
	LOCKFILE_ARGS=("$@")
fi

# Copy source files to a temp directory — semgrep-core needs real file paths
SCAN_DIR="${TEST_TMPDIR}/scan"
mkdir -p "$SCAN_DIR"
SOURCE_FILES=()
for f in ${SOURCE_ARGS[@]+"${SOURCE_ARGS[@]}"}; do
	mkdir -p "$SCAN_DIR/$(dirname "$f")"
	cp "$f" "$SCAN_DIR/$f"
	SOURCE_FILES+=("$SCAN_DIR/$f")
done

# Copy lockfile files to scan directory
LOCKFILE_FILES=()
for f in ${LOCKFILE_ARGS[@]+"${LOCKFILE_ARGS[@]}"}; do
	mkdir -p "$SCAN_DIR/$(dirname "$f")"
	cp "$f" "$SCAN_DIR/$f"
	LOCKFILE_FILES+=("$SCAN_DIR/$f")
done

# Map file extension to semgrep language identifier
detect_lang() {
	case "${1##*.}" in
	py) echo "python" ;;
	go) echo "go" ;;
	js | jsx) echo "javascript" ;;
	ts | tsx) echo "typescript" ;;
	yaml | yml) echo "yaml" ;;
	sh | bash) echo "bash" ;;
	tf) echo "terraform" ;;
	json) echo "json" ;;
	c | h) echo "c" ;;
	cpp | cc | cxx) echo "cpp" ;;
	java) echo "java" ;;
	rb) echo "ruby" ;;
	rs) echo "rust" ;;
	# semgrep-core has no Svelte parser. The repo's Svelte rules are all
	# languages: [generic] regex rules, so map .svelte onto the generic
	# analyzer. Without this the extension falls through to "", the file
	# contributes no language, and a Svelte-only target scans nothing and
	# reports PASSED.
	svelte) echo "generic" ;;
	*) echo "" ;;
	esac
}

# Map lockfile filename to semgrep-core lockfile_kind enum
detect_lockfile_kind() {
	local basename
	basename="$(basename "$1")"
	case "$basename" in
	go.sum) echo "GoModLock" ;;
	requirements*.txt | requirements*.pip) echo "PipRequirementsTxt" ;;
	poetry.lock) echo "PoetryLock" ;;
	Pipfile.lock) echo "PipfileLock" ;;
	uv.lock) echo "UvLock" ;;
	package-lock.json) echo "NpmPackageLockJson" ;;
	yarn.lock) echo "YarnLock" ;;
	pnpm-lock.yaml) echo "PnpmLock" ;;
	*) echo "" ;;
	esac
}

# Build list of unique languages present in source files
_ALL_LANGS=()
for f in ${SOURCE_FILES[@]+"${SOURCE_FILES[@]}"}; do
	lang=$(detect_lang "$f")
	if [[ -n "$lang" ]]; then
		_ALL_LANGS+=("$lang")
	fi
done
# Deduplicate (sort -u is POSIX, no bash 4 associative arrays needed)
SCAN_LANGS=()
if [[ ${#_ALL_LANGS[@]} -gt 0 ]]; then
	while IFS= read -r l; do
		SCAN_LANGS+=("$l")
	done < <(printf '%s\n' "${_ALL_LANGS[@]}" | sort -u)
fi

# --- SAST pass: interfile analysis, per-language ---
# -pro_inter_file requires -lang <lang> <dir> (not -targets).
RESULTS_DIR="${TEST_TMPDIR}/results"
mkdir -p "$RESULTS_DIR"
RESULT_INDEX=0

for lang in ${SCAN_LANGS[@]+"${SCAN_LANGS[@]}"}; do
	for rule_file in "${RULE_FILES[@]}"; do
		RESULT_FILE="$RESULTS_DIR/result_${RESULT_INDEX}.json"
		STDERR_FILE="${TEST_TMPDIR}/stderr_${RESULT_INDEX}.txt"
		semgrep_run_pass "SAST rule=$rule_file lang=$lang" \
			-rules "$rule_file" -pro_inter_file -lang "$lang" "$SCAN_DIR" -json -json_nodots

		RESULT_INDEX=$((RESULT_INDEX + 1))
	done
done

# --- SCA pass: lockfile dependency scanning (when lockfiles present) ---
# Interfile mode doesn't support SCA's dependency_source format, so SCA
# keeps the -targets JSON invocation style.
HAS_LOCKFILES=false
if [[ ${#LOCKFILE_FILES[@]} -gt 0 ]]; then
	HAS_LOCKFILES=true
	# Build dependency_source JSON from first lockfile
	LF="${LOCKFILE_FILES[0]}"
	LF_KIND=$(detect_lockfile_kind "$LF")
	LOCKFILE_JSON=""
	if [[ -n "$LF_KIND" ]]; then
		LF_ABS="$(cd "$(dirname "$LF")" && pwd)/$(basename "$LF")"
		LOCKFILE_JSON=$(printf ',"dependency_source":["LockfileOnly",{"kind":"%s","path":"%s"}]' "$LF_KIND" "$LF_ABS")
	fi

	# Generate targets JSON for SCA scan
	TARGETS_FILE="${TEST_TMPDIR}/sca_targets.json"
	{
		echo -n '["Targets",['
		first=true

		# CodeTargets with SCA product for source files
		for f in ${SOURCE_FILES[@]+"${SOURCE_FILES[@]}"}; do
			lang=$(detect_lang "$f")
			if [[ -z "$lang" ]]; then
				continue
			fi
			abs_path="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
			if [[ "$first" == "true" ]]; then
				first=false
			else
				echo -n ','
			fi
			echo -n "$(printf '["CodeTarget",{"path":{"fpath":"%s","ppath":"%s"},"analyzer":"%s","products":["sca"]%s}]' \
				"$abs_path" "$abs_path" "$lang" "$LOCKFILE_JSON")"
		done

		# DependencySourceTargets for lockfile-only mode (no source files)
		if [[ ${#SOURCE_FILES[@]} -eq 0 ]]; then
			for lf in "${LOCKFILE_FILES[@]}"; do
				lf_kind=$(detect_lockfile_kind "$lf")
				if [[ -z "$lf_kind" ]]; then
					continue
				fi
				lf_abs="$(cd "$(dirname "$lf")" && pwd)/$(basename "$lf")"
				if [[ "$first" == "true" ]]; then
					first=false
				else
					echo -n ','
				fi
				echo -n "$(printf '["DependencySourceTarget",["LockfileOnly",{"kind":"%s","path":"%s"}]]' "$lf_kind" "$lf_abs")"
			done
		fi

		echo ']]'
	} >"$TARGETS_FILE"

	for rule_file in "${RULE_FILES[@]}"; do
		RESULT_FILE="$RESULTS_DIR/result_${RESULT_INDEX}.json"
		STDERR_FILE="${TEST_TMPDIR}/stderr_${RESULT_INDEX}.txt"
		semgrep_run_pass "SCA rule=$rule_file" \
			-rules "$rule_file" -targets "$TARGETS_FILE" -json -json_nodots

		RESULT_INDEX=$((RESULT_INDEX + 1))
	done
fi

# Validate every pass before finding-only filters can change the result.
MERGED_FILE="${TEST_TMPDIR}/results.json"
semgrep_merge

# SCA version filter: semgrep-core does reachability analysis but does not
# compare lockfile versions against the rule's version constraint (that is
# normally pysemgrep's job). Post-filter SCA findings here so we don't
# report CVEs for versions that aren't actually vulnerable.
if [[ "$SCAN_EXIT" -ne 0 && "$HAS_LOCKFILES" == "true" ]]; then
	LOCKFILE_PATHS=""
	for lf in "${LOCKFILE_FILES[@]}"; do
		LOCKFILE_PATHS+="$lf"$'\n'
	done
	RULE_PATHS=""
	for rf in "${RULE_FILES[@]}"; do
		RULE_PATHS+="$rf"$'\n'
	done

	if python3 - "$MERGED_FILE" <<PYEOF; then
import json, os, re, sys

# --- Version comparison (no external dependencies) ---

def parse_version(v):
    """Parse version string into comparable tuple of ints."""
    v = v.strip().lstrip("v")
    # Strip pre-release/build suffixes for basic comparison but keep
    # pre-release ordering: a pre-release sorts before the release.
    pre = None
    for sep in ("-", "+"):
        if sep in v:
            v, pre = v.split(sep, 1)
            break
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return (tuple(parts), 0 if pre is None else -1, pre or "")

def ver_cmp(a, b):
    """Compare two parsed versions. Returns -1, 0, or 1."""
    pa, prea, _ = parse_version(a)
    pb, preb, _ = parse_version(b)
    if pa != pb:
        return -1 if pa < pb else 1
    if prea != preb:
        return -1 if prea < preb else 1
    return 0

def matches_constraint(installed, constraint):
    """Check if installed version satisfies a single constraint like '<0.23.0'."""
    m = re.match(r"(>=|<=|!=|==|>|<)\s*(.+)", constraint.strip())
    if not m:
        return True  # unparseable constraint — keep finding (conservative)
    op, ver = m.group(1), m.group(2)
    c = ver_cmp(installed, ver)
    if op == "==":  return c == 0
    if op == "!=":  return c != 0
    if op == "<":   return c < 0
    if op == "<=":  return c <= 0
    if op == ">":   return c > 0
    if op == ">=":  return c >= 0
    return True

def version_in_range(installed, version_spec):
    """Check if installed version falls within a comma-separated version spec."""
    for part in version_spec.split(","):
        part = part.strip()
        if part and not matches_constraint(installed, part):
            return False
    return True

# --- Lockfile parsing ---

def parse_pip_requirements(path):
    """Parse pip requirements.txt: package==version lines."""
    versions = {}
    with open(path) as f:
        for line in f:
            line = line.strip().split("#")[0].split(";")[0].split("\\\\")[0].strip()
            m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9._-]*)==([^\s;\\\\]+)", line)
            if m:
                # Normalize: pip uses hyphens/underscores interchangeably
                pkg = re.sub(r"[-_.]+", "-", m.group(1)).lower()
                versions[pkg] = m.group(2)
    return versions

def parse_go_sum(path):
    """Parse go.sum: module version hash lines."""
    versions = {}
    with open(path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                mod = parts[0]
                ver = parts[1].split("/")[0]  # strip /go.mod suffix
                ver = ver.lstrip("v")
                if mod not in versions:
                    versions[mod] = ver
    return versions

def parse_pnpm_lock(path):
    """Parse pnpm-lock.yaml for package versions (simplified)."""
    versions = {}
    with open(path) as f:
        for line in f:
            # Matches lines like: '/packagename@version:' or '/@scope/name@version:'
            m = re.match(r"\s+'?/?(@?[^@']+)@([^:('+]+)", line)
            if m:
                pkg = m.group(1).strip("/")
                versions[pkg] = m.group(2)
    return versions

def parse_lockfile(path):
    """Auto-detect lockfile format and parse it."""
    basename = os.path.basename(path)
    if basename == "go.sum":
        return parse_go_sum(path)
    if basename == "pnpm-lock.yaml":
        return parse_pnpm_lock(path)
    # Default: pip requirements format (requirements*.txt, etc.)
    return parse_pip_requirements(path)

# --- Rule constraint extraction ---

def build_rule_constraints(rule_files):
    """Build map of rule_id -> [(package, version_spec)] from rule files."""
    constraints = {}
    for rf in rule_files:
        try:
            with open(rf) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for rule in data.get("rules", []):
            rid = rule.get("id", "")
            if not rid.startswith("ssc-"):
                continue
            dep = rule.get("r2c-internal-project-depends-on", {})
            for entry in dep.get("depends-on-either", []):
                pkg = re.sub(r"[-_.]+", "-", entry.get("package", "")).lower()
                ver = entry.get("version", "")
                if pkg and ver:
                    constraints.setdefault(rid, []).append((pkg, ver))
    return constraints

# --- Main ---

merged_file = sys.argv[1]
lockfile_paths = [p for p in """${LOCKFILE_PATHS}""".strip().splitlines() if p]
rule_paths = [p for p in """${RULE_PATHS}""".strip().splitlines() if p]

# Parse lockfiles
installed = {}
for lf in lockfile_paths:
    installed.update(parse_lockfile(lf))

# Parse SCA rule constraints
constraints = build_rule_constraints(rule_paths)

if not installed or not constraints:
    sys.exit(1)  # can't filter — keep original exit code

# Filter merged results
with open(merged_file) as f:
    data = json.load(f)

kept, dropped = [], 0
for r in data.get("results", []):
    cid = r.get("check_id", "")
    # Extract the ssc- rule ID (may be prefixed with rule file path)
    ssc_match = re.search(r"(ssc-[0-9a-f-]+)", cid)
    if not ssc_match:
        kept.append(r)  # not an SCA finding — keep
        continue
    ssc_id = ssc_match.group(1)
    rule_deps = constraints.get(ssc_id)
    if not rule_deps:
        kept.append(r)  # no constraint info — keep (conservative)
        continue
    vulnerable = False
    for pkg, ver_spec in rule_deps:
        pkg_version = installed.get(pkg)
        if pkg_version and version_in_range(pkg_version, ver_spec):
            vulnerable = True
            break
    if vulnerable:
        kept.append(r)
    else:
        dropped += 1

data["results"] = kept
with open(merged_file, "w") as f:
    json.dump(data, f)

if dropped:
    print(f"  SCA version filter: {dropped} finding(s) not applicable to installed versions")
if kept:
    sys.exit(1)
sys.exit(0)
PYEOF
		SCAN_EXIT=0
	fi
fi

# Upload disabled — semgrep-core scan results are not uploaded to Semgrep App.
# The upload required network access and git CLI, which broke sandbox hermeticity.
# Re-enable when Semgrep App integration is set up.

semgrep_finish
