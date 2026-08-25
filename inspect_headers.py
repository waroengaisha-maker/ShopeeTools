import pandas as pd
import os

file = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'

print(f"File: {os.path.basename(file)}")
# Just try printing whatever is in the file
try:
    df = pd.read_excel(file, header=None, nrows=10)
    print(df)
except Exception as e:
    print(f"Error: {e}")
