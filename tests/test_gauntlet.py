import unittest

import gauntlet


class GauntletTests(unittest.TestCase):
    def test_gauntlet_reports_honest_misses_with_reasons(self):
        result = gauntlet.run()

        self.assertEqual(result["total"], 16)
        self.assertEqual(
            {
                "Lookalike vendor name with the real vendor's bank digits",
                "Line item priced 40% above the usual unit price",
                "Invoice date far in the future",
                "Lookalike sender domain with a different TLD",
            },
            {item["label"] for item in result["cases"][-4:]},
        )
        self.assertEqual(
            {item["label"] for item in result["misses"]},
            {item["label"] for item in result["cases"] if not item["ok"]},
        )
        self.assertTrue(all(item["note"].strip() for item in result["misses"]))
        self.assertIn("false_escalations", result)


if __name__ == "__main__":
    unittest.main()
