#!/usr/bin/env bash
# Install a fixed-size ext4 filesystem. Keep the original tree visible until
# the copy has completed, and retain it underneath the mount for recovery.
set -euo pipefail
umask 077
state=/var/lib/ate
image=/var/lib/factory-ate.ext4
entry="$image $state ext4 loop 0 0"
mkdir -p "$state"
if [[ ! -e "$image" ]]; then
	if mountpoint -q "$state"; then
		echo "unexpected existing mount at $state" >&2
		exit 1
	fi
	stage="$(mktemp -d /var/lib/.factory-ate.XXXXXX)"
	trap 'if mountpoint -q "$stage/fs"; then umount "$stage/fs" || exit 1; fi; rm -rf "$stage"' EXIT
	mkdir "$stage/fs"
	# Keep the backing filesystem above kubelet's nodefs.available threshold.
	read -r total available < <(df -B1 --output=size,avail "$stage" | tail -1)
	if ((available - 8 * 1024 ** 3 <= total / 10)); then
		echo "allocating the image would leave 10% or less free on the host" >&2
		exit 1
	fi
	fallocate -l 8G "$stage/image"
	mkfs.ext4 -q -E nodiscard -m 0 "$stage/image"
	mount -o loop "$stage/image" "$stage/fs"
	read -r free < <(df -B1 --output=avail "$stage/fs" | tail -1)
	read -r used _ < <(du -sx -B1 "$state")
	read -r inodes _ < <(du -sx --inodes "$state")
	read -r ifree < <(df --output=iavail "$stage/fs" | tail -1)
	if ((used > free || inodes > ifree)); then
		echo "ate state exceeds usable image capacity (bytes or inodes)" >&2
		exit 1
	fi
	# Stopping k3s alone leaves containers (including atelet) running.
	if [[ -x /usr/local/bin/k3s-killall.sh ]]; then
		/usr/local/bin/k3s-killall.sh
	fi
	cp -a --one-file-system "$state/." "$stage/fs/"
	umount "$stage/fs"
	mv "$stage/image" "$image"
fi
if ! grep -Fxq "$entry" /etc/fstab; then
	echo "$entry" >>/etc/fstab
fi
systemctl daemon-reload
mountpoint -q "$state" || mount "$state"
losetup -j "$image" | cut -d: -f1 | grep -Fxq "$(findmnt -no SOURCE --mountpoint "$state")"
mkdir -p /etc/systemd/system/k3s.service.d
printf '[Unit]\nRequiresMountsFor=%s\n' "$state" >/etc/systemd/system/k3s.service.d/20-ate.conf
