import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
    get_settlement_stats, generate_product_summary,
    COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
)
from hpp_manager import (
    load_hpp_master, load_mapping, save_mapping, auto_suggest_mapping
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

/* Tag / Highlight */
.adj-badge {
    display: inline-block;
    background: rgba(234, 179, 8, 0.15);
    color: #facc15;
    border: 1px solid rgba(234, 179, 8, 0.35);
    padding: 0.2rem 0.5rem;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    margin: 0.2rem 0.3rem 0.2rem 0;
}
</style>
""", unsafe_allow_html=True)

u_col1, u_col2 = st.columns(2)
with u_col1:
    uploaded_order = st.file_uploader("1. Pilih Laporan Order (Excel) *", type=['xlsx'])
with u_col2:
    uploaded_income = st.file_uploader("2. Pilih Laporan Penghasilan (Excel) *", type=['xlsx'])

uploaded_hpp = st.file_uploader(
    "3. Pilih Laporan Master HPP Rata-rata Periode Terkait (Opsional - default: files/hpp_produk.xlsx)", 
    type=['xlsx'],
    help="Unggah file HPP jika ingin menggunakan harga pokok rata-rata spesifik periode ini. Jika kosong, sistem otomatis memakai master HPP default."
)

if uploaded_order and uploaded_income:
    # ─── Date range picker ───
    # Load date bounds from Order file
    if 'date_bounds' not in st.session_state:
        min_date, max_date = get_order_date_bounds(uploaded_order)
        uploaded_order.seek(0)  # Reset file pointer after reading
        st.session_state.date_bounds = (min_date, max_date)
    
    min_date, max_date = st.session_state.date_bounds

    if min_date and max_date:
        st.subheader("📅 Rentang Tanggal Laporan")
        col_d1, col_d2 = st.columns(2)
        with col_d1:
            start_date = st.date_input("Tanggal Mulai", value=min_date, min_value=min_date, max_value=max_date)
        with col_d2:
            end_date = st.date_input("Tanggal Akhir", value=max_date, min_value=min_date, max_value=max_date)
    else:
        start_date = None
        end_date = None

    # Cek apakah tombol ditekan
    if st.button("🚀 Proses Rekonsiliasi", key="btn_proses"):
        uploaded_order.seek(0)  # Reset file pointer
        uploaded_income.seek(0)
        with st.spinner('Memproses data...'):
            st.session_state.result = process_reconciliation(
                uploaded_order, uploaded_income,
                start_date=start_date, end_date=end_date
            )
            # Ambil data penyesuaian (Adjustment) dari sheet Adjustment
            uploaded_income.seek(0)
            st.session_state.df_adjustments = extract_adjustments(uploaded_income)
            # Ambil opsi filter langsung dari file order (sesuai date range & status valid)
            uploaded_order.seek(0)
            st.session_state.filter_options = get_order_filter_options(
                uploaded_order, start_date=start_date, end_date=end_date
            )
            # Hitung statistik settlement
            uploaded_order.seek(0)
            uploaded_income.seek(0)
            st.session_state.settlement_stats = get_settlement_stats(
                uploaded_order, uploaded_income, start_date=start_date, end_date=end_date
            )
            # Simpan rentang tanggal yang diproses ke session_state
            st.session_state.processed_start_date = start_date
            st.session_state.processed_end_date = end_date

        # Reset date bounds cache saat proses ulang
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

        # Hitung total penyesuaian — selalu filter berdasarkan No. Pesanan yang aktif di settled_result
        adj_orders_list = []
        if not df_adj.empty:
            active_orders = set(settled_result['No. Pesanan'].astype(str).unique())
            relevant_adj = df_adj[df_adj['No. Pesanan'].astype(str).isin(active_orders)]
            total_penyesuaian = int(relevant_adj['Biaya Penyesuaian'].sum()) if not relevant_adj.empty else 0
            adj_orders_list = [o for o in relevant_adj['No. Pesanan'].unique().tolist() if o and str(o) != 'nan']
        else:
            total_penyesuaian = 0
            relevant_adj = pd.DataFrame()

        # Total Penghasilan Bersih (Settled) = Subtotal + Total Biaya + Total Penyesuaian
        total_penghasilan = total_subtotal + total_biaya + total_penyesuaian
        pct_biaya = abs(total_biaya) / total_subtotal * 100 if total_subtotal > 0 else 0

        # ─── Perhitungan Estimasi Potensi Penghasilan dari Unsettled Orders (Per-Produk) ───
        unsettled_result = filtered_result[filtered_result['Is_Settled'] == False].copy() if 'Is_Settled' in filtered_result.columns else pd.DataFrame()
        unsettled_subtotal = int(unsettled_result['Subtotal'].sum()) if not unsettled_result.empty else 0
        
        # Rasio fee global toko sebagai fallback
        global_fee_ratio = (abs(total_biaya) / total_subtotal) if total_subtotal > 0 else 0.15
        
        if not unsettled_result.empty and not settled_result.empty:
            # 1. Pelajari persentase fee fixed per Nama Produk dari data yang sudah settled
            # Rumus per produk: |Total Biaya Produk| / Subtotal Produk
            prod_fee_stats = settled_result.groupby('Nama Produk').apply(
                lambda g: (abs(g['Total Biaya'].sum()) / g['Subtotal'].sum()) if g['Subtotal'].sum() > 0 else global_fee_ratio,
                include_groups=False
            ).to_dict()

            # 2. Hitung estimasi fee per baris pesanan unsettled sesuai Nama Produk masing-masing
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
        
        # Total Keseluruhan (Settled Real + Estimasi Unsettled per Produk)
        total_proyeksi_keseluruhan = total_penghasilan + est_unsettled_net

        # ─── Perhitungan HPP & Laba Bersih Toko (Berdasarkan Mapping HPP Opsi B Multi-Satuan) ───
        hpp_source = uploaded_hpp if uploaded_hpp is not None else None
        if hpp_source is not None:
            hpp_source.seek(0)
        df_hpp_master = load_hpp_master(file_source=hpp_source)
        all_unique_prods = result['Nama Produk'].dropna().unique().tolist()
        mapping_dict = auto_suggest_mapping(all_unique_prods, df_hpp_master)
        
        # Buat dictionary lookup harga pokok per ItemKey (KodeItem_Satuan)
        hpp_by_key = {r['ItemKey']: r.to_dict() for _, r in df_hpp_master.iterrows()}
        hpp_lookup = {}
        for p_name, item_key in mapping_dict.items():
            if item_key in hpp_by_key:
                hpp_lookup[p_name] = hpp_by_key[item_key]

        # Hitung Total HPP dari pesanan settled
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

            # Estimasi HPP untuk pesanan pending (gunakan rasio HPP/subtotal dari settled)
            hpp_ratio = total_hpp_settled / total_subtotal if total_subtotal > 0 else 0.0
            est_hpp_unsettled = int(round(unsettled_subtotal * hpp_ratio)) if unsettled_subtotal > 0 else 0
            total_hpp_proyeksi = total_hpp_settled + est_hpp_unsettled
        else:
            total_hpp_settled = 0
            laba_bersih_settled = total_penghasilan
            margin_laba_settled = 0.0
            total_hpp_proyeksi = 0
            est_hpp_unsettled = 0

        # Hitung rata-rata penghasilan per hari berdasarkan rentang tanggal yang diproses
        proc_start = st.session_state.get('processed_start_date')
        proc_end = st.session_state.get('processed_end_date')
        if proc_start and proc_end:
            from datetime import date as date_type
            num_days = (proc_end - proc_start).days + 1
        else:
            num_days = None
        avg_per_hari = total_penghasilan / num_days if num_days and num_days > 0 else None
        avg_per_hari_fmt = f"Rp {avg_per_hari:,.0f}" if avg_per_hari is not None else ""

        st.subheader("💰 Ringkasan Rekonsiliasi")
        # Label rentang hari
        if num_days:
            period_label = f"{proc_start.strftime('%d %b %Y')} – {proc_end.strftime('%d %b %Y')} ({num_days} hari)"
        else:
            period_label = ""
        if period_label:
            st.caption(f"📅 Periode: **{period_label}**")

        # Ambil statistik settlement
        settle_stats = st.session_state.get('settlement_stats', {})
        settle_rate = settle_stats.get('settlement_rate', 100.0)
        unsettled_count = settle_stats.get('unsettled_orders', 0)
        settled_count = settle_stats.get('settled_orders', 0)
        total_orders_valid = settle_stats.get('total_orders', 0)
        unsettled_list = settle_stats.get('unsettled_list', [])

        settle_color = "#4ade80" if settle_rate == 100 else ("#facc15" if settle_rate >= 80 else "#f87171")
        settle_pct_color = "#86efac" if settle_rate == 100 else ("#fde047" if settle_rate >= 80 else "#fca5a5")

        # Kartu-kartu summary (dibuat single-line agar tidak memicu code block di Streamlit markdown)
        gross_card = (
            '<div class="summary-card card-gross">'
            '<div class="label">Total Subtotal (Gross)</div>'
            f'<div class="value">Rp {total_subtotal:,.0f}</div>'
            '</div>'
        )
        fees_card = (
            '<div class="summary-card card-fees">'
            '<div class="label">Total Biaya (Fees)</div>'
            f'<div class="value">Rp {total_biaya:,.0f}</div>'
            f'<div class="pct">{pct_biaya:.1f}% dari Subtotal</div>'
            '</div>'
        )
        adj_card = (
            '<div class="summary-card card-adj">'
            '<div class="label">Total Penyesuaian (Adjustment)</div>'
            f'<div class="value">Rp {total_penyesuaian:,.0f}</div>'
            '</div>'
        )
        net_card = (
            '<div class="summary-card card-net">'
            '<div class="label">Penghasilan Bersih (Settled)</div>'
            f'<div class="value">Rp {total_penghasilan:,.0f}</div>'
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
                '<div class="label">Laba Bersih Real (Net Profit)</div>'
                f'<div class="value" style="color: {laba_color};">Rp {laba_bersih_settled:,.0f}</div>'
                f'<div class="pct">Margin Bersih: {margin_laba_settled:.1f}%</div>'
                '</div>'
            )

        potential_card = ""
        grand_total_card = ""
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
        laba_proyeksi_card = ""
        if not unsettled_result.empty and total_hpp_proyeksi > 0:
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

        daily_card = ""
        daily_proj_card = ""
        if avg_per_hari is not None:
            daily_card = (
                '<div class="summary-card card-daily">'
                '<div class="label">Penghasilan Real / Hari</div>'
                f'<div class="value">{avg_per_hari_fmt}</div>'
                f'<div class="pct">Real Settled ({num_days} hari)</div>'
                '</div>'
            )
            # Hitung proyeksi bersih per hari (Settled + Estimasi Pending / hari)
            avg_proj_per_hari = total_proyeksi_keseluruhan / num_days if num_days and num_days > 0 else total_proyeksi_keseluruhan
            daily_proj_card = (
                '<div class="summary-card card-grand">'
                '<div class="label">Proyeksi Bersih / Hari</div>'
                f'<div class="value">Rp {avg_proj_per_hari:,.0f}</div>'
                f'<div class="pct">Proyeksi Total ({num_days} hari)</div>'
                '</div>'
            )

        settle_card = (
            '<div class="summary-card card-settle">'
            '<div class="label">Status Settlement</div>'
            f'<div class="value" style="color: {settle_color};">{settle_rate:.1f}%</div>'
            f'<div class="pct" style="color: {settle_pct_color};">{settled_count}/{total_orders_valid} pesanan selesai</div>'
            '</div>'
        )

        summary_html = (
            '<div class="summary-container">'
            + gross_card
            + fees_card
            + adj_card
            + net_card
            + hpp_card
            + laba_card
            + potential_card
            + grand_total_card
            + laba_proyeksi_card
            + daily_card
            + daily_proj_card
            + settle_card
            + '</div>'
        )
        st.markdown(summary_html, unsafe_allow_html=True)


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

        # ─── Highlight No. Pesanan Belum Settlement ───
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

        # ─── Highlight No. Pesanan dengan Penyesuaian ───
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
        st.subheader("📋 Detail Data Produk")
        legends = []
        if 'Returned quantity' in filtered_result.columns and (filtered_result['Returned quantity'] > 0).any():
            legends.append("🟡 **Kuning**: Retur / Penyesuaian (Returned quantity > 0)")
        if 'Is_Settled' in filtered_result.columns and (~filtered_result['Is_Settled']).any():
            legends.append("🔴 **Merah**: Belum Settlement (Dana belum dilepas di Laporan Penghasilan)")
        if legends:
            st.caption(" | ".join(legends))

        display_df = filtered_result.copy()

        # Fungsi highlight gabungan (Retur vs Belum Settlement)
        def highlight_rows(row):
            is_settled = row.get('Is_Settled', True)
            ret_qty = row.get('Returned quantity', 0)
            
            if not is_settled:
                return ['background-color: rgba(239, 68, 68, 0.18); color: #fca5a5; font-weight: 600;'] * len(row)
            if pd.notna(ret_qty) and ret_qty > 0:
                return ['background-color: rgba(234, 179, 8, 0.22); color: #fef08a; font-weight: 600;'] * len(row)
            return [''] * len(row)

        styled_df = display_df.style.apply(highlight_rows, axis=1)

        # Konfigurasi kolom: persentase dan ribuan
        cols_config = {
            'Is_Settled': None,  # Sembunyikan kolom boolean internal dari display tabel
            COL_PCT_ADM: st.column_config.NumberColumn("(%)", format="%.2f%%"),
            COL_PCT_XTRA: st.column_config.NumberColumn("(%) ", format="%.2f%%"),
            COL_PCT_PROMO: st.column_config.NumberColumn("(%)  ", format="%.2f%%"),
            COL_PCT_SUB_BIAYA: st.column_config.NumberColumn("(%)   ", format="%.2f%%"),
            'Jumlah': st.column_config.NumberColumn("Jumlah (Gross)", format="%d", help="Jumlah unit yang dipesan pembeli (Gross)"),
            'Returned quantity': st.column_config.NumberColumn("Retur (Qty)", format="%d", help="Jumlah unit yang diretur"),
            'Jumlah Bersih': st.column_config.NumberColumn("Jumlah Bersih", format="%d", help="Jumlah unit real terjual (Jumlah - Retur)"),
        }
        
        # Format ribuan (koma) untuk kolom uang — tampil saja, data tetap int
        thousand_cols = [
            'Harga (@)', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal', 'Biaya Administrasi', 
            'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Subtotal Biaya', 
            'Biaya Proses Pesanan', 'Total Biaya', 'Pajak'
        ]
        for col in thousand_cols:
            if col in display_df.columns and col not in ['Jumlah', 'Returned quantity', 'Jumlah Bersih']:
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

        # ─── Tabel Rekapitulasi Produk (Grouping - Khusus Pesanan yang SUDAH Settlement) ───
        df_product_summary = generate_product_summary(filtered_result, hpp_lookup=hpp_lookup)
        if not df_product_summary.empty:
            with st.expander("📦 Rekapitulasi Penjualan & Margin Laba per Produk (Sudah Settlement)", expanded=False):
                st.caption("Grouping berdasarkan Nama Produk dan Harga (@) dilengkapi kalkulasi Modal (HPP), Laba Bersih, dan Margin (%)")
                prod_cols_config = {
                    'Total Jumlah Bersih': st.column_config.NumberColumn("Total Jumlah Bersih", format="%d"),
                    'Harga (@)': st.column_config.NumberColumn("Harga (@)", format="%,d"),
                    'Total Penjualan Bersih': st.column_config.NumberColumn("Total Penjualan Bersih", format="%,d"),
                    'HPP (@)': st.column_config.NumberColumn("HPP (@)", format="%,d"),
                    'Total HPP': st.column_config.NumberColumn("Total HPP", format="%,d"),
                    'Laba Bersih': st.column_config.NumberColumn("Laba Bersih", format="%,d"),
                    'Margin Laba (%)': st.column_config.NumberColumn("Margin (%)", format="%.2f%%")
                }
                st.dataframe(
                    df_product_summary,
                    use_container_width=True,
                    hide_index=True,
                    column_config=prod_cols_config
                )

        # ─── Panel Pengaturan Pemetaan HPP (Tabel Bulk Edit) ───
        hpp_source_name = uploaded_hpp.name if uploaded_hpp is not None else "files/hpp_produk.xlsx (Default)"
        with st.expander("🛠️ Pengaturan Pemetaan Master HPP Produk (Kamus Relasi Satuan)", expanded=False):
            st.caption(f"📁 Sumber Master HPP aktif: **{hpp_source_name}** ({len(df_hpp_master)} varian satuan item)")
            st.write("Edit pemetaan langsung di tabel — pilih satuan HPP yang sesuai untuk setiap produk Shopee, lalu klik **Simpan Semua Perubahan**.")

            # Opsi dropdown: label tampil, value = ItemKey
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

            # Build lookup: ItemKey -> HPP per unit (after konversi)
            key_to_hpp = {
                r['ItemKey']: round(r['HargaPokok'] / (r['Konversi'] or 1))
                for _, r in df_hpp_master.iterrows()
            }

            # Simpan juga original HPP untuk deteksi perubahan
            original_hpp = {prod: key_to_hpp.get(mapping_dict.get(prod, ''), None) for prod in all_unique_prods}

            # Bangun DataFrame untuk tabel editor
            table_rows = []
            for prod in sorted(all_unique_prods):
                cur_key = mapping_dict.get(prod, '')
                cur_label = key_to_label.get(cur_key, BELUM_DIPETAKAN)
                hpp_val = key_to_hpp.get(cur_key, None) if cur_key else None
                table_rows.append({
                    'Nama Produk (Shopee)': prod,
                    'Pemetaan HPP (ItemKey & Satuan)': cur_label,
                    'HPP (@)': hpp_val,
                })
            mapping_df = pd.DataFrame(table_rows)

            edited_df = st.data_editor(
                mapping_df,
                column_config={
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
                key="bulk_mapping_editor",
            )

            if st.button("💾 Simpan Semua Perubahan", key="btn_save_bulk_mapping", type="primary"):
                mapping_changed = 0
                hpp_changed = 0

                # ── Deteksi perubahan HPP & tulis balik ke Excel ──
                # Load fresh df untuk diedit
                hpp_excel_path = "files/hpp_produk.xlsx"
                df_hpp_edit = pd.read_excel(hpp_excel_path)
                df_hpp_edit['ItemKey'] = df_hpp_edit['KodeItem'].astype(str) + '_' + df_hpp_edit['Satuan'].astype(str)

                for _, row in edited_df.iterrows():
                    prod_name = row['Nama Produk (Shopee)']
                    chosen_label = row['Pemetaan HPP (ItemKey & Satuan)']
                    new_hpp_unit = row['HPP (@)']

                    # Tentukan ItemKey yang aktif setelah mapping (bisa baru atau lama)
                    if chosen_label == BELUM_DIPETAKAN:
                        active_key = ''
                    else:
                        active_key = label_to_key.get(chosen_label, mapping_dict.get(prod_name, ''))

                    # Update mapping jika berubah
                    if chosen_label == BELUM_DIPETAKAN:
                        if prod_name in mapping_dict:
                            del mapping_dict[prod_name]
                            mapping_changed += 1
                    else:
                        if active_key and mapping_dict.get(prod_name) != active_key:
                            mapping_dict[prod_name] = active_key
                            mapping_changed += 1

                    # Update HPP di Excel jika berubah dan valid
                    if active_key and pd.notna(new_hpp_unit):
                        new_hpp_unit = int(round(new_hpp_unit))
                        old_hpp_unit = key_to_hpp.get(active_key)
                        if old_hpp_unit is None or new_hpp_unit != int(round(old_hpp_unit)):
                            # HargaPokok di master = HPP/unit * Konversi
                            mask = df_hpp_edit['ItemKey'] == active_key
                            if mask.any():
                                konv = df_hpp_edit.loc[mask, 'Konversi'].iloc[0] or 1
                                new_harga_pokok = round(new_hpp_unit * konv, 2)
                                df_hpp_edit.loc[mask, 'HargaPokok'] = new_harga_pokok
                                hpp_changed += 1

                # Simpan mapping
                save_mapping(mapping_dict)

                # Simpan Excel jika ada perubahan HPP
                if hpp_changed > 0:
                    df_hpp_edit.drop(columns=['ItemKey'], inplace=True)
                    df_hpp_edit.to_excel(hpp_excel_path, index=False)

                total_changed = mapping_changed + hpp_changed
                if total_changed > 0:
                    msgs = []
                    if mapping_changed:
                        msgs.append(f"{mapping_changed} pemetaan")
                    if hpp_changed:
                        msgs.append(f"{hpp_changed} HPP di master Excel")
                    st.success(f"✅ Tersimpan: {' dan '.join(msgs)}!")
                    st.rerun()
                else:
                    st.info("Tidak ada perubahan yang perlu disimpan.")





        # ─── Export Excel (raw integer, tanpa formatting) ───
        final_result_excel = add_total_row(filtered_result)

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_result_excel.to_excel(writer, sheet_name='Hasil Rekonsiliasi', index=False)
            if not df_product_summary.empty:
                df_product_summary.to_excel(writer, sheet_name='Rekap Produk & HPP', index=False)
            if not relevant_adj.empty:
                relevant_adj.to_excel(writer, sheet_name='Penyesuaian (Adjustment)', index=False)
        
        st.download_button(
            label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )
