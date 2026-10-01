import unittest
from unittest.mock import Mock

from credentials import Credentials, CredentialStore, validate_connection


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials.parse('test-token', 'ab' * 32)

    def test_representation_does_not_expose_secrets(self):
        self.assertNotIn(self.credentials.token, repr(self.credentials))
        self.assertNotIn(self.credentials.sign_key_hex, repr(self.credentials))

    def test_invalid_keys_are_rejected_without_echoing_input(self):
        for key in ('private-secret', '00' * 31, 'zz' * 32):
            with self.assertRaisesRegex(ValueError, '^key$'):
                Credentials.parse('token', key)

    def test_token_cannot_contain_whitespace(self):
        with self.assertRaisesRegex(ValueError, '^token$'):
            Credentials.parse('Bearer test', 'ab' * 32)

    def test_secure_store_round_trip_and_delete(self):
        backend = Mock()
        store = CredentialStore(backend)
        store.save(self.credentials)
        backend.get_password.return_value = backend.set_password.call_args.args[2]
        self.assertEqual(store.load(), self.credentials)
        store.delete()
        backend.delete_password.assert_called_once()

    def test_missing_store_never_writes_plaintext(self):
        store = CredentialStore()
        store.backend = None
        self.assertIsNone(store.load())
        with self.assertRaises(RuntimeError):
            store.save(self.credentials)

    def test_validation_is_read_only_and_uses_explicit_credentials(self):
        client = Mock()
        client._get.return_value = {'equity': '0'}
        factory = Mock(return_value=client)
        self.assertIs(validate_connection(self.credentials, factory), client)
        factory.assert_called_once_with(token='test-token', sign_key_hex='ab' * 32)
        client._get.assert_called_once_with('/api/query_balance', auth=True)
        client._post_signed.assert_not_called()

    def test_api_failure_cannot_pass_validation(self):
        client = Mock()
        client._get.return_value = {'code': 401, 'equity': '1'}
        with self.assertRaises(ValueError):
            validate_connection(self.credentials, Mock(return_value=client))

    def test_invalid_account_shape_cannot_pass_validation(self):
        client = Mock()
        for data in ([], {}, {'result': []}):
            client._get.return_value = data
            with self.assertRaises(ValueError):
                validate_connection(self.credentials, Mock(return_value=client))
