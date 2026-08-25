import pandas as pd
import numpy as np

def process_reconciliation(order_file, income_file):
    # 1. Load Data
    df_order = pd.read_excel(order_file, sheet_name='orders')
    df_income = pd.read_excel(income_file, sheet_name='Penghasilan', header=2)

    # 2. Pembersihan & Filter Data Order
    # Filter: Status Pesanan != 'Batal' AND No. Resi is not null
    df_order = df_order[df_order['Status Pesanan'] != 'Batal']
    df_order = df_order[df_order['No. Resi'].notna()]
    
    # Perhitungan Jumlah baru: Jumlah - Returned quantity
    df_order['Returned quantity'] = df_order['Returned quantity'].fillna(0)
    df_order['Jumlah'] = df_order['Jumlah'] - df_order['Returned quantity']
    
    # Penggabungan Nama Produk dan Nama Variasi
    df_order['Nama Variasi'] = df_order['Nama Variasi'].fillna('')
    df_order['Nama Produk'] = df_order.apply(lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1)

    # 3. Generasi item_index
    def add_item_index(df):
        # Gunakan 'Nama Produk' yang sudah dimodifikasi
        df = df.sort_values(by=['No. Pesanan', 'Nama Produk'])
        df['item_index'] = df.groupby(['No. Pesanan', 'Nama Produk']).cumcount()
        return df

    df_order = add_item_index(df_order)
    df_income = add_item_index(df_income)

    # 4. Penggabungan (Merge)
    # Konversi kunci ke string untuk konsistensi
    keys = ['No. Pesanan', 'Nama Produk', 'item_index']
    for key in keys:
        df_order[key] = df_order[key].astype(str)
        df_income[key] = df_income[key].astype(str)

    df_merged = pd.merge(df_order, df_income, on=keys, how='left')

    # 5. Agregasi
    # ... (perhitungan Total Fees tetap sama)
    fee_columns = [
        'Biaya Administrasi', 
        'Biaya Proses Pesanan', 
        'Biaya Gratis Ongkir XTRA - Ukuran Khusus (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori D)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori G)',
        'Biaya Transaksi',
        'Biaya Layanan Promo XTRA',
        'Biaya Kampanye',
        'Biaya Komisi AMS',
        'PPh 22'
    ]
    
    for col in fee_columns:
        if col in df_income.columns:
            df_income[col] = df_income[col].fillna(0)
    
    df_income['Total Fees'] = df_income[fee_columns].sum(axis=1)
    
    # Re-merge setelah hitung Total Fees (disederhanakan untuk efisiensi)
    # Gunakan df_merged yang sudah ada dan tambahkan Total Fees dari df_income
    df_merged = pd.merge(df_order, df_income[keys + ['Total Fees']], on=keys, how='left')
    df_merged['Total Fees'] = df_merged['Total Fees'].fillna(0)

    # Agregasi akhir
    df_merged['Harga Setelah Diskon (Ribuan)'] = df_merged['Harga Setelah Diskon'] * 1000
    
    # Agregasi per Nama Produk DAN No. Pesanan
    result = df_merged.groupby(['Nama Produk', 'No. Pesanan']).agg({
        'Jumlah': 'sum',
        'Harga Setelah Diskon (Ribuan)': 'mean',
        'Total Fees': 'sum'
    }).reset_index()
    
    result['Gross Price (@)'] = result['Harga Setelah Diskon (Ribuan)'].apply(lambda x: f"{int(round(x))}")
    result['Total Fees'] = result['Total Fees'].apply(lambda x: f"{int(round(x))}")
    
    result.drop(columns=['Harga Setelah Diskon (Ribuan)'], inplace=True, errors='ignore')
    
    # Atur posisi kolom: No., No. Pesanan, Nama Produk, Jumlah, Gross Price (@), Total Fees
    result = result[['No. Pesanan', 'Nama Produk', 'Jumlah', 'Gross Price (@)', 'Total Fees']]
    
    # Tambahkan kolom No. sebagai urutan
    result.insert(0, 'No.', range(1, len(result) + 1))
    
    return result

# Uji coba fungsi
if __name__ == "__main__":
    order_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'
    income_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'
    
    try:
        report = process_reconciliation(order_path, income_path)
        print(report.to_string())
    except Exception as e:
        print(f"Error: {e}")
