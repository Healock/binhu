import io
import json
import unittest

from deploy.environments import dev_bootstrap_acceptance


def payload(**changes):
    value = {
        "server_version": "0.28.20",
        "environment": "development",
        "environment_id": "development",
        "api_entry": "/dev/api",
        "environment_label": "Dev 环境 · 虚构数据",
        "data_kind": "虚构或脱敏开发数据",
        "available_features": ["synthetic.permission"],
        "private_diagnostic": "must-not-be-reported",
    }
    value.update(changes)
    return value


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, body, *, status=200):
        self.body = body
        self.status = status
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        response = Response(self.body)
        response.status = self.status
        return response


class DevBootstrapAcceptanceTests(unittest.TestCase):
    def test_current_dev_contract_is_accepted_and_summary_is_allowlisted(self):
        result = dev_bootstrap_acceptance.validate_payload(payload(), "0.28.20")

        self.assertTrue(result["contract_verified"])
        self.assertEqual(result["server_version"], "0.28.20")
        self.assertEqual(result["api_entry"], "/dev/api")
        self.assertNotIn("available_features", result)
        self.assertNotIn("private_diagnostic", result)
        self.assertNotIn("must-not-be-reported", json.dumps(result))

    def test_old_version_field_and_old_dev_entry_are_rejected(self):
        old_field = payload()
        old_field["version"] = old_field.pop("server_version")
        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_server_version_mismatch",
        ):
            dev_bootstrap_acceptance.validate_payload(old_field, "0.28.20")

        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_api_entry_mismatch",
        ):
            dev_bootstrap_acceptance.validate_payload(
                payload(api_entry="/dev-api"), "0.28.20"
            )

    def test_each_dev_identity_field_is_required(self):
        for field in dev_bootstrap_acceptance.DEV_BOOTSTRAP_CONTRACT:
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    dev_bootstrap_acceptance.BootstrapContractError,
                    f"dev_bootstrap_{field}_mismatch",
                ):
                    dev_bootstrap_acceptance.validate_payload(
                        payload(**{field: "unexpected"}), "0.28.20"
                    )

    def test_mismatch_does_not_include_received_value(self):
        secret = "synthetic-secret-must-not-leak"
        try:
            dev_bootstrap_acceptance.validate_payload(
                payload(environment_label=secret), "0.28.20"
            )
        except dev_bootstrap_acceptance.BootstrapContractError as exc:
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("mismatched label was accepted")

    def test_fetch_uses_fixed_loopback_endpoint_and_limits_response(self):
        opener = Opener(json.dumps(payload()).encode())
        result = dev_bootstrap_acceptance.verify("0.28.20", opener)

        self.assertTrue(result["contract_verified"])
        self.assertEqual(opener.request.full_url, dev_bootstrap_acceptance.BOOTSTRAP_URL)
        self.assertEqual(opener.timeout, 8)

        oversized = Opener(b"{" + b" " * dev_bootstrap_acceptance.MAX_RESPONSE_BYTES)
        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_response_too_large",
        ):
            dev_bootstrap_acceptance.fetch_payload(oversized)

    def test_invalid_expected_version_and_non_json_response_fail_closed(self):
        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_expected_version_invalid",
        ):
            dev_bootstrap_acceptance.validate_payload(payload(), "latest")

        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_payload_invalid",
        ):
            dev_bootstrap_acceptance.fetch_payload(Opener(b"not-json"))

    def test_non_success_status_is_rejected_without_body_details(self):
        opener = Opener(b'{"detail":"synthetic-private-body"}', status=503)
        with self.assertRaisesRegex(
            dev_bootstrap_acceptance.BootstrapContractError,
            "dev_bootstrap_http_status_invalid",
        ):
            dev_bootstrap_acceptance.fetch_payload(opener)


if __name__ == "__main__":
    unittest.main()
