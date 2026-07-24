"""
Meanwell (Koçtaş Teknoloji) B2B scraper.

Akış:
  1. ASP.NET giriş formuna kullanıcı adı ve şifre ile giriş yap.
  2. Sevkiyat ekranında teslim şeklini Kargo, kargoyu Yurtiçi seç.
  3. Power Supply, Aydınlatma, Laser ve Otomasyon gruplarını sırayla tara.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import threading
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from panel1_b2b.scrapers import BaseB2BScraper, register_scraper


DEFAULT_BASE_URL = "http://78.188.151.72:2520"
LOGIN_PATH = "/Account/Login.aspx"
CHECKPOINT_PATH = "/CheckPoint/"
SEARCH_PATH = "/Ajax/Search.aspx"
PRODUCT_DETAIL_PATH = "/Ajax/Search.aspx/GetProduct"

DELIVERY_TYPE_CARGO = "TS04"
YURTICI_CARGO = "K002"
PRODUCT_GROUPS = (
    ("01", "Power Supply"),
    ("02", "Aydınlatma"),
    ("03", "Laser"),
    ("04", "Otomasyon"),
)
PROCESS_BATCH_SIZE = 100
DETAIL_WORKERS = 8
DETAIL_RETRY_WORKERS = 2
DETAIL_RETRY_ATTEMPTS = 2
DETAIL_RETRY_DELAY = 0.5
REQUEST_TIMEOUT = 45


@register_scraper("meanwell")
class MeanWellScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code", "Model No", 22),
        ("product_name", "Ürün Açıklama", 42),
        ("category", "Grup", 18),
        ("stock", "Stok", 12),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("list_price_text", "Liste Fiyat", 15),
        ("price_text", "Müşteri Fiyat", 15),
        ("currency", "Cinsi", 9),
        ("discount_text", "İskonto", 12),
        ("campaign", "Kampanya", 16),
    ]

    async def scrape(self) -> list[dict]:
        # requests senkron olduğu için event loop'u bloke etmeden ayrı thread'de çalıştır.
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        base_url = (self.url or DEFAULT_BASE_URL).rstrip("/")

        with requests.Session() as session:
            session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                )
            })

            checkpoint_html = _login(
                session, base_url, self.username, self.password
            )
            _select_shipping(session, base_url, checkpoint_html)

            products: list[dict] = []
            seen_codes: set[str] = set()

            for group_code, group_name in PRODUCT_GROUPS:
                rows = _fetch_group(session, base_url, group_code)
                details = _fetch_product_details(session, base_url, rows)

                # Çok büyük grupları kontrollü biçimde 100'erli parçalar halinde işle.
                for start in range(0, len(rows), PROCESS_BATCH_SIZE):
                    batch = rows[start:start + PROCESS_BATCH_SIZE]
                    batch_details = details[start:start + PROCESS_BATCH_SIZE]
                    for row, detail in zip(batch, batch_details):
                        if detail:
                            row = {**row, **detail}
                        product = _normalize_product(row, group_name)
                        unique_code = (
                            product.get("internal_code")
                            or product.get("product_code")
                        )
                        if unique_code and unique_code in seen_codes:
                            continue
                        if unique_code:
                            seen_codes.add(unique_code)
                        products.append(product)

            return products


def _fetch_group(
    session: requests.Session,
    base_url: str,
    group_code: str,
) -> list[dict]:
    response = session.get(
        urljoin(base_url + "/", SEARCH_PATH.lstrip("/")),
        params={
            "i": "Search",
            "mk": "",
            "ma": "",
            "ang": group_code,
            "arg": "",
            "alg": "",
            "sr": "",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()

    if not isinstance(rows, list):
        raise RuntimeError(
            f"Meanwell ürün servisi {group_code} grubu için beklenmeyen veri döndürdü."
        )
    return rows


def _fetch_product_details(
    session: requests.Session,
    base_url: str,
    rows: list[dict],
) -> list[dict]:
    """Detaylar servisinden gerçek iskonto ve satış parti büyüklüğünü al.

    Servis yoğunlukta tekil istekleri zaman zaman boş/hatalı döndürebiliyor.
    İlk paralel turda eksik kalan ürünleri daha düşük eşzamanlılıkla yeniden
    dener; iskonto hâlâ eksikse sessizce hatalı Excel üretmek yerine çekimi
    başarısız sayar.
    """
    cookies = requests.utils.dict_from_cookiejar(session.cookies)
    headers = dict(session.headers)
    thread_local = threading.local()
    worker_sessions: list[requests.Session] = []
    sessions_lock = threading.Lock()

    def get_worker_session() -> requests.Session:
        worker_session = getattr(thread_local, "session", None)
        if worker_session is None:
            worker_session = requests.Session()
            worker_session.headers.update(headers)
            worker_session.cookies.update(cookies)
            thread_local.session = worker_session
            with sessions_lock:
                worker_sessions.append(worker_session)
        return worker_session

    def fetch(row: dict) -> dict:
        mal_kod = _text(row.get("MalKod"))
        if not mal_kod:
            return {}
        try:
            return _fetch_product_detail(
                get_worker_session(), base_url, mal_kod
            )
        except (requests.RequestException, ValueError, TypeError):
            return {}

    def missing_detail_indexes(details: list[dict]) -> list[int]:
        return [
            index
            for index, (row, detail) in enumerate(zip(rows, details))
            if _text(row.get("MalKod"))
            and not _text(detail.get("IskontoDeger"))
        ]

    details: list[dict] = [{} for _ in rows]

    try:
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
            details = list(executor.map(fetch, rows))

        missing_indexes = missing_detail_indexes(details)
        for attempt in range(1, DETAIL_RETRY_ATTEMPTS + 1):
            if not missing_indexes:
                break
            time.sleep(DETAIL_RETRY_DELAY * attempt)
            retry_rows = [rows[index] for index in missing_indexes]
            with ThreadPoolExecutor(
                max_workers=min(DETAIL_RETRY_WORKERS, len(retry_rows))
            ) as executor:
                retry_details = list(executor.map(fetch, retry_rows))
            for index, detail in zip(missing_indexes, retry_details):
                if _text(detail.get("IskontoDeger")):
                    details[index] = detail
            missing_indexes = missing_detail_indexes(details)

        if missing_indexes:
            missing_codes = [
                _text(rows[index].get("IthalatKod"))
                or _text(rows[index].get("MalKod"))
                for index in missing_indexes[:10]
            ]
            raise RuntimeError(
                "Meanwell detay servisi "
                f"{len(missing_indexes)} ürünün iskonto bilgisini "
                f"{DETAIL_RETRY_ATTEMPTS + 1} denemede döndürmedi. "
                "Eksik Excel oluşturulmadı. Örnek ürünler: "
                + ", ".join(missing_codes)
            )

        return details
    finally:
        for worker_session in worker_sessions:
            worker_session.close()


def _fetch_product_detail(
    session: requests.Session,
    base_url: str,
    mal_kod: str,
) -> dict:
    response = session.post(
        urljoin(base_url + "/", PRODUCT_DETAIL_PATH.lstrip("/")),
        json={"MalKod": mal_kod},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    details = payload.get("d", []) if isinstance(payload, dict) else []
    if not isinstance(details, list) or not details:
        return {}
    detail = details[0]
    if not isinstance(detail, dict):
        return {}
    return {
        "IskontoDeger": detail.get("IskontoDeger"),
        "SatisBuyukluk": detail.get("SatisBuyukluk"),
    }


def _login(
    session: requests.Session,
    base_url: str,
    username: str,
    password: str,
) -> str:
    login_url = urljoin(base_url + "/", LOGIN_PATH.lstrip("/"))
    response = session.get(login_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    payload = _hidden_fields(response.text)
    payload.update({
        "txtUserName": username,
        "txtPass": password,
        "btnLogin": "Giriş",
    })

    response = session.post(
        login_url,
        data=payload,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    if "/checkpoint" not in response.url.lower():
        raise RuntimeError("Meanwell girişi başarısız; kullanıcı adı veya şifreyi kontrol edin.")

    return response.text


def _select_shipping(
    session: requests.Session,
    base_url: str,
    checkpoint_html: str,
) -> None:
    soup = BeautifulSoup(checkpoint_html, "lxml")
    cargo_select = soup.select_one("#DrpKargo")
    cargo_values = {
        option.get("value", "") for option in cargo_select.select("option")
    } if cargo_select else set()

    if YURTICI_CARGO not in cargo_values:
        raise RuntimeError("Meanwell sevkiyat ekranında Yurtiçi Kargo seçeneği bulunamadı.")

    payload = _hidden_fields(checkpoint_html)
    payload.update({
        "DrpYetkili": _selected_value(soup, "DrpYetkili"),
        "DrpSevkYer": _selected_value(soup, "DrpSevkYer"),
        "DrpTeslimSekli": DELIVERY_TYPE_CARGO,
        "DrpKargo": YURTICI_CARGO,
        "btnNextStep": "Devam >>",
    })

    checkpoint_url = urljoin(base_url + "/", CHECKPOINT_PATH.lstrip("/"))
    response = session.post(
        checkpoint_url,
        data=payload,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    if response.url.rstrip("/").lower().endswith("/checkpoint"):
        raise RuntimeError("Meanwell Yurtiçi Kargo seçimi tamamlanamadı.")


def _hidden_fields(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    return {
        element["name"]: element.get("value", "")
        for element in soup.select('input[type="hidden"][name]')
    }


def _selected_value(soup: BeautifulSoup, select_id: str) -> str:
    select = soup.select_one(f"#{select_id}")
    if not select:
        raise RuntimeError(f"Meanwell sevkiyat alanı bulunamadı: {select_id}")

    option = select.select_one("option[selected]")
    if option is None:
        option = next(
            (item for item in select.select("option") if item.get("value")),
            None,
        )
    if option is None:
        raise RuntimeError(f"Meanwell sevkiyat alanı boş: {select_id}")
    return option.get("value", "")


def _normalize_product(row: dict, category: str) -> dict:
    list_price = _text(row.get("ListeFiyat"))
    customer_price = _text(row.get("Fiyat"))
    discount = _text(row.get("IskontoDeger"))

    return {
        "product_code": _text(row.get("IthalatKod")),
        "product_name": _text(row.get("MalAd")),
        "brand": "Mean Well",
        "category": category,
        "stock": _number_text(row.get("StokMiktar")),
        "min_package_qty": _number_text(row.get("SatisBuyukluk")),
        "list_price_text": list_price,
        "price_text": customer_price,
        "price": _parse_decimal(customer_price),
        "currency": _text(row.get("DovizCins")),
        "discount_text": discount,
        "discount_percent": _parse_decimal(discount),
        "campaign": _text(row.get("KampanyaVarMi")),
        "internal_code": _text(row.get("MalKod")),
    }


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _number_text(value) -> str:
    text = _text(value)
    try:
        number = Decimal(text.replace(",", "."))
        return str(int(number)) if number == number.to_integral() else str(number)
    except (InvalidOperation, ValueError):
        return text


def _parse_decimal(value: str) -> float | None:
    number = _decimal_value(value)
    return float(number) if number is not None else None


def _decimal_value(value: str) -> Decimal | None:
    text = _text(value).replace("%", "").strip()
    if not text:
        return None
    try:
        # Servis fiyatları "29,4000" biçiminde döndürüyor.
        normalized = (
            text.replace(".", "").replace(",", ".")
            if "," in text
            else text
        )
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
