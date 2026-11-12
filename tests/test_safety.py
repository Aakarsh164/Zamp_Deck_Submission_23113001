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
                         ".venv/", "venv/", "__pycache__/", "*.pyc",
                         "app.db-wal", "app.db-shm", "*.db-journal",
                         "test_invoices/",
                         "smtp_diag.py", "*.log", ".git/"}.issubset(patterns))

    def test_dockerignore_excludes_secrets_databases_and_local_artifacts(self):
        patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

        self.assertTrue({
            ".env", ".env.*", "!.env.example", ".venv/", "venv/",
            "__pycache__/", "*.pyc", "app.db", "*.db-wal", "*.db-shm",
            "*.db-journal", "test_invoices/", "smtp_diag.py", "*.log", ".git/",
        }.issubset(patterns))

    def test_frontend_uses_local_api_without_token_login(self):
        frontend = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        for token_name in ("ADMIN_TOKEN", "INBOUND_TOKEN", "X-Admin-Token", "X-Inbound-Token"):
            self.assertNotIn(token_name, frontend)
            self.assertNotIn(token_name, html)
        for login_marker in ("authGate", "adminTokenInput", "loginForm", "sessionStorage"):
            self.assertNotIn(login_marker, frontend)
            self.assertNotIn(login_marker, html)
        self.assertIn('return fetch(url, options);', frontend)
        self.assertIn("setInterval(pollLive, 1500)", frontend)

    def test_local_development_docs_keep_server_loopback_bound(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("--host 127.0.0.1 --port 8000", readme)
        self.assertIn("authentication must be configured before public or multi-user deployment", readme)

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
