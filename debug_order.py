import pandas as pd

order_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'
income_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

target_order = '26072902TJETFD'

def inspect_order(path, target):
    df = pd.read_excel(path, sheet_name='orders')
    subset = df[df['No. Pesanan'] == target]
    print(f"Data di Laporan Order (ditemukan {len(subset)} baris):")
    print(subset[['No. Pesanan', 'Nama Produk', 'Nama Variasi']])
    print("-" * 20)

def inspect_income(path, target):
    df = pd.read_excel(path, sheet_name='Penghasilan', header=2)
    subset = df[df['No. Pesanan'] == target]
    print(f"Data di Laporan Penghasilan (ditemukan {len(subset)} baris):")
    print(subset[['No. Pesanan', 'Nama Produk']])
    print("-" * 20)

inspect_order(order_path, target_order)
inspect_income(income_path, target_order)
