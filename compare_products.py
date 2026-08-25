import pandas as pd

order_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'
income_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'
target_order = '26072902TJETFD'

# Load & Prep Order
df_order = pd.read_excel(order_path, sheet_name='orders')
df_order = df_order[df_order['No. Pesanan'] == target_order].copy()
df_order['Nama Variasi'] = df_order['Nama Variasi'].fillna('')
df_order['Nama Produk'] = df_order.apply(lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1)
order_products = set(df_order['Nama Produk'].tolist())

# Load & Prep Income
df_income = pd.read_excel(income_path, sheet_name='Penghasilan', header=2)
df_income = df_income[df_income['No. Pesanan'] == target_order].copy()
df_income = df_income[df_income['Lihat berdasarkan'] == 'Sku']
income_products = set(df_income['Nama Produk'].tolist())

print(f"Produk di Order: {order_products}")
print(f"Produk di Penghasilan: {income_products}")

print("\nProduk di Order tapi tidak di Penghasilan:")
print(order_products - income_products)

print("\nProduk di Penghasilan tapi tidak di Order:")
print(income_products - order_products)
