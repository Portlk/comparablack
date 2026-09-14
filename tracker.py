import os
import sys
from datetime import datetime, timezone, timedelta
import httpx
from pymongo import MongoClient, UpdateOne

# 1. Variables de Entorno
MONGO_URI = os.getenv("MONGO_URI")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not MONGO_URI:
    print("ERROR: MONGO_URI no está configurado.")
    sys.exit(1)

# Conexión a Mongo
client = MongoClient(MONGO_URI)
db = client["black_friday_sv"]
products_col = db["products"]
history_col = db["price_history"]

def send_telegram_alert(message: str):
    """Envía notificaciones directas a Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        with httpx.Client(timeout=10) as http:
            http.post(url, json=payload)
    except Exception as e:
        print(f"Error enviando alerta a Telegram: {e}")

def audit_price_and_notify(retailer: str, sku: str, title: str, current_price: float, regular_price: float, url: str):
    """Audita si es una rebaja real o inflación previa."""
    now = datetime.now(timezone.utc)
    threshold_date = now - timedelta(days=45)

    recent_records = list(history_col.find({
        "retailer": retailer,
        "sku": sku,
        "captured_at": {"$gte": threshold_date}
    }).sort("captured_at", 1))

    if not recent_records:
        return

    past_prices = [r["offer_price"] for r in recent_records if r.get("offer_price", 0) > 0]
    if not past_prices:
        return

    min_historical = min(past_prices)
    price_15_days_ago = recent_records[0]["offer_price"]

    # Caso 1: Oferta Real (>10% por debajo del mínimo de 45 días)
    if current_price < (min_historical * 0.90):
        drop_pct = round(((min_historical - current_price) / min_historical) * 100, 1)
        msg = (
            f"🚨 *¡OFERTA REAL DETECTADA!*\n\n"
            f"🛒 *Tienda:* {retailer.upper()}\n"
            f"📦 *Producto:* {title}\n"
            f"💵 *Precio Actual:* ${current_price:.2f}\n"
            f"📉 *Mínimo anterior (45d):* ${min_historical:.2f}\n"
            f"🔥 *Ahorro real:* {drop_pct}%\n\n"
            f"[Ver producto]({url})"
        )
        send_telegram_alert(msg)

    # Caso 2: Inflación Artificial previa a Black Friday
    elif regular_price > (price_15_days_ago * 1.25) and current_price >= price_15_days_ago:
        msg = (
            f"⚠️ *INFLACIÓN ARTIFICIAL DETECTADA*\n\n"
            f"🛒 *Tienda:* {retailer.upper()}\n"
            f"📦 *Producto:* {title}\n"
            f"💵 *Precio venta:* ${current_price:.2f}\n"
            f"📈 *Precio tachado inflado:* ${regular_price:.2f} (antes rondaba ${price_15_days_ago:.2f})\n\n"
            f"[Ver producto]({url})"
        )
        send_telegram_alert(msg)

def fetch_vtex_store(retailer: str, base_url: str, category_slug: str, max_pages: int = 3):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    total_found = 0
    product_ops = []
    history_ops = []
    
    with httpx.Client(timeout=25, headers=headers, follow_redirects=True) as http:
        for page in range(1, max_pages + 1):
            _from = (page - 1) * 30
            _to = (page * 30) - 1
            
            # Intento 1: Búsqueda directa por ruta de categoría
            endpoint = f"{base_url}/api/catalog_system/pub/products/search/{category_slug}?_from={_from}&_to={_to}"
            
            try:
                resp = http.get(endpoint)
                items = resp.json() if resp.status_code == 200 else []
                
                # Intento 2 (Fallback): Búsqueda full-text si la ruta directa vino vacía
                if not items:
                    alt_endpoint = f"{base_url}/api/catalog_system/pub/products/search?ft={category_slug}&_from={_from}&_to={_to}"
                    alt_resp = http.get(alt_endpoint)
                    if alt_resp.status_code == 200:
                        items = alt_resp.json()

                if not items:
                    print(f"[{retailer}] Sin más resultados para '{category_slug}' en pág {page}.")
                    break

                print(f"[{retailer}] Página {page}: obtenidos {len(items)} productos para '{category_slug}'.")

                for item in items:
                    sku = str(item.get("productId", ""))
                    title = item.get("productName", "")
                    brand = item.get("brand", "")
                    link = item.get("linkText", "")
                    url = f"{base_url}/{link}/p" if link else base_url

                    # Precios del primer SKU disponible
                    item_skus = item.get("items", [])
                    if not item_skus:
                        continue
                    sellers = item_skus[0].get("sellers", [])
                    if not sellers:
                        continue
                        
                    comm_offer = sellers[0].get("commertialOffer", {})
                    regular_price = float(comm_offer.get("ListPrice", 0.0))
                    offer_price = float(comm_offer.get("Price", 0.0))

                    if offer_price <= 0:
                        continue

                    total_found += 1
                    audit_price_and_notify(retailer, sku, title, offer_price, regular_price, url)

                    product_ops.append(
                        UpdateOne(
                            {"retailer": retailer, "sku": sku},
                            {"$set": {
                                "title": title,
                                "brand": brand,
                                "url": url,
                                "category": category_slug,
                                "last_updated": datetime.now(timezone.utc)
                            }},
                            upsert=True
                        )
                    )

                    history_ops.append(
                        UpdateOne(
                            {"retailer": retailer, "sku": sku, "date": today_str},
                            {"$set": {
                                "regular_price": regular_price,
                                "offer_price": offer_price,
                                "captured_at": datetime.now(timezone.utc)
                            }},
                            upsert=True
                        )
                    )

            except Exception as e:
                print(f"[{retailer}] Error en {category_slug} pág {page}: {e}")
                break

    if product_ops:
        products_col.bulk_write(product_ops)
    if history_ops:
        history_col.bulk_write(history_ops)
        
    print(f"[{retailer}] Total guardados: {total_found} en '{category_slug}'.")

def run():
    print("Iniciando escaneo de precios...")

    # Tiendas y categorías clave en El Salvador
    targets = [
        # Siman SV
        ("siman", "https://sv.siman.com", "tecnologia", 3),
        ("siman", "https://sv.siman.com", "linea-blanca", 3),
        ("siman", "https://sv.siman.com", "climatizacion", 2),
        
        # La Curacao SV
        ("lacuracao", "https://www.lacuracaonline.com/elsalvador", "refrigeradoras", 2),
        ("lacuracao", "https://www.lacuracaonline.com/elsalvador", "televisores", 2),
        ("lacuracao", "https://www.lacuracaonline.com/elsalvador", "celulares", 2),
        ("lacuracao", "https://www.lacuracaonline.com/elsalvador", "aires acondicionados", 2),
    ]

    for retailer, base_url, cat, pages in targets:
        fetch_vtex_store(retailer, base_url, cat, max_pages=pages)

    print("Escaneo finalizado correctamente.")

if __name__ == "__main__":
    run()
