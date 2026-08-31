import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
    get_settlement_stats, generate_product_summary,
    COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
)
from hpp_manager import (
    load_hpp_master, load_mapping, save_mapping, auto_suggest_mapping,
    get_suggestion_with_confidence
)
import io

st.set_page_config(layout="wide", page_title="Rekonsiliasi Shopee")

# ─── Custom CSS untuk tampilan premium ───
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Summary cards */
.summary-container {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1.2rem;
    margin: 1rem 0 1.5rem 0;
}
.summary-card {
    padding: 1.3rem 1.6rem;
    border-radius: 14px;
    background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
    color: #f8fafc;
    box-shadow: 0 4px 20px rgba(0,0,0,0.18);
    transition: transform 0.18s ease;
}
.summary-card:hover {
    transform: translateY(-3px);
}
.summary-card .label {
    font-size: 0.82rem;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.35rem;
}
.summary-card .value {
    font-size: 1.55rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}
.summary-card .pct {
    font-size: 0.78rem;
    color: #fb923c;
    margin-top: 0.2rem;
}
.card-gross .value { color: #38bdf8; }
.card-fees .value { color: #f87171; }
.card-adj .value { color: #eab308; }
.card-net .value { color: #4ade80; }
.card-daily .value { color: #c084fc; }
.card-daily .pct { font-size: 0.78rem; color: #a78bfa; margin-top: 0.2rem; }
.card-potential .value { color: #fbbf24; }
.card-potential .pct { font-size: 0.78rem; color: #fde68a; margin-top: 0.2rem; }
.card-grand .value { color: #2dd4bf; }
.card-grand .pct { font-size: 0.78rem; color: #99f6e4; margin-top: 0.2rem; }
.card-hpp .value { color: #f97316; }
.card-hpp .pct { font-size: 0.78rem; color: #fdba74; margin-top: 0.2rem; }
.card-laba .value { color: #10b981; }
.card-laba .pct { font-size: 0.78rem; color: #6ee7b7; margin-top: 0.2rem; }
.card-settle .value { color: #38bdf8; }
.card-settle .pct { font-size: 0.78rem; margin-top: 0.2rem; }
.unsettled-badge {
    display: inline-block;
    background: rgba(239, 68, 68, 0.2);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.4);
    border-radius: 6px;
    padding: 0.15rem 0.55rem;
    font-size: 0.8rem;
    font-weight: 600;
    margin: 0.2rem 0.2rem 0.2rem 0;
}

/* Fee breakdown pills */
.breakdown-container {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.85rem;
    margin: 0.8rem 0 1.5rem 0;
}
.breakdown-card {
    padding: 0.9rem 1.1rem;
    border-radius: 10px;
    background: rgba(30, 41, 59, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: #f8fafc;
}
.breakdown-card .title {
    font-size: 0.75rem;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    margin-bottom: 0.25rem;
}
.breakdown-card .val {
    font-size: 1.15rem;
    font-weight: 700;
    color: #f1f5f9;
}
.breakdown-card .sub {
    font-size: 0.72rem;
    color: #fb923c;
    margin-top: 0.15rem;
}

/* Section Grouping */
.section-group {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 1.5rem;
}
.section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
    padding-bottom: 0.6rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.section-title {
    font-size: 1.05rem;
    font-weight: 700;
    color: #f1f5f9;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.section-badge {
    font-size: 0.78rem;
    font-weight: 600;
    padding: 0.2rem 0.65rem;
    border-radius: 20px;
    letter-spacing: 0.02em;
}
.badge-settled {
    background: rgba(16, 185, 129, 0.18);
    color: #6ee7b7;
    border: 1px solid rgba(16, 185, 129, 0.35);
}
.badge-pending {
    background: rgba(245, 158, 11, 0.18);
    color: #fde68a;
    border: 1px solid rgba(245, 158, 11, 0.35);
}
.badge-grand {
    background: rgba(45, 212, 191, 0.18);
    color: #99f6e4;
    border: 1px solid rgba(45, 212, 191, 0.35);
}
</style>
""", unsafe_allow_html=True)

# ─── Sidebar Navigation ───
with st.sidebar:
    st.markdown("## 🏪 **Warung Aisha Tool**")
    st.caption("Alat Analisis & Manajemen Penjualan Shopee")
    st.divider()

    menu = st.radio(
        "📌 **Pilih Menu Navigasi:**",
        ["📊 Rekonsiliasi Shopee", "📦 Kelola Master HPP"],
        index=0,
        key="main_navigation"
    )
    st.divider()


# ==============================================================================
# 📊 MENU 1: REKONSILIASI SHOPEE
# ==============================================================================
if menu == "📊 Rekonsiliasi Shopee":
    st.title("📊 Rekonsiliasi Transaksi & Margin Shopee")
    st.write("Upload laporan Order dan Laporan Penghasilan Shopee untuk melihat analisis keuangan, fee, dan margin laba.")

    with st.sidebar:
        st.subheader("📁 Upload File Transaksi")
        uploaded_order = st.file_uploader("1. Laporan Order (Excel) *", type=['xlsx'], key="order_uploader")
        uploaded_income = st.file_uploader("2. Laporan Penghasilan (Excel) *", type=['xlsx'], key="income_uploader")
        uploaded_hpp = st.file_uploader(
            "3. Laporan Master HPP Periode Ini (Opsional)", 
            type=['xlsx'],
            key="hpp_override_uploader",
            help="Unggah jika ingin memakai HPP periode khusus. Jika kosong, sistem otomatis memakai master HPP default toko."
        )

    if not uploaded_order or not uploaded_income:
        st.info("👈 **Silakan unggah Laporan Order dan Laporan Penghasilan Shopee di sidebar sebelah kiri** untuk memulai rekonsiliasi.")
    else:
        # ─── Date range picker ───
        if 'date_bounds' not in st.session_state:
            min_date, max_date = get_order_date_bounds(uploaded_order)
            uploaded_order.seek(0)
            st.session_state.date_bounds = (min_date, max_date)
        
        min_date, max_date = st.session_state.date_bounds

        with st.sidebar:
            st.divider()
            if min_date and max_date:
                st.subheader("📅 Rentang Tanggal")
                start_date = st.date_input("Tanggal Mulai", value=min_date, min_value=min_date, max_value=max_date, key="date_start")
                end_date = st.date_input("Tanggal Akhir", value=max_date, min_value=min_date, max_value=max_date, key="date_end")
            else:
                start_date = None
                end_date = None

            btn_proses = st.button("🚀 Proses Rekonsiliasi", key="btn_proses", type="primary", use_container_width=True)

        if btn_proses:
            uploaded_order.seek(0)
            uploaded_income.seek(0)
            with st.spinner('Memproses data transaksi...'):
                st.session_state.result = process_reconciliation(
                    uploaded_order, uploaded_income,
                    start_date=start_date, end_date=end_date
                )
                uploaded_income.seek(0)
                st.session_state.df_adjustments = extract_adjustments(uploaded_income)
                uploaded_order.seek(0)
                st.session_state.filter_options = get_order_filter_options(
                    uploaded_order, start_date=start_date, end_date=end_date
                )
                uploaded_order.seek(0)
                uploaded_income.seek(0)
                st.session_state.settlement_stats = get_settlement_stats(
                    uploaded_order, uploaded_income, start_date=start_date, end_date=end_date
                )
                st.session_state.processed_start_date = start_date
                st.session_state.processed_end_date = end_date

            if 'date_bounds' in st.session_state:
                del st.session_state['date_bounds']
            st.success("✅ Rekonsiliasi Selesai!")
        
        if 'result' in st.session_state:
            result = st.session_state.result
            df_adj = st.session_state.get('df_adjustments', pd.DataFrame())
            
            # ─── Filter Data ───
            st.subheader("🔍 Filter Data")
            f_col1, f_col2 = st.columns(2)
            filter_options = st.session_state.get('filter_options', {})
            
            with f_col1:
                allowed_filters = ['No. Pesanan', 'Nama Produk']
                available_filters = [col for col in allowed_filters if col in result.columns]
                if available_filters:
                    filter_col = st.selectbox("Filter berdasarkan:", available_filters)
                else:
                    filter_col = None

            with f_col2:
                if filter_col:
                    unique_values = filter_options.get(filter_col, sorted(result[filter_col].dropna().astype(str).unique().tolist()))
                    selected_values = st.multiselect(f"Pilih nilai untuk {filter_col}:", unique_values, default=[])
                    if selected_values:
                        filtered_result = result[result[filter_col].astype(str).isin([str(v) for v in selected_values])].copy()
                    else:
                        filtered_result = result.copy()
                else:
                    filtered_result = result.copy()

            # Reset dan sisipkan kolom 'No.' agar selalu berurutan 1..N
            if 'No.' in filtered_result.columns:
                filtered_result = filtered_result.drop(columns=['No.'])
            filtered_result.insert(0, 'No.', range(1, len(filtered_result) + 1))

            # ─── Ringkasan Finansial (HANYA dari pesanan yang SUDAH settlement) ───
            settled_result = filtered_result[filtered_result['Is_Settled'] == True].copy() if 'Is_Settled' in filtered_result.columns else filtered_result.copy()

            total_subtotal = int(settled_result['Subtotal'].sum())
            total_biaya = int(settled_result['Total Biaya'].sum())
            
            # Hitung rincian per komponen biaya (hanya dari yang sudah settlement)
            tot_adm = int(settled_result['Biaya Administrasi'].sum()) if 'Biaya Administrasi' in settled_result.columns else 0
            tot_xtra = int(settled_result['Biaya Gratis Ongkir XTRA'].sum()) if 'Biaya Gratis Ongkir XTRA' in settled_result.columns else 0
            tot_promo = int(settled_result['Biaya Promo XTRA'].sum()) if 'Biaya Promo XTRA' in settled_result.columns else 0
            tot_sub_biaya = int(settled_result['Subtotal Biaya'].sum()) if 'Subtotal Biaya' in settled_result.columns else (tot_adm + tot_xtra + tot_promo)
            tot_proses = int(settled_result['Biaya Proses Pesanan'].sum()) if 'Biaya Proses Pesanan' in settled_result.columns else 0
            tot_pajak = int(settled_result['Pajak'].sum()) if 'Pajak' in settled_result.columns else 0

            pct_adm = abs(tot_adm) / total_subtotal * 100 if total_subtotal > 0 else 0
            pct_xtra = abs(tot_xtra) / total_subtotal * 100 if total_subtotal > 0 else 0
            pct_promo = abs(tot_promo) / total_subtotal * 100 if total_subtotal > 0 else 0
            pct_sub_biaya = abs(tot_sub_biaya) / total_subtotal * 100 if total_subtotal > 0 else 0

            # Hitung total penyesuaian
            adj_orders_list = []
            if not df_adj.empty:
                active_orders = set(settled_result['No. Pesanan'].astype(str).unique())
                relevant_adj = df_adj[df_adj['No. Pesanan'].astype(str).isin(active_orders)]
                total_penyesuaian = int(relevant_adj['Biaya Penyesuaian'].sum()) if not relevant_adj.empty else 0
                adj_orders_list = [o for o in relevant_adj['No. Pesanan'].unique().tolist() if o and str(o) != 'nan']
            else:
                total_penyesuaian = 0
                relevant_adj = pd.DataFrame()

            total_penghasilan = total_subtotal + total_biaya + total_penyesuaian
            pct_biaya = abs(total_biaya) / total_subtotal * 100 if total_subtotal > 0 else 0

            # ─── Perhitungan Estimasi Potensi Penghasilan dari Unsettled Orders ───
            unsettled_result = filtered_result[filtered_result['Is_Settled'] == False].copy() if 'Is_Settled' in filtered_result.columns else pd.DataFrame()
            unsettled_subtotal = int(unsettled_result['Subtotal'].sum()) if not unsettled_result.empty else 0
            
            global_fee_ratio = (abs(total_biaya) / total_subtotal) if total_subtotal > 0 else 0.15
            
            if not unsettled_result.empty and not settled_result.empty:
                prod_fee_stats = settled_result.groupby('Nama Produk').apply(
                    lambda g: (abs(g['Total Biaya'].sum()) / g['Subtotal'].sum()) if g['Subtotal'].sum() > 0 else global_fee_ratio,
                    include_groups=False
                ).to_dict()

                def est_row_net(row):
                    p_name = row['Nama Produk']
                    sub = row['Subtotal']
                    fee_rate = prod_fee_stats.get(p_name, global_fee_ratio)
                    return sub * (1 - fee_rate)

                unsettled_result['Est_Net'] = unsettled_result.apply(est_row_net, axis=1)
                est_unsettled_net = int(round(unsettled_result['Est_Net'].sum()))
                effective_fee_ratio = (1 - (est_unsettled_net / unsettled_subtotal)) if unsettled_subtotal > 0 else global_fee_ratio
            else:
                effective_fee_ratio = global_fee_ratio
                est_unsettled_net = int(unsettled_subtotal * (1 - effective_fee_ratio))
            
            total_proyeksi_keseluruhan = total_penghasilan + est_unsettled_net

            # ─── Perhitungan HPP & Laba Bersih Toko (Multi-Satuan) ───
            hpp_source = uploaded_hpp if uploaded_hpp is not None else None
            if hpp_source is not None:
                hpp_source.seek(0)
            df_hpp_master = load_hpp_master(file_source=hpp_source)
            all_unique_prods = result['Nama Produk'].dropna().unique().tolist()
            mapping_dict = auto_suggest_mapping(all_unique_prods, df_hpp_master)
            
            hpp_by_key = {r['ItemKey']: r.to_dict() for _, r in df_hpp_master.iterrows()}
            hpp_lookup = {}
            for p_name, item_key in mapping_dict.items():
                if item_key in hpp_by_key:
                    hpp_lookup[p_name] = hpp_by_key[item_key]

            def get_item_hpp(row):
                p_name = row['Nama Produk']
                qty = row['Jumlah Bersih']
                info = hpp_lookup.get(p_name, {})
                harga = info.get('HargaPokok', 0)
                konv = info.get('Konversi', 1) or 1
                return qty * (harga / konv)

            if not settled_result.empty and hpp_lookup:
                total_hpp_settled = int(round(settled_result.apply(get_item_hpp, axis=1).sum()))
                laba_bersih_settled = total_penghasilan - total_hpp_settled
                margin_laba_settled = (laba_bersih_settled / total_subtotal * 100) if total_subtotal > 0 else 0.0

                hpp_ratio = total_hpp_settled / total_subtotal if total_subtotal > 0 else 0.0
                est_hpp_unsettled = int(round(unsettled_subtotal * hpp_ratio)) if unsettled_subtotal > 0 else 0
                total_hpp_proyeksi = total_hpp_settled + est_hpp_unsettled
            else:
                total_hpp_settled = 0
                laba_bersih_settled = total_penghasilan
                margin_laba_settled = 0.0
                total_hpp_proyeksi = 0
                est_hpp_unsettled = 0

            proc_start = st.session_state.get('processed_start_date')
            proc_end = st.session_state.get('processed_end_date')
            if proc_start and proc_end:
                num_days = (proc_end - proc_start).days + 1
            else:
                num_days = None
            avg_per_hari = total_penghasilan / num_days if num_days and num_days > 0 else None
            avg_per_hari_fmt = f"Rp {avg_per_hari:,.0f}" if avg_per_hari is not None else ""

            st.subheader("💰 Ringkasan Rekonsiliasi")

            # ─── Group 1: Status & Rasio Settlement (Dihitung Langsung dari Hasil Filter Aktif) ───
            total_orders_valid = len(filtered_result['No. Pesanan'].dropna().unique())
            settled_count = len(settled_result['No. Pesanan'].dropna().unique()) if not settled_result.empty else 0
            unsettled_count = len(unsettled_result['No. Pesanan'].dropna().unique()) if not unsettled_result.empty else 0
            settle_rate = (settled_count / total_orders_valid * 100) if total_orders_valid > 0 else 100.0
            unsettled_list = sorted(unsettled_result['No. Pesanan'].dropna().unique().tolist()) if not unsettled_result.empty else []

            settle_color = "#4ade80" if settle_rate == 100 else ("#facc15" if settle_rate >= 80 else "#f87171")
            settle_pct_color = "#86efac" if settle_rate == 100 else ("#fde047" if settle_rate >= 80 else "#fca5a5")

            # ─── Group 2: Realisasi Settled (Dana Sudah Cair) ───
            gross_pct_label = "Subtotal Penjualan (Gross)"
            gross_card = (
                '<div class="summary-card card-gross">'
                f'<div class="label">{gross_pct_label}</div>'
                f'<div class="value">Rp {total_subtotal:,.0f}</div>'
                '<div class="pct">Nilai Penjualan Produk Settled</div>'
                '</div>'
            )
            fees_card = (
                '<div class="summary-card card-fees">'
                '<div class="label">Total Biaya Layanan</div>'
                f'<div class="value">Rp {total_biaya:,.0f}</div>'
                f'<div class="pct">Potongan Biaya: {pct_biaya:.1f}%</div>'
                '</div>'
            )
            adj_card = ""
            if total_penyesuaian != 0:
                adj_color = "#4ade80" if total_penyesuaian > 0 else "#f87171"
                adj_sign = "+" if total_penyesuaian > 0 else ""
                adj_card = (
                    '<div class="summary-card card-adj">'
                    '<div class="label">Total Penyesuaian</div>'
                    f'<div class="value" style="color: {adj_color};">{adj_sign}Rp {total_penyesuaian:,.0f}</div>'
                    f'<div class="pct">{len(adj_orders_list)} pesanan disesuaikan</div>'
                    '</div>'
                )
            net_card = (
                '<div class="summary-card card-net">'
                '<div class="label">Penghasilan Bersih (Net)</div>'
                f'<div class="value">Rp {total_penghasilan:,.0f}</div>'
                '<div class="pct">Dana Sudah Dilepas Shopee</div>'
                '</div>'
            )
            hpp_card = ""
            laba_card = ""
            if total_hpp_settled > 0:
                hpp_card = (
                    '<div class="summary-card card-hpp">'
                    '<div class="label">Total Modal (HPP)</div>'
                    f'<div class="value">Rp {total_hpp_settled:,.0f}</div>'
                    f'<div class="pct">HPP Produk Terjual</div>'
                    '</div>'
                )
                laba_color = "#10b981" if laba_bersih_settled >= 0 else "#f87171"
                laba_card = (
                    '<div class="summary-card card-laba">'
                    '<div class="label">Laba Bersih Real (Profit)</div>'
                    f'<div class="value" style="color: {laba_color};">Rp {laba_bersih_settled:,.0f}</div>'
                    f'<div class="pct">Margin Bersih: {margin_laba_settled:.1f}%</div>'
                    '</div>'
                )
            daily_card = ""
            if avg_per_hari is not None:
                daily_card = (
                    '<div class="summary-card card-daily">'
                    '<div class="label">Penghasilan Real / Hari</div>'
                    f'<div class="value">{avg_per_hari_fmt}</div>'
                    f'<div class="pct">Real Settled ({num_days} hari)</div>'
                    '</div>'
                )

            # ─── Group 3: Estimasi Pending & Total Proyeksi ───
            potential_card = ""
            grand_total_card = ""
            laba_proyeksi_card = ""
            daily_proj_card = ""

            if not unsettled_result.empty:
                potential_card = (
                    '<div class="summary-card card-potential">'
                    '<div class="label">Estimasi Potensi Pending</div>'
                    f'<div class="value">Rp {est_unsettled_net:,.0f}</div>'
                    f'<div class="pct">Subtotal Rp {unsettled_subtotal:,.0f} (est. fee {effective_fee_ratio*100:.1f}%)</div>'
                    '</div>'
                )
                grand_total_card = (
                    '<div class="summary-card card-grand">'
                    '<div class="label">Total Proyeksi Bersih</div>'
                    f'<div class="value">Rp {total_proyeksi_keseluruhan:,.0f}</div>'
                    '<div class="pct">Settled + Estimasi Pending</div>'
                    '</div>'
                )
                if total_hpp_proyeksi > 0:
                    laba_proyeksi = total_proyeksi_keseluruhan - total_hpp_proyeksi
                    margin_proyeksi = (laba_proyeksi / total_proyeksi_keseluruhan * 100) if total_proyeksi_keseluruhan > 0 else 0.0
                    laba_proj_color = "#10b981" if laba_proyeksi >= 0 else "#f87171"
                    laba_proyeksi_card = (
                        '<div class="summary-card card-laba">'
                        '<div class="label">Proyeksi Laba Bersih</div>'
                        f'<div class="value" style="color: {laba_proj_color};">Rp {laba_proyeksi:,.0f}</div>'
                        f'<div class="pct">Margin Proyeksi: {margin_proyeksi:.1f}% | Est. HPP Pending: Rp {est_hpp_unsettled:,.0f}</div>'
                        '</div>'
                    )
                if avg_per_hari is not None:
                    avg_proj_per_hari = total_proyeksi_keseluruhan / num_days if num_days and num_days > 0 else total_proyeksi_keseluruhan
                    daily_proj_card = (
                        '<div class="summary-card card-grand">'
                        '<div class="label">Proyeksi Bersih / Hari</div>'
                        f'<div class="value">Rp {avg_proj_per_hari:,.0f}</div>'
                        f'<div class="pct">Proyeksi Total ({num_days} hari)</div>'
                        '</div>'
                    )

            # ─── Render HTML Terstruktur Berdasarkan Settlement ───
            st.markdown("### 💰 Ringkasan Finansial Rekonsiliasi")

            # Bagian 1: Realisasi Pesanan Selesai (Settled)
            settled_cards_html = gross_card + fees_card + adj_card + net_card + hpp_card + laba_card + daily_card
            settled_html = (
                '<div class="section-group">'
                '<div class="section-header">'
                '<div class="section-title"><span>✅</span> Realisasi Penjualan & Laba Selesai (Settled)</div>'
                f'<div class="section-badge badge-settled">{settled_count}/{total_orders_valid} Pesanan Cair ({settle_rate:.1f}%)</div>'
                '</div>'
                f'<div class="summary-container">{settled_cards_html}</div>'
                '</div>'
            )
            st.markdown(settled_html, unsafe_allow_html=True)

            # Bagian 2: Estimasi Pending & Proyeksi Keseluruhan (Hanya jika ada unsettled)
            if not unsettled_result.empty:
                pending_cards_html = potential_card + grand_total_card + laba_proyeksi_card + daily_proj_card
                pending_html = (
                    '<div class="section-group">'
                    '<div class="section-header">'
                    '<div class="section-title"><span>⏳</span> Estimasi Pending & Total Proyeksi Toko</div>'
                    f'<div class="section-badge badge-pending">{unsettled_count} Pesanan Belum Settlement (Dana Tertahan)</div>'
                    '</div>'
                    f'<div class="summary-container">{pending_cards_html}</div>'
                    '</div>'
                )
                st.markdown(pending_html, unsafe_allow_html=True)

            # ─── Rincian Komponen Biaya ───
            with st.expander("📊 Rincian Detail Komponen Biaya", expanded=True):
                st.markdown(f"""
                <div class="breakdown-container">
                    <div class="breakdown-card">
                        <div class="title">Biaya Administrasi</div>
                        <div class="val">Rp {tot_adm:,.0f}</div>
                        <div class="sub">{pct_adm:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Gratis Ongkir XTRA</div>
                        <div class="val">Rp {tot_xtra:,.0f}</div>
                        <div class="sub">{pct_xtra:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Biaya Promo XTRA</div>
                        <div class="val">Rp {tot_promo:,.0f}</div>
                        <div class="sub">{pct_promo:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Subtotal Biaya</div>
                        <div class="val">Rp {tot_sub_biaya:,.0f}</div>
                        <div class="sub">{pct_sub_biaya:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Proses Pesanan</div>
                        <div class="val">Rp {tot_proses:,.0f}</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Pajak (PPh 22)</div>
                        <div class="val">Rp {tot_pajak:,.0f}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Highlight Pesanan Belum Settlement ───
            if unsettled_list:
                st.markdown("##### ⏳ Pesanan Belum Settlement (Dana Belum Dilepas Shopee)")
                unsettled_badges_html = " ".join([f'<span class="unsettled-badge">⏳ {order}</span>' for order in unsettled_list])
                st.markdown(f"""
                <div style="background: rgba(239, 68, 68, 0.08); border-left: 4px solid #ef4444; padding: 0.8rem 1.2rem; border-radius: 8px; margin-bottom: 1.2rem;">
                    <div style="font-size: 0.88rem; color: #fca5a5; font-weight: 600; margin-bottom: 0.4rem;">
                        Ditemukan {unsettled_count} pesanan ({100 - settle_rate:.1f}%) yang belum settlement:
                    </div>
                    <div style="font-size: 0.82rem; color: #f87171; margin-bottom: 0.6rem;">
                        • Subtotal Gross Pending: <b>Rp {unsettled_subtotal:,.0f}</b><br>
                        • Estimasi Bersih Cair (setelah est. fee {effective_fee_ratio*100:.1f}%): <b>Rp {est_unsettled_net:,.0f}</b>
                    </div>
                    <div>{unsettled_badges_html}</div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Highlight Pesanan dengan Penyesuaian ───
            if adj_orders_list:
                st.markdown("##### ⚠️ Highlight Pesanan dengan Biaya Penyesuaian (Adjustment)")
                badges_html = " ".join([f'<span class="adj-badge">📦 {order}</span>' for order in adj_orders_list])
                st.markdown(f"""
                <div style="background: rgba(234, 179, 8, 0.08); border-left: 4px solid #eab308; padding: 0.8rem 1.2rem; border-radius: 8px; margin-bottom: 1.2rem;">
                    <div style="font-size: 0.88rem; color: #fde047; font-weight: 600; margin-bottom: 0.4rem;">
                        Ditemukan {len(adj_orders_list)} pesanan yang memiliki potongan/penyesuaian saldo setelah dana dilepas:
                    </div>
                    <div>{badges_html}</div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Tabel Detail Produk ───
            st.subheader("📋 Detail Data Transaksi & Produk")
            legends = []
            if 'Returned quantity' in filtered_result.columns and (filtered_result['Returned quantity'] > 0).any():
                legends.append("🟡 **Kuning**: Retur / Penyesuaian (Returned quantity > 0)")
            if 'Is_Settled' in filtered_result.columns and (~filtered_result['Is_Settled']).any():
                legends.append("🔴 **Merah**: Belum Settlement (Dana belum dilepas di Laporan Penghasilan)")
            if legends:
                st.caption(" | ".join(legends))

            display_df = filtered_result.copy()

            def highlight_rows(row):
                is_settled = row.get('Is_Settled', True)
                ret_qty = row.get('Returned quantity', 0)
                
                if not is_settled:
                    return ['background-color: rgba(239, 68, 68, 0.18); color: #fca5a5; font-weight: 600;'] * len(row)
                if pd.notna(ret_qty) and ret_qty > 0:
                    return ['background-color: rgba(234, 179, 8, 0.22); color: #fef08a; font-weight: 600;'] * len(row)
                return [''] * len(row)

            styled_df = display_df.style.apply(highlight_rows, axis=1)

            cols_config = {
                'Is_Settled': None,
                COL_PCT_ADM: st.column_config.NumberColumn("(%)", format="%.2f%%"),
                COL_PCT_XTRA: st.column_config.NumberColumn("(%) ", format="%.2f%%"),
                COL_PCT_PROMO: st.column_config.NumberColumn("(%)  ", format="%.2f%%"),
                COL_PCT_SUB_BIAYA: st.column_config.NumberColumn("(%)   ", format="%.2f%%"),
                'Jumlah': st.column_config.NumberColumn("Jumlah (Gross)", format="%d", help="Jumlah unit yang dipesan pembeli awal"),
                'Returned quantity': st.column_config.NumberColumn("Retur (Qty)", format="%d", help="Jumlah unit yang diretur pembeli"),
                'Jumlah Bersih': st.column_config.NumberColumn("Jumlah Bersih (Unit)", format="%d", help="Kuantitas fisik real terjual (Jumlah - Retur). Dipakai untuk dasar modal HPP."),
                'Subtotal': st.column_config.NumberColumn("Subtotal (Gross Sales)", format="%,d", help="Nilai transaksi kotor awal (Jumlah × Harga). Potongan pengembalian dana retur dicatat pada tabel Penyesuaian (Adjustment)."),
            }
            
            thousand_cols = [
                'Harga (@)', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal', 'Biaya Administrasi', 
                'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Subtotal Biaya', 
                'Biaya Proses Pesanan', 'Total Biaya', 'Pajak'
            ]
            for col in thousand_cols:
                if col in display_df.columns and col not in ['Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal']:
                    cols_config[col] = st.column_config.NumberColumn(col, format="%,d")

            st.dataframe(
                styled_df, 
                use_container_width=True, 
                hide_index=True,
                column_config=cols_config
            )
            
            # ─── Tabel Detail Penyesuaian (Adjustment) ───
            if not relevant_adj.empty:
                st.subheader("⚖️ Detail Penyesuaian (Adjustment)")
                st.caption("Penyesuaian saldo / pengembalian dana setelah dana dilepaskan berdasarkan No. Pesanan")
                adj_cols_config = {
                    'Biaya Penyesuaian': st.column_config.NumberColumn("Biaya Penyesuaian", format="%,d")
                }
                st.dataframe(
                    relevant_adj,
                    use_container_width=True,
                    hide_index=True,
                    column_config=adj_cols_config
                )

            # ─── Tabel Rekapitulasi Produk ───
            df_product_summary = generate_product_summary(filtered_result, hpp_lookup=hpp_lookup)
            if not df_product_summary.empty:
                with st.expander("📦 Rekapitulasi Penjualan & Margin Laba per Produk (Sudah Settlement)", expanded=True):
                    st.caption("Grouping berdasarkan Nama Produk dan Harga (@). **Total Penjualan (Gross Sales)** diambil dari akumulasi Subtotal riil transaksi, sedangkan **Total HPP** dihitung dari Kuantitas Bersih fisik.")
                    prod_cols_config = {
                        'Total Jumlah Bersih': st.column_config.NumberColumn("Qty Terjual Bersih", format="%d", help="Total kuantitas barang fisik yang tidak diretur (basis kalkulasi Total HPP)"),
                        'Harga (@)': st.column_config.NumberColumn("Harga (@)", format="%,d", help="Harga jual satuan produk"),
                        'Total Penjualan Bersih': st.column_config.NumberColumn("Total Penjualan (Gross Sales)", format="%,d", help="Total nilai penjualan kotor riil transaksi (akumulasi Subtotal transaksi)"),
                        'HPP (@)': st.column_config.NumberColumn("HPP (@)", format="%,d", help="Harga pokok per unit terjual"),
                        'Total HPP': st.column_config.NumberColumn("Total HPP", format="%,d", help="Total modal barang = Qty Terjual Bersih × HPP (@)"),
                        'Laba Bersih': st.column_config.NumberColumn("Laba Bersih", format="%,d", help="Total Penjualan + Total Biaya Shopee - Total HPP"),
                        'Margin Laba (%)': st.column_config.NumberColumn("Margin (%)", format="%.2f%%")
                    }
                    st.dataframe(
                        df_product_summary,
                        use_container_width=True,
                        hide_index=True,
                        column_config=prod_cols_config
                    )

            # ─── Export Excel ───
            final_result_excel = add_total_row(filtered_result)

            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
                final_result_excel.to_excel(writer, sheet_name='Hasil Rekonsiliasi', index=False)
                if not df_product_summary.empty:
                    df_product_summary.to_excel(writer, sheet_name='Rekap Produk & HPP', index=False)
                if not relevant_adj.empty:
                    relevant_adj.to_excel(writer, sheet_name='Penyesuaian (Adjustment)', index=False)
            
            st.divider()
            st.download_button(
                label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
                data=buffer,
                file_name="hasil_rekonsiliasi.xlsx",
                mime="application/vnd.ms-excel",
                type="primary"
            )


# ==============================================================================
# 📦 MENU 2: KELOLA MASTER HPP
# ==============================================================================
elif menu == "📦 Kelola Master HPP":
    st.title("📦 Kelola Master HPP & Pemetaan Multi-Satuan")
    st.write("Kelola database harga pokok toko, satuan/konversi kemasan, dan relasi pemetaan SKU Shopee.")

    # Load master HPP data
    hpp_excel_path = "files/hpp_produk.xlsx"
    df_hpp_master = load_hpp_master(file_source=hpp_excel_path)
    mapping_dict = load_mapping()

    tab_mapping, tab_master = st.tabs([
        "🔗 Pemetaan SKU Shopee ↔ Master HPP", 
        "📋 Master Database HPP Toko"
    ])

    # ─── TAB 1: PEMETAAN SKU SHOPEE ───
    with tab_mapping:
        st.subheader("🔗 Pemetaan SKU Shopee ke Satuan Master HPP")
        st.caption("Petakan setiap produk di toko Shopee ke satuan master yang tepat (PCS, RTG, PAK, DUS). Anda juga dapat langsung mengedit nilai HPP/Unit.")

        # Ambil daftar produk yang sudah ada di mapping atau dari file rekonsiliasi jika ada
        all_prods_set = set(mapping_dict.keys())
        if 'result' in st.session_state and not st.session_state.result.empty:
            all_prods_set.update(st.session_state.result['Nama Produk'].dropna().unique().tolist())
        
        all_prods_list = sorted(list(all_prods_set))

        if not all_prods_list:
            st.info("💡 Belum ada produk Shopee yang tercatat. Silakan jalankan rekonsiliasi terlebih dahulu atau tambahkan mapping.")
        else:
            BELUM_DIPETAKAN = "(Belum Dipetakan)"
            hpp_options_list = [BELUM_DIPETAKAN] + [
                f"{r['KodeItem']} - {r['NamaItem']} [{r['Satuan']} (isi {r['Konversi']:g})] (HPP: Rp {r['HargaPokok']:,.0f})"
                for _, r in df_hpp_master.iterrows()
            ]
            key_to_label = {
                r['ItemKey']: f"{r['KodeItem']} - {r['NamaItem']} [{r['Satuan']} (isi {r['Konversi']:g})] (HPP: Rp {r['HargaPokok']:,.0f})"
                for _, r in df_hpp_master.iterrows()
            }
            label_to_key = {v: k for k, v in key_to_label.items()}

            key_to_hpp = {
                r['ItemKey']: round(r['HargaPokok'] / (r['Konversi'] or 1))
                for _, r in df_hpp_master.iterrows()
            }

            table_rows = []
            needs_confirm_count = 0
            for prod in all_prods_list:
                # Source of Truth Akuntansi: Hanya mapping yang SUDAH dikonfirmasi (tercatat di mapping_dict)
                confirmed_key = mapping_dict.get(prod, '')
                
                if confirmed_key:
                    cur_label = key_to_label.get(confirmed_key, BELUM_DIPETAKAN)
                    match_status = "✅ Terpetakan"
                    # HPP dihitung HANYA dari confirmed_key
                    hpp_val = key_to_hpp.get(confirmed_key, None)
                else:
                    # Belum ada mapping terkonfirmasi: Cek apakah ada saran fuzzy matching
                    sugg_key, sugg_score, _ = get_suggestion_with_confidence(prod, df_hpp_master)
                    if sugg_score >= 0.70 and sugg_key:
                        # 70-89%: Pasang suggestion di dropdown agar siap di-review user, TAPI HPP tetap None (tidak masuk akuntansi sebelum disimpan)
                        cur_label = key_to_label.get(sugg_key, BELUM_DIPETAKAN)
                        match_status = f"🔍 Rekomendasi ({int(sugg_score*100)}%)"
                        needs_confirm_count += 1
                        hpp_val = None
                    else:
                        # < 70%: Unmapped
                        cur_label = BELUM_DIPETAKAN
                        match_status = "❌ Belum Terpetakan"
                        hpp_val = None

                table_rows.append({
                    'Status': match_status,
                    'Nama Produk (Shopee)': prod,
                    'Pemetaan HPP (ItemKey & Satuan)': cur_label,
                    'HPP (@)': hpp_val,
                })
            mapping_df = pd.DataFrame(table_rows)

            # Highlight info bar
            unmapped_count = sum(1 for r in table_rows if r['Status'] == "❌ Belum Terpetakan")
            zero_hpp_count = sum(1 for r in table_rows if r['HPP (@)'] == 0 or r['HPP (@)'] is None)

            m_c1, m_c2, m_c3, m_c4 = st.columns(4)
            with m_c1:
                st.metric("Total Produk Terdaftar", f"{len(table_rows)} SKU")
            with m_c2:
                st.metric("Perlu Konfirmasi", f"{needs_confirm_count} SKU", delta=f"{needs_confirm_count} Rekomendasi" if needs_confirm_count > 0 else "0", delta_color="normal")
            with m_c3:
                st.metric("Belum Terpetakan (<70%)", f"{unmapped_count} SKU", delta=f"-{unmapped_count}" if unmapped_count > 0 else "Semua Beres", delta_color="inverse")
            with m_c4:
                st.metric("HPP Kosong / 0", f"{zero_hpp_count} SKU", delta=f"-{zero_hpp_count}" if zero_hpp_count > 0 else "Aman", delta_color="inverse")

            edited_df = st.data_editor(
                mapping_df,
                column_config={
                    'Status': st.column_config.TextColumn(
                        "Status", disabled=True, width="small"
                    ),
                    'Nama Produk (Shopee)': st.column_config.TextColumn(
                        "Nama Produk (Shopee)", disabled=True, width="large"
                    ),
                    'Pemetaan HPP (ItemKey & Satuan)': st.column_config.SelectboxColumn(
                        "Pemetaan HPP (Item, Satuan, HPP)",
                        options=hpp_options_list,
                        required=True,
                        width="large",
                    ),
                    'HPP (@)': st.column_config.NumberColumn(
                        "HPP/Unit (Rp) ✏️",
                        format="%,d",
                        min_value=0,
                        step=1,
                        width="small",
                        help="HPP per unit terjual (setelah konversi). Edit langsung untuk update master HPP.",
                    ),
                },
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                key="bulk_mapping_editor_standalone",
            )

            if st.button("💾 Simpan Semua Perubahan Pemetaan", key="btn_save_mapping_page", type="primary"):
                mapping_changed = 0
                hpp_changed = 0

                df_hpp_edit = pd.read_excel(hpp_excel_path)
                df_hpp_edit['ItemKey'] = df_hpp_edit['KodeItem'].astype(str) + '_' + df_hpp_edit['Satuan'].astype(str)

                for _, row in edited_df.iterrows():
                    prod_name = row['Nama Produk (Shopee)']
                    chosen_label = row['Pemetaan HPP (ItemKey & Satuan)']
                    new_hpp_unit = row['HPP (@)']

                    if chosen_label == BELUM_DIPETAKAN:
                        active_key = ''
                    else:
                        active_key = label_to_key.get(chosen_label, mapping_dict.get(prod_name, ''))

                    if chosen_label == BELUM_DIPETAKAN:
                        if prod_name in mapping_dict:
                            del mapping_dict[prod_name]
                            mapping_changed += 1
                    else:
                        if active_key and mapping_dict.get(prod_name) != active_key:
                            mapping_dict[prod_name] = active_key
                            mapping_changed += 1

                    if active_key and pd.notna(new_hpp_unit):
                        new_hpp_unit = int(round(new_hpp_unit))
                        old_hpp_unit = key_to_hpp.get(active_key)
                        if old_hpp_unit is None or new_hpp_unit != int(round(old_hpp_unit)):
                            mask = df_hpp_edit['ItemKey'] == active_key
                            if mask.any():
                                konv = df_hpp_edit.loc[mask, 'Konversi'].iloc[0] or 1
                                new_harga_pokok = round(new_hpp_unit * konv, 2)
                                df_hpp_edit.loc[mask, 'HargaPokok'] = new_harga_pokok
                                hpp_changed += 1

                save_mapping(mapping_dict)

                if hpp_changed > 0:
                    df_hpp_edit.drop(columns=['ItemKey'], inplace=True)
                    df_hpp_edit.to_excel(hpp_excel_path, index=False)

                total_changed = mapping_changed + hpp_changed
                if total_changed > 0:
                    msgs = []
                    if mapping_changed:
                        msgs.append(f"{mapping_changed} pemetaan")
                    if hpp_changed:
                        msgs.append(f"{hpp_changed} nilai HPP di master Excel")
                    st.success(f"✅ Berhasil disimpan: {' dan '.join(msgs)}!")
                    st.rerun()
                else:
                    st.info("Tidak ada perubahan yang terdeteksi.")

    # ─── TAB 2: MASTER DATABASE HPP TOKO ───
    with tab_master:
        st.subheader("📋 Master Database HPP Produk")
        st.caption("Database utama harga pokok multi-satuan toko (`files/hpp_produk.xlsx`). Anda dapat mengedit harga/konversi langsung di tabel atau upload file Excel baru.")

        with st.sidebar:
            st.subheader("📤 Upload Master HPP Baru")
            uploaded_new_master = st.file_uploader("Upload Excel Master HPP Baru", type=['xlsx'], key="new_master_uploader")
            if uploaded_new_master:
                if st.button("📥 Timpa Master HPP dengan File Ini", type="secondary"):
                    df_new = pd.read_excel(uploaded_new_master)
                    df_new.to_excel(hpp_excel_path, index=False)
                    st.success("✅ File master HPP berhasil diperbarui!")
                    st.rerun()

        # Baca raw master Excel
        df_raw_master = pd.read_excel(hpp_excel_path)
        
        col_m_info1, col_m_info2 = st.columns([3, 1])
        with col_m_info1:
            st.write(f"Total baris data: **{len(df_raw_master)} item/satuan**")
        with col_m_info2:
            # Download file master saat ini
            buffer_master = io.BytesIO()
            df_raw_master.to_excel(buffer_master, index=False)
            st.download_button(
                "⬇️ Download Master Excel",
                data=buffer_master.getvalue(),
                file_name="master_hpp_produk.xlsx",
                mime="application/vnd.ms-excel",
                use_container_width=True
            )

        edited_master_df = st.data_editor(
            df_raw_master,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            key="master_hpp_data_editor",
            column_config={
                'KodeItem': st.column_config.TextColumn("Kode Item", required=True),
                'NamaItem': st.column_config.TextColumn("Nama Item", required=True, width="large"),
                'Satuan': st.column_config.TextColumn("Satuan", required=True, width="small"),
                'Konversi': st.column_config.NumberColumn("Konversi (Isi)", min_value=1.0, step=1.0, format="%.0f"),
                'HargaPokok': st.column_config.NumberColumn("Harga Pokok (Rp)", format="%,d", min_value=0),
                'HargaJual': st.column_config.NumberColumn("Harga Jual (Rp)", format="%,d", min_value=0),
            }
        )

        if st.button("💾 Simpan Perubahan Master Database", key="btn_save_master_db", type="primary"):
            try:
                edited_master_df.to_excel(hpp_excel_path, index=False)
                st.success("✅ Master Database HPP berhasil disimpan!")
                st.rerun()
            except Exception as e:
                st.error(f"❌ Gagal menyimpan data: {e}")
