import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from uuid import uuid4
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
SITE_MAX_RETRY_EVENTS = int(os.getenv("SITE_MAX_RETRY_EVENTS", "5"))

# Cadencia por comercio. La Curacao es bastante más sensible
# a tráfico automatizado, así que la tratamos con más calma.
RETAILER_DELAYS = {
    "lacuracao": float(os.getenv("LACURACAO_REQUEST_DELAY", "2.0")),
    "prado": float(os.getenv("PRADO_REQUEST_DELAY", "0.65")),
    "omnisport": float(os.getenv("OMNISPORT_REQUEST_DELAY", "0.65")),
    "siman": float(os.getenv("SIMAN_REQUEST_DELAY", str(REQUEST_DELAY))),
    "walmart": float(os.getenv("WALMART_REQUEST_DELAY", str(REQUEST_DELAY))),
}

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
tracker_logs_col = db["tracker_logs"]
tracker_runs_col = db["tracker_runs"]

RUN_ID = (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    + "-"
    + uuid4().hex[:8]
)

SITE_STATE = {}



# =========================================================
# TRACKER LOGGING + CIRCUIT BREAKER
# =========================================================
def utc_now():
    return datetime.now(timezone.utc)


def get_site_state(retailer):
    retailer = str(retailer or "unknown").lower()

    if retailer not in SITE_STATE:
        SITE_STATE[retailer] = {
            "retry_events": 0,
            "consecutive_retry_events": 0,
            "failed_requests": 0,
            "blocked": False,
            "reason": "",
            "products": 0,
        }

    return SITE_STATE[retailer]


def reset_site_state(retailer):
    SITE_STATE[str(retailer).lower()] = {
        "retry_events": 0,
        "consecutive_retry_events": 0,
        "failed_requests": 0,
        "blocked": False,
        "reason": "",
        "products": 0,
    }


def log_event(
    retailer,
    level,
    event,
    message,
    *,
    status_code=None,
    context="",
    url="",
    retry_count=None,
    extra=None,
):
    retailer = str(retailer or "system").lower()

    doc = {
        "run_id": RUN_ID,
        "timestamp": utc_now(),
        "retailer": retailer,
        "level": str(level).upper(),
        "event": str(event),
        "message": str(message),
        "status_code": status_code,
        "context": str(context or ""),
        "url": str(url or ""),
        "retry_count": retry_count,
        "extra": extra or {},
    }

    status_text = (
        f" HTTP={status_code}"
        if status_code is not None
        else ""
    )

    print(
        f"[{doc['level']}] [{retailer}] "
        f"{event}{status_text}: {message}"
    )

    try:
        tracker_logs_col.insert_one(doc)
    except Exception as exc:
        # Si Mongo está caído no se puede registrar el error dentro del mismo Mongo.
        print(f"[LOG] No se pudo persistir tracker_logs: {exc}")


def update_run_store(
    retailer,
    *,
    status,
    products=None,
    reason="",
):
    state = get_site_state(retailer)

    payload = {
        f"stores.{retailer}.status": status,
        f"stores.{retailer}.retry_events": state["retry_events"],
        f"stores.{retailer}.failed_requests": state["failed_requests"],
        f"stores.{retailer}.reason": reason or state["reason"],
        f"stores.{retailer}.updated_at": utc_now(),
    }

    if products is not None:
        payload[f"stores.{retailer}.products"] = int(products)

    try:
        tracker_runs_col.update_one(
            {"run_id": RUN_ID},
            {"$set": payload},
            upsert=True,
        )
    except Exception as exc:
        print(
            f"[RUN] No se pudo actualizar estado de {retailer}: {exc}"
        )


def mark_site_success(retailer):
    state = get_site_state(retailer)
    state["consecutive_retry_events"] = 0


def is_site_blocked(retailer):
    return get_site_state(retailer)["blocked"]


def block_site(
    retailer,
    reason,
    *,
    context="",
    status_code=None,
):
    state = get_site_state(retailer)

    if state["blocked"]:
        return

    state["blocked"] = True
    state["reason"] = str(reason)

    log_event(
        retailer,
        "ERROR",
        "SITE_SKIPPED",
        (
            f"Se omite {retailer} durante el resto de esta ejecución. "
            f"Motivo: {reason}"
        ),
        status_code=status_code,
        context=context,
        retry_count=state["retry_events"],
    )

    update_run_store(
        retailer,
        status="skipped",
        products=state.get("products", 0),
        reason=reason,
    )


def register_retry(
    retailer,
    *,
    status_code=None,
    context="",
    url="",
    message="",
):
    state = get_site_state(retailer)

    state["retry_events"] += 1
    state["consecutive_retry_events"] += 1

    streak = state["consecutive_retry_events"]

    log_event(
        retailer,
        "WARNING",
        "HTTP_RETRY",
        (
            message
            or (
                f"Reintento consecutivo "
                f"{streak}/{SITE_MAX_RETRY_EVENTS}."
            )
        ),
        status_code=status_code,
        context=context,
        url=url,
        retry_count=streak,
    )

    if streak >= SITE_MAX_RETRY_EVENTS:
        block_site(
            retailer,
            (
                f"{streak} reintentos consecutivos. "
                "Circuit breaker activado para continuar "
                "con los demás comercios."
            ),
            context=context,
            status_code=status_code,
        )
        return False

    return True


def register_request_failure(
    retailer,
    *,
    context="",
    message="Request agotó sus reintentos.",
):
    state = get_site_state(retailer)
    state["failed_requests"] += 1

    log_event(
        retailer,
        "ERROR",
        "REQUEST_FAILED",
        message,
        context=context,
        retry_count=state["retry_events"],
    )


def retailer_delay(retailer):
    return RETAILER_DELAYS.get(
        str(retailer).lower(),
        REQUEST_DELAY,
    )


def polite_sleep(retailer):
    delay = retailer_delay(retailer)
    if delay > 0:
        time.sleep(delay)


def retry_after_seconds(response, attempt):
    value = response.headers.get("Retry-After")

    if value:
        try:
            return max(
                0.5,
                min(float(value), 20.0),
            )
        except (TypeError, ValueError):
            pass

    return min(12.0, 2.0 * attempt)


# =========================================================
# INDEXES
# =========================================================
def ensure_indexes():
    """
    Crea índices útiles para productos, históricos y observabilidad.
    Si existen duplicados viejos, un índice unique puede fallar;
    no detenemos el tracker por eso.
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
        (
            tracker_logs_col,
            [("timestamp", -1), ("retailer", 1)],
            {
                "name": "tracker_logs_time_store",
            },
        ),
        (
            tracker_logs_col,
            [("run_id", 1), ("level", 1)],
            {
                "name": "tracker_logs_run_level",
            },
        ),
        (
            tracker_runs_col,
            [("started_at", -1)],
            {
                "name": "tracker_runs_started",
            },
        ),
    ]

    for collection, keys, kwargs in indexes:
        try:
            collection.create_index(keys, **kwargs)
        except Exception as exc:
            print(
                f"[INDEX] No se pudo crear "
                f"{kwargs.get('name')}: {exc}"
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
    retailer="system",
    params=None,
    expect_json=False,
    label="request",
):
    retailer = str(retailer or "system").lower()

    if is_site_blocked(retailer):
        return None

    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        if is_site_blocked(retailer):
            return None

        try:
            response = http.request(
                method,
                url,
                params=params,
            )

            status = response.status_code

            if status == 429:
                wait = retry_after_seconds(
                    response,
                    attempt,
                )

                if not register_retry(
                    retailer,
                    status_code=429,
                    context=label,
                    url=str(response.url),
                    message=(
                        f"Rate limit 429 en {label}. "
                        f"Intento {attempt}/{REQUEST_RETRIES}; "
                        f"espera {wait:.1f}s."
                    ),
                ):
                    return None

                time.sleep(wait)
                continue

            if status in (401, 403):
                block_site(
                    retailer,
                    (
                        f"HTTP {status}: el comercio rechazó "
                        "el acceso del tracker."
                    ),
                    context=label,
                    status_code=status,
                )
                return None

            if 500 <= status < 600:
                wait = retry_after_seconds(
                    response,
                    attempt,
                )

                if not register_retry(
                    retailer,
                    status_code=status,
                    context=label,
                    url=str(response.url),
                    message=(
                        f"HTTP {status} en {label}. "
                        f"Intento {attempt}/{REQUEST_RETRIES}; "
                        f"espera {wait:.1f}s."
                    ),
                ):
                    return None

                time.sleep(wait)
                continue

            if status != 200:
                log_event(
                    retailer,
                    "WARNING",
                    "HTTP_NON_200",
                    f"HTTP {status} en {label}.",
                    status_code=status,
                    context=label,
                    url=str(response.url),
                )
                return None

            if expect_json:
                try:
                    data = response.json()
                except Exception as exc:
                    last_error = exc

                    if not register_retry(
                        retailer,
                        context=label,
                        url=str(response.url),
                        message=(
                            f"JSON inválido en {label}: {exc}"
                        ),
                    ):
                        return None

                    time.sleep(
                        min(4.0, 0.75 * attempt)
                    )
                    continue

                mark_site_success(retailer)
                return data

            mark_site_success(retailer)
            return response

        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            last_error = exc

            if not register_retry(
                retailer,
                context=label,
                url=url,
                message=(
                    f"Error de conexión en {label}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ):
                return None

            if attempt < REQUEST_RETRIES:
                time.sleep(
                    min(8.0, 1.5 * attempt)
                )

        except Exception as exc:
            last_error = exc

            if not register_retry(
                retailer,
                context=label,
                url=url,
                message=(
                    f"Error inesperado en {label}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ):
                return None

            if attempt < REQUEST_RETRIES:
                time.sleep(
                    min(8.0, 1.5 * attempt)
                )

    register_request_failure(
        retailer,
        context=label,
        message=(
            f"Falló {label} después de "
            f"{REQUEST_RETRIES} intentos. "
            f"Último error: "
            f"{last_error or 'respuesta no válida'}"
        ),
    )

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


def save_batch(
    product_ops,
    history_ops,
    store_name,
    source_name,
):
    try:
        if product_ops:
            bulk_write_safe(
                products_col,
                product_ops,
            )

        if history_ops:
            bulk_write_safe(
                history_col,
                history_ops,
            )

        print(
            f"[{store_name}] {source_name}: "
            f"{len(product_ops)} productos/SKU procesados."
        )

    except Exception as exc:
        log_event(
            store_name,
            "ERROR",
            "MONGO_WRITE_ERROR",
            (
                f"Error guardando lote '{source_name}': "
                f"{type(exc).__name__}: {exc}"
            ),
            context=source_name,
        )
        raise

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


def get_vtex_categories(
    http,
    base_url,
    retailer,
):
    url = (
        f"{base_url}/api/catalog_system/"
        f"pub/category/tree/5"
    )

    data = request(
        http,
        "GET",
        url,
        retailer=retailer,
        expect_json=True,
        label=f"{retailer} category tree",
    )

    if not isinstance(data, list):
        if not is_site_blocked(retailer):
            log_event(
                retailer,
                "WARNING",
                "CATEGORY_TREE_EMPTY",
                "No se pudo leer el árbol de categorías VTEX.",
                context="category tree",
            )
        return []

    categories = flatten_vtex_categories(data)

    leaf_categories = [
        cat
        for cat in categories
        if cat["is_leaf"]
    ]

    result = leaf_categories or categories

    log_event(
        retailer,
        "INFO",
        "CATEGORY_TREE_OK",
        (
            f"{len(categories)} categorías; "
            f"{len(leaf_categories)} categorías hoja."
        ),
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

    for page in range(
        VTEX_MAX_PAGES_PER_CATEGORY
    ):
        if is_site_blocked(retailer):
            break

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
            retailer=retailer,
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

        product_ops, history_ops = (
            process_vtex_items(
                retailer=retailer,
                base_url=base_url,
                items=items,
                category_path=category_path,
                seen_skus=seen_skus,
            )
        )

        all_product_ops.extend(
            product_ops
        )
        all_history_ops.extend(
            history_ops
        )

        if len(items) < VTEX_PAGE_SIZE:
            break

        polite_sleep(retailer)

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
    total = 0

    for page in range(
        VTEX_MAX_PAGES_PER_CATEGORY
    ):
        if is_site_blocked(retailer):
            break

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
            retailer=retailer,
            params={
                "_from": start,
                "_to": end,
                "O": "OrderByNameASC",
            },
            expect_json=True,
            label=(
                f"{retailer} global "
                f"page={page + 1}"
            ),
        )

        if not isinstance(items, list):
            break

        if not items:
            break

        product_ops, history_ops = (
            process_vtex_items(
                retailer=retailer,
                base_url=base_url,
                items=items,
                category_path="Catálogo general",
                seen_skus=seen_skus,
            )
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

        polite_sleep(retailer)

    return total

def fetch_vtex_catalog(
    retailer,
    base_url,
):
    reset_site_state(retailer)
    update_run_store(
        retailer,
        status="running",
    )

    log_event(
        retailer,
        "INFO",
        "SITE_START",
        "Iniciando barrido VTEX.",
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
                if is_site_blocked(retailer):
                    break

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

                get_site_state(retailer)[
                    "products"
                ] = len(seen_skus)

        elif not is_site_blocked(retailer):
            log_event(
                retailer,
                "WARNING",
                "VTEX_GLOBAL_FALLBACK",
                (
                    "Árbol de categorías no disponible; "
                    "se intenta barrido global."
                ),
            )

            total = (
                fetch_vtex_global_fallback(
                    http,
                    retailer,
                    base_url,
                    seen_skus,
                )
            )

    state = get_site_state(retailer)
    state["products"] = len(seen_skus)

    if state["blocked"]:
        update_run_store(
            retailer,
            status="skipped",
            products=len(seen_skus),
            reason=state["reason"],
        )
    else:
        log_event(
            retailer,
            "INFO",
            "SITE_COMPLETE",
            (
                f"Barrido finalizado: "
                f"{len(seen_skus)} SKU únicos."
            ),
        )

        update_run_store(
            retailer,
            status="completed",
            products=len(seen_skus),
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
    retailer,
    markers,
    max_urls=160,
    crawl_depth=1,
):
    discovered = set()
    visited = set()
    queue = deque(
        [(home_url, 0)]
    )

    while (
        queue
        and len(discovered) < max_urls
    ):
        if is_site_blocked(retailer):
            break

        current_url, depth = (
            queue.popleft()
        )

        if current_url in visited:
            continue

        visited.add(current_url)

        response = request(
            http,
            "GET",
            current_url,
            retailer=retailer,
            label=(
                f"discover {current_url}"
            ),
        )

        if response is None:
            if is_site_blocked(
                retailer
            ):
                break
            continue

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        for anchor in soup.select(
            "a[href]"
        ):
            href = clean_text(
                anchor.get("href")
            )

            if not href:
                continue

            absolute = urljoin(
                current_url,
                href,
            )

            parsed = urlparse(absolute)
            path = parsed.path.lower()

            if not same_host(
                home_url,
                absolute,
            ):
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

            if any(
                marker in path
                for marker in markers
            ):
                cleaned = urlunparse(
                    parsed._replace(
                        query="",
                        fragment="",
                    )
                )

                discovered.add(cleaned)

                if (
                    depth < crawl_depth
                    and cleaned
                    not in visited
                ):
                    queue.append(
                        (
                            cleaned,
                            depth + 1,
                        )
                    )

        polite_sleep(retailer)

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

    for page in range(
        1,
        max_pages + 1,
    ):
        if is_site_blocked(retailer):
            break

        page_url = add_query_param(
            listing_url,
            **{page_param: page},
        )

        response = request(
            http,
            "GET",
            page_url,
            retailer=retailer,
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

        cards = find_product_cards(
            soup
        )

        if not cards:
            empty_streak += 1

            if empty_streak >= 1:
                break

            continue

        product_ops = []
        history_ops = []
        new_on_page = 0

        for card in cards:
            parsed = (
                parse_html_product_card(
                    card,
                    listing_url,
                )
            )

            if not parsed:
                continue

            sku = parsed["sku"]

            if not sku:
                continue

            identity = (
                retailer,
                sku,
            )

            if identity in seen_skus:
                continue

            product_op, history_op = (
                make_ops(
                    retailer=retailer,
                    sku=sku,
                    title=parsed["title"],
                    brand=parsed["brand"],
                    url=parsed["url"],
                    image_url=parsed["image_url"],
                    category=category_name,
                    regular_price=parsed[
                        "regular_price"
                    ],
                    offer_price=parsed[
                        "offer_price"
                    ],
                    model=parsed["model"],
                    ean=parsed["ean"],
                    source="html",
                )
            )

            product_ops.append(
                product_op
            )
            history_ops.append(
                history_op
            )
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

        get_site_state(retailer)[
            "products"
        ] = len(seen_skus)

        if new_on_page == 0:
            empty_streak += 1
        else:
            empty_streak = 0

        if empty_streak >= 2:
            break

        polite_sleep(retailer)

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
    reset_site_state(retailer)
    update_run_store(
        retailer,
        status="running",
    )

    log_event(
        retailer,
        "INFO",
        "SITE_START",
        (
            "Iniciando barrido "
            "HTML/Magento."
        ),
    )

    seen_skus = set()
    total = 0

    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers=HEADERS,
        follow_redirects=True,
    ) as http:
        category_urls = (
            discover_category_urls(
                http,
                home_url,
                retailer=retailer,
                markers=(
                    "/c/",
                    "/categoria/",
                    "/category/",
                    "/categorias/",
                ),
                max_urls=180,
                crawl_depth=1,
            )
        )

        log_event(
            retailer,
            "INFO",
            "CATEGORIES_DISCOVERED",
            (
                f"{len(category_urls)} "
                "categorías descubiertas."
            ),
        )

        for index, category_url in (
            enumerate(
                category_urls,
                start=1,
            )
        ):
            if is_site_blocked(retailer):
                break

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

        # Complemento por búsqueda solamente si el sitio sigue sano.
        if not is_site_blocked(retailer):
            for term in (
                MAGENTO_FALLBACK_TERMS
                .get(retailer, [])
            ):
                if is_site_blocked(retailer):
                    break

                search_url = add_query_param(
                    search_base_url,
                    q=term,
                )

                total += (
                    scan_html_listing(
                        http,
                        retailer=retailer,
                        listing_url=search_url,
                        category_name=(
                            f"search:{term}"
                        ),
                        seen_skus=seen_skus,
                    )
                )

    state = get_site_state(retailer)
    state["products"] = len(seen_skus)

    if state["blocked"]:
        update_run_store(
            retailer,
            status="skipped",
            products=len(seen_skus),
            reason=state["reason"],
        )
    else:
        log_event(
            retailer,
            "INFO",
            "SITE_COMPLETE",
            (
                f"Barrido finalizado: "
                f"{len(seen_skus)} "
                "productos únicos."
            ),
        )

        update_run_store(
            retailer,
            status="completed",
            products=len(seen_skus),
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
    home_url = (
        "https://www.omnisport.com"
    )

    reset_site_state(retailer)
    update_run_store(
        retailer,
        status="running",
    )

    log_event(
        retailer,
        "INFO",
        "SITE_START",
        (
            "Iniciando descubrimiento "
            "de categorías Omnisport."
        ),
    )

    seen_skus = set()
    total = 0

    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers=HEADERS,
        follow_redirects=True,
    ) as http:
        category_urls = (
            discover_category_urls(
                http,
                home_url,
                retailer=retailer,
                markers=(
                    "/categorias/",
                ),
                max_urls=160,
                crawl_depth=1,
            )
        )

        if (
            not category_urls
            and not is_site_blocked(
                retailer
            )
        ):
            category_urls = [
                (
                    "https://www.omnisport.com/"
                    f"categorias/{slug}"
                )
                for slug
                in OMNISPORT_FALLBACK_CATEGORIES
            ]

        for category_url in (
            category_urls
        ):
            if is_site_blocked(retailer):
                break

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

    state = get_site_state(retailer)
    state["products"] = len(seen_skus)

    if state["blocked"]:
        update_run_store(
            retailer,
            status="skipped",
            products=len(seen_skus),
            reason=state["reason"],
        )
    else:
        log_event(
            retailer,
            "INFO",
            "SITE_COMPLETE",
            (
                f"Barrido finalizado: "
                f"{len(seen_skus)} "
                "productos únicos."
            ),
        )

        update_run_store(
            retailer,
            status="completed",
            products=len(seen_skus),
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
def execute_store(
    retailer,
    func,
):
    """
    Un error inesperado de una tienda no detiene las demás.
    """
    try:
        return func()

    except Exception as exc:
        state = get_site_state(retailer)
        state["reason"] = (
            f"{type(exc).__name__}: {exc}"
        )

        log_event(
            retailer,
            "ERROR",
            "STORE_UNHANDLED_ERROR",
            (
                f"Error no controlado: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

        update_run_store(
            retailer,
            status="failed",
            products=state.get(
                "products",
                0,
            ),
            reason=state["reason"],
        )

        return 0


def run():
    started_at = utc_now()

    print("=" * 64)
    print(
        "ComparaBlack SV - "
        "Tracker 3.0"
    )
    print(
        "Catálogo amplio + circuit breaker "
        "+ logs persistentes"
    )
    print(
        f"Run ID: {RUN_ID}"
    )
    print(
        "Máximo de reintentos consecutivos "
        f"por sitio: {SITE_MAX_RETRY_EVENTS}"
    )
    print("=" * 64)

    try:
        tracker_runs_col.update_one(
            {"run_id": RUN_ID},
            {
                "$set": {
                    "run_id": RUN_ID,
                    "started_at": started_at,
                    "status": "running",
                    "max_retry_events": (
                        SITE_MAX_RETRY_EVENTS
                    ),
                }
            },
            upsert=True,
        )
    except Exception as exc:
        print(
            f"[RUN] No se pudo crear "
            f"tracker_runs: {exc}"
        )

    ensure_indexes()

    stores = [
        (
            "siman",
            lambda: fetch_vtex_catalog(
                "siman",
                "https://sv.siman.com",
            ),
        ),
        (
            "walmart",
            lambda: fetch_vtex_catalog(
                "walmart",
                "https://www.walmart.com.sv",
            ),
        ),
        (
            "lacuracao",
            lambda: fetch_magento_store(
                "lacuracao",
                (
                    "https://www.lacuracaonline.com/"
                    "elsalvador/"
                ),
                (
                    "https://www.lacuracaonline.com/"
                    "elsalvador/catalogsearch/result/"
                ),
            ),
        ),
        (
            "prado",
            lambda: fetch_magento_store(
                "prado",
                "https://www.prado.com.sv/",
                (
                    "https://www.prado.com.sv/"
                    "catalogsearch/result/"
                ),
            ),
        ),
        (
            "omnisport",
            fetch_omnisport_catalog,
        ),
    ]

    for retailer, func in stores:
        execute_store(
            retailer,
            func,
        )

    print_database_summary()

    finished_at = utc_now()

    skipped = [
        retailer
        for retailer, state
        in SITE_STATE.items()
        if state.get("blocked")
    ]

    failed = []

    try:
        run_doc = (
            tracker_runs_col.find_one(
                {"run_id": RUN_ID}
            )
            or {}
        )

        for retailer, info in (
            run_doc.get(
                "stores",
                {},
            )
        ).items():
            if (
                info.get("status")
                == "failed"
            ):
                failed.append(retailer)
    except Exception:
        pass

    final_status = (
        "partial"
        if skipped or failed
        else "completed"
    )

    try:
        tracker_runs_col.update_one(
            {"run_id": RUN_ID},
            {
                "$set": {
                    "finished_at": finished_at,
                    "status": final_status,
                    "skipped_stores": skipped,
                    "failed_stores": failed,
                }
            },
        )
    except Exception as exc:
        print(
            f"[RUN] No se pudo cerrar "
            f"tracker_runs: {exc}"
        )

    log_event(
        "system",
        "INFO",
        "RUN_COMPLETE",
        (
            f"Ejecución {final_status}. "
            f"Omitidos: "
            f"{', '.join(skipped) if skipped else 'ninguno'}. "
            f"Fallidos: "
            f"{', '.join(failed) if failed else 'ninguno'}."
        ),
    )

    print(
        f"\nEscaneo finalizado: "
        f"{final_status}."
    )

