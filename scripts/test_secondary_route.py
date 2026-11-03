import unittest

from worker_model import DEFAULT_MODEL, LEGACY_MODEL, SECONDARY_MODEL, frozen_model, route_for


class RouteTests(unittest.TestCase):
    def test_secondary_identity_and_secret_namespace(self):
        self.assertEqual(SECONDARY_MODEL, "secondary-ds/deepseek-v4-flash-ascend1")
        route = route_for(SECONDARY_MODEL)
        self.assertEqual((route.profile, route.credential_file, route.credential_key, route.credential_env),
                         ("codex-ds-secondary", "secondary-credentials.json", "api_key", "OMP_SECONDARY_DS_API_KEY"))
        self.assertEqual(frozen_model({"worker_model": SECONDARY_MODEL}, {"worker_model": SECONDARY_MODEL}), SECONDARY_MODEL)

    def test_historical_routes(self):
        for model in (DEFAULT_MODEL, LEGACY_MODEL, frozen_model({}, {})):
            route = route_for(model)
            self.assertEqual((route.profile, route.credential_file, route.credential_key, route.credential_env),
                             ("codex-ds", "credentials.json", "opencode_api_key", "OMP_OPENCODE_API_KEY"))

    def test_invalid_or_mismatched_routes(self):
        for value in ("unknown/model", None, [], "secondary-ds/deepseek-v4.1-flash"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                route_for(value)
        with self.assertRaises(ValueError):
            frozen_model({"worker_model": SECONDARY_MODEL}, {"worker_model": DEFAULT_MODEL})
