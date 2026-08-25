import pandas as pd
import os

file_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

print(f"Mencetak 5 baris pertama dari sheet 'Penghasilan' (tanpa header):")

try:
    # Membaca 5 baris pertama tanpa menganggap baris pertama sebagai header
    df = pd.read_excel(file_path, sheet_name='Penghasilan', header=None, nrows=5)
    print(df)
        
except Exception as e:
    print(f"Error: {e}")
