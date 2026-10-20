#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install Termux:GUI and Termux:API so that they carry the SAME signature as the Termux you have installed.

Android only lets Termux and its plugins work together when they are signed by the same key (F-Droid, GitHub and
Google Play builds all use different keys). So this script:
  1. reads the signing certificate of the installed Termux,
  2. looks for the plugin APKs on GitHub and F-Droid (the source your Termux came from is tried first),
  3. downloads a candidate and only accepts it if its certificate equals Termux's,
  4. installs it (root: silently with `pm install`; no root: it opens Android's own installer - you just tap Install).
Plugins that are already installed and working are left alone, and a download that Android rejects (wrong key)
makes it try the next source automatically.

    python termux_plugins.py            check, download what is missing/mismatched, install
    python termux_plugins.py --check    only report (exit 0 = all fine, 3 = something missing or mismatched)
Pure Python standard library - nothing to pip install."""
import argparse
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

TERMUX = "com.termux"
PLUGINS = [("com.termux.gui", "Termux:GUI", "termux/termux-gui"),
           ("com.termux.api", "Termux:API", "termux/termux-api")]
GITHUB_API = os.environ.get("FK_GITHUB_API", "https://api.github.com").rstrip("/")
FDROID_API = os.environ.get("FK_FDROID_API", "https://f-droid.org/api/v1/packages").rstrip("/")
FDROID_REPO = os.environ.get("FK_FDROID_REPO", "https://f-droid.org/repo").rstrip("/")
HOME = os.path.expanduser("~")
WORK = os.path.join(HOME, ".flashkit", "plugins")
LOG = os.path.join(HOME, ".flashkit", "plugin-install.log")
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
UA = "flashkit-installer"


def say(msg=""):
    print(msg, flush=True)


class NoMatch(Exception):
    pass


# ---------------------------------------------------------------- reading an APK's signing certificate
def _der(buf, pos):
    """-> (tag, content_start, content_end) of the DER element at pos."""
    tag, n = buf[pos], buf[pos + 1]
    p = pos + 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0:
            raise ValueError("indefinite length")
        n = int.from_bytes(buf[p:p + k], "big")
        p += k
    return tag, p, p + n


def certs_from_pkcs7(blob):
    """The DER certificates inside a PKCS#7 SignedData blob (META-INF/*.RSA / *.DSA / *.EC)."""
    _, s, _ = _der(blob, 0)                 # ContentInfo
    _, _, p = _der(blob, s)                 # contentType OID
    _, s3, _ = _der(blob, p)                # [0] EXPLICIT
    _, p, e4 = _der(blob, s3)               # SignedData
    for _ in range(3):                      # version, digestAlgorithms, contentInfo
        _, _, p = _der(blob, p)
    out = []
    if p < e4 and blob[p] == 0xA0:          # [0] IMPLICIT certificates
        _, q, ce = _der(blob, p)
        while q < ce:
            _, _, qe = _der(blob, q)
            out.append(bytes(blob[q:qe]))
            q = qe
    return out


def _lp(buf, pos):
    n = struct.unpack("<I", buf[pos:pos + 4])[0]
    return buf[pos + 4:pos + 4 + n], pos + 4 + n


def sigblock_certs(path):
    """Certificates from the APK Signing Block (v2/v3 signatures) - the only signature newer apps have."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 70000))
        tail = f.read()
        i = tail.rfind(b"PK\x05\x06")
        if i < 0:
            return []
        cd = struct.unpack("<I", tail[i + 16:i + 20])[0]
        if cd < 32:
            return []
        f.seek(cd - 24)
        hdr = f.read(24)
        if hdr[8:] != b"APK Sig Block 42":
            return []
        blk_size = struct.unpack("<Q", hdr[:8])[0]
        if blk_size > cd:
            return []
        f.seek(cd - blk_size - 8)
        blk = f.read(blk_size + 8)
    out, pos, end = [], 8, len(blk) - 24
    while pos + 12 <= end:
        ln = struct.unpack("<Q", blk[pos:pos + 8])[0]
        bid = struct.unpack("<I", blk[pos + 8:pos + 12])[0]
        if bid in (0x7109871a, 0xf05368c0, 0x1b93ad61):
            val = blk[pos + 12:pos + 8 + ln]
            signers, _ = _lp(val, 0)
            p = 0
            while p < len(signers):
                signer, p = _lp(signers, p)
                signed, _ = _lp(signer, 0)
                _, q = _lp(signed, 0)           # digests
                cseq, _ = _lp(signed, q)        # certificates
                r = 0
                while r < len(cseq):
                    c, r = _lp(cseq, r)
                    out.append(bytes(c))
        pos += 8 + ln
    return out


def apk_certs(path):
    """SHA-256 fingerprints (hex) of the APK's signing certificates, signer first. [] if it can't be read."""
    certs = []
    try:
        certs += sigblock_certs(path)
    except Exception:
        pass
    if not certs:
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if re.match(r"^META-INF/[^/]+\.(RSA|DSA|EC)$", n, re.I):
                        try:
                            certs += certs_from_pkcs7(z.read(n))
                        except Exception:
                            pass
        except Exception:
            pass
    return [hashlib.sha256(c).hexdigest() for c in certs]


def same_signer(a, b):
    return bool(a) and bool(b) and a[0] in b


# ---------------------------------------------------------------- talking to Android
SYS_PATH = "/system/bin:/system/xbin:/vendor/bin"
# Termux sets LD_LIBRARY_PATH/LD_PRELOAD, which makes Android's own tools (pm, cmd) crash with "CANNOT LINK EXECUTABLE"
ROOT_PREFIX = "unset LD_LIBRARY_PATH LD_PRELOAD; export PATH=%s:$PATH; " % SYS_PATH


def sys_env():
    e = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    e["PATH"] = SYS_PATH + ":" + e.get("PATH", "")
    return e


def run(cmd, timeout=30, root=False, sysenv=False):
    env = None
    try:
        if root:
            text = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
            cmd = ["su", "-c", ROOT_PREFIX + text]
        elif sysenv:
            env = sys_env()
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


_ROOT = None


def have_root():
    global _ROOT
    if _ROOT is None:
        if os.environ.get("FK_NO_ROOT"):
            _ROOT = False
        else:
            rc, out = run("id -u", timeout=60, root=True)
            _ROOT = rc == 0 and out.strip().splitlines()[-1:] == ["0"]
    return _ROOT


def parse_pm_list(out):
    res = {}
    for line in out.splitlines():
        m = re.match(r"package:(\S+)(?:\s+installer=(\S+))?", line.strip())
        if m:
            res[m.group(1)] = "" if m.group(2) in (None, "null") else m.group(2)
    return res


def installed_packages():
    """{package: installer}. Root sees every app; without root Android hides apps that Termux doesn't 'query'."""
    res = {}
    if have_root():
        res.update(parse_pm_list(run("pm list packages -i", 60, root=True)[1]))
    for k, v in parse_pm_list(run(["pm", "list", "packages", "-i"], 30, sysenv=True)[1]).items():
        if not res.get(k):
            res[k] = v
    return res


def probe(pkg):
    """Does the plugin actually answer? Works even when Android hides the app from `pm`."""
    if pkg == "com.termux.gui":
        cmd = os.environ.get("FK_PROBE_GUI")
        if cmd:
            return run(["sh", "-c", cmd], 15)[0] == 0
        return run([sys.executable, "-c", "import termuxgui as t\nwith t.Connection() as c: pass"], 15)[0] == 0
    cmd = os.environ.get("FK_PROBE_API")
    if cmd:
        return run(["sh", "-c", cmd], 15)[0] == 0
    rc, out = run(["termux-battery-status"], 12)
    return rc == 0 and "percentage" in out


def present(pkg):
    return pkg in installed_packages() or probe(pkg)


def apk_paths(pkg):
    rc, out = run("pm path %s" % shlex.quote(pkg), 30, root=True) if have_root() else (1, "")
    if "package:" not in out:
        rc, out = run(["pm", "path", pkg], 30, sysenv=True)
    return [l.split("package:", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")]


def installed_certs(pkg, workdir):
    for path in apk_paths(pkg):
        local = path
        if not os.access(path, os.R_OK):
            if not have_root():
                continue
            local = os.path.join(workdir, "installed_%s.apk" % pkg)
            with open(local, "wb") as f:
                if subprocess.run(["su", "-c", "cat " + shlex.quote(path)], stdout=f).returncode != 0:
                    continue
        certs = apk_certs(local)
        if certs:
            return certs
    return []


def device_abi():
    rc, out = run(["getprop", "ro.product.cpu.abi"], 10, sysenv=True)
    out = out.strip()
    return out if out in ABIS else "arm64-v8a"


# ---------------------------------------------------------------- finding and downloading candidates
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(url, dest):
    tmp = dest + ".part"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                total, done, shown = int(r.headers.get("Content-Length") or 0), 0, -1
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    pct = done * 100 // total if total else -1
                    if pct >= 0 and pct // 25 != shown // 25:
                        shown = pct
                        say("    %d%%" % pct)
            os.replace(tmp, dest)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            say("    download problem (%s) - retrying" % e)
            time.sleep(2)
    raise RuntimeError("could not download %s: %s" % (url, last))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def abi_score(name, abi):
    n = name.lower()
    if abi in n:
        return 2
    return 0 if any(a in n for a in ABIS) else 1


def github_candidates(repo, abi):
    rels = []
    try:
        rels.append(json.loads(http_get("%s/repos/%s/releases/latest" % (GITHUB_API, repo))))
    except Exception:
        rels = json.loads(http_get("%s/repos/%s/releases?per_page=5" % (GITHUB_API, repo)))
    out = []
    for rel in rels:
        for a in rel.get("assets", []):
            if a["name"].lower().endswith(".apk"):
                out.append({"src": "github", "name": a["name"], "url": a["browser_download_url"], "tag": rel.get("tag_name", ""),
                            "sha256": (a.get("digest") or "").replace("sha256:", "") or None, "score": abi_score(a["name"], abi)})
        if out:
            break
    return sorted(out, key=lambda c: -c["score"])


def fdroid_candidates(pkg, abi):
    d = json.loads(http_get("%s/%s" % (FDROID_API, pkg)))
    code = d.get("suggestedVersionCode") or max(p["versionCode"] for p in d["packages"])
    return [{"src": "fdroid", "name": "%s_%s.apk" % (pkg, code), "url": "%s/%s_%s.apk" % (FDROID_REPO, pkg, code),
             "tag": str(code), "sha256": None, "score": 1}]


def iter_matching(pkg, label, termux_certs, order, workdir, abi, tried):
    """Yield (apk path, candidate) for downloads signed like Termux (or any, if Termux's key can't be read)."""
    repo = dict((p, r) for p, _, r in PLUGINS)[pkg]
    for src in order:
        say("  %s: looking on %s ..." % (label, "GitHub" if src == "github" else "F-Droid"))
        try:
            cands = github_candidates(repo, abi) if src == "github" else fdroid_candidates(pkg, abi)
        except Exception as e:
            say("    not available there (%s)" % e)
            continue
        for c in cands[:2]:
            dest = os.path.join(workdir, "%s_%s_%s" % (pkg, src, re.sub(r"[^\w.+-]", "_", c["name"])))
            say("    downloading %s %s" % (c["name"], c["tag"]))
            try:
                download(c["url"], dest)
            except RuntimeError as e:
                say("    " + str(e))
                continue
            if c["sha256"] and sha256_file(dest) != c["sha256"]:
                say("    checksum does not match - skipped")
                os.remove(dest)
                continue
            try:
                with zipfile.ZipFile(dest) as z:
                    if z.testzip():
                        raise zipfile.BadZipFile("corrupt")
            except zipfile.BadZipFile:
                say("    not a valid APK - skipped")
                os.remove(dest)
                continue
            certs = apk_certs(dest)
            tried.append((src, c["name"], certs[:1]))
            if not termux_certs:
                say("    (Termux's signature can't be read - Android itself will accept or refuse this one)")
                yield dest, c
            elif same_signer(certs, termux_certs):
                say("    signature matches Termux - good")
                yield dest, c
            else:
                say("    signed by a different key (%s...) - not usable with your Termux" % (certs[0][:12] if certs else "unsigned?"))
            try:
                os.remove(dest)
            except OSError:
                pass


# ---------------------------------------------------------------- status and installing
def status(workdir):
    inst = installed_packages()
    tcerts = installed_certs(TERMUX, workdir)
    rows = []
    for pkg, label, _ in PLUGINS:
        row = {"pkg": pkg, "label": label}
        if pkg in inst:
            certs = installed_certs(pkg, workdir)
            row["state"] = "installed" if not (tcerts and certs) else ("ok" if same_signer(certs, tcerts) else "wrong-signature")
        elif probe(pkg):
            row["state"] = "ok"  # it answers, so Android accepted it next to Termux
        else:
            row["state"] = "missing"
        rows.append(row)
    return inst, tcerts, rows


def install_root_one(apk, pkg, uninstall_first):
    """Install one APK with root in a detached script (Android may restart Termux when a plugin is installed)."""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    q = shlex.quote
    tmp = "/data/local/tmp/fk_plugin.apk"
    open(LOG, "w").close()  # empty it HERE (not in the script), or we'd read the previous run's "DONE"
    lines = []
    if uninstall_first:
        lines.append("pm uninstall %s >> %s 2>&1" % (q(pkg), q(LOG)))
    lines += ["cp %s %s && chmod 644 %s" % (q(apk), tmp, tmp), "pm install -r -d %s >> %s 2>&1" % (tmp, q(LOG)),
              "echo \"install %s rc=$?\" >> %s" % (pkg, q(LOG)), "rm -f %s" % tmp, "echo DONE >> %s" % q(LOG),
              "am start -n com.termux/com.termux.app.TermuxActivity >/dev/null 2>&1"]
    subprocess.Popen(["su", "-c", ROOT_PREFIX + "\n".join(lines)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    end, txt = time.time() + 150, ""
    while time.time() < end:
        try:
            txt = open(LOG).read()
        except OSError:
            txt = ""
        if "DONE" in txt:
            break
        time.sleep(1)
    if "install %s rc=0" % pkg in txt:
        return True, ""
    fail = [l for l in txt.splitlines() if "Failure" in l or "Error" in l]
    return False, (fail[-1] if fail else "no answer from the installer (see %s)" % LOG)


def install_manual_one(apk, pkg):
    """No root: open Android's own installer and wait until the plugin shows up."""
    say("  Opening Android's installer for %s - tap Install." % pkg)
    say("  If it says installing from Termux isn't allowed: tap Settings, turn on 'Allow from this source', press Back, tap Install.")
    run(["termux-open", apk], 20)
    start, settings, again = time.time(), False, False
    while time.time() - start < 300:
        if present(pkg):
            return True, ""
        t = time.time() - start
        if t > 30 and not settings:
            settings = True
            say("  (opening the 'install unknown apps' page for Termux, in case it needs allowing)")
            run(["am", "start", "-a", "android.settings.MANAGE_UNKNOWN_APP_SOURCES", "-d", "package:com.termux"], 15)
        if t > 120 and not again:
            again = True
            run(["termux-open", apk], 20)
        time.sleep(3)
    return False, "timed out waiting for the installation"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install Termux:GUI and Termux:API matching your Termux's signature.")
    ap.add_argument("--check", action="store_true", help="only report")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask before replacing a wrong-signature plugin")
    ap.add_argument("--no-root", action="store_true", help="never use su; open the normal Android installer instead")
    ap.add_argument("--source", choices=["github", "fdroid"], help="only look in one place")
    ap.add_argument("--force", action="store_true", help="reinstall even if already fine")
    args = ap.parse_args(argv)
    if args.no_root:
        os.environ["FK_NO_ROOT"] = "1"
    os.makedirs(WORK, exist_ok=True)
    inst, tcerts, rows = status(WORK)
    installer = inst.get(TERMUX, "")
    where = {"": "GitHub/other (no installer recorded)", "com.android.vending": "Google Play"}.get(installer, "F-Droid" if "fdroid" in installer else installer)
    say("Termux: from %s | signature %s" % (where, (tcerts[0][:16] + "...") if tcerts else "could not be read"))
    for r in rows:
        say("  %-11s %s" % (r["label"], {"ok": "installed, signature matches", "installed": "installed", "missing": "NOT installed",
                                          "wrong-signature": "installed but signed with a DIFFERENT key (won't work)"}[r["state"]]))
    if args.check:
        return 0 if all(r["state"] in ("ok", "installed") for r in rows) else 3
    todo = [r for r in rows if r["state"] in ("missing", "wrong-signature") or args.force]
    if not todo:
        say("Nothing to do.")
        return 0
    order = [args.source] if args.source else (["fdroid", "github"] if "fdroid" in installer else ["github", "fdroid"])
    if "vending" in installer:
        say("Note: your Termux comes from Google Play; its plugins must come from Play too, so this usually can't work.")
    if not have_root():
        say("No root: Android's own installer will open for each app - just tap Install.")
    abi, failed = device_abi(), []
    for r in todo:
        say("\n%s" % r["label"])
        wrong = r["state"] == "wrong-signature"
        if wrong and not have_root():
            say("  It is installed with a different key. Uninstall it (Settings > Apps > %s), then run:  flashkit plugins" % r["label"])
            failed.append(r["label"])
            continue
        if wrong and not args.yes and sys.stdin.isatty():
            if input("  Replace the installed %s (its settings are lost)? [Y/n] " % r["label"]).strip().lower() == "n":
                failed.append(r["label"])
                continue
        tried, done, why = [], False, ""
        for apk, c in iter_matching(r["pkg"], r["label"], tcerts, order, WORK, abi, tried):
            ok, why = install_root_one(apk, r["pkg"], wrong) if have_root() else install_manual_one(apk, r["pkg"])
            if ok:
                say("  %s installed" % r["label"])
                done = True
                break
            say("  Android refused it: %s" % why[:160])
        if not done:
            failed.append(r["label"])
            if not tried:
                say("  !! Couldn't download anything - no internet, or GitHub / F-Droid can't be reached. Check your connection, then run:  flashkit plugins")
            elif why:
                say("  !! None of the downloads could be installed. Install Termux and its plugins from the SAME place (F-Droid or GitHub), then run:  flashkit plugins")
            else:
                say("  !! No download is signed like your Termux (tried: %s)." % ", ".join("%s %s" % (x, f[0][:8] if f else "?") for x, _, f in tried))
                say("     Install Termux and its plugins from the SAME place (F-Droid or GitHub), then run:  flashkit plugins")
    return 0 if not failed else (4 if len(failed) < len(todo) else 2)


if __name__ == "__main__":
    sys.exit(main())
