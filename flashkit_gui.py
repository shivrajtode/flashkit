#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FlashKit GUI - native Android UI (Termux:GUI) for flashing a SECOND device
with adb/fastboot over OTG from a rooted phone.

Run as your normal Termux user (NOT through su):

    python flashkit_gui.py

Root is only used for the actual adb/fastboot calls (su -c ...). Anything that
goes wrong is shown on screen and written to ~/flashkit_gui.log.
"""
import os
import re
import sys
import base64
import json
import time
import queue
import shlex
import shutil
import itertools
import select
import zipfile
import tarfile
import gzip
import bz2
import lzma
import zlib
import struct
import errno
import codecs
import threading
import subprocess
import traceback

VERSION = "g9"
HOME = os.path.expanduser("~")
CREDIT_URL = "https://t.me/Shivraj_editx"
BASE = "/data/data/com.termux/files"
# su's shell starts with a bare environment: without these, adb can't find
# its binaries by name and crashes trying to mkdir '//.android'.
ENV = ('export HOME="%s/home" TMPDIR="%s/usr/tmp" '
       'PATH="%s/usr/bin:$PATH:/system/bin:/system/xbin:/sbin"; ' % (BASE, BASE, BASE))
STORAGE_ROOT = os.environ.get("FLASHKIT_ROOT") or (
    "/storage/emulated/0" if os.path.isdir("/storage/emulated/0") else "/sdcard")
DOWNLOAD = os.path.join(STORAGE_ROOT, "Download")
REMOTE_ROOT = os.environ.get("FLASHKIT_REMOTE_ROOT") or "/sdcard"
LOGFILE = os.path.join(HOME, "flashkit_gui.log")
CFGFILE = os.path.join(HOME, ".flashkit_gui.json")
PROBE_EVERY = float(os.environ.get("FLASHKIT_PROBE_EVERY", "3"))
tg = None  # the termuxgui module, imported in main() after bootstrap


# ------------------------------------------------------------------ basics
def rgb(v):
    """0xRRGGBB -> signed 32-bit ARGB int (what the plugin expects)."""
    v = (v & 0xFFFFFF) | 0xFF000000
    return v - (1 << 32) if v >= (1 << 31) else v


BG = rgb(0x101318)
SURF = rgb(0x1B1F27)
SURF2 = rgb(0x2A303C)
TEXT = rgb(0xE3E2E6)
MUTED = rgb(0x9AA3B2)
ACCENT = rgb(0xAFC6FF)
PRIM_BG = rgb(0x2D4275)
PRIM_TX = rgb(0xDDE6FF)
PILL = rgb(0x3A4560)
GOOD = rgb(0x8EE2A0)
WARN = rgb(0xFFCB6B)
BAD = rgb(0xFF8A80)
BAD_BG = rgb(0x5F1A17)
BAD_TX = rgb(0xFFDAD6)

KINDS = {  # button kind -> (background, text colour)
    "tonal": (SURF2, TEXT),
    "primary": (PRIM_BG, PRIM_TX),
    "danger": (BAD_BG, BAD_TX),
    "flat": (SURF, ACCENT),
    "nav": (BG, MUTED),
    "navsel": (PILL, PRIM_TX),
}


_ICON_CACHE = {}


def icon_bytes(name):
    """PNG bytes of an embedded icon, or None if it isn't there."""
    if name not in _ICON_CACHE:
        data = _ICON_B64.get(name)
        _ICON_CACHE[name] = base64.b64decode(data) if data else None
    return _ICON_CACHE[name]


class Pill:
    """A pill-shaped button: [round cap][coloured Button][round cap]. Looks like one rounded button
    but answers to the same few calls as a plain Button (text, visibility, width...)."""

    def __init__(self, box, btn):
        self.box, self.btn, self.id = box, btn, btn.id

    def setvisibility(self, v):
        safe(self.box.setvisibility, v)
        safe(self.btn.setvisibility, v)

    def setwidth(self, w):
        safe(self.box.setwidth, w)

    def __getattr__(self, name):  # settext, settextcolor, setgravity, ... go to the Button itself
        return getattr(self.btn, name)


def log(msg):
    try:
        if os.path.exists(LOGFILE) and os.path.getsize(LOGFILE) > 300000:
            os.replace(LOGFILE, LOGFILE + ".old")
        with open(LOGFILE, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def safe(fn, *a, **k):
    """Cosmetic calls must never take the app down."""
    try:
        return fn(*a, **k)
    except Exception as e:
        log("safe(%s) failed: %r" % (getattr(fn, "__name__", fn), e))
        return None


def shq(s):
    return shlex.quote(str(s))


def router(fn):
    """Mark an entry function that only decides WHICH screen to show and then
    forwards to it with go(). Routers must never sit on the back stack: Back
    would re-run them and they'd bounce straight forward again, so the screen
    just redraws and you can never leave."""
    fn._router = True
    return fn


def human(n):
    if n is None:
        return ""
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d B" % n) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def load_cfg():
    cfg = {"autofix": True, "keepon": True, "sideload_log": True, "autodetect": True}
    try:
        with open(CFGFILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CFGFILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# ------------------------------------------------------------- running things
class RootShell:
    """ONE long-lived root shell for the quick commands (device checks, listings...), so Superuser is asked once
    instead of on every check. Each command runs in a subshell with stdin closed, so an `exit` or a command that
    reads input can't break it. A command that hangs is killed and the shell restarted."""

    def __init__(self):
        self.lock = threading.Lock()
        self.p = None

    def close(self):
        p, self.p = self.p, None
        if p is not None:
            for fn in (p.stdin.close, p.kill):
                try:
                    fn()
                except Exception:
                    pass

    def _start(self):
        self.p = subprocess.Popen(["su"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        self.p.stdin.write((ENV + "\n").encode())

    def run(self, cmd, timeout=30):
        with self.lock:
            tag = "@@FK" + os.urandom(4).hex() + "@@"
            pat = re.compile(re.escape(("\n" + tag).encode()) + rb"(\d+)\n")
            script = "( " + cmd + "\n) </dev/null 2>&1\nFK_RC=$?\nprintf '\\n" + tag + "%d\\n' \"$FK_RC\"\n"
            for attempt in (0, 1):
                try:
                    if self.p is None or self.p.poll() is not None:
                        self._start()
                    self.p.stdin.write(script.encode())
                    self.p.stdin.flush()
                    fd, buf, end = self.p.stdout.fileno(), b"", time.time() + timeout
                    while True:
                        m = pat.search(buf)
                        if m:
                            return int(m.group(1)), buf[:m.start()].decode("utf-8", "replace").strip()
                        left = end - time.time()
                        if left <= 0:
                            self.close()
                            return 124, "(timed out after %ss)" % timeout
                        if select.select([fd], [], [], min(left, 1.0))[0]:
                            chunk = os.read(fd, 65536)
                            if not chunk:
                                raise EOFError()
                            buf += chunk
                except FileNotFoundError:
                    self.p = None
                    return 127, "su not found - is this phone rooted?"
                except (EOFError, OSError):
                    self.close()
                    if attempt == 1:
                        return 1, "the root shell stopped answering"
            return 1, "the root shell stopped answering"


ROOT = RootShell()


def su_run(cmd, timeout=30):
    """Blocking root command -> (returncode, merged output), through the one persistent root shell."""
    return ROOT.run(cmd, timeout)


def run_user(cmd, timeout=25):
    """Blocking command as the normal Termux user (no su) -> (rc, merged output)."""
    try:
        p = subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ss)" % timeout
    except Exception as e:
        return 1, "error: %s" % e


# What it looks like when adb/fastboot themselves can't even start (as opposed to
# starting fine and finding no device): a linker/library error, a crash, a missing file.
TOOL_ERR = re.compile(r"CANNOT LINK EXECUTABLE|cannot locate symbol|library \".+\" not found|"
                      r"inaccessible or not found|: not found|Segmentation fault|Exec format error|"
                      r"cannot execute|unexpected e_type|bad ELF", re.I)


def tool_error(text):
    """First line of `text` that says a program failed to start, else ''."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line and TOOL_ERR.search(line):
            return line[:200]
    return ""


def tools_broken(st):
    te = st.get("tool_err") or {}
    return bool(te.get("adb") or te.get("fastboot"))


_JOB_SEQ = itertools.count(1)

# Cancel = stop the WHOLE job: its shell script, loops, sleeps and every adb/fastboot it started.
# Every job runs with FLASHKIT_JOB=<token> in its environment (children inherit it), so we find
# them through /proc and kill them all. pkill on the command-line marker and the old blanket
# adb/fastboot kill are only fallbacks for systems where that scan finds nothing.
_KILL = r"""
TOK=@@TOK@@
FOUND=0
kill_job() {
  for f in $(grep -la "FLASHKIT_JOB=$TOK" /proc/[0-9]*/environ 2>/dev/null); do
    p=${f#/proc/}; p=${p%/environ}
    [ "$p" = "$$" ] && continue
    kill -9 "$p" 2>/dev/null && FOUND=$((FOUND+1))
  done
}
kill_job; sleep 0.3; kill_job
pkill -9 -f "FLASHKIT_JO[B]_$TOK" 2>/dev/null
sleep 0.2; kill_job
if [ "$FOUND" -eq 0 ]; then pkill -9 -x fastboot 2>/dev/null; pkill -9 -x adb 2>/dev/null; fi
exit 0
"""


def kill_script(token):
    return _KILL.replace("@@TOK@@", token)


class Job:
    """A command streamed line by line from a worker thread - as root (su) by
    default, or as the normal Termux user with root=False (package repair).
    cancel() really stops it (see _KILL)."""

    def __init__(self, title, script, timeout=600, post=None, root=True):
        self.title, self.script, self.timeout, self.post, self.root = title, script, timeout, post, root
        self.q = queue.Queue()
        self.rc = None
        self.cancelled = False
        self.cancel_started = False
        self.ended = False
        self.proc = None
        self.token = "fk%x_%d" % (os.getpid(), next(_JOB_SEQ))

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _finish(self):
        if not self.ended:
            self.ended = True
            self.q.put(None)

    def cancel(self):
        """Stop the job for real. -> False if it is already being stopped."""
        if self.cancel_started:
            return False
        self.cancel_started = self.cancelled = True
        threading.Thread(target=self._kill, daemon=True).start()
        return True

    def _kill(self):
        for _ in range(25):  # it may not have launched its process yet
            if self.proc is not None:
                break
            time.sleep(0.2)
        try:
            (su_run if self.root else run_user)(kill_script(self.token), 15)
        except Exception:
            log("kill script failed: " + traceback.format_exc())
        p = self.proc
        for _ in range(25):  # let it wind down, then end our side of the pipe
            if p is None or p.poll() is not None:
                return
            time.sleep(0.2)
        try:
            p.kill()
        except Exception:
            pass
        time.sleep(2.0)
        if not self.ended:  # even that didn't unblock the reader: free the screen anyway
            if self.rc is None:
                self.rc = -9
            self._finish()

    def _run(self):
        marker = "FLASHKIT_JOB_" + self.token
        wrapped = ("export FLASHKIT_JOB=%s; if command -v timeout >/dev/null 2>&1; then timeout %d sh -c %s %s; "
                   "else sh -c %s %s; fi" % (self.token, self.timeout, shq(self.script), marker,
                                             shq(self.script), marker))
        try:
            if self.root:
                p = subprocess.Popen(["su", "-c", ENV + wrapped], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
            else:
                p = subprocess.Popen(["sh", "-c", wrapped], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
            self.proc = p
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            fd = p.stdout.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                self.q.put(dec.decode(chunk))
            p.wait()
            if self.rc is None:
                self.rc = p.returncode
        except Exception as e:
            self.q.put("\n[could not run: %s]\n" % e)
            self.rc = 1
        if self.post and not self.cancelled:
            try:
                self.q.put("\n" + self.post())
            except Exception as e:
                self.q.put("\n[analysis failed: %s]\n" % e)
        self._finish()


class LogBuf:
    """Turns raw tool output into lines; \\r rewrites the current line so progress bars don't flood
    the screen. Keeps up to `keep` lines (that is what Copy copies) but only the last `tail` are
    shown, so a long job never makes the screen slow."""

    def __init__(self, keep=6000, tail=350):
        self.lines, self.cur, self.cr = [], "", False
        self.keep, self.tail, self.dropped = keep, tail, 0

    def feed(self, s):
        if "\r" not in s and not self.cr:  # fast path: plain text
            parts = s.split("\n")
            self.cur += parts[0]
            for part in parts[1:]:
                self.lines.append(self.cur)
                self.cur = part
        else:
            for ch in s:
                if ch == "\r":
                    self.cr = True
                elif ch == "\n":
                    self.lines.append(self.cur)
                    self.cur, self.cr = "", False
                else:
                    if self.cr:
                        self.cur, self.cr = "", False
                    self.cur += ch
        if len(self.lines) > self.keep:
            extra = len(self.lines) - self.keep
            del self.lines[:extra]
            self.dropped += extra

    def _all(self):
        return self.lines + ([self.cur] if self.cur else [])

    def text(self):
        lines = self._all()
        if len(lines) > self.tail:
            hidden = len(lines) - self.tail + self.dropped
            lines = ["... %d earlier lines hidden (Copy copies everything) ..." % hidden] + lines[-self.tail:]
        return "\n".join(lines)

    def full_text(self):
        head = ["[%d earlier lines were dropped]" % self.dropped] if self.dropped else []
        return "\n".join(head + self._all())


def copy_to_clipboard(text):
    """-> ('clipboard', None) or ('file', path) or ('fail', error). Uses Termux:API's
    termux-clipboard-set when it works; otherwise saves a text file in Download."""
    exe = shutil.which("termux-clipboard-set")
    if exe:
        try:
            p = subprocess.run([exe], input=text.encode("utf-8", "replace"), timeout=8,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if p.returncode == 0:
                return "clipboard", None
        except Exception:
            pass
    try:
        os.makedirs(DOWNLOAD, exist_ok=True)
        path = os.path.join(DOWNLOAD, "flashkit_output_%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "w") as f:
            f.write(text)
        return "file", path
    except Exception as e:
        return "fail", str(e)


PCT = re.compile(r"(\d{1,3})%")


# --------------------------------------------------------- device detection
def uinfo_script(root="/sys/bus/usb/devices"):
    """Shell snippet: one DEV| line per real (non-hub) USB device, then IF| lines for its
    interfaces (class/subclass/protocol). Tells us WHAT is plugged in even when adb and
    fastboot can't talk to it (MediaTek BROM/Preloader, MTP-only, black-screen recovery...)."""
    return (
        'for d in %s/*; do [ -f "$d/idVendor" ] || continue; v=$(cat "$d/idVendor" 2>/dev/null); '
        '[ "$v" = 1d6b ] && continue; '
        'echo "DEV|$(basename "$d")|$v:$(cat "$d/idProduct" 2>/dev/null)|$(cat "$d/manufacturer" 2>/dev/null)'
        '|$(cat "$d/product" 2>/dev/null)"; '
        'for i in "$d":*; do [ -f "$i/bInterfaceClass" ] && echo "IF|$(cat "$i/bInterfaceClass")/'
        '$(cat "$i/bInterfaceSubClass")/$(cat "$i/bInterfaceProtocol")"; done; '
        'done' % root)


PROBE_SCRIPT = ('chmod -R 666 /dev/bus/usb/*/* 2>/dev/null; '
                'echo @@USB; ls /dev/bus/usb/*/* 2>/dev/null; '
                'echo @@ADB; adb devices 2>&1; '
                'FBOUT=$(fastboot devices 2>&1); echo @@FB; echo "$FBOUT"; '
                'if printf "%s\n" "$FBOUT" | grep -Eq "^[^[:space:]]+[[:space:]]+[Ff]astboot"; then '
                'echo @@FBU; if command -v timeout >/dev/null 2>&1; then timeout 8 fastboot getvar is-userspace 2>&1; '
                'else fastboot getvar is-userspace 2>&1; fi; fi; '
                'echo @@UINFO; ' + uinfo_script() + '; echo @@END')
ADB_LINE = re.compile(r"^(\S+)\s+(device|offline|unauthorized|recovery|sideload|bootloader|"
                      r"rescue|authorizing|connecting|detached|host|no permissions)\b")
FB_LINE = re.compile(r"^(\S+)\s+[Ff]astboot\w*")
USB_LINE = re.compile(r"^/dev/bus/usb/(\d+)/(\d+)$")


def probe():
    rc, out = su_run(PROBE_SCRIPT, timeout=20)
    st = {"root": True, "err": "", "usb": 0, "adb": [], "fb": [], "model": "",
          "fixed": "", "tool_err": {}, "fastbootd": None, "usbinfo": [], "t": time.time()}
    if "@@END" not in out:
        st["root"] = False
        st["err"] = out.strip() or "su gave no output (rc=%s)" % rc
        return st
    sec = None
    raw = {"ADB": [], "FB": []}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("@@"):
            sec = line[2:]
            continue
        if sec in raw:
            raw[sec].append(line)
        if sec == "USB":
            m = USB_LINE.match(line)
            if m and int(m.group(2)) > 1:  # dev 001 is the root hub
                st["usb"] += 1
        elif sec == "ADB":
            m = ADB_LINE.match(line)
            if m:
                st["adb"].append((m.group(1), m.group(2)))
        elif sec == "FB":
            m = FB_LINE.match(line)
            if m:
                st["fb"].append(m.group(1))
        elif sec == "FBU":
            m = re.search(r"is-userspace:\s*(yes|no)", line, re.I)
            if m:
                st["fastbootd"] = m.group(1).lower() == "yes"
        elif sec == "UINFO":
            if line.startswith("DEV|"):
                parts = line.split("|")
                st["usbinfo"].append({"bus": parts[1] if len(parts) > 1 else "",
                                      "id": parts[2] if len(parts) > 2 else "",
                                      "mfr": parts[3] if len(parts) > 3 else "",
                                      "product": parts[4] if len(parts) > 4 else "", "ifs": []})
            elif line.startswith("IF|") and st["usbinfo"]:
                st["usbinfo"][-1]["ifs"].append(line[3:])
    st["tool_err"] = {"adb": tool_error("\n".join(raw["ADB"])), "fastboot": tool_error("\n".join(raw["FB"]))}
    return st


USB_HINTS = {
    "brom": "Looks like the target is in MediaTek BROM mode, the chip's lowest-level USB mode. adb and "
            "fastboot can't talk to it. Unplug it, power it fully off, then boot it to recovery or the "
            "bootloader with its key combo. Flashing from BROM needs a BROM tool (SP Flash Tool or "
            "mtkclient), not adb/fastboot.",
    "preloader": "Looks like the target is in MediaTek Preloader mode. Every MediaTek device shows this for a "
                 "moment at boot; if it STAYS like this it isn't booting any further. adb and fastboot can't "
                 "talk to it. Power it fully off, then boot it to recovery or the bootloader.",
    "edl": "Qualcomm EDL (emergency download) mode. adb and fastboot can't talk to it.",
    "samsungdl": "Samsung Download mode. adb and fastboot can't talk to it. Use Odin on a PC, or hold the "
                 "keys to reboot it.",
    "adb": "The target exposes an ADB interface but adb isn't listing it. Tap Fix connection now. If the target "
           "shows 'Allow USB debugging?', accept it. A custom recovery with a black screen can take a minute to "
           "bring adb up: wait, then Refresh.",
    "fastboot": "The target exposes a fastboot interface but fastboot isn't listing it. Tap Fix connection now, "
                "or re-plug it.",
    "mtp": "The target is in file-transfer/charging mode, not adb. Turn on USB debugging (booted Android) or "
           "boot it to recovery/bootloader. A recovery that shows nothing may not have started adb yet: wait a "
           "minute, then Refresh.",
    "other": "adb and fastboot can't talk to it. Turn on USB debugging on the target, or boot it to "
             "bootloader/recovery.",
}


def usb_kind(dev):
    """-> (kind, label) for one entry of st['usbinfo']."""
    vid, _, pid = (dev.get("id") or "").partition(":")
    ifs = dev.get("ifs") or []
    name = " ".join(x for x in (dev.get("mfr"), dev.get("product")) if x)
    low = (name + " " + (dev.get("id") or "")).lower()
    if vid == "0e8d":
        if pid == "0003" or "brom" in low:
            return "brom", "MediaTek BROM mode"
        if pid in ("2000", "2001") or "preloader" in low or "vcom" in low:
            return "preloader", "MediaTek Preloader mode"
    if vid == "05c6" and pid == "9008":
        return "edl", "Qualcomm EDL mode"
    if vid == "04e8" and pid == "685d":
        return "samsungdl", "Samsung Download mode"
    if "ff/42/01" in ifs:
        return "adb", "ADB interface"
    if "ff/42/03" in ifs:
        return "fastboot", "fastboot interface"
    if any(i.startswith("06/") for i in ifs) or "mtp" in low:
        return "mtp", "file-transfer (MTP) mode"
    return "other", name or "unknown USB mode"


def usb_headline(st):
    """('USB: <what it is> (vid:pid)', kind) for the first real USB device, or None."""
    devs = st.get("usbinfo") or []
    if not devs:
        return None
    kind, label = usb_kind(devs[0])
    head = "USB: %s (%s)" % (label, devs[0].get("id", "?"))
    if len(devs) > 1:
        head += "  +%d more" % (len(devs) - 1)
    return head, kind


def describe(st):
    """(headline, colour, hint) for a probe result."""
    if not st.get("root", True):
        return ("Root access missing", BAD,
                "Allow superuser for Termux in Magisk/KernelSU, then tap Refresh. " + st.get("err", ""))
    adb, fb = st["adb"], st["fb"]
    if tools_broken(st) and not (adb or fb):
        te = st["tool_err"]
        names = [t for t in ("adb", "fastboot") if te.get(t)]
        which = " and ".join(names)
        what = ("The %s programs in Termux crash the moment they launch" if len(names) > 1
                else "The %s program in Termux crashes the moment it launches") % which
        return ("%s can't start" % which, BAD,
                what + " (a library error), so nothing can be detected. Your cable and the target are not "
                "the problem. Tap Repair adb/fastboot.")
    if fb:
        if st.get("fastbootd") is True:
            return ("Fastbootd  \u00b7  " + fb[0], GOOD,
                    "Userspace fastboot (fastbootd): this is where logical partitions like system, product "
                    "and vendor can be deleted and flashed.")
        return ("Fastboot  \u00b7  " + fb[0], GOOD,
                "Bootloader mode: flash, erase, unlock and fastboot commands are available.")
    if adb:
        ser, s = adb[0]
        if s == "device":
            return ("ADB  \u00b7  " + ser, GOOD,
                    "Booted normally: sideload, install, file transfer and reboot are available.")
        if s == "sideload":
            return ("ADB sideload  \u00b7  " + ser, WARN, "Waiting for a zip - use Sideload.")
        if s == "recovery":
            return ("Recovery  \u00b7  " + ser, WARN,
                    "In recovery: file browsing and reboot work. For a ROM zip pick "
                    "'Apply update > ADB' on the target.")
        if s == "unauthorized":
            return ("Unauthorized  \u00b7  " + ser, WARN,
                    "Accept the 'Allow USB debugging?' prompt on the target's screen.")
        if s == "offline":
            return ("ADB offline  \u00b7  " + ser, WARN, "Reconnecting automatically...")
        return ("ADB %s  \u00b7  %s" % (s, ser), WARN, "")
    if st["usb"] > 0:
        uh = usb_headline(st)
        if uh:
            return (uh[0], WARN, USB_HINTS[uh[1]])
        return ("USB device seen, not answering", WARN,
                "It shows up on USB but neither adb nor fastboot replies. Turn on USB debugging "
                "on the target, or boot it to bootloader/recovery.")
    return ("No device connected", MUTED,
            "Plug the target into the OTG adapter (use a data cable, not charge-only).")


REPAIR_INFO = (
    "adb/fastboot can't start on this phone: Termux's own adb/fastboot programs fail with a library "
    "error. That isn't caused by your cable or the target.\n\n"
    "This will:\n"
    "1. refresh Termux's package lists (needs internet)\n"
    "2. upgrade Termux's packages so the libraries match\n"
    "3. reinstall adb, fastboot and the C++ runtime\n"
    "4. check that they start\n\n"
    "It can take several minutes. Only run it on the phone where adb/fastboot is broken. If it works "
    "afterwards, close FlashKit and open it again.")

REPAIR_SCRIPT = r"""
echo "== 1/4: refreshing package lists (needs internet) =="
if ! pkg update -y; then
  echo "!! Could not reach Termux's package servers. Check the internet connection and run this again."
  exit 1
fi
echo "== 2/4: upgrading Termux packages so libraries and tools match =="
apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade \
  || echo "!! the upgrade reported a problem - carrying on"
echo "== 3/4: reinstalling adb, fastboot and the C++ runtime =="
apt-get -y install --reinstall android-tools libc++ \
  || echo "!! the reinstall reported a problem - carrying on"
echo "== 4/4: checking that they start =="
adb version 2>&1 | head -n 3
fastboot --version 2>&1 | head -n 2
exit 0
"""

TOOLS_CHECK = 'adb version 2>&1 | head -n 3; echo @@; fastboot --version 2>&1 | head -n 3'


def tool_health(root=True):
    """Can adb and fastboot start? -> {'adb': error line or '', 'fastboot': ..., 'raw': text}."""
    rc, out = su_run(TOOLS_CHECK, 25) if root else run_user(TOOLS_CHECK)
    a, _, f = out.partition("@@")
    return {"adb": tool_error(a), "fastboot": tool_error(f), "raw": out}


def tools_verdict():
    """After the repair: test as normal user AND as root, and explain what it means."""
    out = ["-------- result --------"]
    u, r = tool_health(root=False), tool_health(root=True)
    ubad, rbad = u["adb"] or u["fastboot"], r["adb"] or r["fastboot"]
    if not ubad and not rbad:
        out.append("OK adb and fastboot start fine now (normal user and root).")
        out.append("Close FlashKit and open it again so it picks up the repaired files.")
        return "\n".join(out)
    err = ubad or rbad
    out.append("X still can't start: " + err)
    if ubad and not rbad:
        out.append("  It fails as the normal Termux user but works as root - an odd mix. "
                   "Send me this screen.")
        return "\n".join(out)
    if rbad and not ubad:
        out.append("  It works as the normal Termux user but fails when run as root, so the problem is "
                   "the root environment, not the packages. Send me this screen.")
        return "\n".join(out)
    m = re.search(r'cannot locate symbol "([^"]+)"', err)
    if m:
        rc, found = run_user("grep -l -a -F -- %s %s/usr/lib/*.so* 2>/dev/null" % (shq(m.group(1)), BASE))
        libs = [os.path.basename(x) for x in found.splitlines() if x.strip()]
        out.append("  Missing symbol: " + m.group(1))
        out.append("  Libraries on this phone that contain it: " + (", ".join(libs[:8]) or "none"))
        if "libc++_shared.so" in libs:
            out.append("  libc++_shared.so has it, so another copy is probably being loaded instead. "
                       "Send me this screen.")
        else:
            out.append("  libc++_shared.so does NOT have it: Termux's C++ library and its adb/fastboot "
                       "build don't match.")
    rc, pol = run_user('apt-cache policy libc++ android-tools 2>/dev/null | grep -E "^[a-z]|Installed|Candidate"')
    if pol:
        out.append("  Package versions:")
        out.extend("    " + x for x in pol.splitlines())
    rc, arch = run_user("uname -m")
    out.append("  CPU: " + (arch or "?"))
    out.append("")
    out.append("This looks like a packaging mismatch inside Termux, not your cable, adapter or the target "
               "(another Termux user reported the same error on a different program on 30 Sep 2026). "
               "What you can do: try again later, or run termux-change-repo in Termux to pick a different "
               "mirror and then Repair again, or use a device where adb/fastboot already works as the host.")
    return "\n".join(out)


_RECOVERY_FIX = r"""
adbx() { @@A@@ "$@"; }
echo "== waiting for the target to show up on adb (up to @@SECS@@ s) =="
i=0; st=""
while [ "$i" -lt @@TRIES@@ ]; do
  st=$(adbx get-state 2>/dev/null)
  case "$st" in recovery|sideload|rescue|device) break ;; esac
  st=""
  [ $((i % 5)) -eq 4 ] && echo "...still waiting"
  sleep @@PAUSE@@; i=$((i+1))
done
if [ -z "$st" ]; then
  echo "!! Nothing showed up on adb. A black-screen recovery can take a minute to start adb."
  echo "   Look at the status line on Home: it says what the USB device actually is."
  echo "   Wait a little, then run this again."
  exit 2
fi
echo "adb sees it: $st"
if [ "$st" = "device" ]; then
  echo "!! The target is booted into Android, not into the custom recovery. Boot it into recovery first."
  exit 3
fi
echo "== mount vendor =="
OUT=$(adbx shell mount -t ext4 /dev/block/mapper/vendor /vendor 2>&1); RC=$?
[ -n "$OUT" ] && echo "$OUT"
if adbx shell mount 2>/dev/null | grep -q " /vendor "; then
  echo "== vendor is mounted =="
  adbx shell "ls /vendor | head -n 8"
  echo "Check the target's screen now. If it is still black, tell me what it does."
  exit 0
fi
echo "!! vendor is still not mounted (mount exit $RC)."
echo "-- what the recovery has in /dev/block/mapper --"
adbx shell "ls -l /dev/block/mapper" 2>&1
exit 1
"""


def recovery_fix_script(adb="adb", tries=45, pause=2):
    """The recovery developer's fix for the black screen (mount vendor), run as soon as the
    target shows up in recovery - no PC needed."""
    vals = {"A": adb, "TRIES": str(tries), "PAUSE": str(pause), "SECS": str(int(tries * pause))}
    return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], _RECOVERY_FIX)


# Sideloading a zip: the host only sees the transfer percentage. The recovery shows its INSTALL
# log (what a custom recovery prints on its own screen) - FlashKit fetches it from /tmp/recovery.log:
#  * live, while the zip installs, IF the recovery lets adb run a shell during sideload (most
#    TWRP/OrangeFox builds don't: they switch adb to a sideload-only mode), else
#  * the moment the recovery is back on adb, with a guess at whether the install worked.
_SIDELOAD = r"""
adbx() { @@A@@ "$@"; }
ZIP=@@ZIP@@
@@REBOOT@@
echo "== sending the zip =="
echo "+ @@A@@ sideload $ZIP"
LAST=-1; NOTED=0; LIVE_SHOWN=0
poll_log() {
  if [ "$LAST" -lt 0 ]; then
    CNT=$(adbx shell "wc -l < /tmp/recovery.log" 2>/dev/null | tr -d '\r ')
    case "$CNT" in
      ''|*[!0-9]*)
        if [ "$NOTED" = 0 ]; then
          NOTED=1
          echo "(this recovery doesn't allow adb shell while it receives a zip, so its install log can't be shown live - you get it the moment it finishes)"
        fi
        return 1 ;;
    esac
    LAST=$CNT
    echo "(live recovery log connected)"
    return 0
  fi
  NEW=$(adbx shell "tail -n +$((LAST+1)) /tmp/recovery.log" 2>/dev/null | tr -d '\r')
  [ -n "$NEW" ] || return 0
  printf '%s\n' "$NEW" | while IFS= read -r l; do echo "[recovery] $l"; done
  LAST=$((LAST + $(printf '%s\n' "$NEW" | wc -l)))
  LIVE_SHOWN=1
  return 0
}
adbx sideload "$ZIP" &
SP=$!
while kill -0 "$SP" 2>/dev/null; do
  @@POLL@@
  sleep 2
done
wait "$SP"; RC=$?
echo "== adb sideload finished (exit $RC) =="
echo "== waiting for the recovery to finish and report (up to @@SECS@@ s) =="
i=0; st=""
while [ "$i" -lt @@TRIES@@ ]; do
  st=$(adbx get-state 2>/dev/null)
  case "$st" in recovery|device) break ;; esac
  st=""
  [ $((i % 10)) -eq 9 ] && echo "...still waiting"
  sleep @@PAUSE@@; i=$((i+1))
done
V=unknown
if [ -z "$st" ]; then
  echo "(the target didn't come back on adb in time - it may have rebooted by itself, or the recovery is still busy. Look at the target.)"
else
  [ "$LAST" -ge 0 ] && poll_log
  RAW=$(adbx shell "tail -n 1500 /tmp/recovery.log" 2>/dev/null | tr -d '\r')
  if [ -z "$RAW" ]; then
    echo "(could not read /tmp/recovery.log from the target - this recovery may not keep one)"
  else
    TOTAL=$(printf '%s\n' "$RAW" | wc -l)
    START=$(printf '%s\n' "$RAW" | grep -n -i -E 'installing zip file|starting adb sideload|starting sideload|update package|installing update|--update_package|starting installation' | grep -v -i 'error' | tail -n 1 | cut -d: -f1)
    if [ -z "$START" ]; then START=1; [ "$TOTAL" -gt 150 ] && START=$((TOTAL-149)); fi
    SEC=$(printf '%s\n' "$RAW" | tail -n +"$START")
    if [ "$LIVE_SHOWN" = 0 ]; then
      echo "== the recovery's own log of the install =="
      printf '%s\n' "$SEC" | tail -n 200 | while IFS= read -r l; do echo "[recovery] $l"; done
    fi
    OKP='script succeeded|Updater process ended with RC=0|complete \(status: 0\)|installation complete|install(ation)? successful|successfully installed|completed with status 0'
    ERRP='script aborted|Updater process ended with ERROR|Error installing zip|complete \(status: [1-9]|Installation aborted|install(ation)? failed|Error flashing|updater process ended with signal|E:Error in'
    SN=$(printf '%s\n' "$SEC" | grep -n -i -E "$OKP" | tail -n 1 | cut -d: -f1)
    EN=$(printf '%s\n' "$SEC" | grep -n -i -E "$ERRP" | tail -n 1 | cut -d: -f1)
    SN=${SN:-0}; EN=${EN:-0}
    if [ "$EN" -gt "$SN" ]; then V=err; elif [ "$SN" -gt 0 ]; then V=ok; fi
    echo "== key lines =="
    printf '%s\n' "$SEC" | grep -i -E '(^|[ ])E:|error|fail|abort|succe|complete|RC=' | tail -n 12 | while IFS= read -r l; do echo "[recovery] $l"; done
    case "$V" in
      ok) echo "== looks like the install SUCCEEDED (a guess from the recovery's log) ==" ;;
      err) echo "== the recovery reported an ERROR while installing (a guess from its log) ==" ;;
      *) echo "== couldn't tell from the log whether it worked - read the lines above ==" ;;
    esac
  fi
fi
if [ "$RC" -ne 0 ]; then exit "$RC"; fi
if [ "$V" = err ]; then exit 3; fi
exit 0
"""

_SIDELOAD_REBOOT = r"""
adbx reboot sideload
echo "Waiting for sideload mode..."
i=0; st=""
while [ "$i" -lt 40 ]; do st=$(adbx get-state 2>/dev/null); [ "$st" = "sideload" ] && break; sleep 1; i=$((i+1)); done
echo "state: $st"
[ "$st" = "sideload" ] || { echo "Target never entered sideload mode."; exit 3; }
"""


def sideload_script(adb, path, reboot=False, live=True, tries=75, pause=2):
    vals = {"A": adb, "ZIP": shq(path), "REBOOT": _SIDELOAD_REBOOT if reboot else "",
            "POLL": "poll_log" if live else ":", "TRIES": str(tries), "PAUSE": str(pause),
            "SECS": str(int(tries * pause))}
    return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], _SIDELOAD)


def dir_script(path):
    return ('cd %s 2>/dev/null || { echo E:cannot-open-folder; exit 0; }; '
            'for f in * .*; do [ "$f" = . ] && continue; [ "$f" = .. ] && continue; '
            '[ -e "$f" ] || continue; if [ -d "$f" ]; then echo D:"$f"; else echo F:"$f"; fi; done'
            ) % shq(path)


def parse_dir(out):
    items, err = [], ""
    for line in out.splitlines():
        if line.startswith("D:"):
            items.append((line[2:], True, None))
        elif line.startswith("F:"):
            items.append((line[2:], False, None))
        elif line.startswith("E:"):
            err = line[2:].replace("-", " ")
    if not items and not err and out.strip():
        err = out.strip().splitlines()[0]
    return items, err


def local_list(path):
    try:
        items = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    d = e.is_dir()
                    size = None if d else e.stat().st_size
                except OSError:
                    d, size = False, None
                items.append((e.name, d, size))
        return items, ""
    except PermissionError:
        pass  # Android hides some folders from apps - root can still read them
    except OSError as e:
        return [], str(e)
    rc, out = su_run(dir_script(path), timeout=15)
    return parse_dir(out)


PRESETS = {
    "fastboot": [
        ("devices", "List connected devices", False),
        ("getvar all", "Show every bootloader variable", False),
        ("getvar current-slot", "Active slot (A/B devices)", False),
        ("getvar is-userspace", "Are we in fastbootd?", False),
        ("oem device-info", "OEM device info", False),
        ("flashing get_unlock_ability", "Can the bootloader be unlocked?", False),
        ("flashing unlock", "Unlock bootloader (WIPES the device)", True),
        ("flashing lock", "Lock bootloader (WIPES the device)", True),
        ("flashing unlock_critical", "Unlock critical partitions", True),
        ("oem unlock", "Unlock bootloader, older devices (WIPES)", True),
        ("oem lock", "Lock bootloader, older devices", True),
        ("set_active a", "Switch active slot to A", True),
        ("set_active b", "Switch active slot to B", True),
        ("continue", "Resume normal boot", False),
        ("reboot", "Reboot to system", False),
        ("reboot bootloader", "Reboot to bootloader", False),
        ("reboot recovery", "Reboot to recovery", False),
        ("reboot fastboot", "Reboot to fastbootd", False),
    ],
    "adb": [
        ("devices -l", "List devices (detailed)", False),
        ("get-state", "Connection state", False),
        ("shell mount -t ext4 /dev/block/mapper/vendor /vendor", "Mount vendor (black-screen recovery fix)", False),
        ("shell tail -n 150 /tmp/recovery.log", "Recovery's log, last 150 lines (what it shows on its screen)", False),
        ("shell getprop ro.product.model", "Device model", False),
        ("shell getprop ro.build.version.release", "Android version", False),
        ("shell getprop ro.build.fingerprint", "Build fingerprint", False),
        ("shell getprop ro.boot.slot_suffix", "Active slot suffix", False),
        ("shell dumpsys battery", "Battery info", False),
        ("shell df -h", "Storage usage", False),
        ("logcat -d -t 200", "Last 200 log lines", False),
        ("remount", "Remount partitions read-write", True),
        ("reboot", "Reboot to system", False),
        ("reboot bootloader", "Reboot to bootloader", False),
        ("reboot recovery", "Reboot to recovery", False),
        ("reboot sideload", "Reboot to sideload", False),
        ("reboot fastboot", "Reboot to fastbootd", False),
    ],
}
FLASH_PARTS = ["boot", "recovery", "vbmeta", "vbmeta_system", "dtbo", "vendor_boot",
               "init_boot", "system", "vendor", "product", "super", "userdata"]
RISKY = {"bootloader", "aboot", "abl", "xbl", "xbl_config", "sbl1", "tz", "hyp", "rpm",
         "modem", "persist", "efs", "modemst1", "modemst2", "fsg", "fsc", "devinfo", "frp",
         "keymaster", "cmnlib", "cmnlib64", "devcfg", "apdp", "msadp", "dsp", "bluetooth"}


GSI_NAMES = {  # mode -> (label used in picker titles, job title)
    "clean": ("Clean GSI", "GSI clean flash"),
    "dirty-system": ("Dirty flash (system)", "Dirty flash: system.img"),
    "dirty-super": ("Dirty flash (super)", "Dirty flash: super.img"),
}

# GSI flashing is three SEPARATE stages so each runs in the mode it needs, and the next one only
# starts after the previous one really worked:
#   1  vbmeta, in the bootloader (plain fastboot)       fastboot --disable-verification flash vbmeta vbmeta.img
#   2  reboot to fastbootd and wait until it answers "is-userspace: yes"
#   3  system (or super) in fastbootd: clean = delete product + flash system (frees more room and
#      retries if it runs out of space); dirty = just flash, nothing deleted
_GSI_COMMON = r"""
T=""; command -v timeout >/dev/null 2>&1 && T="timeout 10"
userspace() { $T @@FB@@ getvar is-userspace 2>&1 | grep -qi 'is-userspace: *yes'; }
# wait_mode userspace|bootloader. fastbootd answers "is-userspace: yes"; a listed device that doesn't
# is in the bootloader (many bootloaders answer FAILED to this variable rather than "no").
wait_mode() {
  i=0
  while [ "$i" -lt @@TRIES@@ ]; do
    if @@FB@@ devices 2>/dev/null | grep -qi fastboot; then
      if userspace; then
        [ "$1" = userspace ] && return 0
      else
        [ "$1" = bootloader ] && return 0
      fi
    fi
    [ $((i % 5)) -eq 4 ] && echo "...still waiting"
    sleep @@PAUSE@@; i=$((i+1))
  done
  return 1
}
"""

_GSI_S1 = r"""
V=@@V@@
echo "== step 1/3: vbmeta, in the bootloader =="
if userspace; then
  echo "The target is in fastbootd, but vbmeta has to be flashed from the bootloader. Going back there first..."
  @@FB@@ reboot bootloader || { echo "!! could not reboot to the bootloader - stopping."; exit 1; }
  sleep 5
  wait_mode bootloader || { echo "!! the target did not come back in the bootloader - stopping."; exit 1; }
  echo "bootloader is up."
fi
echo "+ @@FB@@ --disable-verification flash vbmeta $V"
@@FB@@ --disable-verification flash vbmeta "$V"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "!! vbmeta flash FAILED (exit $RC) - stopping here, nothing else was changed."
  exit "$RC"
fi
echo "== vbmeta flashed =="
exit 0
"""

_GSI_S2 = r"""
echo "== step 2/3: reboot to fastbootd =="
@@FB@@ reboot fastboot || { echo "!! the target refused to reboot to fastbootd - stopping."; exit 1; }
echo "waiting for it to come back as fastbootd (up to @@SECS@@ s)..."
sleep 5
if wait_mode userspace; then
  echo "fastbootd is up."
  exit 0
fi
echo "!! The target did not come back in fastbootd within @@SECS@@ s - stopping here."
echo "   Look at the target's screen. If it shows the bootloader, start the GSI flash again."
exit 1
"""

_GSI_S3_HEAD = r"""
IMG=@@IMG@@
TMPD="${TMPDIR:-/data/local/tmp}"
run_flash() {
  RCF="$TMPD/gsi_rc.$$"; LOG="$TMPD/gsi_log.$$"
  ( @@FB@@ flash "$1" "$2" 2>&1; echo $? > "$RCF" ) | tee "$LOG"
  RC=$(cat "$RCF" 2>/dev/null); [ -n "$RC" ] || RC=1
  rm -f "$RCF"
}
wait_mode userspace || { echo "!! The target is not in fastbootd, so nothing was flashed. Run the GSI flash again."; exit 1; }
"""

_GSI_CLEAN = _GSI_S3_HEAD + r"""
echo "== step 3/3: system, in fastbootd =="
echo "-- delete-logical-partition product --"
@@FB@@ delete-logical-partition product
echo "-- flash system (can take a while for a big GSI) --"
run_flash system "$IMG"
if [ "$RC" -ne 0 ] && grep -qi space "$LOG"; then
  echo "== not enough space - freeing more logical partitions =="
  for p in system_ext system_ext_a system_ext_b product product_a product_b; do
    echo "-- delete-logical-partition $p --"
    @@FB@@ delete-logical-partition "$p" || true
  done
  echo "== retrying system flash =="
  run_flash system "$IMG"
fi
rm -f "$LOG"
if [ "$RC" -eq 0 ]; then
  echo "== ALL DONE. Now on the device: open Recovery, then Factory reset. =="
else
  echo "!! system flash FAILED (exit $RC). Do not boot into it yet - see the messages above."
fi
exit "$RC"
"""

_GSI_DIRTY = _GSI_S3_HEAD + r"""
echo "== step 3/3: dirty flash @@PART@@, in fastbootd (your data is not touched) =="
run_flash @@PART@@ "$IMG"
if [ "$RC" -eq 0 ]; then
  rm -f "$LOG"
  echo "== ALL DONE. Reboot the device normally - nothing was deleted or wiped, no factory reset needed. =="
  exit 0
fi
if grep -qi space "$LOG"; then
  echo "!! Not enough room for this image. If it is a different ROM from the one installed, use the Clean flash instead."
fi
rm -f "$LOG"
echo "!! @@PART@@ flash FAILED (exit $RC). Do not boot into it yet - see the messages above."
exit "$RC"
"""


def gsi_script(stage, mode, vbmeta, image, fb="fastboot", tries=60, pause=2):
    """Shell script for one GSI stage (1 vbmeta / 2 fastbootd / 3 system|super). Single-pass token
    substitution so a file path can never be re-interpreted as a token."""
    dirty = mode != "clean"
    body = {1: _GSI_S1, 2: _GSI_S2, 3: _GSI_DIRTY if dirty else _GSI_CLEAN}[stage]
    vals = {"V": shq(vbmeta), "IMG": shq(image), "FB": fb, "TRIES": str(tries), "PAUSE": str(pause),
            "SECS": str(int(tries * pause)), "PART": "super" if mode == "dirty-super" else "system"}
    return re.sub(r"@@(\w+)@@", lambda m: vals[m.group(1)], _GSI_COMMON + body)


# ====================================================================== archives & files
# Plain Python (no screens): read zips/tars without extracting, safe extraction, file operations, zip
# creation, and in-place zip editing (delete / rename / move / add) that copies untouched entries RAW
# (no recompression), so editing a 1.5 GB ROM zip stays fast.
class ArchiveError(Exception):
    pass


class Cancelled(Exception):
    pass


def check_cancel(ctx):
    if ctx is not None and ctx.cancelled:
        raise Cancelled()


class TaskCtx:
    def __init__(self, job):
        self.job, self._last = job, 0.0

    @property
    def cancelled(self):
        return self.job.cancelled

    def log(self, text):
        self.job.q.put(text + "\n")

    def progress(self, done, total, label="", force=False):
        now = time.time()
        if not force and now - self._last < 0.35:
            return
        self._last = now
        pct = int(done * 100 / total) if total else 0
        self.job.q.put("  %3d%%   %s of %s   %s\n" % (pct, human(done), human(total), label[-60:]))


class TaskJob:
    """A Python function run in a thread, shown on the same live output screen as a Job."""
    root = False

    def __init__(self, title, fn, post=None):
        self.title, self.fn, self.post = title, fn, post
        self.q = queue.Queue()
        self.rc = None
        self.cancelled = self.cancel_started = self.ended = False
        self.proc = None
        self.script = ""
        self.token = "task%d" % next(_JOB_SEQ)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        if self.cancel_started:
            return False
        self.cancel_started = self.cancelled = True
        return True

    def _finish(self):
        if not self.ended:
            self.ended = True
            self.q.put(None)

    def _run(self):
        ctx = TaskCtx(self)
        try:
            rc = self.fn(ctx)
            self.rc = 0 if rc is None else rc
        except Cancelled:
            self.q.put("\nCancelled.\n")
            self.rc = -9
        except Exception as e:
            self.q.put("\n!! %s\n" % (e if isinstance(e, ArchiveError) else "".join(traceback.format_exception_only(type(e), e)).strip()))
            if not isinstance(e, ArchiveError):
                log("task failed: " + traceback.format_exc())
            self.rc = 1
        if self.post and not self.cancelled:
            try:
                self.q.put("\n" + self.post())
            except Exception as e:
                self.q.put("\n[follow-up failed: %s]\n" % e)
        self._finish()


# ---- names and paths ------------------------------------------------------------------------------
def norm_name(name):
    n = name.replace("\x00", "")
    if "\\" in n and "/" not in n:  # zips made by some Windows tools use backslashes
        n = n.replace("\\", "/")
    return n


def safe_parts(name):
    """Path pieces of an archive member that are safe to create under a folder, else None ('..' = unsafe)."""
    parts = []
    for p in norm_name(name).split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return None
        parts.append(p)
    if parts and re.match(r"^[A-Za-z]:$", parts[0]):
        parts = parts[1:]
    return parts or None


_BAD_CHARS = re.compile(r'[\x00-\x1f:*?"<>|\\]')


def fix_component(c):
    """A version of one file name that any Android filesystem accepts (used only if the first try fails)."""
    c = _BAD_CHARS.sub("_", c).rstrip(" .") or "_"
    if len(c.encode("utf-8")) > 200:
        stem, ext = os.path.splitext(c)
        ext = ext[:20]
        keep = 200 - len(ext.encode("utf-8"))
        c = stem.encode("utf-8")[:keep].decode("utf-8", "ignore") + ext
    return c


def unique_path(path):
    if not os.path.lexists(path):
        return path
    base, ext = (path, "") if os.path.isdir(path) else os.path.splitext(path)
    n = 1
    while os.path.lexists("%s (%d)%s" % (base, n, ext)):
        n += 1
    return "%s (%d)%s" % (base, n, ext)


def tmp_beside(path):
    """A short temporary name in the same folder as `path` (can never be too long for the filesystem)."""
    return os.path.join(os.path.dirname(os.path.abspath(path)), ".fk-" + os.urandom(4).hex() + ".tmp")


def free_space(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
    return shutil.disk_usage(p).free


# ---- reading archives -----------------------------------------------------------------------------
ZIP_EXTS = (".zip", ".apk", ".jar", ".aar", ".xapk", ".apks", ".epub", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".kmz", ".whl")
TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".tar.lzma", ".tar.md5")
SINGLE_EXTS = (".gz", ".bz2", ".xz")
TEXT_EXTS = (".txt", ".log", ".prop", ".sh", ".xml", ".json", ".md", ".cfg", ".conf", ".ini", ".rc", ".py", ".csv", ".yml",
             ".yaml", ".html", ".htm", ".js", ".css", ".java", ".kt", ".c", ".h", ".cpp", ".mk", ".bp", ".te", ".fstab",
             ".policy", ".list", ".sha1", ".md5", ".sig")


def archive_kind(name):
    n = name.lower()
    if n.endswith(TAR_EXTS):
        return "tar"
    if n.endswith(ZIP_EXTS):
        return "zip"
    if n.endswith(SINGLE_EXTS):
        return "single"
    return None


def is_texty(name):
    n = name.lower()
    return n.endswith(TEXT_EXTS) or os.path.basename(n) in ("updater-script", "build.prop", "readme", "license")


class Entry:
    __slots__ = ("name", "dir", "size", "csize", "mtime", "enc", "crc", "raw", "link", "order")

    def __init__(self, name, is_dir, size=0, csize=0, mtime=None, enc=False, crc=0, raw=None, link=False, order=0):
        self.name, self.dir, self.size, self.csize, self.mtime = name, is_dir, size, csize, mtime
        self.enc, self.crc, self.raw, self.link, self.order = enc, crc, raw, link, order


class Archive:
    kind = "?"
    editable = False

    def __init__(self, path):
        self.path, self.entries = path, []
        self._tree = self._byname = None

    def close(self):
        pass

    def open_entry(self, e, pwd=None):
        raise NotImplementedError

    def _build(self):
        tree, byname = {"": {}}, {}
        for e in self.entries:
            parts = [p for p in e.name.split("/") if p not in ("", ".")]
            if not parts:
                continue
            byname[e.name] = e
            d = ""
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                kids = tree.setdefault(d, {})
                key = (d + "/" if d else "") + part
                if last and not e.dir:
                    kids[part] = {"name": part, "dir": False, "size": e.size, "csize": e.csize, "count": 1, "entry": e, "key": key}
                else:
                    it = kids.get(part)
                    if it is None or not it["dir"]:
                        it = kids[part] = {"name": part, "dir": True, "size": 0, "csize": 0, "count": 0, "entry": None, "key": key + "/"}
                    if last:
                        it["entry"] = e
                    elif not e.dir:
                        it["size"] += e.size
                        it["csize"] += e.csize
                        it["count"] += 1
                d = key
        self._tree, self._byname = tree, byname

    def listdir(self, prefix=""):
        if self._tree is None:
            self._build()
        kids = self._tree.get(prefix.rstrip("/"), {})
        return sorted(kids.values(), key=lambda it: (not it["dir"], it["name"].lower()))

    def find(self, key):
        if self._tree is None:
            self._build()
        return self._byname.get(key.rstrip("/"))

    def expand(self, keys):
        """Entries covered by the selected keys; a folder brings everything below it."""
        want, dirs = set(), []
        for k in keys:
            if k.endswith("/"):
                dirs.append(k)
            else:
                want.add(k)
        return [e for e in self.entries if e.name in want or any(e.name == d.rstrip("/") or e.name.startswith(d) for d in dirs)]

    def stats(self):
        files = [e for e in self.entries if not e.dir]
        return {"files": len(files), "dirs": len(self.entries) - len(files), "size": sum(e.size for e in files),
                "csize": sum(e.csize for e in files), "encrypted": sum(1 for e in files if e.enc)}


def zip_entry_name(zi):
    """The entry's real name. Zips made by Info-ZIP's `zip` (every Linux/Termux user) store UTF-8 names without the
    UTF-8 flag, so Python reads them as an old DOS code page and shows garbage. If the raw bytes are valid UTF-8
    (or a matching Unicode Path field says so) use that."""
    name = zi.filename
    if zi.flag_bits & 0x800:
        return name
    try:
        raw = zi.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return name
    if raw.isascii():
        return name
    extra, i = zi.extra, 0
    while i + 4 <= len(extra):
        hid, sz = struct.unpack("<HH", extra[i:i + 4])
        blk = extra[i + 4:i + 4 + sz]
        if hid == 0x7075 and len(blk) >= 5 and blk[0] == 1 and struct.unpack("<I", blk[1:5])[0] == (zlib.crc32(raw) & 0xFFFFFFFF):
            try:
                return blk[5:].decode("utf-8")
            except UnicodeDecodeError:
                pass
        i += 4 + sz
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return name


def _zip_mtime(zi):
    try:
        return time.mktime(tuple(zi.date_time) + (0, 0, -1))
    except Exception:
        return None


class ZipArchive(Archive):
    kind, editable = "zip", True

    def __init__(self, path):
        super().__init__(path)
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError) as ex:
            raise ArchiveError("Can't open this as a zip: %s" % ex)
        for i, zi in enumerate(self.zf.infolist()):
            name = norm_name(zip_entry_name(zi)).rstrip("/")
            if not name.strip("/"):
                continue
            self.entries.append(Entry(name, zi.is_dir(), zi.file_size, zi.compress_size, _zip_mtime(zi), bool(zi.flag_bits & 1), zi.CRC, zi, False, i))

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass

    def open_entry(self, e, pwd=None):
        return self.zf.open(e.raw, "r", pwd=pwd)


class TarArchive(Archive):
    kind = "tar"

    def __init__(self, path):
        super().__init__(path)
        try:
            self.tf = tarfile.open(path, "r:*")
            members = self.tf.getmembers()
        except (tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError) as ex:
            raise ArchiveError("Can't open this as a tar archive: %s" % ex)
        for i, m in enumerate(members):
            name = norm_name(m.name).rstrip("/")
            while name.startswith("./"):
                name = name[2:]
            if not name.strip("/") or name == ".":
                continue
            if m.isdir():
                self.entries.append(Entry(name, True, 0, 0, m.mtime, False, 0, m, False, i))
            elif m.isfile():
                self.entries.append(Entry(name, False, m.size, m.size, m.mtime, False, 0, m, False, i))
            elif m.issym() or m.islnk():
                self.entries.append(Entry(name, False, 0, 0, m.mtime, False, 0, m, True, i))

    def close(self):
        try:
            self.tf.close()
        except Exception:
            pass

    def open_entry(self, e, pwd=None):
        f = self.tf.extractfile(e.raw)
        if f is None:
            raise ArchiveError("%s is not a regular file" % e.name)
        return f


class SingleArchive(Archive):
    kind = "single"

    def __init__(self, path):
        super().__init__(path)
        ext = os.path.splitext(path)[1].lower()
        self.opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(ext)
        if not self.opener:
            raise ArchiveError("unsupported file type")
        self.entries.append(Entry(os.path.basename(path)[:-len(ext)] or "data", False, 0, os.path.getsize(path), os.path.getmtime(path), False, 0, None, False, 0))

    def open_entry(self, e, pwd=None):
        return self.opener(self.path, "rb")


def open_archive(path):
    kind = archive_kind(path)
    try:
        if kind == "zip":
            return ZipArchive(path)
        if kind == "tar":
            return TarArchive(path)
        if kind == "single":
            return SingleArchive(path)
        if zipfile.is_zipfile(path):
            return ZipArchive(path)
        if tarfile.is_tarfile(path):
            return TarArchive(path)
    except ArchiveError:
        raise
    except Exception as ex:
        raise ArchiveError("Can't open it: %s" % ex)
    raise ArchiveError("Not a supported archive (zip, tar, tar.gz/xz/bz2, gz, xz, bz2)")


def read_preview(arch, e, limit=256 * 1024, pwd=None):
    """(bytes, truncated) - the start of one entry, read without extracting anything."""
    if e.enc and not pwd:
        raise ArchiveError("this entry is password protected")
    pw = pwd.encode("utf-8") if isinstance(pwd, str) else pwd
    try:
        with arch.open_entry(e, pw) as src:
            data = src.read(limit + 1)
    except RuntimeError as ex:
        raise ArchiveError("wrong password" if "assword" in str(ex) else str(ex))
    except NotImplementedError:
        raise ArchiveError("unsupported compression or encryption")
    except zipfile.BadZipFile as ex:
        raise ArchiveError("corrupt data (%s)" % ex)
    return data[:limit], len(data) > limit


def text_or_hex(data):
    head = data[:4096]
    odd = sum(1 for b in head if b < 9 or 13 < b < 32)
    if b"\x00" in head or (head and odd > len(head) * 0.1):
        lines = []
        for i in range(0, min(len(data), 1024), 16):
            chunk = data[i:i + 16]
            lines.append("%06x  %-47s  %s" % (i, " ".join("%02x" % b for b in chunk), "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)))
        return "(binary data - first %d bytes)\n\n%s" % (min(len(data), 1024), "\n".join(lines))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---- extracting -----------------------------------------------------------------------------------
class Outcome:
    def __init__(self):
        self.files = self.dirs = self.skipped = self.bytes = 0
        self.failed, self.first = [], None


def _prepare_target(dest, parts, is_dir):
    for attempt in (0, 1):
        ps = parts if attempt == 0 else [fix_component(p) for p in parts]
        path = os.path.join(dest, *ps)
        try:
            os.makedirs(path if is_dir else os.path.dirname(path), exist_ok=True)
            return path
        except OSError:
            if attempt == 1:
                raise


def _copy_stream(src, path, ctx, done, total, label):
    tmp = tmp_beside(path)
    try:
        with open(tmp, "wb") as out:
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                done[0] += len(buf)
                if ctx is not None:
                    check_cancel(ctx)
                    ctx.progress(done[0], total, label)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def extract_entries(arch, entries, dest, base="", policy="rename", flatten=False, pwd=None, ctx=None):
    """Extract `entries` of `arch` into `dest` (paths relative to `base`, a folder inside the archive). policy for
    names that already exist: rename (keep both) / overwrite / skip. Never writes outside `dest`, never leaves
    half-written files, keeps going after a bad entry and reports every problem. -> Outcome"""
    res = Outcome()
    ents = sorted(entries, key=lambda e: e.order)
    total = sum(e.size for e in ents if not e.dir)
    if total and free_space(dest) < total + (4 << 20):
        raise ArchiveError("Not enough free space: this needs %s but only %s is free in %s" % (human(total), human(free_space(dest)), dest))
    os.makedirs(dest, exist_ok=True)
    pw = pwd.encode("utf-8") if isinstance(pwd, str) else pwd
    verbose = ctx is not None and len(ents) <= 60
    done = [0]
    on_sd = dest.startswith("/storage") or dest.startswith("/sdcard")
    for e in ents:
        check_cancel(ctx)
        name = e.name
        if base and name == base.rstrip("/"):
            continue
        rel = name[len(base):] if base and name.startswith(base) else name
        parts = safe_parts(rel)
        if parts is None:
            res.failed.append((e.name, "unsafe path (it tries to leave the destination) - skipped"))
            continue
        if flatten:
            if e.dir:
                continue
            parts = parts[-1:]
        try:
            if e.dir:
                _prepare_target(dest, parts, True)
                res.dirs += 1
                continue
            if e.link:
                res.skipped += 1
                if verbose:
                    ctx.log("  skipped link: " + e.name)
                continue
            if e.enc and pw is None:
                res.failed.append((e.name, "password required"))
                continue
            if any(len(p.encode("utf-8")) > 255 for p in parts):
                parts = [fix_component(p) if len(p.encode("utf-8")) > 255 else p for p in parts]
            path = _prepare_target(dest, parts, False)
            if os.path.lexists(path):
                if os.path.isdir(path) or policy == "rename":
                    path = unique_path(path)
                elif policy == "skip":
                    res.skipped += 1
                    if verbose:
                        ctx.log("  skipped (exists): " + e.name)
                    continue
            for attempt in (0, 1):
                try:
                    with arch.open_entry(e, pw) as src:
                        _copy_stream(src, path, ctx, done, total, e.name)
                    break
                except OSError as ex:
                    if attempt == 0 and ex.errno in (errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ):
                        parts = [fix_component(p) for p in parts]
                        path = _prepare_target(dest, parts, False)
                        if os.path.lexists(path) and policy != "overwrite":
                            path = unique_path(path)
                        continue
                    raise
            if e.mtime:
                try:
                    os.utime(path, (e.mtime, e.mtime))
                except OSError:
                    pass
            mode = getattr(e.raw, "mode", None)
            if mode is None and hasattr(e.raw, "external_attr") and getattr(e.raw, "create_system", 0) == 3:
                mode = (e.raw.external_attr >> 16) & 0o7777
            if mode and mode & 0o111 and not on_sd:
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass
            res.files += 1
            res.bytes += e.size
            if res.first is None:
                res.first = os.path.join(dest, parts[0])
            if verbose:
                ctx.log("  extracted: " + e.name)
        except Cancelled:
            raise
        except zipfile.BadZipFile as ex:
            res.failed.append((e.name, "corrupt data (%s)" % ex))
        except RuntimeError as ex:
            res.failed.append((e.name, "wrong password" if "assword" in str(ex) else str(ex)))
        except NotImplementedError:
            res.failed.append((e.name, "unsupported compression or encryption"))
        except (OSError, EOFError, ArchiveError, lzma.LZMAError, tarfile.TarError, zlib.error) as ex:
            res.failed.append((e.name, str(ex) or ex.__class__.__name__))
    if ctx is not None:
        ctx.progress(done[0], total, "", force=True)
    return res


def report_extract(ctx, res, dest):
    ctx.log("")
    ctx.log("== extracted %d file%s (%s) and %d folder%s into %s ==" % (
        res.files, "" if res.files == 1 else "s", human(res.bytes), res.dirs, "" if res.dirs == 1 else "s", dest))
    if res.skipped:
        ctx.log("%d skipped" % res.skipped)
    if res.failed:
        ctx.log("!! %d problem%s:" % (len(res.failed), "" if len(res.failed) == 1 else "s"))
        for name, why in res.failed[:40]:
            ctx.log("   %s  -  %s" % (name, why))
        if len(res.failed) > 40:
            ctx.log("   ... and %d more" % (len(res.failed) - 40))
    return 2 if res.failed else 0


# ---- the zip writer: raw-copies kept entries, compresses new ones, writes a fresh directory --------
class ZipEditError(ArchiveError):
    pass


_NOCOMP = (".zip", ".apk", ".jar", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".mp4", ".mkv", ".avi", ".mov", ".m4a",
           ".aac", ".ogg", ".flac", ".xz", ".gz", ".bz2", ".7z", ".rar", ".br", ".lz4", ".zst", ".heic")


def _dos(dt):
    y, mo, d, h, mi, s = (list(dt) + [0] * 6)[:6]
    return ((max(1980, y) - 1980) << 9) | (max(1, mo) << 5) | max(1, d), (h << 11) | (mi << 5) | (s // 2)


def _strip_zip64(extra):
    out, i = b"", 0
    while i + 4 <= len(extra):
        hid, sz = struct.unpack("<HH", extra[i:i + 4])
        blk = extra[i:i + 4 + sz]
        if hid != 0x0001 and len(blk) == 4 + sz:
            out += blk
        i += 4 + sz
    return out


class ZipWriter:
    def __init__(self, path, comment=b"", limit=0xFFFFFFFF, count_limit=0xFFFF):
        self.path, self.comment, self.limit, self.count_limit = path, comment, limit, count_limit
        self.f = open(path, "wb")
        self.cd, self.names = [], set()

    def abort(self):
        try:
            self.f.close()
        except Exception:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _local(self, ver, flags, method, dtime, ddate, crc, csize, usize, nb, extra):
        self.f.write(struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", ver, flags, method, dtime, ddate, crc, csize, usize, len(nb), len(extra)))
        self.f.write(nb)
        self.f.write(extra)

    def copy_raw(self, fin, zi, name=None, ctx=None, done=None, total=0):
        fin.seek(zi.header_offset)
        lh = fin.read(30)
        if len(lh) < 30 or lh[:4] != b"PK\x03\x04":
            raise ZipEditError("The zip is damaged: bad header for %r" % zi.filename)
        nlen, elen = struct.unpack("<HH", lh[26:30])
        data_at = zi.header_offset + 30 + nlen + elen
        if name is None or name == zi.filename:
            nb = zi.orig_filename.encode("utf-8" if zi.flag_bits & 0x800 else "cp437", "replace")
            flags = zi.flag_bits & ~0x08
            shown = zip_entry_name(zi)
        else:
            nb = name.encode("utf-8")
            flags = (zi.flag_bits & ~0x08 & ~0x800) | (0x800 if any(ord(c) > 127 for c in name) else 0)
            shown = name
        if shown in self.names:
            raise ZipEditError("There would be two entries named %r" % shown)
        self.names.add(shown)
        csize, usize = zi.compress_size, zi.file_size
        z64 = csize >= self.limit or usize >= self.limit
        keep_dd = bool(zi.flag_bits & 0x08 and zi.flag_bits & 0x01)  # ZipCrypto's check byte then comes from the TIME, not the CRC
        if keep_dd:
            flags |= 0x08
        extra = _strip_zip64(zi.extra)
        lextra = extra + (struct.pack("<HHQQ", 1, 16, usize, csize) if z64 else b"")
        ddate, dtime = _dos(zi.date_time)
        ver = max(zi.extract_version or 20, 45 if z64 else 20)
        off = self.f.tell()
        if keep_dd:
            self._local(ver, flags, zi.compress_type, dtime, ddate, 0, 0, 0, nb, extra)
        else:
            self._local(ver, flags, zi.compress_type, dtime, ddate, zi.CRC, 0xFFFFFFFF if z64 else csize, 0xFFFFFFFF if z64 else usize, nb, lextra)
        fin.seek(data_at)
        left = csize
        while left > 0:
            chunk = fin.read(min(1 << 20, left))
            if not chunk:
                raise ZipEditError("The zip is damaged: the data of %r is cut short" % zi.filename)
            self.f.write(chunk)
            left -= len(chunk)
            if ctx is not None:
                check_cancel(ctx)
                if done is not None:
                    done[0] += len(chunk)
                    ctx.progress(done[0], total, shown)
        if keep_dd:
            if z64:
                self.f.write(struct.pack("<4sIQQ", b"PK\x07\x08", zi.CRC, csize, usize))
            else:
                self.f.write(struct.pack("<4sIII", b"PK\x07\x08", zi.CRC, csize, usize))
        self.cd.append(dict(nb=nb, flags=flags, method=zi.compress_type, dtime=dtime, ddate=ddate, crc=zi.CRC, csize=csize, usize=usize,
                            off=off, extra=extra, comment=zi.comment, made=((zi.create_system or 0) << 8) | (zi.create_version or 20),
                            ver=ver, iattr=zi.internal_attr, eattr=zi.external_attr))

    def add_dir(self, arcname, mtime=None):
        name = arcname.strip("/") + "/"
        if name in self.names:
            return
        self.names.add(name)
        nb = name.encode("utf-8")
        flags = 0x800 if any(ord(c) > 127 for c in name) else 0
        ddate, dtime = _dos(time.localtime(mtime or time.time())[:6])
        off = self.f.tell()
        self._local(20, flags, zipfile.ZIP_STORED, dtime, ddate, 0, 0, 0, nb, b"")
        self.cd.append(dict(nb=nb, flags=flags, method=zipfile.ZIP_STORED, dtime=dtime, ddate=ddate, crc=0, csize=0, usize=0, off=off,
                            extra=b"", comment=b"", made=(3 << 8) | 20, ver=20, iattr=0, eattr=(0o40755 << 16) | 0x10))

    def add_file(self, arcname, path, level=6, ctx=None, done=None, total=0):
        arcname = arcname.strip("/")
        if arcname in self.names:
            raise ZipEditError("There would be two entries named %r" % arcname)
        self.names.add(arcname)
        st = os.stat(path)
        nb = arcname.encode("utf-8")
        flags = 0x800 if any(ord(c) > 127 for c in arcname) else 0
        ext = os.path.splitext(arcname)[1].lower()
        method = zipfile.ZIP_STORED if (level == 0 or ext in _NOCOMP) else zipfile.ZIP_DEFLATED
        z64 = st.st_size >= self.limit
        lextra = struct.pack("<HHQQ", 1, 16, 0, 0) if z64 else b""
        ddate, dtime = _dos(time.localtime(st.st_mtime)[:6])
        off = self.f.tell()
        self._local(45 if z64 else 20, flags, method, dtime, ddate, 0, 0xFFFFFFFF if z64 else 0, 0xFFFFFFFF if z64 else 0, nb, lextra)
        comp = zlib.compressobj(max(1, min(9, level)), zlib.DEFLATED, -15) if method == zipfile.ZIP_DEFLATED else None
        crc = usize = csize = 0
        with open(path, "rb") as src:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                usize += len(chunk)
                out = comp.compress(chunk) if comp else chunk
                if out:
                    self.f.write(out)
                    csize += len(out)
                if ctx is not None:
                    check_cancel(ctx)
                    if done is not None:
                        done[0] += len(chunk)
                        ctx.progress(done[0], total, arcname)
        if comp:
            out = comp.flush()
            if out:
                self.f.write(out)
                csize += len(out)
        end = self.f.tell()
        if (usize >= self.limit or csize >= self.limit) and not z64:
            raise ZipEditError("%s grew while it was being added" % path)
        self.f.seek(off + 14)
        self.f.write(struct.pack("<III", crc & 0xFFFFFFFF, 0xFFFFFFFF if z64 else csize, 0xFFFFFFFF if z64 else usize))
        if z64:
            self.f.seek(off + 30 + len(nb) + 4)
            self.f.write(struct.pack("<QQ", usize, csize))
        self.f.seek(end)
        self.cd.append(dict(nb=nb, flags=flags, method=method, dtime=dtime, ddate=ddate, crc=crc & 0xFFFFFFFF, csize=csize, usize=usize,
                            off=off, extra=b"", comment=b"", made=(3 << 8) | (45 if z64 else 20), ver=45 if z64 else 20, iattr=0,
                            eattr=(st.st_mode & 0xFFFF) << 16))

    def add_tree(self, arc_prefix, path, level=6, ctx=None, done=None, total=0, skip=None):
        name = (arc_prefix.strip("/") + "/" if arc_prefix.strip("/") else "") + os.path.basename(path.rstrip("/"))
        if skip and os.path.abspath(path) in skip:
            return
        if os.path.islink(path):
            return
        if os.path.isdir(path):
            self.add_dir(name, os.path.getmtime(path))
            for child in sorted(os.listdir(path)):
                self.add_tree(name, os.path.join(path, child), level, ctx, done, total, skip)
        elif os.path.isfile(path):
            self.add_file(name, path, level, ctx, done, total)

    def close(self):
        f = self.f
        start = f.tell()
        for c in self.cd:
            usize32, csize32, off32, vals = c["usize"], c["csize"], c["off"], []
            if c["usize"] >= self.limit:
                usize32 = 0xFFFFFFFF
                vals.append(c["usize"])
            if c["csize"] >= self.limit:
                csize32 = 0xFFFFFFFF
                vals.append(c["csize"])
            if c["off"] >= self.limit:
                off32 = 0xFFFFFFFF
                vals.append(c["off"])
            extra = c["extra"] + (struct.pack("<HH%dQ" % len(vals), 1, 8 * len(vals), *vals) if vals else b"")
            f.write(struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", c["made"], max(c["ver"], 45) if vals else c["ver"], c["flags"], c["method"],
                                c["dtime"], c["ddate"], c["crc"], csize32, usize32, len(c["nb"]), len(extra), len(c["comment"]), 0, c["iattr"],
                                c["eattr"], off32))
            f.write(c["nb"])
            f.write(extra)
            f.write(c["comment"])
        size, n = f.tell() - start, len(self.cd)
        if n >= self.count_limit or start >= self.limit or size >= self.limit:
            at = f.tell()
            f.write(struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, n, n, size, start))
            f.write(struct.pack("<4sIQI", b"PK\x06\x07", 0, at, 1))
            f.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, min(n, 0xFFFF), min(n, 0xFFFF), min(size, 0xFFFFFFFF), min(start, 0xFFFFFFFF), len(self.comment)))
        else:
            f.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, n, n, size, start, len(self.comment)))
        f.write(self.comment)
        f.close()


def _zname(zi):
    return norm_name(zip_entry_name(zi)).rstrip("/")


def zip_apply(path, mapper, adds=(), level=6, ctx=None, limit=0xFFFFFFFF, count_limit=0xFFFF):
    """Rewrite a zip: mapper(zipinfo) -> list of names to write it under ([] = drop, [its own name] = keep).
    `adds` = [(folder inside the zip, path on disk)]. Writes a temporary file next to the zip and only replaces
    the original when everything worked, so a failure or Cancel never damages your zip."""
    size = os.path.getsize(path)
    add_total = sum(path_size(p) for _, p in adds)
    need = size + add_total + (4 << 20)
    if free_space(os.path.dirname(os.path.abspath(path))) < need:
        raise ArchiveError("Not enough free space to edit this zip: it needs about %s free next to it" % human(need))
    tmp = tmp_beside(path)
    total, done, kept = size + add_total, [0], 0
    with zipfile.ZipFile(path) as zin, open(path, "rb") as fin:
        w = ZipWriter(tmp, zin.comment, limit, count_limit)
        try:
            for zi in zin.infolist():
                for nm in mapper(zi):
                    w.copy_raw(fin, zi, nm, ctx, done, total)
                    kept += 1
            skip = {os.path.abspath(path), os.path.abspath(tmp)}
            for folder, p in adds:
                w.add_tree(folder, p, level, ctx, done, total, skip)
            w.close()
        except BaseException:
            w.abort()
            raise
    try:
        with zipfile.ZipFile(tmp) as z2:
            if len(z2.infolist()) < kept:
                raise ZipEditError("The rewritten zip came out incomplete - your original was left alone")
    except zipfile.BadZipFile as ex:
        os.remove(tmp)
        raise ZipEditError("The rewritten zip didn't check out (%s) - your original was left alone" % ex)
    os.replace(tmp, path)
    return kept


def _under(n, key):
    k = key.rstrip("/")
    return n == k or n.startswith(k + "/")


def zip_delete(path, keys, **kw):
    keys = list(keys)

    def mapper(zi):
        n = _zname(zi)
        return [] if any(_under(n, k) if k.endswith("/") else n == k for k in keys) else [zi.filename]
    return zip_apply(path, mapper, **kw)


def zip_move(path, keys, dest_prefix, copy=False, new_name=None, **kw):
    """Move (or copy) entries inside the zip to a folder (dest_prefix, '' = top), optionally renaming one."""
    keys = list(keys)
    dest_prefix = dest_prefix.strip("/")
    dp = dest_prefix + "/" if dest_prefix else ""
    for k in keys:
        if k.endswith("/") and (dp + "/").startswith(k.rstrip("/") + "/"):
            raise ZipEditError("A folder can't be moved into itself")
    plan = {}
    for k in keys:
        nm = new_name if (new_name and len(keys) == 1) else k.rstrip("/").rsplit("/", 1)[-1]
        plan[k] = dp + nm

    def mapper(zi):
        n = _zname(zi)
        for k, target in plan.items():
            if (_under(n, k) if k.endswith("/") else n == k):
                out = target + n[len(k.rstrip("/")):] + ("/" if zi.filename.endswith("/") or zi.filename.endswith("\\") else "")
                return [zi.filename, out] if copy else [out]
        return [zi.filename]
    return zip_apply(path, mapper, **kw)


def zip_add(path, sources, dest_prefix="", replace=True, **kw):
    """Add files/folders from the phone to the zip under dest_prefix. Same-named entries are replaced."""
    dest_prefix = dest_prefix.strip("/")
    names = {(dest_prefix + "/" if dest_prefix else "") + os.path.basename(p.rstrip("/")) for p in sources}

    def mapper(zi):
        return [] if replace and any(_under(_zname(zi), b) for b in names) else [zi.filename]
    return zip_apply(path, mapper, adds=[(dest_prefix, p) for p in sources], **kw)


def make_zip(dest, paths, level=6, ctx=None):
    """Create a zip of files/folders (stored under their own names). -> number of entries."""
    total = sum(path_size(p) for p in paths)
    if total and free_space(os.path.dirname(os.path.abspath(dest))) < total + (4 << 20):
        raise ArchiveError("Not enough free space for the zip (about %s needed)" % human(total))
    tmp = tmp_beside(dest)
    w = ZipWriter(tmp)
    done = [0]
    try:
        skip = {os.path.abspath(dest), os.path.abspath(tmp)}
        for p in paths:
            w.add_tree("", p, level, ctx, done, total, skip)
        n = len(w.cd)
        w.close()
    except BaseException:
        w.abort()
        raise
    os.replace(tmp, dest)
    if ctx is not None:
        ctx.progress(done[0], total, "", force=True)
    return n


# ---- plain file operations ------------------------------------------------------------------------
def path_size(path):
    try:
        if os.path.isfile(path) or os.path.islink(path):
            return os.path.getsize(path)
        total = 0
        for root, dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _copy_file(src, dst, ctx, done, total):
    tmp = tmp_beside(dst)
    try:
        with open(src, "rb") as a, open(tmp, "wb") as b:
            while True:
                buf = a.read(1 << 20)
                if not buf:
                    break
                b.write(buf)
                done[0] += len(buf)
                if ctx is not None:
                    check_cancel(ctx)
                    ctx.progress(done[0], total, os.path.basename(src))
        os.replace(tmp, dst)
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _copy_tree(src, dst, policy, ctx, done, total, errors):
    if os.path.isdir(src) and not os.path.islink(src):
        os.makedirs(dst, exist_ok=True)
        for child in sorted(os.listdir(src)):
            s, d = os.path.join(src, child), os.path.join(dst, child)
            if os.path.lexists(d) and not (os.path.isdir(s) and os.path.isdir(d)):
                if policy == "skip":
                    continue
                if policy == "rename":
                    d = unique_path(d)
            try:
                _copy_tree(s, d, policy, ctx, done, total, errors)
            except Cancelled:
                raise
            except OSError as ex:
                errors.append((s, str(ex)))
    elif os.path.islink(src):
        errors.append((src, "symbolic link skipped"))
    else:
        _copy_file(src, dst, ctx, done, total)


def fs_transfer(sources, dest_dir, move=False, policy="rename", ctx=None):
    """Copy or move files/folders into dest_dir. -> (list of new paths, list of (path, problem))"""
    total = sum(path_size(p) for p in sources)
    if free_space(dest_dir) < total + (4 << 20) and not move:
        raise ArchiveError("Not enough free space: this needs %s but only %s is free" % (human(total), human(free_space(dest_dir))))
    made, errors, done = [], [], [0]
    os.makedirs(dest_dir, exist_ok=True)
    for src in sources:
        check_cancel(ctx)
        src = src.rstrip("/")
        target = os.path.join(dest_dir, os.path.basename(src))
        real_src, real_dst = os.path.realpath(src), os.path.realpath(dest_dir)
        if os.path.isdir(src) and (real_dst == real_src or real_dst.startswith(real_src + os.sep)):
            errors.append((src, "can't put a folder inside itself"))
            continue
        if move and os.path.dirname(real_src) == real_dst:
            errors.append((src, "already in that folder"))
            continue
        try:
            if os.path.lexists(target):
                if policy == "skip":
                    errors.append((src, "skipped (name exists)"))
                    continue
                if policy == "rename" or (os.path.isdir(target) != os.path.isdir(src)):
                    target = unique_path(target)
            if move:
                if os.path.lexists(target) and not os.path.isdir(target):
                    os.remove(target)
                if not os.path.lexists(target):
                    try:
                        os.rename(src, target)
                        made.append(target)
                        continue
                    except OSError as ex:
                        if ex.errno != errno.EXDEV:
                            raise
            errs = []
            _copy_tree(src, target, "overwrite" if policy == "overwrite" else policy, ctx, done, total, errs)
            errors.extend(errs)
            if move and not errs:
                fs_delete([src])
            made.append(target)
        except Cancelled:
            raise
        except OSError as ex:
            errors.append((src, str(ex)))
    if ctx is not None:
        ctx.progress(done[0], total, "", force=True)
    return made, errors


def fs_delete(paths, ctx=None):
    """-> (number deleted, list of (path, problem))"""
    n, errors = 0, []
    for p in paths:
        check_cancel(ctx)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, onerror=lambda fn, path, exc: errors.append((path, str(exc[1]))))
            else:
                os.remove(p)
            if not os.path.lexists(p):
                n += 1
        except OSError as ex:
            errors.append((p, str(ex)))
    return n, errors


def fs_rename(path, newname):
    newname = newname.strip()
    if not newname or newname in (".", "..") or "/" in newname or "\x00" in newname:
        raise ArchiveError("That isn't a valid name")
    target = os.path.join(os.path.dirname(path.rstrip("/")), newname)
    if target != path and os.path.lexists(target):
        raise ArchiveError("'%s' already exists here" % newname)
    os.rename(path, target)
    return target



# ------------------------------------------------------------------ job view
class JobView:
    """Live output screen for a Job. It follows the output like a terminal, stops following the
    moment you scroll up (and never fights your finger), and has Copy / Clear / Cancel / Done.
    on_ok: next step to start by itself when the job succeeds. extras: [(label, fn)] shown on success."""

    SHOW_EVERY = 0.4
    BIG = 10 ** 7

    def __init__(self, app, parent, job, scope, then=None, on_ok=None, extras=None):
        self.app, self.job, self.scope, self.then, self.on_ok = app, job, scope, then, on_ok
        self.buf, self.pct = LogBuf(), None
        self.t0 = time.time()
        self.finished = self.reported = self.left = False
        self.dirty, self.last, self.last_stat = True, 0.0, 0.0
        self.follow, self.ref_y, self.shown = True, None, None
        a = app.a
        self.stat = app.label(parent, "Starting...", size=15, color=ACCENT)
        self.sv = tg.NestedScrollView(a, parent)
        safe(self.sv.setlinearlayoutparams, 1)
        safe(self.sv.setbackgroundcolor, SURF)
        self.tv = tg.TextView(a, "", self.sv, selectabletext=True)
        safe(self.tv.setheight, tg.View.WRAP_CONTENT)
        safe(self.tv.setlinearlayoutparams, 0)
        safe(self.tv.settextsize, 13)
        safe(self.tv.settextcolor, TEXT)
        safe(self.tv.setmargin, 8)
        tools = app.row(parent)
        self.b_follow = app.btn(tools, "Pause scroll", self.toggle_follow, scope, kind="flat", h=48, weight=1)
        app.btn(tools, "Copy", self.copy, scope, kind="flat", h=48, weight=1)
        app.btn(tools, "Clear", self.clear_out, scope, kind="flat", h=48, weight=1)
        self.extra_row = app.row(parent)
        self.extra_btns = [app.btn(self.extra_row, text, fn, scope, kind="tonal", h=56, weight=1)
                           for text, fn in (extras or [])]
        for b in self.extra_btns:
            safe(b.setvisibility, tg.View.GONE)
        row = app.row(parent)
        self.b_cancel = app.btn(row, "Cancel job", self.cancel, scope, kind="danger", h=56, weight=1)
        self.b_done = app.btn(row, "Done", self.done, scope, kind="primary", h=56, weight=1)
        safe(self.b_done.setvisibility, tg.View.GONE)
        app.jobviews.append(self)

    # ---- buttons
    def cancel(self):
        if self.finished:
            return
        if self.job.cancel():
            self.app.toast("Cancelling...")
            safe(self.b_cancel.settext, "Cancelling...")
            self.app.set_text(self.stat, "Cancelling...", WARN)
        else:
            self.app.toast("Already cancelling - one moment")

    def done(self):
        self.left = True
        (self.then or self.app.leave_flow)()

    def copy(self):
        self.app.copy_output(self.buf.full_text())

    def clear_out(self):
        self.buf = LogBuf()
        self.shown = None
        self.ref_y = None
        safe(self.tv.settext, "")
        self.dirty = True

    def set_follow(self, on):
        self.follow = on
        safe(self.b_follow.settext, "Pause scroll" if on else "Jump to end")

    def toggle_follow(self):
        self.set_follow(not self.follow)
        if self.follow:
            self.ref_y = None
            self.scroll_end()

    # ---- scrolling like a terminal
    def user_scrolled_up(self):
        """True if the view is clearly above where we last put it (you scrolled up to read)."""
        pos = safe(self.sv.getscrollposition)
        try:
            y = int(pos[1])
        except (TypeError, ValueError, IndexError):
            return False
        if self.ref_y is not None and y < self.ref_y - 60:
            return True
        self.ref_y = y if self.ref_y is None else max(self.ref_y, y)
        return False

    def scroll_end(self):
        if self.follow and self.user_scrolled_up():
            self.set_follow(False)
        if self.follow:
            safe(self.sv.setscrollposition, 0, self.BIG, False)
            self.app.after("settle:%d" % id(self), 0.3, self.settle)

    def settle(self):
        """The text grows after layout; scroll once more so we really end on the last line."""
        if self.follow and not self.left:
            safe(self.sv.setscrollposition, 0, self.BIG, False)

    # ---- per-tick work
    def refresh_text(self, now):
        self.dirty, self.last = False, now
        txt = self.buf.text() or "..."
        if txt != self.shown:
            self.shown = txt
            safe(self.tv.settext, txt)
            self.scroll_end()

    def tick(self):
        got = False
        try:
            while True:
                item = self.job.q.get_nowait()
                if item is None:
                    self.finished = True
                    break
                self.buf.feed(item)
                got = True
                p = PCT.findall(item) if "[recovery]" not in item else None  # recovery text isn't our progress
                if p:
                    self.pct = min(100, int(p[-1]))
        except queue.Empty:
            pass
        now = time.time()
        if got or self.finished:
            self.dirty = True
        if self.dirty and (self.finished or now - self.last >= self.SHOW_EVERY):
            self.refresh_text(now)
        if not self.finished and now - self.last_stat >= 1.0:
            self.last_stat = now
            el = int(now - self.t0)
            pct = "  %d%%" % self.pct if self.pct is not None else ""
            word, col = ("Cancelling", WARN) if self.job.cancel_started else ("Running", ACCENT)
            self.app.set_text(self.stat, "%s%s  \u00b7  %02d:%02d" % (word, pct, el // 60, el % 60), col)
        if self.finished and not self.reported:
            self.finish(now)

    def finish(self, now):
        self.reported = True
        el = int(now - self.t0)
        rc = self.job.rc
        ok = rc == 0 and not self.job.cancelled
        if self.job.cancelled:
            msg, col = "Cancelled", WARN
        elif rc == 0:
            msg, col = "Finished OK", GOOD
        else:
            msg, col = "Failed (exit %s)" % rc, BAD
        chaining = bool(ok and self.on_ok)
        text = "%s  \u00b7  %02d:%02d" % (msg, el // 60, el % 60)
        if chaining:
            text += "  \u00b7  next step starting..."
        self.app.set_text(self.stat, text, col)
        safe(self.b_cancel.setvisibility, tg.View.GONE)
        if chaining:
            self.app.job_finished(self.job, chaining=True)
            self.app.after("chain:%d" % id(self), 0.9, self.chain)
        else:
            safe(self.b_done.setvisibility, tg.View.VISIBLE)
            if ok:
                for b in self.extra_btns:
                    safe(b.setvisibility, tg.View.VISIBLE)
            self.app.job_finished(self.job)
        self.scroll_end()

    def chain(self):
        if self.left:
            return
        self.left = True
        try:
            self.on_ok()
        finally:
            if self.app.busy is self.job:  # the next step never started: don't leave the app locked
                self.app.busy = None


# ------------------------------------------------------------ file browser
class Browser:
    """Tap-to-navigate file list, used for this phone (local) and for the
    connected device (remote). Confined to `root`. Paginated so big folders
    don't lag, with a live search box."""

    PAGE = 30

    def __init__(self, app, parent, scope, lister, root, start=None, exts=None, mode="file",
                 on_file=None, on_use=None, on_dir=None, toolbar=None, remote=False):
        self.app, self.scope, self.lister, self.root = app, scope, lister, root
        self.exts, self.mode, self.on_file, self.on_use = exts, mode, on_file, on_use
        self.on_dir, self.remote = on_dir, remote
        self.dir = start if start and (start == root or start.startswith(root.rstrip("/") + "/")) else root
        self.q, self.page, self.items, self.err = "", 0, [], ""
        self.token, self.alive = 0, True
        self.showall = exts is None
        self.lscope = "list:" + scope
        self.path_tv = app.label(parent, self.dir, size=13, color=MUTED)
        row = app.row(parent)
        app.btn(row, "Up", self.up, scope, kind="tonal", h=52, weight=1)
        app.btn(row, "Refresh", self.reload, scope, kind="tonal", h=52, weight=1)
        for text, fn, kind in (toolbar or []):
            app.btn(row, text, fn, scope, kind=kind, h=52, weight=1)
        if mode == "dir":
            app.btn(parent, "Use this folder", self.use, scope, kind="primary", h=56)
        frow = app.row(parent)
        self.edit = tg.EditText(app.a, "", frow, singleline=True)
        safe(self.edit.setlinearlayoutparams, 1)
        safe(self.edit.setheight, 52)
        safe(self.edit.settextcolor, TEXT)
        safe(self.edit.sendtextevent, True)
        app.on_text(self.edit, self.typed, scope)
        gb = app.btn(frow, "Search", self.apply_search, scope, kind="tonal", h=52, weight=0)
        safe(gb.setwidth, tg.View.WRAP_CONTENT)
        self.ext_btn = None
        if exts:
            self.ext_btn = app.btn(parent, "", self.toggle_all, scope, kind="flat", h=48, pill=False)
            self.label_ext()
        self.lst = app.col(parent)
        self.reload()

    def label_ext(self):
        if self.ext_btn is not None:
            t = ("Showing every file - tap to show only %s" % "/".join(self.exts)) if self.showall \
                else ("Showing only %s - tap to show every file" % "/".join(self.exts))
            safe(self.ext_btn.settext, t)

    def toggle_all(self):
        self.showall = not self.showall
        self.label_ext()
        self.page = 0
        self.render()

    def typed(self, text):
        self.q = text or ""
        self.app.after("filter:" + self.scope, 0.45, self.apply_typed)

    def apply_typed(self):
        self.page = 0
        self.render()

    def apply_search(self):
        self.q = safe(self.edit.gettext) or ""
        self.page = 0
        self.render()

    def join(self, name):
        return self.dir.rstrip("/") + "/" + name

    def up(self):
        if self.dir.rstrip("/") == self.root.rstrip("/"):
            self.app.toast("Already at the top")
            return
        self.go(os.path.dirname(self.dir.rstrip("/")) or self.root)

    def go(self, path):
        self.dir = path
        self.q = ""
        safe(self.edit.settext, "")
        safe(self.path_tv.settext, path)
        if self.on_dir:
            self.on_dir(path)
        self.reload()

    def use(self):
        if self.on_use:
            self.on_use(self.dir)

    def reload(self):
        self.app.clear(self.lscope, self.lst)
        self.app.label(self.lst, "Loading...", color=MUTED, scope=self.lscope)
        self.token += 1
        tok, path = self.token, self.dir
        if not self.remote:  # this phone's own storage is instant
            self.items, self.err = self.lister(path)
            self.page = 0
            self.render()
            return

        def work():  # the device's storage goes through su + adb: keep the screen responsive meanwhile
            try:
                items, err = self.lister(path)
            except Exception as e:
                items, err = [], str(e)
            self.app.post(lambda: self.loaded(tok, path, items, err))
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, tok, path, items, err):
        if not self.alive or tok != self.token or path != self.dir:
            return  # you already went somewhere else
        self.items, self.err, self.page = items, err, 0
        self.render()

    def visible(self):
        q = self.q.strip().lower()
        out = []
        for name, d, size in self.items:
            if name.startswith(".") and not q.startswith("."):
                continue
            if q and q not in name.lower():
                continue
            if not d and self.exts and not self.showall and not name.lower().endswith(tuple(self.exts)):
                continue
            out.append((name, d, size))
        out.sort(key=lambda t: (not t[1], t[0].lower()))
        return out

    def render(self):
        app = self.app
        app.clear(self.lscope, self.lst)
        if self.err:
            app.label(self.lst, "Can't open this folder: %s" % self.err, color=BAD, scope=self.lscope)
        vis = self.visible()
        pages = max(1, (len(vis) + self.PAGE - 1) // self.PAGE)
        self.page = min(self.page, pages - 1)
        chunk = vis[self.page * self.PAGE:(self.page + 1) * self.PAGE]
        if not vis and not self.err:
            app.label(self.lst, "Nothing here%s." % (" that matches" if (self.q or self.exts) else ""),
                      color=MUTED, scope=self.lscope)
        for name, d, size in chunk:
            if d:
                b = app.btn(self.lst, name + "/", (lambda n=name: self.go(self.join(n))),
                            self.lscope, kind="flat", h=52, pill=False)
            else:
                text = name + ("   \u00b7   " + human(size) if size is not None else "")
                b = app.btn(self.lst, text, (lambda n=name: self.pick(n)), self.lscope,
                            kind="tonal", h=52, pill=False)
            safe(b.setgravity, 0, 1)
        if pages > 1:
            r = app.row(self.lst)
            app.btn(r, "Prev", self.prev, self.lscope, kind="tonal", h=52, weight=1)
            app.label(r, "%d / %d" % (self.page + 1, pages), color=MUTED)
            app.btn(r, "Next", self.next, self.lscope, kind="tonal", h=52, weight=1)

    def prev(self):
        self.page = max(0, self.page - 1)
        self.render()

    def next(self):
        self.page += 1
        self.render()

    def pick(self, name):
        if self.on_file:
            self.on_file(self.join(name))


# ------------------------------------------------------------------- the app
def fmt_date(ts):
    try:
        return time.strftime("%d %b %Y", time.localtime(ts))
    except Exception:
        return ""


def archive_stem(name):
    """'ROM.zip' -> 'ROM', 'fw.tar.gz' -> 'fw'"""
    low = name.lower()
    for ext in sorted(TAR_EXTS + ZIP_EXTS + SINGLE_EXTS, key=len, reverse=True):
        if low.endswith(ext) and len(name) > len(ext):
            return name[:-len(ext)]
    return os.path.splitext(name)[0] or name


class ItemList:
    """Paged, searchable list of rows with optional multi-select. Used for this phone's folders and for the
    contents of an archive. Items are dicts: key (unique), name, dir (bool), sub (small grey text). Selecting
    only changes the text of the rows involved - it never rebuilds the list."""

    PAGE = 20

    def __init__(self, app, parent, scope, on_open, on_change=None, hide_dot=True):
        self.app, self.scope, self.on_open, self.on_change = app, scope, on_open, on_change
        self.lscope = scope + ":list"
        self.items, self.q, self.page, self.rows, self.loading = [], "", 0, {}, False
        self.selecting, self.selected = False, set()
        self.hide_dot = hide_dot
        frow = app.row(parent)
        self.edit = tg.EditText(app.a, "", frow, singleline=True)
        safe(self.edit.setlinearlayoutparams, 1)
        safe(self.edit.setheight, 52)
        safe(self.edit.settextcolor, TEXT)
        safe(self.edit.sendtextevent, True)
        app.on_text(self.edit, self.typed, scope)
        gb = app.btn(frow, "Search", self.apply_search, scope, kind="tonal", h=52, weight=0)
        safe(gb.setwidth, tg.View.WRAP_CONTENT)
        self.lst = app.col(parent)

    def typed(self, text):
        self.q = text or ""
        self.app.after("filter:" + self.lscope, 0.45, self.apply_typed)

    def apply_typed(self):
        self.page = 0
        self.render()

    def apply_search(self):
        self.q = safe(self.edit.gettext) or ""
        self.page = 0
        self.render()

    def clear_search(self):
        self.q = ""
        safe(self.edit.settext, "")

    def visible(self):
        q = self.q.strip().lower()
        out = []
        for it in self.items:
            n = it["name"]
            if self.hide_dot and n.startswith(".") and not q.startswith("."):
                continue
            if q and q not in n.lower():
                continue
            out.append(it)
        return out

    def set_items(self, items):
        self.items, self.loading = items, False
        self.selected &= {it["key"] for it in items}
        self.page = 0
        self.render()

    def set_loading(self):
        self.items, self.loading = [], True
        self.selected.clear()
        self.render()

    def row_text(self, it):
        mark = ("[x]  " if it["key"] in self.selected else "[  ]  ") if self.selecting else ""
        sub = it.get("sub") or ""
        return mark + it["name"] + ("/" if it["dir"] else "") + (("   \u00b7   " + sub) if sub else "")

    def render(self):
        app = self.app
        app.clear(self.lscope, self.lst)
        self.rows = {}
        vis = self.visible()
        pages = max(1, (len(vis) + self.PAGE - 1) // self.PAGE)
        self.page = min(self.page, pages - 1)
        chunk = vis[self.page * self.PAGE:(self.page + 1) * self.PAGE]
        if not vis:
            app.label(self.lst, "Loading..." if self.loading else ("Nothing matches." if self.q.strip() else "Nothing here."), color=MUTED)
        for it in chunk:
            b = app.btn(self.lst, self.row_text(it), (lambda it=it: self.tap(it)), self.lscope,
                        kind="flat" if it["dir"] else "tonal", h=52, pill=False)
            safe(b.setgravity, 0, 1)
            self.rows[it["key"]] = (b, it)
        if pages > 1:
            r = app.row(self.lst)
            app.btn(r, "Prev", self.prev, self.lscope, kind="tonal", h=52, weight=1)
            app.label(r, "%d / %d" % (self.page + 1, pages), color=MUTED)
            app.btn(r, "Next", self.next, self.lscope, kind="tonal", h=52, weight=1)

    def refresh_rows(self):
        for b, it in self.rows.values():
            safe(b.settext, self.row_text(it))

    def prev(self):
        self.page = max(0, self.page - 1)
        self.render()

    def next(self):
        self.page += 1
        self.render()

    def changed(self):
        if self.on_change:
            self.on_change()

    def tap(self, it):
        if self.selecting:
            self.selected.symmetric_difference_update({it["key"]})
            row = self.rows.get(it["key"])
            if row:
                safe(row[0].settext, self.row_text(it))
            self.changed()
        else:
            self.on_open(it)

    def set_selecting(self, on):
        self.selecting = on
        if not on:
            self.selected.clear()
        self.refresh_rows()
        self.changed()

    def select_all(self):
        self.selected |= {it["key"] for it in self.visible()}
        self.refresh_rows()
        self.changed()

    def select_none(self):
        self.selected.clear()
        self.refresh_rows()
        self.changed()


class App:
    def __init__(self, c):
        self.c = c
        self.a = tg.Activity(c)
        self.cfg = load_cfg()
        self.clicks, self.texts, self.timers = [], [], {}
        self.jobviews = []
        self.busy = None
        self.status = {"root": True, "usb": 0, "adb": [], "fb": [], "model": "", "err": "", "fixed": ""}
        self.tab, self.in_flow = "home", False
        self.stack, self.current = [], None
        self.lastdir = {}
        self.files_dir = REMOTE_ROOT
        self.con_tool = "adb"
        self.models = {}
        self.quit = False
        self.probe_q = queue.Queue()
        self.ui_q = queue.Queue()  # work handed to the UI thread by background threads
        self._shown = {}  # last text/colour per view, so unchanged labels cost nothing
        self.browser = None
        self.st = {"root": STORAGE_ROOT, "dir": STORAGE_ROOT, "clip": None}   # Storage tab state
        self.ilist = self.arc_list = self.arc = None
        self.arc_cache, self.arc_tok, self.arc_clip, self.xopts, self.pick = None, 0, None, {}, {}
        self.probe_evt = threading.Event()
        self.paused = self.quitting = self.probe_forced = False
        self.storage_ready, self._bar_sig, self._abar_sig, self.bar_count, self.abar_count = False, None, None, None, None
        self.st_tok, self.last_ev = 0, time.time()
        self.stop_evt = threading.Event()
        self.last_fix, self.fix_tries, self.bad_streak = 0.0, 0, 0
        self.build()

    # ---- tiny widget helpers -------------------------------------------
    def toast(self, msg):
        safe(self.c.toast, msg)

    def fit(self, v, weight=0, h=None):
        safe(v.setlinearlayoutparams, weight)
        safe(v.setheight, tg.View.WRAP_CONTENT if h is None else h)

    def col(self, parent):
        v = tg.LinearLayout(self.a, parent)
        self.fit(v)
        return v

    def row(self, parent):
        v = tg.LinearLayout(self.a, parent, vertical=False)
        self.fit(v)
        return v

    def label(self, parent, text, size=15, color=None, margin=6, scope=None, selectable=False, weight=0):
        v = tg.TextView(self.a, text, parent, selectabletext=selectable)
        self.fit(v, weight)
        safe(v.settextsize, size)
        safe(v.settextcolor, TEXT if color is None else color)
        safe(v.setmargin, margin)
        return v

    def image(self, parent, name, w, h):
        """ImageView showing an embedded icon (None if the art is missing)."""
        data = icon_bytes(name)
        if not data:
            return None
        iv = tg.ImageView(self.a, parent)
        safe(iv.setimage, data)
        self.fit(iv, 0, h)
        safe(iv.setwidth, w if w else tg.View.MATCH_PARENT)  # full width + fixed height: the icon is centred
        return iv

    def btn(self, parent, text, fn, scope, kind="tonal", h=56, weight=0, pill=True):
        bg, fg = KINDS[kind]
        capL = capR = None
        if pill and icon_bytes("cap_%s_L" % kind):
            box = tg.LinearLayout(self.a, parent, vertical=False)
            self.fit(box, weight, h)
            safe(box.setmargin, 3)
            capw = max(1, h // 2)
            capL = self.image(box, "cap_%s_L" % kind, capw, h)
            b = tg.Button(self.a, text, box)
            safe(b.setlinearlayoutparams, 1)
            safe(b.setheight, h)
            safe(b.setbackgroundcolor, bg)
            safe(b.settextcolor, fg)
            capR = self.image(box, "cap_%s_R" % kind, capw, h)
            if fn:
                self.clicks.append((b, fn, scope))
            return Pill(box, b)
        b = tg.Button(self.a, text, parent)
        self.fit(b, weight, h)
        safe(b.setbackgroundcolor, bg)
        safe(b.settextcolor, fg)
        safe(b.setmargin, 3)
        if fn:
            self.clicks.append((b, fn, scope))
        return b

    def tap_area(self, view, fn, scope):
        """Make an icon tappable as well (best effort: the label button under it always works)."""
        if view is not None and fn:
            safe(view.sendclickevent, True)
            self.clicks.append((view, fn, scope))

    def tile(self, parent, icon, label, fn, scope):
        """Home quick-action: a rounded icon tile with its label under it."""
        col = tg.LinearLayout(self.a, parent)
        self.fit(col, 1)
        safe(col.setmargin, 4)
        iv = self.image(col, "tile_" + icon, None, 72)
        self.tap_area(iv, fn, scope)
        b = tg.Button(self.a, label, col)
        self.fit(b, 0, 52)
        safe(b.setbackgroundcolor, BG)
        safe(b.settextcolor, TEXT)
        safe(b.settextsize, 12)
        if fn:
            self.clicks.append((b, fn, scope))
        return col

    def navitem(self, parent, name, label, fn):
        """Bottom-bar entry: icon in a pill when selected, label under it."""
        col = tg.LinearLayout(self.a, parent)
        self.fit(col, 1, 72)
        iv = self.image(col, "nav_" + name, None, 32)
        self.tap_area(iv, fn, "nav")
        lb = tg.Button(self.a, label, col)
        self.fit(lb, 0, 40)
        safe(lb.setbackgroundcolor, BG)
        safe(lb.settextcolor, MUTED)
        safe(lb.settextsize, 12)
        self.clicks.append((lb, fn, "nav"))
        return iv, lb

    def post(self, fn):
        """Run fn on the UI thread (safe to call from any thread)."""
        self.ui_q.put(fn)

    def set_text(self, view, text, color=None):
        """settext/settextcolor only when something actually changed."""
        key = view.id if hasattr(view, "id") else id(view)
        old = self._shown.get(key, (None, None))
        if old[0] != text:
            safe(view.settext, text)
        if color is not None and old[1] != color:
            safe(view.settextcolor, color)
        self._shown[key] = (text, color if color is not None else old[1])

    def copy_output(self, text):
        if not (text or "").strip():
            self.toast("Nothing to copy yet")
            return
        n = text.count("\n") + 1

        def work():
            how, where = copy_to_clipboard(text)
            if how == "clipboard":
                msg = "Copied %d lines to the clipboard" % n
            elif how == "file":
                msg = "Clipboard not available (Termux:API missing?). Saved to %s" % where
            else:
                msg = "Could not copy: %s" % where
            self.post(lambda: self.toast(msg))
        threading.Thread(target=work, daemon=True).start()

    def open_url(self, url):
        def work():
            rc, out = run_user("termux-open-url %s" % shq(url), timeout=10)
            if rc != 0:
                rc, out = run_user("am start -a android.intent.action.VIEW -d %s" % shq(url), timeout=10)
            if rc != 0:
                self.post(lambda: self.toast("Open it yourself: " + url))
        threading.Thread(target=work, daemon=True).start()

    def on_text(self, view, fn, scope):
        self.texts.append((view, fn, scope))

    def drop(self, scope):
        pre = scope + ":"
        self.clicks = [t for t in self.clicks if t[2] != scope and not str(t[2]).startswith(pre)]
        self.texts = [t for t in self.texts if t[2] != scope and not str(t[2]).startswith(pre)]

    def clear(self, scope, container):
        self.drop(scope)
        self.jobviews = [j for j in self.jobviews if j.scope != scope]
        container.clearchildren()

    def after(self, key, delay, fn):
        self.timers[key] = (time.time() + delay, fn)

    def page_view(self, parent, hidden=True):
        sv = tg.NestedScrollView(self.a, parent, visibility=(tg.View.GONE if hidden else None))
        safe(sv.setlinearlayoutparams, 1)
        ll = tg.LinearLayout(self.a, sv)
        safe(ll.setheight, tg.View.WRAP_CONTENT)
        return sv, ll

    # ---- shell of the app ------------------------------------------------
    def build(self):
        a = self.a
        safe(a.settheme, BG, PRIM_BG, BG, TEXT, ACCENT)
        safe(a.interceptbackbutton, True)
        self.root = tg.LinearLayout(a)
        hdr = tg.LinearLayout(a, self.root)
        self.fit(hdr)
        safe(hdr.setbackgroundcolor, SURF)
        self.label(hdr, "FlashKit  \u00b7  " + VERSION, size=20, color=TEXT, margin=8)
        self.stat_tv = self.label(hdr, "Checking device...", size=16, color=MUTED, margin=8)
        self.hint_tv = self.label(hdr, "", size=13, color=MUTED, margin=8)
        self.pages = {}
        for name in ("home", "files", "storage", "console", "settings"):
            self.pages[name] = self.page_view(self.root)
        self.flow_sv, self.flow = self.page_view(self.root)
        self.job_box = tg.LinearLayout(a, self.root, visibility=tg.View.GONE)  # job output: its own bounded scroller
        safe(self.job_box.setlinearlayoutparams, 1)
        nav = tg.LinearLayout(a, self.root, vertical=False)
        self.fit(nav, 0, 72)
        safe(nav.setbackgroundcolor, BG)
        self.nav = {}
        for name, text in (("home", "Home"), ("files", "Files"), ("storage", "Storage"), ("console", "Console"),
                           ("settings", "Settings")):
            self.nav[name] = self.navitem(nav, name, text, (lambda n=name: self.select_tab(n)))
        self.build_home()
        self.build_console()
        self.build_settings()
        self.select_tab("home", force=True)

    def select_tab(self, name, force=False):
        if self.busy and self.in_flow and not force:
            self.toast("A job is running - cancel it or wait for it to finish")
            return
        self.stack, self.current, self.in_flow = [], None, False
        self.tab = name
        for n, (sv, _) in self.pages.items():
            safe(sv.setvisibility, tg.View.VISIBLE if n == name else tg.View.GONE)
        safe(self.flow_sv.setvisibility, tg.View.GONE)
        safe(self.job_box.setvisibility, tg.View.GONE)
        for n, (iv, lb) in self.nav.items():
            on = n == name
            if iv is not None:
                safe(iv.setimage, icon_bytes("nav_%s%s" % (n, "_sel" if on else "")))
            safe(lb.settextcolor, PRIM_TX if on else MUTED)
        if name == "files":
            self.build_files()
        elif name == "storage":
            if self.storage_ready:
                self.storage_refresh()
            else:
                self.build_storage()
        elif name == "home":
            self.refresh_home()

    def show_flow(self):
        self.in_flow = True
        for sv, _ in self.pages.values():
            safe(sv.setvisibility, tg.View.GONE)
        safe(self.job_box.setvisibility, tg.View.GONE)
        safe(self.flow_sv.setvisibility, tg.View.VISIBLE)

    def leave_flow(self):
        self.select_tab(self.tab, force=True)

    def begin_flow(self):
        self.show_flow()
        self.clear("flow", self.flow)

    def begin_job(self, title):
        self.in_flow = True
        for sv, _ in self.pages.values():
            safe(sv.setvisibility, tg.View.GONE)
        safe(self.flow_sv.setvisibility, tg.View.GONE)
        self.clear("flow", self.job_box)
        safe(self.job_box.setvisibility, tg.View.VISIBLE)
        self.label(self.job_box, title, size=20, margin=8)
        return self.job_box

    def screen(self, title, back=True):
        self.begin_flow()
        r = self.row(self.flow)
        if back:
            b = self.btn(r, "\u2039 Back", self.back, "flow", kind="flat", h=52)
            safe(b.setwidth, tg.View.WRAP_CONTENT)
        self.label(r, title, size=20, weight=1)
        return self.flow

    def go(self, fn, *args):
        if self.current and not getattr(self.current[0], "_router", False):
            self.stack.append(self.current)
        self.current = (fn, args)
        fn(*args)

    def retry(self, fn, *args):
        """Re-run a step from a message screen without leaving another copy
        of that message behind on the back history."""
        self.current = self.stack.pop() if self.stack else None
        fn(*args)

    def start_flow(self, fn, *args):
        if self.busy:
            self.toast("A job is still running")
            return
        self.stack, self.current = [], None
        self.go(fn, *args)

    def back(self):
        if self.busy:
            self.toast("A job is running - use Cancel job")
            return
        if self.stack:
            fn, args = self.stack.pop()
            self.current = (fn, args)
            fn(*args)
        else:
            self.leave_flow()

    def on_back(self):
        if self.in_flow:
            self.back()
        elif self.tab == "storage" and self.storage_back():
            pass
        elif self.tab != "home":
            self.select_tab("home")
        else:
            safe(self.a.finish)
            self.quit = True

    # ---- jobs -------------------------------------------------------------
    def start_job(self, job):
        self.busy = job
        if self.cfg.get("keepon"):
            safe(self.a.keepscreenon, True)
        job.start()

    def job_finished(self, job, chaining=False):
        if chaining:
            return  # the next step is about to start: keep the probe quiet and the screen on
        if self.busy is job:
            self.busy = None
        safe(self.a.keepscreenon, False)
        self.probe_soon()

    def adb(self):
        a = self.status.get("adb", [])
        return "adb -s %s" % shq(a[0][0]) if len(a) > 1 else "adb"

    def fbt(self):
        f = self.status.get("fb", [])
        return "fastboot -s %s" % shq(f[0]) if len(f) > 1 else "fastboot"

    def adb_mode(self):
        a = self.status.get("adb", [])
        return a[0][1] if a else None

    def fresh(self):
        st = probe()
        self.apply_status(st)
        return st

    def fresh_enough(self, maxage=4.0):
        """Status no older than `maxage` s (the background poll keeps it current), so tapping a
        button doesn't freeze the UI on a full su + adb + fastboot round trip."""
        st = self.status
        if st.get("t") and time.time() - st["t"] < maxage:
            return st
        return self.fresh()

    # ---- status polling ----------------------------------------------------
    def probe_soon(self):
        self.probe_forced = True  # an explicit request (Refresh, job finished, back in the app): check once even if auto-check is off
        self.probe_evt.set()

    def start_threads(self):
        threading.Thread(target=self._probe_loop, daemon=True).start()

    def want_probe(self):
        return not (self.paused or self.quitting) and bool(self.cfg.get("autodetect", True))

    def probe_interval(self):
        return PROBE_EVERY if (self.tab in ("home", "files") or self.in_flow) else max(PROBE_EVERY, 10.0)

    def _probe_cycle(self):
        forced, self.probe_forced = self.probe_forced, False
        if self.busy or self.quitting or not (forced or self.want_probe()):
            return False
        try:
            st = probe()
            if self.cfg.get("autofix"):
                st = self.autofix(st)
            self.enrich(st)
            self.probe_q.put(st)
        except Exception:
            log("probe loop: " + traceback.format_exc())
        return True

    def _probe_loop(self):
        while not self.stop_evt.is_set():
            self._probe_cycle()
            self.probe_evt.wait(self.probe_interval())
            self.probe_evt.clear()

    def enrich(self, st):
        if st["adb"] and st["adb"][0][1] == "device":
            ser = st["adb"][0][0]
            if ser not in self.models:
                rc, out = su_run("adb -s %s shell 'getprop ro.product.model; getprop ro.build.version.release'"
                                 % shq(ser), timeout=10)
                lines = [l.strip() for l in out.splitlines() if l.strip()]
                self.models[ser] = (lines[0] + "  \u00b7  Android " + lines[1]) if len(lines) >= 2 else ""
            st["model"] = self.models.get(ser, "")

    def autofix(self, st):
        """Quiet self-repair: restart a wedged adb server / reopen USB
        permissions. Capped, so it can never loop."""
        if tools_broken(st):
            return st  # restarting the adb server can't help a program that won't start
        healthy = bool(st["adb"] or st["fb"]) or st["usb"] == 0
        if healthy and not any(s == "offline" or s.startswith("no permissions") for _, s in st["adb"]):
            self.fix_tries = self.bad_streak = 0
            return st
        problem = None
        if any(s == "offline" or s.startswith("no permissions") for _, s in st["adb"]):
            problem = "adb was offline"
        else:
            self.bad_streak += 1
            if self.bad_streak >= 2:
                problem = "USB device wasn't answering"
        now = time.time()
        if not problem or self.fix_tries >= 3 or now - self.last_fix < 12:
            return st
        self.last_fix, self.fix_tries, self.bad_streak = now, self.fix_tries + 1, 0
        log("autofix: " + problem)
        su_run("adb kill-server; sleep 1; chmod -R 666 /dev/bus/usb/*/* 2>/dev/null; adb start-server",
               timeout=25)
        time.sleep(1.0)
        st2 = probe()
        st2["fixed"] = "Auto-fix: restarted the adb server (%s)" % problem
        return st2

    def apply_status(self, st):
        self.status = st
        text, col, hint = describe(st)
        self.set_text(self.stat_tv, text, col)
        self.set_text(self.hint_tv, hint)
        if self.tab == "home" and not self.in_flow:
            self.refresh_home()

    # ---- Home -----------------------------------------------------------------
    def build_home(self):
        ll = self.pages["home"][1]
        self.label(ll, "Device", size=13, color=MUTED, margin=8)
        self.home_card = self.label(ll, "Checking...", size=15, margin=8)
        self.repair_btn = self.btn(ll, "Repair adb/fastboot", lambda: self.start_flow(self.flow_repair), "home",
                                   kind="danger", h=64)
        safe(self.repair_btn.setvisibility, tg.View.GONE)
        self.label(ll, "Quick actions", size=13, color=MUTED, margin=8)
        r = self.row(ll)
        self.tile(r, "sideload", "Sideload a ROM zip", lambda: self.start_flow(self.flow_sideload), "home")
        self.tile(r, "flash", "Flash an image", lambda: self.start_flow(self.flow_flash), "home")
        self.tile(r, "apk", "Install an APK", lambda: self.start_flow(self.flow_install), "home")
        r = self.row(ll)
        self.tile(r, "reboot", "Reboot target", lambda: self.start_flow(self.flow_reboot), "home")
        self.tile(r, "erase", "Erase a partition", lambda: self.start_flow(self.flow_erase), "home")
        self.tile(r, "gsi", "GSI flash", lambda: self.start_flow(self.flow_gsi), "home")
        r = self.row(ll)
        self.tile(r, "recovery", "Recovery black-screen fix", lambda: self.start_flow(self.flow_recovery_fix), "home")
        self.tile(r, "refresh", "Refresh status", self.probe_soon, "home")
        self.tile(r, "fix", "Fix connection now", self.fix_now, "home")
        self.btn(ll, "Made by @Shivraj_editx  \u00b7  t.me/Shivraj_editx", lambda: self.open_url(CREDIT_URL),
                 "home", kind="flat", h=48)

        self.btn(ll, "Quit completely (force stop)", self.sc_quit, "home", kind="tonal", h=48)

    def refresh_home(self):
        st = self.status
        lines = []
        if not st.get("root", True):
            lines.append("Root: NOT granted")
        else:
            adb, fb = st["adb"], st["fb"]
            if fb:
                lines.append("Mode: fastbootd (userspace)" if st.get("fastbootd") is True
                             else "Mode: fastboot (bootloader)")
                lines.append("Serial: " + fb[0])
            elif adb:
                lines.append("Mode: adb (%s)" % adb[0][1])
                lines.append("Serial: " + adb[0][0])
                if st.get("model"):
                    lines.append("Device: " + st["model"])
            elif tools_broken(st):
                lines.append("Mode: unknown (adb/fastboot can't start)")
            else:
                lines.append("Mode: none")
            lines.append("USB devices seen: %d" % st.get("usb", 0))
            for dev in (st.get("usbinfo") or [])[:3]:
                nm = " ".join(x for x in (dev.get("mfr"), dev.get("product")) if x)
                lines.append("  %s  %s" % (dev.get("id", "?"), nm))
                if dev.get("ifs"):
                    lines.append("    interfaces: " + ", ".join(dev["ifs"][:6]))
            for tool in ("adb", "fastboot"):
                if (st.get("tool_err") or {}).get(tool):
                    lines.append("%s: %s" % (tool, st["tool_err"][tool][:110]))
            if len(st["adb"]) + len(st["fb"]) > 1:
                lines.append("Several devices connected - using the first one")
        if st.get("fixed"):
            lines.append(st["fixed"])
        self.set_text(self.home_card, "\n".join(lines))
        safe(self.repair_btn.setvisibility, tg.View.VISIBLE if tools_broken(st) else tg.View.GONE)

    def fix_now(self):
        self.toast("Restarting adb server...")
        su_run("adb kill-server; sleep 1; chmod -R 666 /dev/bus/usb/*/* 2>/dev/null; adb start-server",
               timeout=25)
        self.fresh()
        self.toast("Done")

    # ---- generic screens -----------------------------------------------------
    def sc_message(self, title, body, buttons=None):
        f = self.screen(title)
        self.label(f, body, size=15, selectable=True, margin=10)
        for text, fn, kind in (buttons or []):
            self.btn(f, text, fn, "flow", kind=kind, h=60)
        if tools_broken(self.status):
            self.btn(f, "Repair adb/fastboot", lambda: self.go(self.flow_repair), "flow", kind="danger", h=60)

    @router
    def flow_recovery_fix(self):
        st = self.fresh_enough()
        if tools_broken(st):
            self.go(self.sc_message, "adb can't start", describe(st)[2])
            return
        self.go(self.sc_job, "Recovery: mount vendor", recovery_fix_script(self.adb()), 300)

    @router
    def flow_repair(self):
        self.go(self.sc_confirm, "Repair adb/fastboot", REPAIR_INFO,
                lambda: self.go(self.sc_job, "Repairing adb/fastboot", REPAIR_SCRIPT, 1800, None,
                                tools_verdict, False), "Repair now", False)

    def sc_pick_file(self, title, exts, on_pick, mode="file"):
        f = self.screen(title)
        Browser(self, f, "flow", local_list, STORAGE_ROOT, start=self.lastdir.get(title), exts=exts,
                mode=mode, on_file=on_pick, on_use=on_pick,
                on_dir=lambda d, t=title: self.lastdir.__setitem__(t, d))

    def sc_confirm(self, title, body, on_yes, yes_text="Continue", danger=False, toggles=None):
        f = self.screen(title)
        self.label(f, body, size=15, selectable=True, margin=10)
        for label, opts, key in (toggles or []):
            self.toggle_btn(f, label, opts, key)
        self.btn(f, "Cancel", self.back, "flow", kind="tonal", h=60)
        self.btn(f, yes_text, on_yes, "flow", kind="danger" if danger else "primary", h=60)

    def toggle_btn(self, parent, label, opts, key):
        def fmt():
            return ("[x]  " if opts.get(key) else "[  ]  ") + label
        b = self.btn(parent, fmt(), None, "flow", kind="flat", h=52, pill=False)
        safe(b.setgravity, 0, 1)

        def flip():
            opts[key] = not opts.get(key)
            safe(b.settext, fmt())
        self.clicks.append((b, flip, "flow"))

    def sc_typeconfirm(self, title, body, expect, on_ok, ok_text="Confirm"):
        f = self.screen(title)
        self.label(f, body, size=15, selectable=True, margin=10)
        self.label(f, "Type  %s  to confirm:" % expect, size=14, color=WARN)
        ed = tg.EditText(self.a, "", f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)

        def check():
            if (safe(ed.gettext) or "").strip() == expect:
                safe(self.a.hidesoftkeyboard)
                on_ok()
            else:
                self.toast("That doesn't match '%s'" % expect)
        self.btn(f, "Cancel", self.back, "flow", kind="tonal", h=60)
        self.btn(f, ok_text, check, "flow", kind="danger", h=60)

    def sc_job(self, title, script, timeout=600, then=None, post=None, root=True, on_ok=None, extras=None):
        self.stack = []  # a job is the end of the road: Back must not re-show the confirm screen
        box = self.begin_job(title)
        job = Job(title, script, timeout, post, root)
        JobView(self, box, job, "flow", then=then, on_ok=on_ok, extras=extras)
        self.start_job(job)

    def go_job(self, title, script, timeout=600, then=None, post=None, root=True, on_ok=None, extras=None):
        self.go(self.sc_job, title, script, timeout, then, post, root, on_ok, extras)

    # ---- flows ---------------------------------------------------------------
    def need(self, what, ok, body):
        """Show a 'not ready' screen unless ok."""
        if ok:
            return True
        self.sc_message("Not ready", body, [
            ("Check again", lambda: self.start_flow(what), "primary"),
        ])
        return False

    # sideload
    @router
    def flow_sideload(self):
        self.go(self.sc_pick_file, "Pick a ROM / update zip", (".zip",), self.sideload_picked)

    def sideload_picked(self, path):
        st = self.fresh_enough()
        mode = self.adb_mode()
        size = human(os.path.getsize(path)) if os.path.exists(path) else "?"
        info = "File:\n%s\n(%s)" % (path, size)
        opts = {"log": self.cfg.get("sideload_log", True)}
        logtoggle = [("Show the recovery's install log (live if it allows, else when it finishes)", opts, "log")]
        if mode == "sideload":
            self.go(self.sc_confirm, "Send to sideload", "Target is waiting in sideload mode.\n\n" + info,
                    lambda: self.run_sideload(path, False, opts), "Send it", False, logtoggle)
        elif mode == "device":
            self.go(self.sc_confirm, "Sideload", "Target is booted normally. FlashKit will reboot it into "
                    "sideload mode, wait for it, then send:\n\n" + info,
                    lambda: self.run_sideload(path, True, opts), "Reboot & send", False, logtoggle)
        elif mode == "recovery":
            self.go(self.sc_message, "Target is in recovery",
                    "On the target choose 'Apply update > Apply from ADB' (TWRP: Advanced > ADB Sideload), "
                    "then tap Retry.\n\nOr try to push it into sideload from here.", [
                        ("Retry", lambda: self.retry(self.sideload_picked, path), "primary"),
                        ("Try 'adb reboot sideload'", lambda: self.run_sideload(path, True), "tonal")])
        else:
            text = describe(st)
            self.go(self.sc_message, "No adb device", "%s\n\n%s\n\nFor a target in fastboot mode, reboot "
                    "it to recovery first." % (text[0], text[2]),
                    [("Retry", lambda: self.retry(self.sideload_picked, path), "primary")])

    def run_sideload(self, path, reboot, opts=None):
        live = self.cfg.get("sideload_log", True) if opts is None else bool(opts.get("log"))
        if self.cfg.get("sideload_log") != live:
            self.cfg["sideload_log"] = live
            save_cfg(self.cfg)
        self.go(self.sc_job, "Sideloading " + os.path.basename(path),
                sideload_script(self.adb(), path, reboot, live), 3600)

    # flash
    @router
    def flow_flash(self):
        st = self.fresh_enough()
        if not st["fb"]:
            btns = []
            if self.adb_mode() in ("device", "recovery"):
                btns.append(("Reboot to bootloader", lambda: self.go(
                    self.sc_job, "Rebooting to bootloader", self.adb() + " reboot bootloader", 60), "primary"))
            btns.append(("Check again", lambda: self.start_flow(self.flow_flash), "tonal"))
            self.go(self.sc_message, "Target isn't in fastboot mode",
                    "Flashing needs the target in bootloader (fastboot) mode.\n\n" + describe(st)[2], btns)
            return
        self.go(self.sc_flash_part)

    def sc_flash_part(self):
        f = self.screen("Flash: choose partition")
        self.label(f, "Pick a partition, or type a name below (e.g. boot_a for A/B slots).", size=14,
                   color=MUTED)
        for i in range(0, len(FLASH_PARTS), 2):
            r = self.row(f)
            for p in FLASH_PARTS[i:i + 2]:
                self.btn(r, p, (lambda p=p: self.go(self.flash_pick_file, p)), "flow", kind="tonal",
                         h=56, weight=1)
        ed = tg.EditText(self.a, "", f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)

        def custom():
            name = (safe(ed.gettext) or "").strip()
            if not re.match(r"^[A-Za-z0-9_.\-]+$", name):
                self.toast("Type a partition name first")
                return
            safe(self.a.hidesoftkeyboard)
            self.go(self.flash_pick_file, name)
        self.btn(f, "Use typed name", custom, "flow", kind="primary", h=56)

    def flash_pick_file(self, part):
        self.sc_pick_file("Image for " + part, (".img", ".bin", ".mbn", ".elf", ".zip"),
                          lambda path: self.flash_picked(part, path))

    def flash_picked(self, part, path):
        size = os.path.getsize(path) if os.path.exists(path) else None
        notes = []
        if size == 0:
            self.toast("That file is empty")
            return
        if path.lower().endswith(".zip"):
            notes.append("NOTE: this is a .zip. `fastboot flash` wants a raw image; ROM zips go through "
                         "Sideload or recovery instead.")
        opts = {"verity": False}
        risky = part.split("_")[0] in RISKY or part in RISKY
        body = "Flash to partition '%s'?\n\nFile: %s\nSize: %s\n\n%s" % (
            part, path, human(size), "\n".join(notes))
        if risky:
            body = ("WARNING: '%s' is a critical partition. A bad flash here can hard-brick the device.\n\n"
                    % part) + body

        def run():
            cmd = self.fbt()
            if opts["verity"]:
                cmd += " --disable-verity --disable-verification"
            script = "%s flash %s %s" % (cmd, shq(part), shq(path))
            self.go(self.sc_job, "Flashing " + part, script, 1800)

        def gate():
            if risky:
                self.go(self.sc_typeconfirm, "Critical partition", "You're about to flash '%s'." % part,
                        part, run, "Flash it")
            else:
                run()
        toggles = [("Disable verity + verification (for vbmeta)", opts, "verity")] \
            if part.startswith("vbmeta") else None
        self.go(self.sc_confirm, "Confirm flash", body, gate, "Flash", risky, toggles)

    # install
    @router
    def flow_install(self):
        st = self.fresh_enough()
        if self.adb_mode() != "device":
            self.go(self.sc_message, "Target isn't booted",
                    "Installing an APK needs the target booted normally with USB debugging authorized.\n\n"
                    + describe(st)[2], [("Check again", lambda: self.start_flow(self.flow_install), "primary")])
            return
        self.go(self.sc_pick_file, "Pick an APK", (".apk",), self.install_picked)

    def install_picked(self, path):
        opts = {"r": True, "d": False, "g": False}

        def run():
            flags = "".join(" -" + k for k in ("r", "d", "g") if opts[k])
            self.go(self.sc_job, "Installing " + os.path.basename(path),
                    "%s install%s %s" % (self.adb(), flags, shq(path)), 600)
        self.go(self.sc_confirm, "Install APK", "Install:\n%s\n(%s)" % (path, human(os.path.getsize(path)
                if os.path.exists(path) else None)), run, "Install", False,
                [("Replace if already installed (-r)", opts, "r"), ("Allow downgrade (-d)", opts, "d"),
                 ("Grant all permissions (-g)", opts, "g")])

    # reboot
    @router
    def flow_reboot(self):
        st = self.fresh_enough()
        mode = self.adb_mode()
        if mode in ("device", "recovery", "sideload"):
            tool, opts = self.adb(), [("System", "reboot"), ("Bootloader", "reboot bootloader"),
                                      ("Recovery", "reboot recovery"), ("Sideload", "reboot sideload"),
                                      ("Fastbootd", "reboot fastboot")]
        elif st["fb"]:
            tool, opts = self.fbt(), [("System", "reboot"), ("Bootloader", "reboot bootloader"),
                                      ("Recovery", "reboot recovery"), ("Fastbootd", "reboot fastboot")]
        else:
            self.go(self.sc_message, "No device", describe(st)[2],
                    [("Check again", lambda: self.start_flow(self.flow_reboot), "primary")])
            return
        f = self.screen("Reboot target to...")
        for text, sub in opts:
            self.btn(f, text, (lambda sub=sub, text=text: self.go(
                self.sc_job, "Rebooting to " + text, "%s %s" % (tool, sub), 60)), "flow",
                kind="tonal", h=64)

    # erase - three gates
    @router
    def flow_erase(self):
        st = self.fresh_enough()
        if not st["fb"]:
            btns = []
            if self.adb_mode() in ("device", "recovery"):
                btns.append(("Reboot to bootloader", lambda: self.go(
                    self.sc_job, "Rebooting to bootloader", self.adb() + " reboot bootloader", 60), "primary"))
            self.go(self.sc_message, "Target isn't in fastboot mode",
                    "Erasing needs the target in bootloader (fastboot) mode.\n\n" + describe(st)[2], btns)
            return
        self.go(self.sc_erase_ask)

    def sc_erase_ask(self):
        f = self.screen("Erase a partition")
        self.label(f, "Partition to erase (fastboot erase):", size=15, color=BAD)
        ed = tg.EditText(self.a, "", f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)
        r = self.row(f)
        for p in ("userdata", "cache", "metadata"):
            self.btn(r, p, (lambda p=p: safe(ed.settext, p)), "flow", kind="tonal", h=52, weight=1)

        def nxt():
            name = (safe(ed.gettext) or "").strip()
            if not re.match(r"^[A-Za-z0-9_.\-]+$", name):
                self.toast("Type a partition name first")
                return
            safe(self.a.hidesoftkeyboard)
            self.go(self.sc_confirm, "WARNING 1 of 3",
                    "You are about to ERASE the '%s' partition.\n\nThis cannot be undone." % name,
                    lambda: self.go(self.sc_confirm, "WARNING 2 of 3",
                                    "Erasing '%s' can leave this device unable to boot unless you reflash it "
                                    "right afterwards.\n\nStill continue?" % name,
                                    lambda: self.go(self.sc_typeconfirm, "WARNING 3 of 3",
                                                    "Last chance. '%s' will be erased for good." % name, name,
                                                    lambda: self.go(self.sc_job, "Erasing " + name,
                                                                    "%s erase %s" % (self.fbt(), shq(name)), 120),
                                                    "Erase it"),
                                    "I understand, continue", True),
                    "Yes, continue", True)
        self.btn(f, "Continue", nxt, "flow", kind="danger", h=60)

    # GSI flashing - three modes, all fully automatic once the files are picked:
    #   clean         vbmeta -> fastbootd -> delete 'product' -> flash system
    #                 (frees more logical partitions and retries if it runs out of
    #                 room); you factory-reset in recovery afterwards.
    #   dirty-system  vbmeta -> fastbootd -> flash system.img }  nothing deleted,
    #   dirty-super   vbmeta -> fastbootd -> flash super.img  }  userdata untouched
    @router
    def flow_gsi(self):
        st = self.fresh_enough()
        if not st["fb"]:
            btns = []
            if self.adb_mode() in ("device", "recovery"):
                btns.append(("Reboot to bootloader", lambda: self.go(
                    self.sc_job, "Rebooting to bootloader", self.adb() + " reboot bootloader", 60), "primary"))
            btns.append(("Check again", lambda: self.start_flow(self.flow_gsi), "tonal"))
            self.go(self.sc_message, "Target isn't in fastboot mode",
                    "GSI flashing needs the target in bootloader (fastboot) mode first.\n\n"
                    + describe(st)[2], btns)
            return
        self.go(self.sc_gsi_mode)

    def sc_gsi_mode(self):
        f = self.screen("GSI flash")
        self.label(f, "Choose how to flash. Once you've picked the files, everything runs by itself - "
                   "no more questions.", size=14, color=MUTED, margin=8)
        for mode, text, kind, h in (
                ("clean", "Clean flash\nvbmeta, fastbootd, delete product, flash system.img\n"
                          "Factory reset in recovery afterwards", "danger", 96),
                ("dirty-system", "Dirty flash: system.img\nKeeps your data - nothing deleted, "
                                 "no factory reset", "primary", 84),
                ("dirty-super", "Dirty flash: super.img\nKeeps your data - replaces the whole super "
                                "partition", "primary", 84)):
            b = self.btn(f, text, (lambda m=mode: self.go(self.gsi_pick_vbmeta, m)), "flow", kind=kind, h=h)
            safe(b.setgravity, 0, 1)

    def gsi_pick_vbmeta(self, mode):
        self.sc_pick_file("%s 1/2: pick vbmeta.img" % GSI_NAMES[mode][0], (".img",),
                          lambda p: self.go(self.gsi_pick_image, mode, p))

    def gsi_pick_image(self, mode, vbmeta):
        part = "super" if mode == "dirty-super" else "system"
        self.sc_pick_file("%s 2/2: pick %s.img" % (GSI_NAMES[mode][0], part), (".img",),
                          lambda p: self.run_gsi(mode, vbmeta, p))

    def run_gsi(self, mode, vbmeta, image):
        """Three jobs in a row, each starting by itself when the last one succeeded."""
        fb = self.fbt()
        part = "super" if mode == "dirty-super" else "system"
        after = ([("Reboot to recovery", lambda: self.start_flow(
                      self.sc_job, "Rebooting to recovery", fb + " reboot recovery", 60))] if mode == "clean" else
                 [("Reboot to system", lambda: self.start_flow(
                      self.sc_job, "Rebooting to system", fb + " reboot", 60))])

        def stage3():
            self.go_job("GSI 3/3  \u00b7  %s in fastbootd" % part,
                        gsi_script(3, mode, vbmeta, image, fb), 3600, extras=after)

        def stage2():
            self.go_job("GSI 2/3  \u00b7  reboot to fastbootd", gsi_script(2, mode, vbmeta, image, fb), 600,
                        on_ok=stage3)
        self.go_job("GSI 1/3  \u00b7  vbmeta in the bootloader", gsi_script(1, mode, vbmeta, image, fb), 600,
                    on_ok=stage2)

    # ---- Console --------------------------------------------------------------
    def build_console(self):
        ll = self.pages["console"][1]
        self.clear("console", ll)
        self.label(ll, "Console", size=13, color=MUTED, margin=8)
        r = self.row(ll)
        for tool in ("adb", "fastboot"):
            self.btn(r, tool, (lambda t=tool: self.set_tool(t)), "console",
                     kind="primary" if self.con_tool == tool else "tonal", h=56, weight=1)
        self.label(ll, "Custom command (without '%s'):" % self.con_tool, size=13, color=MUTED)
        r = self.row(ll)
        self.con_edit = tg.EditText(self.a, "", r, singleline=True)
        safe(self.con_edit.setlinearlayoutparams, 1)
        safe(self.con_edit.setheight, 56)
        safe(self.con_edit.settextcolor, TEXT)
        rb = self.btn(r, "Run", self.console_custom, "console", kind="primary", h=56, weight=0)
        safe(rb.setwidth, tg.View.WRAP_CONTENT)
        self.label(ll, "Presets - each one opens its own result screen (Done to come back, "
                   "nothing lingers here)", size=13, color=MUTED, margin=8)
        for cmd, desc, danger in PRESETS[self.con_tool]:
            b = self.btn(ll, "%s\n%s" % (cmd, desc), (lambda c=cmd, d=danger: self.console_preset(c, d)),
                         "console", kind="danger" if danger else "tonal", h=68, pill=False)
            safe(b.setgravity, 0, 1)

    def set_tool(self, tool):
        self.con_tool = tool
        self.build_console()

    def console_preset(self, cmd, danger):
        if danger:
            self.start_flow(self.sc_confirm, "Run this command?",
                            "%s %s\n\nThis can change or wipe the target." % (self.con_tool, cmd),
                            lambda: self.console_run(cmd), "Run it", True)
        else:
            self.console_run(cmd)

    def console_custom(self):
        text = (safe(self.con_edit.gettext) or "").strip()
        if text:
            safe(self.a.hidesoftkeyboard)
            self.console_run(text)

    def console_run(self, text):
        try:
            args = shlex.split(text)
        except ValueError as e:
            self.toast("Bad quoting: %s" % e)
            return
        name = self.con_tool
        if args and args[0] in ("adb", "fastboot"):
            name, args = args[0], args[1:]  # you typed the tool name: use THAT tool, whatever is selected
        if not args:
            return
        tool = self.adb() if name == "adb" else self.fbt()
        script = tool + " " + " ".join(shq(x) for x in args)
        self.start_flow(self.sc_job, "%s %s" % (name, " ".join(args)), script, 300)

    # ---- Files (device storage) ---------------------------------------------
    def remote_list(self, path):
        rc, out = su_run("%s shell %s" % (self.adb(), shq(dir_script(path))), timeout=20)
        return parse_dir(out)

    def build_files(self):
        ll = self.pages["files"][1]
        self.clear("files", ll)
        self.label(ll, "Target device storage (over USB)", size=13, color=MUTED, margin=8)
        mode = self.adb_mode()
        if mode not in ("device", "recovery"):
            self.label(ll, "Connect the target booted normally (or in recovery) with USB debugging on to browse "
                       "its files.\n\n" + describe(self.status)[2], size=15, margin=10)
            self.btn(ll, "Check again", lambda: (self.fresh(), self.build_files()), "files", kind="primary",
                     h=60)
            if tools_broken(self.status):
                self.btn(ll, "Repair adb/fastboot", lambda: self.start_flow(self.flow_repair), "files",
                         kind="danger", h=60)
            return
        if self.browser:
            self.browser.alive = False
        self.browser = Browser(self, ll, "files", self.remote_list, REMOTE_ROOT, start=self.files_dir,
                on_file=lambda p: self.start_flow(self.sc_file_action, p),
                on_dir=lambda d: setattr(self, "files_dir", d), remote=True,
                toolbar=[("Push a file here", self.push_here, "primary"),
                         ("Pull folder", self.pull_folder, "tonal")])

    def push_here(self):
        dest = self.files_dir
        self.start_flow(self.sc_pick_file, "File to push", None, lambda p: self.push_picked(p, dest))

    def push_picked(self, path, dest):
        opts = {"move": False}
        target = dest.rstrip("/") + "/" + os.path.basename(path)

        def run():
            A = self.adb()
            script = "%s push %s %s" % (A, shq(path), shq(target))
            if opts["move"]:
                script += " && rm -f %s && echo 'Moved: deleted the original on this phone.'" % shq(path)
            self.go(self.sc_job, "Pushing " + os.path.basename(path), script, 3600)
        self.go(self.sc_confirm, "Push to device", "From this phone:\n%s\n\nTo the device:\n%s" % (path, target),
                run, "Push", False, [("Move instead of copy (delete original here afterwards)", opts, "move")])

    def sc_file_action(self, rpath):
        name = rpath.rsplit("/", 1)[-1]
        f = self.screen(name)
        self.label(f, "On the device:\n%s\n\nSaves to this phone's Download folder." % rpath,
                   selectable=True, margin=10)

        def pull(move):
            dst = os.path.join(DOWNLOAD, name)
            script = "mkdir -p %s; %s pull %s %s" % (shq(DOWNLOAD), self.adb(), shq(rpath), shq(dst))
            if move:
                script += " && %s shell %s && echo 'Moved: deleted the original on the device.'" % (
                    self.adb(), shq("rm -f " + shq(rpath)))
            self.go(self.sc_job, ("Moving " if move else "Pulling ") + name, script, 3600)
        self.btn(f, "Copy to this phone", lambda: pull(False), "flow", kind="primary", h=60)
        self.btn(f, "Move to this phone (delete on device)", lambda: pull(True), "flow", kind="tonal", h=60)
        self.btn(f, "Delete on device", lambda: self.go(
            self.sc_confirm, "Delete on device?", "Permanently delete:\n%s" % rpath,
            lambda: self.go(self.sc_job, "Deleting " + name, "%s shell %s" % (
                self.adb(), shq("rm -f " + shq(rpath))), 60), "Delete", True), "flow", kind="danger", h=60)

    def pull_folder(self):
        src = self.files_dir
        name = src.rstrip("/").rsplit("/", 1)[-1] or "sdcard"
        self.start_flow(self.sc_confirm, "Pull folder", "Copy the whole folder:\n%s\n\nto this phone's Download "
                        "folder." % src, lambda: self.go(
                            self.sc_job, "Pulling " + name, "mkdir -p %s; %s pull %s %s" % (
                                shq(DOWNLOAD), self.adb(), shq(src), shq(DOWNLOAD)), 3600), "Pull it")

    # ---- Storage (this phone) + archives ---------------------------------------------------------------
    def sc_task(self, title, fn, then=None, post=None, on_ok=None, extras=None):
        """Run a Python task (copy, extract, compress...) on the live output screen."""
        self.stack = []
        box = self.begin_job(title)
        job = TaskJob(title, fn, post)
        JobView(self, box, job, "flow", then=then, on_ok=on_ok, extras=extras)
        self.start_job(job)

    def radio(self, parent, title, options, state, key, scope="flow", on_pick=None):
        if title:
            self.label(parent, title, size=13, color=MUTED, margin=6)
        rows = []

        def text_for(val, text):
            return ("(\u2022)  " if state.get(key) == val else "(    )  ") + text

        def refresh():
            for val, text, b in rows:
                safe(b.settext, text_for(val, text))

        def pick(v):
            if on_pick and on_pick(v) is False:
                return
            state[key] = v
            refresh()
        for val, text in options:
            b = self.btn(parent, text_for(val, text), None, scope, kind="flat", h=52, pill=False)
            safe(b.setgravity, 0, 1)
            rows.append((val, text, b))
            self.clicks.append((b, (lambda v=val: pick(v)), scope))

    def sc_input(self, title, label, initial, on_ok, ok_text="OK"):
        f = self.screen(title)
        self.label(f, label, size=15, margin=8)
        ed = tg.EditText(self.a, initial or "", f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)

        def go():
            text = (safe(ed.gettext) or "").strip()
            safe(self.a.hidesoftkeyboard)
            on_ok(text)
        self.btn(f, "Cancel", self.back, "flow", kind="tonal", h=60)
        self.btn(f, ok_text, go, "flow", kind="primary", h=60)

    def sc_text(self, title, text, actions=None):
        """Read-only text on a bounded, scrollable page (file preview, archive info...)."""
        box = self.begin_job(title)
        r = self.row(box)
        self.btn(r, "\u2039 Back", self.back, "flow", kind="flat", h=52, weight=1)
        self.btn(r, "Copy", lambda: self.copy_output(text), "flow", kind="flat", h=52, weight=1)
        for lbl, fn in (actions or []):
            self.btn(r, lbl, fn, "flow", kind="tonal", h=52, weight=1)
        sv = tg.NestedScrollView(self.a, box)
        safe(sv.setlinearlayoutparams, 1)
        safe(sv.setbackgroundcolor, SURF)
        tv = tg.TextView(self.a, text[:200000] or "(empty)", sv, selectabletext=True)
        safe(tv.setheight, tg.View.WRAP_CONTENT)
        safe(tv.setlinearlayoutparams, 0)
        safe(tv.settextsize, 13)
        safe(tv.settextcolor, TEXT)
        safe(tv.setmargin, 8)

    def sc_policy(self, title, names, on_pick):
        f = self.screen(title)
        shown = ", ".join(names[:6]) + (" and %d more" % (len(names) - 6) if len(names) > 6 else "")
        self.label(f, "These names already exist there:\n%s\n\nWhat should happen?" % shown, size=15, selectable=True,
                   margin=10)
        for pol, text, kind in (("rename", "Keep both (new ones get a number)", "primary"),
                                ("overwrite", "Replace the existing ones", "danger"),
                                ("skip", "Skip the ones that exist", "tonal")):
            self.btn(f, text, (lambda p=pol: on_pick(p)), "flow", kind=kind, h=60)
        self.btn(f, "Cancel", self.back, "flow", kind="flat", h=52)

    # ---- the Storage tab
    def storage_roots(self):
        roots = [("Internal", STORAGE_ROOT), ("Termux", HOME)]
        try:
            n = 0
            for d in sorted(os.listdir("/storage")):
                p = os.path.join("/storage", d)
                if d not in ("emulated", "self") and os.path.isdir(p) and os.access(p, os.R_OK):
                    n += 1
                    roots.append(("SD card" + (" %d" % n if n > 1 else ""), p))
        except OSError:
            pass
        return roots

    def root_of(self, path):
        cands = [r for _, r in self.storage_roots() if path == r or path.startswith(r.rstrip("/") + "/")]
        return max(cands, key=len) if cands else STORAGE_ROOT

    def build_storage(self):
        ll = self.pages["storage"][1]
        self.clear("storage", ll)
        st = self.st
        if not os.path.isdir(st["dir"]):
            st["dir"] = st["root"]
        self.label(ll, "This phone's storage", size=13, color=MUTED, margin=8)
        r = self.row(ll)
        for name, path in self.storage_roots():
            self.btn(r, name, (lambda p=path: self.storage_root(p)), "storage",
                     kind="primary" if st["root"] == path else "tonal", h=48, weight=1)
        self.st_path = self.label(ll, st["dir"], size=13, color=MUTED, margin=6, selectable=True)
        r = self.row(ll)
        self.btn(r, "Up", self.storage_up, "storage", kind="tonal", h=52, weight=1)
        self.btn(r, "New folder", self.storage_newfolder, "storage", kind="tonal", h=52, weight=1)
        self.sel_btn = self.btn(r, "Select", self.storage_toggle_select, "storage", kind="tonal", h=52, weight=1)
        self.bar_box = self.col(ll)
        self._bar_sig = self.bar_count = None
        self.ilist = ItemList(self, ll, "storage", self.storage_open, on_change=self.storage_changed)
        self.storage_ready = True
        self.storage_load()
        self.storage_changed()

    def scan_items(self, d):
        items = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".fk-") and e.name.endswith(".tmp"):
                        continue
                    try:
                        isd, stt = e.is_dir(), e.stat()
                        size, mt = stt.st_size, stt.st_mtime
                    except OSError:
                        isd, size, mt = False, 0, 0
                    items.append({"key": e.name, "name": e.name, "dir": isd,
                                  "sub": fmt_date(mt) if isd else "%s  \u00b7  %s" % (human(size), fmt_date(mt))})
        except OSError as ex:
            return None, str(ex)
        items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
        return items, ""

    def storage_load(self):
        d = self.st["dir"]
        try:
            count = len(os.listdir(d))
        except OSError as ex:
            self.toast("Can't open this folder: %s" % ex)
            self.ilist.set_items([])
            return
        self.st_tok += 1
        tok = self.st_tok
        if count <= 300:  # small folder: instant
            items, err = self.scan_items(d)
            self.ilist.set_items(items or [])
            return
        self.ilist.set_loading()  # big folder: scan in the background so the screen stays smooth

        def work():
            items, err = self.scan_items(d)
            self.post(lambda: self.storage_loaded(tok, d, items, err))
        threading.Thread(target=work, daemon=True).start()

    def storage_loaded(self, tok, d, items, err):
        if tok != self.st_tok or d != self.st["dir"]:
            return
        if items is None:
            self.toast("Can't open this folder: %s" % err)
        self.ilist.set_items(items or [])

    def storage_refresh(self):
        self.set_text(self.st_path, self.st["dir"])
        self.ilist.selected.clear()
        self.ilist.selecting = False
        self.ilist.clear_search()
        self.storage_load()
        self.storage_changed()

    def storage_root(self, path):
        self.st.update(root=path, dir=path)
        self.build_storage()

    def storage_up(self):
        st = self.st
        if st["dir"].rstrip("/") == st["root"].rstrip("/"):
            self.toast("Already at the top")
            return
        st["dir"] = os.path.dirname(st["dir"].rstrip("/")) or st["root"]
        self.storage_refresh()

    def storage_back(self):
        """Hardware Back inside the Storage tab: leave selection, then go up. True if it did something."""
        if self.ilist is not None and self.ilist.selecting:
            self.ilist.set_selecting(False)
            return True
        if self.st["dir"].rstrip("/") != self.st["root"].rstrip("/"):
            self.storage_up()
            return True
        return False

    def storage_goto(self, path):
        d = path if os.path.isdir(path) else os.path.dirname(path)
        root = self.root_of(d)
        if root != self.st["root"]:
            self.storage_ready = False  # different drive: rebuild the tab so the right one is highlighted
        self.st.update(root=root, dir=d)
        self.select_tab("storage", force=True)

    def storage_open(self, it):
        path = os.path.join(self.st["dir"], it["name"])
        if it["dir"]:
            self.st["dir"] = path
            self.storage_refresh()
        elif archive_kind(it["name"]):
            self.start_flow(self.sc_archive, path, "")
        elif is_texty(it["name"]):
            self.start_flow(self.sc_text_file, path)
        else:
            self.start_flow(self.sc_file_actions, path)

    def storage_toggle_select(self):
        self.ilist.set_selecting(not self.ilist.selecting)

    def sel_paths(self):
        return [os.path.join(self.st["dir"], k) for k in sorted(self.ilist.selected)]

    def storage_changed(self):
        il = self.ilist
        n = len(il.selected)
        clip = self.st["clip"]
        arch_only = bool(il.selecting and n and all(archive_kind(k) for k in il.selected))
        sig = (il.selecting, (clip[0], len(clip[1])) if clip else None, (n == 1, arch_only) if il.selecting else None)
        self.set_text(self.sel_btn, "Done" if il.selecting else "Select")
        if sig == self._bar_sig:
            if il.selecting and self.bar_count is not None:
                self.set_text(self.bar_count, "%d selected" % n)
            return
        self._bar_sig, self.bar_count = sig, None
        self.clear("storage:bar", self.bar_box)
        box = self.bar_box
        if clip:
            mode, paths = clip
            self.label(box, "Clipboard: %d item%s to %s" % (len(paths), "" if len(paths) == 1 else "s", mode),
                       size=14, color=WARN, margin=6)
            r = self.row(box)
            self.btn(r, "Paste here", self.storage_paste, "storage:bar", kind="primary", h=52, weight=1)
            self.btn(r, "Clear clipboard", self.storage_clip_clear, "storage:bar", kind="tonal", h=52, weight=1)
        if il.selecting:
            self.bar_count = self.label(box, "%d selected" % n, size=14, color=ACCENT, margin=6)
            r = self.row(box)
            self.btn(r, "All", il.select_all, "storage:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "None", il.select_none, "storage:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "Copy", lambda: self.storage_clip(False), "storage:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "Move", lambda: self.storage_clip(True), "storage:bar", kind="tonal", h=52, weight=1)
            r = self.row(box)
            self.btn(r, "Delete", self.storage_delete, "storage:bar", kind="danger", h=52, weight=1)
            if n == 1:
                self.btn(r, "Rename", self.storage_rename, "storage:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "Compress", self.storage_compress, "storage:bar", kind="tonal", h=52, weight=1)
            if arch_only:
                self.btn(r, "Extract", self.storage_extract, "storage:bar", kind="primary", h=52, weight=1)

    def storage_clip(self, move):
        paths = self.sel_paths()
        if not paths:
            self.toast("Select something first")
            return
        self.st["clip"] = ("move" if move else "copy", paths)
        self.ilist.set_selecting(False)
        self.toast("%d item%s %s - open the destination and tap Paste here" % (
            len(paths), "" if len(paths) == 1 else "s", "to move" if move else "copied"))

    def storage_clip_clear(self):
        self.st["clip"] = None
        self.storage_changed()

    def storage_paste(self):
        clip = self.st["clip"]
        if not clip:
            return
        mode, paths = clip
        paths = [p for p in paths if os.path.lexists(p)]
        if not paths:
            self.toast("Those items don't exist any more")
            self.st["clip"] = None
            self.storage_changed()
            return
        dest = self.st["dir"]
        clashes = [os.path.basename(p) for p in paths if os.path.lexists(os.path.join(dest, os.path.basename(p)))]
        move = mode == "move"
        if clashes and not (move and all(os.path.dirname(p.rstrip("/")) == dest for p in paths)):
            self.start_flow(self.sc_policy, "Names already exist", clashes,
                            lambda pol: self.go(self.sc_transfer, paths, dest, move, pol))
        else:
            self.start_flow(self.sc_transfer, paths, dest, move, "rename")

    def clear_clip(self):
        self.st["clip"] = None

    def sc_transfer(self, paths, dest, move, policy):
        def fn(ctx):
            ctx.log("%s %d item%s into %s" % ("Moving" if move else "Copying", len(paths), "" if len(paths) == 1 else "s", dest))
            made, errs = fs_transfer(paths, dest, move, policy, ctx)
            ctx.log("")
            ctx.log("== %d item%s %s ==" % (len(made), "" if len(made) == 1 else "s", "moved" if move else "copied"))
            for p, why in errs[:30]:
                ctx.log("!! %s  -  %s" % (os.path.basename(p), why))
            if move and made:
                self.post(self.clear_clip)
            return 2 if errs else 0
        self.sc_task("Moving" if move else "Copying", fn, extras=[("Open folder", lambda: self.storage_goto(dest))])

    def storage_delete(self):
        paths = self.sel_paths()
        if not paths:
            return
        shown = "\n".join("  " + os.path.basename(p) for p in paths[:8])
        more = "\n  ... and %d more" % (len(paths) - 8) if len(paths) > 8 else ""
        self.start_flow(self.sc_confirm, "Delete?", "Delete %d item%s?\n\n%s%s\n\nThis can't be undone." % (
            len(paths), "" if len(paths) == 1 else "s", shown, more),
            lambda: self.go(self.sc_delete, paths), "Delete", True)

    def sc_delete(self, paths):
        def fn(ctx):
            n, errs = fs_delete(paths, ctx)
            ctx.log("== deleted %d item%s ==" % (n, "" if n == 1 else "s"))
            for p, why in errs[:30]:
                ctx.log("!! %s  -  %s" % (os.path.basename(p), why))
            return 2 if errs else 0
        self.sc_task("Deleting", fn)

    def storage_rename(self):
        paths = self.sel_paths()
        if len(paths) == 1:
            self.start_flow(self.sc_input, "Rename", "New name:", os.path.basename(paths[0]),
                            lambda t: self.do_rename(paths[0], t), "Rename")

    def do_rename(self, path, text):
        try:
            fs_rename(path, text)
        except (ArchiveError, OSError) as ex:
            self.toast(str(ex))
            return
        self.leave_flow()

    def storage_newfolder(self):
        self.start_flow(self.sc_input, "New folder", "Folder name:", "", self.do_newfolder, "Create")

    def do_newfolder(self, text):
        name = text.strip()
        if not name or "/" in name or name in (".", ".."):
            self.toast("That isn't a valid folder name")
            return
        try:
            os.makedirs(os.path.join(self.st["dir"], name))
        except OSError as ex:
            self.toast("Can't create it: %s" % ex)
            return
        self.leave_flow()

    # ---- compress
    def storage_compress(self):
        paths = self.sel_paths()
        if paths:
            self.start_flow(self.sc_compress_opts, paths)

    def sc_compress_opts(self, paths):
        o = self.xopts
        if o.get("for") != paths:
            base = os.path.basename(paths[0].rstrip("/"))
            nm = (os.path.splitext(base)[0] if os.path.isfile(paths[0]) else base) if len(paths) == 1 else "Archive"
            o.clear()
            o.update({"for": paths, "name": nm + ".zip", "level": 6})
        f = self.screen("Compress to zip")
        self.label(f, "%d item%s  ->  a zip in\n%s" % (len(paths), "" if len(paths) == 1 else "s", self.st["dir"]),
                   size=15, margin=10, selectable=True)
        self.label(f, "Name", size=13, color=MUTED)
        ed = tg.EditText(self.a, o["name"], f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)
        safe(ed.sendtextevent, True)
        self.on_text(ed, lambda t: o.__setitem__("name", t), "flow")
        self.radio(f, "Compression", [(0, "Store (no compression, fastest)"), (1, "Fast"), (6, "Normal"),
                                      (9, "Maximum")], o, "level")
        self.btn(f, "Cancel", self.back, "flow", kind="tonal", h=60)
        self.btn(f, "Compress", lambda: self.compress_go(paths, safe(ed.gettext)), "flow", kind="primary", h=60)

    def compress_go(self, paths, name):
        name = (name or self.xopts.get("name") or "").strip()
        if not name or "/" in name:
            self.toast("Type a valid name")
            return
        if not name.lower().endswith(".zip"):
            name += ".zip"
        safe(self.a.hidesoftkeyboard)
        dest = unique_path(os.path.join(self.st["dir"], name))
        self.go(self.sc_compress_run, paths, dest, self.xopts.get("level", 6))

    def sc_compress_run(self, paths, dest, level):
        def fn(ctx):
            ctx.log("Creating %s from %d item%s ..." % (os.path.basename(dest), len(paths), "" if len(paths) == 1 else "s"))
            n = make_zip(dest, paths, level, ctx)
            ctx.log("")
            ctx.log("== done: %d entries, %s ==" % (n, human(os.path.getsize(dest))))
            return 0
        self.sc_task("Compressing", fn, extras=[("Open folder", lambda: self.storage_goto(os.path.dirname(dest)))])

    # ---- extract (all, or only what you picked)
    def storage_extract(self):
        paths = [p for p in self.sel_paths() if archive_kind(p)]
        if paths:
            self.extract_start([(p, None) for p in paths], "")

    def extract_start(self, jobs, base):
        self.xopts = {"dest": "named", "policy": "rename", "keep": True, "pwd": "", "dest_path": None}
        self.start_flow(self.sc_extract_opts, jobs, base)

    def extract_dest(self, path, o):
        stem = archive_stem(os.path.basename(path))
        d = o.get("dest", "named")
        if d == "here":
            return os.path.dirname(path)
        if d == "termux":
            return os.path.join(HOME, "extracted", stem)
        if d == "pick" and o.get("dest_path"):
            return o["dest_path"]
        return os.path.join(os.path.dirname(path), stem)

    def xpick(self, v):
        if v == "pick":
            self.go(self.sc_pick_file, "Pick the destination folder", None, self.xpicked, "dir")
            return False
        return True

    def xpicked(self, path):
        self.xopts["dest"], self.xopts["dest_path"] = "pick", path
        self.back()

    def sc_extract_opts(self, jobs, base=""):
        o = self.xopts
        f = self.screen("Extract")
        if len(jobs) == 1 and jobs[0][1] is not None:
            what = "%d selected item%s from %s" % (len(jobs[0][1]), "" if len(jobs[0][1]) == 1 else "s",
                                                   os.path.basename(jobs[0][0]))
        elif len(jobs) == 1:
            what = "everything in %s" % os.path.basename(jobs[0][0])
        else:
            what = "everything in %d archives" % len(jobs)
        self.label(f, "Extract " + what, size=15, margin=10, selectable=True)
        self.radio(f, "Where to", [("named", "A folder named after the archive, next to it"),
                                   ("here", "The same folder as the archive"),
                                   ("termux", "Termux home  (~/extracted/...)"),
                                   ("pick", "Another folder: " + (o.get("dest_path") or "choose..."))],
                   o, "dest", on_pick=self.xpick)
        self.radio(f, "If a name already exists", [("rename", "Keep both (new ones get a number)"),
                                                   ("overwrite", "Replace the existing ones"),
                                                   ("skip", "Skip the ones that exist")], o, "policy")
        self.toggle_btn(f, "Keep the folder structure", o, "keep")
        self.label(f, "Password (only if the archive asks for it)", size=13, color=MUTED)
        ed = tg.EditText(self.a, o.get("pwd", ""), f, singleline=True)
        self.fit(ed, 0, 56)
        safe(ed.settextcolor, TEXT)
        safe(ed.sendtextevent, True)
        self.on_text(ed, lambda t: o.__setitem__("pwd", t), "flow")
        self.btn(f, "Cancel", self.back, "flow", kind="tonal", h=60)
        self.btn(f, "Extract", lambda: (safe(self.a.hidesoftkeyboard), self.go(self.sc_extract_run, jobs, base)),
                 "flow", kind="primary", h=60)

    def sc_extract_run(self, jobs, base=""):
        o = dict(self.xopts)
        pw = o.get("pwd") or None
        dest0 = self.extract_dest(jobs[0][0], o)

        def fn(ctx):
            worst = 0
            for path, keys in jobs:
                check_cancel(ctx)
                dest = self.extract_dest(path, o)
                ctx.log("Opening %s ..." % os.path.basename(path))
                arch = open_archive(path)
                try:
                    ents = arch.expand(keys) if keys is not None else list(arch.entries)
                    ctx.log("Extracting %d entr%s into %s" % (len(ents), "y" if len(ents) == 1 else "ies", dest))
                    res = extract_entries(arch, ents, dest, base if keys is not None else "", o.get("policy", "rename"),
                                          not o.get("keep", True), pw, ctx)
                    worst = max(worst, report_extract(ctx, res, dest))
                finally:
                    arch.close()
            return worst
        self.sc_task("Extracting", fn, extras=[("Open folder", lambda: self.storage_goto(dest0))])

    # ---- one file: actions, text view
    def sc_file_actions(self, path):
        f = self.screen(os.path.basename(path))
        try:
            st = os.stat(path)
            info = "%s\n%s  \u00b7  modified %s" % (path, human(st.st_size), fmt_date(st.st_mtime))
        except OSError:
            info = path
        self.label(f, info, size=14, color=MUTED, selectable=True, margin=10)
        kind = archive_kind(path)
        if kind:
            self.btn(f, "Open (see inside, without extracting)", lambda: self.go(self.sc_archive, path, ""), "flow", kind="primary", h=60)
            self.btn(f, "Extract...", lambda: self.extract_start([(path, None)], ""), "flow", kind="tonal", h=60)
        if is_texty(path):
            self.btn(f, "View as text", lambda: self.go(self.sc_text_file, path), "flow", kind="tonal", h=60)
        if path.lower().endswith(".zip"):
            self.btn(f, "Sideload this zip to the target", lambda: self.start_flow(self.sideload_picked, path), "flow", kind="tonal", h=60)
        r = self.row(f)
        self.btn(r, "Copy", lambda: self.file_clip(path, "copy"), "flow", kind="tonal", h=56, weight=1)
        self.btn(r, "Move", lambda: self.file_clip(path, "move"), "flow", kind="tonal", h=56, weight=1)
        r = self.row(f)
        self.btn(r, "Rename", lambda: self.go(self.sc_input, "Rename", "New name:", os.path.basename(path),
                                              lambda t: self.do_rename(path, t), "Rename"), "flow", kind="tonal", h=56, weight=1)
        self.btn(r, "Compress", lambda: (self.xopts.clear(), self.go(self.sc_compress_opts, [path])), "flow", kind="tonal", h=56, weight=1)
        self.btn(f, "Delete", lambda: self.go(self.sc_confirm, "Delete?", "Delete %s?\n\nThis can't be undone." % os.path.basename(path),
                                              lambda: self.go(self.sc_delete, [path]), "Delete", True), "flow", kind="danger", h=60)

    def file_clip(self, path, mode):
        self.st["clip"] = (mode, [path])
        self.toast("%s - open the destination and tap Paste here" % ("Copied" if mode == "copy" else "Ready to move"))
        self.leave_flow()

    def sc_text_file(self, path):
        try:
            with open(path, "rb") as fh:
                data = fh.read(256 * 1024 + 1)
        except OSError as ex:
            self.sc_message(os.path.basename(path), "Can't read it: %s" % ex)
            return
        text = text_or_hex(data[:256 * 1024]) + ("\n\n[... only the first 256 KB is shown ...]" if len(data) > 256 * 1024 else "")
        self.sc_text(os.path.basename(path), text, [("Actions", lambda: self.go(self.sc_file_actions, path))])

    # ---- the archive viewer: look inside a zip/tar without extracting it
    def file_sig(self, path):
        try:
            return (os.path.getmtime(path), os.path.getsize(path))
        except OSError:
            return None

    def sc_archive(self, path, prefix=""):
        f = self.screen(os.path.basename(path))
        self.arc_tok += 1
        tok = self.arc_tok
        c = self.arc_cache
        if c and c["path"] == path and c["sig"] == self.file_sig(path):
            self.arc_build(f, c["obj"], path, prefix)
            return
        lab = self.label(f, "Opening the archive...", size=15, color=MUTED, margin=10)

        def work():
            try:
                obj, err = open_archive(path), None
            except Exception as ex:
                obj, err = None, ex
            self.post(lambda: self.arc_loaded(tok, f, lab, path, prefix, obj, err))
        threading.Thread(target=work, daemon=True).start()

    def arc_loaded(self, tok, f, lab, path, prefix, obj, err):
        alive = tok == self.arc_tok and self.in_flow and self.current and self.current[0] == self.sc_archive
        if not alive:
            if obj:
                obj.close()
            return
        if err is not None:
            self.set_text(lab, str(err), BAD)
            return
        if self.arc_cache and self.arc_cache["obj"] is not obj:
            self.arc_cache["obj"].close()
        self.arc_cache = {"path": path, "sig": self.file_sig(path), "obj": obj}
        safe(lab.setvisibility, tg.View.GONE)
        self.arc_build(f, obj, path, prefix)

    def arc_items(self, obj, prefix):
        out = []
        for it in obj.listdir(prefix):
            if it["dir"]:
                sub = "%d file%s  \u00b7  %s" % (it["count"], "" if it["count"] == 1 else "s", human(it["size"]))
            else:
                e = it["entry"]
                sub = human(it["size"]) + ("  \u00b7  %s packed" % human(it["csize"]) if it["csize"] and it["csize"] != it["size"] else "")
                if e.enc:
                    sub += "  \u00b7  password"
            out.append({"key": it["key"], "name": it["name"], "dir": it["dir"], "sub": sub})
        return out

    def arc_build(self, f, obj, path, prefix):
        self.arc = {"obj": obj, "path": path, "prefix": prefix}
        s = obj.stats()
        self.label(f, "%s  \u00b7  %d file%s, %d folder%s  \u00b7  %s (%s packed)" % (
            obj.kind.upper(), s["files"], "" if s["files"] == 1 else "s", s["dirs"], "" if s["dirs"] == 1 else "s",
            human(s["size"]), human(s["csize"])), size=13, color=MUTED, margin=6)
        self.label(f, "Inside:  /" + prefix, size=14, color=ACCENT, margin=6, selectable=True)
        r = self.row(f)
        self.arc_sel_btn = self.btn(r, "Select", self.arc_toggle_select, "flow", kind="tonal", h=52, weight=1)
        self.btn(r, "Extract all", lambda: self.extract_start([(path, None)], ""), "flow", kind="primary", h=52, weight=1)
        self.btn(r, "Info", lambda: self.go(self.sc_text, "Archive info", self.arc_info(obj)), "flow", kind="tonal", h=52, weight=1)
        r = self.row(f)
        self.btn(r, "File actions", lambda: self.go(self.sc_file_actions, path), "flow", kind="flat", h=48, weight=1)
        if obj.editable:
            self.btn(r, "Add files", lambda: self.arc_add(path, prefix), "flow", kind="flat", h=48, weight=1)
        self.arc_bar = self.col(f)
        self._abar_sig = self.abar_count = None
        self.arc_list = ItemList(self, f, "flow", self.arc_open, on_change=self.arc_changed, hide_dot=False)
        self.arc_list.set_items(self.arc_items(obj, prefix))
        self.arc_changed()

    def arc_info(self, obj):
        s = obj.stats()
        lines = ["File:        %s" % obj.path, "Type:        %s" % obj.kind, "Files:       %d" % s["files"],
                 "Folders:     %d" % s["dirs"], "Unpacked:    %s (%d bytes)" % (human(s["size"]), s["size"]),
                 "Packed:      %s" % human(s["csize"])]
        if s["size"]:
            lines.append("Ratio:       %d%%" % (100 * s["csize"] // max(1, s["size"])))
        if s["encrypted"]:
            lines.append("Password protected entries: %d" % s["encrypted"])
        if obj.kind == "zip" and obj.zf.comment:
            lines += ["", "Comment:", obj.zf.comment.decode("utf-8", "replace")]
        return "\n".join(lines)

    def arc_toggle_select(self):
        self.arc_list.set_selecting(not self.arc_list.selecting)

    def arc_open(self, it):
        a = self.arc
        if it["dir"]:
            self.go(self.sc_archive, a["path"], it["key"])
        else:
            self.go(self.sc_archive_item, a["path"], it["key"])

    def arc_changed(self):
        a, il = self.arc, self.arc_list
        n = len(il.selected)
        clip = self.arc_clip if (self.arc_clip and self.arc_clip[1] == a["path"]) else None
        one_file = bool(il.selecting and n == 1 and not next(iter(il.selected)).endswith("/"))
        sig = (il.selecting, (clip[0], len(clip[2])) if clip else None, (n == 1, one_file) if il.selecting else None)
        self.set_text(self.arc_sel_btn, "Done" if il.selecting else "Select")
        if sig == self._abar_sig:
            if il.selecting and self.abar_count is not None:
                self.set_text(self.abar_count, "%d selected" % n)
            return
        self._abar_sig, self.abar_count = sig, None
        self.clear("flow:bar", self.arc_bar)
        box, obj = self.arc_bar, a["obj"]
        if clip:
            self.label(box, "%d item%s to %s here" % (len(clip[2]), "" if len(clip[2]) == 1 else "s", clip[0]),
                       size=14, color=WARN, margin=6)
            r = self.row(box)
            self.btn(r, "Paste here", self.arc_paste, "flow:bar", kind="primary", h=52, weight=1)
            self.btn(r, "Cancel", self.arc_clip_clear, "flow:bar", kind="tonal", h=52, weight=1)
        if il.selecting:
            self.abar_count = self.label(box, "%d selected" % n, size=14, color=ACCENT, margin=6)
            r = self.row(box)
            self.btn(r, "All", il.select_all, "flow:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "None", il.select_none, "flow:bar", kind="tonal", h=52, weight=1)
            self.btn(r, "Extract", self.arc_extract_sel, "flow:bar", kind="primary", h=52, weight=1)
            if one_file:
                self.btn(r, "View", lambda: self.arc_view(a["path"], next(iter(il.selected))), "flow:bar", kind="tonal", h=52, weight=1)
            if obj.editable:
                r = self.row(box)
                self.btn(r, "Delete", self.arc_delete_sel, "flow:bar", kind="danger", h=52, weight=1)
                if n == 1:
                    self.btn(r, "Rename", self.arc_rename_sel, "flow:bar", kind="tonal", h=52, weight=1)
                self.btn(r, "Move", lambda: self.arc_clip_set("move"), "flow:bar", kind="tonal", h=52, weight=1)
                self.btn(r, "Copy", lambda: self.arc_clip_set("copy"), "flow:bar", kind="tonal", h=52, weight=1)

    def arc_extract_sel(self):
        keys = sorted(self.arc_list.selected)
        if not keys:
            self.toast("Select something first")
            return
        self.extract_start([(self.arc["path"], keys)], self.arc["prefix"])

    def sc_archive_item(self, path, key):
        obj = self.arc_cache["obj"] if self.arc_cache and self.arc_cache["path"] == path else None
        e = obj.find(key) if obj else None
        f = self.screen(key.rsplit("/", 1)[-1])
        if e is None:
            self.label(f, "This entry isn't there any more.", color=BAD)
            return
        self.label(f, "%s\n%s  \u00b7  %s packed%s%s" % (
            key, human(e.size), human(e.csize), "  \u00b7  password protected" if e.enc else "",
            "\nModified " + fmt_date(e.mtime) if e.mtime else ""), size=14, color=MUTED, selectable=True, margin=10)
        self.btn(f, "View (without extracting)", lambda: self.arc_view(path, key), "flow", kind="primary", h=60)
        self.btn(f, "Extract this file...", lambda: self.extract_start([(path, [key])], key.rsplit("/", 1)[0] + "/" if "/" in key else ""), "flow", kind="tonal", h=60)
        if obj.editable:
            parent = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
            self.btn(f, "Rename", lambda: self.go(self.sc_input, "Rename", "New name:", key.rsplit("/", 1)[-1],
                     lambda t: self.zip_edit_go("Renaming", path, parent, lambda ctx: zip_move(path, [key], parent.rstrip("/"), new_name=t, ctx=ctx)), "Rename"),
                     "flow", kind="tonal", h=60)
            self.btn(f, "Delete from the zip", lambda: self.go(self.sc_confirm, "Delete from the zip?", "Delete %s from the zip?\n\nThe zip is rewritten without it." % key,
                     lambda: self.zip_edit_go("Deleting", path, parent, lambda ctx: zip_delete(path, [key], ctx=ctx)), "Delete", True),
                     "flow", kind="danger", h=60)

    def arc_view(self, path, key, pwd=None):
        obj = self.arc_cache["obj"] if self.arc_cache and self.arc_cache["path"] == path else None
        e = obj.find(key) if obj else None
        if e is None:
            self.toast("That entry isn't there any more")
            return
        if e.enc and not pwd:
            self.go(self.sc_input, "Password", "This entry is password protected:", "", lambda t: (self.back(), self.arc_view(path, key, t)), "Open")
            return

        def work():
            try:
                data, trunc = read_preview(obj, e, 256 * 1024, pwd)
                text, err = text_or_hex(data) + ("\n\n[... only the first 256 KB is shown ...]" if trunc else ""), None
            except Exception as ex:
                text, err = None, ex
            self.post(lambda: self.arc_viewed(key, text, err))
        self.toast("Reading...")
        threading.Thread(target=work, daemon=True).start()

    def arc_viewed(self, key, text, err):
        if err is not None:
            self.toast("Can't read it: %s" % err)
        else:
            self.go(self.sc_text, key.rsplit("/", 1)[-1], text)

    # ---- editing a zip without extracting it
    def zip_edit_go(self, title, path, prefix, op):
        if self.arc_cache:
            self.arc_cache["obj"].close()
        self.arc_cache = None
        self.arc_clip = None

        def fn(ctx):
            ctx.log("%s ..." % title)
            op(ctx)
            with zipfile.ZipFile(path) as z:
                n = len(z.infolist())
            ctx.log("")
            ctx.log("== done - the zip now has %d entries (%s) ==" % (n, human(os.path.getsize(path))))
            return 0
        self.sc_task(title, fn, then=lambda: self.start_flow(self.sc_archive, path, prefix))

    def arc_delete_sel(self):
        keys, a = sorted(self.arc_list.selected), self.arc
        if not keys:
            return
        shown = "\n".join("  " + k for k in keys[:8]) + ("\n  ... and %d more" % (len(keys) - 8) if len(keys) > 8 else "")
        self.go(self.sc_confirm, "Delete from the zip?", "Delete %d item%s from the zip?\n\n%s\n\nThe zip is rewritten without them." % (
            len(keys), "" if len(keys) == 1 else "s", shown),
            lambda: self.go(self.zip_edit_screen, "Deleting", a["path"], a["prefix"], lambda ctx: zip_delete(a["path"], keys, ctx=ctx)), "Delete", True)

    def zip_edit_screen(self, title, path, prefix, op):
        self.zip_edit_go(title, path, prefix, op)

    def arc_rename_sel(self):
        a = self.arc
        keys = sorted(self.arc_list.selected)
        if len(keys) != 1:
            return
        k = keys[0]
        self.go(self.sc_input, "Rename", "New name:", k.rstrip("/").rsplit("/", 1)[-1],
                lambda t: self.go(self.zip_edit_screen, "Renaming", a["path"], a["prefix"],
                                  lambda ctx: zip_move(a["path"], [k], a["prefix"].rstrip("/"), new_name=t, ctx=ctx)), "Rename")

    def arc_clip_set(self, mode):
        keys = sorted(self.arc_list.selected)
        if not keys:
            return
        self.arc_clip = (mode, self.arc["path"], keys)
        self.arc_list.set_selecting(False)
        self.toast("Open the destination folder inside the zip and tap Paste here")

    def arc_clip_clear(self):
        self.arc_clip = None
        self.arc_changed()

    def arc_paste(self):
        mode, path, keys = self.arc_clip
        prefix = self.arc["prefix"]
        self.go(self.zip_edit_screen, "Moving" if mode == "move" else "Copying", path, prefix,
                lambda ctx: zip_move(path, keys, prefix.rstrip("/"), copy=(mode == "copy"), ctx=ctx))

    def arc_add(self, path, prefix):
        self.pick = {"dir": STORAGE_ROOT, "sel": set()}
        self.go(self.sc_pick_paths, "Pick files to add", lambda paths: self.go(
            self.zip_edit_screen, "Adding files", path, prefix, lambda ctx: zip_add(path, paths, prefix, ctx=ctx)))

    # ---- pick several files/folders from this phone
    def sc_pick_paths(self, title, on_done):
        f = self.screen(title)
        pk = self.pick
        self.label(f, "Tap files to pick them, tap folders to open them.", size=13, color=MUTED, margin=6)
        self.pk_path = self.label(f, pk["dir"], size=13, color=ACCENT, margin=6)
        self.pk_count = self.label(f, "", size=14, color=WARN, margin=6)
        r = self.row(f)
        self.btn(r, "Up", self.pick_up, "flow", kind="tonal", h=52, weight=1)
        self.btn(r, "Pick this folder", self.pick_this, "flow", kind="tonal", h=52, weight=1)
        self.btn(f, "Done - add the picked items", lambda: on_done(sorted(pk["sel"])) if pk["sel"] else self.toast("Pick something first"),
                 "flow", kind="primary", h=56)
        self.pk_list = ItemList(self, f, "flow", self.pick_open)
        self.pick_load()

    def pick_load(self):
        pk = self.pick
        items = []
        try:
            for e in os.scandir(pk["dir"]):
                full = os.path.join(pk["dir"], e.name)
                d = e.is_dir()
                items.append({"key": full, "name": ("[x] " if full in pk["sel"] else "") + e.name, "dir": d, "sub": ""})
        except OSError:
            pass
        items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
        self.pk_list.set_items(items)
        self.set_text(self.pk_path, pk["dir"])
        self.set_text(self.pk_count, "%d picked" % len(pk["sel"]) if pk["sel"] else "")

    def pick_open(self, it):
        pk = self.pick
        if it["dir"]:
            pk["dir"] = it["key"]
        else:
            pk["sel"].symmetric_difference_update({it["key"]})
        self.pick_load()

    def pick_up(self):
        pk = self.pick
        if pk["dir"].rstrip("/") != STORAGE_ROOT.rstrip("/"):
            pk["dir"] = os.path.dirname(pk["dir"].rstrip("/"))
            self.pick_load()

    def pick_this(self):
        pk = self.pick
        pk["sel"].symmetric_difference_update({pk["dir"].rstrip("/")})
        self.set_text(self.pk_count, "%d picked" % len(pk["sel"]) if pk["sel"] else "")

    # ---- Quit completely -------------------------------------------------------------------------------
    def sc_quit(self):
        self.start_flow(self.sc_confirm, "Quit completely?",
                        "This stops any running job, stops the adb server, closes FlashKit and force-stops Termux:GUI, "
                        "so nothing keeps running in the background.\n\nTo use FlashKit again, start it from Termux.",
                        self.quit_all, "Quit now", True)

    def quit_all(self):
        """Stop everything and force-stop Termux:GUI. The cleanup runs in detached shells that outlive us."""
        self.quitting = True
        self.stop_evt.set()
        self.probe_evt.set()
        jobs = [self.busy] if self.busy else []
        jobs += [jv.job for jv in self.jobviews if not jv.finished and jv.job not in jobs]
        root_lines, user_lines = [], []
        for j in jobs:
            try:
                j.cancel()
            except Exception:
                pass
            if isinstance(getattr(j, "token", None), str) and not j.token.startswith("task"):
                (root_lines if j.root else user_lines).append(kill_script(j.token))
        root_lines += ["adb kill-server", "pkill -9 -x adb", "pkill -9 -x fastboot", "sleep 0.4", "am force-stop com.termux.gui"]
        user_lines.append("adb kill-server")
        for cmd in (["su", "-c", ENV + "\n".join(root_lines)], ["sh", "-c", "\n".join(user_lines)]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception:
                log("quit: " + traceback.format_exc())
        if self.arc_cache:
            self.arc_cache["obj"].close()
        ROOT.close()
        safe(self.a.finish)
        time.sleep(0.5)
        os._exit(0)

    # ---- Settings -------------------------------------------------------------
    def build_settings(self):
        ll = self.pages["settings"][1]
        self.clear("settings", ll)
        self.label(ll, "Settings", size=13, color=MUTED, margin=8)
        for label, key in (("Auto-fix connection problems", "autofix"),
                           ("Keep the screen on while a job runs", "keepon"),
                           ("Show the recovery's install log while sideloading", "sideload_log"),
                           ("Check the device automatically (uses root every few seconds)", "autodetect")):
            self.cfg_toggle(ll, label, key)
        self.label(ll, "Diagnostics", size=13, color=MUTED, margin=8)
        self.btn(ll, "Run full diagnostics + auto-fix", self.run_diag, "settings", kind="primary", h=60)
        self.btn(ll, "Repair adb/fastboot (updates Termux packages)", lambda: self.start_flow(self.flow_repair),
                 "settings", kind="tonal", h=60)
        self.label(ll, "About", size=13, color=MUTED, margin=8)
        self.label(ll, "FlashKit GUI %s\nMade by @Shivraj_editx\nt.me/Shivraj_editx" % VERSION, size=16,
                   color=ACCENT, margin=8, selectable=True)
        self.btn(ll, "Open t.me/Shivraj_editx", lambda: self.open_url(CREDIT_URL), "settings", kind="tonal", h=56)
        self.btn(ll, "Quit completely (force stop)", self.sc_quit, "settings", kind="danger", h=56)
        self.label(ll, "Log file: %s\nadb + fastboot run through su; this app itself runs as "
                   "your normal Termux user." % LOGFILE, size=14, color=MUTED, margin=8, selectable=True)

    def cfg_toggle(self, parent, label, key):
        def fmt():
            return ("[x]  " if self.cfg.get(key) else "[  ]  ") + label
        b = self.btn(parent, fmt(), None, "settings", kind="flat", h=56, pill=False)
        safe(b.setgravity, 0, 1)

        def flip():
            self.cfg[key] = not self.cfg.get(key)
            save_cfg(self.cfg)
            safe(b.settext, fmt())
        self.clicks.append((b, flip, "settings"))

    def run_diag(self):
        if self.busy:
            self.toast("A job is still running")
            return
        script = ('echo "== root =="; id; '
                  'echo "== tools =="; command -v adb; adb version 2>&1 | head -n 1; command -v fastboot; '
                  'fastboot --version 2>&1 | head -n 1; '
                  'echo "== usb nodes =="; ls -l /dev/bus/usb/*/* 2>&1 | head -n 12; '
                  'echo "== usb devices (id | maker | name / interfaces) =="; ' + uinfo_script() + '; '
                  'echo "== adb =="; adb devices -l 2>&1; '
                  'echo "== fastboot =="; fastboot devices -l 2>&1; '
                  'echo "== storage =="; df -h %s 2>&1 | tail -n 1' % shq(STORAGE_ROOT))
        self.start_flow(self.sc_job, "Diagnostics", script, 90, None, self.analyze)

    def analyze(self):
        """Runs after the raw checks: explains what it found and repairs what it can."""
        out = ["-------- findings --------"]
        if not shutil.which("adb") or not shutil.which("fastboot"):
            out.append("! adb/fastboot missing - installing android-tools...")
            rc = subprocess.call(["pkg", "install", "-y", "android-tools"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            out.append("  install %s" % ("ok" if rc == 0 else "FAILED - check internet"))
        st = probe()
        if not st["root"]:
            out.append("X Root not granted to Termux: %s" % st["err"])
            out.append("  -> allow superuser for Termux in Magisk/KernelSU")
            return "\n".join(out)
        out.append("OK root works")
        th = tool_health(root=True)
        if th["adb"] or th["fastboot"]:
            out.append("X adb/fastboot can't start: " + (th["adb"] or th["fastboot"]))
            out.append("  Not your cable or the target - the programs themselves crash when launched.")
            out.append("  -> use Settings > Repair adb/fastboot")
            return "\n".join(out)
        if not st["adb"] and not st["fb"]:
            if st["usb"] == 0:
                out.append("X Nothing on the OTG port.")
                out.append("  -> try another OTG adapter/cable (many are charge-only) and re-plug")
            else:
                out.append("! Target visible on USB but not answering; restarting the adb server...")
                su_run("adb kill-server; sleep 1; chmod -R 666 /dev/bus/usb/*/* 2>/dev/null; adb start-server",
                       timeout=25)
                time.sleep(1.5)
                st = probe()
        for ser, s in st["adb"]:
            out.append({"device": "OK adb device %s is ready",
                        "sideload": "OK %s is waiting in sideload mode",
                        "recovery": "OK %s is in recovery",
                        "unauthorized": "! %s is unauthorized -> accept the USB-debugging prompt on it",
                        "offline": "! %s is offline -> re-plug it (auto-fix already tried a restart)"}
                       .get(s, "? %s state: " + s) % ser)
        for ser in st["fb"]:
            out.append("OK fastboot device %s is ready" % ser)
        if not (st["adb"] or st["fb"]) and st["usb"] > 0:
            uh = usb_headline(st)
            if uh:
                out.append("X " + uh[0])
                out.append("  " + USB_HINTS[uh[1]])
            else:
                out.append("X Still nothing from adb/fastboot. -> enable USB debugging on the target, or boot "
                           "it to bootloader/recovery; then run this again.")
        return "\n".join(out)

    # ---- event loop -----------------------------------------------------------
    def dispatch(self, hid):
        for v, fn, scope in reversed(self.clicks):
            if v == hid:
                fn()
                return True
        return False

    def dispatch_text(self, val):
        hid = val.get("id")
        for v, fn, scope in reversed(self.texts):
            if v == hid:
                fn(val.get("text", ""))
                return

    def handle(self, ev):
        t, v = ev.type, (ev.value or {})
        if t == tg.Event.click:
            self.dispatch(v.get("id"))
        elif t == tg.Event.text:
            self.dispatch_text(v)
        elif t == tg.Event.back:
            self.on_back()
        elif t == tg.Event.destroy:
            if v.get("finishing", True):
                self.quit = True
        elif t == getattr(tg.Event, "pause", "pause"):
            self.paused = True
        elif t == tg.Event.resume:
            self.paused = False
            self.probe_soon()

    def tick(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    log("ui task failed: " + traceback.format_exc())
        except queue.Empty:
            pass
        st = None
        try:
            while True:
                st = self.probe_q.get_nowait()
        except queue.Empty:
            pass
        if st is not None:
            self.apply_status(st)
        for jv in list(self.jobviews):
            jv.tick()
        now = time.time()
        for key, (due, fn) in list(self.timers.items()):
            if now >= due:
                del self.timers[key]
                fn()

    def fail(self):
        tb = traceback.format_exc()
        log("ERROR " + tb)
        try:
            if self.busy is None:
                self.stack, self.current = [], None
            f = self.screen("Something went wrong", back=False)
            self.label(f, "This was written to %s\n\n%s" % (LOGFILE, tb[-1800:]), size=12, color=BAD,
                       selectable=True, margin=8)
            self.btn(f, "Copy error", lambda: self.copy_output(tb), "flow", kind="tonal", h=56)
            self.btn(f, "Continue", self.leave_flow, "flow", kind="primary", h=60)
        except Exception:
            log("could not even show the error screen: " + traceback.format_exc())

    def next_event(self):
        return self.c.checkevent()

    def run(self):
        self.start_threads()
        self.probe_soon()
        while not self.quit:
            try:
                ev = self.next_event()
                if ev is not None:
                    self.last_ev = time.time()
                    self.handle(ev)
                    continue
                self.tick()
            except KeyboardInterrupt:
                break
            except Exception:
                self.fail()
            time.sleep(0.03 if time.time() - self.last_ev < 3 else 0.08)
        self.stop_evt.set()
        self.probe_evt.set()


# Icon art for the Home tiles, the bottom bar and the pill buttons: small PNGs drawn offline,
# embedded so the app stays a single file. If one is missing the screen just shows its text.
_ICON_B64 = {
    "cap_danger_L": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEVfGhdfGhdfGhdfGhdfGhdfGhdfGhdfGhdfGhdf"
        "GhdfGhdfGhdfGhdfGhdfGhdfGhcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPvinvAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAgFJREFUeNrt21FugzAQhOEYY2KD7bn/bRNVrZS2JAow/0OU+ACfaCFg786eTp4VxjHnpdam"
        "6zJwQ7pqXTfroBjPc9O/deQSU1kBj6AhzV331s6/+t417kbDVPV4bSaH3CUvGoueWAC5CQ1ZcqNTkxuNVTKjoUhudGyy"
        "o1lyo0OVHU1ddjRLbnTjXX8KDVV2NDbZ0dhlR/ebIkwRpghThLmOhiY7uveZf4jO8qNZfjTJjw4dQKv8aJYfHeVHjz71"
        "q2iRH40C0Aqgk/xo6ABa5EcHAWgBUOOFirhQERcq69vpD+p7Rm/QswC0AugoAC0A6r1N3+gkAF0AdOgAmgSgBUBNH/vf"
        "aBSAngl0BlD/v1Tmd/4POhFoJtAFQIH7JPu79AsdBaCJQPPLoPW90Uag+qAf9AXQ9t6/fQR9nTc/8uFDPtHIZgLZ9iAb"
        "NGYriWx6ke05cpBAjjzM4Qw5RiIHXuRozhQRkHIHUphhSkhIsQspyzEFRKTUyRRlkfIxUuhmSvJI84BpcyANGaZ1hDS5"
        "mHYc0jhkWpxMMxZpGzMNbqQVz4QGmHgDE8RAIiNMuIWJ4TCBISTaxISwmLgYE2xjInhMWJCJNTIBTCYqyoRamfgtFBRm"
        "Is1M+BqKiTOBdiZ6Dw0JQOMMzOAFNCICDbNAYzfQgBA1ygQNXVHjYdgg2/rI3QU4Eokxt159PwAAAABJRU5ErkJggg=="
    ),
    "cap_danger_R": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEVfGhdfGhdfGhdfGhdfGhdfGhdfGhdfGhdfGhdf"
        "GhdfGhdfGhdfGhdfGhdfGhdfGhcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPvinvAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAeBJREFUeNrt2+1ygyAQheEigsqH5/7vtpomnTTNNG2y74+dygU8QwANwtk3bW1trc1zGofw"
        "ZtN01dZe5xyDLXqm6zwGa3RvveTBHD0N9DSYo7tbUjBH93F4qrt62JYIoFIdAfTPrH7ZSgRQaQ4Aqp4AdBvaAUC1ZgDd"
        "JmwAUK0jgG7LgEBVAoCqDQCqNQLog7WlZ1sm0J9UEaoIVYQqQn0NVSTQ+0/Bi6h6AFBVAr33JnwdVSLQdQDQ78NqgWoi"
        "0NtnwAZtBHrzEjBCv64AI1SFQDUSaCPQ67myQzuBXnVVRFdFdNUSbQT6uVZN0UqglzeAKXr5E7RFO4Gep8oYLQS6BgD9"
        "+P3W6EygnUBPWwBzdCLQQqD71lrEoNqjiUAXAq0Euj3+ImZKxEwB6ESgC4E2N6gO9EAP9EAP9J+9+Rc3f9HJy7YH2aBV"
        "N5ve5OVDAvnkKW4+I6OXT/PZy3EHcjBTvBx2dS8HiMhRZ/VyfNy8HMl3L9cc3cvVUfNyHVe8XHFmL9fG0clVfPUSb0hO"
        "IiPVSQwHCQxFJyGs7CTYlp2EBbOPACYRFSVCrUT8FggKE5FmIHxNxMSBQDsRvQeKBIByBqDwAigRsS9msS+7sS8Qsi9l"
        "Mi+6Mi4PMy1ksy65ewf7l4uDW7sZ2AAAAABJRU5ErkJggg=="
    ),
    "cap_flat_L": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUbHycbHycbHycbHycbHycbHycbHycbHycbHycb"
        "HycbHycbHycbHycbHycbHycbHycAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAqXX8cAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAgFJREFUeNrt21FugzAQhOEYY2KD7bn/bRNVrZS2JAow/0OU+ACfaCFg786eTp4VxjHnpdam"
        "6zJwQ7pqXTfroBjPc9O/deQSU1kBj6AhzV331s6/+t417kbDVPV4bSaH3CUvGoueWAC5CQ1ZcqNTkxuNVTKjoUhudGyy"
        "o1lyo0OVHU1ddjRLbnTjXX8KDVV2NDbZ0dhlR/ebIkwRpghThLmOhiY7uveZf4jO8qNZfjTJjw4dQKv8aJYfHeVHjz71"
        "q2iRH40C0Aqgk/xo6ABa5EcHAWgBUOOFirhQERcq69vpD+p7Rm/QswC0AugoAC0A6r1N3+gkAF0AdOgAmgSgBUBNH/vf"
        "aBSAngl0BlD/v1Tmd/4POhFoJtAFQIH7JPu79AsdBaCJQPPLoPW90Uag+qAf9AXQ9t6/fQR9nTc/8uFDPtHIZgLZ9iAb"
        "NGYriWx6ke05cpBAjjzM4Qw5RiIHXuRozhQRkHIHUphhSkhIsQspyzEFRKTUyRRlkfIxUuhmSvJI84BpcyANGaZ1hDS5"
        "mHYc0jhkWpxMMxZpGzMNbqQVz4QGmHgDE8RAIiNMuIWJ4TCBISTaxISwmLgYE2xjInhMWJCJNTIBTCYqyoRamfgtFBRm"
        "Is1M+BqKiTOBdiZ6Dw0JQOMMzOAFNCICDbNAYzfQgBA1ygQNXVHjYdgg2/rI3QU4Eokxt159PwAAAABJRU5ErkJggg=="
    ),
    "cap_flat_R": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUbHycbHycbHycbHycbHycbHycbHycbHycbHycb"
        "HycbHycbHycbHycbHycbHycbHycAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAqXX8cAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAeBJREFUeNrt2+1ygyAQheEigsqH5/7vtpomnTTNNG2y74+dygU8QwANwtk3bW1trc1zGofw"
        "ZtN01dZe5xyDLXqm6zwGa3RvveTBHD0N9DSYo7tbUjBH93F4qrt62JYIoFIdAfTPrH7ZSgRQaQ4Aqp4AdBvaAUC1ZgDd"
        "JmwAUK0jgG7LgEBVAoCqDQCqNQLog7WlZ1sm0J9UEaoIVYQqQn0NVSTQ+0/Bi6h6AFBVAr33JnwdVSLQdQDQ78NqgWoi"
        "0NtnwAZtBHrzEjBCv64AI1SFQDUSaCPQ67myQzuBXnVVRFdFdNUSbQT6uVZN0UqglzeAKXr5E7RFO4Gep8oYLQS6BgD9"
        "+P3W6EygnUBPWwBzdCLQQqD71lrEoNqjiUAXAq0Euj3+ImZKxEwB6ESgC4E2N6gO9EAP9EAP9J+9+Rc3f9HJy7YH2aBV"
        "N5ve5OVDAvnkKW4+I6OXT/PZy3EHcjBTvBx2dS8HiMhRZ/VyfNy8HMl3L9cc3cvVUfNyHVe8XHFmL9fG0clVfPUSb0hO"
        "IiPVSQwHCQxFJyGs7CTYlp2EBbOPACYRFSVCrUT8FggKE5FmIHxNxMSBQDsRvQeKBIByBqDwAigRsS9msS+7sS8Qsi9l"
        "Mi+6Mi4PMy1ksy65ewf7l4uDW7sZ2AAAAABJRU5ErkJggg=="
    ),
    "cap_primary_L": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUtQnUtQnUtQnUtQnUtQnUtQnUtQnUtQnUtQnUt"
        "QnUtQnUtQnUtQnUtQnUtQnUtQnUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC44gkxAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAgFJREFUeNrt21FugzAQhOEYY2KD7bn/bRNVrZS2JAow/0OU+ACfaCFg786eTp4VxjHnpdam"
        "6zJwQ7pqXTfroBjPc9O/deQSU1kBj6AhzV331s6/+t417kbDVPV4bSaH3CUvGoueWAC5CQ1ZcqNTkxuNVTKjoUhudGyy"
        "o1lyo0OVHU1ddjRLbnTjXX8KDVV2NDbZ0dhlR/ebIkwRpghThLmOhiY7uveZf4jO8qNZfjTJjw4dQKv8aJYfHeVHjz71"
        "q2iRH40C0Aqgk/xo6ABa5EcHAWgBUOOFirhQERcq69vpD+p7Rm/QswC0AugoAC0A6r1N3+gkAF0AdOgAmgSgBUBNH/vf"
        "aBSAngl0BlD/v1Tmd/4POhFoJtAFQIH7JPu79AsdBaCJQPPLoPW90Uag+qAf9AXQ9t6/fQR9nTc/8uFDPtHIZgLZ9iAb"
        "NGYriWx6ke05cpBAjjzM4Qw5RiIHXuRozhQRkHIHUphhSkhIsQspyzEFRKTUyRRlkfIxUuhmSvJI84BpcyANGaZ1hDS5"
        "mHYc0jhkWpxMMxZpGzMNbqQVz4QGmHgDE8RAIiNMuIWJ4TCBISTaxISwmLgYE2xjInhMWJCJNTIBTCYqyoRamfgtFBRm"
        "Is1M+BqKiTOBdiZ6Dw0JQOMMzOAFNCICDbNAYzfQgBA1ygQNXVHjYdgg2/rI3QU4Eokxt159PwAAAABJRU5ErkJggg=="
    ),
    "cap_primary_R": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUtQnUtQnUtQnUtQnUtQnUtQnUtQnUtQnUtQnUt"
        "QnUtQnUtQnUtQnUtQnUtQnUtQnUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC44gkxAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAeBJREFUeNrt2+1ygyAQheEigsqH5/7vtpomnTTNNG2y74+dygU8QwANwtk3bW1trc1zGofw"
        "ZtN01dZe5xyDLXqm6zwGa3RvveTBHD0N9DSYo7tbUjBH93F4qrt62JYIoFIdAfTPrH7ZSgRQaQ4Aqp4AdBvaAUC1ZgDd"
        "JmwAUK0jgG7LgEBVAoCqDQCqNQLog7WlZ1sm0J9UEaoIVYQqQn0NVSTQ+0/Bi6h6AFBVAr33JnwdVSLQdQDQ78NqgWoi"
        "0NtnwAZtBHrzEjBCv64AI1SFQDUSaCPQ67myQzuBXnVVRFdFdNUSbQT6uVZN0UqglzeAKXr5E7RFO4Gep8oYLQS6BgD9"
        "+P3W6EygnUBPWwBzdCLQQqD71lrEoNqjiUAXAq0Euj3+ImZKxEwB6ESgC4E2N6gO9EAP9EAP9J+9+Rc3f9HJy7YH2aBV"
        "N5ve5OVDAvnkKW4+I6OXT/PZy3EHcjBTvBx2dS8HiMhRZ/VyfNy8HMl3L9cc3cvVUfNyHVe8XHFmL9fG0clVfPUSb0hO"
        "IiPVSQwHCQxFJyGs7CTYlp2EBbOPACYRFSVCrUT8FggKE5FmIHxNxMSBQDsRvQeKBIByBqDwAigRsS9msS+7sS8Qsi9l"
        "Mi+6Mi4PMy1ksy65ewf7l4uDW7sZ2AAAAABJRU5ErkJggg=="
    ),
    "cap_tonal_L": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUqMDwqMDwqMDwqMDwqMDwqMDwqMDwqMDwqMDwq"
        "MDwqMDwqMDwqMDwqMDwqMDwqMDwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD4AWjAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAgFJREFUeNrt21FugzAQhOEYY2KD7bn/bRNVrZS2JAow/0OU+ACfaCFg786eTp4VxjHnpdam"
        "6zJwQ7pqXTfroBjPc9O/deQSU1kBj6AhzV331s6/+t417kbDVPV4bSaH3CUvGoueWAC5CQ1ZcqNTkxuNVTKjoUhudGyy"
        "o1lyo0OVHU1ddjRLbnTjXX8KDVV2NDbZ0dhlR/ebIkwRpghThLmOhiY7uveZf4jO8qNZfjTJjw4dQKv8aJYfHeVHjz71"
        "q2iRH40C0Aqgk/xo6ABa5EcHAWgBUOOFirhQERcq69vpD+p7Rm/QswC0AugoAC0A6r1N3+gkAF0AdOgAmgSgBUBNH/vf"
        "aBSAngl0BlD/v1Tmd/4POhFoJtAFQIH7JPu79AsdBaCJQPPLoPW90Uag+qAf9AXQ9t6/fQR9nTc/8uFDPtHIZgLZ9iAb"
        "NGYriWx6ke05cpBAjjzM4Qw5RiIHXuRozhQRkHIHUphhSkhIsQspyzEFRKTUyRRlkfIxUuhmSvJI84BpcyANGaZ1hDS5"
        "mHYc0jhkWpxMMxZpGzMNbqQVz4QGmHgDE8RAIiNMuIWJ4TCBISTaxISwmLgYE2xjInhMWJCJNTIBTCYqyoRamfgtFBRm"
        "Is1M+BqKiTOBdiZ6Dw0JQOMMzOAFNCICDbNAYzfQgBA1ygQNXVHjYdgg2/rI3QU4Eokxt159PwAAAABJRU5ErkJggg=="
    ),
    "cap_tonal_R": (
        "iVBORw0KGgoAAAANSUhEUgAAAFQAAACoCAMAAABJ5TmRAAADAFBMVEUqMDwqMDwqMDwqMDwqMDwqMDwqMDwqMDwqMDwq"
        "MDwqMDwqMDwqMDwqMDwqMDwqMDwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD4AWjAAAAEHRSTlMAESIzRFVm"
        "d4iZqrvM3e7/dpUBFQAAAeBJREFUeNrt2+1ygyAQheEigsqH5/7vtpomnTTNNG2y74+dygU8QwANwtk3bW1trc1zGofw"
        "ZtN01dZe5xyDLXqm6zwGa3RvveTBHD0N9DSYo7tbUjBH93F4qrt62JYIoFIdAfTPrH7ZSgRQaQ4Aqp4AdBvaAUC1ZgDd"
        "JmwAUK0jgG7LgEBVAoCqDQCqNQLog7WlZ1sm0J9UEaoIVYQqQn0NVSTQ+0/Bi6h6AFBVAr33JnwdVSLQdQDQ78NqgWoi"
        "0NtnwAZtBHrzEjBCv64AI1SFQDUSaCPQ67myQzuBXnVVRFdFdNUSbQT6uVZN0UqglzeAKXr5E7RFO4Gep8oYLQS6BgD9"
        "+P3W6EygnUBPWwBzdCLQQqD71lrEoNqjiUAXAq0Euj3+ImZKxEwB6ESgC4E2N6gO9EAP9EAP9J+9+Rc3f9HJy7YH2aBV"
        "N5ve5OVDAvnkKW4+I6OXT/PZy3EHcjBTvBx2dS8HiMhRZ/VyfNy8HMl3L9cc3cvVUfNyHVe8XHFmL9fG0clVfPUSb0hO"
        "IiPVSQwHCQxFJyGs7CTYlp2EBbOPACYRFSVCrUT8FggKE5FmIHxNxMSBQDsRvQeKBIByBqDwAigRsS9msS+7sS8Qsi9l"
        "Mi+6Mi4PMy1ksy65ewf7l4uDW7sZ2AAAAABJRU5ErkJggg=="
    ),
    "nav_console": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEWkrLWnrdGiq7t/f3+nsMCnsMCjrLt/f/+oscF/"
        "v79/f7R///+ZzMwAAP+lsL8AAACjq7urtMSnp7D///+iqrqjq7uiqbmjq7sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAR"
        "CRypAAAAGHRSTlMOFcsC3JyYAkkEBQIFAUQA+/4KAbhCNHI2QoHXAAACdUlEQVR42u2abW+jMAyAHRL6su3OxKHh///T"
        "cxoC7LoWbrK05GR/qFSKwE/8GqeAjQsogAIogAIogAIogAIogAIogAIogAIogAIogAIowP8AEHakbgAyR+75VSvAfXkN"
        "0AsBQ3zPuU4AVv/Sd8OOdP3lDalGgIAweu/3APgWFwUJQE7/uK99YZjkCKQAbIDhoP4J4SZGAGIGGI/rz0JSCVUIgPCW"
        "9d+Pgfw5Sb0ZZA3gJkOGs+WzNGrglgE6qssCATqfF5YrwWv3jtkGhLYiAE6hs2uTmYahB4RnrcQHZlSpMJYCoAwAOPnk"
        "IIDXp+V6rBnAmHuUegf4xMnnaKkVgGitVfjeIAAs5cCnQGjQAqkhmgnclwTVAyCH8VysWEnbHkBgI7hihOmRoH4AfiT0"
        "KwG1B5A0m9zc+MDfTVsLAAEwLl0ntgeQDFBc6NSgC21DYKRgw+dNfwMWiG5d//R8m7u49xQdoX4A7kbXMsBrDusbgFpo"
        "JRb3dxHBBv7ajbMMw4kCVG4BU9Y/tUKUm+sijoPCXhvpRqfk8I+TCh+x9hhI6vkhJvdfrn6ap1QeA+c0nmP3N5vd10b/"
        "jt7q3FIGKmrxrv034Mc5Zc4r0ei34qItqBFDRQBIZSPzMF83JhYx6cdM+kWP8bMAUAZbnDS7JW8+StflsOYaV9dcqAyG"
        "NknziQyyHiQ4G43/MBsVHE+LjddpqcEH9D/VN16/z3eHQycEaeQid9gneMRESL07EAN9FBqLSgMktyBzub2UiwGUPCKT"
        "PWYN5hCnxVoBWCztyFn4qFv/aqAACqAACqAACqAACqAACqAACqAACqAACqAACqAA35A/lk0+tskJWfoAAAAASUVORK5C"
        "YII="
    ),
    "nav_console_sel": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEXU3fY6SnQAAP9DU3xfbZSsuNY7THcyMnw6SnQt"
        "VXZ1dnwtPWlVVaqGkrV5hqq7xuL///8/Pz85SXQAAADb5P7k7P5VVVU7SnYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/"
        "yuIIAAAAGHRSTlP/SAH////gA58hA/8D////AQT+AP//AxEGd0AVAAAEXUlEQVR42u2ca5OrIAyGAyo3te2Cl///Tw8I"
        "WnVttXtSCzN9v2xnZ8+ZPJKEGEjBJC74AnwBvgDHRM8WGgDvOFfyA89WVbyS/wvQ8fChP12TBfzPAF01mE6IEEyfLiYE"
        "IbIaluJPANVg/CdMvzM4DOLWoqKvAjjzidBRSBBlQ5G+AtAZIwnT0YiJfjDqIACVcZnvl8EiqGMAljQ6850IN9URAG56"
        "oaMUk0bRPQCqDNHRivyOBFg/fiN0xLKRUD0DUNZ9WMwAzLoRfwxg7WdR2++0IoCV/Tp+LQlgkX5SsN8RVFsAlCZivyNQ"
        "GwDciFQAWH+jvwASst9m07sTwVQ/EJ2OmJgCOQBw2uukRMYtGcYFEGkBsD6URZCgAwUnqmYASjGdmkIuBf/+iLUApc52"
        "hJiJugnghrUAeXGFPRUX1CUAxAXIirZtmx3ZP4EccQlgKOJQFiCHXetHBpxF6A0dAJBSUHbUfoeAsgbEJSJwHoRSRBTH"
        "7W9awAhmJv0KGJRNOG+DaTvyfwZ1iRLGnQXA8aCygMH+Ot9Joz5SWsCqiACrDL0OdtkHa7eCZ0+3zL0XYUQBUzcLcJMM"
        "JYQHz7Bm1TbVZ/uoOVIeAo4TAgEgK2v7Aa7ZXrRj5aEfQEuiHkD7UIC8PAHABYFdAYLoQpn/6faq8oQVsJsxIMXw5EI+"
        "G1kLixMAGFfWhRiqC2XjhgbXB26ECGCjGB1A60sz7mnbBJgAyqZRpZEByhxGN6rfDSAtQI8N4GwMBHB5MwB5E4B1o0CQ"
        "pQlgvShtgHrMREWaLjSFQKHfD6A4chqdJaFCn5CF0PeBetwGmmEbmDVSwkfkfeAHp6m4qEaHj34bc8X1qFBnY5YSUmEX"
        "c3rp/g6nneR/i1zM/eCW06EaberZb1ftCORyWhmJ6kIuA01V0JsBbBLiYIxEdSHXnrtXccWSAADXhaRR4NpCGP+XbzY4"
        "y/N81W28y70zh00axYWkGboSKEHgXwOchWW5aELMpUudX9HaKsNRGVpj69L65H8tnst3th6/r70WAq6xZRSiD82T5rYa"
        "vLaQ7n13GsmH8hd6o1Aj7QK+N3rDKYcuhwkAxYH8pQkYzrgJ0gHBkQ57Cw3K87cLMJ7QUKSSWme2ctiNAShw6rhwVBwO"
        "+ZCuGdgsuSuNJDE7paRU6eREZqeUCR50j6es41UDpPeyEzXeGRoBcGrS08TWlz2cE7GkHIgnfeFpdmUL7ld2e5ZcACzu"
        "zHXphAGZXbyE+cyDTM/+xcVXngYBeXTxNREC8vjq8UDA0rJ/ff2+inb6YXqN588HINz8ScT7V7+2//cIihvhiPUO/sYA"
        "x9YQUKxDNGJrhGZzDMtNkUWHIIgx/PAgXBUbgjNf8RdGEd0QZjzjNNZ5jOxeHAZ1/6CTn2fw06C0+8M4Lg3TuOJTFG4g"
        "Vzq/eTYXDYfmoftekrPV9/6pV93/jaTzinefGvhXnO/OpB/7UgBKu9PFj30twPd7Jb4AX4DE9Q9UvO7JGqds7QAAAABJ"
        "RU5ErkJggg=="
    ),
    "nav_files": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEWmqLWjq7upq+enr8AAAP9/f79/f/9/v79///+o"
        "tMTMzMwAAACiq7v///+np6+rs8Sjq7uiqrqjqrp/f3+err2lqruiqrqttcQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABg"
        "dHsrAAAAGHRSTlMMiRDeAQQCBAJBBQD7AQr+Qr1wAhAsyhBilV47AAABiUlEQVR42u3Z626EIBAFYNTecWbwAr7/m5bu"
        "Nu26SioNZofknB+bSFbDFxgwYmzlMQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPB9A6lQHo9e2+hSsC0HP8YZ75J/MS"
        "W6sBtLH349Cv0znL1YwAN53IHUBkPFtgShXvuOn9NUxTBQCy4373exlOLgNTpv+mTwB6ObkMCgH4u7c7gLC0tIm6KURB"
        "LvPF3yyjs7t4pNm7gbXVwHAB+HXzcB0RxwuvUnKDKAtwL+Z3mvDku0RhfG0Qk0rAqmDb5NpUcIM4E0BsQlLQmzKlfCYg"
        "NvsUIO5wVj8gXjVSNSBeJsugDkBca9plvl1CF/ZVAXbWGlMXwG5eI7gyQOq9CQAAAAAAAAAAAAAAbQD6oD+jGeCP/Fkx"
        "oA/DgQTFADkUxYCcKAOEbIDRBGDrMgHSkKZPi3ayo+Ska7jQIUGpQz6ypnWH817ubLHYkzInBD9pA1jijEz6RuBRAQAA"
        "AAAAAAAAAAAAAAAAAAAAAAAA4F/5BHwqJBDR8LJOAAAAAElFTkSuQmCC"
    ),
    "nav_files_sel": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEU6SnQAAP/T3PZEU3xicJewu9k7THcyMnw6SnQt"
        "VXZ1dnxVVaosPWl6h6uKl7j///+7xuE/Pz85SXQAAADb5P5VVVU7Snbj7P4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6"
        "xsOBAAAAGHRSTlNIAf/////gA58hAwP///8B/wT+AP8DEf/8/B0SAAADvElEQVR42u2ca2/rIAyGTZpwbdcOGvL//+kB"
        "kqW57qSrSUDK+2maVMlPMdgYu2AyF5wAJ8AJsE1kb6EBsJoxKQ74bqViSnwKULPuD7u7egvYnwFqFUwH4Jzq3UU5BxAq"
        "LMWfAFQw/gjTXwweA/xaKPIugDcfuE5CHKTbiuQdgNoYAVQnI8ptMGojABFpmd8ug0OQ2wAcaXLmewEzagsAM5brJEWF"
        "keR/AEQa0MkK5jsBpl+/4TphuZ2gfgOQzn1oygDUuRFbB3D206Tt95oQwMR+nb7GBDA6fnKw3xOoJQBCMrHfE8gFAGZ4"
        "LgDU3skMICP73Wn6ciLo8wfQ+YjyfiN3AIxYnZXgJyTDzwLwvACo7dIiyNCBOidSAwApqc5N3VkK7f0RsrPfnUR1D3BH"
        "WoCrvoy1wxIA3gJcL19VMdajjL4EEJI4imJ+UTTPsZriFpHAGhIAcI4gZ/5zQUVENwJ/EIH3IIQk4mvRfLcGVcw7crsC"
        "BiEIX1bsdwRlzG3svAfHgzqA5jncwt02iJsRAVIaWjXBX8rhGXoLe7r4ukYjkHcHcBcYQaAFKEe2Xqv2VCqn4QEtQLhz"
        "CJhByUM7gPE/y5+jaCakAAHmG5DyuEWA621tb+MECL8J3AoAjQWg9aNZJUAKxoB0lVwBKNcAcCIcZdK5ENXxAFyAayIC"
        "uF0cG0Df5jsYEUC6Y1TqqADzI7Qs8ACEA7CRAVaiNg4AnAAnwAkgGc0YQMSPA3EBXBz4xikqHgNAhYyczEUG8Mncd8R0"
        "OjZASKelEfm6kLsQgzEiXxcSRvriEM/WhYQJVQnIFcC/ESAVto4BCEUtX9vlmQLYtjqN4kNLdaE1YQGE+rp3oTtCOtTW"
        "sB7VBj2wrpShaQLCG/fnwbgFaDYJCYD3LzQEYRtXq/WTaGUV6F9oMF4I9gfgg1dKQj4uTawWEdfsr3AWAO+h+1Y0b+hZ"
        "oRxBr1YDhHvZpXxHGGnQCAAnJ91NdNrs4Z0op26DWbtNZg1Pg5YteLXs2nwABk1zMOj5zmYbwKDxEoYzDyI/+0eNrywP"
        "AlhrfM2EANZbjwMBzcv+afu9Snb6ob/Gs98HIPwdJ+H4Zaf2z0dQ/AhHqj34CwMcS0NAqQ7R8KURmsUxLD9FlhwCD+WH"
        "rYNwKjUEb75kb4wi+iHMdMZpnPMYUb85DOo/UIvjGdppUFL/YRyXdNO4/CgKP5ArvN/8NhcNm+ahrRWwt6xtv3VVfzaS"
        "zhSrjxr4l4z9dyZ9248CEFLvLrbtZwHO35U4AU6AzPUPIsb0BYJlwOgAAAAASUVORK5CYII="
    ),
    "nav_home": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEWjq7uepbR/f3+krbyoqOiosMGoscF////MzMx/"
        "v7+nsL8AAP9/f/+nr8GZzMy/f78AAACiqrqstMX///+oqayiqrqjq7u+vr4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB5"
        "Co17AAAAGHRSTlNSKALHB9ynAgUErgECWgUEAP39AQa9hgTpnWq6AAACe0lEQVR42u3ZbXPCIAwAYKC16l4R0vL//+kC"
        "KoWuc+4u7Mhd8nF6M08DCW2VZR5KAAIQgAAEIAABCEAAAhCAAAQgAAEIYC8APGfAWzLwBWDq4zu0FbQEBKsPzh1UU4Fq"
        "nP/l4hwKPEPAySpMP4ZbLBh2AMj5J4E1vADe2yXnj4KXZlu5DcDU+aNgADvyARhr6vyjQLepQQvAaGFY87/vZKftBw/A"
        "aHXO3yntCkHgALi1/+vFx3+fNXEghP4BmH91zet6WN87APPPq/6gY+fBBvpSCKiHMjFgHb9r38ERlnsS/VCmBUCZf+78"
        "BioB7VCmBJTj1016Xe/4wQojHsqEgHL8bi/0fmn6ApTjF/P3ZjMcvm+OvgDl+HU79zCb9vrRGyDU7X5njWgUXOiHsqJq"
        "P3n84hLZX+NjsUXoRhoNIHaZ9fZlfttNLpRHVNwlph+A8Xn8JsD+1Q31PQ7Rvb6i2QCqTO05wNwVYPk7YKDZBYpmC+gd"
        "gIejT3EE2F1CoadNvDiX2/wVUF3f1xqA3x2076cCcQyraVbXnXAFGKun+RZTapp3gFPxu5qoj1INspSNXgFxVRURW04G"
        "LF2eRgFO56IC4OdiwR8uuGBWAAR/fu3vNHo/cUYAFqB+qoLnt6ICocMKbAFQAxRXwK018QUolQ6pfAGjnXkDAJhXANhX"
        "gCUATgG2SwggAJ8KYHzWm1inH2MCGFSMTRuNMfMApNPy+gjl+5/6BzwOAbQD6OcAqlOArx6uPABoyvc0hIDtXdhPQfue"
        "iRIQBe/zLzFp2vdkpIDnnhYebb8Aa4J/HMcz8ft6YsD/hwAEIAABCEAAAhCAAAQgAAEIQAACYBpfCXGA4ZFfqBMAAAAA"
        "SUVORK5CYII="
    ),
    "nav_home_sel": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEU6SnQAAP/g6futuNZfbZQsPWk7THcyMnw6SnSJ"
        "lrgtVXZ1dnxVVapDUny8x+J4han///8/Pz85SXQAAADb5P7k7P5VVVU7SnYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAm"
        "D0szAAAAGHRSTlNIAf/////gA5//IQMD////AQT+AP//AxGLUSVmAAAEYklEQVR42u2c63KrIBCAF0UhoDEV0fd/0wIm"
        "iEZT0ywJzmT7J9OeM7Mfe+WyAXVwgS/AF+ALsE/IuwUNgPWMSfGBtZUVq8SrAD27fhjeLl4D9m+AvnKqA3Betm+XknMA"
        "UTlT/Augcsp/QvWJwWKAtUVFngWw6gNvkxAO0oQieQagV0pA2SYjJR+cUjsBiEhL/dEMBkHuAzCkyalvBZiq9gAwNfA2"
        "SSmFkuQvACIVtMkK3EcCLJdf8TZhMZFQPQKQxn3KlAFK40ZsG8DoXyatv5UFASz0b9OXOQHM0s8R9LcE1RoAIQfR3xLI"
        "FQCm+FEAyuFC7gAOpL/JppMTge8f4tQvSusozZ0P5CsAI0MU9c9aZ7SIUpP7GUAfpQDTTned1k0RIwyubRHEc6CiMepb"
        "yfIoTlQFAFKW8fSPQ3DLpTDuHyMY4OT1N26Ux8hEvQe4RDBAnnWBZOdYJoBIBpjrb2xwruOYAFwTh22A+jz5//WTSafY"
        "BIMiDiBCCjrf1l83LpW6jx12QQCbiMB6EHITQWerTs8TAfYeebSAwi3Cxd2aB/YokMPYeA+6B03637y+znUUAtcRAXIb"
        "OpWvMO/4nIRb0kp5MQAXEUX/eeaPRGDyEDDUEPDld6mo/wNqUQb1A6ghsL3QW6Z5OQiMBfAOQr3+K8tcND648YqyKcaA"
        "F8NT+V1NNsV9enrdBEwaF8IygE/33UaypN5AaEV5UGgAvvwa/9nQrjihE0iTRiVu+XqQZyYAtGQkDMCArP8+gA4nDgAJ"
        "YKbaPgAcE3wBvAvpZwGyU5ESgKlhWutsH0CmzQ9OEBsAyZDSaH5uRjvcAGjupQkBspP5ywlrR4BYyEytDQFoZ40yStbM"
        "AIzzIBUyUwd+EA8VZwB5GBZZHQIgbiolajMXAtSzcxVNFxbAa+Z+ENvpdwO4dloqERNA65gWMBtiUEpEdKGMjqcqkQCE"
        "kvZwiEcEaIs8HkAplDuVgJgWaCMC2DsC1IOtFYA6JoA71LJnu/ygAMN4Oo3nQ3+4UIsM4M7XrQtdkNohD+BahUUQF/gW"
        "cI8mwN1xIxXjK8CZWpmn0ZP91dhdYAFwf0NDsML4titwDVwXFrLxV7rDBAB/Q4N2QxBua7YFCYAHt5SEyPZwABDcUmLd"
        "EbwT4HrLentqgLMvq7M9BJripCA5A8DpSWk27cI2pcHogpaPPawTYdiAnvK/hOJEMIv04Kl4+R/sMsD0ZAumJ7vDcV5s"
        "BY/mIHjzLY6iPwQPLyGceRDH03/28JUdgwC2Hr4ehAC2nx47gvJY+i+f31fJTj/4bTx7PABh9zjp6s+Hpf73Iyh2hCPV"
        "N/grAxxrQ0CpDtHwtRGa1TEsO0WWHAJ3xw97B+Gq1BCs+pI9MYpohzDTGacxzqNE/+QwqP0Pvfg8wzgNSvp/jOOS6zQu"
        "/xSFHcgV1m8ezUXDrnnoYRDwbhmGcdWr/rWRdFax/lMD/5KxP2fS930pACH924Xt+1qA7/dKfAG+AAeXXyW89w9hvNt3"
        "AAAAAElFTkSuQmCC"
    ),
    "nav_settings": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEWrrreiq7qjq7yqq9+nsMB/f3+pscGnr8B/f79/"
        "f//MzMwAAP9///+iqrt/v78A//8AAACiq7ustcX///+pqa6iqrqkrLyjq7sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC"
        "sH0bAAAAGHRSTlMWrcsOpwJZ2gQCBQECTQQBAP3+AQeRTG9j7yevAAADRElEQVR42u2a25LiIBCGO2d1ZmehaZL3f9MF"
        "EhNIMqUXHReqmhs1WNof0Ad+AFV4AwEQAAEQAAEQAAEQAAEQAAEQAAE4b8YUDUDOfNOUC+DMpya8lAnwrf60WtfTpQRw"
        "5fhPiFoj2o5KBCA1OvN9wyvn4EIAUy0AWsN1wYgX4OtG94aWBQRP8zWCmh9Sc6fbV74AJnoxijaAcQYwydfyA3CDPrZt"
        "Oy4ETb0uIfoyoT90AysBcNpPNfpm/XgTbE5sHY3rVTZ018RJwAjQwTLkzmCffZv+OQG2cX/TOPvn7hq6HAG2EXdRR0E/"
        "6PUj4mDdI9xcAjIEILWGTZzGAVfzZwSc7NpdKcoQwKjNaXVq/vxoe1czOgEnQHW0+rS5GTBZLqHxXYAxyyXkRtW+ReCj"
        "aq6JzOJLBOS1n7uUgP6V/S3kW0qc+EFIvbv188i4mFuT8TOWVtbaOkHAbGfAlfx+DxaZWo8h2EATx9eQxUx+YTToD48u"
        "zmVVKOnAQ8Rcmlydx6dVAJf7Bv0BkpFWP39nOIoJ3AaTU6vgAZj1h2Hc7HQl57ZOzCPK0lgBp1YBPOO/6A9JtoXYu2lX"
        "FrFpFXBF8AwrPXFUOhZKTFoFC4A5WrermCH1Y0atAjgWEBwT7pT+8tkkrVrF/wegNwAArylKOZaQSfLvacUJp26iTCYA"
        "xwXuo+iL3Q7TxpInjJp5J4C/LXBzb5IKLyDSdy5RKMzB4AUhG6UrFc3BI5ojrCZfoXKV1WylBECfeKp3425NxGkHQQ8q"
        "p0zsVazwY7E3h5rnh+h2U2kICuVdqP6yKqcNkUnTlSt6FrhpiJ92xn2XTZvj3dCYXclTTeM4tRqvkiTYt5TNLqDicUtZ"
        "Zb2lLHxTX7qsUrqwVby0WLy4m8rrZ4s/c3k9PuCwB29GrHI/4IhKfleKQhsPuUtpvaLMj5iSQz6vaXmlZW7V5DWj3A/5"
        "0mPWO20lnC/rDJnsj1l3B92bJIqaqISD7v1Vg+NdidyvGuwuexzvSmR/2eNXrYJUIddtzrUK1rj/OYBNq2DSHz4OsGgV"
        "mnsH8DmAWasAVeqtxadWQcXeG521iu7af7gY4PomAAIgAAIgAAIgAAIgAAIgAAJQbPsH9Xp/qYl2IOoAAAAASUVORK5C"
        "YII="
    ),
    "nav_settings_sel": (
        "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"
    ),
    "nav_storage": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEWqsNmbp7B/f7p/f/+oscGnr8CnsMAAAP+ZzMy3"
        "wNIAAACiq7unp6+rs8T///+jq7uiqrqiqrqjq7t/f3+iqbmjq7t///+9vb0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA5"
        "zcldAAAAGHRSTlMXDgQCR+WZAQX/APoK/gFCvMlqAjGbAgSPuLG1AAACE0lEQVR42u3Z227DIAwGYEjSbiOGHMjh/d90"
        "UMaqJmlHNV840u+LadLQ5K+cjKvMyUMBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD8L1xByAWQKh33IRFw+2iV"
        "pj9CKwrjanmAkH5zHdqCGK5NZUgawBndW2tLAGHYZWITKK78p7Lss2HkEvAAvNPtG/lHQsckUEwT0L+XfwjiOVBZAGS6"
        "lH/ZHkg/R7OKAeQJuIyk/jpGle4SYCA5M+DWwaYPtSimNAdkvBBAOELT4gi3mF+2lYOv77/qqtL+0yQuzzbmAdDPvozL"
        "2j+kv4R1slkryvVyAUbr7V813ceNfR8vAMGA0bZD/xhtO6cT0/k5XMJ2XuQC3Ph1qxQe4tLa3seFFHVpq8vdA08uAhtL"
        "n3zW2kEuYHpVN5wBoObDGYh3ljvHElK9PYhQPJ9kE4eEVNNtQ2njznKMPikw/ab0kwtY6qP67a6qiKowXNwecJRTWuvl"
        "ZUuF3LL+nlnGCQHkRRGOoaLRQ65GxQDW/KDZ1REHMaTbzs5GznsgPwj2dcRRtJwriO1NPL35JmZrSzC1VfI1W5z/LKut"
        "cnvXt8WdodgW4mrysrUWydD1UrgHrhPLc5i5uUux7bwvJHbRhNKCrzXK2V53xf+LvJEIiEUPFUTN+hUHvmICAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAOGF8A9to3SUkVwhXAAAAAElFTkSuQmCC"
    ),
    "nav_storage_sel": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAABgCAMAAAB8Ho3bAAAAYFBMVEU6SnQAAP/U3fZEU3xhb5Wvutg7THcyMnw6SnQt"
        "VXZ1dnxVVap4hakrPGiFkrS5w+D///8/Pz85SXQAAADb5P7j7P5VVVU7SnYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABg"
        "0inYAAAAGHRSTlNIAf/////gA58hAwP/////AQT+AP//AxHLsTZ6AAAD9klEQVR42u2c2XKrMAxAZQe8Zqsd4P//9HqB"
        "LISmptUl1kz01MnQGR20WLYlwBMX+AB8AD4AZcK2FjQA2Utp9BverbHS6r8C9HL8Y9hcrhrIXwP0NqkOoJRwm4tQCkDb"
        "ZIpfAdik/DtUvzFEDIi2sGwtQFQflKtCFJgQimwNQO+9BuGqEaGGpFQhANN1qZ/NEBBMGUAgrU79KCC9LQGQflCuShHa"
        "G/YTADMeXLUCz5EA89fvlatYQiTYVwAmuI+oGUAEN5LfAwT9RdX6R5kRwEx/V788EsBD+qGgfySwSwCMEdE/EpgFAOkV"
        "FQAxnNkTACH9Qza9ORFc6wdwdESoayCPAJINjpTAtCTDZABFC0AMY1kEBB1odCJ7B2CMcNRkzKWQ949ATv+QiforwJmg"
        "ASYTAFUDTCaAVMRRNIBzg2cJoKdpgLgW2ARgKRURj3vkbAGPtggf3K5AMMM4eA+iBzXtiZdIu0esiACtDN21XdddCiQ8"
        "xhscAnMOAGeNon/Di7SfGPZYeQgkTgjs1ugfERqcPPQFSCHQrtP/0vEdThAEC2AchDbdqFaB5Ef58YCyGANKDB9anvQ/"
        "lqTRHC0dx4hiaYILYcTwKelU9lIPTfaiBiWKcQB2yQDFbp1xMQBMSKPGbQ7QogHoADCgAjxXCve/7Nu0CuMBADbAMRQK"
        "Mwm/TAgtD4twWzFA0D9VCo9yiTqn95+f2h+qBWj4y2U3L3bd6QPwAGAkXhp17SIB5//NhTTyOpBK6ie5Fs/4QRzWgS+M"
        "Q8VbGm0W5P+lUaENTjH3roUsFnNfGOX0mwBSOW28RvDFXGA2ZRXymKwajCQkwXuMLWXOkGVb3eaEVk5rb+LhEEIU73OK"
        "v5zanyXvaLoWI4Z9OpUALB9aqCMW5IK2HYh3BFgHW83KPTE/oqzD8WDLG5Trpf0qAo7gQHE/lo4WcXwo5JbSk5WOXzDe"
        "fz5fjy50ljhHc6GYLooB3uIczKWmCUh33DgtZofFQuJVafFHA0w3NMwTuyS+hfD1ko/gDYG6u6VkzFA1ANmL7umWdWo1"
        "MJKaD009QxMASk26nYh5s0d0IkHKgSTphqe7li24tewOglwAPPTM9XTCAO4aL+F+5kHT0/+h8VXSIIDvGl+JEMD3rceJ"
        "QNDSf95+b6udfrhu4+XrAYi4x6l4/Rrm+j+PoMQRjlp78BcGOJaGgGodolFLIzSLY1hxiqw6BJWOH0oH4WxtCFF9I1eM"
        "IsYhzHrGaYLzeN2vHAaN/9Dr9zPkaVDW/2Icl43TuOpdFHEgV0e/eTUXDUXz0MOgYWsZhvzWbf+3kXRpZf+ugX8j5Y8z"
        "6WUfBWCs31xk2WcBPt+V+AB8AIjLPzT08kIBVlc0AAAAAElFTkSuQmCC"
    ),
    "tile_apk": (
        "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"
    ),
    "tile_erase": (
        "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"
        "RK5CYII="
    ),
    "tile_fix": (
        "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"
        "U/4D6O8ob6YB+F4AAAAASUVORK5CYII="
    ),
    "tile_flash": (
        "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"
        "CtyWQLQtFQAAAABJRU5ErkJggg=="
    ),
    "tile_gsi": (
        "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"
    ),
    "tile_reboot": (
        "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"
        "SpZXb3UmMX8AAAAASUVORK5CYII="
    ),
    "tile_recovery": (
        "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"
    ),
    "tile_refresh": (
        "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"
        "AABJRU5ErkJggg=="
    ),
    "tile_sideload": (
        "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"
        "fKJkJYcENgAAAABJRU5ErkJggg=="
    ),
}


# ------------------------------------------------------------------ start up
def bootstrap():
    try:
        import termuxgui  # noqa: F401
    except ImportError:
        print("Installing the termuxgui Python package...")
        subprocess.call([sys.executable, "-m", "pip", "install", "termuxgui"])
    if not (shutil.which("adb") and shutil.which("fastboot")):
        print("Installing android-tools (adb + fastboot)...")
        subprocess.call(["pkg", "install", "-y", "android-tools"])


def main():
    global tg
    bootstrap()
    try:
        import termuxgui as _tg
    except ImportError:
        print("Could not import termuxgui - run: pip install termuxgui")
        return 1
    tg = _tg
    try:
        conn = tg.Connection()
    except Exception as e:
        print("Could not connect to Termux:GUI (%s).\nInstall the Termux:GUI app from the same source as "
              "Termux itself (F-Droid or GitHub) and open it once." % e)
        return 1
    with conn as c:
        App(c).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
