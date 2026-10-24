"""Native navigation boundary checks; no app launch, account or model call."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopNavigationTests(unittest.TestCase):
    def guard(self):
        source = (ROOT / 'desktop/GrowthDesk.swift').read_text()
        policy = source.split('decidePolicyFor action:WKNavigationAction', 1)[1].split('func webView(', 1)[0]
        match = re.search(r'if ([^\n]+about:srcdoc[^\n]+?) \{decisionHandler\(\.allow\);return\}', policy)
        self.assertIsNotNone(match, 'srcdoc must have its own explicit navigation guard')
        expression = match.group(1)
        self.assertEqual(expression, 'u.absoluteString=="about:srcdoc" && action.targetFrame?.isMainFrame == false')
        self.assertIn('guard let u=action.request.url else {decisionHandler(.cancel);return}', policy)
        self.assertIn('u.scheme=="http" && u.host=="127.0.0.1" && u.port==60128', policy)
        self.assertIn('decisionHandler(.cancel)', policy)
        self.assertNotIn('u.scheme=="about"', policy)
        return expression

    def test_srcdoc_allows_only_the_exact_subframe_case(self):
        self.guard()

    @unittest.skipUnless(shutil.which('swift'), 'Swift runtime not installed; static guard test remains required')
    def test_actual_swift_guard_rejects_main_popup_external_and_url_variants(self):
        expression = self.guard()
        program = r'''import Foundation
struct Frame {let isMainFrame: Bool}
struct Action {let targetFrame: Frame?}
func allowsPreview(_ raw: String, _ main: Bool?) -> Bool {
    let u = URL(string: raw)!
    let action = Action(targetFrame: main.map {Frame(isMainFrame: $0)})
    return GUARD
}
assert(allowsPreview("about:srcdoc", false))
assert(!allowsPreview("about:srcdoc", true))
assert(!allowsPreview("about:srcdoc", nil))
for raw in ["about:blank", "about:srcdoc#heading", "about:srcdoc?extra=1", "data:text/html,x", "blob:https://example.invalid/id", "javascript:alert(1)", "https://example.invalid/", "http://127.0.0.1:60128/"] {
    assert(!allowsPreview(raw, false), raw)
}
print("Native srcdoc boundary assertions passed")
'''.replace('GUARD', expression)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'NavigationBoundary.swift'
            script.write_text(program)
            result = subprocess.run([shutil.which('swift'), str(script)], capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Native srcdoc boundary assertions passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
