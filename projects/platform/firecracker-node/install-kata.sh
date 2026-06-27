#!/usr/bin/env bash
# Step 3a: install Kata Containers static bundle (Firecracker hypervisor + guest
# kernel + rootfs) to /opt/kata. NON-DISRUPTIVE: files + one shim symlink.
set -euo pipefail

if [ -x /opt/kata/bin/containerd-shim-kata-v2 ] && [ -x /opt/kata/bin/firecracker ]; then
	echo "kata + firecracker already installed under /opt/kata"
else
	API=https://api.github.com/repos/kata-containers/kata-containers/releases/latest
	echo "=== assets on latest kata release ==="
	ASSETS=$(curl -fsSL "$API" | grep -oP '"browser_download_url":\s*"\K[^"]+')
	echo "$ASSETS" | sed 's#.*/##'
	# Pick the static x86_64/amd64 tarball, whatever its exact name/compression.
	ASSET_URL=$(echo "$ASSETS" | grep -E 'kata-static' | grep -Ei 'amd64|x86_64' | grep -E '\.tar\.(xz|zst|gz)$' | head -1)
	if [ -z "$ASSET_URL" ]; then
		echo "ERROR: no matching kata-static x86_64 asset (see asset list above)"
		exit 1
	fi
	echo
	echo "=== downloading $ASSET_URL ==="
	FILE=/tmp/$(basename "$ASSET_URL")
	curl -fL --retry 3 -o "$FILE" "$ASSET_URL"
	# Tarball is rooted at ./opt/kata -> extract at / lands it in /opt/kata.
	case "$FILE" in
	*.zst) tar --zstd -C / -xf "$FILE" ;;
	*) tar -C / -xf "$FILE" ;;
	esac
	ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2
	rm -f "$FILE"
fi

echo
echo "=== kata-runtime version ==="
/opt/kata/bin/kata-runtime --version 2>/dev/null | head -3 || true
echo
echo "=== firecracker present? ==="
ls -la /opt/kata/bin/firecracker 2>/dev/null && /opt/kata/bin/firecracker --version 2>/dev/null | head -1 || echo "FIRECRACKER MISSING"
echo
echo "=== fc config + guest assets ==="
ls -la /opt/kata/share/defaults/kata-containers/configuration-fc.toml 2>/dev/null || echo "configuration-fc.toml MISSING"
grep -nE '^\s*(path|kernel|image|initrd|shared_fs|block_device_driver|default_vcpus|default_memory)\s*=' \
	/opt/kata/share/defaults/kata-containers/configuration-fc.toml 2>/dev/null | head -25
echo
echo "=== shim on PATH ==="
ls -la /usr/local/bin/containerd-shim-kata-v2
