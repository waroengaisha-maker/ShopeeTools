import streamlit as st
import pandas as pd
from data_processor import process_reconciliation
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
        
        # Fitur Filter
        st.subheader("Filter Data")
        allowed_filters = ['No. Pesanan', 'Nama Produk']
        available_filters = [col for col in allowed_filters if col in result.columns]
        
        if available_filters:
            filter_col = st.selectbox("Pilih kolom untuk difilter:", available_filters)
            
            # Dapatkan nilai unik dari kolom yang dipilih
            unique_values = result[filter_col].unique().tolist()
            selected_values = st.multiselect(f"Pilih nilai untuk {filter_col}:", unique_values, default=[])
            
            # Terapkan filter: jika kosong, tampilkan semua data
            if selected_values:
                filtered_result = result[result[filter_col].isin(selected_values)].copy()
            else:
                filtered_result = result.copy()
        else:
            filtered_result = result.copy()

        # Reset kolom No. agar berurutan kembali setelah filter
        if 'No.' in filtered_result.columns:
            filtered_result = filtered_result.drop(columns=['No.'])
        filtered_result.insert(0, 'No.', range(1, len(filtered_result) + 1))

        st.dataframe(filtered_result, use_container_width=True)
        
        # Export to Excel (gunakan data yang sudah difilter)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            filtered_result.to_excel(writer, index=False)
        
        st.download_button(
            label="Unduh Hasil (Excel)",
            data=buffer,
            file_name="hasil_rekonsiliasi.xlsx",
            mime="application/vnd.ms-excel"
        )
