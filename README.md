# FlashKit

Flash and manage a **second Android device** over USB-OTG from a **rooted phone**, with a native Android UI
(Termux:GUI): sideload ROM zips, flash images, GSI flashing in stages, erase, reboot, APK install, a file manager
with a zip/tar viewer (look inside, extract all or only what you need, edit zips in place), and a console.

> Made by [@Shivraj_editx](https://t.me/Shivraj_editx)

## Install (one command, in Termux)

```sh
pkg install -y git && git clone https://github.com/shivrajtode/flashkit && bash flashkit/install.sh
```

That installs everything it needs: python, adb/fastboot, termux-api, the `termuxgui` Python library, and the
**Termux:GUI** and **Termux:API** apps - picked so that their **signature matches your Termux**
(Android only lets them work together when they come from the same source). With root they're installed silently;
otherwise the normal Android installer opens.

Then open the app any time with:

```sh
flashkit
```

## Update

After new changes are published to this repo:

```sh
flashkit update
```

## Commands

| Command | What it does |
|---|---|
| `flashkit` | open the app |
| `flashkit update` | pull the latest version from GitHub and refresh dependencies |
| `flashkit setup` | (re)install packages, plugins and the command |
| `flashkit plugins` | check / install Termux:GUI and Termux:API (`--check` only reports) |
| `flashkit doctor` | check root, tools, plugins, storage permission |
| `flashkit cli` | the older text-menu version |
| `flashkit uninstall` | remove the command and settings |

## Needs

* **Termux from F-Droid or GitHub** (not Google Play) - the plugins must come from the same place.
* A **rooted** phone (Magisk / KernelSU). Root is used only for the adb/fastboot calls.
* An OTG cable to the device you want to flash.

## If something doesn't work

* *"Signature" message:* install Termux and its plugins from the same place (F-Droid **or** GitHub), then `flashkit plugins`.
* *Termux restarted while installing plugins:* normal on some phones - open Termux again and type `flashkit`.
* *Root denied:* allow Termux in your root manager, then `flashkit doctor`.
* Logs: `~/flashkit_gui.log`, plugin install log: `~/.flashkit/plugin-install.log`.

## Publishing your own changes

```sh
mkdir -p ~/flashkit-upload && cd ~/flashkit-upload && unzip -o "$(ls -t ~/storage/downloads/flashkit-repo*.zip | head -n 1)" && cd flashkit && bash tools/publish.sh
```

`tools/publish.sh` finds your GitHub username, creates the repo if needed, commits and pushes.

## License

MIT - see `LICENSE`.
