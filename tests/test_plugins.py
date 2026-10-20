"""Tests for tools/termux_plugins.py with fake `pm`/`su`, a local fake GitHub + F-Droid, and real openssl certificates."""
import hashlib, http.server, json, os, shutil, stat, struct, subprocess, sys, tempfile, threading, unittest, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "termux_plugins.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import termux_plugins as tp

def sh(cmd): return subprocess.run(cmd, shell=True, check=True, capture_output=True)

class Key:
    def __init__(self, tmp, name):
        self.tmp, self.name = tmp, name
        sh("openssl req -x509 -newkey rsa:2048 -nodes -keyout %s/%s.key -out %s/%s.pem -subj /CN=%s -days 3650" % (tmp, name, tmp, name, name))
        self.der = subprocess.run(["openssl", "x509", "-in", "%s/%s.pem" % (tmp, name), "-outform", "DER"], capture_output=True, check=True).stdout
        self.fp = hashlib.sha256(self.der).hexdigest()
        open(tmp + "/data.bin", "wb").write(b"signed content")
        self.p7 = subprocess.run(["openssl", "smime", "-sign", "-binary", "-in", tmp + "/data.bin", "-signer", "%s/%s.pem" % (tmp, name),
                                  "-inkey", "%s/%s.key" % (tmp, name), "-outform", "DER"], capture_output=True, check=True).stdout

def lp(b): return struct.pack("<I", len(b)) + b

def make_apk(path, pkg, key, scheme="v1"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest"); z.writestr("fake_package.txt", pkg); z.writestr("fake_key.txt", key.name if key else "none")
        if key and scheme in ("v1", "both"): z.writestr("META-INF/CERT.RSA", key.p7)
    if key and scheme in ("v2", "both"):
        data = open(path, "rb").read()
        i = data.rfind(b"PK\x05\x06"); cd = struct.unpack("<I", data[i + 16:i + 20])[0]
        signed = lp(b"") + lp(lp(key.der))
        value = lp(lp(lp(signed) + lp(b"") + lp(b"")))
        pairs = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", 0x7109871a) + value
        block = struct.pack("<Q", len(pairs) + 24) + pairs + struct.pack("<Q", len(pairs) + 24) + b"APK Sig Block 42"
        data = data[:cd] + block + data[cd:]
        j = data.rfind(b"PK\x05\x06"); data = data[:j + 16] + struct.pack("<I", cd + len(block)) + data[j + 20:]
        open(path, "wb").write(data)

FAKE_PM = r'''#!/bin/sh
S="$FKSTATE"
blind() { [ -z "$FKROOT" ] && [ -f "$S/blind" ]; }
case "$1" in
 list) if blind; then echo "package:com.termux  installer=null"; else while read -r p i; do echo "package:$p  installer=$i"; done < "$S/packages"; fi ;;
 path) if blind && [ "$2" != com.termux ]; then exit 0; fi; grep "^$2 " "$S/paths" | cut -d' ' -f2 | sed 's/^/package:/' ;;
 install) f="$4"; [ -f "$f" ] || { echo "Failure [no file]"; exit 1; }; p=$(unzip -p "$f" fake_package.txt); k=$(unzip -p "$f" fake_key.txt); mkdir -p "$S/apks" "$S/keys"
   if [ "$p" != com.termux ] && [ -f "$S/keys/com.termux" ] && [ "$k" != "$(cat "$S/keys/com.termux")" ]; then echo "Failure [INSTALL_FAILED_SHARED_USER_INCOMPATIBLE: signatures do not match]"; exit 1; fi
   if [ -f "$S/keys/$p" ] && [ "$k" != "$(cat "$S/keys/$p")" ]; then echo "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Package $p signatures do not match previously installed version; ignoring!]"; exit 1; fi
   cp "$f" "$S/apks/$p.apk"; echo "$k" > "$S/keys/$p"
   grep -v "^$p " "$S/packages" > "$S/p.tmp"; echo "$p null" >> "$S/p.tmp"; mv "$S/p.tmp" "$S/packages"
   grep -v "^$p " "$S/paths" > "$S/q.tmp"; echo "$p $S/apks/$p.apk" >> "$S/q.tmp"; mv "$S/q.tmp" "$S/paths"; echo "$p" >> "$S/installed.log"; echo Success ;;
 uninstall) grep -v "^$2 " "$S/packages" > "$S/p.tmp"; mv "$S/p.tmp" "$S/packages"; rm -f "$S/keys/$2"; echo "$2" >> "$S/uninstalled.log"; echo Success ;;
esac
'''
FAKE_SU = r'''#!/bin/sh
[ "$1" = "-c" ] || exit 1
export FKROOT=1
case "$2" in *"id -u") echo 0 ;; *) mkdir -p "$FKSTATE/tmp"; exec sh -c "$(printf '%s' "$2" | sed "s#/data/local/tmp#$FKSTATE/tmp#g")" ;; esac
'''
FAKE_OPEN = '#!/bin/sh\necho "$1" >> "$FKSTATE/open.log"\nFKROOT=1 pm install -r -d "$1" >> "$FKSTATE/open-out.log" 2>&1\n'

class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="fkt_"); self.home = self.tmp + "/home"; self.state = self.tmp + "/state"; self.bin = self.tmp + "/bin"
        for d in (self.home, self.state, self.bin, self.tmp + "/apks"): os.makedirs(d)
        for name, body in (("pm", FAKE_PM), ("su", FAKE_SU), ("termux-open", FAKE_OPEN), ("am", '#!/bin/sh\necho "$@" >> "$FKSTATE/am.log"\n'), ("getprop", "#!/bin/sh\necho arm64-v8a\n")):
            open(self.bin + "/" + name, "w").write(body); os.chmod(self.bin + "/" + name, 0o755)
        self.keys = {}; self.requests = []; self.routes = {}
        open(self.state + "/packages", "w").close(); open(self.state + "/paths", "w").close(); os.makedirs(self.state + "/keys")
        env = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                env.requests.append(self.path)
                r = env.routes.get(self.path)
                if r is None: self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Length", str(len(r))); self.end_headers(); self.wfile.write(r)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def key(self, n):
        if n not in self.keys: self.keys[n] = Key(self.tmp, n)
        return self.keys[n]
    def install_pkg(self, pkg, key, installer="null", scheme="v1", real_key=None):
        p = "%s/apks/%s.apk" % (self.tmp, pkg); make_apk(p, pkg, self.key(key) if key else None, scheme)
        os.makedirs(self.state + "/keys", exist_ok=True); open(self.state + "/keys/" + pkg, "w").write(real_key or key or "none")
        for f in ("packages", "paths"):
            lines = [l for l in open(self.state + "/" + f).read().splitlines() if not l.startswith(pkg + " ")]
            lines.append("%s %s" % (pkg, installer if f == "packages" else p)); open(self.state + "/" + f, "w").write("\n".join(lines) + "\n")
    def github(self, repo, assets):  # assets: [(name, key, digest)]
        out = []
        for name, key, digest in assets:
            make_apk(self.tmp + "/apks/dl_" + name, {"termux/termux-gui": "com.termux.gui", "termux/termux-api": "com.termux.api"}[repo], self.key(key)); self.routes["/dl/" + name] = open(self.tmp + "/apks/dl_" + name, "rb").read()
            a = {"name": name, "browser_download_url": "http://127.0.0.1:%d/dl/%s" % (self.port, name)}
            if digest: a["digest"] = digest
            out.append(a)
        self.routes["/repos/%s/releases/latest" % repo] = json.dumps({"tag_name": "v1", "assets": out}).encode()
    def fdroid(self, pkg, key):
        make_apk(self.tmp + "/apks/fd_" + pkg, pkg, self.key(key)); self.routes["/fdroid-repo/%s_7.apk" % pkg] = open(self.tmp + "/apks/fd_" + pkg, "rb").read()
        self.routes["/fdroid-api/" + pkg] = json.dumps({"packageName": pkg, "suggestedVersionCode": 7, "packages": [{"versionCode": 7}]}).encode()
    def run(self, *args, **extra):
        e = dict(os.environ, **extra); e.update(HOME=self.home, FKSTATE=self.state, PATH=self.bin + ":" + os.environ["PATH"],
                 FK_GITHUB_API="http://127.0.0.1:%d" % self.port, FK_FDROID_API="http://127.0.0.1:%d/fdroid-api" % self.port,
                 FK_FDROID_REPO="http://127.0.0.1:%d/fdroid-repo" % self.port)
        for k in ("FK_NO_ROOT", "FK_PROBE_GUI", "FK_PROBE_API"):
            if k not in extra: e.pop(k, None)
        p = subprocess.run([sys.executable, SCRIPT] + list(args), env=e, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        return p.returncode, p.stdout + p.stderr
    def installed(self): return sorted(l.split()[0] for l in open(self.state + "/packages").read().splitlines() if l.strip())
    def close(self): self.srv.shutdown(); shutil.rmtree(self.tmp, ignore_errors=True)

class Fingerprints(unittest.TestCase):
    def test_all_schemes(self):
        e = Env(); self.addCleanup(e.close); k = e.key("A")
        for scheme in ("v1", "v2", "both"):
            p = "%s/%s.apk" % (e.tmp, scheme); make_apk(p, "x", k, scheme)
            self.assertEqual(tp.apk_certs(p), [k.fp], scheme)
            self.assertEqual(subprocess.run(["openssl", "x509", "-in", e.tmp + "/A.pem", "-noout", "-fingerprint", "-sha256"], capture_output=True, text=True).stdout.strip().split("=")[1].replace(":", "").lower(), k.fp)
        make_apk(e.tmp + "/u.apk", "x", None); self.assertEqual(tp.apk_certs(e.tmp + "/u.apk"), [])
        open(e.tmp + "/junk.apk", "wb").write(b"not a zip"); self.assertEqual(tp.apk_certs(e.tmp + "/junk.apk"), [])
        self.assertTrue(tp.same_signer([k.fp], [k.fp, "x"])); self.assertFalse(tp.same_signer([k.fp], ["y"])); self.assertFalse(tp.same_signer([], [k.fp]))

class Flow(unittest.TestCase):
    def setUp(self): self.e = Env(); self.addCleanup(self.e.close)
    def both_github(self, key, digest=None):
        self.e.github("termux/termux-gui", [("gui-x86.apk", key, digest), ("gui-arm64-v8a.apk", key, digest)]); self.e.github("termux/termux-api", [("api.apk", key, digest)])
    def both_fdroid(self, key): self.e.fdroid("com.termux.gui", key); self.e.fdroid("com.termux.api", key)
    def test_github_source_matches(self):
        e = self.e; e.install_pkg("com.termux", "K1"); self.both_github("K1"); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertEqual(e.installed(), ["com.termux", "com.termux.api", "com.termux.gui"])
        self.assertFalse([r for r in e.requests if "fdroid" in r], "F-Droid not needed"); self.assertIn("signature matches Termux", out)
        self.assertTrue([r for r in e.requests if "gui-arm64-v8a" in r], "picked the build for this CPU")
        self.assertIn("DONE", open(e.home + "/.flashkit/plugin-install.log").read()); self.assertIn("com.termux.app.TermuxActivity", open(e.state + "/am.log").read())
    def test_fdroid_termux_tries_fdroid_first(self):
        e = self.e; e.install_pkg("com.termux", "K2", installer="org.fdroid.fdroid"); self.both_github("K1"); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertFalse([r for r in e.requests if r.startswith("/dl/") or "/repos/" in r], "GitHub not needed")
    def test_falls_back_to_other_source(self):
        e = self.e; e.install_pkg("com.termux", "K2", scheme="v2"); self.both_github("K1"); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertIn("signed by a different key", out); self.assertIn("com.termux.gui", e.installed())
    def test_no_matching_download(self):
        e = self.e; e.install_pkg("com.termux", "K3"); self.both_github("K1"); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 2, out); self.assertIn("SAME place", out); self.assertEqual(e.installed(), ["com.termux"])
    def test_bad_checksum_is_skipped(self):
        e = self.e; e.install_pkg("com.termux", "K2"); self.both_github("K2", digest="sha256:" + "0" * 64); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertIn("checksum does not match", out)
    def test_wrong_signature_plugin_is_replaced(self):
        e = self.e; e.install_pkg("com.termux", "K1"); e.install_pkg("com.termux.gui", "K9"); e.install_pkg("com.termux.api", "K1"); self.both_github("K1")
        rc, out = e.run("--check"); self.assertEqual(rc, 3); self.assertIn("DIFFERENT key", out)
        rc, out = e.run("--yes"); self.assertEqual(rc, 0, out)
        self.assertIn("com.termux.gui", open(e.state + "/uninstalled.log").read()); self.assertEqual(open(e.state + "/installed.log").read().split(), ["com.termux.gui"])
        self.assertEqual(e.run("--check")[0], 0)
    def test_nothing_to_do_makes_no_requests(self):
        e = self.e
        for p in ("com.termux", "com.termux.gui", "com.termux.api"): e.install_pkg(p, "K1")
        rc, out = e.run(); self.assertEqual((rc, e.requests), (0, [])); self.assertIn("Nothing to do", out)
    def test_unreadable_termux_signature_still_installs(self):
        e = self.e; e.install_pkg("com.termux", None, real_key="K1"); self.both_github("K1")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertIn("can't be read", out)
    def test_blind_pm_without_root_is_not_fooled(self):
        """Android hides other apps from a normal Termux user - the installed plugins must still be found (through root)."""
        e = self.e
        for p in ("com.termux", "com.termux.gui", "com.termux.api"): e.install_pkg(p, "K1")
        open(e.state + "/blind", "w").close(); self.both_github("K9")
        rc, out = e.run(); self.assertEqual((rc, e.requests), (0, [])); self.assertIn("Nothing to do", out); self.assertIn("signature matches", out)
    def test_working_plugin_counts_as_installed_even_if_hidden(self):
        e = self.e; e.install_pkg("com.termux", "K1"); open(e.state + "/packages", "w").write("com.termux null\n"); open(e.state + "/blind", "w").close()
        rc, out = e.run(FK_PROBE_GUI="true", FK_PROBE_API="true", FK_NO_ROOT="1")
        self.assertEqual((rc, e.requests), (0, [])); self.assertIn("Nothing to do", out)
    def test_android_refuses_first_source_so_next_is_tried(self):
        e = self.e; e.install_pkg("com.termux", None, real_key="K2"); self.both_github("K1"); self.both_fdroid("K2")
        rc, out = e.run(); self.assertEqual(rc, 0, out); self.assertIn("Android refused it", out)
        self.assertEqual(e.installed(), ["com.termux", "com.termux.api", "com.termux.gui"]); self.assertTrue([r for r in e.requests if "fdroid-repo" in r])
    def test_existing_working_plugin_is_never_overwritten(self):
        e = self.e; e.install_pkg("com.termux", None, real_key="K2"); e.install_pkg("com.termux.gui", "K2"); e.install_pkg("com.termux.api", "K2"); self.both_github("K1")
        rc, out = e.run(); self.assertEqual((rc, e.requests), (0, [])); self.assertIn("Nothing to do", out)
    def test_no_root_opens_the_android_installer(self):
        e = self.e; e.install_pkg("com.termux", "K1"); self.both_github("K1")
        rc, out = e.run(FK_NO_ROOT="1"); self.assertEqual(rc, 0, out); self.assertIn("No root", out); self.assertIn("tap Install", out)
        self.assertEqual(len(open(e.state + "/open.log").read().split()), 2); self.assertEqual(sorted(open(e.state + "/installed.log").read().split()), ["com.termux.api", "com.termux.gui"])
        self.assertFalse(os.path.exists(e.state + "/tmp"), "nothing went through su")
    def test_no_root_and_blind_pm_uses_the_plugins_answer(self):
        e = self.e; e.install_pkg("com.termux", "K1"); open(e.state + "/blind", "w").close(); self.both_github("K1")
        rc, out = e.run(FK_NO_ROOT="1", FK_PROBE_GUI='test -f "$FKSTATE/apks/com.termux.gui.apk"', FK_PROBE_API='test -f "$FKSTATE/apks/com.termux.api.apk"')
        self.assertEqual(rc, 0, out); self.assertIn("Termux:GUI installed", out)
    def test_offline_is_reported_not_crashed(self):
        e = self.e; e.install_pkg("com.termux", "K1"); rc, out = e.run()
        self.assertEqual(rc, 2); self.assertIn("not available there", out); self.assertIn("no internet", out); self.assertNotIn("Traceback", out)

if __name__ == "__main__": unittest.main(verbosity=1)
