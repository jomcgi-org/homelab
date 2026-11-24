#!/usr/bin/env bash
# Turn KSM off on this node now and at every boot. Run as root from this
# directory (install-node.sh runs it; run it alone to change only KSM):
#
#   sudo ./ksm-off.sh
#
# Idempotent. It installs factory-ksm.conf for systemd-tmpfiles and writes 0 to
# /sys/kernel/mm/ksm/run. It does not unmerge pages that KSM already merged
# (that would be 2). To undo: delete /etc/tmpfiles.d/factory-ksm.conf and write 1
# to /sys/kernel/mm/ksm/run.
#
# KSM_RUN and TMPFILES_DIR are for the test.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KSM_RUN="${KSM_RUN:-/sys/kernel/mm/ksm/run}"
TMPFILES_DIR="${TMPFILES_DIR:-/etc/tmpfiles.d}"

install -D -m 0644 "${HERE}/factory-ksm.conf" "${TMPFILES_DIR}/factory-ksm.conf"

if [[ ! -e "${KSM_RUN}" ]]; then
	echo "ksm-off: ${KSM_RUN} does not exist (kernel without KSM): nothing to turn off"
	exit 0
fi
before="$(<"${KSM_RUN}")"
if [[ "${before}" != 0 ]]; then
	echo 0 >"${KSM_RUN}"
fi
after="$(<"${KSM_RUN}")"
if [[ "${after}" != 0 ]]; then
	echo "ksm-off: ${KSM_RUN} is ${after}, not 0" >&2
	exit 1
fi
echo "ksm-off: run was ${before}, is ${after}; ${TMPFILES_DIR}/factory-ksm.conf keeps it at boot"
