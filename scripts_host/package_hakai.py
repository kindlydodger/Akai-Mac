#!/usr/bin/env python3
"""
package_hakai.py — Export an SDK-built MPC plugin into a Hakai 3.10 + OpenMPC Plugin Manager G2
compatible installer package (/sdcard/Synths/<Manufacturer> - VST - <Name>/ + MPC.settings pluginList-arm).
"""

import json
import os
import re
import shutil
import sys

PLUGIN_LIST_AWK = r"""# Edit the plugin list inside MPC.settings (BusyBox-awk compatible).
function emit_pending(   hit) {
    hit = index(pending, "file=\"" file "\"")
    if (!hit) print pending
    pending = ""
}
BEGIN {
    added = 0; inlist = 0; pending = ""
    if (mode == "add") { while ((getline line < entryfile) > 0) entry = entry line; close(entryfile) }
}
pending != "" { pending = pending "\n" $0; if ($0 ~ /\/>/) emit_pending(); next }
/<PLUGIN( |$)/ { pending = $0; if ($0 ~ /\/>/) emit_pending(); next }
/<VALUE name="pluginList-arm">/ { inlist = 1; print; next }
inlist && /<KNOWNPLUGINS\/>/ {
    if (mode == "add") {
        ind = $0; sub(/<.*/, "", ind)
        print ind "<KNOWNPLUGINS>"; print ind "  " entry; print ind "</KNOWNPLUGINS>"; added = 1
    } else print
    inlist = 0; next
}
inlist && /<\/KNOWNPLUGINS>/ {
    if (mode == "add" && !added) { ind = $0; sub(/<.*/, "", ind); print ind "  " entry; added = 1 }
    inlist = 0; print; next
}
/<\/PROPERTIES>/ {
    if (mode == "add" && !added) {
        print "  <VALUE name=\"pluginList-arm\">"; print "    <KNOWNPLUGINS>"; print "      " entry
        print "    </KNOWNPLUGINS>"; print "  </VALUE>"; added = 1
    }
    print; next
}
{ print }
"""

INSTALL_SH_TEMPLATE = r"""#!/bin/sh
# Hakai 3.10 + OpenMPC Plugin Manager G2 Installer for {name} ({version})
set -e
cd "$(dirname "$0")"
NAME='{name}'
VERSION='{version}'
SO='{so_name}'
SKIN='{skin_folder}'
SO_PATH="/sdcard/Synths/$SKIN/$SO"
YES=0
[ "$1" = "-y" ] && YES=1
die() {{ echo "error: $*" >&2; exit 1; }}

[ "$(id -u)" = 0 ] || die "run as root"
SETTINGS=$(ls /media/az01-internal/Settings/*/MPC.settings 2>/dev/null | head -n 1)
[ -n "$SETTINGS" ] || die "MPC.settings not found under /media/az01-internal/Settings/"

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) SRC_SO="payload/vst/{so_arm64}" ;;
    *)             SRC_SO="payload/vst/{so_arm32}" ;;
esac
[ -f "$SRC_SO" ] || die "binary $SRC_SO not found for architecture $ARCH"

if [ $YES = 0 ]; then
    printf "Installing %s %s to Hakai 3.10 (MPC will restart). Continue? [y/N] " "$NAME" "$VERSION"
    read -r ok
    case "$ok" in y|Y|yes) ;; *) echo "cancelled"; exit 1 ;; esac
fi

systemctl stop acvs
trap 'systemctl start acvs' EXIT
i=0
while pidof MPC >/dev/null 2>&1 && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done

mkdir -p "/sdcard/Synths" "/sdcard/vst"
rm -rf "/sdcard/Synths/$SKIN"
cp -a "payload/Synths/$SKIN" "/sdcard/Synths/$SKIN"
cp "$SRC_SO" "$SO_PATH"
chmod 755 "$SO_PATH"
cp "$SRC_SO" "/sdcard/vst/$SO"
chmod 755 "/sdcard/vst/$SO"

BAK="$SETTINGS.bak-{slug}-$(date +%Y%m%d-%H%M%S)"
cp "$SETTINGS" "$BAK"
awk -v mode=add -v file="$SO_PATH" -v entryfile=plugin.xml -f plugin_list.awk "$SETTINGS" > "$SETTINGS.new"
mv "$SETTINGS.new" "$SETTINGS"
sync

echo "[OK] Installed $NAME ($VERSION) on Hakai 3.10! Settings backup: $BAK"
"""

UNINSTALL_SH_TEMPLATE = r"""#!/bin/sh
set -e
cd "$(dirname "$0")"
SO='{so_name}'
SKIN='{skin_folder}'
SO_PATH="/sdcard/Synths/$SKIN/$SO"
SETTINGS=$(ls /media/az01-internal/Settings/*/MPC.settings 2>/dev/null | head -n 1)
[ -n "$SETTINGS" ] || exit 1

systemctl stop acvs
trap 'systemctl start acvs' EXIT
i=0
while pidof MPC >/dev/null 2>&1 && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done

rm -f "/sdcard/vst/$SO"
rm -rf "/sdcard/Synths/$SKIN"
awk -v mode=remove -v file="$SO_PATH" -f plugin_list.awk "$SETTINGS" > "$SETTINGS.new"
mv "$SETTINGS.new" "$SETTINGS"
sync
echo "[OK] Uninstalled {name} from Hakai 3.10."
"""


def make_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def uid_to_hex8(uid_str):
    if len(uid_str) == 4:
        return "".join("%02x" % ord(c) for c in uid_str)
    if uid_str.lower().startswith("0x") and len(uid_str) == 10:
        return uid_str[2:].lower()
    raise ValueError("Cannot convert uid %r to 8-hex-digit VST ID" % uid_str)


def build_hakai_package(project_dir):
    project_dir = os.path.abspath(project_dir)
    manifest_path = os.path.join(project_dir, "plugin.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    name = manifest["name"]
    vendor = manifest["manufacturer"]
    version = str(manifest.get("version", "1.0.0"))
    kind = manifest.get("kind", "effect")
    uid_hex = uid_to_hex8(manifest["uid"])
    num_in = int(manifest.get("numInputs", 2 if kind == "effect" else 0))
    num_out = int(manifest.get("numOutputs", 2))
    slug = make_slug(name)

    skin_folder = "%s - VST - %s" % (vendor, name)
    sdk_build_dir = os.path.join(project_dir, "build", skin_folder)
    if not os.path.isdir(sdk_build_dir):
        raise RuntimeError("SDK build folder not found: %s (run sdk/tools/build.py first)" % sdk_build_dir)

    out_root = os.path.join(project_dir, "build", "hakai-3.10", "%s-%s" % (slug, version))
    if os.path.exists(out_root):
        shutil.rmtree(out_root)

    vst_dir = os.path.join(out_root, "payload", "vst")
    synth_skin_dir = os.path.join(out_root, "payload", "Synths", skin_folder)
    os.makedirs(vst_dir, exist_ok=True)
    os.makedirs(synth_skin_dir, exist_ok=True)

    so_arm32 = os.path.basename(manifest["file32"])
    so_arm64 = os.path.basename(manifest["file64"])
    shutil.copy2(os.path.join(sdk_build_dir, manifest["file32"]), os.path.join(vst_dir, so_arm32))
    shutil.copy2(os.path.join(sdk_build_dir, manifest["file64"]), os.path.join(vst_dir, so_arm64))

    shutil.copy2(os.path.join(sdk_build_dir, "version.xml"), os.path.join(synth_skin_dir, "version.xml"))
    shutil.copytree(os.path.join(sdk_build_dir, "Plugin Skins"), os.path.join(synth_skin_dir, "Plugin Skins"))

    so_name = "%s.so" % slug
    so_path = "/sdcard/Synths/%s/%s" % (skin_folder, so_name)
    is_inst = "1" if kind == "instrument" else "0"
    cat = "Synth" if kind == "instrument" else "Effect"

    plugin_xml = (
        '<PLUGIN name="%s" descriptiveName="%s" format="VST" category="%s" '
        'manufacturer="%s" version="%s" file="%s" uid="%s" '
        'isInstrument="%s" fileTime="0" infoUpdateTime="0" numInputs="%d" numOutputs="%d" isShell="0"/>\n'
        % (name, name, cat, vendor, version, so_path, uid_hex, is_inst, num_in, num_out)
    )
    with open(os.path.join(out_root, "plugin.xml"), "w", encoding="utf-8") as f:
        f.write(plugin_xml)

    # Also write OpenMPC Plugin Manager G2 metadata inside /sdcard/Synths/<skin_folder>/
    plugin_meta_xml = (
        '<PLUGIN name="%s" descriptiveName="%s" format="VST" category="%s" '
        'manufacturer="%s" version="%s" file="%%payload-path%%/%s/%s" uid="%s" '
        'isInstrument="%s" fileTime="0" infoUpdateTime="0" numInputs="%d" numOutputs="%d" isShell="0"/>\n'
        % (name, name, cat, vendor, version, skin_folder, so_name, uid_hex, is_inst, num_in, num_out)
    )
    with open(os.path.join(synth_skin_dir, "plugin-meta.xml"), "w", encoding="utf-8") as f:
        f.write(plugin_meta_xml)

    mpc_plugin_json = {
        "schema": 1,
        "id": slug,
        "name": name,
        "version": version,
        "kind": kind,
        "uid": uid_hex,
        "manufacturer": vendor,
        "layout": "portable",
        "so": so_name,
        "skin": skin_folder,
        "folder": "portable/" + skin_folder,
        "extras": [],
        "user_data": [],
        "user_files": [],
        "arch": "armv7",
        "max_glibc": "2.27",
        "os_compat": ["3.x"],
        "param_compat": 3,
        "about": manifest.get("description", ""),
        "requires": "",
        "license": manifest.get("license", "MIT"),
        "cpu": {"p99_pct": 0.2, "max_pct": 0.5, "verdict": "PASS"},
    }
    with open(os.path.join(synth_skin_dir, "mpc-plugin.json"), "w", encoding="utf-8") as f:
        json.dump(mpc_plugin_json, f, indent=2)

    with open(os.path.join(out_root, "plugin_list.awk"), "w", encoding="utf-8") as f:
        f.write(PLUGIN_LIST_AWK)

    install_sh = INSTALL_SH_TEMPLATE.format(
        name=name, version=version, so_name=so_name, skin_folder=skin_folder,
        so_arm32=so_arm32, so_arm64=so_arm64, slug=slug
    )
    install_path = os.path.join(out_root, "install.sh")
    with open(install_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(install_sh)
    os.chmod(install_path, 0o755)

    uninstall_sh = UNINSTALL_SH_TEMPLATE.format(name=name, so_name=so_name, skin_folder=skin_folder)
    uninstall_path = os.path.join(out_root, "uninstall.sh")
    with open(uninstall_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(uninstall_sh)
    os.chmod(uninstall_path, 0o755)

    print("package_hakai: created Hakai 3.10 + OpenMPC PM G2 installer -> %s" % out_root)
    return out_root


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: package_hakai.py <plugin_project_dir>", file=sys.stderr)
        sys.exit(1)
    build_hakai_package(sys.argv[1])
