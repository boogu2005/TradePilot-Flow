"""Public deployments must not expose account data without credentials."""

import unittest
from unittest.mock import patch

from backend import config
from backend.auth import auth_enabled, verify_credentials


class DashboardAuthConfigTests(unittest.TestCase):
    def test_missing_password_is_rejected(self):
        with patch.object(config, "DASHBOARD_USERNAME", "admin"), \
             patch.object(config, "DASHBOARD_PASSWORD", ""), \
             patch.object(config, "DASHBOARD_AUTH_SECRET", "x" * 32):
            with self.assertRaises(RuntimeError):
                config.validate_auth_config()
            self.assertFalse(auth_enabled())
            self.assertFalse(verify_credentials("admin", ""))

    def test_short_secret_is_rejected(self):
        with patch.object(config, "DASHBOARD_USERNAME", "admin"), \
             patch.object(config, "DASHBOARD_PASSWORD", "test-password"), \
             patch.object(config, "DASHBOARD_AUTH_SECRET", "short"):
            with self.assertRaises(RuntimeError):
                config.validate_auth_config()
            self.assertFalse(auth_enabled())

    def test_private_credentials_allow_startup(self):
        with patch.object(config, "DASHBOARD_USERNAME", "admin"), \
             patch.object(config, "DASHBOARD_PASSWORD", "test-password"), \
             patch.object(config, "DASHBOARD_AUTH_SECRET", "x" * 32):
            config.validate_auth_config()
            self.assertTrue(auth_enabled())


if __name__ == "__main__":
    unittest.main()
