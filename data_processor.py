import pandas as pd
import numpy as np

# Definisi nama kolom persentase agar unik bagi PyArrow & Windows Console namun tetap tampil sebagai '(%)'
COL_PCT_ADM = '(%)'
COL_PCT_XTRA = '(%) '
COL_PCT_PROMO = '(%)  '
COL_PCT_SUB_BIAYA = '(%)   '

def get_order_date_bounds(order_file):
    """Membaca file Order dan mengembalikan (min_date, max_date) dari kolom 'Waktu Pesanan Dibuat'.
    
    Returns:
        tuple: (min_date, max_date) sebagai datetime.date, atau (None, None) jika gagal.
    """
    try:
        df = pd.read_excel(order_file, sheet_name='orders', usecols=['Waktu Pesanan Dibuat'])
        df['Waktu Pesanan Dibuat'] = pd.to_datetime(df['Waktu Pesanan Dibuat'], errors='coerce')
        df = df.dropna(subset=['Waktu Pesanan Dibuat'])
        if df.empty:
            return None, None
        return df['Waktu Pesanan Dibuat'].min().date(), df['Waktu Pesanan Dibuat'].max().date()
    except Exception:
        return None, None


def format_thousands(val):
    """Format angka dengan pemisah ribuan koma (contoh: 1,234,567)."""
    if val == '' or pd.isna(val):
        return ''
    # Jika sudah berupa string persentase (e.g. 5.22%), biarkan apa adanya
    if isinstance(val, str) and '%' in val:
        return val
    try:
        num = float(val)
        return f"{int(round(num)):,}"
    except (ValueError, TypeError):
        return str(val)

def add_total_row(df):
    """Menambahkan baris Total dan Total Penghasilan selalu di bagian paling bawah."""
    if df.empty:
        return df
    
    # Salin agar tidak mengubah data asli df
    df = df.copy()
    
    # Format kolom persentase pada data produk ke string '%' khusus untuk visualisasi Excel
    pct_cols = [COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA]
    for col in pct_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.2f}%" if isinstance(x, (int, float)) else str(x))
            
    numeric_cols = [
        'Harga (@)',
        'Jumlah',
        'Subtotal',
        'Biaya Administrasi', 
        'Biaya Gratis Ongkir XTRA', 
        'Biaya Promo XTRA', 
        'Subtotal Biaya',
        'Biaya Proses Pesanan', 
        'Total Biaya',
        'Pajak'
    ]
    
    # 1. Baris Total (penjumlahan per kolom)
    total_row = {
        'No.': '',
        'No. Pesanan': '',
        'Nama Produk': 'Total'
    }
    
    for col in numeric_cols:
        if col in df.columns:
            total_row[col] = int(round(pd.to_numeric(df[col], errors='coerce').fillna(0).sum()))
            
    # Hitung persentase pada baris Total
    total_subtotal = total_row.get('Subtotal', 0)
    total_adm = total_row.get('Biaya Administrasi', 0)
    total_xtra = total_row.get('Biaya Gratis Ongkir XTRA', 0)
    total_promo = total_row.get('Biaya Promo XTRA', 0)
    total_sub_biaya = total_row.get('Subtotal Biaya', 0)
    
    total_row[COL_PCT_ADM] = f"{abs(total_adm) / total_subtotal * 100:.2f}%" if total_subtotal > 0 else "0.00%"
    total_row[COL_PCT_XTRA] = f"{abs(total_xtra) / total_subtotal * 100:.2f}%" if total_subtotal > 0 else "0.00%"
    total_row[COL_PCT_PROMO] = f"{abs(total_promo) / total_subtotal * 100:.2f}%" if total_subtotal > 0 else "0.00%"
    total_row[COL_PCT_SUB_BIAYA] = f"{abs(total_sub_biaya) / total_subtotal * 100:.2f}%" if total_subtotal > 0 else "0.00%"
            
    # 2. Baris Total Penghasilan (Subtotal - Total Biaya)
    total_biaya = total_row.get('Total Biaya', 0)
    total_penghasilan = total_subtotal - abs(total_biaya)
    
    total_penghasilan_row = {
        'No.': '',
        'No. Pesanan': '',
        'Nama Produk': 'Total Penghasilan',
        'Harga (@)': '',
        'Jumlah': '',
        'Subtotal': total_penghasilan,
        'Biaya Administrasi': '',
        COL_PCT_ADM: '',
        'Biaya Gratis Ongkir XTRA': '',
        COL_PCT_XTRA: '',
        'Biaya Promo XTRA': '',
        COL_PCT_PROMO: '',
        'Subtotal Biaya': '',
        COL_PCT_SUB_BIAYA: '',
        'Biaya Proses Pesanan': '',
        'Total Biaya': '',
        'Pajak': ''
    }
    
    summary_df = pd.DataFrame([total_row, total_penghasilan_row])
    return pd.concat([df, summary_df], ignore_index=True)

def process_reconciliation(order_file, income_file, start_date=None, end_date=None, add_total=False):
    # 1. Load Data (force Harga Setelah Diskon as string to preserve formatting like 8.500)
    df_order = pd.read_excel(order_file, sheet_name='orders', dtype={'Harga Setelah Diskon': str})
    df_income = pd.read_excel(income_file, sheet_name='Penghasilan', header=2)

    # 2. Pembersihan & Filter Data Order
    # Filter berdasarkan rentang tanggal pesanan jika disediakan
    if start_date and end_date:
        df_order['Waktu Pesanan Dibuat_dt'] = pd.to_datetime(df_order['Waktu Pesanan Dibuat'], errors='coerce')
        start_dt = pd.to_datetime(start_date).normalize()
        end_dt = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        df_order = df_order[(df_order['Waktu Pesanan Dibuat_dt'] >= start_dt) & (df_order['Waktu Pesanan Dibuat_dt'] <= end_dt)]
        df_order = df_order.drop(columns=['Waktu Pesanan Dibuat_dt'], errors='ignore')

    # Filter: Status Pesanan != 'Batal' AND No. Resi is not null
    df_order = df_order[df_order['Status Pesanan'] != 'Batal']
    df_order = df_order[df_order['No. Resi'].notna()]

    # Hapus tanda titik pemisah ribuan dari kolom Harga Setelah Diskon
    df_order['Harga Setelah Diskon'] = (
        df_order['Harga Setelah Diskon']
        .fillna('0')
        .astype(str)
        .str.replace('.', '', regex=False)
        .str.replace(',', '', regex=False)
        .astype(int)
    )
    
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

    # 3. Generasi item_index berdasarkan Nama Produk ASLI (tanpa variasi) dan Total Harga
    df_order['Item_Price_Total'] = (df_order['Jumlah'] * df_order['Harga Setelah Diskon']).round().astype(int)
    df_income['Item_Price_Total'] = df_income['Harga Produk'].round().astype(int)

    def add_item_index(df):
        df = df.sort_values(by=['No. Pesanan', 'Nama Produk Asli', 'Item_Price_Total'])
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

    # Kolom biaya di df_income SEBELUM merger
    
    # 1. Biaya Administrasi & Biaya Proses Pesanan (dipisahkan)
    df_income['Biaya Administrasi'] = df_income['Biaya Administrasi'].fillna(0)
    df_income['Biaya Proses Pesanan'] = df_income['Biaya Proses Pesanan'].fillna(0)
    
    # 2. Biaya Gratis Ongkir XTRA
    xtra_cols = [
        'Biaya Gratis Ongkir XTRA - Ukuran Khusus (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori D)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori G)'
    ]
    df_income['Biaya Gratis Ongkir XTRA'] = df_income[xtra_cols].fillna(0).sum(axis=1)
    
    # 3. Biaya Promo XTRA
    layanan_cols = ['Biaya Transaksi', 'Biaya Layanan Promo XTRA']
    df_income['Biaya Promo XTRA'] = df_income[layanan_cols].fillna(0).sum(axis=1)
    
    # 4. Pajak
    df_income['Pajak'] = df_income['PPh 22'].fillna(0)
    
    # Hitung Total Biaya
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
    
    df_income['Total Biaya'] = df_income[fee_columns].fillna(0).sum(axis=1)
    
    # Re-merge setelah hitung Total Biaya
    cols_to_merge = keys + ['Total Biaya', 'Biaya Administrasi', 'Biaya Proses Pesanan', 'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Pajak']
    df_merged = pd.merge(df_order, df_income[cols_to_merge], on=keys, how='left')
    
    # Isi NaN dengan 0
    for col in ['Total Biaya', 'Biaya Administrasi', 'Biaya Proses Pesanan', 'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Pajak']:
        df_merged[col] = df_merged[col].fillna(0)

    # Penggabungan Nama Produk dan Nama Variasi untuk laporan final (setelah join sukses)
    df_merged['Nama Produk'] = df_merged['Nama Produk'].fillna('')
    df_merged['Nama Variasi'] = df_merged['Nama Variasi'].fillna('')
    df_merged['Nama Produk Tampilan'] = df_merged.apply(lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1)

    # Agregasi akhir
    df_merged['Harga Setelah Diskon'] = df_merged['Harga Setelah Diskon'].astype(float)

    # Agregasi per Nama Produk Tampilan DAN No. Pesanan
    agg_dict = {
        'Harga Setelah Diskon': 'mean',
        'Jumlah': 'sum',
        'Biaya Administrasi': 'sum',
        'Biaya Gratis Ongkir XTRA': 'sum',
        'Biaya Promo XTRA': 'sum',
        'Biaya Proses Pesanan': 'sum',
        'Total Biaya': 'sum',
        'Pajak': 'sum'
    }
    
    result = df_merged.groupby(['Nama Produk Tampilan', 'No. Pesanan']).agg(agg_dict).reset_index()
    result.rename(columns={'Nama Produk Tampilan': 'Nama Produk'}, inplace=True)
    
    # Format & konversi ke integer
    result['Harga (@)'] = result['Harga Setelah Diskon'].round().astype(int)
    result.drop(columns=['Harga Setelah Diskon'], inplace=True, errors='ignore')
    
    # Hitung Subtotal = Jumlah * Harga (@)
    result['Subtotal'] = (result['Jumlah'] * result['Harga (@)']).round().astype(int)
    
    # Hitung Subtotal Biaya = Biaya Administrasi + Biaya Gratis Ongkir XTRA + Biaya Promo XTRA
    result['Subtotal Biaya'] = (result['Biaya Administrasi'] + result['Biaya Gratis Ongkir XTRA'] + result['Biaya Promo XTRA']).round().astype(int)
    
    numeric_cols = [
        'Harga (@)',
        'Jumlah', 
        'Subtotal',
        'Biaya Administrasi', 
        'Biaya Gratis Ongkir XTRA', 
        'Biaya Promo XTRA', 
        'Subtotal Biaya',
        'Biaya Proses Pesanan', 
        'Total Biaya',
        'Pajak'
    ]
    for col in numeric_cols:
        result[col] = result[col].round().astype(int)
    
    # Urutkan data produk berdasarkan Nama Produk dan Harga (@) Ascending (tanpa mengikutsertakan baris Total)
    result = result.sort_values(by=['Nama Produk', 'Harga (@)'], ascending=[True, True]).reset_index(drop=True)
    
    # Hitung kolom persentase (%) untuk masing-masing baris produk
    result[COL_PCT_ADM] = [abs(adm) / sub * 100 if sub > 0 else 0.0 for adm, sub in zip(result['Biaya Administrasi'], result['Subtotal'])]
    result[COL_PCT_XTRA] = [abs(xtra) / sub * 100 if sub > 0 else 0.0 for xtra, sub in zip(result['Biaya Gratis Ongkir XTRA'], result['Subtotal'])]
    result[COL_PCT_PROMO] = [abs(promo) / sub * 100 if sub > 0 else 0.0 for promo, sub in zip(result['Biaya Promo XTRA'], result['Subtotal'])]
    result[COL_PCT_SUB_BIAYA] = [abs(b) / sub * 100 if sub > 0 else 0.0 for b, sub in zip(result['Subtotal Biaya'], result['Subtotal'])]
    
    # Atur posisi kolom:
    # No., No. Pesanan, Nama Produk, Harga (@), Jumlah, Subtotal,
    # Biaya Administrasi, (%), Biaya Gratis Ongkir XTRA, (%), Biaya Promo XTRA, (%), Subtotal Biaya, (%), Biaya Proses Pesanan, Total Biaya, Pajak
    result = result[[
        'No. Pesanan', 
        'Nama Produk', 
        'Harga (@)', 
        'Jumlah', 
        'Subtotal', 
        'Biaya Administrasi', 
        COL_PCT_ADM,
        'Biaya Gratis Ongkir XTRA', 
        COL_PCT_XTRA,
        'Biaya Promo XTRA', 
        COL_PCT_PROMO,
        'Subtotal Biaya',
        COL_PCT_SUB_BIAYA,
        'Biaya Proses Pesanan', 
        'Total Biaya', 
        'Pajak'
    ]]
    
    # Tambahkan kolom No. sebagai urutan setelah disortir
    result.insert(0, 'No.', range(1, len(result) + 1))
    
    # Jika add_total=True, tambahkan baris Total dan Total Penghasilan selalu di baris paling bawah
    if add_total:
        result = add_total_row(result)
        
    return result

# Uji coba fungsi
if __name__ == "__main__":
    order_path = r'E:\Warung Aisha\Tool\files\Order.all.20260701_20260731.xlsx'
    income_path = r'E:\Warung Aisha\Tool\files\Income.sudah dilepas.id.20260701_20260824.xlsx'
    
    try:
        report = process_reconciliation(order_path, income_path, add_total=True)
        print(report.tail(10).to_string())
    except Exception as e:
        print(f"Error: {e}")
