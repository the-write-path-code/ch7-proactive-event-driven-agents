import sqlite3
import math
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
import h3
from src.logger import logger

st.set_page_config(layout="wide", page_title="DON Dashboard")

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def h3_center(hex_id):
    """Return the (lat, lng) centroid of an H3 hex cell."""
    return h3.cell_to_latlng(hex_id)

def h3_boundary(hex_id):
    """Return a list of (lat, lng) tuples forming the hex polygon."""
    boundary = h3.cell_to_boundary(hex_id)
    return list(boundary) + [boundary[0]]

def h3_distance_miles(hex1, hex2):
    """Approximate distance in miles between two H3 cells via grid_distance."""
    try:
        steps = h3.grid_distance(hex1, hex2)
        return round(steps / 1.9, 1)
    except Exception:
        return float('inf')

def fmt_hours(val):
    """Return the value as a string, or 'N/A' if missing/invalid."""
    try:
        v = float(val)
        if pd.isna(v):
            return "N/A"
        return str(int(v))
    except (TypeError, ValueError):
        return "N/A"

# ─── DATA SYNC (runs once per session, or on manual refresh) ──────────────────

import sys, os
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from etl.sync import sync_db_from_excels

@st.cache_data(show_spinner="Syncing data from Excel files…")
def run_sync():
    """Run the ETL pipeline once and return the result + timestamp."""
    result = sync_db_from_excels()
    return result, datetime.now()

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

sync_result, sync_ts = run_sync()

if not sync_result.ok:
    logger.error(f"Data sync failed: {sync_result.error}")
    st.error(sync_result.error)
    st.stop()
else:
    logger.info(f"Data sync complete: {sync_result.total_clients} clients, {sync_result.total_staff} staff loaded.")

st.session_state.last_refreshed = sync_ts

summary_parts = [
    f"**{sync_result.total_clients}** clients",
    f"**{sync_result.total_staff}** staff loaded",
]
if sync_result.new_geocoded:
    summary_parts.append(f"**{sync_result.new_geocoded}** new address(es) geocoded")
if sync_result.addr_updated:
    summary_parts.append(f"**{sync_result.addr_updated}** address(es) re-geocoded")
if sync_result.clients_removed or sync_result.staff_removed:
    summary_parts.append(
        f"**{sync_result.clients_removed + sync_result.staff_removed}** removed record(s)"
    )

st.sidebar.success(" · ".join(summary_parts))

if 'last_refreshed' in st.session_state:
    ts_str = st.session_state.last_refreshed.strftime("%Y-%m-%d %H:%M")
    st.sidebar.caption(f"Last refreshed: {ts_str}")

if sync_result.geocode_failures:
    with st.sidebar.expander(f"⚠️ {len(sync_result.geocode_failures)} geocoding failure(s)"):
        for addr in sync_result.geocode_failures:
            st.write(f"• {addr}")

@st.cache_data(show_spinner=False)
def load_data():
    conn = sqlite3.connect('data/staffing_engine_secure.db')
    clients_df = pd.read_sql_query("SELECT * FROM clients", conn)
    staff_df   = pd.read_sql_query("SELECT * FROM staff_capacity", conn)
    conn.close()
    clients_df['Full Name'] = clients_df['First Name'] + ' ' + clients_df['Last Name']
    return clients_df, staff_df

clients_df, staff_df = load_data()

sorted_client_names = sorted(clients_df['Full Name'].tolist())
dropdown_options    = ["— Select a Client —"] + sorted_client_names

# ─── SESSION STATE ────────────────────────────────────────────────────────────

if 'selected_client' not in st.session_state:
    st.session_state.selected_client = None

# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.header("Select Client")

sel = st.session_state.selected_client
current_idx = sorted_client_names.index(sel) if sel in sorted_client_names else None

dropdown_choice = st.sidebar.selectbox(
    "Client Name",
    options=sorted_client_names,
    index=current_idx,
    placeholder="Type or scroll to find a client…",
    label_visibility="collapsed",
)

if dropdown_choice != st.session_state.selected_client:
    was_none = st.session_state.selected_client is None
    st.session_state.selected_client = dropdown_choice
    st.session_state.agent_staff_ids = None
    if dropdown_choice is not None and was_none and not st.session_state.get('radius_user_set'):
        st.session_state.radius_filter = "5"
    st.rerun()

st.sidebar.header("Filters")

has_client = bool(st.session_state.selected_client)

RADIUS_OPTIONS = ["5", "10", "15", "20", "25", ">25 mi"]
INF_OPTION     = ">25 mi"

if 'radius_filter' not in st.session_state:
    st.session_state.radius_filter = "5"

st.sidebar.markdown(
    "**Distance Radius (miles)**" + ("  ℹ️ *select a client first*" if not has_client else "")
)

col_left, col_right = st.sidebar.columns(2)
for i, opt in enumerate(RADIUS_OPTIONS):
    col = col_left if i % 2 == 0 else col_right
    is_active = (st.session_state.radius_filter == opt)
    btn_type  = "primary" if is_active else "secondary"
    label     = f"✔ {opt}" if is_active else opt
    if col.button(label, key=f"rad_{opt}", disabled=not has_client, type=btn_type, use_container_width=True):
        st.session_state.radius_filter = opt
        st.session_state.radius_user_set = True
        st.session_state.agent_staff_ids = None
        st.rerun()

radius_filter = st.session_state.radius_filter
max_dist = {"5": 5, "10": 10, "15": 15, "20": 20, "25": 25}.get(radius_filter, float('inf'))

st.sidebar.subheader("Legend")
st.sidebar.markdown("⬡ Blue hex = **Client**")
st.sidebar.markdown("**Staff Roles (Hex Colors):**")
st.sidebar.markdown(
    """
<div style='font-size: 15px; display: flex; flex-direction: column; gap: 10px;'>
    <div style='display: flex; align-items: center; gap: 10px;'>
        <svg width='24' height='24' viewBox='0 0 30 30'>
            <polygon points='15,2 27,9 27,21 15,28 3,21 3,9' stroke='#28a745' stroke-width='2' fill='#28a745' fill-opacity='0.6'/>
        </svg>
        <span style='color:#1a1a1a;'>PCA</span>
    </div>
    <div style='display: flex; align-items: center; gap: 10px;'>
        <svg width='24' height='24' viewBox='0 0 30 30'>
            <polygon points='15,2 27,9 27,21 15,28 3,21 3,9' stroke='#7B2FBE' stroke-width='2' fill='#7B2FBE' fill-opacity='0.6'/>
        </svg>
        <span style='color:#1a1a1a;'>LPN</span>
    </div>
    <div style='display: flex; align-items: center; gap: 10px;'>
        <svg width='24' height='24' viewBox='0 0 30 30'>
            <polygon points='15,2 27,9 27,21 15,28 3,21 3,9' stroke='#E8700A' stroke-width='2' fill='#E8700A' fill-opacity='0.6'/>
        </svg>
        <span style='color:#1a1a1a;'>RN</span>
    </div>
</div>
""",
    unsafe_allow_html=True,
)

st.sidebar.markdown("---")
if st.sidebar.button("Reset Map"):
    st.session_state.selected_client = None
    st.session_state.radius_filter = "5"
    st.session_state.radius_user_set = False
    st.session_state.agent_staff_ids = None
    st.rerun()

# ─── LAYOUT ───────────────────────────────────────────────────────────────────

st.title("Staffing Dashboard")
if st.session_state.selected_client:
    st.write(f"Showing staff near **{st.session_state.selected_client}**.")
else:
    st.write("Select a client from the sidebar or click a hex on the map.")

# ══════════════════════════════════════════════════════════════════════════════
#  FULL-WIDTH MAP
# ══════════════════════════════════════════════════════════════════════════════

m = folium.Map(location=[37.5, -77.5], zoom_start=11)

fg_clients = folium.FeatureGroup(name="Clients")
fg_rns     = folium.FeatureGroup(name="RNs")
fg_lpns    = folium.FeatureGroup(name="LPNs")
fg_pcas    = folium.FeatureGroup(name="PCAs")

if st.session_state.selected_client:
    selected_client_row = clients_df[
        clients_df['Full Name'] == st.session_state.selected_client
    ].iloc[0]
    clients_to_draw = clients_df[clients_df['Full Name'] == st.session_state.selected_client]
    c_hex = selected_client_row.get('H3_Index')

    k_threshold = math.ceil(max_dist * 1.9) if max_dist != float('inf') else 999

    staff_work = staff_df.copy()
    staff_work['Grid_Steps'] = staff_work['H3_Index'].apply(
        lambda s_hex: h3.grid_distance(c_hex, s_hex) if (c_hex and s_hex) else 999
    )
    staff_work['Distance_Miles'] = (staff_work['Grid_Steps'] / 1.9).round(1)

    agent_staff_ids = st.session_state.get("agent_staff_ids")
    if agent_staff_ids is not None:
        staff_to_draw = staff_work[staff_work['Staff_ID'].isin(agent_staff_ids)]
    else:
        staff_to_draw = staff_work[staff_work['Grid_Steps'] <= k_threshold]

    all_centers = [h3_center(c_hex)] if c_hex else []
    for _, row in staff_to_draw.iterrows():
        if row.get('H3_Index'):
            all_centers.append(h3_center(row['H3_Index']))
    if all_centers:
        all_lats = [c[0] for c in all_centers]
        all_lons = [c[1] for c in all_centers]
        padding = 0.02
        sw = [min(all_lats) - padding, min(all_lons) - padding]
        ne = [max(all_lats) + padding, max(all_lons) + padding]
        m.fit_bounds([sw, ne])
else:
    clients_to_draw = clients_df
    staff_to_draw   = staff_df.copy()
    staff_to_draw['Distance_Miles'] = None
    staff_to_draw['Grid_Steps'] = None

def _safe(row, col, fallback='N/A'):
    val = row.get(col)
    if pd.isna(val) or str(val).strip() == '':
        return fallback
    return str(val).strip()

for _, client in clients_to_draw.iterrows():
    hex_id = client.get('H3_Index')
    if not hex_id or pd.isna(hex_id):
        continue

    client_tooltip = (
        f"<b>{client['Full Name']}</b><br>"
        f"📞 {_safe(client, 'Phone')}<br>"
        f"<b>Class:</b> {_safe(client, 'Class')}"
    )

    hex_boundary = h3_boundary(hex_id)
    folium.Polygon(
        locations=hex_boundary,
        color='#1a73e8',
        fill=True,
        fill_color='#4285f4',
        fill_opacity=0.45,
        weight=2,
        tooltip=folium.Tooltip(client_tooltip, sticky=True),
    ).add_to(fg_clients)

ROLE_COLORS = {'PCA': '#28a745', 'LPN': '#7B2FBE', 'RN': '#E8700A'}

for _, staff in staff_to_draw.iterrows():
    hex_id = staff.get('H3_Index')
    if not hex_id or pd.isna(hex_id):
        continue

    role = _safe(staff, 'Role', 'PCA')
    fg = {'LPN': fg_lpns, 'RN': fg_rns}.get(role, fg_pcas)
    fill_color = ROLE_COLORS.get(role, '#28a745')

    avail = staff.get('Available_Hours')
    try:
        avail_num = float(avail)
        if pd.isna(avail_num) or avail_num <= 0:
            fill_opacity = 0.25
        elif avail_num >= 20:
            fill_opacity = 0.8
        else:
            fill_opacity = 0.25 + (avail_num / 20) * 0.55
    except (TypeError, ValueError):
        fill_opacity = 0.25

    max_h   = fmt_hours(staff.get('Max_Weekly_Hours'))
    avail_h = fmt_hours(staff.get('Available_Hours'))
    dist_str = f"{staff['Distance_Miles']} mi" if staff.get('Distance_Miles') is not None else ''

    tooltip_html = (
        f"<b>Name:</b> {_safe(staff, 'First Name', '')} {_safe(staff, 'Last Name', '')}<br>"
        f"<b>Phone:</b> {_safe(staff, 'Mobile')}<br>"
        f"<b>Role:</b> {role}<br>"
        f"<b>Gender:</b> {_safe(staff, 'Gender')}<br>"
        f"<b>Max Weekly Hours:</b> {max_h}<br>"
        f"<b>Available Hours:</b> {avail_h}"
    )
    if dist_str:
        tooltip_html += f"<br><b>Distance:</b> ~{dist_str}"

    hex_boundary = h3_boundary(hex_id)
    folium.Polygon(
        locations=hex_boundary,
        color=fill_color,
        fill=True,
        fill_color=fill_color,
        fill_opacity=fill_opacity,
        weight=2,
        tooltip=tooltip_html,
    ).add_to(fg)

fg_clients.add_to(m)
fg_rns.add_to(m)
fg_lpns.add_to(m)
fg_pcas.add_to(m)
folium.LayerControl().add_to(m)

st_data = st_folium(
    m,
    use_container_width=True,
    height=550,
    returned_objects=["last_object_clicked_tooltip", "last_clicked"],
    key=f"map_{st.session_state.selected_client}_{max_dist}",
)

clicked_tooltip = st_data.get("last_object_clicked_tooltip") or ""
if clicked_tooltip:
    matched_name = next(
        (name for name in clients_df['Full Name'].values if name in clicked_tooltip),
        None,
    )
    if matched_name and st.session_state.selected_client != matched_name:
        was_none = st.session_state.selected_client is None
        st.session_state.selected_client = matched_name
        st.session_state.agent_staff_ids = None
        if was_none and not st.session_state.get('radius_user_set'):
            st.session_state.radius_filter = "5"
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
#  TWO COLUMNS BELOW MAP
# ══════════════════════════════════════════════════════════════════════════════

col_chat, col_table = st.columns(2, gap="large")

with col_table:
    if st.session_state.selected_client:
        st.subheader(f"📋 Nearby Staff")
        filtered_staff = staff_to_draw.copy()

        if not filtered_staff.empty:
            filtered_staff = filtered_staff.sort_values(
                by='Distance_Miles', ascending=True, na_position='last',
            )
            desired_cols = ['First Name', 'Last Name', 'Mobile', 'Role',
                            'Distance_Miles', 'Max_Weekly_Hours', 'Available_Hours']
            display_cols = [c for c in desired_cols if c in filtered_staff.columns]
            formatted_df = filtered_staff[display_cols].copy()

            if 'Distance_Miles' in formatted_df.columns:
                formatted_df['Distance_Miles'] = formatted_df['Distance_Miles'].round(1)
            if 'Max_Weekly_Hours' in formatted_df.columns:
                formatted_df['Max_Weekly_Hours'] = formatted_df['Max_Weekly_Hours'].apply(fmt_hours)
            if 'Available_Hours' in formatted_df.columns:
                formatted_df['Available_Hours'] = formatted_df['Available_Hours'].apply(fmt_hours)

            formatted_df = formatted_df.rename(columns={
                'Mobile': 'Phone', 'Distance_Miles': 'Dist (mi)',
                'Max_Weekly_Hours': 'Max Hrs', 'Available_Hours': 'Avail Hrs',
            })

            row_px = 35
            header_px = 38
            table_height = header_px + len(formatted_df) * row_px
            st.dataframe(
                formatted_df, use_container_width=True,
                height=min(table_height, 400), hide_index=True,
            )
        else:
            st.info("No staff within the selected radius.")
    else:
        st.caption("📋 Select a client above to see nearby staff.")

with col_chat:
    st.subheader("🤖 AI Assistant")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "session_id" not in st.session_state:
        import uuid
        st.session_state.session_id = str(uuid.uuid4())

    user_query = st.chat_input(
        "e.g. Find a PCA near Claire Ferguson…", key="chat_input"
    )

    if st.session_state.chat_history or user_query:
        chat_container = st.container(height=350)
        with chat_container:
            for msg in st.session_state.chat_history:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
    else:
        chat_container = st.container()
        chat_container.caption(
            "Ask questions like *\"How many RNs do we have?\"* "
            "or *\"Find LPNs within 15 miles of Client C001\"*"
        )

    if user_query:
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        with chat_container:
            with st.chat_message("user"):
                st.markdown(user_query)

            with st.chat_message("assistant"):
                with st.spinner("Thinking…"):
                    try:
                        from src.agent import ask_agent

                        logger.info(f"Sending query to agent: {user_query}")
                        result = ask_agent(
                            user_query,
                            session_id=st.session_state.session_id,
                        )

                        if result["success"]:
                            logger.info("Agent query successful.")
                            answer = result["answer"]
                            st.session_state.chat_history.append(
                                {"role": "assistant", "content": answer}
                            )

                            context = result.get("context", {})
                            map_update = context.get("map_update")
                            if map_update:
                                c_name = map_update.get("client_name")
                                c_rad  = map_update.get("radius")
                                s_ids  = map_update.get("staff_ids")
                                if c_name:
                                    st.session_state.selected_client = c_name
                                if c_rad is not None:
                                    st.session_state.radius_filter = str(c_rad)
                                    st.session_state.radius_user_set = True
                                if s_ids is not None:
                                    st.session_state.agent_staff_ids = s_ids
                                else:
                                    st.session_state.agent_staff_ids = None
                                st.rerun()

                            st.markdown(answer)

                        else:
                            err_msg = f"⚠️ {result['error']}"
                            logger.warning(f"Agent returned error: {result['error']}")
                            st.error(err_msg)
                            st.session_state.chat_history.append(
                                {"role": "assistant", "content": err_msg}
                            )

                    except Exception as exc:
                        logger.exception(f"Exception during agent call: {exc}")
                        err_msg = f"⚠️ Could not reach AI assistant: {exc}"
                        st.error(err_msg)
                        st.session_state.chat_history.append(
                            {"role": "assistant", "content": err_msg}
                        )
