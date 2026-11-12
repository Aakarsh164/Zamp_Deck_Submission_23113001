import re
import unittest
from pathlib import Path

from check_secrets import find_secrets


ROOT = Path(__file__).resolve().parents[1]


class SafetyConfigurationTests(unittest.TestCase):
    def test_environment_example_contains_only_empty_values(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        assignments = [line for line in lines if line and not line.startswith("#")]

        self.assertTrue(assignments)
        self.assertTrue(all(re.fullmatch(r"[A-Z_]+=", line) for line in assignments))

    def test_gitignore_keeps_environment_and_local_data_out_of_version_control(self):
        patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

        self.assertTrue({".env", ".env.*", "!.env.example", "app.db",
                         ".venv/", "__pycache__/", "test_invoices/"}.issubset(patterns))

    def test_secret_scanner_detects_key_prefix_and_smtp_password_patterns(self):
        examples = (
            "AIza" + "A" * 24,
            "sk-ant-" + "B" * 24,
            "gsk_" + "C" * 24,
            "SMTP_PASSWORD=" + "part-" + "0123456789",
        )

        for example in examples:
            with self.subTest(pattern=example.split("=")[0][:8]):
                self.assertTrue(find_secrets(example))

    def test_secret_scanner_ignores_empty_smtp_password_placeholder(self):
        self.assertEqual(find_secrets("SMTP_PASSWORD=\n"), [])

    def test_secret_scanner_ignores_python_test_identifiers(self):
        self.assertEqual(
            find_secrets("def test_kpis_and_decision_distribution_are_from_real_invoice_rows(self):"),
            [],
        )

    def test_secret_scanner_detects_long_mixed_case_tokens(self):
        token = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9" + "St0Uv"

        self.assertTrue(find_secrets("token=" + token))


if __name__ == "__main__":
    unittest.main()
