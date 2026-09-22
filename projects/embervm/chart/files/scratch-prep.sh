#!/bin/sh
# Prepare EmberVM's host scratch mount. Destructive ext4 to XFS migration is
# deliberately gated twice: XFS must be selected for new images and migration
# must be enabled separately. The script never reformats an existing XFS image.
# Single-quoted programs below expand only after nsenter reaches the host shell.
# shellcheck disable=SC2016
set -eu

SCRATCH="${SCRATCH_PATH:-/var/lib/embervm/scratch}"
CONTAINER_SCRATCH="${CONTAINER_SCRATCH_PATH:-/host/scratch}"
IMG="${SCRATCH_IMAGE_PATH:-/host/scratch.img}"
HOST_IMG="${HOST_SCRATCH_IMAGE_PATH:-/var/lib/embervm/scratch.img}"
FSTAB="${HOST_FSTAB_PATH:-/etc/fstab}"
MARKER="${SCRATCH_MARKER_PATH:-$SCRATCH/.scratch-generation}"
SIZE_GI="${SCRATCH_SIZE_GI:?SCRATCH_SIZE_GI unset}"
FILESYSTEM="${SCRATCH_FILESYSTEM:-ext4}"
MIGRATE_EXT4="${SCRATCH_MIGRATE_EXT4_TO_XFS:-false}"
NODE="${NODE_NAME:-unknown-node}"

fail() {
	echo "scratch-prep: $*" >&2
	exit 1
}

host() {
	nsenter -t 1 -m -- "$@"
}

case "$FILESYSTEM" in
ext4 | xfs) ;;
*) fail "SCRATCH_FILESYSTEM must be ext4 or xfs, got $FILESYSTEM" ;;
esac
case "$MIGRATE_EXT4" in
true | false) ;;
*) fail "SCRATCH_MIGRATE_EXT4_TO_XFS must be true or false" ;;
esac
if [ "$MIGRATE_EXT4" = true ] && [ "$FILESYSTEM" != xfs ]; then
	fail "ext4 migration requires SCRATCH_FILESYSTEM=xfs"
fi

filesystem_type() {
	type=$(blkid -p -s TYPE -o value "$IMG" 2>/dev/null) ||
		fail "cannot identify the filesystem on existing managed image $HOST_IMG"
	case "$type" in
	ext4 | xfs) printf '%s\n' "$type" ;;
	*) fail "unsupported filesystem '$type' on existing managed image $HOST_IMG" ;;
	esac
}

managed_path_identity() {
	{ [ -f "$IMG" ] && [ ! -L "$IMG" ]; } ||
		fail "managed image path is not a regular non-symlink file: $HOST_IMG"
	stat -c '%d:%i' "$IMG" || fail "cannot inspect managed image identity for $HOST_IMG"
}

managed_image_identity() {
	managed_path_identity >/dev/null
	identity=$(stat -c '%d:%i:%h' "$IMG") ||
		fail "cannot inspect managed image identity for $HOST_IMG"
	links=${identity##*:}
	[ "$links" -eq 1 ] || fail "managed image has hard-link aliases"
	printf '%s\n' "${identity%:*}"
}

has_active_consumer() {
	target=$1
	mode=${2:-path}
	if [ "$mode" = mount ]; then
		if fuser -m "$target" >/dev/null 2>&1; then
			status=0
		else
			status=$?
		fi
	else
		if fuser "$target" >/dev/null 2>&1; then
			status=0
		else
			status=$?
		fi
	fi
	[ "$status" -ne 0 ] || return 0
	[ "$status" -eq 1 ] || fail "consumer inspection failed for $target"
	return 1
}

format_new_image() {
	selected=$1
	# Format a unique inode before publishing it at the managed path. The hard
	# link is an atomic no-clobber operation: if any file or symlink appeared
	# after the caller's absence check, it is preserved and this attempt fails.
	tmp_image=$(mktemp "${IMG}.scratch-prep.XXXXXX") || return
	chmod 0600 "$tmp_image" || {
		rm -f "$tmp_image"
		return 1
	}
	fallocate -l "${SIZE_GI}G" "$tmp_image" || {
		rm -f "$tmp_image"
		return 1
	}
	if [ "$selected" = xfs ]; then
		mkfs.xfs -m reflink=1 -f "$tmp_image" || {
			rm -f "$tmp_image"
			return 1
		}
	else
		mkfs.ext4 -q -F "$tmp_image" || {
			rm -f "$tmp_image"
			return 1
		}
	fi
	if ! ln "$tmp_image" "$IMG"; then
		rm -f "$tmp_image"
		return 1
	fi
	rm -f "$tmp_image"
}

format_xfs() {
	expected_identity=$1
	# Keep the verified inode open across mkfs so a concurrent path replacement
	# can never redirect the destructive operation to the replacement. The path
	# must still name that inode before and after formatting; otherwise fail
	# without mounting or publishing readiness.
	exec 9<>"$IMG" || fail "cannot open managed image for migration: $HOST_IMG"
	fd_path="/proc/$$/fd/9"
	fd_identity=$(stat -Lc '%d:%i' "$fd_path") || {
		exec 9>&-
		fail "cannot pin managed image identity for $HOST_IMG"
	}
	path_identity=$(managed_image_identity)
	if [ "$fd_identity" != "$expected_identity" ] || [ "$path_identity" != "$expected_identity" ]; then
		exec 9>&-
		fail "managed image identity changed before migration"
	fi
	if ! mkfs.xfs -m reflink=1 -f "$fd_path"; then
		exec 9>&-
		fail "failed to replace ext4 on $HOST_IMG with XFS"
	fi
	path_identity=$(managed_image_identity)
	if [ "$path_identity" != "$expected_identity" ]; then
		exec 9>&-
		fail "managed image identity changed during migration"
	fi
	exec 9>&-
}

fstab_identity_is_safe() {
	if host awk -v image="$HOST_IMG" -v scratch="$SCRATCH" '
    /^[[:space:]]*#/ || NF == 0 { next }
    $1 == image && $2 != scratch { exit 42 }
    $2 == scratch && $1 != image { exit 43 }
  ' "$FSTAB"; then
		status=0
	else
		status=$?
	fi
	case "$status" in
	0) return 0 ;;
	42 | 43) return 1 ;;
	*) fail "cannot inspect fstab identity for $HOST_IMG and $SCRATCH (status $status)" ;;
	esac
}

fstab_has_managed_entry() {
	expected_fs=$1
	host awk -v image="$HOST_IMG" -v scratch="$SCRATCH" -v filesystem="$expected_fs" '
    /^[[:space:]]*#/ || NF == 0 { next }
    $1 == image && $2 == scratch {
      found = 1
      has_loop = 0
      option_count = split($4, options, ",")
      for (option_index = 1; option_index <= option_count; option_index++) {
        if (options[option_index] == "loop") { has_loop = 1 }
      }
      if ($3 != filesystem || !has_loop) { unsafe = 1 }
    }
    END { if (!found || unsafe) { exit 1 } }
  ' "$FSTAB"
}

reconcile_fstab() {
	fs=$1
	if ! fstab_identity_is_safe; then
		fail "fstab contains a foreign or aliased entry for $HOST_IMG or $SCRATCH"
	fi
	host sh -c '
    set -eu
    fstab=$1
    image=$2
    scratch=$3
    filesystem=$4
    tmp="${fstab}.scratch-prep.$$"
    trap '\''rm -f "$tmp"'\'' EXIT HUP INT TERM
    awk -v image="$image" -v scratch="$scratch" '\''
      $1 == image && $2 == scratch { next }
      { print }
    '\'' "$fstab" > "$tmp"
    printf "%s %s %s loop,defaults 0 0\n" "$image" "$scratch" "$filesystem" >> "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$fstab"
    trap - EXIT HUP INT TERM
  ' sh "$FSTAB" "$HOST_IMG" "$SCRATCH" "$fs"
}

write_marker() {
	generation="${NODE}-$(cat /proc/sys/kernel/random/uuid)"
	host sh -c '
    set -eu
    marker=$1
    generation=$2
    if [ ! -s "$marker" ]; then
      printf "%s\n" "$generation" > "${marker}.tmp"
      mv "${marker}.tmp" "$marker"
    fi
  ' sh "$MARKER" "$generation"
	echo "scratch-prep: generation marker ready at $MARKER on $NODE"
}

mounted=false
if host mountpoint -q "$SCRATCH"; then
	mounted=true
fi

if [ "$mounted" = true ]; then
	# The default path keeps the historical mountpoint guard. In particular, a
	# node-4 bind mount is not inspected, unmounted, or changed.
	if [ "$FILESYSTEM" != xfs ] || [ "$MIGRATE_EXT4" != true ]; then
		echo "scratch-prep: $SCRATCH already mounted on $NODE, leaving it untouched"
		write_marker
		exit 0
	fi

	# Migration is allowed only when the mounted source is the one unambiguous
	# loop device whose backing file is our exact managed regular image.
	{ [ -f "$IMG" ] && [ ! -L "$IMG" ]; } || {
		echo "scratch-prep: mounted scratch is not backed by the managed image, leaving it untouched"
		write_marker
		exit 0
	}
	# Bind every subsequent eligibility check to the inode that occupied the
	# managed path before loop, filesystem, and consumer inspection began.
	migration_identity=$(managed_path_identity)
	mount_info=$(host findmnt -n -o SOURCE,FSTYPE --target "$SCRATCH") ||
		fail "cannot inspect the mounted source for $SCRATCH"
	# Intentionally split findmnt's exact SOURCE,FSTYPE pair into two fields.
	# shellcheck disable=SC2086
	set -- $mount_info
	[ "$#" -eq 2 ] || fail "ambiguous mounted source for $SCRATCH"
	mount_source=$1
	mount_type=$2

	loop_info=$(host losetup -j "$HOST_IMG") || fail "cannot inspect loop identity for $HOST_IMG"
	if [ -z "$loop_info" ]; then
		echo "scratch-prep: $SCRATCH is a foreign mount, leaving it untouched"
		write_marker
		exit 0
	fi
	[ "$(printf '%s\n' "$loop_info" | wc -l | tr -d ' ')" -eq 1 ] ||
		fail "managed image has multiple loop aliases"
	loop_device=${loop_info%%:*}
	if [ "$mount_source" != "$loop_device" ]; then
		echo "scratch-prep: $SCRATCH is a foreign mount, leaving it untouched"
		write_marker
		exit 0
	fi
	backing=$(host losetup -n -O BACK-FILE "$loop_device") ||
		fail "cannot inspect backing file for $loop_device"
	[ "$backing" = "$HOST_IMG" ] || fail "loop backing identity does not match $HOST_IMG"
	targets=$(host findmnt -rn -S "$loop_device" -o TARGET) ||
		fail "cannot inspect mount aliases for $loop_device"
	[ "$targets" = "$SCRATCH" ] || fail "managed loop device has a foreign or additional mount"

	image_type=$(filesystem_type)
	[ "$mount_type" = "$image_type" ] || fail "mounted and on-disk filesystem types disagree"
	if [ "$image_type" = xfs ]; then
		echo "scratch-prep: existing XFS scratch is preserved"
		write_marker
		exit 0
	fi
	[ "$image_type" = ext4 ] || fail "only a managed ext4 image is eligible for migration"
	verified_identity=$(managed_image_identity)
	[ "$verified_identity" = "$migration_identity" ] ||
		fail "managed image identity changed during migration verification"
	if has_active_consumer "$CONTAINER_SCRATCH" mount; then
		fail "managed ext4 scratch has an active consumer; drain and quiesce the node before retrying"
	fi
	fstab_identity_is_safe || fail "fstab identity is unsafe for managed ext4 migration"
	fstab_has_managed_entry ext4 ||
		fail "fstab does not identify the managed ext4 loop image for migration"
	host umount "$SCRATCH" || fail "ordinary unmount failed; no force or lazy unmount was attempted"
	if host mountpoint -q "$SCRATCH"; then
		fail "scratch remains mounted after ordinary unmount"
	fi
	remaining_aliases=$(host losetup -j "$HOST_IMG") || fail "cannot inspect loop aliases for $HOST_IMG after unmount"
	[ -z "$remaining_aliases" ] || fail "unmounted managed image still has a loop alias"
	format_xfs "$migration_identity"
	image_type=xfs
	mounted=false
	echo "scratch-prep: migrated the explicitly enabled managed ext4 image to XFS"
fi

if [ "$mounted" = false ]; then
	fstab_identity_is_safe || fail "fstab contains a foreign or aliased entry for $HOST_IMG or $SCRATCH"
	host mkdir -p "$SCRATCH"
	if [ ! -e "$IMG" ] && [ ! -L "$IMG" ]; then
		echo "scratch-prep: creating ${SIZE_GI}Gi $FILESYSTEM backing file at $HOST_IMG on $NODE"
		if ! format_new_image "$FILESYSTEM"; then
			fail "failed to create and format managed image $HOST_IMG"
		fi
		image_type=$FILESYSTEM
	else
		{ [ -f "$IMG" ] && [ ! -L "$IMG" ]; } ||
			fail "managed image path exists but is not a regular non-symlink file: $HOST_IMG"
		# Capture identity before probing the filesystem or loop aliases so a
		# replacement during eligibility checks can never become the format target.
		migration_identity=$(managed_path_identity)
		image_type=$(filesystem_type)
		if [ "$image_type" = ext4 ] && [ "$FILESYSTEM" = xfs ]; then
			if [ "$MIGRATE_EXT4" != true ]; then
				echo "scratch-prep: existing ext4 image retained because migration is disabled"
			else
				aliases=$(host losetup -j "$HOST_IMG") || fail "cannot inspect loop aliases for $HOST_IMG"
				[ -z "$aliases" ] || fail "unmounted managed image still has a loop alias"
				verified_identity=$(managed_image_identity)
				[ "$verified_identity" = "$migration_identity" ] ||
					fail "managed image identity changed during migration verification"
				if has_active_consumer "$IMG"; then
					fail "managed ext4 image has an active consumer; drain and quiesce the node before retrying"
				fi
				fstab_identity_is_safe || fail "fstab identity is unsafe for managed ext4 migration"
				fstab_has_managed_entry ext4 ||
					fail "fstab does not identify the managed ext4 loop image for migration"
				format_xfs "$migration_identity"
				image_type=xfs
				echo "scratch-prep: migrated the explicitly enabled managed ext4 image to XFS"
			fi
		elif [ "$image_type" = xfs ]; then
			echo "scratch-prep: existing XFS image preserved"
		fi
	fi

	reconcile_fstab "$image_type"
	host mount -t "$image_type" -o loop "$HOST_IMG" "$SCRATCH" ||
		fail "failed to mount managed $image_type scratch"
	echo "scratch-prep: ${SIZE_GI}Gi capped $image_type loop scratch mounted at $SCRATCH on $NODE"
fi

write_marker
