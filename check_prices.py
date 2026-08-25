import pandas as pd
import os

file_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'

print("Mencetak 5 baris pertama dari kolom 'Harga Setelah Diskon':")

try:
    # Membaca sheet 'orders'
    df = pd.read_excel(file_path, sheet_name='orders')
    
    # Menampilkan 5 baris pertama dari kolom 'Harga Setelah Diskon'
    print(df['Harga Setelah Diskon'].head(5))
        
except Exception as e:
    print(f"Error: {e}")
