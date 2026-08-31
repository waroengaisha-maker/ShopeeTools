import pandas as pd
from data_processor import filter_valid_income_sku_rows

order_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'
income_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

target_order = '26072902TJETFD'

def inspect_order(path, target):
    df = pd.read_excel(path, sheet_name='orders')
    subset = df[df['No. Pesanan'] == target]
    print(f"Data di Laporan Order (ditemukan {len(subset)} baris):")
    print(subset[['No. Pesanan', 'Nama Produk', 'Nama Variasi']])
    print("-" * 20)

def inspect_income_filtered(path, target):
    # Load dengan header yang sesuai
    df = pd.read_excel(path, sheet_name='Penghasilan', header=2)
    
    # Terapkan validasi Income yang sama seperti di data_processor.py.
    # Baris settlement dengan Total Penghasilan = 0 tetap dipertahankan.
    df = filter_valid_income_sku_rows(df)
    
    subset = df[df['No. Pesanan'] == target]
    print(f"Data di Laporan Penghasilan (setelah filter 'Sku', ditemukan {len(subset)} baris):")
    print(subset[['No. Pesanan', 'Nama Produk', 'Lihat berdasarkan', 'Total Penghasilan']])
    print("-" * 20)

inspect_order(order_path, target_order)
inspect_income_filtered(income_path, target_order)
