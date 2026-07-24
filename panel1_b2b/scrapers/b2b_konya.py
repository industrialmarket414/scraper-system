"""
Konya Enerji B2B scraper.

Playwright yalnızca oturum açmak için kullanılır. Ürünler Angular tablosundan
okunmaz; sitenin kullandığı /api/ProductList servisi üzerinden alınır.
"""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

BASE_URL = "https://b2b.konyaenerji.com.tr"
PRODUCTS_URL = f"{BASE_URL}/#!/products"
FIRM_NAME = "KONYA ENERJİ"

API_BATCH_SIZE = 500
API_TIMEOUT_MS = 120_000
BRAND_RETRY_COUNT = 3
BRAND_RETRY_DELAYS_MS = (10_000, 30_000)
FAILED_BRANDS_SECOND_PASS_DELAY_MS = 60_000
BRAND_DELAY_MS = 2_500
CART_PRICE_BATCH_SIZE = 20
CART_BATCH_DELAY_MS = 300
PAYMENT_METHOD_CODE = "90"


class CartInUseError(RuntimeError):
    """İskonto hesabı sırasında kullanıcı sepeti doluysa çekimi durdurur."""

ALL_BRANDS = [
    "ENTES", "EMAS", "SCHNEİDER", "ÇETİNKAYA", "KLEMSAN",
    "LEGRAND", "PHOENIX CONTACT", "WEIDMULLER", "ORTAÇ", "SİEMENS",
    "OMRON", "OSRAM","OPAŞ", "WOER", "BEMİS", "CATA", "HES", "WAGO",
    "PHILIPS", "ACK", "BYLİON", "MUTLUSAN", "NADE", "NEAD",
    "VİKO-PANASONİC", "METE", "TELEMECANIQUE", "KRAUS NAİMER",
    "ŞAFAK", "KARDEŞ",
]


@register_scraper("konya")
class KonyaScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code", "Ürün Kodu", 20),
        ("stock", "Stok", 15),
        ("brand", "Marka", 20),
        ("list_price", "Liste Fiyatı", 16),
        ("discount_percent", "İskonto %", 12),
        ("net_price", "Net Fiyat", 16),
        ("currency", "Para Birimi", 12),
    ]

    async def scrape(self) -> list[dict]:
        brands = self.brands if self.brands else ALL_BRANDS
        products: list[dict] = []
        seen: set[tuple[str, str]] = set()
        failed: dict[str, Exception | None] = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
            )
            context: BrowserContext | None = None

            try:
                context, page, payment_method = await _open_authenticated_session(
                    browser,
                    self.username,
                    self.password,
                )

                normalized_brands = [
                    unquote(str(raw_brand)).strip()
                    for raw_brand in brands
                    if unquote(str(raw_brand)).strip()
                ]
                for brand_index, brand in enumerate(normalized_brands):
                    brand_items, last_error = await _fetch_brand_with_retries(
                        page,
                        brand,
                        payment_method,
                        self.username,
                        self.password,
                    )

                    if not brand_items:
                        failed[brand] = last_error
                        print(
                            f"[KONYA] İlk turda başarısız marka: {brand} | "
                            f"Hata: {_exception_text(last_error)}"
                        )
                    else:
                        added = _append_unique_products(
                            products,
                            seen,
                            brand,
                            brand_items,
                        )
                        print(
                            f"[KONYA] {brand}: {added} benzersiz ürün kaydedildi"
                        )

                    if brand_index < len(normalized_brands) - 1:
                        await page.wait_for_timeout(BRAND_DELAY_MS)

                if failed:
                    failed_names = list(failed)
                    print(
                        "[KONYA] Başarısız markalar temiz oturumla ikinci kez "
                        f"denenecek: {', '.join(failed_names)}"
                    )
                    await page.wait_for_timeout(
                        FAILED_BRANDS_SECOND_PASS_DELAY_MS
                    )
                    await context.close()
                    context, page, payment_method = (
                        await _open_authenticated_session(
                            browser,
                            self.username,
                            self.password,
                        )
                    )

                    second_pass_failed: dict[str, Exception | None] = {}
                    for brand_index, brand in enumerate(failed_names):
                        brand_items, last_error = await _fetch_brand_with_retries(
                            page,
                            brand,
                            payment_method,
                            self.username,
                            self.password,
                        )
                        if not brand_items:
                            second_pass_failed[brand] = last_error
                            print(
                                f"[KONYA] İkinci turda da başarısız marka: {brand} | "
                                f"Hata: {_exception_text(last_error)}"
                            )
                        else:
                            added = _append_unique_products(
                                products,
                                seen,
                                brand,
                                brand_items,
                            )
                            print(
                                f"[KONYA] İkinci tur {brand}: "
                                f"{added} benzersiz ürün kaydedildi"
                            )

                        if brand_index < len(failed_names) - 1:
                            await page.wait_for_timeout(BRAND_DELAY_MS)

                    failed = second_pass_failed
            finally:
                if context is not None:
                    await context.close()
                await browser.close()

        if not products:
            raise RuntimeError("Konya: hiçbir markadan ürün çekilemedi")

        if failed:
            details = "; ".join(
                f"{brand}: {_exception_text(error)}"
                for brand, error in failed.items()
            )
            raise RuntimeError(
                "Konya çekimi eksik kaldı; rapor oluşturulmadı. "
                f"Çekilemeyen markalar: {details}"
            )

        calculated_discounts = [
            item["discount_percent"]
            for item in products
            if item.get("discount_percent") not in (None, "")
        ]
        if calculated_discounts and not any(
            isinstance(value, (int, float)) and value > 0
            for value in calculated_discounts
        ):
            raise RuntimeError(
                "Konya sepet servisi hesaplanan bütün ürünlerde iskontoyu %0 "
                "döndürdü. Yanlış iskonto raporu oluşturmamak için çekim "
                "durduruldu; Konya hesabındaki iskonto/fiyat tanımını kontrol edin."
            )

        return products


async def _open_authenticated_session(
    browser: Browser,
    username: str,
    password: str,
) -> tuple[BrowserContext, Page, dict]:
    context = await browser.new_context(
        viewport={"width": 1600, "height": 1000},
    )
    page = await context.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    try:
        await _do_login(page, username, password)
        await page.goto(
            PRODUCTS_URL,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_function(
            "() => Boolean(window.angular && angular.element(document.body).injector())",
            timeout=30_000,
        )
        if not await _is_cart_empty(page):
            raise CartInUseError(
                "Konya sepeti boş değil. Eksiksiz iskonto hesabı için "
                "sepeti boşaltıp çekim boyunca kullanmayın."
            )
        payment_method = await _get_payment_method(
            page,
            PAYMENT_METHOD_CODE,
        )
        return context, page, payment_method
    except Exception:
        await context.close()
        raise


async def _fetch_brand_with_retries(
    page: Page,
    brand: str,
    payment_method: dict,
    username: str,
    password: str,
) -> tuple[list[dict], Exception | None]:
    last_error: Exception | None = None
    for attempt in range(1, BRAND_RETRY_COUNT + 1):
        try:
            brand_items = await _fetch_brand_api(
                page,
                brand,
                cart_pricing_enabled=True,
                payment_method=payment_method,
            )
            if brand_items:
                return brand_items, None
            last_error = RuntimeError(
                f"{brand}: {attempt}. denemede API 0 ürün döndürdü"
            )
        except Exception as exc:
            if isinstance(exc, CartInUseError):
                raise
            last_error = exc
            if "401" in str(exc) or "oturum" in str(exc).casefold():
                await _do_login(page, username, password)

        if attempt < BRAND_RETRY_COUNT:
            delay_index = min(
                attempt - 1,
                len(BRAND_RETRY_DELAYS_MS) - 1,
            )
            delay_ms = BRAND_RETRY_DELAYS_MS[delay_index]
            print(
                f"[KONYA] {brand}: {attempt}. deneme başarısız; "
                f"{delay_ms // 1000} saniye sonra tekrar denenecek | "
                f"Hata: {_exception_text(last_error)}"
            )
            await page.wait_for_timeout(delay_ms)

    return [], last_error


def _append_unique_products(
    products: list[dict],
    seen: set[tuple[str, str]],
    brand: str,
    brand_items: list[dict],
) -> int:
    added = 0
    for item in brand_items:
        key = (brand.casefold(), item["product_code"].casefold())
        if key in seen:
            continue
        seen.add(key)
        products.append(item)
        added += 1
    return added


async def _do_login(page: Page, username: str, password: str) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)

    email = page.locator("input[type='text']:visible").first
    password_input = page.locator("input[type='password']:visible").first
    await email.wait_for(state="visible", timeout=30_000)
    await email.fill(username)
    await password_input.fill(password)

    login_button = page.locator("#login")
    try:
        await login_button.click()
    except Exception:
        await login_button.evaluate("el => el.click()")

    try:
        firm = page.locator(
            f'.firmImage:has(img[title="{FIRM_NAME}"])'
        ).first
        await firm.wait_for(state="visible", timeout=30_000)
        await firm.click()

        await page.wait_for_function(
            """() => Boolean(
                window.site &&
                site.parameters &&
                site.parameters.customer &&
                !window.location.hash.toLowerCase().includes('login')
            )""",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Konya login başarısız; oturum açılamadı ({page.url})"
        ) from exc

    await _close_popup(page)


async def _close_popup(page: Page) -> None:
    selectors = [
        ".modal.show .close",
        ".modal.show [aria-label='Close']",
        ".popup .close",
        "xpath=/html/body/form/div[4]/div[2]/div[2]/div[2]/div/div/div[2]/div/div/div/div[1]/div[1]/div/img",
    ]
    for selector in selectors:
        try:
            popup = page.locator(selector).first
            if await popup.count() and await popup.is_visible():
                await popup.click(force=True)
                return
        except Exception:
            continue


async def _fetch_brand_api(
    page: Page,
    brand: str,
    cart_pricing_enabled: bool = False,
    payment_method: dict | None = None,
) -> list[dict]:
    products: list[dict] = []
    seen_codes: set[str] = set()
    skip = 0
    total: int | None = None

    while total is None or skip < total:
        data = await asyncio.wait_for(
            page.evaluate(
                """async params => {
                    const injector = window.angular.element(document.body).injector();
                    const http = injector.get('$http');
                    const response = await http.get('/api/ProductList', {params});
                    return response.data;
                }""",
                {
                    "skip": str(skip),
                    "take": str(API_BATCH_SIZE),
                    "f1": brand,
                    "categoryView": "false",
                    "_": str(int(time.time() * 1000)),
                },
            ),
            timeout=API_TIMEOUT_MS / 1000,
        )
        rows = data.get("DataList")
        if not isinstance(rows, list):
            raise RuntimeError(
                f"Konya API beklenmeyen yanıt döndürdü ({brand})"
            )

        try:
            total = int(data.get("Count", len(rows)))
        except (TypeError, ValueError):
            total = len(rows)

        if not rows:
            break

        if cart_pricing_enabled:
            await _enrich_rows_with_cart_prices(
                page,
                brand,
                rows,
                payment_method,
            )

        for row in rows:
            product = _normalize_product(row, brand)
            code = product["product_code"]
            if not code or code.casefold() in seen_codes:
                continue
            seen_codes.add(code.casefold())
            products.append(product)

        skip += len(rows)
        if len(rows) == 0:
            break

    return products


async def _is_cart_empty(page: Page) -> bool:
    data = await page.evaluate(
        """async () => {
            const injector = window.angular.element(document.body).injector();
            const http = injector.get('$http');
            const response = await http.get('/api/ShoppingCart/Get', {
                params: {
                    details: true, financialDetail: false, header: true,
                    shoppingCartHeaderId: '', simulate: true,
                    summary: false, _: Date.now().toString()
                }
            });
            return response.data;
        }"""
    )
    details_result = data.get("Details") or {}
    if isinstance(details_result, dict):
        details = details_result.get("Details") or []
    elif isinstance(details_result, list):
        details = details_result
    else:
        details = []
    return not any(
        isinstance(detail, dict) and detail.get("Type") == "Product"
        for detail in details
    )


async def _get_payment_method(page: Page, code: str) -> dict:
    data = await page.evaluate(
        """async code => {
            const injector = window.angular.element(document.body).injector();
            const http = injector.get('$http');
            const customer = site.parameters.customer || {};
            const response = await http.post('/coreWork/api/DataSource/Post', {
                DataSourceName: 'PaymentMethodSelect',
                Parameters: {
                    CustomerId: customer.CustomerId,
                    CustomerCode: customer.Code || customer.CustomerCode
                },
                Sorts: [{Field: 'SortNumber', IsDescending: false}],
                Skip: 0,
                Take: 100
            });
            const methods = response.data.DataItems || [];
            return methods.find(method => String(method.Code) === String(code)) || null;
        }""",
        code,
    )
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Konya: {code} gün ödeme yöntemi bulunamadı"
        )
    return data


async def _enrich_rows_with_cart_prices(
    page: Page,
    brand: str,
    rows: list[dict],
    payment_method: dict | None,
) -> None:
    if not payment_method:
        raise RuntimeError("Konya: iskonto için ödeme yöntemi seçilemedi")
    candidates = [row for row in rows if _can_calculate_cart_price(row)]
    for start in range(0, len(candidates), CART_PRICE_BATCH_SIZE):
        # Çekim sırasında kullanıcı sepete ürün eklediyse onun sepetine
        # dokunmadan eksik Excel üretimini engellemek için çekimi durdur.
        if not await _is_cart_empty(page):
            raise CartInUseError(
                "Konya sepetine çekim sırasında ürün eklendi. Eksik iskonto "
                "raporu oluşturmamak için çekim durduruldu."
            )
        batch = candidates[start:start + CART_PRICE_BATCH_SIZE]
        result = await asyncio.wait_for(
            page.evaluate(
                """async arg => {
                    const injector = window.angular.element(document.body).injector();
                    const http = injector.get('$http');
                    const params = {
                        skip: '0', take: '100', f1: arg.brand,
                        categoryView: 'false', pageName: 'ProductList'
                    };
                    const update = async (quantity, shoppingCartHeaderId = '') => {
                        const products = arg.products.map(product => ({
                            ...product, Quantity: quantity, dirty: true
                        }));
                        const response = await http.put('/api/ProductList', {
                            Products: products,
                            Params: params,
                            ExceptPreConfirm: '1',
                            ShoppingCartHeaderId: shoppingCartHeaderId
                        });
                        return response.data;
                    };

                    let calculatedCartDetails = [];
                    let shoppingCartHeaderId = '';
                    try {
                        const calculated = await update(1);
                        shoppingCartHeaderId =
                            calculated.Summary?.Calculation?.ShoppingCartHeaderId || '';
                        if (!shoppingCartHeaderId) {
                            throw new Error('Konya sepet kimliği oluşturulamadı');
                        }

                        const cart = (await http.get('/api/ShoppingCart/Get', {
                            params: {
                                details: true, financialDetail: true, header: true,
                                shoppingCartHeaderId, simulate: true, summary: true,
                                _: Date.now().toString()
                            }
                        })).data;
                        const header = {
                            ...cart.Header,
                            PaymentMethodId: arg.paymentMethod.PaymentMethodId,
                            PaymentMethodName: arg.paymentMethod.Name,
                            PaymentMethodCode: arg.paymentMethod.Code,
                            PaymentMethodTypeId: arg.paymentMethod.PaymentMethodTypeId,
                            PaymentMethodTypeName: arg.paymentMethod.PaymentMethodTypeName,
                            PaymentMethodTypeCode: arg.paymentMethod.PaymentMethodTypeCode
                        };
                        const priced = (await http.put(
                            '/api/ShoppingCartHeader', header
                        )).data;
                        const details = priced.Details?.Details || priced.Details || [];
                        calculatedCartDetails = details
                            .filter(detail => detail && detail.Type === 'Product')
                            .map(detail => ({...detail}));
                    } finally {
                        await update(null, shoppingCartHeaderId);
                    }

                    const check = await http.get('/api/ShoppingCart/Get', {
                        params: {
                            details: true, financialDetail: false, header: true,
                            shoppingCartHeaderId, simulate: true,
                            summary: false, _: Date.now().toString()
                        }
                    });
                    const checkDetails = check.data.Details?.Details ||
                        check.data.Details || [];
                    return {
                        cartDetails: calculatedCartDetails,
                        cartDetailsAfter: checkDetails.filter(
                            detail => detail && detail.Type === 'Product'
                        )
                    };
                }""",
                {
                    "brand": brand,
                    "products": batch,
                    "paymentMethod": payment_method,
                },
            ),
            timeout=API_TIMEOUT_MS / 1000,
        )

        batch_product_ids = {row.get("ProductId") for row in batch}
        remaining_test_ids = {
            detail.get("ProductId")
            for detail in result.get("cartDetailsAfter") or []
            if isinstance(detail, dict)
        } & batch_product_ids
        if remaining_test_ids:
            raise RuntimeError(
                "Konya iskonto hesabı sonrasında test ürünleri sepetten "
                f"kaldırılamadı ({len(remaining_test_ids)} ürün kaldı)"
            )

        by_product_id = {
            detail.get("ProductId"): detail
            for detail in result.get("cartDetails") or []
            if isinstance(detail, dict)
        }
        for row in batch:
            detail = by_product_id.get(row.get("ProductId"))
            if not detail:
                continue
            factor = detail.get("DiscountFactor")
            if isinstance(factor, (int, float)):
                row["_CartDiscountPercent"] = round((1 - factor) * 100, 2)
            row["_CartNetPrice"] = _clean_number(
                detail.get("PriceWithDiscount")
            )

        if start + CART_PRICE_BATCH_SIZE < len(candidates):
            await page.wait_for_timeout(CART_BATCH_DELAY_MS)


def _can_calculate_cart_price(row: dict) -> bool:
    if row.get("Price") in (None, "") or row.get("IsAddedToCart"):
        return False
    if row.get("AddToCartWarehouseBased"):
        return False
    if row.get("MinQuantity") not in (None, 0, 1):
        return False
    if row.get("QuantityStep") not in (None, 0, 1):
        return False
    if row.get("HasStock") is True:
        return True
    stock = row.get("Stock")
    return isinstance(stock, (int, float)) and stock > 0


def _clean_number(value):
    if isinstance(value, (int, float)):
        return round(value, 6)
    return value if value not in (None, "") else ""


def _exception_text(exc: Exception | None) -> str:
    if exc is None:
        return "Bilinmeyen hata"
    message = str(exc).strip()
    error_type = type(exc).__name__
    return f"{error_type}: {message}" if message else error_type


def _normalize_product(row: dict, brand: str) -> dict:
    code = _first_text(
        row,
        "ProductCode",
        "Code",
        "MalzemeKodu",
        "ItemCode",
    )
    stock = _first_text(
        row,
        "StockInformation",
        "Stock",
        "StockQuantity",
        "OnHandQuantity",
    )
    # Logo B2B ürün listesinde iskonto öncesi fiyat `Price` alanında gelir.
    # Bazı sürümlerde aynı değer açıkça `ListPrice` adıyla döndürülebildiği
    # için onu öncelikli alternatif olarak destekliyoruz.
    list_price = _first_value(
        row,
        "ListPrice",
        "Price",
        "SalesPrice",
        "UnitPrice",
    )
    return {
        "product_code": code,
        "stock": stock,
        "brand": _first_text(row, "BrandName", "Brand") or brand,
        "list_price": list_price,
        "discount_percent": row.get("_CartDiscountPercent", ""),
        "net_price": row.get("_CartNetPrice", ""),
        "currency": _first_text(
            row,
            "CurrencyCode",
            "CurrencySymbol",
            "CurrencyName",
            "Currency",
        ),
    }


def _first_text(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _first_value(row: dict, *keys: str):
    """İlk dolu değeri, Excel'de sayılar sayı kalacak şekilde döndürür."""
    for key in keys:
        value = row.get(key)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value.strip() if isinstance(value, str) else value
    return ""
