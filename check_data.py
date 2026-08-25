import pandas as pd
import os

file_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

print(f"Membaca data mulai baris ke-4 dari sheet 'Penghasilan':")

try:
    # Membaca data dengan header pada baris ke-3 (index 2 karena 0-indexed)
    # Pandas akan menggunakan baris tersebut sebagai nama kolom
    df = pd.read_excel(file_path, sheet_name='Penghasilan', header=2, nrows=5)
    
    print("Kolom yang terdeteksi:")
    print(df.columns.tolist())
    
    print("\n5 Baris pertama data:")
    print(df)
        
except Exception as e:
    print(f"Error: {e}")
