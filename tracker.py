import os
import sys
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from pymongo import MongoClient, UpdateOne

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    print("ERROR: MONGO_URI no configurada.")
    sys.exit(1)

client = MongoClient(MONGO_URI)
db = client["black_friday_sv"]
products_col = db["products"]
history_col = db["price_history"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json,application/xhtml+xml"
}

def save_batch(product_ops, history_ops, store_name, category):
    if product_ops:
        products_col.bulk_write(product_ops)
    if history_ops:
        history_col.bulk_write(history_ops)
    print(f"[{store_name}] Guardados {len(product_ops)} productos con fotos en '{category}'.")

# 1. Extractor VTEX (Siman y Walmart SV) con extracción de imágenes
def fetch_vtex(retailer: str, base_url: str, query: str, max_pages: int = 2):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prod_ops, hist_ops = [], []

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as http:
        for page in range(1, max_pages + 1):
            _from = (page - 1) * 30
            _to = (page * 30) - 1
            url = f"{base_url}/api/catalog_system/pub/products/search/{query}?_from={_from}&_to={_to}"

            try:
                resp = http.get(url)
                items = resp.json() if resp.status_code == 200 else []
                if not items:
                    alt_url = f"{base_url}/api/catalog_system/pub/products/search?ft={query}&_from={_from}&_to={_to}"
                    alt_resp = http.get(alt_url)
                    if alt_resp.status_code == 200:
                        items = alt_resp.json()

                if not items:
                    break

                for item in items:
                    sku = str(item.get("productId", ""))
                    title = item.get("productName", "")
                    brand = item.get("brand", "")
                    link = item.get("linkText", "")
                    product_url = f"{base_url}/{link}/p" if link else base_url

                    # Extraer imagen principal de alta resolución
                    image_url = ""
                    skus = item.get("items", [])
                    if skus and skus[0].get("images"):
                        image_url = skus[0]["images"][0].get("imageUrl", "")

                    if not skus or not skus[0].get("sellers"):
                        continue
                    comm = skus[0]["sellers"][0].get("commertialOffer", {})
                    reg_price = float(comm.get("ListPrice", 0.0))
                    off_price = float(comm.get("Price", 0.0))

                    if off_price <= 0:
                        continue

                    prod_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku},
                        {"$set": {
                            "title": title,
                            "brand": brand,
                            "url": product_url,
                            "image_url": image_url,
                            "category": query,
                            "last_updated": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
                    hist_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku, "date": today_str},
                        {"$set": {
                            "regular_price": reg_price,
                            "offer_price": off_price,
                            "captured_at": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
            except Exception as e:
                print(f"[{retailer}] Error en {query}: {e}")
                break

    save_batch(prod_ops, hist_ops, retailer, query)

# 2. Extractor Magento (La Curacao SV y Prado)
def fetch_magento(retailer: str, search_base_url: str, query: str, max_pages: int = 2):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prod_ops, hist_ops = [], []

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as http:
        for page in range(1, max_pages + 1):
            url = f"{search_base_url}?q={query}&p={page}"
            try:
                resp = http.get(url)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select(".product-item, .item.product")
                if not items:
                    break

                for item in items:
                    title_elem = item.select_one(".product-item-link, a.product-item-name")
                    price_elem = item.select_one("[data-price-type='finalPrice'] .price, .special-price .price, .price")
                    img_elem = item.select_one(".product-image-photo, img")

                    if not title_elem or not price_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    prod_url = title_elem.get("href", "")
                    img_url = img_elem.get("src", "") if img_elem else ""

                    p_clean = price_elem.get_text(strip=True).replace("$", "").replace(",", "")
                    try:
                        price = float(p_clean)
                    except ValueError:
                        continue

                    old_price_elem = item.select_one("[data-price-type='oldPrice'] .price, .old-price .price")
                    reg_price = price
                    if old_price_elem:
                        try:
                            reg_price = float(old_price_elem.get_text(strip=True).replace("$", "").replace(",", ""))
                        except ValueError:
                            pass

                    sku = prod_url.split("/")[-1].replace(".html", "")

                    prod_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku},
                        {"$set": {
                            "title": title,
                            "url": prod_url,
                            "image_url": img_url,
                            "category": query,
                            "last_updated": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
                    hist_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku, "date": today_str},
                        {"$set": {
                            "regular_price": reg_price,
                            "offer_price": price,
                            "captured_at": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
            except Exception as e:
                print(f"[{retailer}] Error en {query}: {e}")
                break

    save_batch(prod_ops, hist_ops, retailer, query)

# 3. Extractor Omnisport
def fetch_omnisport(category_slug: str, max_pages: int = 2):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prod_ops, hist_ops = [], []

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as http:
        for page in range(1, max_pages + 1):
            url = f"https://www.omnisport.com/categorias/{category_slug}?page={page}"
            try:
                resp = http.get(url)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select(".product-card, div[data-product-id], .card")
                if not cards:
                    break

                for card in cards:
                    title_elem = card.select_one(".product-title, h3, a")
                    price_elem = card.select_one(".price, .special-price")
                    link_elem = card.select_one("a[href*='/productos/']")
                    img_elem = card.select_one("img[src*='buketomnisportpweb'], img")

                    if not title_elem or not price_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    p_str = price_elem.get_text(strip=True).replace("$", "").replace(",", "")
                    try:
                        price = float(p_str)
                    except ValueError:
                        continue

                    prod_url = link_elem["href"] if link_elem else "https://www.omnisport.com"
                    if not prod_url.startswith("http"):
                        prod_url = f"https://www.omnisport.com{prod_url}"

                    img_url = img_elem.get("src", "") if img_elem else ""
                    sku = prod_url.split("/")[-1]

                    prod_ops.append(UpdateOne(
                        {"retailer": "omnisport", "sku": sku},
                        {"$set": {
                            "title": title,
                            "url": prod_url,
                            "image_url": img_url,
                            "category": category_slug,
                            "last_updated": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
                    hist_ops.append(UpdateOne(
                        {"retailer": "omnisport", "sku": sku, "date": today_str},
                        {"$set": {
                            "regular_price": price,
                            "offer_price": price,
                            "captured_at": datetime.now(timezone.utc)
                        }},
                        upsert=True
                    ))
            except Exception as e:
                print(f"[omnisport] Error en {category_slug}: {e}")
                break

    save_batch(prod_ops, hist_ops, "omnisport", category_slug)

def run():
    print("Iniciando escaneo multi-tienda en El Salvador...")

    # 1. Siman SV (VTEX)
    for cat in ["televisores", "refrigeradoras", "lavadoras", "celulares", "climatizacion"]:
        fetch_vtex("siman", "https://sv.siman.com", cat, max_pages=2)

    # 2. Walmart El Salvador (VTEX)
    for cat in ["televisores", "refrigeradoras", "lavadoras", "celulares", "aires-acondicionados"]:
        fetch_vtex("walmart", "https://www.walmart.com.sv", cat, max_pages=2)

    # 3. La Curacao El Salvador (Magento)
    for term in ["televisor", "refrigeradora", "lavadora", "celular", "aire acondicionado"]:
        fetch_magento("lacuracao", "https://www.lacuracaonline.com/elsalvador/catalogsearch/result/", term, max_pages=2)

    # 4. Prado El Salvador (Magento)
    for term in ["pantalla", "refrigeradora", "lavadora", "celular", "aire"]:
        fetch_magento("prado", "https://www.prado.com.sv/catalogsearch/result/", term, max_pages=2)

    # 5. Omnisport
    for cat in ["lavadoras", "electrodomesticos", "aires-acondicionados"]:
        fetch_omnisport(cat, max_pages=2)

    print("Escaneo finalizado.")

if __name__ == "__main__":
    run()
