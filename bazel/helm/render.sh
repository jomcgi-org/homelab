#!/usr/bin/env bash
# Render one repository Helm chart with the Helm CLI from PATH.

set -euo pipefail

usage() {
	cat <<'EOF'
Usage: render.sh [options] SERVICE_OR_CHART

Render one Helm chart to manifests/all.yaml below the chart directory.

Arguments:
  SERVICE_OR_CHART  Service name (for example monolith or
                    mcp/context-forge-gateway), or a chart directory path

Options:
  --release NAME       Helm release name (default: Chart.yaml name)
  --namespace NAME     Kubernetes namespace (default: release name)
  --values PATH        Values file, repeatable; resolved from the repository
                       root first, then from the chart directory
  --output PATH        Output file relative to the repository root, or - for
                       stdout (default: CHART/manifests/all.yaml)
  -h, --help           Show this help
EOF
}

die() {
	printf 'render: %s\n' "$*" >&2
	exit 1
}

RELEASE_NAME=""
NAMESPACE=""
OUTPUT_FILE=""
CHART_ARG=""
VALUES_FILES=()

while [[ $# -gt 0 ]]; do
	case "$1" in
	--release)
		[[ $# -ge 2 ]] || {
			printf 'render: --release requires a value\n' >&2
			exit 2
		}
		RELEASE_NAME="$2"
		shift 2
		;;
	--namespace)
		[[ $# -ge 2 ]] || {
			printf 'render: --namespace requires a value\n' >&2
			exit 2
		}
		NAMESPACE="$2"
		shift 2
		;;
	--values)
		[[ $# -ge 2 ]] || {
			printf 'render: --values requires a path\n' >&2
			exit 2
		}
		VALUES_FILES+=("$2")
		shift 2
		;;
	--output)
		[[ $# -ge 2 ]] || {
			printf 'render: --output requires a path\n' >&2
			exit 2
		}
		OUTPUT_FILE="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	--*)
		printf 'render: unknown option: %s\n' "$1" >&2
		usage >&2
		exit 2
		;;
	*)
		if [[ -n "$CHART_ARG" ]]; then
			printf 'render: expected one service or chart, got: %s\n' "$1" >&2
			exit 2
		fi
		CHART_ARG="$1"
		shift
		;;
	esac
done

if [[ -z "$CHART_ARG" ]]; then
	usage >&2
	exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" ||
	die "not inside a git work tree"
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"

resolve_chart() {
	local candidate
	for candidate in \
		"$CHART_ARG" \
		"$REPO_ROOT/$CHART_ARG" \
		"$REPO_ROOT/projects/$CHART_ARG/chart" \
		"$REPO_ROOT/projects/$CHART_ARG/deploy" \
		"$REPO_ROOT/projects/$CHART_ARG"; do
		if [[ -f "$candidate/Chart.yaml" ]]; then
			(cd "$candidate" && pwd -P)
			return 0
		fi
	done
	return 1
}

CHART_DIR="$(resolve_chart)" ||
	die "cannot find Chart.yaml for '$CHART_ARG'"
case "$CHART_DIR/" in
"$REPO_ROOT/"*) ;;
*) die "chart must be inside the repository: $CHART_DIR" ;;
esac

if [[ -z "$RELEASE_NAME" ]]; then
	RELEASE_NAME="$(
		sed -n '/^name:[[:space:]]*/ {
			s/^name:[[:space:]]*//
			p
			q
		}' "$CHART_DIR/Chart.yaml" | tr -d "\"'"
	)"
	[[ -n "$RELEASE_NAME" ]] || die "Chart.yaml has no top-level name"
fi
[[ -n "$NAMESPACE" ]] || NAMESPACE="$RELEASE_NAME"

HELM_BIN="${HELM:-helm}"
command -v "$HELM_BIN" >/dev/null 2>&1 ||
	die "helm not found; run ./bootstrap.sh and ensure the extracted tools are on PATH"

HELM_ARGS=(template "$RELEASE_NAME" "$CHART_DIR" --namespace "$NAMESPACE")
if [[ ${#VALUES_FILES[@]} -gt 0 ]]; then
	for values_file in "${VALUES_FILES[@]}"; do
		if [[ "$values_file" = /* && -f "$values_file" ]]; then
			resolved_values="$values_file"
		elif [[ -f "$REPO_ROOT/$values_file" ]]; then
			resolved_values="$REPO_ROOT/$values_file"
		elif [[ -f "$CHART_DIR/$values_file" ]]; then
			resolved_values="$CHART_DIR/$values_file"
		else
			die "values file not found: $values_file"
		fi
		HELM_ARGS+=(--values "$resolved_values")
	done
fi

CHART_REL="${CHART_DIR#"$REPO_ROOT"/}"
if [[ -z "$OUTPUT_FILE" ]]; then
	OUTPUT_PATH="$CHART_DIR/manifests/all.yaml"
	OUTPUT_LABEL="$CHART_REL/manifests/all.yaml"
elif [[ "$OUTPUT_FILE" == "-" ]]; then
	OUTPUT_PATH="-"
	OUTPUT_LABEL="stdout"
elif [[ "$OUTPUT_FILE" = /* ]]; then
	OUTPUT_PATH="$OUTPUT_FILE"
	OUTPUT_LABEL="$OUTPUT_FILE"
else
	OUTPUT_PATH="$REPO_ROOT/$OUTPUT_FILE"
	OUTPUT_LABEL="$OUTPUT_FILE"
fi

printf 'Rendering %s as release %s in namespace %s\n' \
	"$CHART_REL" "$RELEASE_NAME" "$NAMESPACE" >&2

if [[ "$OUTPUT_PATH" == "-" ]]; then
	"$HELM_BIN" "${HELM_ARGS[@]}"
	printf 'Rendered manifests to stdout\n' >&2
	exit 0
fi

case "$OUTPUT_PATH" in
"$REPO_ROOT"/*) ;;
*) die "output must be inside the repository: $OUTPUT_PATH" ;;
esac
case "/$OUTPUT_PATH/" in
*/../*) die "output path must not contain '..': $OUTPUT_PATH" ;;
esac

OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
case "$OUTPUT_DIR/" in
"$REPO_ROOT/"*) ;;
*) die "output must resolve inside the repository: $OUTPUT_DIR" ;;
esac
OUTPUT_PATH="$OUTPUT_DIR/$(basename "$OUTPUT_PATH")"
TEMP_OUTPUT="$(mktemp "$OUTPUT_DIR/.render.XXXXXX")"
cleanup() {
	rm -f "$TEMP_OUTPUT"
}
trap cleanup EXIT

set +e
"$HELM_BIN" "${HELM_ARGS[@]}" >"$TEMP_OUTPUT"
rc=$?
set -e
[[ $rc -eq 0 ]] || exit "$rc"
mv "$TEMP_OUTPUT" "$OUTPUT_PATH"
trap - EXIT
printf 'Rendered manifests to %s\n' "$OUTPUT_LABEL" >&2
