import hashlib
import hmac
import json
import unittest

from crypto_client import CryptoComClient, CryptoComError, params_to_str, sign
from tests.fakes import FakeResponse, FakeSession, Ticker


def reference_sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class ParamStringTests(unittest.TestCase):
    def test_keys_sorted_and_concatenated_without_delimiters(self):
        params = {"side": "BUY", "instrument_name": "USDU_USD", "notional": "12.34", "type": "MARKET"}
        self.assertEqual(params_to_str(params), "instrument_nameUSDU_USDnotional12.34sideBUYtypeMARKET")

    def test_nested_order_list_matches_documented_algorithm(self):
        # Request from the official "Digital Signature" Python example.
        params = {
            "contingency_type": "LIST",
            "order_list": [
                {"instrument_name": "ONE_USDT", "side": "BUY", "type": "LIMIT", "price": "0.24", "quantity": "1.0"},
                {"instrument_name": "ONE_USDT", "side": "BUY", "type": "STOP_LIMIT", "price": "0.27",
                 "quantity": "1.0", "trigger_price": "0.26"},
            ],
        }
        self.assertEqual(
            params_to_str(params),
            "contingency_typeLISTorder_list"
            "instrument_nameONE_USDTprice0.24quantity1.0sideBUYtypeLIMIT"
            "instrument_nameONE_USDTprice0.27quantity1.0sideBUYtrigger_price0.26typeSTOP_LIMIT",
        )

    def test_none_is_rendered_as_null(self):
        self.assertEqual(params_to_str({"b": None, "a": "x"}), "axbnull")

    def test_scalar_list_values_are_concatenated(self):
        self.assertEqual(params_to_str({"exec_inst": ["POST_ONLY"]}), "exec_instPOST_ONLY")

    def test_float_and_bool_params_are_rejected(self):
        with self.assertRaises(TypeError):
            params_to_str({"notional": 12.34})
        with self.assertRaises(TypeError):
            params_to_str({"flag": True})


class SignatureTests(unittest.TestCase):
    def test_signature_is_hmac_of_method_id_key_params_nonce(self):
        # Values from the official JavaScript signing example.
        sig = sign("private/get-order-detail", 11, "token", {"order_id": "53287421324"}, 1587846358253, "secretKey")
        expected = reference_sig(
            "secretKey", "private/get-order-detail11tokenorder_id532874213241587846358253"
        )
        self.assertEqual(sig, expected)

    def test_empty_params_sign_without_param_string(self):
        sig = sign("private/user-balance", 1, "k", {}, 1000, "s")
        self.assertEqual(sig, reference_sig("s", "private/user-balance1k1000"))

    def test_signed_request_is_verifiable_and_never_contains_secret(self):
        clock = Ticker()
        session = FakeSession(FakeResponse(200, {"id": 1, "method": "private/user-balance", "code": 0,
                                                 "result": {"data": []}}))
        client = CryptoComClient("my-key", "super-secret-value", session=session, clock=clock)
        client.get_available_balance("USD")

        body = session.calls[0]["json"]
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertTrue(session.calls[0]["url"].endswith("/private/user-balance"))
        self.assertEqual(body["api_key"], "my-key")
        self.assertAlmostEqual(body["nonce"], clock.now * 1000, delta=10)
        payload = f"{body['method']}{body['id']}{body['api_key']}{params_to_str(body['params'])}{body['nonce']}"
        self.assertEqual(body["sig"], reference_sig("super-secret-value", payload))
        self.assertNotIn("super-secret-value", json.dumps(body))
        self.assertNotIn("super-secret-value", repr(client))

    def test_private_call_without_credentials_is_refused_before_sending(self):
        session = FakeSession()
        client = CryptoComClient(session=session)
        with self.assertRaises(CryptoComError):
            client.get_available_balance("USD")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
