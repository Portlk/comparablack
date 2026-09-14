import os
import sys
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from pymongo import MongoClient, UpdateOne

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    print("ERROR: MONGO_URI no configurado.")
    sys.exit(1)

client = MongoClient(MONGO_URI)
db = client["black_friday_sv"]
products_col = db["products"]
history_col = db["price_history"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html"
}

def save_batch(product_ops, history_ops, store_name, category):
    if product_ops:
        products_col.bulk_write(product_ops)
    if history_ops:
        history_col.bulk_write(history_ops)
    print(f"[{store_name}] Guardados {len(product_ops)} productos en '{category}'.")

# 1. Extractor para tiendas VTEX (Siman, La Curacao, Walmart SV)
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

                    skus = item.get("items", [])
                    if not skus or not skus[0].get("sellers"):
                        continue
                    comm = skus[0]["sellers"][0].get("commertialOffer", {})
                    reg_price = float(comm.get("ListPrice", 0.0))
                    off_price = float(comm.get("Price", 0.0))

                    if off_price <= 0:
                        continue

                    prod_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku},
                        {"$set": {"title": title, "brand": brand, "url": product_url, "category": query, "last_updated": datetime.now(timezone.utc)}},
                        upsert=True
                    ))
                    hist_ops.append(UpdateOne(
                        {"retailer": retailer, "sku": sku, "date": today_str},
                        {"$set": {"regular_price": reg_price, "offer_price": off_price, "captured_at": datetime.now(timezone.utc)}},
                        upsert=True
                    ))
            except Exception as e:
                print(f"[{retailer}] Error en {query}: {e}")
                break

    save_batch(prod_ops, hist_ops, retailer, query)

# 2. Extractor para Omnisport
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
                cards = soup.select(".product-card, .card-product, div[data-product-id]")
                if not cards:
                    break

                for card in cards:
                    title_elem = card.select_one(".product-title, .title, h3, a")
                    price_elem = card.select_one(".price, .current-price, .special-price")
                    link_elem = card.select_one("a[href*='/productos/']")

                    if not title_elem or not price_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    price_text = price_elem.get_text(strip=True).replace("$", "").replace(",", "")
                    try:
                        price = float(price_text)
                    except ValueError:
                        continue

                    prod_url = link_elem["href"] if link_elem else "https://www.omnisport.com"
                    if not prod_url.startswith("http"):
                        prod_url = f"https://www.omnisport.com{prod_url}"

                    sku = prod_url.split("/")[-1]

                    prod_ops.append(UpdateOne(
                        {"retailer": "omnisport", "sku": sku},
                        {"$set": {"title": title, "url": prod_url, "category": category_slug, "last_updated": datetime.now(timezone.utc)}},
                        upsert=True
                    ))
                    hist_ops.append(UpdateOne(
                        {"retailer": "omnisport", "sku": sku, "date": today_str},
                        {"$set": {"offer_price": price, "regular_price": price, "captured_at": datetime.now(timezone.utc)}},
                        upsert=True
                    ))
            except Exception as e:
                print(f"[omnisport] Error en {category_slug}: {e}")
                break

    save_batch(prod_ops, hist_ops, "omnisport", category_slug)

def run():
    print("Iniciando escaneo multi-tienda en El Salvador...")

    # Siman SV
    for cat in ["tecnologia", "linea-blanca", "climatizacion"]:
        fetch_vtex("siman", "https://sv.siman.com", cat, max_pages=2)

    # La Curacao SV
    for cat in ["televisores", "refrigeradoras", "lavadoras", "celulares", "aires acondicionados"]:
        fetch_vtex("lacuracao", "https://www.lacuracaonline.com/elsalvador", cat, max_pages=2)

    # Walmart El Salvador
    for cat in ["televisores", "linea-blanca", "celulares", "electrodomesticos"]:
        fetch_vtex("walmart", "https://www.walmart.com.sv", cat, max_pages=2)

    # Omnisport
    for cat in ["lavadoras", "electrodomesticos", "aires-acondicionados"]:
        fetch_omnisport(cat, max_pages=2)

    print("Escaneo multi-tienda completado.")

if __name__ == "__main__":
    run()
