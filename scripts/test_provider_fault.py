import unittest
from provider_fault import fallback_reason


class FaultTests(unittest.TestCase):
    def test_confirmed_transport_and_gateway_failures(self):
        for message in ("HTTP 502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout",
                        "408 Request Timeout", "connect ECONNRESET", "getaddrinfo ENOTFOUND api.example.invalid",
                        "Unable to connect. Is the computer able to access the url?",
                        "connect ETIMEDOUT", "The socket connection was closed unexpectedly"):
            with self.subTest(message=message):
                self.assertEqual(fallback_reason([message]), "network")

    def test_reported_billing_exhaustion(self):
        for message in ("402 Payment Required", "429 insufficient_quota\ninsufficient_quota (type=insufficient_quota)", "429 {\"error\":{\"code\":\"insufficient_quota\"}}",
                        "429 {\"error\":{\"code\":\"billing_hard_limit_reached\"}}"):
            with self.subTest(message=message):
                self.assertEqual(fallback_reason([message]), "billing")

    def test_no_switch_for_other_or_mixed_failures(self):
        for values in (None, [], "402", [None], ["401 invalid key"], ["403 Forbidden"], ["404 Not Found"],
                       ["429 rate limited"], ["429 TPM limit exceeded"], ["400 context length exceeded"],
                       ["500 internal server error"], ["Thinking loop detected: ECONNRESET"], ["tool_idle"],
                       ["HTTP 404 body ECONNRESET"], ["certificate verify failed"], ["402","401"],
                       ["429 quota has not been exhausted"], ["429 please check insufficient_quota documentation"]):
            with self.subTest(values=values):
                self.assertIsNone(fallback_reason(values))
