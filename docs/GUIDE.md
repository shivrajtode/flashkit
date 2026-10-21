# 📖 FlashKit guide

Plain steps for everything FlashKit does. New here? Install first: see the [README](../README.md#-install-in-3-steps).

**Contents:** [Home & status](#1-the-home-screen) · [Sideload](#2-sideload-a-rom-zip) · [Flash an image](#3-flash-an-image) · [GSI](#4-gsi-flash-in-3-stages) · [Recovery fix](#5-recovery-black-screen-fix) · [Erase](#6-erase-a-partition) · [Storage & zips](#7-storage--zip-tools-your-phone) · [Console](#8-console) · [Settings & quitting](#9-settings--quitting)

---

## 1. The Home screen

Plug the second device into your phone with the OTG cable. The card at the top tells you what FlashKit sees:

| Status | Meaning | What you can do |
|---|---|---|
| **adb device** | Android is running, USB debugging is on | Install APK, reboot, Files tab, Console |
| **Recovery / sideload** | The device is in recovery (sideload mode) | Sideload a ROM zip |
| **fastboot (bootloader)** | Bootloader mode | Flash an image, erase, GSI stage 1 |
| **fastbootd (userspace)** | Dynamic-partition fastboot | GSI stage 3, flash `system` / `super` |
| **MediaTek BROM / Preloader, Qualcomm EDL, download modes** | Recognised and explained | These need other tools (SP Flash Tool, mtkclient, EDL tools) - FlashKit tells you which |

Tap **Refresh status** to re-check. If adb/fastboot themselves are crashing, a **Fix connection now** button appears.

> **Tip:** FlashKit checks the device every few seconds using root. You can switch that off in **Settings → Check the device automatically**.

## 2. Sideload a ROM zip

1. Put the target in **recovery → "Apply update from ADB"** (sideload). 
2. Home → **Sideload a ROM zip**.
3. Pick the `.zip` (you can also start from the **Storage** tab: tap the zip → *Sideload this zip to the target*).
4. Read the confirmation. The **install log** switch shows what the recovery says while it installs (live if your recovery allows it, otherwise right after).
5. Watch the output. FlashKit ends with a verdict - *succeeded*, *error* or *can't tell* - based on the recovery's log.

The output screen has **Pause scroll / Jump to end**, **Copy**, **Clear** and **Cancel job**.

## 3. Flash an image

1. Put the target in **bootloader** (fastboot) or **fastbootd**.
2. Home → **Flash an image**.
3. Choose the partition: `boot, recovery, vbmeta, vbmeta_system, dtbo, vendor_boot, init_boot, system, vendor, product, super, userdata`.
4. Pick the `.img` file.
5. Confirm. For `vbmeta` there is a switch to **disable verity/verification**. Risky partitions (bootloader, modem, persist, efs...) make you **type the partition name** first.

## 4. GSI flash in 3 stages

Home → **GSI flash**. Pick one mode:

| Mode | What it does | Keeps your data? |
|---|---|---|
| **Clean flash** | vbmeta → fastbootd → delete `product` → flash `system.img` | No |
| **Dirty flash: system.img** | vbmeta → fastbootd → flash `system.img` | Yes |
| **Dirty flash: super.img** | vbmeta → fastbootd → flash `super.img` | Yes |

Then pick **vbmeta.img** (step 1/2) and the **system.img** or **super.img** (step 2/2). After that it runs by itself:

1. **Stage 1** - flashes vbmeta with verification disabled (in the bootloader).
2. **Stage 2** - reboots the target to **fastbootd** and waits until it is ready.
3. **Stage 3** - flashes the system (clean mode frees space first if needed).

A stage only starts if the previous one succeeded, and every stage has **Cancel**.

## 5. Recovery black-screen fix

Some recoveries show a black screen until the vendor partition is mounted. Put the target in recovery → Home → **Recovery fix**. FlashKit waits for the device and runs the mount for you. (The same command is in **Console → adb**.)

## 6. Erase a partition

Home → **Erase a partition** → three gates: two warnings, then **type the exact partition name**. There is no undo.

## 7. Storage & zip tools (your phone)

The **Storage** tab is a file manager for *your* phone (Internal, Termux and SD card).

**Everyday tasks**
- Tap **Select**, tick items, then **Copy / Move / Rename / Delete / Compress**.
- After Copy or Move, open the destination folder and tap **Paste here**.
- **New folder** makes a folder; the search box filters the list.

**Look inside a zip (without extracting)**
1. Tap a `.zip` (also `.apk`, `.tar`, `.tar.gz`, `.tar.xz`, `.tar.bz2`, `.gz`, `.xz`, `.bz2`).
2. Browse its folders. Tap a file → **View (without extracting)** to read it.

**Extract**
- **Extract all** - everything, into a folder named after the zip (or wherever you choose).
- **Only what you need** - tap **Select**, tick the files/folders, tap **Extract**.
- Choose *where to*, what to do **if a name already exists** (keep both / replace / skip), keep or flatten the folder structure, and give a **password** if the zip asks.
- Unsafe paths (`../`) are blocked, free space is checked first, and a bad file never stops the rest.

**Edit a zip in place** (zip files only)
- Inside a zip: **Select** → **Delete / Rename / Move / Copy**, or **Add files** from your phone. 
- Edits don't recompress your data, so even a big ROM zip is quick, and the original is left untouched if anything fails.

**Make a zip** - Select files/folders → **Compress** → pick a name and level (Store / Fast / Normal / Maximum).

## 8. Console

**Console** has ready-made adb and fastboot commands (device info, logs, reboots, *Mount vendor*, *Recovery's log*...) and a box for your own command. Each result opens on its own screen with **Copy**.

## 9. Settings & quitting

| Setting | What it does |
|---|---|
| Auto-fix connection problems | Restarts adb or re-detects when the link drops |
| Keep the screen on while a job runs | Stops the phone sleeping mid-flash |
| Show the recovery's install log | Live log while sideloading |
| Check the device automatically | Turn off to check only when you tap Refresh |
| **Quit completely (force stop)** | Stops jobs, stops adb and force-stops Termux:GUI so nothing runs in the background |

Need more help? See [Help & troubleshooting](../README.md#-help--troubleshooting) or open an [issue](https://github.com/shivrajtode/flashkit/issues).
