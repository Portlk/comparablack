import os
import streamlit as st
import pandas as pd
import plotly.express as px
from pymongo import MongoClient

st.set_page_config(
    page_title="ComparaBlack SV - Comparador de Precios",
    page_icon="🛒",
    layout="wide"
)

# Estilo visual
st.markdown("""
<style>
    .metric-card {
        background-color: #1e232a;
        padding: 18px;
        border-radius: 12px;
        border: 1px solid #2d3748;
        margin-bottom: 12px;
    }
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

# Header
st.title("🛒 ComparaBlack SV")
st.caption("Rastreador y auditor histórico de precios para evitar ofertas falsas en Black Friday (El Salvador)")

# Barra superior: Búsqueda y Filtros
col_search, col_cat = st.columns()
with col_search:
    search_query = st.text_input("🔍 Buscar producto o modelo (ej. Samsung 55, Mabe, Inverter, LG OLED):", "")
with col_cat:
    selected_retailer = st.selectbox("Filtrar por tienda:", ["Todas", "siman", "lacuracao", "walmart", "omnisport", "prado"])

# Métricas generales
col_m1, col_m2, col_m3 = st.columns(3)
total_prods = products_col.count_documents({})
col_m1.metric("📦 Productos Monitoreados", total_prods)

# Construir query para búsqueda
query_filter = {}
if search_query.strip():
    query_filter["title"] = {"$regex": search_query.strip(), "$options": "i"}
if selected_retailer != "Todas":
    query_filter["retailer"] = selected_retailer

# Obtener productos coincidentes
products = list(products_col.find(query_filter).limit(60))

if not products:
    st.info("No se encontraron productos con el criterio de búsqueda. Prueba con otro término o espera a que el rastreador complete más tiendas.")
else:
    st.subheader(f"Resultados encontrados ({len(products)})")

    for prod in products:
        retailer = prod.get("retailer", "").upper()
        sku = prod.get("sku")
        title = prod.get("title", "")
        url = prod.get("url", "#")
        brand = prod.get("brand", "N/A")

        # Buscar historial de precios
        history = list(history_col.find({"retailer": prod.get("retailer"), "sku": sku}).sort("date", 1))

        if history:
            current_price = history[-1].get("offer_price", 0.0)
            regular_price = history[-1].get("regular_price", 0.0)
            prices = [h.get("offer_price", 0.0) for h in history if h.get("offer_price", 0.0) > 0]
            min_price = min(prices) if prices else current_price
        else:
            current_price = regular_price = min_price = 0.0

        with st.expander(f"**[{retailer}]** {title} — **${current_price:.2f}**", expanded=False):
            col_info, col_chart = st.columns(2)

            with col_info:
                st.markdown(f"**Marca:** {brand}")
                st.markdown(f"**Precio Actual:** `${current_price:.2f}`")
                if regular_price > current_price:
                    st.markdown(f"**Precio Regular/Tachado:** ~~`${regular_price:.2f}`~~")
                st.markdown(f"**Mínimo Histórico Registrado:** `${min_price:.2f}`")
                
                # Evaluación antifraude
                if current_price <= min_price and len(history) > 1:
                    st.success("✅ Precio en su punto más bajo registrado.")
                elif regular_price > (min_price * 1.25):
                    st.warning("⚠️ Posible inflación previa de precio regular.")

                st.markdown(f"[🔗 Ver en {retailer}]({url})")

            with col_chart:
                if len(history) > 1:
                    df_hist = pd.DataFrame(history)
                    fig = px.line(
                        df_hist,
                        x="date",
                        y="offer_price",
                        title="Evolución Histórica del Precio",
                        markers=True,
                        labels={"date": "Fecha", "offer_price": "Precio ($ USD)"}
                    )
                    fig.update_layout(height=240, margin=dict(l=20, r=20, t=30, b=20))
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.caption("Solo se cuenta con un punto de captura por el momento. La gráfica se activará con los escaneos diarios.")
