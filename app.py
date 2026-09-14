import os
import re
from collections import defaultdict

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from pymongo import MongoClient


# =========================================================
# CONFIG
# =========================================================
st.set_page_config(
    page_title="ComparaBlack SV",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

STORES = {
    "siman": {"label": "Siman", "class": "siman"},
    "walmart": {"label": "Walmart", "class": "walmart"},
    "lacuracao": {"label": "La Curacao", "class": "lacuracao"},
    "omnisport": {"label": "Omnisport", "class": "omnisport"},
    "prado": {"label": "Prado", "class": "prado"},
}

SORT_OPTIONS = {
    "Mejor oportunidad": "score",
    "Menor precio": "price_asc",
    "Mayor descuento": "discount",
    "Más reciente": "recent",
}


# =========================================================
# STYLES
# =========================================================
st.markdown(
    """
    <style>
        :root {
            --bg: #f7f8fa;
            --surface: #ffffff;
            --surface-2: #f1f3f5;
            --text: #111827;
            --muted: #6b7280;
            --line: #e5e7eb;
            --success: #15803d;
            --warning: #b45309;
            --danger: #b91c1c;
            --accent: #111827;
        }

        .stApp { background: var(--bg); }

        .block-container {
            max-width: 1500px;
            padding-top: 1.4rem;
            padding-bottom: 3rem;
        }

        #MainMenu, footer { visibility: hidden; }

        h1, h2, h3 { letter-spacing: -0.025em; }

        .hero { padding: 1.15rem 0 1.35rem; }

        .eyebrow {
            display: inline-flex;
            align-items: center;
            gap: .4rem;
            color: #374151;
            background: #eef0f3;
            border: 1px solid #e5e7eb;
            border-radius: 999px;
            padding: .35rem .65rem;
            font-size: .78rem;
            font-weight: 700;
            margin-bottom: .8rem;
        }

        .hero h1 {
            font-size: clamp(2rem, 4vw, 3.45rem);
            line-height: 1.02;
            margin: 0;
            color: var(--text);
        }

        .hero p {
            max-width: 760px;
            color: var(--muted);
            font-size: 1rem;
            margin: .7rem 0 0;
        }

        .section-title {
            font-size: 1.18rem;
            font-weight: 800;
            margin: .35rem 0 .15rem;
            color: var(--text);
        }

        .section-copy {
            color: var(--muted);
            font-size: .9rem;
            margin-bottom: .9rem;
        }

        .store-badge {
            display: inline-flex;
            align-items: center;
            width: fit-content;
            padding: .28rem .55rem;
            border-radius: 999px;
            font-size: .7rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .04em;
            border: 1px solid transparent;
            margin-bottom: .45rem;
        }

        .badge-siman { background: #fff1f2; color: #be123c; border-color: #fecdd3; }
        .badge-walmart { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
        .badge-lacuracao { background: #fffbeb; color: #a16207; border-color: #fde68a; }
        .badge-omnisport { background: #fff7ed; color: #c2410c; border-color: #fed7aa; }
        .badge-prado { background: #f0fdf4; color: #15803d; border-color: #bbf7d0; }
        .badge-default { background: #f3f4f6; color: #374151; border-color: #e5e7eb; }

        .price {
            font-size: 1.65rem;
            line-height: 1.15;
            font-weight: 850;
            color: var(--text);
            margin: .2rem 0;
        }

        .old-price {
            color: #9ca3af;
            text-decoration: line-through;
            font-size: .92rem;
            font-weight: 600;
            margin-left: .35rem;
        }

        .discount {
            display: inline-flex;
            padding: .2rem .45rem;
            border-radius: 999px;
            background: #ecfdf5;
            color: #047857;
            font-weight: 800;
            font-size: .72rem;
            margin-left: .3rem;
        }

        .status {
            display: inline-flex;
            align-items: center;
            gap: .3rem;
            border-radius: 999px;
            padding: .28rem .55rem;
            font-size: .72rem;
            font-weight: 800;
            margin-top: .4rem;
        }

        .status-best {
            color: #166534;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
        }

        .status-good {
            color: #1d4ed8;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
        }

        .status-warn {
            color: #92400e;
            background: #fffbeb;
            border: 1px solid #fde68a;
        }

        .status-neutral {
            color: #4b5563;
            background: #f9fafb;
            border: 1px solid #e5e7eb;
        }

        .monitor { margin-top: .8rem; }

        .monitor-labels {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            color: #6b7280;
            font-size: .7rem;
            margin-bottom: .28rem;
        }

        .monitor-track {
            height: 8px;
            border-radius: 999px;
            background: #e5e7eb;
            overflow: hidden;
        }

        .monitor-fill {
            height: 100%;
            border-radius: 999px;
            background: linear-gradient(90deg, #16a34a, #f59e0b, #dc2626);
        }

        .monitor-marker-wrap {
            position: relative;
            height: 11px;
            margin-top: -9px;
        }

        .monitor-marker {
            position: absolute;
            top: -1px;
            width: 3px;
            height: 14px;
            background: #111827;
            border-radius: 999px;
            transform: translateX(-50%);
        }

        .mini-note {
            color: #6b7280;
            font-size: .76rem;
            margin-top: .45rem;
        }

        div[data-testid="stMetric"] {
            background: white;
            border: 1px solid var(--line);
            padding: 1rem 1.05rem;
            border-radius: 16px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
        }

        div[data-testid="stMetricLabel"] { color: #6b7280; }

        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: white;
            border-radius: 18px;
            border-color: var(--line) !important;
            box-shadow: 0 1px 2px rgba(0,0,0,.025);
        }

        .stButton button, .stLinkButton a {
            border-radius: 10px !important;
            font-weight: 750 !important;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: .35rem;
            background: white;
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: .3rem;
        }

        .stTabs [data-baseweb="tab"] {
            border-radius: 10px;
            padding-left: 1rem;
            padding-right: 1rem;
        }

        .stTabs [aria-selected="true"] {
            background: #111827 !important;
            color: white !important;
        }

        @media (max-width: 900px) {
            .block-container {
                padding-left: 1rem;
                padding-right: 1rem;
            }

            .hero h1 { font-size: 2.2rem; }
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# SAFE HTML RENDERER
# =========================================================
def render_html(markup: str):
    """Render UI HTML without letting Markdown treat indentation as code."""
    if hasattr(st, "html"):
        st.html(markup)
        return

    # Fallback for older Streamlit versions. Compacting the markup prevents
    # indented HTML lines from becoming Markdown code blocks.
    compact = " ".join(line.strip() for line in markup.splitlines() if line.strip())
    st.markdown(compact, unsafe_allow_html=True)


# =========================================================
# DATABASE
# =========================================================
def get_mongo_uri():
    uri = os.getenv("MONGO_URI")
    if uri:
        return uri

    try:
        return st.secrets["MONGO_URI"]
    except Exception:
        return None


MONGO_URI = get_mongo_uri()

if not MONGO_URI:
    st.error(
        "MONGO_URI no está configurada. "
        "Agrégala a variables de entorno o a `.streamlit/secrets.toml`."
    )
    st.stop()


@st.cache_resource
def get_database(uri: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client["black_friday_sv"]


try:
    db = get_database(MONGO_URI)
except Exception as exc:
    st.error(f"No fue posible conectar a MongoDB: {exc}")
    st.stop()

products_col = db["products"]
history_col = db["price_history"]


# =========================================================
# DATA HELPERS
# =========================================================
def normalize_store(value):
    return str(value or "").strip().lower()


def store_meta(store):
    key = normalize_store(store)
    meta = STORES.get(key)
    if meta:
        return meta
    return {
        "label": (store or "Tienda").title(),
        "class": "default",
    }


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value):
    return f"${safe_float(value):,.2f}"


def valid_url(url):
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return None


def product_label(prod):
    title = str(prod.get("title") or "Producto sin nombre").strip()
    retailer = store_meta(prod.get("retailer"))["label"]
    return f"{title[:85]} · {retailer}"


@st.cache_data(ttl=90, show_spinner=False)
def load_products(search_text="", stores=(), limit=120):
    filt = {}

    cleaned = (search_text or "").strip()
    if cleaned:
        pattern = re.escape(cleaned)
        filt["$or"] = [
            {"title": {"$regex": pattern, "$options": "i"}},
            {"brand": {"$regex": pattern, "$options": "i"}},
            {"model": {"$regex": pattern, "$options": "i"}},
            {"sku": {"$regex": pattern, "$options": "i"}},
        ]

    if stores:
        filt["retailer"] = {"$in": list(stores)}

    projection = {
        "title": 1,
        "brand": 1,
        "model": 1,
        "sku": 1,
        "retailer": 1,
        "url": 1,
        "image_url": 1,
        "offer_price": 1,
        "regular_price": 1,
        "price": 1,
        "updated_at": 1,
    }

    return list(products_col.find(filt, projection).limit(limit))


@st.cache_data(ttl=90, show_spinner=False)
def load_history(pairs):
    pairs = [pair for pair in pairs if pair[0] and pair[1]]
    if not pairs:
        return {}

    or_filters = [
        {"retailer": retailer, "sku": sku}
        for retailer, sku in pairs
    ]

    cursor = history_col.find(
        {"$or": or_filters},
        {
            "_id": 0,
            "retailer": 1,
            "sku": 1,
            "date": 1,
            "offer_price": 1,
            "regular_price": 1,
        },
    ).sort("date", 1)

    grouped = defaultdict(list)

    for row in cursor:
        key = (
            normalize_store(row.get("retailer")),
            str(row.get("sku")),
        )
        grouped[key].append(row)

    return dict(grouped)


@st.cache_data(ttl=120, show_spinner=False)
def load_global_metrics():
    total = products_col.count_documents({})
    stores = len(products_col.distinct("retailer"))
    return total, stores


def history_for(prod, history_map):
    key = (
        normalize_store(prod.get("retailer")),
        str(prod.get("sku")),
    )
    return history_map.get(key, [])


def product_metrics(prod, history):
    current = 0.0
    regular = 0.0

    if history:
        latest = history[-1]
        current = safe_float(latest.get("offer_price"))
        regular = safe_float(latest.get("regular_price"))

    if current <= 0:
        current = safe_float(
            prod.get("offer_price") or prod.get("price")
        )

    if regular <= 0:
        regular = safe_float(prod.get("regular_price"))

    historical_prices = [
        safe_float(item.get("offer_price"))
        for item in history
        if safe_float(item.get("offer_price")) > 0
    ]

    historical_min = (
        min(historical_prices)
        if historical_prices
        else current
    )

    historical_max = (
        max(historical_prices)
        if historical_prices
        else current
    )

    historical_avg = (
        sum(historical_prices) / len(historical_prices)
        if historical_prices
        else current
    )

    discount_pct = 0.0

    if regular > 0 and current > 0 and regular > current:
        discount_pct = ((regular - current) / regular) * 100

    if historical_min > 0 and current > 0:
        proximity = max(
            0.0,
            min(1.0, historical_min / current),
        )
    else:
        proximity = 0.0

    # 50 puntos por descuento y 50 por cercanía al mínimo histórico.
    score = min(
        100,
        min(discount_pct, 50) + (proximity * 50),
    )

    if (
        historical_max > historical_min
        and current > 0
    ):
        position = (
            (current - historical_min)
            / (historical_max - historical_min)
        )
        position = max(0.0, min(1.0, position))
    else:
        position = 0.0

    if (
        len(historical_prices) >= 2
        and current <= historical_min * 1.01
    ):
        status = ("🔥 Mínimo histórico", "best")

    elif (
        discount_pct >= 20
        and current <= historical_avg * 1.05
    ):
        status = ("✅ Buena oferta", "good")

    elif (
        len(historical_prices) >= 3
        and regular > historical_min * 1.25
        and current > historical_min * 1.08
    ):
        status = ("⚠️ Revisar descuento", "warn")

    else:
        status = ("Precio estable", "neutral")

    return {
        "current": current,
        "regular": regular,
        "historical_min": historical_min,
        "historical_max": historical_max,
        "historical_avg": historical_avg,
        "discount_pct": discount_pct,
        "score": score,
        "position": position,
        "status_text": status[0],
        "status_kind": status[1],
        "captures": len(historical_prices),
    }


def enrich_products(products, history_map):
    enriched = []

    for prod in products:
        history = history_for(prod, history_map)
        metrics = product_metrics(prod, history)

        enriched.append(
            {
                "product": prod,
                "history": history,
                **metrics,
            }
        )

    return enriched


def sort_products(items, sort_key):
    if sort_key == "price_asc":
        return sorted(
            items,
            key=lambda x: (
                x["current"]
                if x["current"] > 0
                else float("inf")
            ),
        )

    if sort_key == "discount":
        return sorted(
            items,
            key=lambda x: x["discount_pct"],
            reverse=True,
        )

    if sort_key == "recent":
        return list(items)

    return sorted(
        items,
        key=lambda x: x["score"],
        reverse=True,
    )


# =========================================================
# UI HELPERS
# =========================================================
def store_badge(store):
    meta = store_meta(store)

    return (
        f'<span class="store-badge badge-{meta["class"]}">'
        f'{meta["label"]}</span>'
    )


def status_badge(item):
    return (
        f'<span class="status status-{item["status_kind"]}">'
        f'{item["status_text"]}</span>'
    )


def price_html(item):
    current = item["current"]
    regular = item["regular"]
    discount = item["discount_pct"]

    old = ""
    disc = ""

    if regular > current > 0:
        old = (
            f'<span class="old-price">'
            f'{money(regular)}</span>'
        )

        if discount > 0:
            disc = (
                f'<span class="discount">'
                f'-{discount:.0f}%</span>'
            )

    current_text = (
        money(current)
        if current > 0
        else "Sin precio"
    )

    return (
        f'<div class="price">'
        f'{current_text}{old}{disc}'
        f'</div>'
    )


def monitor_html(item):
    lo = item["historical_min"]
    hi = item["historical_max"]
    current = item["current"]
    position = int(item["position"] * 100)

    if lo <= 0 or hi <= 0:
        return (
            '<div class="mini-note">'
            'Aún no hay suficiente histórico.'
            '</div>'
        )

    return (
        f'<div class="monitor">'
        f'<div class="monitor-labels">'
        f'<span>Mín {money(lo)}</span>'
        f'<span>Actual {money(current)}</span>'
        f'<span>Máx {money(hi)}</span>'
        f'</div>'
        f'<div class="monitor-track">'
        f'<div class="monitor-fill" style="width:100%"></div>'
        f'</div>'
        f'<div class="monitor-marker-wrap">'
        f'<span class="monitor-marker" style="left:{position}%"></span>'
        f'</div>'
        f'</div>'
    )


def render_product_card(item, key_prefix="card"):
    prod = item["product"]

    title = str(
        prod.get("title")
        or "Producto sin nombre"
    ).strip()

    brand = str(
        prod.get("brand")
        or "Sin marca"
    ).strip()

    model = str(
        prod.get("model")
        or ""
    ).strip()

    sku = prod.get("sku") or "N/D"
    image = valid_url(prod.get("image_url"))
    url = valid_url(prod.get("url"))

    with st.container(border=True):
        if image:
            st.image(
                image,
                use_container_width=True,
            )
        else:
            render_html(
                """
                <div style="height:150px;display:grid;place-items:center;background:#f3f4f6;border-radius:12px;color:#9ca3af;">
                    Sin imagen
                </div>
                """
            )

        render_html(store_badge(prod.get("retailer")))

        short_title = (
            title[:78] + "…"
            if len(title) > 78
            else title
        )

        st.markdown(f"**{short_title}**")

        detail = brand
        if model:
            detail += f" · {model}"

        st.caption(detail)

        render_html(price_html(item))

        render_html(status_badge(item))

        render_html(monitor_html(item))

        st.caption(
            f"{item['captures']} capturas · "
            f"SKU {sku} · "
            f"Score {item['score']:.0f}/100"
        )

        if url:
            st.link_button(
                "Ver en tienda ↗",
                url,
                use_container_width=True,
            )
        else:
            st.button(
                "Enlace no disponible",
                disabled=True,
                use_container_width=True,
                key=(
                    f"{key_prefix}_"
                    f"{normalize_store(prod.get('retailer'))}_"
                    f"{sku}"
                ),
            )


def render_product_grid(
    items,
    columns=4,
    max_items=12,
    key_prefix="grid",
):
    visible = items[:max_items]

    for start in range(
        0,
        len(visible),
        columns,
    ):
        cols = st.columns(columns)

        for offset, col in enumerate(cols):
            idx = start + offset

            if idx >= len(visible):
                break

            with col:
                render_product_card(
                    visible[idx],
                    key_prefix=f"{key_prefix}_{idx}",
                )


def history_dataframe(item):
    rows = []

    for row in item["history"]:
        rows.append(
            {
                "Fecha": row.get("date"),
                "Precio oferta": safe_float(
                    row.get("offer_price")
                ),
                "Precio regular": safe_float(
                    row.get("regular_price")
                ),
            }
        )

    return pd.DataFrame(rows)


# =========================================================
# HEADER
# =========================================================
render_html(
    """
    <div class="hero">
        <div class="eyebrow">● PRICE INTELLIGENCE · EL SALVADOR</div>
        <h1>ComparaBlack SV</h1>
        <p>Monitorea precios, valida descuentos y compara comercios antes de comprar. Menos ruido de “ofertas”; más evidencia histórica.</p>
    </div>
    """
)


# =========================================================
# FILTERS
# =========================================================
f1, f2, f3, f4 = st.columns(
    [2.2, 1.35, 1.2, .9]
)

with f1:
    search_query = st.text_input(
        "Buscar",
        placeholder=(
            "Ej. Samsung 55, LG, "
            "Mabe, Inverter..."
        ),
        label_visibility="collapsed",
    )

with f2:
    store_options = list(STORES.keys())

    selected_stores = st.multiselect(
        "Tiendas",
        options=store_options,
        default=[],
        format_func=lambda x: STORES[x]["label"],
        placeholder="Todas las tiendas",
        label_visibility="collapsed",
    )

with f3:
    sort_label = st.selectbox(
        "Ordenar",
        list(SORT_OPTIONS.keys()),
        label_visibility="collapsed",
    )

with f4:
    cards_per_row = st.selectbox(
        "Columnas",
        [3, 4],
        index=1,
        label_visibility="collapsed",
    )


# =========================================================
# LOAD DATA
# =========================================================
products = load_products(
    search_text=search_query,
    stores=tuple(selected_stores),
    limit=120,
)

pairs = tuple(
    (
        normalize_store(p.get("retailer")),
        str(p.get("sku")),
    )
    for p in products
    if p.get("retailer") and p.get("sku")
)

history_map = load_history(pairs)

items = enrich_products(
    products,
    history_map,
)

items = sort_products(
    items,
    SORT_OPTIONS[sort_label],
)

total_global, stores_global = (
    load_global_metrics()
)

active_offers = sum(
    1
    for item in items
    if item["discount_pct"] > 0
)

historical_lows = sum(
    1
    for item in items
    if item["status_kind"] == "best"
)


# =========================================================
# KPIs
# =========================================================
k1, k2, k3, k4 = st.columns(4)

k1.metric(
    "Productos rastreados",
    f"{total_global:,}",
)

k2.metric(
    "Comercios activos",
    stores_global,
)

k3.metric(
    "Ofertas visibles",
    active_offers,
)

k4.metric(
    "Mínimos históricos",
    historical_lows,
)

st.write("")


# =========================================================
# NAVIGATION
# =========================================================
(
    tab_overview,
    tab_offers,
    tab_compare,
    tab_monitor,
) = st.tabs(
    [
        "Resumen",
        "Ofertas",
        "Comparar",
        "Monitoreo",
    ]
)


# =========================================================
# TAB: OVERVIEW
# =========================================================
with tab_overview:
    if not items:
        st.info(
            "No encontramos productos "
            "con esos filtros."
        )

    else:
        render_html('<div class="section-title">Oportunidades destacadas</div>')

        render_html('<div class="section-copy">Prioriza precio actual, descuento y cercanía al mínimo histórico.</div>')

        render_product_grid(
            items,
            columns=cards_per_row,
            max_items=12,
            key_prefix="overview",
        )

        st.write("")

        left, right = st.columns(
            [1.15, .85]
        )

        with left:
            render_html('<div class="section-title">Distribución de precios por comercio</div>')

            chart_rows = []

            for item in items:
                if item["current"] <= 0:
                    continue

                chart_rows.append(
                    {
                        "Tienda": store_meta(
                            item["product"].get(
                                "retailer"
                            )
                        )["label"],
                        "Precio": item["current"],
                    }
                )

            if chart_rows:
                df_prices = pd.DataFrame(
                    chart_rows
                )

                fig = px.box(
                    df_prices,
                    x="Tienda",
                    y="Precio",
                    points=False,
                )

                fig.update_layout(
                    height=340,
                    margin=dict(
                        l=10,
                        r=10,
                        t=10,
                        b=10,
                    ),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    showlegend=False,
                    xaxis_title="",
                    yaxis_title="Precio (USD)",
                )

                st.plotly_chart(
                    fig,
                    use_container_width=True,
                )

            else:
                st.caption(
                    "Aún no hay suficientes "
                    "precios para graficar."
                )

        with right:
            render_html('<div class="section-title">Cobertura del catálogo</div>')

            coverage = pd.DataFrame(
                [
                    {
                        "Tienda": store_meta(
                            item["product"].get(
                                "retailer"
                            )
                        )["label"],
                        "Producto": 1,
                    }
                    for item in items
                ]
            )

            if not coverage.empty:
                coverage = (
                    coverage
                    .groupby(
                        "Tienda",
                        as_index=False,
                    )["Producto"]
                    .sum()
                )

                coverage = coverage.sort_values(
                    "Producto",
                    ascending=True,
                )

                fig = px.bar(
                    coverage,
                    x="Producto",
                    y="Tienda",
                    orientation="h",
                    text="Producto",
                )

                fig.update_layout(
                    height=340,
                    margin=dict(
                        l=10,
                        r=10,
                        t=10,
                        b=10,
                    ),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    xaxis_title="Productos",
                    yaxis_title="",
                    showlegend=False,
                )

                st.plotly_chart(
                    fig,
                    use_container_width=True,
                )


# =========================================================
# TAB: OFFERS
# =========================================================
with tab_offers:
    render_html('<div class="section-title">Ofertas verificables</div>')

    render_html('<div class="section-copy">Productos con rebaja visible, ordenados por oportunidad real y contexto histórico.</div>')

    o1, o2, o3 = st.columns(3)

    min_discount = o1.slider(
        "Descuento mínimo",
        0,
        60,
        10,
        5,
    )

    only_historical_low = o2.toggle(
        "Solo mínimos históricos",
        value=False,
    )

    max_price = o3.number_input(
        "Precio máximo",
        min_value=0.0,
        value=0.0,
        step=25.0,
    )

    offer_items = [
        item
        for item in items
        if item["discount_pct"] >= min_discount
        and (
            not only_historical_low
            or item["status_kind"] == "best"
        )
        and (
            max_price <= 0
            or item["current"] <= max_price
        )
    ]

    if offer_items:
        offer_items = sorted(
            offer_items,
            key=lambda x: (
                x["score"],
                x["discount_pct"],
            ),
            reverse=True,
        )

        render_product_grid(
            offer_items,
            columns=cards_per_row,
            max_items=24,
            key_prefix="offers",
        )

    else:
        st.info(
            "No hay ofertas que cumplan "
            "esos criterios."
        )


# =========================================================
# TAB: COMPARE
# =========================================================
with tab_compare:
    render_html('<div class="section-title">Comparador entre almacenes</div>')

    render_html('<div class="section-copy">Busca un modelo o término específico y revisa coincidencias entre comercios.</div>')

    compare_query = st.text_input(
        "Producto a comparar",
        value=search_query,
        placeholder=(
            "Ej. LG OLED C4 55, "
            "Samsung QLED 65..."
        ),
        key="compare_query",
    )

    if not compare_query.strip():
        st.info(
            "Escribe un modelo o nombre "
            "de producto para iniciar "
            "la comparación."
        )

    else:
        compare_products = load_products(
            search_text=compare_query,
            stores=(),
            limit=60,
        )

        compare_pairs = tuple(
            (
                normalize_store(
                    p.get("retailer")
                ),
                str(p.get("sku")),
            )
            for p in compare_products
            if p.get("retailer")
            and p.get("sku")
        )

        compare_history = load_history(
            compare_pairs
        )

        compare_items = enrich_products(
            compare_products,
            compare_history,
        )

        compare_items = sorted(
            [
                x
                for x in compare_items
                if x["current"] > 0
            ],
            key=lambda x: x["current"],
        )

        if not compare_items:
            st.warning(
                "No encontramos precios "
                "para ese término."
            )

        else:
            cheapest = (
                compare_items[0]["current"]
            )

            rows = []

            for item in compare_items:
                prod = item["product"]

                rows.append(
                    {
                        "Tienda": store_meta(
                            prod.get("retailer")
                        )["label"],
                        "Producto": (
                            prod.get("title")
                            or ""
                        ),
                        "Marca": (
                            prod.get("brand")
                            or ""
                        ),
                        "Modelo": (
                            prod.get("model")
                            or ""
                        ),
                        "Actual": item["current"],
                        "Regular": item["regular"],
                        "Descuento %": round(
                            item["discount_pct"],
                            1,
                        ),
                        "Mín. histórico": (
                            item["historical_min"]
                        ),
                        "Estado": (
                            item["status_text"]
                        ),
                        "Mejor precio": (
                            "Sí"
                            if item["current"]
                            == cheapest
                            else ""
                        ),
                    }
                )

            df_compare = pd.DataFrame(rows)

            best = compare_items[0]

            b1, b2, b3 = st.columns(3)

            b1.metric(
                "Mejor precio encontrado",
                money(best["current"]),
            )

            b2.metric(
                "Tienda",
                store_meta(
                    best["product"].get(
                        "retailer"
                    )
                )["label"],
            )

            if len(compare_items) > 1:
                spread = (
                    max(
                        x["current"]
                        for x in compare_items
                    )
                    - cheapest
                )
            else:
                spread = 0

            b3.metric(
                "Brecha entre resultados",
                money(spread),
            )

            chart_df = (
                df_compare
                .head(12)
                .copy()
            )

            chart_df["Etiqueta"] = (
                chart_df["Tienda"]
                + " · "
                + chart_df["Producto"].str.slice(
                    0,
                    34,
                )
            )

            fig = px.bar(
                chart_df.sort_values(
                    "Actual",
                    ascending=True,
                ),
                x="Actual",
                y="Etiqueta",
                orientation="h",
                text="Actual",
            )

            fig.update_traces(
                texttemplate="$%{text:,.2f}",
                textposition="outside",
            )

            fig.update_layout(
                height=max(
                    340,
                    len(chart_df) * 42,
                ),
                margin=dict(
                    l=10,
                    r=35,
                    t=10,
                    b=10,
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis_title="Precio (USD)",
                yaxis_title="",
                showlegend=False,
            )

            st.plotly_chart(
                fig,
                use_container_width=True,
            )

            st.dataframe(
                df_compare,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Actual": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Regular": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Mín. histórico": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Descuento %": (
                        st.column_config.NumberColumn(
                            format="%.1f%%"
                        )
                    ),
                },
            )

            st.caption(
                "Importante: una coincidencia "
                "de búsqueda no garantiza que "
                "dos tiendas publiquen exactamente "
                "la misma variante. Para comparación "
                "1:1, guarda un campo común como "
                "`model`, `mpn`, `ean` o `gtin` "
                "en MongoDB."
            )


# =========================================================
# TAB: MONITOR
# =========================================================
with tab_monitor:
    render_html('<div class="section-title">Monitoreo de precio</div>')

    render_html('<div class="section-copy">Selecciona un producto y revisa su trayectoria, rango y nivel actual.</div>')

    monitored = [
        item
        for item in items
        if item["history"]
    ]

    if not monitored:
        st.info(
            "No hay productos con historial "
            "para los filtros actuales."
        )

    else:
        selected_index = st.selectbox(
            "Producto",
            range(len(monitored)),
            format_func=lambda i: product_label(
                monitored[i]["product"]
            ),
            key="monitor_product",
        )

        item = monitored[selected_index]

        m1, m2, m3, m4 = st.columns(4)

        m1.metric(
            "Precio actual",
            money(item["current"]),
        )

        m2.metric(
            "Mínimo histórico",
            money(item["historical_min"]),
        )

        m3.metric(
            "Máximo histórico",
            money(item["historical_max"]),
        )

        m4.metric(
            "Capturas",
            item["captures"],
        )

        render_html(monitor_html(item))

        df_h = history_dataframe(item)

        if not df_h.empty:
            df_h["Fecha"] = pd.to_datetime(
                df_h["Fecha"],
                errors="coerce",
            )

            df_h = (
                df_h
                .dropna(subset=["Fecha"])
                .sort_values("Fecha")
            )

            fig = go.Figure()

            fig.add_trace(
                go.Scatter(
                    x=df_h["Fecha"],
                    y=df_h["Precio oferta"],
                    mode="lines+markers",
                    name="Oferta",
                    line=dict(width=3),
                )
            )

            if (
                df_h["Precio regular"] > 0
            ).any():
                fig.add_trace(
                    go.Scatter(
                        x=df_h["Fecha"],
                        y=df_h["Precio regular"],
                        mode="lines",
                        name="Regular",
                        line=dict(
                            width=1.5,
                            dash="dot",
                        ),
                    )
                )

            fig.add_hline(
                y=item["historical_min"],
                line_dash="dash",
                annotation_text=(
                    f"Mínimo "
                    f"{money(item['historical_min'])}"
                ),
                annotation_position=(
                    "bottom right"
                ),
            )

            fig.update_layout(
                height=430,
                margin=dict(
                    l=10,
                    r=10,
                    t=20,
                    b=10,
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis_title="",
                yaxis_title="Precio (USD)",
                hovermode="x unified",
                legend=dict(
                    orientation="h",
                    y=1.08,
                    x=0,
                ),
            )

            st.plotly_chart(
                fig,
                use_container_width=True,
            )

            recent = df_h.tail(10).copy()

            recent["Cambio"] = (
                recent["Precio oferta"].diff()
            )

            st.dataframe(
                recent.sort_values(
                    "Fecha",
                    ascending=False,
                ),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Precio oferta": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Precio regular": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Cambio": (
                        st.column_config.NumberColumn(
                            format="$%.2f"
                        )
                    ),
                    "Fecha": (
                        st.column_config.DatetimeColumn(
                            format=(
                                "DD/MM/YYYY HH:mm"
                            )
                        )
                    ),
                },
            )


# =========================================================
# FOOTER
# =========================================================
st.divider()

st.caption(
    "ComparaBlack SV · Los indicadores se basan "
    "en tu propio historial de capturas. Mientras "
    "más días de seguimiento acumules, más confiable "
    "será la auditoría."
)
