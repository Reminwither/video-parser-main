"""Credential JSON format checks without a server or account database."""
import copy
import unittest

import auth_db


class AuthPayloadTests(unittest.TestCase):
    def test_non_object_json_values_are_rejected(self):
        for value in (None, [], [1], True, False, 1, 1.5, "", "credentials"):
            with self.subTest(value=value):
                self.assertFalse(auth_db.valid_auth_payload(value))

    def test_username_and_password_are_required_strings(self):
        valid = {"username": "creator", "password": "password123"}
        for field in ("username", "password"):
            missing = dict(valid)
            del missing[field]
            with self.subTest(field=field, value="missing"):
                self.assertFalse(auth_db.valid_auth_payload(missing))
            for value in (None, [], {}, True, False, 123):
                payload = {**valid, field: value}
                with self.subTest(field=field, value=value):
                    self.assertFalse(auth_db.valid_auth_payload(payload))

    def test_optional_confirmation_must_be_a_string_when_provided(self):
        valid = {"username": "creator", "password": "password123"}
        self.assertTrue(auth_db.valid_auth_payload(valid))
        self.assertTrue(auth_db.valid_auth_payload({**valid, "password2": "password123"}))
        for value in (None, [], {}, True, False, 123):
            with self.subTest(value=value):
                self.assertFalse(auth_db.valid_auth_payload({**valid, "password2": value}))

    def test_legal_strings_and_existing_extra_fields_are_accepted(self):
        for payload in (
            {"username": "creator", "password": "password123"},
            {"username": " creator ", "password": " 密码 with spaces ", "password2": " 密码 with spaces "},
            {"username": "creator", "password": "password123", "next": "/account/files", "extra": [1]},
        ):
            with self.subTest(payload=payload):
                self.assertTrue(auth_db.valid_auth_payload(payload))

    def test_format_check_leaves_missing_content_and_password_policy_to_routes(self):
        for payload in (
            {"username": "", "password": ""},
            {"username": "bad name", "password": "short"},
            {"username": "creator", "password": "a" * 20_000},
            {"username": "creator", "password": "password123", "password2": "different"},
        ):
            with self.subTest(username=payload["username"], length=len(payload["password"])):
                self.assertTrue(auth_db.valid_auth_payload(payload))

    def test_payload_is_not_mutated(self):
        payload = {"username": " creator ", "password": " password ", "password2": " password ", "extra": [1]}
        original = copy.deepcopy(payload)
        self.assertTrue(auth_db.valid_auth_payload(payload))
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
