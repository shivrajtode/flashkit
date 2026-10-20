#!/system/bin/sh
# FlashKit - root adb/fastboot toolkit for Termux, dialog UI
# Run as: su -c sh /path/to/flashkit.sh

PREFIX=/data/data/com.termux/files/usr
HOME=/data/data/com.termux/files/home
TMPDIR="$PREFIX/tmp"
PATH="$PREFIX/bin:$PATH:/system/bin:/system/xbin:/sbin"
export PREFIX HOME TMPDIR PATH
mkdir -p "$TMPDIR" 2>/dev/null

if [ "$(id -u)" != "0" ]; then
  echo "Not root. Run it as: su -c sh $0"
  exit 1
fi

VERSION="v11"
STORAGE_ROOT="/storage/emulated/0"
[ -d "$STORAGE_ROOT" ] || STORAGE_ROOT="/sdcard"

# Dark theme: box background (blue) is deliberately different from the
# plain black screen behind it, so the dialog reads as a raised "card"
# instead of just outlined text on the same backdrop. Selected items/
# buttons use white text (not black) for contrast that holds up
# regardless of exactly how a given terminal renders ANSI magenta.
DIALOGRC="$TMPDIR/flashkit.dialogrc"
export DIALOGRC
cat > "$DIALOGRC" << 'RC_EOF'
use_shadow = ON
use_colors = ON
screen_color = (WHITE,BLACK,OFF)
shadow_color = (BLACK,BLACK,ON)
dialog_color = (WHITE,BLUE,OFF)
title_color = (MAGENTA,BLUE,ON)
border_color = (MAGENTA,BLUE,OFF)
border2_color = (MAGENTA,BLUE,OFF)
button_active_color = (WHITE,MAGENTA,ON)
button_inactive_color = (WHITE,BLUE,OFF)
button_key_active_color = (WHITE,MAGENTA,ON)
button_key_inactive_color = (MAGENTA,BLUE,ON)
button_label_active_color = (WHITE,MAGENTA,ON)
button_label_inactive_color = (WHITE,BLUE,OFF)
inputbox_color = (WHITE,BLUE,OFF)
inputbox_border_color = (CYAN,BLUE,OFF)
inputbox_border2_color = (CYAN,BLUE,OFF)
menubox_color = (WHITE,BLUE,OFF)
menubox_border_color = (MAGENTA,BLUE,OFF)
menubox_border2_color = (MAGENTA,BLUE,OFF)
item_color = (WHITE,BLUE,OFF)
item_selected_color = (WHITE,MAGENTA,ON)
tag_color = (CYAN,BLUE,ON)
tag_selected_color = (WHITE,MAGENTA,ON)
tag_key_color = (CYAN,BLUE,ON)
tag_key_selected_color = (WHITE,MAGENTA,ON)
position_indicator_color = (CYAN,BLUE,ON)
uarrow_color = (CYAN,BLUE,ON)
darrow_color = (CYAN,BLUE,ON)
RC_EOF

# Size dialog boxes against the ACTUAL terminal, not dialog's own "0 0"
# auto-size. Recomputed every call in case the on-screen keyboard
# opening/closing changed the visible rows.
dsize() {
  sz=$(stty size 2>/dev/null)
  rows=${sz%% *}
  cols=${sz##* }
  case "$rows" in ''|*[!0-9]*) rows=30 ;; esac
  case "$cols" in ''|*[!0-9]*) cols=50 ;; esac
  DH=$((rows - 2)); [ "$DH" -lt 10 ] && DH=10
  DW=$((cols - 2)); [ "$DW" -lt 30 ] && DW=30
  MH=$((DH - 8)); [ "$MH" -lt 3 ] && MH=3
}

HAVE_TIMEOUT=0
command -v timeout >/dev/null 2>&1 && HAVE_TIMEOUT=1

# Runs a command entirely inside the themed dialog UI: a "please wait"
# box while it runs, then its captured output (plus exit code) in a
# scrollable box you dismiss with OK - no more dropping to plain
# unstyled terminal text for the actual flashing/pushing/etc.
run_boxed() {
  title="$1"; secs="$2"; shift 2
  dsize
  dialog --title "$title" --infobox "Working, please wait..." "$DH" "$DW"
  outfile="$TMPDIR/flashkit_out"
  if [ "$HAVE_TIMEOUT" = "1" ]; then
    timeout "$secs" "$@" > "$outfile" 2>&1
  else
    "$@" > "$outfile" 2>&1
  fi
  rc=$?
  {
    echo ""
    if [ "$rc" = "0" ]; then echo "[done - exit code 0]"; else echo "[exit code $rc]"; fi
  } >> "$outfile"
  dsize
  dialog --title "$title" --textbox "$outfile" "$DH" "$DW"
}

install_deps() {
  clear
  echo "FlashKit $VERSION - checking packages (adb, fastboot, dialog)..."
  pkg update -y >/dev/null 2>&1
  pkg install -y android-tools dialog >/dev/null 2>&1
  if command -v adb >/dev/null 2>&1 && command -v fastboot >/dev/null 2>&1 && command -v dialog >/dev/null 2>&1; then
    echo "OK - adb, fastboot and dialog are all present."
  else
    echo "WARNING: something is still missing after install (check your internet), continuing anyway..."
    sleep 2
  fi
}

# Lightweight auto-fix: reinstalls adb/fastboot if missing, clears a
# stuck server, and re-opens USB node permissions.
fix_adb() {
  if ! command -v adb >/dev/null 2>&1 || ! command -v fastboot >/dev/null 2>&1; then
    pkg install -y android-tools >/dev/null 2>&1
  fi
  adb kill-server >/dev/null 2>&1
  rm -f "$HOME"/.android/adb*.lock 2>/dev/null
  chmod -R 666 /dev/bus/usb/*/* 2>/dev/null
  adb start-server >/dev/null 2>&1
  sleep 1
}

info() { dsize; dialog --title "$1" --msgbox "$2" "$DH" "$DW"; }
# Big full-width Yes/No rows instead of dialog's tiny "< OK >  <Cancel>".
yesno() {
  dsize
  r=$(dialog --title "Confirm" --menu "$1" "$DH" "$DW" 4 \
    yes "Yes, continue" no "No, cancel" \
    3>&1 1>&2 2>&3)
  [ "$r" = "yes" ]
}
inputbox() { dsize; dialog --title "$1" --inputbox "$2" "$DH" "$DW" "$3" 3>&1 1>&2 2>&3; }

# Own tap-friendly file browser (numbered --menu) instead of dialog's
# --fselect, which only updates its path box on keyboard navigation.
# Confined to $STORAGE_ROOT and below. Always has a big "Cancel" row of
# its own, not just the small Cancel button.
browse_pick() {
  dir="$1"
  title="$2"
  TAB="$(printf '\t')"
  while true; do
    map="$TMPDIR/flashkit_map"
    : > "$map"
    n=0
    set --
    if [ "$dir" != "$STORAGE_ROOT" ]; then
      n=$((n+1))
      printf '%s%s%s\n' "$n" "$TAB" "$(dirname "$dir")" >> "$map"
      set -- "$@" "$n" ".. (up)"
    fi
    for entry in "$dir"/*/; do
      [ -d "$entry" ] || continue
      name="${entry%/}"; name="${name##*/}"
      n=$((n+1))
      printf '%s%s%s\n' "$n" "$TAB" "$entry" >> "$map"
      set -- "$@" "$n" "$name/"
    done
    for entry in "$dir"/*; do
      [ -f "$entry" ] || continue
      name="${entry##*/}"
      n=$((n+1))
      printf '%s%s%s\n' "$n" "$TAB" "$entry" >> "$map"
      set -- "$@" "$n" "$name"
    done
    set -- "$@" 0 "Type a path instead" C "Cancel / back"
    dsize
    sel=$(dialog --title "$title" --menu "$dir" "$DH" "$DW" "$MH" "$@" 3>&1 1>&2 2>&3)
    if [ -z "$sel" ] || [ "$sel" = "C" ]; then
      rm -f "$map"
      return 1
    fi
    if [ "$sel" = "0" ]; then
      rm -f "$map"
      p=$(inputbox "$title" "Full path:" "$dir/")
      [ -z "$p" ] && return 1
      if [ -f "$p" ]; then echo "$p"; return 0; fi
      if [ -d "$p" ]; then dir="${p%/}"; continue; fi
      return 1
    fi
    picked=$(grep "^${sel}${TAB}" "$map" | cut -f2-)
    rm -f "$map"
    if [ -d "$picked" ]; then
      dir="${picked%/}"
    else
      echo "$picked"
      return 0
    fi
  done
}

pick_file() {
  browse_pick "$STORAGE_ROOT" "$1"
}

adb_state() {
  adb devices 2>/dev/null | grep -v "^List of devices" | grep -v "^$" | head -n1 | cut -f2
}

fastboot_serial() {
  fastboot devices 2>/dev/null | head -n1 | cut -f1
}

adb_present() {
  s=$(adb_state)
  if [ -z "$s" ]; then
    fix_adb
    s=$(adb_state)
  fi
  echo "$s"
}

ensure_adb_device() {
  s=$(adb_state)
  if [ "$s" != "device" ]; then
    fix_adb
    s=$(adb_state)
  fi
  [ "$s" = "device" ]
}

ensure_fastboot_device() {
  d=$(fastboot_serial)
  if [ -z "$d" ]; then
    fix_adb
    d=$(fastboot_serial)
  fi
  [ -n "$d" ]
}

# Lists a directory ON THE CONNECTED DEVICE via adb shell (D:name / F:name).
remote_ls() {
  rdir="$1"
  qdir=$(printf '%s' "$rdir" | sed "s/'/'\\\\''/g")
  adb shell "cd '$qdir' 2>/dev/null && for f in * .*; do [ \"\$f\" = '.' ] && continue; [ \"\$f\" = '..' ] && continue; [ -e \"\$f\" ] || continue; if [ -d \"\$f\" ]; then echo D:\"\$f\"; else echo F:\"\$f\"; fi; done" 2>/dev/null
}

# Same browsing UX as browse_pick, against the device's own filesystem.
# mode=file returns on landing on a file; mode=dir also lets you confirm
# whichever folder you're sitting in. Confined to its starting root.
remote_browse() {
  dir="$1"
  title="$2"
  mode="$3"
  root="$dir"
  TAB="$(printf '\t')"
  while true; do
    map="$TMPDIR/flashkit_rmap"
    : > "$map"
    n=0
    set --
    if [ "$dir" != "$root" ]; then
      n=$((n+1))
      printf '%s%s%s\n' "$n" "$TAB" "$(dirname "$dir")" >> "$map"
      set -- "$@" "$n" ".. (up)"
    fi
    while IFS= read -r line; do
      case "$line" in
        D:*|F:*) : ;;
        *) continue ;;
      esac
      kind="${line%%:*}"
      name="${line#*:}"
      n=$((n+1))
      case "$dir" in
        */) full="${dir}${name}" ;;
        *) full="${dir}/${name}" ;;
      esac
      printf '%s%s%s%s%s\n' "$n" "$TAB" "$full" "$TAB" "$kind" >> "$map"
      if [ "$kind" = "D" ]; then
        set -- "$@" "$n" "$name/"
      else
        set -- "$@" "$n" "$name"
      fi
    done <<REMOTE_EOF
$(remote_ls "$dir")
REMOTE_EOF
    if [ "$mode" = "dir" ]; then
      set -- "$@" U "[use this folder]"
    fi
    set -- "$@" 0 "Type a path instead" C "Cancel / back"
    dsize
    sel=$(dialog --title "$title" --menu "$dir (on device)" "$DH" "$DW" "$MH" "$@" 3>&1 1>&2 2>&3)
    if [ -z "$sel" ] || [ "$sel" = "C" ]; then
      rm -f "$map"
      return 1
    fi
    if [ "$sel" = "U" ]; then
      rm -f "$map"
      echo "$dir"
      return 0
    fi
    if [ "$sel" = "0" ]; then
      rm -f "$map"
      p=$(inputbox "$title" "Full path on device:" "$dir/")
      [ -z "$p" ] && return 1
      echo "$p"
      return 0
    fi
    row=$(grep "^${sel}${TAB}" "$map")
    rm -f "$map"
    rpath=$(printf '%s' "$row" | cut -f2)
    rkind=$(printf '%s' "$row" | cut -f3)
    if [ "$rkind" = "D" ]; then
      dir="$rpath"
    else
      echo "$rpath"
      return 0
    fi
  done
}

act_flash() {
  if ! ensure_fastboot_device; then
    info "No device" "No fastboot device found.

Check the OTG cable/adapter and that the target is in bootloader mode."
    return
  fi
  dsize
  part=$(dialog --title "Partition" --menu "Pick a partition:" "$DH" "$DW" "$MH" \
    boot "" recovery "" vbmeta "" vbmeta_a "" vbmeta_b "" system "" vendor "" dtbo "" other "type a name" \
    3>&1 1>&2 2>&3)
  [ -z "$part" ] && return
  if [ "$part" = "other" ]; then
    part=$(inputbox "Partition" "Partition name:" "")
    [ -z "$part" ] && return
  fi
  f=$(pick_file "Image for $part")
  [ -z "$f" ] && return
  yesno "Flash this file:
$f

to partition: $part ?" || return
  run_boxed "Flashing $part" 300 fastboot flash "$part" "$f"
}

act_sideload() {
  f=$(pick_file "ROM/update zip")
  [ -z "$f" ] && return
  s=$(adb_state)
  if [ "$s" != "sideload" ] && [ "$s" != "device" ]; then
    fix_adb
    s=$(adb_state)
  fi
  case "$s" in
    sideload) : ;;
    device)
      yesno "Reboot target to recovery and sideload:
$f" || return
      dsize
      dialog --title "Sideload" --infobox "Rebooting to sideload mode..." "$DH" "$DW"
      adb reboot sideload >/dev/null 2>&1
      sleep 5
      ;;
    *)
      info "No device" "adb state: ${s:-none}

Put the target at 'Apply update > Apply from ADB' in recovery, or boot it normally with USB debugging authorized, then retry."
      return
      ;;
  esac
  run_boxed "Sideloading $(basename "$f")" 300 adb sideload "$f"
}

act_install() {
  if ! ensure_adb_device; then
    info "No device" "No adb device found.

Check the OTG cable/adapter and USB-debugging authorization on the target."
    return
  fi
  f=$(pick_file "APK to install")
  [ -z "$f" ] && return
  run_boxed "Installing $(basename "$f")" 120 adb install "$f"
}

act_reboot() {
  s=$(adb_state)
  d=$(fastboot_serial)
  if [ -z "$s" ] && [ -z "$d" ]; then
    fix_adb
    s=$(adb_state)
    d=$(fastboot_serial)
  fi
  dsize
  case "$s" in
    device|recovery|sideload)
      t=$(dialog --title "Reboot (adb - $s)" --menu "Reboot target to:" "$DH" "$DW" "$MH" \
        system "" bootloader "" recovery "" sideload "" \
        3>&1 1>&2 2>&3)
      [ -z "$t" ] && return
      if [ "$t" = "system" ]; then run_boxed "Rebooting" 20 adb reboot; else run_boxed "Rebooting to $t" 20 adb reboot "$t"; fi
      ;;
    *)
      if [ -n "$d" ]; then
        t=$(dialog --title "Reboot (fastboot)" --menu "Reboot target to:" "$DH" "$DW" "$MH" \
          system "" bootloader "" recovery "" fastbootd "" \
          3>&1 1>&2 2>&3)
        [ -z "$t" ] && return
        case "$t" in
          system) run_boxed "Rebooting" 20 fastboot reboot ;;
          fastbootd) run_boxed "Rebooting to fastbootd" 20 fastboot reboot fastboot ;;
          *) run_boxed "Rebooting to $t" 20 fastboot reboot "$t" ;;
        esac
      else
        info "No device" "adb state: ${s:-none}, no fastboot device either."
      fi
      ;;
  esac
}

# Three separate gates before anything gets erased: two escalating
# yes/no warnings, then a type-the-exact-name confirmation.
act_erase() {
  if ! ensure_fastboot_device; then
    info "No device" "No fastboot device found."
    return
  fi
  part=$(inputbox "Erase" "Partition to ERASE:" "")
  [ -z "$part" ] && return
  yesno "WARNING 1 of 3

You are about to ERASE the '$part' partition.

This cannot be undone." || return
  yesno "WARNING 2 of 3

Erasing '$part' can leave this device unable to boot unless you reflash it right after.

Still continue?" || return
  confirm_text=$(inputbox "WARNING 3 of 3" "Type the partition name exactly to confirm:" "")
  if [ "$confirm_text" != "$part" ]; then
    info "Cancelled" "Text didn't match '$part' - erase cancelled, nothing was touched."
    return
  fi
  run_boxed "Erasing $part" 60 fastboot erase "$part"
}

# Stays in the picked tool (adb or fastboot) after running a command,
# looping back to the same command list instead of dropping to the main
# menu - pick "Back" when you're actually done.
act_raw() {
  dsize
  tool=$(dialog --title "Raw command" --menu "Tool:" "$DH" "$DW" "$MH" adb "" fastboot "" 3>&1 1>&2 2>&3)
  [ -z "$tool" ] && return
  while true; do
    dsize
    if [ "$tool" = "fastboot" ]; then
      cmdline=$(dialog --title "fastboot command" --menu "Pick one (or Custom):" "$DH" "$DW" "$MH" \
        "devices" "list connected devices" \
        "getvar all" "show all bootloader variables" \
        "flashing unlock" "unlock bootloader (newer devices)" \
        "flashing lock" "lock bootloader (newer devices)" \
        "flashing unlock_critical" "unlock critical partitions" \
        "oem unlock" "unlock bootloader (older devices)" \
        "oem lock" "lock bootloader (older devices)" \
        "continue" "resume normal boot" \
        "reboot" "reboot to system" \
        "reboot bootloader" "reboot to bootloader" \
        "reboot recovery" "reboot to recovery" \
        "reboot fastboot" "reboot to fastbootd" \
        "set_active a" "switch active slot to A" \
        "set_active b" "switch active slot to B" \
        "Custom" "type your own command" \
        "Back" "back to main menu" \
        3>&1 1>&2 2>&3)
    else
      cmdline=$(dialog --title "adb command" --menu "Pick one (or Custom):" "$DH" "$DW" "$MH" \
        "devices" "list connected devices" \
        "shell getprop ro.build.version.release" "Android version" \
        "shell getprop ro.product.model" "device model" \
        "reboot" "reboot to system" \
        "reboot bootloader" "reboot to bootloader" \
        "reboot recovery" "reboot to recovery" \
        "reboot sideload" "reboot to sideload" \
        "wait-for-device" "block until a device shows up" \
        "Custom" "type your own command" \
        "Back" "back to main menu" \
        3>&1 1>&2 2>&3)
    fi
    [ -z "$cmdline" ] && return
    [ "$cmdline" = "Back" ] && return
    if [ "$cmdline" = "Custom" ]; then
      cmdline=$(inputbox "Raw $tool command" "Command, no '$tool' prefix:" "")
      [ -z "$cmdline" ] && continue
    fi
    if [ "$tool" = "adb" ]; then
      s=$(adb_present)
      case "$s" in
        device|recovery|sideload) : ;;
        "")
          info "No device" "No adb device found at all.

Check the OTG cable/adapter."
          continue
          ;;
        *)
          info "Not ready" "adb state: $s

If that says unauthorized, accept the USB-debugging prompt on the target's screen."
          continue
          ;;
      esac
    else
      ensure_fastboot_device || { info "No device" "No fastboot device found."; continue; }
    fi
    run_boxed "Running: $tool $cmdline" 300 "$tool" $cmdline
  done
}

# Browse the connected device's own storage, and push/pull files between
# it and this phone.
act_devfiles() {
  s=$(adb_present)
  case "$s" in
    device|recovery) : ;;
    *)
      info "No device" "adb state: ${s:-none}

Need a normal or recovery adb connection to browse the device's files."
      return
      ;;
  esac
  while true; do
    dsize
    op=$(dialog --title "Device files" --menu "What do you want to do?" "$DH" "$DW" "$MH" \
      push "Copy a file from THIS phone TO the device" \
      pull "Copy a file from the device TO this phone" \
      browse "Just look around on the device" \
      back "Back to main menu" \
      3>&1 1>&2 2>&3)
    case "$op" in
      push)
        f=$(pick_file "File to push to the device")
        [ -z "$f" ] && continue
        dest=$(remote_browse "/sdcard" "Pick a destination FOLDER on the device" "dir")
        [ -z "$dest" ] && continue
        remote_target="${dest%/}/$(basename "$f")"
        yesno "Push:
$f

to the device at:
$remote_target ?" || continue
        run_boxed "Pushing $(basename "$f")" 300 adb push "$f" "$remote_target"
        ;;
      pull)
        rf=$(remote_browse "/sdcard" "Pick a file on the device to pull" "file")
        [ -z "$rf" ] && continue
        local_dest="$STORAGE_ROOT/Download/$(basename "$rf")"
        yesno "Pull:
$rf

from the device, saving to:
$local_dest ?" || continue
        mkdir -p "$STORAGE_ROOT/Download" 2>/dev/null
        run_boxed "Pulling $(basename "$rf")" 300 adb pull "$rf" "$local_dest"
        ;;
      browse)
        remote_browse "/sdcard" "Device storage" "dir" >/dev/null
        ;;
      back|"") return ;;
    esac
  done
}

act_list() {
  out="-- adb --
$(adb devices 2>/dev/null)

-- fastboot --
$(fastboot devices 2>/dev/null)"
  info "Connected devices" "$out"
}

# Full diagnostic pass: binaries, raw USB enumeration, adb state, fastboot
# state, all in one report.
act_fix() {
  msg=""
  if command -v adb >/dev/null 2>&1 && command -v fastboot >/dev/null 2>&1; then
    msg="adb/fastboot: installed"
  else
    msg="adb/fastboot: was missing, reinstalling now"
  fi
  fix_adb
  usb="$(ls /dev/bus/usb/*/* 2>/dev/null)"
  if [ -z "$usb" ]; then
    msg="$msg
USB: nothing on the OTG port at all - try a different cable/adapter"
  else
    msg="$msg
USB nodes: $usb"
  fi
  s=$(adb_state)
  d=$(fastboot_serial)
  msg="$msg
adb state: ${s:-none}
fastboot device: ${d:-none}"
  info "Diagnostics" "$msg"
}

main_menu() {
  dsize
  dialog --backtitle "FlashKit $VERSION" --title "Main menu" --menu "Choose an action:" "$DH" "$DW" "$MH" \
    1 "Flash a partition/image (fastboot)" \
    2 "Sideload a ROM/zip (adb)" \
    3 "Install an APK (adb)" \
    4 "Reboot target" \
    5 "Erase a partition [destructive]" \
    6 "Raw adb/fastboot command" \
    7 "Fix ADB/USB detection" \
    8 "List connected devices" \
    9 "Device files (push/pull/browse)" \
    0 "Exit" \
    3>&1 1>&2 2>&3
}

install_deps
fix_adb
while true; do
  choice=$(main_menu)
  case "$choice" in
    1) act_flash ;;
    2) act_sideload ;;
    3) act_install ;;
    4) act_reboot ;;
    5) act_erase ;;
    6) act_raw ;;
    7) act_fix ;;
    8) act_list ;;
    9) act_devfiles ;;
    0|"") clear; stty sane 2>/dev/null; exit 0 ;;
    *) : ;;
  esac
done
