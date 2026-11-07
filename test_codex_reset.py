from copy import deepcopy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import codex_reset as watch


NOW = 1800000000


def credit(credit_id, expires):
    return {"id": credit_id, "expiresAt": expires, "status": "available",
            "resetType": "codexRateLimits"}


def snapshot(rows=None, used=95):
    rows = rows if rows is not None else [credit("later", NOW + 86400), credit("first", NOW + 600)]
    return {"rateLimitResetCredits": {"availableCount": len(rows), "credits": rows},
            "rateLimitsByLimitId": {"codex": {"primary": {
                "usedPercent": used, "windowDurationMins": 10080, "resetsAt": NOW + 3600}}}}


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "attempt.json"
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def run_reset(self, client, credit_id="first"):
        return watch.apply_reset(client, credit_id, self.path, 15, NOW)

    def test_earliest_is_independent_of_server_order(self):
        for rows in ([credit("later", NOW + 86400), credit("first", NOW + 600)],
                     [credit("first", NOW + 600), credit("later", NOW + 86400)]):
            self.assertEqual(watch.earliest(snapshot(rows), NOW)["id"], "first")

    def test_expired_and_nonexpiring_are_not_selected(self):
        rows = [credit("expired", NOW), credit("never", None), credit("first", NOW + 600)]
        self.assertEqual(watch.earliest(snapshot(rows), NOW)["id"], "first")

    def test_ambiguous_lists_refuse_consumption(self):
        base = snapshot()
        variants = []
        for field, value in (("credits", None), ("availableCount", 3)):
            item = deepcopy(base)
            item["rateLimitResetCredits"][field] = value
            variants.append(item)
        item = deepcopy(base)
        del item["rateLimitResetCredits"]["credits"][0]["expiresAt"]
        variants.append(item)
        item = deepcopy(base)
        item["rateLimitResetCredits"]["credits"][0]["id"] = "first"
        variants.append(item)
        for value in variants:
            with self.subTest(value=value):
                client = Mock()
                client.call.return_value = value
                with self.assertRaises(ValueError):
                    self.run_reset(client)
                self.assertEqual(client.call.call_count, 1)

    def test_later_credit_is_never_consumed_even_if_authorized(self):
        client = Mock()
        client.call.return_value = snapshot()
        self.run_reset(client, "later")
        client.call.assert_called_once_with(watch.READ)
        self.assertFalse(self.path.exists())

    def test_waits_until_near_expiry_and_eligible(self):
        cases = [snapshot(used=89), snapshot([credit("first", NOW + 901)]),
                 snapshot([credit("first", NOW)]), snapshot([])]
        stale = snapshot()
        stale["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] = NOW
        cases.append(stale)
        for value in cases:
            client = Mock()
            client.call.return_value = value
            self.run_reset(client)
            client.call.assert_called_once_with(watch.READ)

    def test_exact_id_is_sent_and_other_credits_are_verified(self):
        client = Mock()
        client.call.side_effect = [snapshot(), {"outcome": "reset"},
                                   snapshot([credit("later", NOW + 86400)], used=0)]
        self.run_reset(client)
        method, params = client.call.call_args_list[1].args
        self.assertEqual(method, watch.CONSUME)
        self.assertEqual(params["creditId"], "first")
        self.assertTrue(params["idempotencyKey"])
        self.assertEqual(json.loads(self.path.read_text())["outcome"], "reset")

    def test_uncertain_response_retries_same_id_and_key(self):
        client = Mock()
        client.call.side_effect = [snapshot(), TimeoutError("lost response")]
        with self.assertRaises(TimeoutError):
            self.run_reset(client)
        first_params = client.call.call_args_list[1].args[1]
        client.reset_mock()
        client.call.side_effect = [snapshot(), {"outcome": "alreadyRedeemed"},
                                   snapshot([credit("later", NOW + 86400)])]
        self.run_reset(client)
        self.assertEqual(client.call.call_args_list[1].args[1], first_params)

    def test_success_with_failed_refresh_never_consumes_again(self):
        client = Mock()
        client.call.side_effect = [snapshot(), {"outcome": "reset"}, TimeoutError("refresh")]
        with self.assertRaises(TimeoutError):
            self.run_reset(client)
        client.reset_mock()
        client.call.side_effect = None
        client.call.return_value = snapshot([credit("later", NOW + 86400)])
        self.run_reset(client)
        client.call.assert_called_once_with(watch.READ)

    def test_verified_success_does_not_recheck_other_credits_later(self):
        watch.save(self.path, {"creditId": "first", "outcome": "reset", "verified": True})
        client = Mock()
        self.run_reset(client)
        client.call.assert_not_called()

    def test_absent_credit_after_uncertain_response_stops(self):
        watch.save(self.path, {"creditId": "first", "idempotencyKey": "same", "outcome": "pending"})
        client = Mock()
        client.call.return_value = snapshot([credit("later", NOW + 86400)])
        with self.assertRaises(RuntimeError):
            self.run_reset(client)
        client.call.assert_called_once_with(watch.READ)

    def test_wrong_credit_disappearance_is_detected_and_never_retried(self):
        client = Mock()
        client.call.side_effect = [snapshot(), {"outcome": "reset"}, snapshot([])]
        with self.assertRaisesRegex(RuntimeError, "Other reset IDs"):
            self.run_reset(client)
        self.assertEqual(json.loads(self.path.read_text())["outcome"], "reset")

    def test_refused_attempt_gets_new_key_only_after_definitive_response(self):
        client = Mock()
        client.call.side_effect = [snapshot(), {"outcome": "nothingToReset"}]
        self.run_reset(client)
        sent_key = client.call.call_args_list[1].args[1]["idempotencyKey"]
        self.assertNotEqual(json.loads(self.path.read_text())["idempotencyKey"], sent_key)

    def test_protocol_must_explicitly_support_credit_id(self):
        schema = {"oneOf": [{"properties": {"method": {"enum": [watch.CONSUME]},
                  "params": {"$ref": "#/definitions/Params"}}}],
                  "definitions": {"Params": {"properties": {"creditId": {"type": "string"}}}}}
        self.assertTrue(watch.supports_exact_credit(schema))
        del schema["definitions"]["Params"]["properties"]["creditId"]
        self.assertFalse(watch.supports_exact_credit(schema))


if __name__ == "__main__":
    unittest.main()
