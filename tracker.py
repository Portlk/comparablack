import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import httpx
from bs4 import BeautifulSoup
from pymongo import MongoClient, UpdateOne


# =========================================================
# CONFIG
# =========================================================
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("ERROR: MONGO_URI no configurada.")
    sys.exit(1)

DB_NAME = os.getenv("MONGO_DB", "black_friday_sv")
TRACKER_SCOPE = os.getenv("TRACKER_SCOPE", "all").strip().lower()

# VTEX: 50 es un tamaño práctico por página.
VTEX_PAGE_SIZE = int(os.getenv("VTEX_PAGE_SIZE", "50"))
VTEX_MAX_PAGES_PER_CATEGORY = int(
    os.getenv("VTEX_MAX_PAGES_PER_CATEGORY", "50")
)

# HTML/Magento/Omnisport
HTML_MAX_PAGES = int(os.getenv("HTML_MAX_PAGES", "30"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "3"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.15"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/json,"
        "text/plain;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-SV,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
products_col = db["products"]
history_col = db["price_history"]


# =========================================================
# INDEXES
# =========================================================
def ensure_indexes():
    """
    Si ya existen duplicados viejos, Mongo puede rechazar el índice unique.
    No detenemos el tracker por eso; imprimimos el diagnóstico.
    """
    indexes = [
        (
            products_col,
            [("retailer", 1), ("sku", 1)],
            {
                "name": "uniq_retailer_sku",
                "unique": True,
            },
        ),
        (
            history_col,
            [("retailer", 1), ("sku", 1), ("date", 1)],
            {
                "name": "uniq_retailer_sku_date",
                "unique": True,
            },
        ),
        (
            products_col,
            [("comparison_key", 1)],
            {
                "name": "comparison_key_idx",
                "sparse": True,
            },
        ),
        (
            products_col,
            [("retailer", 1), ("model", 1)],
            {
                "name": "retailer_model_idx",
                "sparse": True,
            },
        ),
        (
            products_col,
            [("retailer", 1), ("ean", 1)],
            {
                "name": "retailer_ean_idx",
                "sparse": True,
            },
        ),
    ]

    for collection, keys, kwargs in indexes:
        try:
            collection.create_index(keys, **kwargs)
        except Exception as exc:
            print(
                f"[INDEX] No se pudo crear {kwargs.get('name')}: {exc}"
            )


# =========================================================
# NORMALIZATION / MATCH METADATA
# =========================================================
def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value):
    value = clean_text(value).upper()
    value = re.sub(r"[^A-Z0-9]+", "", value)
    return value


MODEL_STOPWORDS = {
    "4K",
    "8K",
    "UHD",
    "FHD",
    "HD",
    "LED",
    "OLED",
    "QLED",
    "LCD",
    "SMART",
    "TV",
    "WIFI",
    "USB",
    "HDMI",
    "BT",
    "INVERTER",
    "PRO",
    "PLUS",
    "MAX",
    "LITE",
    "ULTRA",
}


def infer_model(*values):
    """
    Intenta sacar un modelo comparable desde título/referencia.

    Priorizamos tokens alfanuméricos que mezclan letras y números:
    UN55U8000HPX, RMS510IXMRX0, LMA72215CBAB0, etc.
    """
    text = " ".join(clean_text(v) for v in values if v)
    tokens = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9._/-]{3,30}\b", text)

    candidates = []

    for token in tokens:
        normalized = normalize_key(token)

        if not normalized:
            continue

        if normalized in MODEL_STOPWORDS:
            continue

        has_letter = any(ch.isalpha() for ch in normalized)
        has_digit = any(ch.isdigit() for ch in normalized)

        if not (has_letter and has_digit):
            continue

        # Evita capacidades simples como 128GB o 55PULG.
        if re.fullmatch(r"\d+(GB|TB|MB|KG|LB|L|LT|CM|MM|HZ|W)", normalized):
            continue

        candidates.append(normalized)

    if not candidates:
        return ""

    # Normalmente el modelo real es uno de los tokens más específicos/largos.
    candidates.sort(
        key=lambda x: (
            len(x),
            sum(ch.isdigit() for ch in x),
        ),
        reverse=True,
    )

    return candidates[0]


def build_comparison_key(ean="", model="", brand=""):
    ean_key = normalize_key(ean)

    if ean_key and ean_key.isdigit() and len(ean_key) >= 8:
        return f"ean:{ean_key}"

    model_key = normalize_key(model)

    if model_key:
        brand_key = normalize_key(brand)
        if brand_key:
            return f"model:{brand_key}:{model_key}"
        return f"model:{model_key}"

    return ""


def first_reference_id(sku_item):
    refs = sku_item.get("referenceId") or []

    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict):
                value = ref.get("Value") or ref.get("value")
                if value:
                    return clean_text(value)
            elif ref:
                return clean_text(ref)

    if isinstance(refs, dict):
        return clean_text(
            refs.get("Value") or refs.get("value")
        )

    return ""


# =========================================================
# HTTP
# =========================================================
def request(
    http,
    method,
    url,
    *,
    params=None,
    expect_json=False,
    label="request",
):
    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = http.request(
                method,
                url,
                params=params,
            )

            if response.status_code == 429:
                wait = min(8, 1.5 * attempt)
                print(
                    f"[HTTP] 429 en {label}. "
                    f"Reintento en {wait:.1f}s..."
                )
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                wait = min(8, 1.5 * attempt)
                print(
                    f"[HTTP] {response.status_code} en {label}. "
                    f"Reintento en {wait:.1f}s..."
                )
                time.sleep(wait)
                continue

            if response.status_code != 200:
                return None

            if expect_json:
                try:
                    return response.json()
                except Exception as exc:
                    last_error = exc
                    print(
                        f"[HTTP] JSON inválido en {label}: {exc}"
                    )
                    time.sleep(0.5 * attempt)
                    continue

            return response

        except Exception as exc:
            last_error = exc

            if attempt < REQUEST_RETRIES:
                time.sleep(min(8, 1.25 * attempt))

    if last_error:
        print(f"[HTTP] Error final en {label}: {last_error}")

    return None


def add_query_param(url, **params):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update(
        {
            key: str(value)
            for key, value in params.items()
            if value is not None
        }
    )

    return urlunparse(
        parsed._replace(query=urlencode(query))
    )


# =========================================================
# MONGO WRITE
# =========================================================
def bulk_write_safe(collection, operations, chunk_size=500):
    total = 0

    for start in range(0, len(operations), chunk_size):
        chunk = operations[start:start + chunk_size]

        if not chunk:
            continue

        result = collection.bulk_write(
            chunk,
            ordered=False,
        )

        total += (
            result.upserted_count
            + result.modified_count
            + result.matched_count
        )

    return total


def save_batch(product_ops, history_ops, store_name, source_name):
    if product_ops:
        bulk_write_safe(products_col, product_ops)

    if history_ops:
        bulk_write_safe(history_col, history_ops)

    print(
        f"[{store_name}] {source_name}: "
        f"{len(product_ops)} productos/SKU procesados."
    )


def make_ops(
    *,
    retailer,
    sku,
    title,
    brand,
    url,
    image_url,
    category,
    regular_price,
    offer_price,
    model="",
    ean="",
    reference_id="",
    product_id="",
    stock=None,
    source="",
):
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    sku = clean_text(sku)
    title = clean_text(title)
    brand = clean_text(brand)
    model = clean_text(model) or infer_model(
        title,
        reference_id,
    )
    ean = normalize_key(ean)

    comparison_key = build_comparison_key(
        ean=ean,
        model=model,
        brand=brand,
    )

    product_doc = {
        "title": title,
        "brand": brand,
        "model": model,
        "ean": ean,
        "reference_id": clean_text(reference_id),
        "product_id": clean_text(product_id),
        "url": clean_text(url),
        "image_url": clean_text(image_url),
        "category": clean_text(category),
        "comparison_key": comparison_key,
        "regular_price": safe_float(regular_price),
        "offer_price": safe_float(offer_price),
        "last_updated": now,
        "last_seen": now,
        "source": source,
    }

    if stock is not None:
        try:
            product_doc["stock"] = int(stock)
            product_doc["available"] = int(stock) > 0
        except (TypeError, ValueError):
            pass

    product_op = UpdateOne(
        {
            "retailer": retailer,
            "sku": sku,
        },
        {
            "$set": product_doc,
            "$setOnInsert": {
                "first_seen": now,
            },
        },
        upsert=True,
    )

    history_op = UpdateOne(
        {
            "retailer": retailer,
            "sku": sku,
            "date": today_str,
        },
        {
            "$set": {
                "regular_price": safe_float(regular_price),
                "offer_price": safe_float(offer_price),
                "captured_at": now,
                "stock": (
                    int(stock)
                    if stock is not None
                    and str(stock).lstrip("-").isdigit()
                    else None
                ),
                "source": source,
            }
        },
        upsert=True,
    )

    return product_op, history_op


# =========================================================
# VTEX: SIMAN + WALMART
# =========================================================
def flatten_vtex_categories(nodes, parents=None):
    parents = parents or []
    rows = []

    for node in nodes or []:
        cat_id = str(node.get("id") or "").strip()
        name = clean_text(node.get("name"))
        children = node.get("children") or []

        path_parts = parents + ([name] if name else [])
        path = " > ".join(path_parts)

        if cat_id:
            rows.append(
                {
                    "id": cat_id,
                    "name": name,
                    "path": path,
                    "is_leaf": not bool(children),
                }
            )

        rows.extend(
            flatten_vtex_categories(
                children,
                path_parts,
            )
        )

    return rows


def get_vtex_categories(http, base_url, retailer):
    url = (
        f"{base_url}/api/catalog_system/"
        f"pub/category/tree/5"
    )

    data = request(
        http,
        "GET",
        url,
        expect_json=True,
        label=f"{retailer} category tree",
    )

    if not isinstance(data, list):
        print(
            f"[{retailer}] No se pudo leer el árbol "
            "de categorías VTEX."
        )
        return []

    categories = flatten_vtex_categories(data)

    leaf_categories = [
        cat
        for cat in categories
        if cat["is_leaf"]
    ]

    result = leaf_categories or categories

    print(
        f"[{retailer}] Árbol VTEX: "
        f"{len(categories)} categorías, "
        f"{len(leaf_categories)} hojas."
    )

    return result


def select_best_offer(sku_item):
    candidates = []

    for seller in sku_item.get("sellers") or []:
        offer = seller.get("commertialOffer") or {}
        price = safe_float(offer.get("Price"))
        list_price = safe_float(offer.get("ListPrice"))
        stock = offer.get("AvailableQuantity")

        if price <= 0:
            continue

        candidates.append(
            {
                "price": price,
                "list_price": (
                    list_price
                    if list_price > 0
                    else price
                ),
                "stock": stock,
                "seller": seller.get("sellerName")
                or seller.get("sellerId")
                or "",
            }
        )

    if not candidates:
        return None

    # Preferimos oferta con stock; si varias, menor precio.
    candidates.sort(
        key=lambda x: (
            0 if safe_float(x.get("stock"), 0) > 0 else 1,
            x["price"],
        )
    )

    return candidates[0]


def process_vtex_items(
    retailer,
    base_url,
    items,
    category_path,
    seen_skus,
):
    product_ops = []
    history_ops = []

    for product in items or []:
        product_id = clean_text(
            product.get("productId")
        )
        product_name = clean_text(
            product.get("productName")
        )
        brand = clean_text(product.get("brand"))
        link = clean_text(product.get("linkText"))

        product_url = (
            f"{base_url}/{link}/p"
            if link
            else base_url
        )

        product_reference = clean_text(
            product.get("productReference")
        )

        for sku_item in product.get("items") or []:
            sku_id = clean_text(
                sku_item.get("itemId")
                or sku_item.get("id")
                or product_id
            )

            if not sku_id:
                continue

            identity = (retailer, sku_id)

            if identity in seen_skus:
                continue

            offer = select_best_offer(sku_item)

            if not offer:
                continue

            title = clean_text(
                sku_item.get("nameComplete")
                or sku_item.get("name")
                or product_name
            )

            images = sku_item.get("images") or []
            image_url = ""

            if images:
                image_url = clean_text(
                    images[0].get("imageUrl")
                    or images[0].get("imageTag")
                )

            ean = clean_text(sku_item.get("ean"))
            reference_id = (
                first_reference_id(sku_item)
                or product_reference
            )

            model = infer_model(
                title,
                product_reference,
                reference_id,
            )

            product_op, history_op = make_ops(
                retailer=retailer,
                sku=sku_id,
                title=title,
                brand=brand,
                url=product_url,
                image_url=image_url,
                category=category_path,
                regular_price=offer["list_price"],
                offer_price=offer["price"],
                model=model,
                ean=ean,
                reference_id=reference_id,
                product_id=product_id,
                stock=offer.get("stock"),
                source="vtex",
            )

            product_ops.append(product_op)
            history_ops.append(history_op)
            seen_skus.add(identity)

    return product_ops, history_ops


def fetch_vtex_category(
    http,
    retailer,
    base_url,
    category,
    seen_skus,
):
    all_product_ops = []
    all_history_ops = []

    category_id = category["id"]
    category_path = category["path"]

    for page in range(VTEX_MAX_PAGES_PER_CATEGORY):
        start = page * VTEX_PAGE_SIZE
        end = start + VTEX_PAGE_SIZE - 1

        url = (
            f"{base_url}/api/catalog_system/"
            f"pub/products/search"
        )

        params = {
            "fq": f"C:/{category_id}/",
            "_from": start,
            "_to": end,
            "O": "OrderByNameASC",
        }

        items = request(
            http,
            "GET",
            url,
            params=params,
            expect_json=True,
            label=(
                f"{retailer} cat={category_id} "
                f"page={page + 1}"
            ),
        )

        if not isinstance(items, list):
            break

        if not items:
            break

        product_ops, history_ops = process_vtex_items(
            retailer=retailer,
            base_url=base_url,
            items=items,
            category_path=category_path,
            seen_skus=seen_skus,
        )

        all_product_ops.extend(product_ops)
        all_history_ops.extend(history_ops)

        if len(items) < VTEX_PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY)

    if all_product_ops:
        save_batch(
            all_product_ops,
            all_history_ops,
            retailer,
            category_path,
        )

    return len(all_product_ops)


def fetch_vtex_global_fallback(
    http,
    retailer,
    base_url,
    seen_skus,
):
    """
    Fallback sin palabras clave. Se usa únicamente si el árbol
    de categorías no está disponible.
    """
    total = 0

    for page in range(VTEX_MAX_PAGES_PER_CATEGORY):
        start = page * VTEX_PAGE_SIZE
        end = start + VTEX_PAGE_SIZE - 1

        url = (
            f"{base_url}/api/catalog_system/"
            f"pub/products/search"
        )

        items = request(
            http,
            "GET",
            url,
            params={
                "_from": start,
                "_to": end,
                "O": "OrderByNameASC",
            },
            expect_json=True,
            label=f"{retailer} global page={page + 1}",
        )

        if not isinstance(items, list) or not items:
            break

        product_ops, history_ops = process_vtex_items(
            retailer=retailer,
            base_url=base_url,
            items=items,
            category_path="Catálogo general",
            seen_skus=seen_skus,
        )

        if product_ops:
            save_batch(
                product_ops,
                history_ops,
                retailer,
                f"global página {page + 1}",
            )

        total += len(product_ops)

        if len(items) < VTEX_PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY)

    return total


def fetch_vtex_catalog(retailer, base_url):
    print(
        f"\n[{retailer}] Iniciando barrido VTEX "
        "por árbol de categorías..."
    )

    seen_skus = set()
    total = 0

    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers=HEADERS,
        follow_redirects=True,
    ) as http:
        categories = get_vtex_categories(
            http,
            base_url,
            retailer,
        )

        if categories:
            for index, category in enumerate(
                categories,
                start=1,
            ):
                print(
                    f"[{retailer}] Categoría "
                    f"{index}/{len(categories)}: "
                    f"{category['path']}"
                )

                total += fetch_vtex_category(
                    http=http,
                    retailer=retailer,
                    base_url=base_url,
                    category=category,
                    seen_skus=seen_skus,
                )
        else:
            print(
                f"[{retailer}] Usando barrido global "
                "de respaldo."
            )

            total = fetch_vtex_global_fallback(
                http,
                retailer,
                base_url,
                seen_skus,
            )

    print(
        f"[{retailer}] FINAL: "
        f"{len(seen_skus)} SKU únicos capturados."
    )

    return total


# =========================================================
# GENERIC HTML / MAGENTO HELPERS
# =========================================================
PRICE_RE = re.compile(
    r"(?<!\d)(?:US\$|\$)?\s*"
    r"([0-9]{1,6}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
)


def parse_price(value):
    text = clean_text(value).replace(",", "")

    matches = PRICE_RE.findall(text)

    for match in matches:
        price = safe_float(match)
        if price > 0:
            return price

    return 0.0


def image_src(img):
    if not img:
        return ""

    for attr in (
        "src",
        "data-src",
        "data-original",
        "data-lazy",
        "data-srcset",
    ):
        value = img.get(attr)

        if value:
            return clean_text(value).split(" ")[0]

    return ""


def same_host(url_a, url_b):
    return (
        urlparse(url_a).netloc.lower()
        == urlparse(url_b).netloc.lower()
    )


def discover_category_urls(
    http,
    home_url,
    *,
    markers,
    max_urls=160,
    crawl_depth=1,
):
    """
    Descubre categorías desde navegación visible.
    Esto evita depender exclusivamente de 5 palabras clave.
    """
    discovered = set()
    visited = set()
    queue = deque([(home_url, 0)])

    while queue and len(discovered) < max_urls:
        current_url, depth = queue.popleft()

        if current_url in visited:
            continue

        visited.add(current_url)

        response = request(
            http,
            "GET",
            current_url,
            label=f"discover {current_url}",
        )

        if response is None:
            continue

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        for anchor in soup.select("a[href]"):
            href = clean_text(anchor.get("href"))

            if not href:
                continue

            absolute = urljoin(current_url, href)
            parsed = urlparse(absolute)
            path = parsed.path.lower()

            if not same_host(home_url, absolute):
                continue

            if any(
                bad in path
                for bad in (
                    "/login",
                    "/account",
                    "/checkout",
                    "/cart",
                    "/contact",
                    "/blog",
                    "/p/",
                    "/productos/",
                )
            ):
                continue

            if any(marker in path for marker in markers):
                cleaned = urlunparse(
                    parsed._replace(
                        query="",
                        fragment="",
                    )
                )

                discovered.add(cleaned)

                if (
                    depth < crawl_depth
                    and cleaned not in visited
                ):
                    queue.append(
                        (cleaned, depth + 1)
                    )

        time.sleep(REQUEST_DELAY)

    return sorted(discovered)


def find_product_cards(soup):
    selectors = [
        ".product-item",
        ".item.product",
        "li.product-item",
        ".products-grid .product-item",
        ".product-card",
        "[data-product-id]",
    ]

    seen = set()
    cards = []

    for selector in selectors:
        for card in soup.select(selector):
            marker = id(card)

            if marker in seen:
                continue

            seen.add(marker)
            cards.append(card)

    return cards


def parse_html_product_card(card, base_url):
    title_elem = card.select_one(
        ".product-item-link, "
        "a.product-item-name, "
        ".product-title, "
        ".product-name a, "
        "h2 a, h3 a"
    )

    link_elem = (
        title_elem
        or card.select_one(
            "a[href*='/p'], "
            "a[href*='/producto'], "
            "a[href*='/productos/']"
        )
    )

    if not title_elem or not link_elem:
        return None

    title = clean_text(
        title_elem.get_text(" ", strip=True)
    )

    href = clean_text(link_elem.get("href"))

    if not title or not href:
        return None

    product_url = urljoin(base_url, href)

    final_price_elem = card.select_one(
        "[data-price-type='finalPrice'] .price, "
        ".special-price .price, "
        ".price-final_price .price, "
        ".price-box .price, "
        ".product-price, "
        ".price"
    )

    if not final_price_elem:
        return None

    offer_price = parse_price(
        final_price_elem.get_text(" ", strip=True)
    )

    if offer_price <= 0:
        return None

    old_price_elem = card.select_one(
        "[data-price-type='oldPrice'] .price, "
        ".old-price .price, "
        ".price-old, "
        ".regular-price .price"
    )

    regular_price = offer_price

    if old_price_elem:
        parsed_old = parse_price(
            old_price_elem.get_text(
                " ",
                strip=True,
            )
        )

        if parsed_old > 0:
            regular_price = parsed_old

    img = card.select_one(
        ".product-image-photo, "
        ".product-image img, "
        "img"
    )

    img_url = urljoin(
        base_url,
        image_src(img),
    ) if img else ""

    data_sku = (
        card.get("data-product-sku")
        or card.get("data-sku")
        or ""
    )

    path_tail = (
        urlparse(product_url)
        .path.rstrip("/")
        .split("/")[-1]
    )

    path_tail = re.sub(
        r"\.html?$",
        "",
        path_tail,
        flags=re.I,
    )

    sku = clean_text(data_sku or path_tail)

    # Muchos sitios incluyen UPC/EAN al final del URL.
    ean_match = re.search(
        r"(?:^|[-_/])(\d{8,14})(?:$|[-_/])",
        urlparse(product_url).path,
    )

    ean = (
        ean_match.group(1)
        if ean_match
        else ""
    )

    model = infer_model(title)

    brand = ""

    brand_elem = card.select_one(
        ".product-brand, "
        ".brand, "
        "[data-brand]"
    )

    if brand_elem:
        brand = clean_text(
            brand_elem.get("data-brand")
            or brand_elem.get_text(
                " ",
                strip=True,
            )
        )

    return {
        "sku": sku,
        "title": title,
        "brand": brand,
        "url": product_url,
        "image_url": img_url,
        "regular_price": regular_price,
        "offer_price": offer_price,
        "model": model,
        "ean": ean,
    }


def scan_html_listing(
    http,
    *,
    retailer,
    listing_url,
    category_name,
    page_param="p",
    seen_skus,
    max_pages=HTML_MAX_PAGES,
):
    total = 0
    empty_streak = 0

    for page in range(1, max_pages + 1):
        page_url = add_query_param(
            listing_url,
            **{page_param: page},
        )

        response = request(
            http,
            "GET",
            page_url,
            label=(
                f"{retailer} {category_name} "
                f"page={page}"
            ),
        )

        if response is None:
            break

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        cards = find_product_cards(soup)

        if not cards:
            empty_streak += 1

            if empty_streak >= 1:
                break

            continue

        product_ops = []
        history_ops = []
        new_on_page = 0

        for card in cards:
            parsed = parse_html_product_card(
                card,
                listing_url,
            )

            if not parsed:
                continue

            sku = parsed["sku"]

            if not sku:
                continue

            identity = (retailer, sku)

            if identity in seen_skus:
                continue

            product_op, history_op = make_ops(
                retailer=retailer,
                sku=sku,
                title=parsed["title"],
                brand=parsed["brand"],
                url=parsed["url"],
                image_url=parsed["image_url"],
                category=category_name,
                regular_price=parsed["regular_price"],
                offer_price=parsed["offer_price"],
                model=parsed["model"],
                ean=parsed["ean"],
                source="html",
            )

            product_ops.append(product_op)
            history_ops.append(history_op)
            seen_skus.add(identity)
            new_on_page += 1

        if product_ops:
            save_batch(
                product_ops,
                history_ops,
                retailer,
                f"{category_name} p{page}",
            )

        total += new_on_page

        # Si una página tiene cards pero ya todas eran repetidas,
        # seguimos una página más; dos seguidas no aportan valor.
        if new_on_page == 0:
            empty_streak += 1
        else:
            empty_streak = 0

        if empty_streak >= 2:
            break

        time.sleep(REQUEST_DELAY)

    return total


# =========================================================
# MAGENTO: LA CURACAO + PRADO
# =========================================================
MAGENTO_FALLBACK_TERMS = {
    "lacuracao": [
        "televisor",
        "pantalla",
        "refrigeradora",
        "lavadora",
        "secadora",
        "cocina",
        "microondas",
        "aire acondicionado",
        "celular",
        "tablet",
        "laptop",
        "computadora",
        "impresora",
        "audio",
        "barra de sonido",
        "mueble",
        "comedor",
        "sala",
        "cama",
        "colchon",
        "electrodomestico",
        "licuadora",
        "cafetera",
        "freidora",
        "moto",
        "bicicleta",
    ],
    "prado": [
        "pantalla",
        "televisor",
        "refrigeradora",
        "lavadora",
        "secadora",
        "cocina",
        "microondas",
        "aire acondicionado",
        "celular",
        "tablet",
        "laptop",
        "computadora",
        "audio",
        "mueble",
        "comedor",
        "sala",
        "cama",
        "colchon",
        "electrodomestico",
    ],
}


def fetch_magento_store(
    retailer,
    home_url,
    search_base_url,
):
    print(
        f"\n[{retailer}] Iniciando barrido "
        "de categorías HTML/Magento..."
    )

    seen_skus = set()
    total = 0

    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers=HEADERS,
        follow_redirects=True,
    ) as http:
        category_urls = discover_category_urls(
            http,
            home_url,
            markers=(
                "/c/",
                "/categoria/",
                "/category/",
                "/categorias/",
            ),
            max_urls=180,
            crawl_depth=1,
        )

        print(
            f"[{retailer}] Categorías descubiertas: "
            f"{len(category_urls)}"
        )

        for index, category_url in enumerate(
            category_urls,
            start=1,
        ):
            path = (
                urlparse(category_url)
                .path.rstrip("/")
                .split("/")[-1]
            ) or f"categoria-{index}"

            total += scan_html_listing(
                http,
                retailer=retailer,
                listing_url=category_url,
                category_name=path,
                seen_skus=seen_skus,
            )

        # Search fallback complementa categorías que la navegación
        # no dejó visibles en el HTML.
        for term in MAGENTO_FALLBACK_TERMS.get(
            retailer,
            [],
        ):
            search_url = add_query_param(
                search_base_url,
                q=term,
            )

            total += scan_html_listing(
                http,
                retailer=retailer,
                listing_url=search_url,
                category_name=f"search:{term}",
                seen_skus=seen_skus,
            )

    print(
        f"[{retailer}] FINAL: "
        f"{len(seen_skus)} productos únicos."
    )

    return total


# =========================================================
# OMNISPORT
# =========================================================
OMNISPORT_FALLBACK_CATEGORIES = [
    "televisores",
    "pantallas",
    "refrigeradoras",
    "lavadoras",
    "secadoras",
    "cocinas",
    "aires-acondicionados",
    "electrodomesticos",
    "audio",
    "celulares",
    "computacion",
    "muebles",
]


def fetch_omnisport_catalog():
    retailer = "omnisport"
    home_url = "https://www.omnisport.com"

    print(
        "\n[omnisport] Iniciando descubrimiento "
        "de categorías..."
    )

    seen_skus = set()
    total = 0

    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers=HEADERS,
        follow_redirects=True,
    ) as http:
        category_urls = discover_category_urls(
            http,
            home_url,
            markers=("/categorias/",),
            max_urls=160,
            crawl_depth=1,
        )

        if not category_urls:
            category_urls = [
                (
                    "https://www.omnisport.com/"
                    f"categorias/{slug}"
                )
                for slug in OMNISPORT_FALLBACK_CATEGORIES
            ]

        print(
            f"[omnisport] Categorías a revisar: "
            f"{len(category_urls)}"
        )

        for category_url in category_urls:
            slug = (
                urlparse(category_url)
                .path.rstrip("/")
                .split("/")[-1]
            )

            total += scan_html_listing(
                http,
                retailer=retailer,
                listing_url=category_url,
                category_name=slug,
                page_param="page",
                seen_skus=seen_skus,
            )

    print(
        f"[omnisport] FINAL: "
        f"{len(seen_skus)} productos únicos."
    )

    return total


# =========================================================
# DIAGNOSTICS
# =========================================================
def print_database_summary():
    print("\n" + "=" * 64)
    print("RESUMEN ACTUAL EN MONGODB")
    print("=" * 64)

    pipeline = [
        {
            "$group": {
                "_id": "$retailer",
                "products": {"$sum": 1},
                "last_seen": {"$max": "$last_seen"},
            }
        },
        {"$sort": {"products": -1}},
    ]

    try:
        rows = list(
            products_col.aggregate(pipeline)
        )

        for row in rows:
            print(
                f"- {row.get('_id')}: "
                f"{row.get('products', 0)} productos/SKU "
                f"| último: {row.get('last_seen')}"
            )
    except Exception as exc:
        print(
            f"No se pudo generar resumen: {exc}"
        )


# =========================================================
# RUN
# =========================================================
def run():
    print("=" * 64)
    print("ComparaBlack SV - Tracker 2.0")
    print(
        "Modo: catálogo amplio, categorías dinámicas "
        "y paginación completa"
    )
    print("=" * 64)

    ensure_indexes()

    # Siman y Walmart:
    # Ya NO dependen de cinco términos de tecnología/línea blanca.
    fetch_vtex_catalog(
        "siman",
        "https://sv.siman.com",
    )

    fetch_vtex_catalog(
        "walmart",
        "https://www.walmart.com.sv",
    )

    # La Curacao / Prado:
    # Descubrimos categorías navegables y luego complementamos
    # con búsquedas amplias.
    fetch_magento_store(
        "lacuracao",
        "https://www.lacuracaonline.com/elsalvador/",
        (
            "https://www.lacuracaonline.com/"
            "elsalvador/catalogsearch/result/"
        ),
    )

    fetch_magento_store(
        "prado",
        "https://www.prado.com.sv/",
        (
            "https://www.prado.com.sv/"
            "catalogsearch/result/"
        ),
    )

    fetch_omnisport_catalog()

    print_database_summary()

    print("\nEscaneo finalizado.")


if __name__ == "__main__":
    run()
