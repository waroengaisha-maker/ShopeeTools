import streamlit as st
import pandas as pd
from data_processor import process_reconciliation, add_total_row, format_thousands, COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
import io

st.set_page_config(layout="wide")

st.title("Aplikasi Rekonsiliasi Shopee")

st.write("Unggah laporan Order dan laporan Penghasilan untuk mendapatkan ringkasan SKU.")

uploaded_order = st.file_uploader("Pilih Laporan Order (Excel)", type=['xlsx'])
uploaded_income = st.file_uploader("Pilih Laporan Penghasilan (Excel)", type=['xlsx'])

if uploaded_order and uploaded_income:
    # Cek apakah tombol ditekan
    if st.button("Proses Rekonsiliasi", key="btn_proses"):
        with st.spinner('Memproses data...'):
            st.session_state.result = process_reconciliation(uploaded_order, uploaded_income)
        st.success("Rekonsiliasi Selesai!")
    
    if 'result' in st.session_state:
        result = st.session_state.result
        
        # Fitur Filter & Pengurutan
        st.subheader("Filter & Pengurutan Data")
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
        
        # 2. Pilihan Kolom Pengurutan (di backend)
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

        # Terapkan pengurutan di backend
        filtered_result = filtered_result.sort_values(by=sort_by, ascending=ascending).reset_index(drop=True)

        # Reset dan sisipkan kolom 'No.' secara fisik agar selalu berurutan dari 1 sampai N setelah disortir
        if 'No.' in filtered_result.columns:
            filtered_result = filtered_result.drop(columns=['No.'])
        filtered_result.insert(0, 'No.', range(1, len(filtered_result) + 1))

        # Hitung Ringkasan Finansial untuk Metrik UI
        total_subtotal = int(filtered_result['Subtotal'].sum())
        total_biaya = int(filtered_result['Total Biaya'].sum())
        total_penghasilan = total_subtotal + total_biaya  # Total Biaya bernilai negatif, jadi ditambah

        # Tampilkan Ringkasan Finansial dalam bentuk Kartu Metrik Premium
        st.subheader("Ringkasan Rekonsiliasi")
        m_col1, m_col2, m_col3 = st.columns(3)
        m_col1.metric(
            label="Total Subtotal (Gross)", 
            value=f"Rp {total_subtotal:,.0f}".replace(",", ".")
        )
        m_col2.metric(
            label="Total Biaya (Fees)", 
            value=f"Rp {total_biaya:,.0f}".replace(",", "."),
            delta=f"Potongan Biaya" if total_biaya == 0 else f"{total_biaya/total_subtotal*100:.1f}% dari Subtotal",
            delta_color="inverse"
        )
        m_col3.metric(
            label="Total Penghasilan Bersih", 
            value=f"Rp {total_penghasilan:,.0f}".replace(",", ".")
        )

        st.subheader("Detail Data Produk")

        # Tampilkan tabel menggunakan data numerik asli (tanpa format_thousands)
        # agar sorting interaktif (klik header) maupun backend berjalan 100% secara numerik
        display_df = filtered_result.copy()

        # Konfigurasi kolom Streamlit agar menampilkan ribuan dan persentase secara rapi
        cols_config = {
            COL_PCT_ADM: st.column_config.NumberColumn("(%)", format="%.2f%%"),
            COL_PCT_XTRA: st.column_config.NumberColumn("(%) ", format="%.2f%%"),
            COL_PCT_PROMO: st.column_config.NumberColumn("(%)  ", format="%.2f%%"),
            COL_PCT_SUB_BIAYA: st.column_config.NumberColumn("(%)   ", format="%.2f%%"),
        }
        
        # Format ribuan untuk kolom uang/biaya dan jumlah
        thousand_cols = [
            'Harga (@)', 'Jumlah', 'Subtotal', 'Biaya Administrasi', 
            'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Subtotal Biaya', 
            'Biaya Proses Pesanan', 'Total Biaya', 'Pajak'
        ]
        for col in thousand_cols:
            if col in display_df.columns:
                # Memaksa pemisah ribuan menggunakan koma (,) di sisi UI
                cols_config[col] = st.column_config.NumberColumn(col, format="%,d")

        st.dataframe(
            display_df, 
            use_container_width=True, 
            hide_index=True,
            column_config=cols_config
        )
        
        # Tambahkan baris total khusus untuk ekspor Excel saja
        final_result_excel = add_total_row(filtered_result)

        # Export to Excel (menggunakan data yang sudah difilter dan memiliki baris total di paling bawah)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            final_result_excel.to_excel(writer, index=False)
        
        st.download_button(
            label="Unduh Laporan Excel Lengkap (.xlsx)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )

