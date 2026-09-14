import os
import streamlit as st
import pandas as pd
import plotly.express as px
from pymongo import MongoClient

st.set_page_config(
    page_title="ComparaBlack SV",
    page_icon="🛍️",
    layout="wide"
)

# Estilos CSS personalizados
st.markdown("""
<style>
    .store-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        margin-bottom: 8px;
    }
    .badge-siman { background-color: #e53e3e; color: white; }
    .badge-walmart { background-color: #0071dc; color: #ffc220; }
    .badge-lacuracao { background-color: #d69e2e; color: #742a2a; }
    .badge-omnisport { background-color: #dd6b20; color: white; }
    .badge-prado { background-color: #38a169; color: white; }
    
    .product-price {
        font-size: 1.5rem;
        font-weight: 800;
        color: #48bb78;
        margin: 4px 0;
    }
    .old-price {
        font-size: 0.95rem;
        color: #a0aec0;
        text-decoration: line-through;
        margin-left: 8px;
    }
    .status-pill {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .pill-best { background-color: rgba(72, 187, 120, 0.2); color: #48bb78; }
    .pill-warn { background-color: rgba(245, 101, 101, 0.2); color: #f56565; }
</style>
""", unsafe_allow_html=True)

# Conexión a Mongo
MONGO_URI = os.getenv("MONGO_URI") or st.secrets.get("MONGO_URI")

@st.cache_resource
def get_database():
    client = MongoClient(MONGO_URI)
    return client["black_friday_sv"]

if not MONGO_URI:
    st.error("⚠️ MONGO_URI no configurada.")
    st.stop()

db = get_database()
products_col = db["products"]
history_col = db["price_history"]

# Header Principal
st.title("🛍️ ComparaBlack SV")
st.caption("Comparador y auditor de precios contra ofertas engañosas en Black Friday (El Salvador)")

# Barra Superior: Búsqueda, Filtro y Selector de Vista
col_search, col_store, col_view = st.columns()
with col_search:
    search_query = st.text_input("🔍 Buscar producto o modelo (ej. Samsung 55, Inverter, Mabe, LG):", "")
with col_store:
    selected_store = st.selectbox("Tienda:", ["Todas", "siman", "walmart", "lacuracao", "omnisport", "prado"])
with col_view:
    view_mode = st.radio("Vista:", ["🖼️ Mosaico", "📋 Lista"], horizontal=True)

# Métricas Bento
total_prods = products_col.count_documents({})
m1, m2, m3 = st.columns(3)
m1.metric("📦 Productos Rastreados", total_prods)
stores_count = len(products_col.distinct("retailer"))
m2.metric("🏪 Comercios Activos", stores_count)
m3.metric("🎯 Modo", "Auditoría Black Friday")

# Consulta a Base de Datos
query_filter = {}
if search_query.strip():
    query_filter["title"] = {"$regex": search_query.strip(), "$options": "i"}
if selected_store != "Todas":
    query_filter["retailer"] = selected_store

products = list(products_col.find(query_filter).limit(60))

def get_store_badge(store_name):
    s = store_name.lower()
    return f'<span class="store-badge badge-{s}">{store_name.upper()}</span>'

if not products:
    st.info("No se encontraron productos con ese término. Prueba con otro modelo o ejecuta el rastreador.")
else:
    st.write(f"Mostrando **{len(products)}** productos:")

    # ==================== VISTA 1: MOSAICO / CARDS TIPO TIENDA ====================
    if view_mode == "🖼️ Mosaico":
        cols_per_row = 3
        for i in range(0, len(products), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                idx = i + j
                if idx < len(products):
                    prod = products[idx]
                    retailer = prod.get("retailer", "tienda")
                    sku = prod.get("sku")
                    title = prod.get("title", "")
                    brand = prod.get("brand", "N/A")
                    url = prod.get("url", "#")
                    img_url = prod.get("image_url") or "https://via.placeholder.com/350x250?text=Sin+Imagen"

                    # Obtener historial
                    history = list(history_col.find({"retailer": retailer, "sku": sku}).sort("date", 1))
                    if history:
                        curr_price = history[-1].get("offer_price", 0.0)
                        reg_price = history[-1].get("regular_price", 0.0)
                        prices = [h.get("offer_price", 0.0) for h in history if h.get("offer_price", 0.0) > 0]
                        min_price = min(prices) if prices else curr_price
                    else:
                        curr_price = reg_price = min_price = 0.0

                    with cols[j]:
                        with st.container(border=True):
                            # Imagen
                            st.image(img_url, use_container_width=True)
                            
                            # Badge y Marca
                            st.markdown(get_store_badge(retailer), unsafe_allow_html=True)
                            st.caption(f"Marca: **{brand}**")
                            
                            # Título
                            disp_title = title if len(title) <= 55 else f"{title[:52]}..."
                            st.markdown(f"**{disp_title}**")

                            # Precios
                            if reg_price > curr_price:
                                st.markdown(f'<div class="product-price">${curr_price:.2f} <span class="old-price">${reg_price:.2f}</span></div>', unsafe_allow_html=True)
                            else:
                                st.markdown(f'<div class="product-price">${curr_price:.2f}</div>', unsafe_allow_html=True)

                            # Auditoría de precio
                            if len(history) > 1 and curr_price <= min_price:
                                st.markdown('<span class="status-pill pill-best">🔥 Mínimo Histórico</span>', unsafe_allow_html=True)
                            elif len(history) > 1 and reg_price > (min_price * 1.25):
                                st.markdown('<span class="status-pill pill-warn">⚠️ Inflación Previa</span>', unsafe_allow_html=True)

                            st.link_button("Ir a la Tienda ↗", url, use_container_width=True)

                            # Gráfica desplegable
                            with st.expander("📈 Ver Histórico de Precios"):
                                if len(history) > 1:
                                    df_h = pd.DataFrame(history)
                                    fig = px.line(df_h, x="date", y="offer_price", markers=True)
                                    fig.update_layout(height=180, margin=dict(l=5, r=5, t=10, b=10))
                                    st.plotly_chart(fig, use_container_width=True)
                                else:
                                    st.caption(f"1 captura registrada hoy (${curr_price:.2f}). Se irá graficando diariamente.")

    # ==================== VISTA 2: LISTA DETALLADA ====================
    else:
        for prod in products:
            retailer = prod.get("retailer", "")
            sku = prod.get("sku")
            title = prod.get("title", "")
            brand = prod.get("brand", "N/A")
            url = prod.get("url", "#")
            img_url = prod.get("image_url") or "https://via.placeholder.com/100x100?text=No+Img"

            history = list(history_col.find({"retailer": retailer, "sku": sku}).sort("date", 1))
            curr_price = history[-1].get("offer_price", 0.0) if history else 0.0
            reg_price = history[-1].get("regular_price", 0.0) if history else 0.0

            with st.container(border=True):
                c_img, c_desc, c_prc, c_act = st.columns()
                with c_img:
                    st.image(img_url, width=90)
                with c_desc:
                    st.markdown(get_store_badge(retailer), unsafe_allow_html=True)
                    st.markdown(f"**{title}**")
                    st.caption(f"Marca: {brand} | SKU: {sku}")
                with c_prc:
                    st.markdown(f'<div class="product-price">${curr_price:.2f}</div>', unsafe_allow_html=True)
                    if reg_price > curr_price:
                        st.caption(f"Antes: ~~${reg_price:.2f}~~")
                with c_act:
                    st.link_button("Comprar ↗", url, use_container_width=True)
