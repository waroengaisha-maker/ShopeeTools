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
    
    # Filter: Total Penghasilan != 0
    df_income = df_income[df_income['Total Penghasilan'] != 0]
    
    # Filter: Lihat berdasarkan == 'Sku'
    df_income = df_income[df_income['Lihat berdasarkan'] == 'Sku']
    
    # Perhitungan Jumlah baru: Jumlah - Returned quantity
    df_order['Returned quantity'] = df_order['Returned quantity'].fillna(0)
    df_order['Jumlah'] = df_order['Jumlah'] - df_order['Returned quantity']
    
    # Simpan Nama Produk asli (tanpa variasi) untuk join
    df_order['Nama Produk Asli'] = df_order['Nama Produk']
    df_income['Nama Produk Asli'] = df_income['Nama Produk']

    # 3. Generasi item_index berdasarkan Nama Produk ASLI (tanpa variasi)
    def add_item_index(df):
        df = df.sort_values(by=['No. Pesanan', 'Nama Produk Asli'])
        df['item_index'] = df.groupby(['No. Pesanan', 'Nama Produk Asli']).cumcount()
        return df

    df_order = add_item_index(df_order)
    df_income = add_item_index(df_income)

    # 4. Penggabungan (Merge)
    # Konversi kunci ke string untuk konsistensi
    keys = ['No. Pesanan', 'Nama Produk Asli', 'item_index']
    for key in keys:
        df_order[key] = df_order[key].astype(str)
        df_income[key] = df_income[key].astype(str)

    # Perbaikan: Hitung kolom biaya baru di df_income SEBELUM merger
    
    # 1. Biaya Platform
    df_income['Biaya Platform'] = df_income[['Biaya Administrasi', 'Biaya Proses Pesanan']].fillna(0).sum(axis=1)
    
    # 2. Biaya Gratis Ongkir XTRA (Opsional)
    xtra_cols = [
        'Biaya Gratis Ongkir XTRA - Ukuran Khusus (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori D)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori G)'
    ]
    df_income['Biaya Gratis Ongkir XTRA (Opsional)'] = df_income[xtra_cols].fillna(0).sum(axis=1)
    
    # 3. Biaya Layanan (Opsional)
    layanan_cols = ['Biaya Transaksi', 'Biaya Layanan Promo XTRA']
    df_income['Biaya Layanan (Opsional)'] = df_income[layanan_cols].fillna(0).sum(axis=1)
    
    # 4. Pajak
    df_income['Pajak'] = df_income['PPh 22'].fillna(0)
    
    # Hitung Total Fees (tetap butuh ini untuk perhitungan total)
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
    
    df_income['Total Fees'] = df_income[fee_columns].fillna(0).sum(axis=1)
    
    # Re-merge setelah hitung Total Fees
    cols_to_merge = keys + ['Total Fees', 'Biaya Platform', 'Biaya Gratis Ongkir XTRA (Opsional)', 'Biaya Layanan (Opsional)', 'Pajak']
    df_merged = pd.merge(df_order, df_income[cols_to_merge], on=keys, how='left')
    
    # Isi NaN dengan 0
    for col in ['Total Fees', 'Biaya Platform', 'Biaya Gratis Ongkir XTRA (Opsional)', 'Biaya Layanan (Opsional)', 'Pajak']:
        df_merged[col] = df_merged[col].fillna(0)

    # Penggabungan Nama Produk dan Nama Variasi untuk laporan final (setelah join sukses)
    df_merged['Nama Variasi'] = df_merged['Nama Variasi'].fillna('')
    df_merged['Nama Produk Tampilan'] = df_merged.apply(lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1)

    # Agregasi akhir
    df_merged['Harga Setelah Diskon (Ribuan)'] = df_merged['Harga Setelah Diskon'] * 1000
    
    # Agregasi per Nama Produk Tampilan DAN No. Pesanan
    agg_dict = {
        'Jumlah': 'sum',
        'Harga Setelah Diskon (Ribuan)': 'mean',
        'Total Fees': 'sum',
        'Biaya Platform': 'sum',
        'Biaya Gratis Ongkir XTRA (Opsional)': 'sum',
        'Biaya Layanan (Opsional)': 'sum',
        'Pajak': 'sum'
    }
    
    result = df_merged.groupby(['Nama Produk Tampilan', 'No. Pesanan']).agg(agg_dict).reset_index()
    result.rename(columns={'Nama Produk Tampilan': 'Nama Produk'}, inplace=True)
    
    # Format angka
    for col in ['Gross Price (@)', 'Total Fees', 'Biaya Platform', 'Biaya Gratis Ongkir XTRA (Opsional)', 'Biaya Layanan (Opsional)', 'Pajak']:
        if col != 'Gross Price (@)':
            result[col] = result[col].apply(lambda x: f"{int(round(x))}")
            
    result['Gross Price (@)'] = result['Harga Setelah Diskon (Ribuan)'].apply(lambda x: f"{int(round(x))}")
    
    result.drop(columns=['Harga Setelah Diskon (Ribuan)'], inplace=True, errors='ignore')
    
    # Perbaikan: Konversi Gross Price (@) ke numerik sementara untuk sorting yang benar
    result['Gross Price (@) Numeric'] = result['Gross Price (@)'].astype(int)
    
    # Urutkan berdasarkan Nama Produk dan Gross Price (@) Ascending
    result = result.sort_values(by=['Nama Produk', 'Gross Price (@) Numeric'], ascending=[True, True])
    
    # Atur posisi kolom dan hapus kolom sorting sementara
    result = result[['No. Pesanan', 'Nama Produk', 'Jumlah', 'Gross Price (@)', 'Biaya Platform', 'Biaya Gratis Ongkir XTRA (Opsional)', 'Biaya Layanan (Opsional)', 'Pajak', 'Total Fees']]
    
    # Tambahkan kolom No. sebagai urutan setelah disortir
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
