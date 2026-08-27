import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds,
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
    display: flex;
    gap: 1.2rem;
    margin: 1rem 0 1.5rem 0;
}
.summary-card {
    flex: 1;
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
    font-size: 1.65rem;
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
.card-net .value { color: #4ade80; }
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
        # Reset date bounds cache saat proses ulang
        if 'date_bounds' in st.session_state:
            del st.session_state['date_bounds']
        st.success("✅ Rekonsiliasi Selesai!")
    
    if 'result' in st.session_state:
        result = st.session_state.result
        
        # ─── Filter & Sorting ───
        st.subheader("🔍 Filter & Pengurutan Data")
        f_col1, f_col2, f_col3 = st.columns(3)
        
        # 1. Filter Data
        with f_col1:
            allowed_filters = ['No. Pesanan', 'Nama Produk']
            available_filters = [col for col in allowed_filters if col in result.columns]
            if available_filters:
                filter_col = st.selectbox("Filter berdasarkan:", available_filters)
                unique_values = result[filter_col].unique().tolist()
                selected_values = st.multiselect(f"Pilih nilai untuk {filter_col}:", unique_values, default=[])
                if selected_values:
                    filtered_result = result[result[filter_col].isin(selected_values)].copy()
                else:
                    filtered_result = result.copy()
            else:
                filtered_result = result.copy()
        
        # 2. Pilihan Kolom Pengurutan
        with f_col2:
            sortable_cols = [
                'Nama Produk', 'Harga (@)', 'Jumlah', 'Subtotal', 
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
        total_penghasilan = total_subtotal + total_biaya  # Total Biaya negatif

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
            <div class="summary-card card-net">
                <div class="label">Total Penghasilan Bersih</div>
                <div class="value">Rp {total_penghasilan:,.0f}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # ─── Tabel Detail ───
        st.subheader("📋 Detail Data Produk")

        display_df = filtered_result.copy()

        # Konfigurasi kolom: persentase dan ribuan
        cols_config = {
            COL_PCT_ADM: st.column_config.NumberColumn("(%)", format="%.2f%%"),
            COL_PCT_XTRA: st.column_config.NumberColumn("(%) ", format="%.2f%%"),
            COL_PCT_PROMO: st.column_config.NumberColumn("(%)  ", format="%.2f%%"),
            COL_PCT_SUB_BIAYA: st.column_config.NumberColumn("(%)   ", format="%.2f%%"),
        }
        
        # Format ribuan (koma) untuk kolom uang — tampil saja, data tetap int
        thousand_cols = [
            'Harga (@)', 'Jumlah', 'Subtotal', 'Biaya Administrasi', 
            'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Subtotal Biaya', 
            'Biaya Proses Pesanan', 'Total Biaya', 'Pajak'
        ]
        for col in thousand_cols:
            if col in display_df.columns:
                cols_config[col] = st.column_config.NumberColumn(col, format="%,d")

        st.dataframe(
            display_df, 
            use_container_width=True, 
            hide_index=True,
            column_config=cols_config
        )
        
        # ─── Export Excel (raw integer, tanpa formatting) ───
        final_result_excel = add_total_row(filtered_result)

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_result_excel.to_excel(writer, index=False)
        
        st.download_button(
            label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )
