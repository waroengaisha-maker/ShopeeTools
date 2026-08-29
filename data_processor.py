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


def get_order_filter_options(order_file, start_date=None, end_date=None):
    """Membaca file Order dan mengembalikan daftar unik No. Pesanan dan Nama Produk
    yang sesuai dengan rentang tanggal dan filter status pesanan valid.

    Returns:
        dict: {'No. Pesanan': [...], 'Nama Produk': [...]}
    """
    try:
        usecols = ['Waktu Pesanan Dibuat', 'Status Pesanan', 'No. Resi', 'No. Pesanan', 'Nama Produk', 'Nama Variasi']
        df = pd.read_excel(order_file, sheet_name='orders', usecols=usecols)

        # Filter tanggal
        if start_date and end_date:
            df['Waktu Pesanan Dibuat'] = pd.to_datetime(df['Waktu Pesanan Dibuat'], errors='coerce')
            start_dt = pd.to_datetime(start_date).normalize()
            end_dt = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            df = df[(df['Waktu Pesanan Dibuat'] >= start_dt) & (df['Waktu Pesanan Dibuat'] <= end_dt)]

        # Filter status valid (sama seperti process_reconciliation)
        df = df[~df['Status Pesanan'].isin(['Batal', 'Belum Bayar'])]
        df = df[df['No. Resi'].notna()]

        # Daftar No. Pesanan unik
        order_ids = sorted(df['No. Pesanan'].dropna().astype(str).unique().tolist())

        # Daftar Nama Produk unik (gabungan nama + variasi, sama seperti tampilan di tabel)
        df['Nama Produk'] = df['Nama Produk'].fillna('')
        df['Nama Variasi'] = df['Nama Variasi'].fillna('')
        df['Nama Produk Tampilan'] = df.apply(
            lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1
        )
        product_names = sorted(df['Nama Produk Tampilan'].dropna().unique().tolist())

        return {'No. Pesanan': order_ids, 'Nama Produk': product_names}
    except Exception:
        return {'No. Pesanan': [], 'Nama Produk': []}


def get_settlement_stats(order_file, income_file, start_date=None, end_date=None):
    """Menghitung statistik settlement: berapa persen pesanan sudah ada di laporan penghasilan.

    Returns:
        dict: {
            'total_orders': int,
            'settled_orders': int,
            'unsettled_orders': int,
            'settlement_rate': float,  # 0-100
            'unsettled_list': list[str]  # No. Pesanan yang belum settle
        }
    """
    try:
        # --- Order: semua pesanan valid dalam rentang tanggal ---
        usecols_order = ['Waktu Pesanan Dibuat', 'Status Pesanan', 'No. Resi', 'No. Pesanan']
        df_ord = pd.read_excel(order_file, sheet_name='orders', usecols=usecols_order)

        if start_date and end_date:
            df_ord['Waktu Pesanan Dibuat'] = pd.to_datetime(df_ord['Waktu Pesanan Dibuat'], errors='coerce')
            start_dt = pd.to_datetime(start_date).normalize()
            end_dt = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            df_ord = df_ord[(df_ord['Waktu Pesanan Dibuat'] >= start_dt) & (df_ord['Waktu Pesanan Dibuat'] <= end_dt)]

        df_ord = df_ord[~df_ord['Status Pesanan'].isin(['Batal', 'Belum Bayar'])]
        df_ord = df_ord[df_ord['No. Resi'].notna()]
        all_order_ids = set(df_ord['No. Pesanan'].dropna().astype(str).unique())

        # --- Income: No. Pesanan yang sudah ada di laporan Penghasilan ---
        df_inc = pd.read_excel(income_file, sheet_name='Penghasilan', header=2)
        df_inc = df_inc[df_inc['Total Penghasilan'] != 0]
        df_inc = df_inc[df_inc['Lihat berdasarkan'] == 'Sku']
        settled_ids = set(df_inc['No. Pesanan'].dropna().astype(str).unique())

        settled = all_order_ids & settled_ids
        unsettled = all_order_ids - settled_ids

        total = len(all_order_ids)
        rate = len(settled) / total * 100 if total > 0 else 0.0

        return {
            'total_orders': total,
            'settled_orders': len(settled),
            'unsettled_orders': len(unsettled),
            'settlement_rate': rate,
            'unsettled_list': sorted(unsettled)
        }
    except Exception as e:
        return {
            'total_orders': 0,
            'settled_orders': 0,
            'unsettled_orders': 0,
            'settlement_rate': 0.0,
            'unsettled_list': []
        }


def extract_adjustments(income_file):
    """Membaca sheet Adjustment dari file laporan Penghasilan.
    
    Returns:
        pd.DataFrame: DataFrame berisi detail penyesuaian per pesanan.
    """
    try:
        df_raw = pd.read_excel(income_file, sheet_name='Adjustment', header=None)
    except Exception:
        return pd.DataFrame()
        
    header_idx = None
    for idx, row in df_raw.iterrows():
        vals = [str(x).strip() for x in row.dropna().tolist()]
        if 'No. Pesanan Terhubung' in vals:
            header_idx = idx
            break
            
    if header_idx is None:
        return pd.DataFrame()
        
    df_adj = pd.read_excel(income_file, sheet_name='Adjustment', header=header_idx)
    
    # Filter hanya baris transaksi valid (No. Pesanan Terhubung tidak kosong dan bukan baris Total)
    if 'No. Pesanan Terhubung' not in df_adj.columns:
        return pd.DataFrame()
        
    df_adj = df_adj[df_adj['No. Pesanan Terhubung'].notna()]
    df_adj = df_adj[~df_adj['No. Pesanan Terhubung'].astype(str).str.contains('Total|nan', case=False)]
    
    df_adj['Biaya Penyesuaian'] = pd.to_numeric(df_adj['Biaya Penyesuaian'], errors='coerce').fillna(0).round().astype(int)
    df_adj['No. Pesanan'] = df_adj['No. Pesanan Terhubung'].astype(str).str.strip()
    
    # Isi NaN pada kolom teks agar tidak tampil sebagai 'None' di UI
    if 'Alasan Penyesuaian' in df_adj.columns:
        df_adj['Alasan Penyesuaian'] = df_adj['Alasan Penyesuaian'].fillna('-')
    if 'Tipe Penyesuaian | Deskripsi' in df_adj.columns:
        df_adj['Tipe Penyesuaian | Deskripsi'] = df_adj['Tipe Penyesuaian | Deskripsi'].fillna('-')

    # Pilih dan rapikan kolom yang informatif
    cols_to_keep = [
        'Tanggal Penyesuaian Dibuat',
        'No. Pesanan',
        'Tipe Penyesuaian | Deskripsi',
        'Alasan Penyesuaian',
        'Biaya Penyesuaian',
        'Tanggal Dana Dilepaskan'
    ]
    existing_cols = [c for c in cols_to_keep if c in df_adj.columns]
    df_adj = df_adj[existing_cols].reset_index(drop=True)
    df_adj.insert(0, 'No.', range(1, len(df_adj) + 1))
    
    return df_adj


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
        'Returned quantity',
        'Jumlah Bersih',
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
        'Returned quantity': '',
        'Jumlah Bersih': '',
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


def generate_product_summary(df, hpp_lookup=None):
    """Membuat tabel rekapitulasi penjualan per produk (group by Nama Produk dan Harga (@)).
    
    Kolom: No., Nama Produk, Total Jumlah Bersih, Harga (@), Total Penjualan Bersih,
    HPP (@), Total HPP, Laba Bersih, Margin Laba (%).
    Dilengkapi baris Total di bagian akhir.
    """
    if df.empty:
        return pd.DataFrame()

    # Salin dan abaikan baris Total / Total Penghasilan jika sudah ada
    clean_df = df.copy()
    if 'No. Pesanan' in clean_df.columns:
        clean_df = clean_df[~clean_df['No. Pesanan'].astype(str).isin(['Total', 'Total Penghasilan', ''])].copy()
    if 'Nama Produk' in clean_df.columns:
        clean_df = clean_df[~clean_df['Nama Produk'].astype(str).isin(['Total', 'Total Penghasilan'])].copy()
    # HANYA masukkan pesanan yang SUDAH settlement (Is_Settled == True)
    if 'Is_Settled' in clean_df.columns:
        clean_df = clean_df[clean_df['Is_Settled'] == True].copy()

    if clean_df.empty:
        return pd.DataFrame()

    # Pastikan tipe data numerik
    clean_df['Jumlah Bersih'] = pd.to_numeric(clean_df['Jumlah Bersih'], errors='coerce').fillna(0).astype(int)
    clean_df['Jumlah'] = pd.to_numeric(clean_df['Jumlah'], errors='coerce').fillna(0).astype(int)
    clean_df['Harga (@)'] = pd.to_numeric(clean_df['Harga (@)'], errors='coerce').fillna(0).astype(int)
    clean_df['Subtotal'] = pd.to_numeric(clean_df['Subtotal'], errors='coerce').fillna(0).astype(int)
    clean_df['Total Biaya'] = pd.to_numeric(clean_df['Total Biaya'], errors='coerce').fillna(0).astype(int)

    # Grouping berdasarkan Nama Produk dan Harga (@)
    # Sumber kebenaran finansial: Subtotal dan Total Biaya di-SUM langsung dari data transaksi
    grouped = clean_df.groupby(['Nama Produk', 'Harga (@)'], as_index=False).agg({
        'Jumlah Bersih': 'sum',
        'Subtotal': 'sum',
        'Total Biaya': 'sum'
    })

    # Total Penjualan Bersih diambil langsung dari agregasi Subtotal transaksi (Sumber Kebenaran Finansial)
    grouped['Total Penjualan Bersih'] = grouped['Subtotal'].astype(int)
    grouped.drop(columns=['Subtotal'], inplace=True, errors='ignore')

    # Urutkan berdasarkan Nama Produk lalu Harga (@)
    grouped = grouped.sort_values(by=['Nama Produk', 'Harga (@)'], ascending=[True, True]).reset_index(drop=True)

    # Atur posisi kolom: Total Jumlah Bersih sebelum Harga (@)
    grouped = grouped.rename(columns={'Jumlah Bersih': 'Total Jumlah Bersih'})

    # Tambahkan kalkulasi HPP jika hpp_lookup disediakan
    if hpp_lookup is not None:
        def get_hpp_unit(p_name):
            info = hpp_lookup.get(p_name, {})
            harga = info.get('HargaPokok', 0)
            konv = info.get('Konversi', 1) or 1
            return int(round(harga / konv))

        def get_satuan_unit(p_name):
            return str(hpp_lookup.get(p_name, {}).get('Satuan', '-'))

        grouped['Satuan'] = grouped['Nama Produk'].apply(get_satuan_unit)
        grouped['HPP (@)'] = grouped['Nama Produk'].apply(get_hpp_unit)
        grouped['Total HPP'] = (grouped['Total Jumlah Bersih'] * grouped['HPP (@)']).astype(int)
        
        # Laba Bersih = Total Penjualan Bersih (Subtotal Riil) + Total Biaya Shopee - Total HPP (Total Biaya bernilai negatif)
        grouped['Laba Bersih'] = (grouped['Total Penjualan Bersih'] + grouped['Total Biaya'] - grouped['Total HPP']).astype(int)
        grouped['Margin Laba (%)'] = grouped.apply(
            lambda r: (r['Laba Bersih'] / r['Total Penjualan Bersih'] * 100) if r['Total Penjualan Bersih'] > 0 else 0.0,
            axis=1
        )
        
        grouped = grouped[[
            'Nama Produk', 'Total Jumlah Bersih', 'Satuan', 'Harga (@)', 'Total Penjualan Bersih',
            'HPP (@)', 'Total HPP', 'Laba Bersih', 'Margin Laba (%)'
        ]]
    else:
        grouped = grouped[['Nama Produk', 'Total Jumlah Bersih', 'Harga (@)', 'Total Penjualan Bersih']]

    # Sisipkan nomor urut 1..N
    grouped.insert(0, 'No.', range(1, len(grouped) + 1))

    # Hitung baris Total
    tot_qty = int(grouped['Total Jumlah Bersih'].sum())
    tot_sales = int(grouped['Total Penjualan Bersih'].sum())

    row_total = {
        'No.': '',
        'Nama Produk': 'Total',
        'Total Jumlah Bersih': tot_qty,
        'Harga (@)': '',
        'Total Penjualan Bersih': tot_sales
    }

    if hpp_lookup is not None:
        tot_hpp = int(grouped['Total HPP'].sum())
        tot_laba = int(grouped['Laba Bersih'].sum())
        tot_margin = (tot_laba / tot_sales * 100) if tot_sales > 0 else 0.0
        row_total['Satuan'] = ''
        row_total['HPP (@)'] = ''
        row_total['Total HPP'] = tot_hpp
        row_total['Laba Bersih'] = tot_laba
        row_total['Margin Laba (%)'] = tot_margin

    grouped = pd.concat([grouped, pd.DataFrame([row_total])], ignore_index=True)
    return grouped


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

    # Filter: Status Pesanan != 'Batal' & != 'Belum Bayar' AND No. Resi is not null
    df_order = df_order[~df_order['Status Pesanan'].isin(['Batal', 'Belum Bayar'])]
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
    
    # Catat No. Pesanan yang sudah settlement di laporan Income (untuk flag Is_Settled)
    settled_order_ids = set(df_income['No. Pesanan'].dropna().astype(str).unique())
    
    # Pastikan Returned quantity terisi numerik (tanpa mengurangi Jumlah gross)
    df_order['Returned quantity'] = df_order['Returned quantity'].fillna(0).astype(int)
    
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
    df_income['Biaya Administrasi'] = df_income['Biaya Administrasi'].fillna(0) if 'Biaya Administrasi' in df_income.columns else 0
    df_income['Biaya Proses Pesanan'] = df_income['Biaya Proses Pesanan'].fillna(0) if 'Biaya Proses Pesanan' in df_income.columns else 0
    
    # 2. Biaya Gratis Ongkir XTRA (ambil kolom yang ada di dataframe)
    all_xtra_cols = [
        'Biaya Gratis Ongkir XTRA - Ukuran Khusus (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori D)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori E)',
        'Biaya Gratis Ongkir XTRA - Ukuran Biasa (Kategori G)'
    ]
    # Bisa juga tangkap otomatis kolom yang mengandung kata Gratis Ongkir XTRA
    matched_xtra_cols = [c for c in df_income.columns if 'Gratis Ongkir XTRA' in str(c)]
    xtra_cols = list(set([c for c in all_xtra_cols if c in df_income.columns] + matched_xtra_cols))
    
    if xtra_cols:
        df_income['Biaya Gratis Ongkir XTRA'] = df_income[xtra_cols].fillna(0).sum(axis=1)
    else:
        df_income['Biaya Gratis Ongkir XTRA'] = 0
    
    # 3. Biaya Promo XTRA (ambil kolom yang ada di dataframe)
    all_layanan_cols = ['Biaya Transaksi', 'Biaya Layanan Promo XTRA']
    layanan_cols = [c for c in all_layanan_cols if c in df_income.columns]
    if layanan_cols:
        df_income['Biaya Promo XTRA'] = df_income[layanan_cols].fillna(0).sum(axis=1)
    else:
        df_income['Biaya Promo XTRA'] = 0
    
    # 4. Pajak
    if 'PPh 22' in df_income.columns:
        df_income['Pajak'] = df_income['PPh 22'].fillna(0)
    elif 'Pajak' in df_income.columns:
        df_income['Pajak'] = df_income['Pajak'].fillna(0)
    else:
        df_income['Pajak'] = 0
    
    # Hitung Total Biaya secara dinamis berdasarkan kolom biaya yang ada
    candidate_fee_columns = [
        'Biaya Administrasi', 
        'Biaya Proses Pesanan', 
        'Biaya Transaksi',
        'Biaya Layanan Promo XTRA',
        'Biaya Kampanye',
        'Biaya Komisi AMS',
        'PPh 22'
    ] + xtra_cols
    
    actual_fee_columns = list(set([c for c in candidate_fee_columns if c in df_income.columns]))
    
    if actual_fee_columns:
        df_income['Total Biaya'] = df_income[actual_fee_columns].fillna(0).sum(axis=1)
    else:
        df_income['Total Biaya'] = 0
    
    # Re-merge setelah hitung Total Biaya
    fee_columns = ['Total Biaya', 'Biaya Administrasi', 'Biaya Proses Pesanan', 'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Pajak']
    cols_to_merge = keys + fee_columns
    df_merged = pd.merge(df_order, df_income[cols_to_merge], on=keys, how='left')

    # Penggabungan Nama Produk dan Nama Variasi untuk laporan final (setelah join sukses)
    df_merged['Nama Produk'] = df_merged['Nama Produk'].fillna('')
    df_merged['Nama Variasi'] = df_merged['Nama Variasi'].fillna('')
    df_merged['Nama Produk Tampilan'] = df_merged.apply(lambda x: f"{x['Nama Produk']} {x['Nama Variasi']}".strip(), axis=1)

    # Agregasi per Nama Produk Tampilan DAN No. Pesanan
    # Untuk kolom biaya: gunakan custom sum yang menjaga NaN jika SEMUA nilai dalam grup adalah NaN (belum settlement)
    def sum_or_nan(series):
        if series.isna().all():
            return np.nan
        return series.dropna().sum()

    agg_dict = {
        'Item_Price_Total': 'sum',
        'Jumlah': 'sum',
        'Returned quantity': 'sum',
        'Biaya Administrasi': sum_or_nan,
        'Biaya Gratis Ongkir XTRA': sum_or_nan,
        'Biaya Promo XTRA': sum_or_nan,
        'Biaya Proses Pesanan': sum_or_nan,
        'Total Biaya': sum_or_nan,
        'Pajak': sum_or_nan
    }
    
    result = df_merged.groupby(['Nama Produk Tampilan', 'No. Pesanan']).agg(agg_dict).reset_index()
    result.rename(columns={'Nama Produk Tampilan': 'Nama Produk'}, inplace=True)
    
    # Subtotal dihitung langsung dari jumlah nilai kotor (Gross) aktual per item
    result['Subtotal'] = result['Item_Price_Total'].round().astype(int)
    result.drop(columns=['Item_Price_Total'], inplace=True, errors='ignore')

    # Harga (@) menggunakan Weighted Average (Subtotal / Jumlah)
    result['Harga (@)'] = (result['Subtotal'] / result['Jumlah']).round().astype(int)
    
    # Hitung Jumlah Bersih (Qty Real Terjual setelah retur) untuk keperluan akuntansi
    result['Jumlah Bersih'] = result['Jumlah'] - result['Returned quantity']
    
    # Hitung Subtotal Biaya = Biaya Administrasi + Biaya Gratis Ongkir XTRA + Biaya Promo XTRA (hanya jika ada biaya)
    def calc_subtotal_biaya(row):
        adm = row['Biaya Administrasi']
        xtra = row['Biaya Gratis Ongkir XTRA']
        promo = row['Biaya Promo XTRA']
        if pd.isna(adm) and pd.isna(xtra) and pd.isna(promo):
            return np.nan
        return (0 if pd.isna(adm) else adm) + (0 if pd.isna(xtra) else xtra) + (0 if pd.isna(promo) else promo)

    result['Subtotal Biaya'] = result.apply(calc_subtotal_biaya, axis=1)

    # Tandai baris yang belum settlement (No. Pesanan tidak ada di laporan Penghasilan)
    result['Is_Settled'] = result['No. Pesanan'].astype(str).isin(settled_order_ids)

    # Kolom kuantitas dan uang kotor selalu integer
    gross_numeric_cols = ['Harga (@)', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal']
    for col in gross_numeric_cols:
        result[col] = result[col].round().astype(int)

    # Kolom biaya: tetap nullable / float agar membedakan 0 (bebas biaya) vs NaN (belum ada data income)
    for col in fee_columns + ['Subtotal Biaya']:
        result[col] = pd.to_numeric(result[col], errors='coerce')
    
    # Urutkan data produk berdasarkan Nama Produk dan Harga (@) Ascending (tanpa mengikutsertakan baris Total)
    result = result.sort_values(by=['Nama Produk', 'Harga (@)'], ascending=[True, True]).reset_index(drop=True)
    
    # Hitung kolom persentase (%) untuk masing-masing baris produk (hanya untuk yang sudah ada data biaya / settled)
    def calc_pct(fee_val, subtotal_val):
        if pd.isna(fee_val) or subtotal_val <= 0:
            return np.nan
        return abs(fee_val) / subtotal_val * 100

    result[COL_PCT_ADM] = [calc_pct(adm, sub) for adm, sub in zip(result['Biaya Administrasi'], result['Subtotal'])]
    result[COL_PCT_XTRA] = [calc_pct(xtra, sub) for xtra, sub in zip(result['Biaya Gratis Ongkir XTRA'], result['Subtotal'])]
    result[COL_PCT_PROMO] = [calc_pct(promo, sub) for promo, sub in zip(result['Biaya Promo XTRA'], result['Subtotal'])]
    result[COL_PCT_SUB_BIAYA] = [calc_pct(b, sub) for b, sub in zip(result['Subtotal Biaya'], result['Subtotal'])]
    
    # Atur posisi kolom:
    result = result[[
        'No. Pesanan',
        'Nama Produk',
        'Is_Settled',
        'Jumlah Bersih',
        'Harga (@)',
        'Jumlah',
        'Returned quantity',
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
