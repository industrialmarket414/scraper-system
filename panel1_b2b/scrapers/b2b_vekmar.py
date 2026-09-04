"""Vekmar B2B ürünlerini sitenin kullandığı JSON API üzerinden çeker.

Tarayıcı açılmaz. Ürünler küçük sayfalar halinde alınır ve API yanıtındaki
gerçek stok ile liste fiyatı doğrudan rapora aktarılır.
"""

import asyncio
import base64
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable

import requests
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core.log_manager import add_log
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper


BASE_URL = "https://b2b.vekmar.com.tr"
API_URL = f"{BASE_URL}:9080/Api"
DEFAULT_BRANDS = [
    "SCHNEIDER",
    "HİMEL",
    "KLEMSAN",
    "METE ENERJİ",
    "SIEMENS",
    "ŞAFAK",
]
PAGE_SIZE = 1_000
REQUEST_TIMEOUT = (10, 60)

# Vekmar'ın web uygulamasındaki app.config.js ile aynı çözme ayarlarıdır.
_API_PASSPHRASE = b"C0zBakal1m"
_API_SALT = b"INF_B2B_SALT_2025"


@register_scraper("vekmar")
class VekmarScraper(BaseB2BScraper):
    COLUMNS = [
        ("brand", "Marka", 18),
        ("product_code", "Ürün Kodu", 22),
        ("product_name", "Açıklama", 40),
        ("stock", "Stok", 10),
        ("price_text", "Geçerli Fiyat", 14),
        ("currency", "Para Br.", 8),
        ("campaign_name", "Kampanya", 18),
        ("discount_percent", "İskonto %", 12),
    ]

    async def scrape(self) -> list[dict]:
        brands = self.brands or DEFAULT_BRANDS

        def progress(message: str) -> None:
            print(f"[Vekmar] {message}", flush=True)
            add_log(
                "panel1", self.site_id, self.site_name, "info", message,
                notify=False,
            )

        return await asyncio.to_thread(
            _scrape_api,
            self.username,
            self.password,
            brands,
            progress,
        )


def _scrape_api(
    username: str,
    password: str,
    brands: list[str],
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    products: list[dict] = []
    with requests.Session() as session:
        user = _login(session, username, password)
        session.headers.update({
            "USERREF": str(user["USERS_LOGICALREF"]),
            "TOKEN": str(user["TOKEN"]),
        })

        if progress:
            progress("API girişi tamamlandı; ürünler okunuyor")

        for brand in brands:
            products.extend(_fetch_brand(session, user, brand, progress))

    return products


def _login(session: requests.Session, username: str, password: str) -> dict:
    if not username or not password:
        raise RuntimeError("Vekmar kullanıcı adı veya şifresi eksik")

    try:
        response = session.get(
            f"{API_URL}/USERS",
            params={"USERCODE": username, "PASSWORD": password},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Vekmar API giriş bağlantısı kurulamadı") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Vekmar API girişi HTTP {response.status_code} döndürdü")

    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Vekmar API giriş yanıtı okunamadı") from exc

    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Vekmar girişi reddedildi; kullanıcı adı ve şifreyi kontrol edin")

    user = payload[0]
    required = ("TOKEN", "USERS_LOGICALREF", "USERS_CLCARDLOGICALREF", "CUST_GROUP")
    if not isinstance(user, dict) or any(user.get(field) in (None, "") for field in required):
        raise RuntimeError("Vekmar API giriş yanıtında gerekli oturum bilgileri yok")
    return user


def _fetch_brand(
    session: requests.Session,
    user: dict,
    brand: str,
    progress: Callable[[str], None] | None = None,
    *,
    page_size: int = PAGE_SIZE,
    max_products: int | None = None,
) -> list[dict]:
    products: list[dict] = []
    seen_pages: set[tuple[str, int]] = set()
    skip = 0

    while True:
        take = min(page_size, max_products - len(products)) if max_products else page_size
        if take <= 0:
            break

        params = {
            "FirmNr": 322,
            "PeriodNr": 1,
            "MOD": 90,
            "USERREF": user["USERS_LOGICALREF"],
            "filter": "",
            "sort": "",
            "group": "",
            "skip": skip,
            "take": take,
            "CUST_GROUP": user["CUST_GROUP"],
            "CLIENTREF": user["USERS_CLCARDLOGICALREF"],
            "PRCLIST_GRPCODE": "",
            "MARK_CODE": brand,
        }

        try:
            response = session.get(
                f"{API_URL}/LG_ITEMS3", params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Vekmar API {brand} ürün bağlantısı kurulamadı") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Vekmar API {brand} ürünleri için HTTP {response.status_code} döndürdü"
            )

        rows = _decode_product_response(response)
        if not rows:
            break

        first_code = str(rows[0].get("ITEMS_CODE", "")).strip()
        page_marker = (first_code, len(rows))
        if page_marker in seen_pages:
            raise RuntimeError(f"Vekmar API {brand} için aynı ürün sayfasını tekrarladı")
        seen_pages.add(page_marker)

        products.extend(_map_product(row, brand) for row in rows)
        if progress:
            progress(f"{brand}: {len(products)} ürün okundu")

        if len(rows) < take:
            break
        skip += len(rows)

    return products


def _decode_product_response(response: requests.Response) -> list[dict]:
    try:
        envelope = response.json()
        if not isinstance(envelope, list) or not envelope or not isinstance(envelope[0], str):
            raise ValueError("beklenmeyen zarf")
        raw = base64.b64decode(envelope[0], validate=True)
        if len(raw) <= 16:
            raise ValueError("kısa şifreli veri")

        iv, ciphertext = raw[:16], raw[16:]
        key = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=32,
            salt=_API_SALT,
            iterations=10_000,
        ).derive(_API_PASSPHRASE)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded_data = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded_data) + unpadder.finalize()
        rows = json.loads(plain.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Vekmar API ürün yanıtı çözülemedi") from exc

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Vekmar API ürün yanıtının biçimi beklenenden farklı")
    return rows


def _map_product(row: dict, requested_brand: str) -> dict:
    code = str(row.get("ITEMS_CODE") or "").strip()
    name = str(row.get("ITEMS_NAME") or "").strip()
    if not code:
        raise RuntimeError("Vekmar API yanıtında ürün kodu eksik")

    normal_discount = _number_or_blank(row.get("DISC_RATE"))
    term_discount = _number_or_blank(row.get("STOCK_OUT_DISC_RATE"))
    list_price = row.get("GENELLISTEFIYATI")
    list_currency = str(row.get("GENELLISTEFIYATI_PB") or "").strip()
    campaign_name = str(row.get("KAMPANYA_ADI") or "").strip()
    campaign_price = row.get("KAMPANYA_FIYATI")
    campaign_currency = str(row.get("KAMPANYA_PB") or list_currency).strip()
    has_campaign = bool(campaign_name) and _is_positive_number(campaign_price)

    if has_campaign:
        price = campaign_price
        currency = campaign_currency
        discount = _campaign_discount(
            list_price, campaign_price, list_currency, campaign_currency
        )
    else:
        price = list_price
        currency = list_currency
        discount = normal_discount if normal_discount not in ("", 0) else term_discount

    return {
        "brand": str(row.get("MARK_DESCR") or requested_brand).strip(),
        "product_code": code,
        "product_name": name,
        "stock": _format_stock(row.get("GNTOTST_REALSTOCK")),
        "price_text": _format_number(price),
        "currency": currency,
        "campaign_name": campaign_name,
        "discount_percent": discount,
    }


def _format_number(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return str(value).strip()
    if number == number.to_integral():
        return str(int(number))
    return format(number.normalize(), "f")


def _format_stock(value: object) -> str:
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return _format_number(value)
    return _format_number(max(number, Decimal(0)))


def _number_or_blank(value: object) -> int | float | str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return ""
    if number == number.to_integral():
        return int(number)
    return float(number)


def _is_positive_number(value: object) -> bool:
    try:
        return Decimal(str(value).replace(",", ".")) > 0
    except (InvalidOperation, ValueError):
        return False


def _campaign_discount(
    list_price: object,
    campaign_price: object,
    list_currency: str,
    campaign_currency: str,
) -> int | float | str:
    if not list_currency or list_currency.upper() != campaign_currency.upper():
        return ""
    try:
        listed = Decimal(str(list_price).replace(",", "."))
        campaign = Decimal(str(campaign_price).replace(",", "."))
    except (InvalidOperation, ValueError):
        return ""
    if listed <= 0 or campaign <= 0 or campaign > listed:
        return ""
    percent = ((listed - campaign) / listed * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return _number_or_blank(percent)
