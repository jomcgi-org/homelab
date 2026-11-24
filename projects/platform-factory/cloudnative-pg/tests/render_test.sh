#!/usr/bin/env bash
set -euo pipefail

helm=$1
archive=$2
shared_values=$3
factory_values=$4
filter=$5
manifest=$6
"$helm" template cnpg "$archive" --namespace cnpg-system --kube-version 1.37.2 \
	--include-crds --skip-tests -f "$shared_values" -f "$factory_values" |
	awk -f "$filter" | diff -- "$manifest" -
