#!/usr/bin/env bash
set -euo pipefail
cd projects/factory-cluster/nodes
for arg in audit-policy-file=/etc/rancher/k3s/audit-policy.yaml audit-log-path=/var/log/k3s-audit/audit.log audit-log-maxsize=50 audit-log-maxbackup=4 audit-log-maxage=7 pod-max-pids=4096; do
	grep -Fq "\"$arg\"" k3s-config.yaml
done
grep -Fq 'level: Metadata' audit-policy.yaml
for asset in audit-policy.yaml cap-ate.sh; do
	grep -Fq "$asset" install-node.sh
	grep -Fq "$asset" bootstrap-nodes.sh
done
grep -Fq 'fallocate -l 8G' cap-ate.sh
grep -Fq 'RequiresMountsFor=' cap-ate.sh
# Both bootstrap modes must execute the checked local download.
awk '/^[[:space:]]*echo "[a-f0-9]+  .*install.sh.*sha256sum -c/ {checked=1}
	index($0, "sh \"${tmp}/install.sh\" server") {if (!checked) exit 1; calls++}
	END {if (calls != 2) exit 1}' install-node.sh
