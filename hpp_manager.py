import pandas as pd
import json
import os
import re
import difflib

MAPPING_FILE = os.path.join(os.path.dirname(__file__), 'product_hpp_mapping.json')
HPP_MASTER_FILE = os.path.join(os.path.dirname(__file__), 'files', 'hpp_produk.xlsx')

def load_hpp_master(file_source=None):
    """Membaca file master HPP multi-satuan (DataFrame)."""
    try:
        source = file_source if file_source is not None else HPP_MASTER_FILE
        df_hpp = pd.read_excel(source)
        
        # Kolom yang dibutuhkan
        req_cols = ['KodeItem', 'NamaItem', 'HargaPokok', 'HargaJual']
        df_hpp = df_hpp.dropna(subset=['KodeItem', 'NamaItem'])
        
        # Pastikan kolom Satuan dan Konversi ada
        if 'Satuan' not in df_hpp.columns:
            df_hpp['Satuan'] = 'PCS'
        else:
            df_hpp['Satuan'] = df_hpp['Satuan'].fillna('PCS').astype(str).str.strip()

        if 'Konversi' not in df_hpp.columns:
            df_hpp['Konversi'] = 1.0
        else:
            df_hpp['Konversi'] = pd.to_numeric(df_hpp['Konversi'], errors='coerce').fillna(1.0)

        df_hpp['HargaPokok'] = pd.to_numeric(df_hpp['HargaPokok'], errors='coerce').fillna(0)
        df_hpp['HargaJual'] = pd.to_numeric(df_hpp['HargaJual'], errors='coerce').fillna(0)
        
        # Kunci unik: KodeItem + '_' + Satuan (contoh: IT0001_PCS, IT0001_DUS)
        df_hpp['ItemKey'] = df_hpp['KodeItem'].astype(str) + '_' + df_hpp['Satuan']
        
        return df_hpp
    except Exception as e:
        return pd.DataFrame(columns=['KodeItem', 'NamaItem', 'Satuan', 'Konversi', 'HargaPokok', 'HargaJual', 'ItemKey'])


def load_mapping():
    """Membaca kamus mapping Nama Produk Shopee -> ItemKey HPP (KodeItem_Satuan)."""
    if os.path.exists(MAPPING_FILE):
        try:
            with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_mapping(mapping_dict):
    """Menyimpan kamus mapping ke file JSON."""
    try:
        with open(MAPPING_FILE, 'w', encoding='utf-8') as f:
            json.dump(mapping_dict, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def clean_shopee_name_for_suggestion(name):
    """Membersihkan nama produk Shopee untuk tebakan awal fuzzy matching."""
    s = re.sub(r'rev\d+', '', str(name), flags=re.IGNORECASE)
    s = re.sub(r'kebutuhan memasak|bahan baking|bumbu masak|susu & olahan|dressing|aneka varian|kaleng|seafood kaleng', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[^\w\s]', ' ', s)
    return ' '.join(s.lower().split())


def get_suggestion_with_confidence(shopee_name, df_hpp):
    """Mencari pasangan HPP terbaik beserta skor kecocokan (confidence 0.0 - 1.0).
    
    Returns:
        tuple: (best_item_key, best_score, best_item_label) atau (None, 0.0, None)
    """
    if df_hpp.empty:
        return None, 0.0, None

    # Gunakan baris dengan Konversi == 1.0 (satuan terkecil/ecer) sebagai basis nama
    df_base = df_hpp.sort_values(by=['Konversi'], ascending=[True]).drop_duplicates(subset=['KodeItem'], keep='first')
    
    cleaned_shopee = clean_shopee_name_for_suggestion(shopee_name)
    best_key = None
    best_score = 0.0
    best_label = None

    for _, row in df_base.iterrows():
        cleaned_master = clean_shopee_name_for_suggestion(row['NamaItem'])
        # Hitung SequenceMatcher ratio
        score = difflib.SequenceMatcher(None, cleaned_shopee, cleaned_master).ratio()
        
        # Beri boost jika ada exact match kata kunci utama
        if cleaned_master and cleaned_master == cleaned_shopee:
            score = 1.0
        elif cleaned_master and cleaned_master in cleaned_shopee:
            # Jika nama master terkandung utuh dalam nama produk Shopee (misal nama Shopee lebih panjang dengan variasi)
            coverage = len(cleaned_master) / len(cleaned_shopee)
            score = max(score, min(0.95, 0.80 + (0.20 * coverage)))

        if score > best_score:
            best_score = score
            best_key = row['ItemKey']
            best_label = f"{row['KodeItem']} - {row['NamaItem']} [{row['Satuan']} (isi {row['Konversi']:g})]"

    return best_key, round(best_score, 3), best_label


def auto_suggest_mapping(shopee_product_names, df_hpp):
    """Menghasilkan mapping dengan filter confidence ketat:
    - Confidence >= 0.90 (90%): Auto-mapped & disimpan permanen.
    - Confidence 0.70 - 0.89 (70-89%): Disarankan di memori / butuh konfirmasi user (TIDAK auto-save).
    - Confidence < 0.70 (<70%): Dibiarkan kosong / Unmapped.
    """
    existing_mapping = load_mapping()
    updated = False
    
    if df_hpp.empty:
        return existing_mapping

    for s_name in shopee_product_names:
        if s_name not in existing_mapping:
            best_key, score, _ = get_suggestion_with_confidence(s_name, df_hpp)
            # Hanya simpan permanen jika tingkat keyakinan >= 90%
            if score >= 0.90 and best_key:
                existing_mapping[s_name] = best_key
                updated = True

    if updated:
        save_mapping(existing_mapping)

    return existing_mapping

