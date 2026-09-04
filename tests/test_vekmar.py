import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import requests
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from panel1_b2b.scrapers import b2b_vekmar as vekmar


def encrypted_response(rows):
    iv = bytes(range(16))
    key = PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=32, salt=vekmar._API_SALT, iterations=10_000
    ).derive(vekmar._API_PASSPHRASE)
    padder = padding.PKCS7(128).padder()
    plain = json.dumps(rows).encode()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return SimpleNamespace(
        status_code=200,
        json=lambda: [base64.b64encode(iv + ciphertext).decode()],
    )


class VekmarApiTests(unittest.TestCase):
    def test_requested_brands_are_in_default_vekmar_list(self):
        self.assertEqual(
            vekmar.DEFAULT_BRANDS,
            ["SCHNEIDER", "HİMEL", "KLEMSAN", "METE ENERJİ", "SIEMENS", "ŞAFAK"],
        )

    def test_encrypted_product_pages_are_decoded_and_paginated(self):
        session = Mock()
        session.get.side_effect = [
            encrypted_response([
                {"ITEMS_CODE": "P1", "ITEMS_NAME": "Bir", "MARK_DESCR": "SCHNEIDER",
                 "GNTOTST_REALSTOCK": 37, "GENELLISTEFIYATI": 12.5,
                 "GENELLISTEFIYATI_PB": "EUR", "DISC_RATE": 0,
                 "STOCK_OUT_DISC_RATE": 57},
                {"ITEMS_CODE": "P2", "ITEMS_NAME": "İki", "MARK_DESCR": "SCHNEIDER",
                 "GNTOTST_REALSTOCK": -4, "GENELLISTEFIYATI": 20,
                 "GENELLISTEFIYATI_PB": "TRY", "DISC_RATE": 63,
                 "STOCK_OUT_DISC_RATE": 57},
            ]),
            encrypted_response([
                {"ITEMS_CODE": "P3", "ITEMS_NAME": "Üç", "MARK_DESCR": "SCHNEIDER",
                 "GNTOTST_REALSTOCK": 4.5, "GENELLISTEFIYATI": 3,
                 "GENELLISTEFIYATI_PB": "USD"},
            ]),
        ]
        user = {
            "USERS_LOGICALREF": 1,
            "USERS_CLCARDLOGICALREF": 2,
            "CUST_GROUP": "A",
        }

        products = vekmar._fetch_brand(session, user, "SCHNEIDER", page_size=2)

        self.assertEqual([p["product_code"] for p in products], ["P1", "P2", "P3"])
        self.assertEqual([p["stock"] for p in products], ["37", "0", "4.5"])
        self.assertEqual([p["discount_percent"] for p in products], [57, 63, ""])
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["skip"], 0)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["skip"], 2)

    def test_repeated_page_stops_instead_of_creating_duplicates_forever(self):
        page = encrypted_response([{"ITEMS_CODE": "P1", "GNTOTST_REALSTOCK": 1}])
        session = Mock()
        session.get.side_effect = [page, page]
        user = {
            "USERS_LOGICALREF": 1,
            "USERS_CLCARDLOGICALREF": 2,
            "CUST_GROUP": "A",
        }

        with self.assertRaisesRegex(RuntimeError, "aynı ürün sayfasını"):
            vekmar._fetch_brand(session, user, "SCHNEIDER", page_size=1)

    def test_campaign_price_and_calculated_discount_apply_only_to_campaign_rows(self):
        campaign = vekmar._map_product({
            "ITEMS_CODE": "CAMPAIGN",
            "GENELLISTEFIYATI": 199,
            "GENELLISTEFIYATI_PB": "EUR",
            "KAMPANYA_ADI": "KAMPANYA 1",
            "KAMPANYA_FIYATI": 51.52,
            "KAMPANYA_PB": "EUR",
            "DISC_RATE": 0,
            "STOCK_OUT_DISC_RATE": 0,
        }, "SCHNEIDER")
        regular = vekmar._map_product({
            "ITEMS_CODE": "REGULAR",
            "GENELLISTEFIYATI": 199,
            "GENELLISTEFIYATI_PB": "EUR",
            "KAMPANYA_ADI": "",
            "KAMPANYA_FIYATI": 0,
            "KAMPANYA_PB": "",
            "DISC_RATE": 0,
            "STOCK_OUT_DISC_RATE": 57,
        }, "SCHNEIDER")

        self.assertEqual(campaign["price_text"], "51.52")
        self.assertEqual(campaign["discount_percent"], 74.11)
        self.assertEqual(campaign["campaign_name"], "KAMPANYA 1")
        self.assertEqual(regular["price_text"], "199")
        self.assertEqual(regular["discount_percent"], 57)
        self.assertEqual(regular["campaign_name"], "")

    def test_campaign_with_no_comparable_list_price_has_no_calculated_discount(self):
        product = vekmar._map_product({
            "ITEMS_CODE": "CAMPAIGN",
            "GENELLISTEFIYATI": 0,
            "GENELLISTEFIYATI_PB": "",
            "KAMPANYA_ADI": "KAMPANYA 1",
            "KAMPANYA_FIYATI": 40.82,
            "KAMPANYA_PB": "EUR",
            "STOCK_OUT_DISC_RATE": 0,
        }, "SCHNEIDER")

        self.assertEqual(product["price_text"], "40.82")
        self.assertEqual(product["currency"], "EUR")
        self.assertEqual(product["discount_percent"], "")

    def test_login_network_error_does_not_expose_password(self):
        session = Mock()
        secret = "very-secret-password"
        session.get.side_effect = requests.RequestException(f"URL contains {secret}")

        with self.assertRaises(RuntimeError) as caught:
            vekmar._login(session, "user", secret)

        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
