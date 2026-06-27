#!/usr/bin/env bash
# Step 3 (corrected path): add devmapper snapshotter + kata-fc runtime to the
# ACTIVE k3s containerd config (data-dir = /disks/nvme-01/k3s-data), via the
# config.toml.tmpl mechanism. DISRUPTIVE: restarts k3s-agent. Auto-rollback +
# hard verification that the additions actually render this time.
set -uo pipefail
ACT=/disks/nvme-01/k3s-data/agent/etc/containerd
TMPL="$ACT/config.toml.tmpl"
BAK="$ACT/config.toml.pre-kata.$(date +%s).bak"
STRAY=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl

# Remove the stray template that went to the abandoned default tree.
[ -f "$STRAY" ] && rm -f "$STRAY" && echo "removed stray $STRAY"

mkdir -p /var/lib/containerd-devmapper
cp -a "$ACT/config.toml" "$BAK"
echo "backed up active config -> $BAK"

# Full-content template: copy live config verbatim (no transcription risk), append.
cp "$ACT/config.toml" "$TMPL"
cat >>"$TMPL" <<'EOF'

# --- kata-fc (Firecracker microVMs), ADR platform/010 ---
# Devmapper snapshotter: FC needs a block-device container rootfs (not overlayfs).
# Used ONLY by kata-fc below; overlayfs stays the default for every other pod.
[plugins.'io.containerd.snapshotter.v1.devmapper']
  pool_name = "devpool"
  root_path = "/var/lib/containerd-devmapper"
  base_image_size = "10GB"
  discard_blocks = true

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.'kata-fc']
  runtime_type = "io.containerd.kata.v2"
  snapshotter = "devmapper"

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.'kata-fc'.options]
  ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-fc.toml"
EOF
echo "wrote $TMPL"

echo "=== restarting k3s-agent (node-4 disruption) ==="
systemctl restart k3s-agent

OK=0
for i in $(seq 1 15); do
	if systemctl is-active --quiet k3s-agent && k3s ctr version >/dev/null 2>&1; then
		OK=1
		break
	fi
	sleep 3
done

if [ "$OK" != "1" ]; then
	echo "!!! UNHEALTHY -> AUTO-ROLLBACK"
	rm -f "$TMPL"
	systemctl restart k3s-agent
	for i in $(seq 1 15); do
		systemctl is-active --quiet k3s-agent && k3s ctr version >/dev/null 2>&1 && break
		sleep 3
	done
	systemctl is-active --quiet k3s-agent && echo "ROLLED BACK OK (default config restored)" ||
		echo "ROLLBACK FAILED: run -> rm $TMPL ; cp $BAK $ACT/config.toml ; systemctl restart k3s-agent"
	exit 1
fi

echo "=== node healthy; verifying additions actually rendered ==="
grep -nE "devmapper|kata-fc" "$ACT/config.toml" | head
DMSTATE=$(k3s ctr plugins ls 2>/dev/null | awk '$2=="devmapper"{print $NF}')
RENDERED=$(grep -cE "kata-fc" "$ACT/config.toml")
echo "kata-fc lines in rendered config: $RENDERED ; devmapper plugin state: ${DMSTATE:-missing}"

if [ "${RENDERED:-0}" -gt 0 ] && [ "$DMSTATE" = "ok" ]; then
	echo "SUCCESS: template applied, devmapper=ok, kata-fc registered"
elif [ "${RENDERED:-0}" -gt 0 ]; then
	echo "PARTIAL: kata-fc rendered but devmapper state=$DMSTATE (expected ok). Node healthy; investigate pool/config before testing a pod."
else
	echo "STILL NOT APPLIED: kata-fc absent from rendered config. Node healthy; do not proceed."
fi
