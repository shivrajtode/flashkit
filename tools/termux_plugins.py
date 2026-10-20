#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install Termux:GUI and Termux:API so that they carry the SAME signature as the Termux you have installed.

Android only lets Termux and its plugins work together when they are signed by the same key (F-Droid, GitHub and
Google Play builds all use different keys). So this script:
  1. reads the signing certificate of the installed Termux,
  2. looks for the plugin APKs on GitHub and F-Droid (the source your Termux came from is tried first),
  3. downloads a candidate and only accepts it if its certificate equals Termux's,
  4. installs it (root: silently with `pm install`; otherwise it opens the normal Android installer).

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
def run(cmd, timeout=30, root=False):
    try:
        if root:
            cmd = ["su", "-c", cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
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


def installed_packages():
    """{package: installer} from `pm list packages -i` (installer '' when unknown)."""
    rc, out = run(["pm", "list", "packages", "-i"], 30)
    if "package:" not in out and have_root():
        rc, out = run("pm list packages -i", 60, root=True)
    res = {}
    for line in out.splitlines():
        m = re.match(r"package:(\S+)(?:\s+installer=(\S+))?", line.strip())
        if m:
            res[m.group(1)] = "" if m.group(2) in (None, "null") else m.group(2)
    return res


def apk_paths(pkg):
    rc, out = run(["pm", "path", pkg], 30)
    if "package:" not in out and have_root():
        rc, out = run("pm path %s" % shlex.quote(pkg), 30, root=True)
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
    rc, out = run(["getprop", "ro.product.cpu.abi"], 10)
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


def fetch_matching(pkg, label, termux_certs, order, workdir, abi):
    """Download candidates (source order given) until one is signed like Termux. -> (apk path, candidate)."""
    seen = []
    for src in order:
        say("  %s: looking on %s ..." % (label, "GitHub" if src == "github" else "F-Droid"))
        try:
            cands = github_candidates(dict((p, r) for p, _, r in PLUGINS)[pkg], abi) if src == "github" else fdroid_candidates(pkg, abi)
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
            seen.append((src, c["name"], certs[:1]))
            if not termux_certs:
                say("    (can't read Termux's signature, so it can't be compared - using it anyway)")
                return dest, c
            if same_signer(certs, termux_certs):
                say("    signature matches Termux - good")
                return dest, c
            say("    signed by a different key (%s...) - not usable with your Termux" % (certs[0][:12] if certs else "unsigned?"))
            os.remove(dest)
    raise NoMatch(seen)


# ---------------------------------------------------------------- status and installing
def status(workdir):
    inst = installed_packages()
    tcerts = installed_certs(TERMUX, workdir) if TERMUX in inst else []
    rows = []
    for pkg, label, _ in PLUGINS:
        row = {"pkg": pkg, "label": label, "state": "missing"}
        if pkg in inst:
            certs = installed_certs(pkg, workdir)
            row["state"] = "installed" if not (tcerts and certs) else ("ok" if same_signer(certs, tcerts) else "wrong-signature")
        rows.append(row)
    return inst, tcerts, rows


def install_root(items, uninstall):
    """items = [(apk, package)]. One detached root script, so Termux is restarted at most once."""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    q = shlex.quote
    lines = [": > %s" % q(LOG)]
    for pkg in uninstall:
        lines.append("pm uninstall %s >> %s 2>&1; echo \"uninstall %s rc=$?\" >> %s" % (q(pkg), q(LOG), pkg, q(LOG)))
    for i, (apk, pkg) in enumerate(items):
        tmp = "/data/local/tmp/fk_plugin_%d.apk" % i
        lines += ["cp %s %s && chmod 644 %s" % (q(apk), tmp, tmp),
                  "pm install -r -d %s >> %s 2>&1" % (tmp, q(LOG)),
                  "echo \"install %s rc=$?\" >> %s" % (pkg, q(LOG)),
                  "rm -f %s" % tmp]
    lines += ["echo DONE >> %s" % q(LOG), "am start -n com.termux/com.termux.app.TermuxActivity >/dev/null 2>&1"]
    say("Installing with root (Android may restart Termux once - if it does, open it again and type: flashkit)")
    subprocess.Popen(["su", "-c", "\n".join(lines)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 150
    while time.time() < end:
        try:
            txt = open(LOG).read()
        except OSError:
            txt = ""
        if "DONE" in txt:
            for l in txt.splitlines():
                if l.startswith(("install", "uninstall")) or "Failure" in l or "Success" in l:
                    say("  " + l)
            return True
        time.sleep(1)
    return False


def install_manual(items, uninstall):
    if uninstall:
        say("These have the wrong signature - remove them first (Settings > Apps): " + ", ".join(uninstall))
    for apk, pkg in items:
        say("Opening the Android installer for %s - tap Install, then come back to Termux." % pkg)
        run(["termux-open", apk], 20)
        end = time.time() + 240
        while time.time() < end and pkg not in installed_packages():
            time.sleep(3)
    return True


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
    say("Termux: %s | signature %s" % ("installed by " + installer if installer else "installer unknown", (tcerts[0][:16] + "...") if tcerts else "could not be read"))
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
    abi, items, uninstall, failed = device_abi(), [], [], []
    for r in todo:
        say("\n%s" % r["label"])
        try:
            path, _ = fetch_matching(r["pkg"], r["label"], tcerts, order, WORK, abi)
        except NoMatch as e:
            failed.append(r["label"])
            if not e.args[0]:
                say("  !! Couldn't download anything - no internet, or GitHub / F-Droid can't be reached. Check your connection, then run:  flashkit plugins")
            else:
                say("  !! No download is signed like your Termux. Tried: %s" % ", ".join("%s %s" % (s, f[0][:8] if f else "?") for s, _, f in e.args[0]))
                say("     Install Termux and its plugins from the SAME place (F-Droid or GitHub), then run this again.")
            continue
        items.append((path, r["pkg"]))
        if r["state"] == "wrong-signature":
            uninstall.append(r["pkg"])
    if not items:
        return 2
    if uninstall and not args.yes and sys.stdin.isatty():
        if input("Replace %s (settings/data of the plugin are lost)? [Y/n] " % ", ".join(uninstall)).strip().lower() == "n":
            return 2
    (install_root if have_root() else install_manual)(items, uninstall)
    after = installed_packages()
    bad = [p for _, p in items if p not in after]
    for _, p in items:
        say("  %s: %s" % (p, "installed" if p not in bad else "NOT installed - see %s" % LOG))
    return 0 if not bad and not failed else 4


if __name__ == "__main__":
    sys.exit(main())
