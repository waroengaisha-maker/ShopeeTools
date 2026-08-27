import pandas as pd
import json
import os
import re
import difflib

MAPPING_FILE = os.path.join(os.path.dirname(__file__), 'product_hpp_mapping.json')
HPP_MASTER_FILE = os.path.join(os.path.dirname(__file__), 'files', 'hpp_produk.xlsx')

def load_hpp_master(file_source=None):
    """Membaca file master HPP (DataFrame)."""
    try:
        source = file_source if file_source is not None else HPP_MASTER_FILE
        df_hpp = pd.read_excel(source)
        df_hpp = df_hpp[['KodeItem', 'NamaItem', 'HargaPokok', 'HargaJual']].dropna(subset=['KodeItem', 'NamaItem'])
        df_hpp['HargaPokok'] = pd.to_numeric(df_hpp['HargaPokok'], errors='coerce').fillna(0)
        df_hpp['HargaJual'] = pd.to_numeric(df_hpp['HargaJual'], errors='coerce').fillna(0)
        return df_hpp
    except Exception as e:
        return pd.DataFrame(columns=['KodeItem', 'NamaItem', 'HargaPokok', 'HargaJual'])


def load_mapping():
    """Membaca kamus mapping Nama Produk Shopee -> KodeItem HPP."""
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


def auto_suggest_mapping(shopee_product_names, df_hpp):
    """Menghasilkan tebakan awal otomatis untuk produk Shopee yang belum dimapping."""
    existing_mapping = load_mapping()
    updated = False
    
    if df_hpp.empty:
        return existing_mapping

    hpp_clean_map = {clean_shopee_name_for_suggestion(row['NamaItem']): row['KodeItem'] for _, row in df_hpp.iterrows()}
    hpp_clean_keys = list(hpp_clean_map.keys())

    for s_name in shopee_product_names:
        if s_name not in existing_mapping:
            cleaned_s = clean_shopee_name_for_suggestion(s_name)
            matches = difflib.get_close_matches(cleaned_s, hpp_clean_keys, n=1, cutoff=0.35)
            if matches:
                suggested_code = hpp_clean_map[matches[0]]
                existing_mapping[s_name] = suggested_code
                updated = True

    if updated:
        save_mapping(existing_mapping)

    return existing_mapping
