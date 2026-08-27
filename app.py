import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
    get_settlement_stats, generate_product_summary,
    COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
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

st.title("📊 Aplikasi Rekonsiliasi Shopee")
st.write("Unggah laporan **Order** dan laporan **Penghasilan** untuk mendapatkan ringkasan SKU.")

uploaded_order = st.file_uploader("Pilih Laporan Order (Excel)", type=['xlsx'])
uploaded_income = st.file_uploader("Pilih Laporan Penghasilan (Excel)", type=['xlsx'])

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
        
        # ─── Filter & Sorting ───
        st.subheader("🔍 Filter & Pengurutan Data")
        f_col1, f_col2, f_col3 = st.columns(3)
        
        # 1. Filter Data
        filter_options = st.session_state.get('filter_options', {})
        with f_col1:
            allowed_filters = ['No. Pesanan', 'Nama Produk']
            available_filters = [col for col in allowed_filters if col in result.columns]
            if available_filters:
                filter_col = st.selectbox("Filter berdasarkan:", available_filters)
                # Gunakan opsi dari file order (mencerminkan semua pesanan valid sesuai date range)
                unique_values = filter_options.get(filter_col, sorted(result[filter_col].dropna().astype(str).unique().tolist()))
                selected_values = st.multiselect(f"Pilih nilai untuk {filter_col}:", unique_values, default=[])
                if selected_values:
                    filtered_result = result[result[filter_col].astype(str).isin([str(v) for v in selected_values])].copy()
                else:
                    filtered_result = result.copy()
            else:
                filtered_result = result.copy()
        
        # 2. Pilihan Kolom Pengurutan
        with f_col2:
            sortable_cols = [
                'Nama Produk', 'Jumlah Bersih', 'Harga (@)', 'Jumlah', 'Returned quantity', 'Subtotal', 
                'Biaya Administrasi', COL_PCT_ADM, 
                'Biaya Gratis Ongkir XTRA', COL_PCT_XTRA, 
                'Biaya Promo XTRA', COL_PCT_PROMO, 
                'Subtotal Biaya', COL_PCT_SUB_BIAYA,
                'Biaya Proses Pesanan', 'Total Biaya', 'Pajak', 'No. Pesanan'
            ]
            sortable_cols = [c for c in sortable_cols if c in filtered_result.columns]
            sort_by = st.selectbox("Urutkan berdasarkan:", sortable_cols, index=0)
            
        # 3. Arah Pengurutan
        with f_col3:
            sort_dir = st.radio("Arah urutan:", ["Kecil ke Besar (Ascending)", "Besar ke Kecil (Descending)"], index=0)
            ascending = True if "Ascending" in sort_dir else False

        # Terapkan pengurutan — pastikan kolom numerik di-sort secara numerik dan teks tetap string
        if pd.api.types.is_numeric_dtype(filtered_result[sort_by]):
            filtered_result = filtered_result.sort_values(by=sort_by, ascending=ascending).reset_index(drop=True)
        else:
            filtered_result = filtered_result.sort_values(by=sort_by, ascending=ascending).reset_index(drop=True)

        # Reset dan sisipkan kolom 'No.' agar selalu berurutan 1..N setelah sorting
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

        # ─── Perhitungan Estimasi Potensi Penghasilan dari Unsettled Orders ───
        unsettled_result = filtered_result[filtered_result['Is_Settled'] == False].copy() if 'Is_Settled' in filtered_result.columns else pd.DataFrame()
        unsettled_subtotal = int(unsettled_result['Subtotal'].sum()) if not unsettled_result.empty else 0
        
        # Rasio fee rata-rata dari pesanan yang sudah settled (fallback ke 15% jika belum ada settled)
        effective_fee_ratio = (abs(total_biaya) / total_subtotal) if total_subtotal > 0 else 0.15
        
        # Estimasi biaya dan estimasi bersih untuk unsettled orders
        est_unsettled_net = int(unsettled_subtotal * (1 - effective_fee_ratio))
        
        # Total Keseluruhan (Settled Real + Estimasi Unsettled)
        total_proyeksi_keseluruhan = total_penghasilan + est_unsettled_net

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
            + potential_card
            + grand_total_card
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
        df_product_summary = generate_product_summary(filtered_result)
        if not df_product_summary.empty:
            with st.expander("📦 Rekapitulasi Penjualan Bersih per Produk (Sudah Settlement)", expanded=False):
                st.caption("Grouping berdasarkan Nama Produk dan Harga (@) dari pesanan yang **sudah settlement** dengan akumulasi Total Jumlah Bersih")
                prod_cols_config = {
                    'Total Jumlah Bersih': st.column_config.NumberColumn("Total Jumlah Bersih", format="%d"),
                    'Harga (@)': st.column_config.NumberColumn("Harga (@)", format="%,d"),
                    'Total Penjualan Bersih': st.column_config.NumberColumn("Total Penjualan Bersih", format="%,d")
                }
                st.dataframe(
                    df_product_summary,
                    use_container_width=True,
                    hide_index=True,
                    column_config=prod_cols_config
                )

        # ─── Export Excel (raw integer, tanpa formatting) ───
        final_result_excel = add_total_row(filtered_result)

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_result_excel.to_excel(writer, sheet_name='Hasil Rekonsiliasi', index=False)
            if not df_product_summary.empty:
                df_product_summary.to_excel(writer, sheet_name='Rekap Produk', index=False)
            if not relevant_adj.empty:
                relevant_adj.to_excel(writer, sheet_name='Penyesuaian (Adjustment)', index=False)
        
        st.download_button(
            label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )
