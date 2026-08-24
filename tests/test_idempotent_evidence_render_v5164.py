import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IdempotentEvidenceRenderTests(unittest.TestCase):
    def test_evidence_has_one_owned_render_region(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="fundamentalEvidence"'), 1)

    def test_render_replaces_instead_of_appending_evidence(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("$('fundamentalEvidence').innerHTML=`${ledgerHtml}${evidenceHtml}`", source)
        self.assertNotIn("$('fundamentalTable').insertAdjacentHTML('afterend',ledgerHtml)", source)
        self.assertNotIn("document.querySelector('.eps-ledger')?.insertAdjacentHTML('afterend',evidenceHtml)", source)

    def test_mobile_details_are_rebuilt_inside_replaced_region(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")
        evidence_render = source.index("$('fundamentalEvidence').innerHTML")
        mobile_setup = source.index("setupMobileDetails();", evidence_render)
        self.assertLess(evidence_render, mobile_setup)


if __name__ == "__main__":
    unittest.main()
