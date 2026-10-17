#!/usr/bin/env bash
#
# uninstall_merc_proxy.sh
# Reverse of install_merc_proxy.sh: removes exactly what the installer
# added and leaves everything else untouched - including anything that was
# already on the board before the first install, and anything YOU changed
# after installing.
#
# Usage:
#   sudo bash uninstall_merc_proxy.sh            # perform the uninstall
#   bash uninstall_merc_proxy.sh --dry-run       # report only, change nothing
#
# Safe to re-run: once the footprint is gone it reports "nothing to do".
#
# Strategy (surgical token removal):
#   * Only the exact strings/lines the installer is known to add are
#     stripped (the cmdline.txt token, the dtoverlay=dwc2 line, the three
#     /etc/modules lines), so running the installer N times then this
#     script once still lands on the pre-first-install state.
#   * Files the installer created (gadget script, forwarder, service unit,
#     modules-load.d drop-in, manifest) are deleted.
#   * Before touching anything, the CURRENT state is saved to a fresh
#     /var/backups/merc-proxy/uninstall-<timestamp>/ directory, so a bad
#     uninstall is itself reversible.
#   * Afterwards, each reverted file is compared against the OLDEST install
#     backup (taken before the first install). Identical => guaranteed clean.
#     Different => the diff is shown and restore commands are printed.

set -euo pipefail

MANIFEST="/etc/merc-proxy/manifest"
BACKUP_ROOT="/var/backups/merc-proxy"
GADGET_SH="/usr/bin/enable_keyboard_gadget.sh"
SERVICE_UNIT="/etc/systemd/system/merc-proxy.service"
MODULES_LOAD_D="/etc/modules-load.d/merc-gadget.conf"
GADGET_PATH="/sys/kernel/config/usb_gadget/g1"
CMDLINE_TOKEN="modules-load=dwc2,libcomposite"

log()  { printf '\n\033[1;32m[%s]\033[0m %s\n' "$1" "${*:2}"; }
warn() { printf '\033[1;33m   WARNING: %s\033[0m\n' "$*"; }

# ------------------------------------------------------------------- options
DRY=0
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY=1 ;;
        -h|--help) grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
        *) printf 'Unknown option: %s (supported: --dry-run)\n' "${arg}" >&2; exit 2 ;;
    esac
done

if [ "${DRY}" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    log "SUDO" "This uninstaller needs root. Re-running itself under sudo..."
    exec sudo bash "$0" "$@"
fi
if [ "${DRY}" -eq 1 ]; then
    log "DRY-RUN" "Reporting only - nothing will be changed."
fi

# --------------------------------------------------------- locate everything
CMDLINE=""
if [ -f /boot/firmware/cmdline.txt ]; then
    CMDLINE="/boot/firmware/cmdline.txt"
elif [ -f /boot/cmdline.txt ]; then
    CMDLINE="/boot/cmdline.txt"
else
    warn "No cmdline.txt found (not a standard Pi OS layout?) - skipping boot-file steps."
fi
CONFIG_TXT=""
if [ -n "${CMDLINE}" ]; then
    CONFIG_TXT="$(dirname "${CMDLINE}")/config.txt"
fi

# Target home dir: manifest first, then the service unit, then a home scan.
TARGET_HOME=""
if [ -f "${MANIFEST}" ]; then
    # shellcheck disable=SC1091
    . "${MANIFEST}"
    TARGET_HOME="${MERC_TARGET_HOME:-}"
fi
if [ -z "${TARGET_HOME}" ] && [ -f "${SERVICE_UNIT}" ]; then
    TARGET_HOME="$(sed -n 's/^WorkingDirectory=//p' "${SERVICE_UNIT}" | head -n 1)"
fi
if [ -z "${TARGET_HOME}" ]; then
    for d in /home/*/forward_merc.py; do
        if [ -f "${d}" ]; then
            TARGET_HOME="$(dirname "${d}")"
            break
        fi
    done
fi
FORWARD_PY=""
if [ -n "${TARGET_HOME}" ]; then
    FORWARD_PY="${TARGET_HOME}/forward_merc.py"
fi

# ------------------------------------------------------------ footprint scan
log "SCAN" "Looking for installer footprint..."
found=0
for f in "${SERVICE_UNIT}" "${GADGET_SH}" "${MODULES_LOAD_D}" "${MANIFEST}" "${FORWARD_PY}"; do
    if [ -n "${f}" ] && [ -e "${f}" ]; then
        log "FOUND" "${f}"
        found=1
    fi
done
if [ -n "${CMDLINE}" ] && [ -f "${CMDLINE}" ] && grep -q "${CMDLINE_TOKEN}" "${CMDLINE}"; then
    log "FOUND" "${CMDLINE_TOKEN} token in ${CMDLINE}"
    found=1
fi
if [ -n "${CONFIG_TXT}" ] && [ -f "${CONFIG_TXT}" ] && grep -qxE 'dtoverlay=dwc2' "${CONFIG_TXT}"; then
    log "FOUND" "dtoverlay=dwc2 line in ${CONFIG_TXT}"
    found=1
fi
if [ -f /etc/modules ]; then
    for m in configfs dwc2 libcomposite; do
        if grep -qxE "${m}" /etc/modules; then
            log "FOUND" "${m} line in /etc/modules"
            found=1
        fi
    done
fi
if [ -d "${GADGET_PATH}" ]; then
    log "FOUND" "live USB gadget at ${GADGET_PATH}"
    found=1
fi

if [ "${found}" -eq 0 ]; then
    log "DONE" "No merc-proxy footprint found - nothing to uninstall."
    exit 0
fi

# ------------------------------------------------ safety backup of CURRENT state
SAFETY_DIR="${BACKUP_ROOT}/uninstall-$(date +%Y%m%d-%H%M%S)"
safety=0
for f in "${CMDLINE}" "${CONFIG_TXT}" /etc/modules "${MODULES_LOAD_D}" \
         "${GADGET_SH}" "${SERVICE_UNIT}" "${FORWARD_PY}" "${MANIFEST}"; do
    if [ -n "${f}" ] && [ -e "${f}" ]; then safety=1; fi
done
if [ "${safety}" -eq 1 ]; then
    if [ "${DRY}" -eq 1 ]; then
        log "BACKUP" "Would save current state to ${SAFETY_DIR}"
    else
        mkdir -p "${SAFETY_DIR}"
        for f in "${CMDLINE}" "${CONFIG_TXT}" /etc/modules "${MODULES_LOAD_D}" \
                 "${GADGET_SH}" "${SERVICE_UNIT}" "${FORWARD_PY}" "${MANIFEST}"; do
            if [ -n "${f}" ] && [ -e "${f}" ]; then
                cp -p "${f}" "${SAFETY_DIR}/$(echo "${f}" | tr '/' '_')"
            fi
        done
        log "BACKUP" "Current state saved to ${SAFETY_DIR}"
    fi
fi

# ------------------------------------------------------------ stop the service
if [ -f "${SERVICE_UNIT}" ]; then
    if [ "${DRY}" -eq 1 ]; then
        log "SERVICE" "Would stop + disable merc-proxy and delete ${SERVICE_UNIT}"
    else
        log "SERVICE" "Stopping + disabling merc-proxy..."
        systemctl disable --now merc-proxy.service >/dev/null 2>&1 || true
        rm -f "${SERVICE_UNIT}"
        systemctl daemon-reload
        log "SERVICE" "Service removed."
    fi
fi

# ---------------------------------------------------------- tear down gadget
if [ -d "${GADGET_PATH}" ]; then
    if [ "${DRY}" -eq 1 ]; then
        log "GADGET" "Would tear down the live USB gadget at ${GADGET_PATH}"
    else
        log "GADGET" "Tearing down the live USB gadget (the PC's virtual keyboard disappears)..."
        echo "" > "${GADGET_PATH}/UDC" 2>/dev/null || true
        sleep 1
        rm -rf "${GADGET_PATH}"
        log "GADGET" "Gadget removed."
    fi
fi

# ------------------------------------------------------------ cmdline.txt token
if [ -n "${CMDLINE}" ] && [ -f "${CMDLINE}" ] && grep -q "${CMDLINE_TOKEN}" "${CMDLINE}"; then
    if [ "${DRY}" -eq 1 ]; then
        log "CMDLINE" "Would strip '${CMDLINE_TOKEN}' from ${CMDLINE}"
    else
        content="$(tr -d '\n\r' < "${CMDLINE}")"
        content="${content// ${CMDLINE_TOKEN}/}"
        content="${content#"${CMDLINE_TOKEN}"}"
        printf '%s\n' "${content}" > "${CMDLINE}"
        log "CMDLINE" "Stripped '${CMDLINE_TOKEN}' from ${CMDLINE} (still one line)."
    fi
fi

# ------------------------------------------------------------- config.txt line
if [ -n "${CONFIG_TXT}" ] && [ -f "${CONFIG_TXT}" ] && grep -qxE 'dtoverlay=dwc2' "${CONFIG_TXT}"; then
    if [ "${DRY}" -eq 1 ]; then
        log "CONFIGTXT" "Would delete the 'dtoverlay=dwc2' line from ${CONFIG_TXT}"
    else
        tmp="$(mktemp "$(dirname "${CONFIG_TXT}")/.config.XXXXXX")"
        grep -vxE 'dtoverlay=dwc2' "${CONFIG_TXT}" > "${tmp}" || true
        mv "${tmp}" "${CONFIG_TXT}"
        # If the [all] header we appended is now the last content in the file
        # (i.e. that section holds nothing else), remove the header too.
        last_line="$(awk 'NF { line = $0 } END { print line }' "${CONFIG_TXT}")"
        if [ "${last_line}" = "[all]" ]; then
            awk '
                { lines[++n] = $0 }
                END {
                    while (n > 0 && lines[n] ~ /^[[:space:]]*$/) n--
                    if (n > 0 && lines[n] == "[all]") n--
                    while (n > 0 && lines[n] ~ /^[[:space:]]*$/) n--
                    for (i = 1; i <= n; i++) print lines[i]
                }
            ' "${CONFIG_TXT}" > "${CONFIG_TXT}.awk" && mv "${CONFIG_TXT}.awk" "${CONFIG_TXT}"
            log "CONFIGTXT" "Removed the now-empty [all] section we appended."
        fi
        log "CONFIGTXT" "Deleted 'dtoverlay=dwc2' from ${CONFIG_TXT}."
    fi
fi

# ----------------------------------------------------------- /etc/modules lines
if [ -f /etc/modules ]; then
    for m in configfs dwc2 libcomposite; do
        if grep -qxE "${m}" /etc/modules; then
            if [ "${DRY}" -eq 1 ]; then
                log "MODULES" "Would delete the '${m}' line from /etc/modules"
            else
                tmp="$(mktemp /etc/.modules.XXXXXX)"
                grep -vxE "${m}" /etc/modules > "${tmp}" || true
                mv "${tmp}" /etc/modules
                log "MODULES" "Deleted '${m}' from /etc/modules."
            fi
        fi
    done
fi

# ------------------------------------------------- delete installer-created files
for f in "${MODULES_LOAD_D}" "${GADGET_SH}" "${FORWARD_PY}" "${MANIFEST}"; do
    if [ -n "${f}" ] && [ -e "${f}" ]; then
        if [ "${DRY}" -eq 1 ]; then
            log "DELETE" "Would delete ${f}"
        else
            rm -f "${f}"
            log "DELETE" "Deleted ${f}"
        fi
    fi
done
if [ "${DRY}" -eq 0 ]; then
    rmdir /etc/merc-proxy 2>/dev/null || true
fi

# ------------------------------------------------------------- verify removal
log "VERIFY" "Re-scanning for residue..."
residue=0
for f in "${SERVICE_UNIT}" "${GADGET_SH}" "${MODULES_LOAD_D}" "${MANIFEST}" "${FORWARD_PY}"; do
    if [ -n "${f}" ] && [ -e "${f}" ]; then
        warn "Still present: ${f}"
        residue=1
    fi
done
if [ -n "${CMDLINE}" ] && grep -q "${CMDLINE_TOKEN}" "${CMDLINE}"; then
    warn "Still present: ${CMDLINE_TOKEN} in ${CMDLINE}"
    residue=1
fi
if [ -n "${CONFIG_TXT}" ] && grep -qxE 'dtoverlay=dwc2' "${CONFIG_TXT}"; then
    warn "Still present: dtoverlay=dwc2 in ${CONFIG_TXT}"
    residue=1
fi
if [ -f /etc/modules ]; then
    for m in configfs dwc2 libcomposite; do
        if grep -qxE "${m}" /etc/modules; then
            warn "Still present: ${m} in /etc/modules"
            residue=1
        fi
    done
fi
if [ "${residue}" -eq 0 ]; then
    log "VERIFY" "All installer footprint removed."
fi

# ------------------------------------- compare against the pre-install backup
oldest="$(find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | sort | head -n 1 || true)"
if [ -n "${oldest}" ]; then
    log "CHECK" "Comparing against pre-install backup: ${oldest}"
    for f in "${CMDLINE}" "${CONFIG_TXT}" /etc/modules; do
        if [ -z "${f}" ] || [ ! -f "${f}" ]; then continue; fi
        bak="${oldest}/$(echo "${f}" | tr '/' '_')"
        if [ ! -f "${bak}" ]; then
            warn "No pre-install copy of ${f} in ${oldest} (older installer version?) - skipping."
            continue
        fi
        if [ "${DRY}" -eq 1 ]; then
            log "CHECK" "Would compare ${f} against ${bak}"
        elif cmp -s "${bak}" "${f}"; then
            log "MATCH" "${f} is byte-identical to its pre-install state."
        else
            warn "${f} differs from its pre-install state (manual edits after install?)"
            diff -u "${bak}" "${f}" || true
            log "RESTORE" "To force the exact pre-install state, run:"
            log "RESTORE" "  sudo cp -p '${bak}' '${f}'"
        fi
    done
else
    warn "No pre-install backup found under ${BACKUP_ROOT}/ - cannot cross-check."
    warn "The surgical removal above is complete; nothing further to do."
fi

# -------------------------------------------------------------------- final
if [ "${DRY}" -eq 1 ]; then
    cat <<DRYOUT

================================================================
 DRY RUN COMPLETE - nothing was changed.
 Re-run without --dry-run to apply the above.
================================================================
DRYOUT
    exit 0
fi

cat <<DONE

================================================================
 UNINSTALL COMPLETE
================================================================
 Removed (installer-created):
   * ${SERVICE_UNIT}
   * ${GADGET_SH}
   * ${MODULES_LOAD_D}
   * ${FORWARD_PY:-~/forward_merc.py}
   * ${MANIFEST}
 Stripped (installer-edited):
   * '${CMDLINE_TOKEN}' from ${CMDLINE}
   * 'dtoverlay=dwc2' (+ empty [all]) from ${CONFIG_TXT}
   * configfs / dwc2 / libcomposite lines from /etc/modules

 Left in place (manual, optional):
   * python3-evdev package. Remove with:
       sudo apt-get remove -y python3-evdev
   * Backups (your safety net - safe to delete once happy):
       ${BACKUP_ROOT}/
       Current state before this uninstall: ${SAFETY_DIR}

 REBOOT RECOMMENDED:
   The dwc2/libcomposite kernel modules stay loaded until the next
   reboot:  sudo reboot
================================================================
DONE
