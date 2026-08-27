import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
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

        # ─── Ringkasan Finansial (di luar tabel) ───
        total_subtotal = int(filtered_result['Subtotal'].sum())
        total_biaya = int(filtered_result['Total Biaya'].sum())
        
        # Hitung rincian per komponen biaya
        tot_adm = int(filtered_result['Biaya Administrasi'].sum()) if 'Biaya Administrasi' in filtered_result.columns else 0
        tot_xtra = int(filtered_result['Biaya Gratis Ongkir XTRA'].sum()) if 'Biaya Gratis Ongkir XTRA' in filtered_result.columns else 0
        tot_promo = int(filtered_result['Biaya Promo XTRA'].sum()) if 'Biaya Promo XTRA' in filtered_result.columns else 0
        tot_sub_biaya = int(filtered_result['Subtotal Biaya'].sum()) if 'Subtotal Biaya' in filtered_result.columns else (tot_adm + tot_xtra + tot_promo)
        tot_proses = int(filtered_result['Biaya Proses Pesanan'].sum()) if 'Biaya Proses Pesanan' in filtered_result.columns else 0
        tot_pajak = int(filtered_result['Pajak'].sum()) if 'Pajak' in filtered_result.columns else 0

        pct_adm = abs(tot_adm) / total_subtotal * 100 if total_subtotal > 0 else 0
        pct_xtra = abs(tot_xtra) / total_subtotal * 100 if total_subtotal > 0 else 0
        pct_promo = abs(tot_promo) / total_subtotal * 100 if total_subtotal > 0 else 0
        pct_sub_biaya = abs(tot_sub_biaya) / total_subtotal * 100 if total_subtotal > 0 else 0

        # Hitung total penyesuaian — selalu filter berdasarkan No. Pesanan yang aktif di filtered_result
        adj_orders_list = []
        if not df_adj.empty:
            active_orders = set(filtered_result['No. Pesanan'].astype(str).unique())
            relevant_adj = df_adj[df_adj['No. Pesanan'].astype(str).isin(active_orders)]
            total_penyesuaian = int(relevant_adj['Biaya Penyesuaian'].sum()) if not relevant_adj.empty else 0
            adj_orders_list = [o for o in relevant_adj['No. Pesanan'].unique().tolist() if o and str(o) != 'nan']
        else:
            total_penyesuaian = 0
            relevant_adj = pd.DataFrame()

        # Total Penghasilan Bersih = Subtotal + Total Biaya + Total Penyesuaian (Total Biaya & Penyesuaian bernilai negatif)
        total_penghasilan = total_subtotal + total_biaya + total_penyesuaian

        pct_biaya = abs(total_biaya) / total_subtotal * 100 if total_subtotal > 0 else 0

        st.subheader("💰 Ringkasan Rekonsiliasi")
        st.markdown(f"""
        <div class="summary-container">
            <div class="summary-card card-gross">
                <div class="label">Total Subtotal (Gross)</div>
                <div class="value">Rp {total_subtotal:,.0f}</div>
            </div>
            <div class="summary-card card-fees">
                <div class="label">Total Biaya (Fees)</div>
                <div class="value">Rp {total_biaya:,.0f}</div>
                <div class="pct">{pct_biaya:.1f}% dari Subtotal</div>
            </div>
            <div class="summary-card card-adj">
                <div class="label">Total Penyesuaian (Adjustment)</div>
                <div class="value">Rp {total_penyesuaian:,.0f}</div>
            </div>
            <div class="summary-card card-net">
                <div class="label">Total Penghasilan Bersih</div>
                <div class="value">Rp {total_penghasilan:,.0f}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

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
        if 'Returned quantity' in filtered_result.columns and (filtered_result['Returned quantity'] > 0).any():
            st.caption("🟡 Baris berwarna kuning menandakan produk dengan **Returned quantity > 0** (terkait penyesuaian/retur).")

        display_df = filtered_result.copy()

        # Konfigurasi kolom: persentase dan ribuan
        cols_config = {
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

        # Fungsi highlight baris yang memiliki Returned quantity > 0
        def highlight_returned(row):
            ret_qty = row.get('Returned quantity', 0)
            if pd.notna(ret_qty) and ret_qty > 0:
                return ['background-color: rgba(234, 179, 8, 0.22); color: #fef08a; font-weight: 600;'] * len(row)
            return [''] * len(row)

        styled_df = display_df.style.apply(highlight_returned, axis=1)

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

        # ─── Export Excel (raw integer, tanpa formatting) ───
        final_result_excel = add_total_row(filtered_result)

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_result_excel.to_excel(writer, sheet_name='Hasil Rekonsiliasi', index=False)
            if not relevant_adj.empty:
                relevant_adj.to_excel(writer, sheet_name='Penyesuaian (Adjustment)', index=False)
        
        st.download_button(
            label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )
