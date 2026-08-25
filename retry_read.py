import pandas as pd
import os

file_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

print(f"Inspecting columns in 'Penghasilan' sheet:")

try:
    # Read the sheet with header=1 (second row, index 1)
    df = pd.read_excel(file_path, sheet_name='Penghasilan', header=1, nrows=1)
    print("Columns in 'Penghasilan' sheet:")
    for col in df.columns:
        print(f"- {col}")
        
except Exception as e:
    print(f"Error: {e}")
