"""The README must not point at missing images or pages; every SVG must be valid; the social image must fit GitHub's limits."""
import glob, os, re, struct, unittest, xml.dom.minidom
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

class Docs(unittest.TestCase):
    def test_readme_links_exist(self):
        text = read("README.md")
        refs = set(re.findall(r'src="([^"#:]+)"', text)) | set(re.findall(r'\]\(((?:assets|docs)/[^)#]+|LICENSE|CHANGELOG\.md)\)', text))
        self.assertTrue(refs)
        for r in refs: self.assertTrue(os.path.exists(os.path.join(ROOT, r)), "missing: " + r)
    def test_svgs_are_valid_and_clean(self):
        files = glob.glob(os.path.join(ROOT, "assets", "*.svg")); self.assertGreaterEqual(len(files), 5)
        for f in files:
            raw = open(f, "rb").read(); xml.dom.minidom.parseString(raw)
            self.assertTrue(raw.startswith(b"<?xml"), os.path.basename(f)); self.assertNotIn("\u00c2".encode("utf-8"), raw, "garbled character in " + f)
            self.assertNotIn(b"<script", raw)
    def test_social_preview(self):
        p = os.path.join(ROOT, "assets", "social-preview.png"); b = open(p, "rb").read()
        self.assertEqual(b[:8], b"\x89PNG\r\n\x1a\n"); self.assertEqual(struct.unpack(">II", b[16:24]), (1280, 640)); self.assertLess(len(b), 1024 * 1024)
    def test_placeholder_only_where_publish_fills_it(self):
        allowed = {"README.md", "docs/GUIDE.md", "get.sh", "tools/repo.conf", ".github/ISSUE_TEMPLATE/config.yml", "tools/publish.sh", "bin/flashkit"}
        for dp, _, fs in os.walk(ROOT):
            if ".git" in dp.split(os.sep) or "__pycache__" in dp: continue
            for f in fs:
                rel = os.path.relpath(os.path.join(dp, f), ROOT)
                if rel.endswith((".png", ".pyc")) or rel.startswith("tests"): continue
                if "__GH_REPO__" in open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read(): self.assertIn(rel, allowed, rel)
    def test_publish_fills_every_file_that_has_the_placeholder(self):
        pub = read("tools/publish.sh"); m = re.search(r"for f in ([^;]+); do", pub); self.assertTrue(m)
        filled = set(m.group(1).split())
        for rel in ("README.md", "docs/GUIDE.md", "get.sh", "tools/repo.conf", ".github/ISSUE_TEMPLATE/config.yml"): self.assertIn(rel, filled)

if __name__ == "__main__": unittest.main()
