# 📝 Changelog

## g9.5 - A real "uninstall completely"
- `flashkit uninstall` now also offers to delete the app folder itself and to uninstall Termux:GUI/Termux:API (root, or Android's own uninstall screen without root).
- `flashkit uninstall -y` does all of that in one command, no prompts.

## g9.4 - README cleanup
- Removed the maintainer-only publishing section from the public README - the same steps are already in `tools/publish.sh`'s own header comment, for personal use only.

## g9.3 - Repo makeover
- New README with banner, badges, a 3-step install and a pop-up cheat-sheet.
- New icons and artwork (`assets/`), a social preview image, and a full user guide (`docs/GUIDE.md`).
- Issue templates that ask for `flashkit doctor`; `publish.sh` also sets the repo description and topics.

## g9.2 - FlashKit menu
- `flashkit` opens a menu: **Start FlashKit**, **Run manually**, Update, plugins, Doctor, autostart.
- `start flashkit` and `flashkit start`; a *FlashKit* entry for the Termux:Widget home-screen widget.
- After Android restarts Termux during setup, the menu opens by itself once.
- Grants "Display over other apps" to Termux and Termux:GUI automatically (root).
- Fixed a crash caused by an old root-owned install log.

## g9.1 - Plugin installer fixes
- Plugins are found through root (Android hides other apps from a normal Termux user).
- A working plugin is never reinstalled; if Android refuses a download, the next source is tried.
- No-root path: Android's own installer opens, you tap **Install**.
- `flashkit check`.

## g9 - GitHub install
- `git clone` + `install.sh` sets up everything; Termux:GUI / Termux:API are chosen to match your Termux's signature.
- `flashkit update`, `flashkit plugins`, `flashkit doctor`, `tools/publish.sh`.
- **App:** one persistent root shell instead of a new `su` every few seconds, calmer device checks (and an off switch), a lighter file manager, **Quit completely**.

## App history
- **g8** - Storage tab: look inside zips/tars, extract all or selected, edit zips in place, compress, copy/move/rename/delete.
- **g7** - Cancel stops the whole job; live install log while sideloading.
- **g6 and earlier** - icon UI, GSI in 3 stages, USB mode detection (BROM / EDL / fastbootd), adb/fastboot repair, recovery black-screen fix, job output screen, file browser and console.
