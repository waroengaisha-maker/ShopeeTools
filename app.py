import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
    get_settlement_stats, generate_product_summary,
    COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
)
from hpp_manager import (
    load_hpp_master, load_mapping, save_mapping,
    get_suggestion_with_confidence
)
import io
import hashlib
import html
import json
import logging
import math
import os
import pickle
import re
import shutil
import time
import uuid
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo


def _parse_shopee_rupiah_series(series):
    """Parse nominal Shopee Indonesia seperti ``10.590`` sebagai Rp10,590."""
    def parse_value(value):
        if pd.isna(value):
            return 0
        if isinstance(value, (int, float)):
            return int(round(value))
        text = str(value).strip().replace("Rp", "").replace(" ", "")
        if not text:
            return 0
        # Pada export Shopee, titik/koma adalah pemisah ribuan.
        text = text.replace(".", "").replace(",", "")
        try:
            return int(round(float(text)))
        except (TypeError, ValueError):
            return 0
    return series.map(parse_value).astype("int64")


def _customer_geocode_cache_path():
    return os.path.join(os.path.dirname(__file__), "data", "customer_geocode_cache.json")


def _load_customer_geocode_cache():
    try:
        with open(_customer_geocode_cache_path(), "r", encoding="utf-8") as cache_file:
            return json.load(cache_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_customer_geocode_cache(cache):
    try:
        os.makedirs(os.path.dirname(_customer_geocode_cache_path()), exist_ok=True)
        with open(_customer_geocode_cache_path(), "w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2)
    except OSError:
        LOGGER.warning("Customer geocode cache tidak dapat disimpan.")


def _geocode_place(place):
    query = urllib.parse.quote(str(place).strip())
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q={query}",
        headers={"User-Agent": "WarungAishaCustomerDistance/1.0"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        results = pd.DataFrame(json.loads(response.read().decode("utf-8")))
    return (float(results.iloc[0]["lat"]), float(results.iloc[0]["lon"])) if not results.empty else None
from pathlib import Path

st.set_page_config(layout="wide", page_title="Rekonsiliasi Shopee")

if st.query_params.get("reset") == "1":
    st.session_state.clear()
    st.query_params.clear()
    st.query_params["session"] = uuid.uuid4().hex
    st.rerun()


def _read_shopee_stock_excel(uploaded_file):
    """Baca ekspor stok Shopee, termasuk workbook dengan activePane invalid."""
    raw = uploaded_file.getvalue()
    try:
        return pd.read_excel(io.BytesIO(raw), sheet_name=0, header=2)
    except ValueError:
        # Ekspor Shopee tertentu memakai nilai activePane yang ditolak
        # openpyxl; normalisasi metadata worksheet lalu coba baca ulang.
        repaired = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as source, zipfile.ZipFile(repaired, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = re.sub(rb'activePane="(?:topLeft|bottomRight|bottomLeft|topRight|[^\"]+)"', b'activePane="topLeft"', data)
                target.writestr(item, data)
        repaired.seek(0)
        return pd.read_excel(repaired, sheet_name=0, header=2)


def _validate_report_upload(uploaded_file, report_type):
    """Validasi ringan file Order/Income sebelum proses rekonsiliasi."""
    if uploaded_file is None:
        return False, "File belum dipilih."
    try:
        uploaded_file.seek(0)
        excel = pd.ExcelFile(uploaded_file)
        expected_sheet = "orders" if report_type == "order" else "Penghasilan"
        if expected_sheet not in excel.sheet_names:
            return False, f"Sheet '{expected_sheet}' tidak ditemukan. Sheet tersedia: {', '.join(excel.sheet_names)}."
        header = 0 if report_type == "order" else 2
        columns = set(pd.read_excel(uploaded_file, sheet_name=expected_sheet, header=header, nrows=0).columns)
        required = (
            {"No. Pesanan", "Status Pesanan", "Waktu Pesanan Dibuat", "Nama Produk", "Jumlah", "Harga Setelah Diskon"}
            if report_type == "order" else {"No. Pesanan", "Nama Produk", "Harga Produk"}
        )
        missing = sorted(required - columns)
        if missing:
            return False, f"Kolom wajib tidak ditemukan: {', '.join(missing)}."
        return True, "File valid."
    except Exception as exc:
        return False, f"File tidak dapat dibaca: {exc}"
    finally:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass


@st.cache_data(show_spinner=False)
def _cached_reconciliation(order_bytes, income_bytes, start_date, end_date):
    """Cache hasil rekonsiliasi berdasarkan isi file dan rentang tanggal."""
    return process_reconciliation(
        io.BytesIO(order_bytes),
        io.BytesIO(income_bytes),
        start_date=start_date,
        end_date=end_date,
    )

# Setiap browser session memiliki ruang upload sendiri. Token ikut disimpan di
# URL agar unggahan tetap dapat ditemukan setelah halaman dimuat ulang atau
# pengguna berpindah menu melalui tautan navigasi.
session_token = st.query_params.get("session")
if not isinstance(session_token, str) or not re.fullmatch(r"[a-f0-9]{32}", session_token):
    existing_session_id = st.session_state.get("session_id")
    session_token = (
        existing_session_id
        if isinstance(existing_session_id, str) and re.fullmatch(r"[a-f0-9]{32}", existing_session_id)
        else uuid.uuid4().hex
    )
    st.query_params["session"] = session_token
st.session_state.session_id = session_token
SESSION_UPLOAD_ROOT = Path("data") / "uploads"
SESSION_UPLOAD_DIR = SESSION_UPLOAD_ROOT / st.session_state.session_id
SESSION_UPLOAD_MAX_AGE_SECONDS = 24 * 60 * 60
SESSION_CLEANUP_INTERVAL_SECONDS = 10 * 60
SESSION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
LOGGER = logging.getLogger(__name__)


def _short_session_id(session_id):
    return f"{session_id[:8]}..."


def touch_session_activity():
    """Catat aktivitas sesi aktif tanpa mengganggu alur upload bila disk terkunci."""
    try:
        SESSION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (SESSION_UPLOAD_DIR / ".last_activity").touch()
    except (PermissionError, OSError) as exc:
        LOGGER.warning("Session activity update failed for %s: %s", _short_session_id(st.session_state.session_id), exc)


def _get_session_activity_timestamp(session_dir):
    """Ambil aktivitas sesi; None berarti sesi tidak aman untuk dihapus."""
    marker_path = session_dir / ".last_activity"
    try:
        return marker_path.stat().st_mtime
    except FileNotFoundError:
        # Folder dari versi sebelum marker memakai aktivitas file/directory sebagai
        # fallback kompatibel. Kegagalan membaca fallback selalu berarti KEEP.
        pass
    except (PermissionError, OSError) as exc:
        LOGGER.warning("Session cleanup failed to read activity for %s: %s", _short_session_id(session_dir.name), exc)
        return None

    try:
        latest_activity = session_dir.stat().st_mtime
        for child in session_dir.iterdir():
            latest_activity = max(latest_activity, child.stat().st_mtime)
        return latest_activity
    except (PermissionError, OSError) as exc:
        LOGGER.warning("Session cleanup failed to verify legacy session %s: %s", _short_session_id(session_dir.name), exc)
        return None


def cleanup_expired_sessions():
    """Hapus hanya child session yang terverifikasi kedaluwarsa dari data/uploads."""
    active_session_id = st.session_state.session_id
    try:
        SESSION_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        upload_root = SESSION_UPLOAD_ROOT.resolve()
    except (PermissionError, OSError) as exc:
        LOGGER.warning("Session cleanup failed to prepare upload root: %s", exc)
        return

    LOGGER.info("Session cleanup started")
    now = time.time()
    try:
        session_children = list(upload_root.iterdir())
    except (PermissionError, OSError) as exc:
        LOGGER.warning("Session cleanup failed to scan upload root: %s", exc)
        return

    for session_dir in session_children:
        # Hanya folder sesi langsung dengan ID yang dibuat aplikasi yang boleh
        # diproses. Symlink dilewati agar cleanup tidak dapat menjangkau path lain.
        try:
            if not session_dir.is_dir() or session_dir.is_symlink() or not SESSION_ID_PATTERN.fullmatch(session_dir.name):
                continue
        except (PermissionError, OSError) as exc:
            LOGGER.warning("Session cleanup failed to inspect child %s: %s", _short_session_id(session_dir.name), exc)
            continue
        if session_dir.name == active_session_id:
            LOGGER.info("Session cleanup skipped active session %s", _short_session_id(session_dir.name))
            continue
        try:
            if session_dir.resolve().parent != upload_root:
                LOGGER.warning("Session cleanup skipped unsafe path %s", _short_session_id(session_dir.name))
                continue
        except (PermissionError, OSError) as exc:
            LOGGER.warning("Session cleanup failed to verify path %s: %s", _short_session_id(session_dir.name), exc)
            continue

        last_activity = _get_session_activity_timestamp(session_dir)
        if last_activity is None or now - last_activity <= SESSION_UPLOAD_MAX_AGE_SECONDS:
            continue

        LOGGER.info("Session cleanup found expired session %s", _short_session_id(session_dir.name))
        try:
            shutil.rmtree(session_dir)
            LOGGER.info("Session cleanup removed expired session %s", _short_session_id(session_dir.name))
        except FileNotFoundError:
            LOGGER.info("Session cleanup found session already removed %s", _short_session_id(session_dir.name))
        except (PermissionError, OSError) as exc:
            LOGGER.warning("Session cleanup failed for %s: %s", _short_session_id(session_dir.name), exc)


def maybe_cleanup_expired_sessions():
    """Throttle full scan agar rerun Streamlit tidak selalu menjalankan cleanup."""
    now = time.time()
    last_cleanup = st.session_state.get("session_upload_cleanup_last_run", 0.0)
    if isinstance(last_cleanup, (int, float)) and now - last_cleanup < SESSION_CLEANUP_INTERVAL_SECONDS:
        return
    st.session_state.session_upload_cleanup_last_run = now
    cleanup_expired_sessions()


touch_session_activity()
maybe_cleanup_expired_sessions()


def persist_session_order_upload(uploaded_file):
    """Simpan file Order upload ke folder session dan kembalikan byte/path aktif."""
    raw_bytes = uploaded_file.getvalue()
    touch_session_activity()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if st.session_state.get('uploaded_order_sha256') != digest:
        SESSION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(uploaded_file.name).name).strip('._') or 'order.xlsx'
        stored_path = SESSION_UPLOAD_DIR / f"order_{digest[:12]}_{safe_name}"
        stored_path.write_bytes(raw_bytes)
        st.session_state.uploaded_order_sha256 = digest
        st.session_state.uploaded_order_path = str(stored_path)
        st.session_state.uploaded_order_name = uploaded_file.name
    return raw_bytes


def _get_session_result_path():
    return SESSION_UPLOAD_DIR / "reconciliation_result.pkl"


def _save_session_result():
    payload = {
        "result": st.session_state.get("result"),
        "df_adjustments": st.session_state.get("df_adjustments"),
        "filter_options": st.session_state.get("filter_options"),
        "settlement_stats": st.session_state.get("settlement_stats"),
        "processed_start_date": st.session_state.get("processed_start_date"),
        "processed_end_date": st.session_state.get("processed_end_date"),
        "processed_at": st.session_state.get("processed_at"),
        "uploaded_order_bytes": st.session_state.get("uploaded_order_bytes"),
        "uploaded_income_bytes": st.session_state.get("uploaded_income_bytes"),
        "uploaded_order_name": st.session_state.get("uploaded_order_name"),
        "uploaded_income_name": st.session_state.get("uploaded_income_name"),
    }
    result_path = _get_session_result_path()
    try:
        SESSION_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with result_path.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    except (PermissionError, OSError, pickle.PickleError) as exc:
        LOGGER.warning("Failed to persist reconciliation result for %s: %s", _short_session_id(st.session_state.session_id), exc)


def _load_session_result():
    result_path = _get_session_result_path()
    if not result_path.exists():
        return False
    try:
        with result_path.open("rb") as fh:
            payload = pickle.load(fh)
    except (PermissionError, OSError, pickle.PickleError, EOFError, AttributeError, TypeError) as exc:
        LOGGER.warning("Failed to load reconciliation result for %s: %s", _short_session_id(st.session_state.session_id), exc)
        return False

    if not isinstance(payload, dict) or "result" not in payload:
        return False

    for key, value in payload.items():
        st.session_state[key] = value
    return True


def _format_processed_period(start_date, end_date):
    if start_date and end_date:
        return f"{start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')}"
    return "-"


def _get_status_retur_series(df):
    if 'Returned quantity' in df.columns:
        return df['Returned quantity'].fillna(0).astype(float).gt(0).map({True: 'Ada Retur', False: 'Tanpa Retur'})
    return pd.Series(['Tanpa Retur'] * len(df), index=df.index)


def _build_dashboard_filter_options(df):
    options = {}
    if 'Waktu Pesanan Dibuat' in df.columns:
        month_series = pd.to_datetime(df['Waktu Pesanan Dibuat'], errors='coerce').dt.to_period('M')
        options['Periode'] = sorted(month_series.dropna().astype(str).unique().tolist())
    if 'Nama Produk' in df.columns:
        options['Produk'] = sorted(df['Nama Produk'].dropna().astype(str).unique().tolist())
    if 'Nama Variasi' in df.columns:
        options['SKU'] = sorted(df['Nama Variasi'].dropna().astype(str).unique().tolist())
    if 'Is_Settled' in df.columns:
        options['Status Settlement'] = ['Settled', 'Belum Settlement']
    if 'Returned quantity' in df.columns:
        options['Status Retur'] = ['Ada Retur', 'Tanpa Retur']
    return options


def _apply_dashboard_filters(df, periode_vals, produk_vals, sku_vals, settlement_vals, retur_vals):
    filtered = df.copy()
    if periode_vals and 'Waktu Pesanan Dibuat' in filtered.columns:
        month_series = pd.to_datetime(filtered['Waktu Pesanan Dibuat'], errors='coerce').dt.to_period('M').astype(str)
        filtered = filtered[month_series.isin(periode_vals)]
    if produk_vals and 'Nama Produk' in filtered.columns:
        filtered = filtered[filtered['Nama Produk'].astype(str).isin([str(v) for v in produk_vals])]
    if sku_vals and 'Nama Variasi' in filtered.columns:
        filtered = filtered[filtered['Nama Variasi'].astype(str).isin([str(v) for v in sku_vals])]
    if settlement_vals and 'Is_Settled' in filtered.columns:
        settled_map = filtered['Is_Settled'].map({True: 'Settled', False: 'Belum Settlement'}).fillna('Belum Settlement')
        filtered = filtered[settled_map.isin(settlement_vals)]
    if retur_vals and 'Returned quantity' in filtered.columns:
        retur_map = _get_status_retur_series(filtered)
        filtered = filtered[retur_map.isin(retur_vals)]
    return filtered


def _build_hpp_lookup_for_dashboard(result_df, hpp_source=None):
    df_hpp_master = load_hpp_master(file_source=hpp_source)
    if result_df is None or result_df.empty or 'Nama Produk' not in result_df.columns:
        return {}
    all_unique_prods = result_df['Nama Produk'].dropna().unique().tolist()
    # Dashboard hanya memakai mapping yang sudah dikonfirmasi/disimpan.
    # Auto-suggestion diproses khusus di menu Kelola Master HPP.
    mapping_dict = load_mapping()
    hpp_by_key = {r['ItemKey']: r.to_dict() for _, r in df_hpp_master.iterrows()}
    return {p: hpp_by_key[k] for p, k in mapping_dict.items() if k in hpp_by_key}


def _find_session_order_file():
    if not SESSION_UPLOAD_DIR.exists():
        return None
    candidates = sorted(
        [p for p in SESSION_UPLOAD_DIR.glob("order_*.xlsx") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _build_cancelled_order_summary(order_file_path, start_date=None, end_date=None, settled_fee_ratio=0.0):
    """Menghitung pesanan batal dari file Order, di luar KPI finansial settled."""
    empty_summary = {'count': 0, 'value': 0, 'income_lost': 0, 'seller_count': 0, 'seller_value': 0, 'rate': 0.0, 'details': pd.DataFrame(), 'by_type': []}
    if not order_file_path or not Path(order_file_path).exists():
        return empty_summary

    try:
        order_df = pd.read_excel(
            order_file_path,
            sheet_name='orders',
            dtype={'Harga Setelah Diskon': str},
        )
        order_df['Waktu Pesanan Dibuat'] = pd.to_datetime(order_df['Waktu Pesanan Dibuat'], errors='coerce')
        if start_date is not None:
            order_df = order_df[order_df['Waktu Pesanan Dibuat'] >= pd.to_datetime(start_date).normalize()]
        if end_date is not None:
            end_dt = pd.to_datetime(end_date).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            order_df = order_df[order_df['Waktu Pesanan Dibuat'] <= end_dt]

        order_df['No. Pesanan'] = order_df['No. Pesanan'].astype(str).str.strip()
        order_df = order_df[order_df['No. Pesanan'].ne('') & order_df['No. Pesanan'].ne('nan')]
        status = order_df['Status Pesanan'].astype(str).str.strip().str.casefold()
        cancelled = order_df[status.eq('batal')].copy()
        denominator = order_df.loc[~status.eq('belum bayar'), 'No. Pesanan'].nunique()
        cancelled_count = cancelled['No. Pesanan'].nunique()

        raw_price = cancelled['Harga Setelah Diskon'].astype(str).str.strip()
        # Format ekspor Shopee menggunakan titik/koma sebagai pemisah ribuan.
        price = raw_price.str.replace(r'\.0$', '', regex=True)
        price = price.str.replace('.', '', regex=False).str.replace(',', '', regex=False)
        price = pd.to_numeric(price, errors='coerce').fillna(0)
        quantity = pd.to_numeric(cancelled['Jumlah'], errors='coerce').fillna(0)
        line_value = price * quantity
        cancelled_value = int((price * quantity).sum())
        income_lost = int(round(cancelled_value * (1 - max(0.0, min(float(settled_fee_ratio), 1.0)))))
        type_candidates = [
            'Tipe Pembatalan', 'Jenis Pembatalan', 'Alasan Pembatalan',
            'Alasan Pembatalan Pesanan', 'Cancellation Type',
            'Cancellation Reason', 'Alasan Batal',
        ]
        type_col = next((col for col in type_candidates if col in cancelled.columns), None)
        reason_text = cancelled[type_col].astype(str).str.casefold() if type_col else pd.Series('', index=cancelled.index)
        seller_reason = (
            reason_text.str.contains(r'dibatalkan oleh penjual|cancelled by seller', regex=True, na=False)
            | reason_text.str.contains(r'dibatalkan secara otomatis.*penjual|otomatis.*penjual tidak', regex=True, na=False)
        ) & ~reason_text.str.contains(r'dibatalkan oleh pembeli|cancelled by buyer', regex=True, na=False)
        cancellation_type = (
            cancelled[type_col].map(lambda value: str(value).strip() if pd.notna(value) and str(value).strip() else 'Tidak tersedia')
            if type_col else pd.Series('Tidak tersedia', index=cancelled.index)
        )
        fee_factor = 1 - max(0.0, min(float(settled_fee_ratio), 1.0))
        by_type = []
        for cancellation_label, type_rows in cancellation_type.groupby(cancellation_type):
            type_value = int(round(line_value.loc[type_rows.index].sum()))
            by_type.append({
                'type': cancellation_label,
                'count': int(cancelled.loc[type_rows.index, 'No. Pesanan'].nunique()),
                'income_lost': int(round(type_value * fee_factor)),
            })
        by_type.sort(key=lambda item: item['income_lost'], reverse=True)

        detail_columns = {
            'Waktu Pesanan Dibuat': 'Waktu Pesanan Dibuat',
            'No. Pesanan': 'No. Pesanan',
            'No. Resi': 'No. Resi',
            'Nama Produk': 'Nama Produk',
            'Nama Variasi': 'Nama Variasi',
            'Jumlah': 'Jumlah',
            'Harga Setelah Diskon': 'Harga Setelah Diskon',
        }
        details = pd.DataFrame()
        available = {label: col for label, col in detail_columns.items() if col in cancelled.columns}
        if available:
            details = cancelled[list(available.values())].rename(columns={v: k for k, v in available.items()}).copy()
            details['Tipe Pembatalan'] = (
                cancelled[type_col].map(lambda value: str(value).strip() if pd.notna(value) and str(value).strip() else 'Tidak tersedia')
                if type_col else 'Tidak tersedia'
            )
            details['Nilai Transaksi'] = line_value.loc[details.index].round(0).astype(int)
            details['Estimasi Penghasilan Hilang'] = (
                details['Nilai Transaksi'] * (1 - max(0.0, min(float(settled_fee_ratio), 1.0)))
            ).round(0).astype(int)
            details['Waktu Pesanan Dibuat'] = pd.to_datetime(details['Waktu Pesanan Dibuat'], errors='coerce').dt.strftime('%d/%m/%Y %H:%M')
            if 'Harga Setelah Diskon' in details.columns:
                details['Harga Setelah Diskon'] = price.loc[details.index].round(0).astype(int)
            details = details.rename(columns={'Harga Setelah Diskon': 'Harga Satuan'})
            details = details.sort_values('Waktu Pesanan Dibuat', ascending=False, na_position='last')
        return {
            'count': int(cancelled_count),
            'value': cancelled_value,
            'income_lost': income_lost,
            'seller_count': int(cancelled.loc[seller_reason, 'No. Pesanan'].nunique()),
            'seller_value': int(line_value.loc[seller_reason].sum()),
            'by_type': by_type,
            'rate': (cancelled_count / denominator * 100) if denominator > 0 else 0.0,
            'details': details,
        }
    except Exception as exc:
        LOGGER.warning("Cancelled order summary failed for %s: %s", order_file_path, exc)
        return empty_summary


def _compute_dashboard_kpis(result_df, hpp_lookup_map):
    df = result_df.copy()
    settled = df[df['Is_Settled'] == True].copy() if 'Is_Settled' in df.columns else df.copy()
    unsettled = df[(df['Is_Settled'] == False) & (~df.get('Is_Cancelled', False))].copy() if 'Is_Settled' in df.columns else pd.DataFrame()

    total_subtotal = int(settled['Subtotal'].sum()) if 'Subtotal' in settled.columns else 0
    total_biaya = int(settled['Total Biaya'].sum()) if 'Total Biaya' in settled.columns else 0
    total_penghasilan = int(settled['Subtotal'].sum()) + int(settled['Total Biaya'].sum()) if not settled.empty else 0

    def get_item_hpp(row):
        info = hpp_lookup_map.get(row.get('Nama Produk'), {})
        harga_pokok = info.get('HargaPokok', 0)
        konversi = info.get('Konversi', 1) or 1
        return row.get('Jumlah Bersih', 0) * (harga_pokok / konversi)

    hpp_valid_mask = settled['Nama Produk'].astype(str).isin(hpp_lookup_map) if not settled.empty and 'Nama Produk' in settled.columns else pd.Series(False, index=settled.index)
    hpp_valid_settled = settled[hpp_valid_mask].copy()
    total_hpp = int(round(hpp_valid_settled.apply(get_item_hpp, axis=1).sum())) if not hpp_valid_settled.empty else 0
    valid_penghasilan = int(hpp_valid_settled['Subtotal'].sum() + hpp_valid_settled['Total Biaya'].sum()) if not hpp_valid_settled.empty else 0
    laba_bersih = valid_penghasilan - total_hpp
    margin_laba = (laba_bersih / valid_penghasilan * 100) if valid_penghasilan > 0 else 0.0
    hpp_missing_product_count = int(settled.loc[~hpp_valid_mask, 'Nama Produk'].nunique()) if not settled.empty and 'Nama Produk' in settled.columns else 0
    total_orders_valid = len(df['No. Pesanan'].dropna().unique()) if 'No. Pesanan' in df.columns else 0
    settled_count = len(settled['No. Pesanan'].dropna().unique()) if not settled.empty and 'No. Pesanan' in settled.columns else 0
    unsettled_count = len(unsettled['No. Pesanan'].dropna().unique()) if not unsettled.empty and 'No. Pesanan' in unsettled.columns else 0
    settle_rate = (settled_count / total_orders_valid * 100) if total_orders_valid > 0 else 100.0
    pending_count = unsettled_count

    # Proyeksi pending memakai pola fee dan HPP dari transaksi settled.
    unsettled_subtotal = int(unsettled['Subtotal'].sum()) if not unsettled.empty and 'Subtotal' in unsettled.columns else 0
    history_subtotal = int(settled['Subtotal'].sum()) if 'Subtotal' in settled.columns else 0
    history_total_fee = abs(int(settled['Total Biaya'].sum())) if 'Total Biaya' in settled.columns else 0
    history_process_fee = abs(int(settled['Biaya Proses Pesanan'].sum())) if 'Biaya Proses Pesanan' in settled.columns else 0
    settled_order_count = settled['No. Pesanan'].dropna().nunique() if not settled.empty and 'No. Pesanan' in settled.columns else 0
    estimated_process_fee_per_order = history_process_fee / settled_order_count if settled_order_count > 0 else 0
    global_non_process_fee_ratio = (
        max(history_total_fee - history_process_fee, 0) / history_subtotal
        if history_subtotal > 0 else 0.15
    )
    product_fee_ratio = {}
    if not settled.empty and 'Nama Produk' in settled.columns and 'Subtotal' in settled.columns:
        for product_name, product_rows in settled.groupby('Nama Produk'):
            product_subtotal = int(product_rows['Subtotal'].sum())
            product_total_fee = abs(int(product_rows['Total Biaya'].sum())) if 'Total Biaya' in product_rows.columns else 0
            product_process_fee = abs(int(product_rows['Biaya Proses Pesanan'].sum())) if 'Biaya Proses Pesanan' in product_rows.columns else 0
            product_fee_ratio[product_name] = (
                max(product_total_fee - product_process_fee, 0) / product_subtotal
                if product_subtotal > 0 else global_non_process_fee_ratio
            )
    estimated_non_process_fee = int(round(sum(
        row['Subtotal'] * product_fee_ratio.get(row.get('Nama Produk'), global_non_process_fee_ratio)
        for _, row in unsettled.iterrows()
    ))) if not unsettled.empty else 0
    estimated_process_fee = int(round(
        estimated_process_fee_per_order * unsettled['No. Pesanan'].dropna().nunique()
    )) if not unsettled.empty and 'No. Pesanan' in unsettled.columns else 0
    estimated_fee = -(estimated_non_process_fee + estimated_process_fee)
    estimated_income = unsettled_subtotal + estimated_fee
    estimated_hpp = int(round(unsettled.apply(get_item_hpp, axis=1).sum())) if not unsettled.empty and hpp_lookup_map else 0
    estimated_profit = estimated_income - estimated_hpp

    return {
        'total_subtotal': total_subtotal,
        'total_biaya': abs(total_biaya),
        'total_penghasilan': total_penghasilan,
        'total_hpp': total_hpp,
        'laba_bersih': laba_bersih,
        'margin_laba': margin_laba,
        'hpp_missing_product_count': hpp_missing_product_count,
        'total_orders_valid': total_orders_valid,
        'settled_count': settled_count,
        'unsettled_count': unsettled_count,
        'settle_rate': settle_rate,
        'pending_count': pending_count,
        'unsettled_subtotal': unsettled_subtotal,
        'estimated_fee': estimated_fee,
        'estimated_income': estimated_income,
        'estimated_hpp': estimated_hpp,
        'estimated_profit': estimated_profit,
        'projected_income': total_penghasilan + estimated_income,
        'projected_profit': laba_bersih + estimated_profit,
    }


def _build_daily_chart_data(order_file_path, result_df, hpp_lookup_map):
    if not order_file_path or not Path(order_file_path).exists():
        return pd.DataFrame()

    try:
        usecols = ['Waktu Pesanan Dibuat', 'Status Pesanan', 'No. Resi', 'No. Pesanan', 'Nama Produk', 'Nama Variasi']
        df_order = pd.read_excel(order_file_path, sheet_name='orders', usecols=usecols)
        df_order['Waktu Pesanan Dibuat'] = pd.to_datetime(df_order['Waktu Pesanan Dibuat'], errors='coerce')
        df_order = df_order.dropna(subset=['Waktu Pesanan Dibuat'])
        df_order = df_order[~df_order['Status Pesanan'].isin(['Batal', 'Belum Bayar'])]
        df_order = df_order[df_order['No. Resi'].notna()].copy()
        if df_order.empty or result_df is None or result_df.empty:
            return pd.DataFrame()

        df_order['No. Pesanan'] = df_order['No. Pesanan'].astype(str)
        result_df = result_df.copy()
        if 'No. Pesanan' not in result_df.columns:
            return pd.DataFrame()
        result_df['No. Pesanan'] = result_df['No. Pesanan'].astype(str)

        daily_rows = []
        for day, chunk in df_order.groupby(df_order['Waktu Pesanan Dibuat'].dt.date):
            order_ids = chunk['No. Pesanan'].dropna().unique().tolist()
            subset = result_df[result_df['No. Pesanan'].isin(order_ids)].copy()
            if subset.empty:
                continue

            settled_subset = subset[subset['Is_Settled'] == True].copy() if 'Is_Settled' in subset.columns else subset.copy()
            omzet = int(settled_subset['Subtotal'].sum()) if 'Subtotal' in settled_subset.columns else 0
            biaya = int(settled_subset['Total Biaya'].sum()) if 'Total Biaya' in settled_subset.columns else 0
            penghasilan = omzet + biaya

            def _row_hpp(row):
                info = hpp_lookup_map.get(row.get('Nama Produk'), {})
                harga_pokok = info.get('HargaPokok', 0)
                konversi = info.get('Konversi', 1) or 1
                return row.get('Jumlah Bersih', 0) * (harga_pokok / konversi)

            hpp = int(round(settled_subset.apply(_row_hpp, axis=1).sum())) if not settled_subset.empty and hpp_lookup_map else 0
            laba = penghasilan - hpp

            daily_rows.append({
                'Tanggal': pd.to_datetime(day),
                'Omzet': omzet,
                'Biaya': abs(biaya),
                'Penghasilan': penghasilan,
                'HPP': hpp,
                'Laba': laba,
            })

        chart_df = pd.DataFrame(daily_rows)
        if chart_df.empty:
            return pd.DataFrame()

        chart_df = chart_df.sort_values('Tanggal').reset_index(drop=True)
        day_names = {0: 'Sen', 1: 'Sel', 2: 'Rab', 3: 'Kam', 4: 'Jum', 5: 'Sab', 6: 'Min'}
        chart_df['Hari'] = chart_df['Tanggal'].dt.dayofweek.map(day_names)
        chart_df['TanggalLabel'] = chart_df.apply(
            lambda r: f"{r['Hari']}, {r['Tanggal'].day} {r['Tanggal'].strftime('%b')}",
            axis=1,
        )
        return chart_df
    except Exception as exc:
        LOGGER.warning("Daily chart build failed for %s: %s", order_file_path, exc)
        return pd.DataFrame()


def _build_daily_transaction_detail(order_file_path, result_df, selected_date, hpp_lookup_map=None):
    if not order_file_path or not Path(order_file_path).exists() or selected_date is None:
        return pd.DataFrame()

    try:
        usecols = ['Waktu Pesanan Dibuat', 'Status Pesanan', 'No. Resi', 'No. Pesanan', 'Nama Produk', 'Nama Variasi']
        df_order = pd.read_excel(order_file_path, sheet_name='orders', usecols=usecols)
        df_order['Waktu Pesanan Dibuat'] = pd.to_datetime(df_order['Waktu Pesanan Dibuat'], errors='coerce')
        df_order = df_order.dropna(subset=['Waktu Pesanan Dibuat'])
        df_order = df_order[~df_order['Status Pesanan'].isin(['Batal', 'Belum Bayar'])]
        df_order = df_order[df_order['No. Resi'].notna()].copy()
        df_order['Tanggal'] = df_order['Waktu Pesanan Dibuat'].dt.date
        target_date = pd.to_datetime(selected_date).date()
        df_day = df_order[df_order['Tanggal'] == target_date].copy()
        if df_day.empty or result_df is None or result_df.empty:
            return pd.DataFrame()

        result_day = result_df[result_df['No. Pesanan'].astype(str).isin(df_day['No. Pesanan'].astype(str))].copy()
        if result_day.empty:
            return pd.DataFrame()

        result_day['Tanggal'] = pd.to_datetime(selected_date)
        def _row_hpp(row):
            if not hpp_lookup_map:
                return 0
            info = hpp_lookup_map.get(row.get('Nama Produk'), {})
            harga_pokok = info.get('HargaPokok', 0)
            konversi = info.get('Konversi', 1) or 1
            return int(round(row.get('Jumlah Bersih', 0) * (harga_pokok / konversi)))

        detail = result_day.copy()
        detail['HPP (@)'] = detail.apply(
            lambda row: int(round(
                hpp_lookup_map.get(row.get('Nama Produk'), {}).get('HargaPokok', 0)
                / (hpp_lookup_map.get(row.get('Nama Produk'), {}).get('Konversi', 1) or 1)
            )) if hpp_lookup_map else 0,
            axis=1,
        )
        detail['HPP'] = detail.apply(_row_hpp, axis=1).astype(int)
        subtotal_biaya_series = pd.to_numeric(detail.get('Subtotal Biaya', 0), errors='coerce').fillna(0)
        biaya_proses_series = pd.to_numeric(detail.get('Biaya Proses Pesanan', 0), errors='coerce').fillna(0)
        detail['Total Biaya'] = (subtotal_biaya_series + biaya_proses_series).astype(int)
        detail['Penghasilan'] = (
            pd.to_numeric(detail.get('Subtotal', 0), errors='coerce').fillna(0).astype(int)
            + detail['Total Biaya']
        )
        detail['Laba Bersih'] = (
            detail['Penghasilan']
            - detail['HPP']
        )
        qty_bersih_series = pd.to_numeric(detail.get('Jumlah Bersih', 0), errors='coerce').fillna(0)
        detail['Laba Bersih (@)'] = (
            detail['Laba Bersih'].div(qty_bersih_series.where(qty_bersih_series > 0, pd.NA))
            .round()
            .fillna(0)
            .astype(int)
        )
        subtotal_series = pd.to_numeric(detail.get('Subtotal', 0), errors='coerce').fillna(0)
        detail['Biaya Administrasi (%)'] = pd.to_numeric(detail.get(COL_PCT_ADM, 0), errors='coerce').fillna(0)
        detail['Biaya Gratis Ongkir XTRA (%)'] = pd.to_numeric(detail.get(COL_PCT_XTRA, 0), errors='coerce').fillna(0)
        detail['Biaya Promo XTRA (%)'] = pd.to_numeric(detail.get(COL_PCT_PROMO, 0), errors='coerce').fillna(0)
        detail['Subtotal Biaya (%)'] = pd.to_numeric(detail.get(COL_PCT_SUB_BIAYA, 0), errors='coerce').fillna(0)
        cols = [
            'No. Pesanan', 'Nama Produk', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Harga (@)',
            'Subtotal',
            'Biaya Administrasi', 'Biaya Administrasi (%)',
            'Biaya Gratis Ongkir XTRA', 'Biaya Gratis Ongkir XTRA (%)',
            'Biaya Promo XTRA', 'Biaya Promo XTRA (%)',
            'Subtotal Biaya', 'Subtotal Biaya (%)',
            'Biaya Proses Pesanan',
            'Total Biaya', 'Penghasilan', 'HPP (@)', 'Laba Bersih (@)', 'HPP', 'Laba Bersih', 'Is_Settled'
        ]
        detail = detail[[c for c in cols if c in detail.columns]].copy()
        return detail
    except Exception as exc:
        LOGGER.warning("Daily transaction detail build failed for %s: %s", selected_date, exc)
        return pd.DataFrame()

# ─── Custom CSS untuk tampilan premium ───
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Summary cards */
.summary-container {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1.2rem;
    margin: 1rem 0 1.5rem 0;
}
.summary-card {
    padding: 1.3rem 1.6rem;
    border-radius: 14px;
    background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
    color: #f8fafc;
    box-shadow: 0 4px 20px rgba(0,0,0,0.18);
    transition: transform 0.18s ease;
}
.summary-card:hover {
    transform: translateY(-3px);
}
.summary-card .label {
    font-size: 0.82rem;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.35rem;
}
.summary-card .value {
    font-size: 1.55rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}
.summary-card .pct {
    font-size: 0.78rem;
    color: #fb923c;
    margin-top: 0.2rem;
}
.card-gross .value { color: #38bdf8; }
.card-fees .value { color: #f87171; }
.card-adj .value { color: #eab308; }
.card-net .value { color: #4ade80; }
.card-daily .value { color: #c084fc; }
.card-daily .pct { font-size: 0.78rem; color: #a78bfa; margin-top: 0.2rem; }
.card-potential .value { color: #fbbf24; }
.card-potential .pct { font-size: 0.78rem; color: #fde68a; margin-top: 0.2rem; }
.card-grand .value { color: #2dd4bf; }
.card-grand .pct { font-size: 0.78rem; color: #99f6e4; margin-top: 0.2rem; }
.card-hpp .value { color: #f97316; }
.card-hpp .pct { font-size: 0.78rem; color: #fdba74; margin-top: 0.2rem; }
.card-laba .value { color: #10b981; }
.card-laba .pct { font-size: 0.78rem; color: #6ee7b7; margin-top: 0.2rem; }
.card-settle .value { color: #38bdf8; }
.card-settle .pct { font-size: 0.78rem; margin-top: 0.2rem; }
.unsettled-badge {
    display: inline-block;
    background: rgba(239, 68, 68, 0.2);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.4);
    border-radius: 6px;
    padding: 0.15rem 0.55rem;
    font-size: 0.8rem;
    font-weight: 600;
    margin: 0.2rem 0.2rem 0.2rem 0;
}

/* Fee breakdown pills */
.breakdown-container {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.85rem;
    margin: 0.8rem 0 1.5rem 0;
}
.breakdown-card {
    padding: 0.9rem 1.1rem;
    border-radius: 10px;
    background: rgba(30, 41, 59, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: #f8fafc;
}
.breakdown-card .title {
    font-size: 0.75rem;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    margin-bottom: 0.25rem;
}
.breakdown-card .val {
    font-size: 1.15rem;
    font-weight: 700;
    color: #f1f5f9;
}
.breakdown-card .sub {
    font-size: 0.72rem;
    color: #fb923c;
    margin-top: 0.15rem;
}

/* Section Grouping */
.section-group {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 1.5rem;
}
.section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
    padding-bottom: 0.6rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.section-title {
    font-size: 1.05rem;
    font-weight: 700;
    color: #f1f5f9;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.section-badge {
    font-size: 0.78rem;
    font-weight: 600;
    padding: 0.2rem 0.65rem;
    border-radius: 20px;
    letter-spacing: 0.02em;
}
.badge-settled {
    background: rgba(16, 185, 129, 0.18);
    color: #6ee7b7;
    border: 1px solid rgba(16, 185, 129, 0.35);
}
.badge-pending {
    background: rgba(245, 158, 11, 0.18);
    color: #fde68a;
    border: 1px solid rgba(245, 158, 11, 0.35);
}
.badge-grand {
    background: rgba(45, 212, 191, 0.18);
    color: #99f6e4;
    border: 1px solid rgba(45, 212, 191, 0.35);
}

.dashboard-hero {
    background: #0d1117;
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 8px;
    padding: 1rem 1.05rem;
    margin-bottom: 1rem;
    box-shadow: none;
}
.dashboard-kicker {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: #93c5fd;
    font-weight: 700;
    margin-bottom: 0.35rem;
}
.dashboard-title {
    font-size: 1.55rem;
    line-height: 1.1;
    font-weight: 800;
    color: #f8fafc;
    margin-bottom: 0.65rem;
}
.dashboard-meta {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0.75rem;
    margin-top: 0.85rem;
}
.dashboard-meta-card {
    background: #111827;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 14px;
    padding: 0.85rem 1rem;
}
.dashboard-meta-card .label {
    display: block;
    font-size: 0.73rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #94a3b8;
    margin-bottom: 0.25rem;
}
.dashboard-meta-card .value {
    font-size: 1rem;
    font-weight: 700;
    color: #f8fafc;
}
.dashboard-meta-card.ok .value {
    color: #6ee7b7;
}
.filter-panel {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 1rem 1.2rem;
    margin: 1rem 0 1.2rem 0;
}
/* Card styling untuk metric dan panel interaktif dashboard */
[data-testid="stMetric"] {
    background: linear-gradient(135deg, rgba(30, 41, 59, 0.96) 0%, rgba(15, 23, 42, 0.92) 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 1rem 1.05rem;
    min-height: 92px;
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.16);
    font-family: 'Inter', sans-serif;
}
[data-testid="stMetricLabel"] {
    color: #94a3b8 !important;
    font-size: 0.72rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
[data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] div {
    color: #94a3b8 !important;
}
[data-testid="stMetricValue"] {
    color: #f8fafc !important;
    font-size: 1.25rem !important;
    font-weight: 800 !important;
}
[data-testid="stMetricValue"] div,
[data-testid="stMetricValue"] p,
[data-testid="stMetricValue"] span {
    color: #f8fafc !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 800 !important;
}
[data-testid="stMetricDelta"] {
    color: #cbd5e1 !important;
}
[data-testid="stVerticalBlockBorderWrapper"] {
    background: linear-gradient(135deg, rgba(15, 23, 42, 0.92) 0%, rgba(30, 41, 59, 0.88) 48%, rgba(17, 24, 39, 0.92) 100%);
    border: 1px solid rgba(148, 163, 184, 0.18) !important;
    border-radius: 20px;
    box-shadow: 0 14px 35px rgba(0, 0, 0, 0.20);
}
[data-testid="stVerticalBlockBorderWrapper"]:has(.section-parent-card) {
    background: linear-gradient(135deg, rgba(14, 42, 76, 0.98) 0%, rgba(19, 63, 105, 0.94) 48%, rgba(15, 42, 74, 0.98) 100%);
    border: 1px solid rgba(96, 165, 250, 0.34) !important;
    border-radius: 20px;
    padding: 1.4rem 1.5rem !important;
    margin: 0.75rem 0;
    box-shadow: 0 14px 35px rgba(3, 25, 52, 0.28);
}
[data-testid="stExpander"] {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
}
[data-testid="stExpander"] summary {
    color: #f1f5f9;
    font-weight: 700;
}
/* Informasi pembatalan disajikan melalui card Anomali & Risiko, bukan alert berulang. */
[data-testid="stAlert"] {
    display: none !important;
}
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.85rem;
    margin: 1rem 0 1.2rem 0;
}
.kpi-card {
    padding: 0.9rem 1rem;
    border-radius: 14px;
    background: linear-gradient(135deg, rgba(30, 41, 59, 0.96) 0%, rgba(15, 23, 42, 0.92) 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: none;
}
.kpi-card .label {
    display: block;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #94a3b8;
    margin-bottom: 0.35rem;
    font-weight: 700;
}
.kpi-card .value {
    font-size: 1.2rem;
    line-height: 1.1;
    font-weight: 800;
    color: #f8fafc;
}
.kpi-card .pct {
    font-size: 0.72rem;
    color: #cbd5e1;
    margin-top: 0.25rem;
}
.kpi-gross .value { color: #38bdf8; }
.kpi-fee .value { color: #f87171; }
.kpi-net .value { color: #4ade80; }
.kpi-hpp .value { color: #f97316; }
.kpi-profit .value { color: #10b981; }
.kpi-margin .value { color: #c084fc; }
.section-card-grid {
    display: grid;
    gap: 0.85rem;
    margin: 0.75rem 0 0.8rem 0;
}
.risk-card-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.projection-card-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
.section-metric-card {
    padding: 0.9rem 1rem;
    border-radius: 14px;
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.18);
    box-shadow: none;
}
.section-metric-card .label {
    display: block;
    color: #94a3b8;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 0.35rem;
}
.section-metric-card .value {
    color: #f8fafc;
    font-size: 1.2rem;
    font-weight: 800;
    line-height: 1.15;
}
.section-metric-card .sub {
    color: #cbd5e1;
    font-size: 0.72rem;
    margin-top: 0.3rem;
}
.section-parent-card {
    margin: 0 0 0.65rem 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
}
.section-parent-card .title { color: #f8fafc; font-size: 1.45rem; line-height: 1.1; font-weight: 800; }
.section-parent-card .description { color: #bfdbfe; font-size: 0.78rem; margin-top: 0.45rem; letter-spacing: 0.03em; }
.metric-blue .value { color: #38bdf8; }
.metric-green .value { color: #4ade80; }
.metric-red .value { color: #f87171; }
.metric-orange .value { color: #f97316; }
.metric-amber .value { color: #fbbf24; }
.metric-teal .value { color: #2dd4bf; }
.kpi-row-triplet {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.85rem;
}
.daily-summary-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
}
.daily-summary-grid .kpi-row-triplet {
    display: contents;
}
.kpi-chip {
    display: inline-block;
    margin-top: 0.5rem;
    padding: 0.32rem 0.7rem;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    background: rgba(99, 102, 241, 0.18);
    color: #c7d2fe;
    font-size: 0.74rem;
    font-weight: 700;
}
.weakest-product-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    margin-top: 0.15rem;
}
.weakest-product-name {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 1rem;
    line-height: 1.15;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.weakest-product-laba {
    flex: 0 0 auto;
    font-size: 0.92rem;
    color: #cbd5e1;
    white-space: nowrap;
}
.kpi-row-inline {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.85rem;
}

/* Tautan navigasi dapat dibuka pada tab baru lewat Ctrl/Cmd+klik atau klik kanan. */
.sidebar-nav-link {
    display: block;
    padding: 0.7rem 0.8rem;
    margin: 0.35rem 0;
    border-radius: 8px;
    color: #cbd5e1 !important;
    text-decoration: none !important;
    font-weight: 600;
    border: 1px solid transparent;
}
.sidebar-nav-link:hover {
    background: rgba(99, 102, 241, 0.14);
    color: #e0e7ff !important;
}
.sidebar-nav-link.active {
    background: rgba(99, 102, 241, 0.22);
    color: #e0e7ff !important;
    border-color: rgba(129, 140, 248, 0.42);
}
.sidebar-brand-link {
    color: inherit !important;
    text-decoration: none !important;
}
.sidebar-brand-link:hover {
    color: #a5b4fc !important;
}
</style>
""", unsafe_allow_html=True)

# ─── Sidebar Navigation ───
menu = st.query_params.get("page", "dashboard")
if menu not in {"dashboard", "order", "reconciliation", "customers", "hpp", "stock", "settings"}:
    menu = "dashboard"
session_query = f"session={st.session_state.session_id}"

with st.sidebar:
    st.markdown(
        f'<h2><a class="sidebar-brand-link" href="?reset=1" target="_self" title="Mulai sesi baru">🏪 Warung Aisha Tool</a></h2>',
        unsafe_allow_html=True,
    )
    st.caption("Alat Analisis & Manajemen Penjualan Shopee")
    st.divider()

    st.markdown("📌 **Navigasi**")
    dashboard_active = " active" if menu == "dashboard" else ""
    reconciliation_active = " active" if menu in {"reconciliation", "order"} else ""
    hpp_active = " active" if menu == "hpp" else ""
    stock_active = " active" if menu == "stock" else ""
    settings_active = " active" if menu == "settings" else ""
    customers_active = " active" if menu == "customers" else ""
    st.markdown(
        f'<a class="sidebar-nav-link{dashboard_active}" href="?{session_query}" target="_self">🏠 Dashboard</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{reconciliation_active}" href="?page=order&{session_query}" target="_self">📋 Order</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{customers_active}" href="?page=customers&{session_query}" target="_self">👥 Customers</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{stock_active}" href="?page=stock&{session_query}" target="_self">📦 Valuasi Stok</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{hpp_active}" href="?page=hpp&{session_query}" target="_self">📦 Kelola Master HPP</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{settings_active}" href="?page=settings&{session_query}" target="_self">⚙️ Setting</a>',
        unsafe_allow_html=True,
    )
    st.caption("Gunakan Ctrl/Cmd+klik atau klik kanan → buka di tab baru.")
    st.divider()

    if False:
        st.markdown("⚙️ **Dashboard Setting**")
        if 'laba_warn_threshold' not in st.session_state:
            st.session_state.laba_warn_threshold = 10000
        st.session_state.laba_warn_threshold = st.number_input(
            "Threshold laba kuning",
            min_value=0,
            max_value=1000000,
            value=int(st.session_state.laba_warn_threshold),
            step=1000,
            key="laba_warn_threshold_sidebar",
            help="Nilai laba bersih sampai batas ini akan diberi warna kuning. Di atasnya hijau.",
        )
        st.caption("Dipakai untuk warna kolom Laba Bersih di detail transaksi harian.")
        st.divider()


# ==============================================================================
# 🏠 MENU 1: DASHBOARD (belum ada konten)
# ==============================================================================
if menu == "dashboard":
    st.title("Dashboard Penjualan")
    if 'result' not in st.session_state:
        st.subheader("Mulai dari sini")
        st.caption("Unggah laporan Order dan Penghasilan Shopee, lalu proses untuk mengisi dashboard.")
        dashboard_order = st.file_uploader("Laporan Order Shopee (.xlsx)", type=["xlsx"], key="order_uploader")
        dashboard_income = st.file_uploader("Laporan Penghasilan Shopee (.xlsx)", type=["xlsx"], key="income_uploader")
        dashboard_start_input = None
        dashboard_end_input = None
        if dashboard_order is not None:
            try:
                order_min_date, order_max_date = get_order_date_bounds(dashboard_order)
                dashboard_start_input = st.date_input("Tanggal Mulai", value=order_min_date, min_value=order_min_date, max_value=order_max_date, key="dashboard_date_start")
                dashboard_end_input = st.date_input("Tanggal Akhir", value=order_max_date, min_value=order_min_date, max_value=order_max_date, key="dashboard_date_end")
            except Exception:
                st.warning("Rentang tanggal belum dapat dibaca dari file Order.")
        dashboard_submit = False
        if dashboard_order is not None and dashboard_income is not None:
            dashboard_submit = st.button(
                "Proses Rekonsiliasi & Tampilkan Dashboard",
                type="primary",
                use_container_width=True,
            )
        if dashboard_order is not None:
            st.session_state.uploaded_order_bytes = persist_session_order_upload(dashboard_order)
            st.session_state.uploaded_order_name = dashboard_order.name
        if dashboard_income is not None:
            st.session_state.uploaded_income_name = dashboard_income.name
            st.session_state.uploaded_income_bytes = dashboard_income.getvalue()
        if dashboard_order is not None and dashboard_income is not None:
            order_valid, order_message = _validate_report_upload(dashboard_order, "order")
            income_valid, income_message = _validate_report_upload(dashboard_income, "income")
            if not order_valid:
                st.error(f"Laporan Order tidak valid: {order_message}")
            if not income_valid:
                st.error(f"Laporan Penghasilan tidak valid: {income_message}")
            dashboard_start, dashboard_end = (dashboard_start_input, dashboard_end_input) if order_valid else (None, None)
            if order_valid and income_valid and dashboard_submit and dashboard_start <= dashboard_end:
                dashboard_order.seek(0)
                dashboard_income.seek(0)
                try:
                    progress = st.progress(0, text="Menyiapkan proses rekonsiliasi...")
                    with st.spinner("Memproses data transaksi..."):
                        progress.progress(25, text="Membaca file Order dan Income...")
                        order_bytes = dashboard_order.getvalue()
                        income_bytes = dashboard_income.getvalue()
                        st.session_state.uploaded_order_bytes = persist_session_order_upload(dashboard_order)
                        st.session_state.uploaded_order_name = dashboard_order.name
                        st.session_state.uploaded_income_bytes = income_bytes
                        st.session_state.uploaded_income_name = dashboard_income.name
                        st.session_state.result = _cached_reconciliation(order_bytes, income_bytes, dashboard_start, dashboard_end)
                        progress.progress(75, text="Menghitung ringkasan dan biaya...")
                        dashboard_income.seek(0)
                        st.session_state.df_adjustments = extract_adjustments(dashboard_income)
                        st.session_state.processed_start_date = dashboard_start
                        st.session_state.processed_end_date = dashboard_end
                        st.session_state.processed_at = datetime.now(ZoneInfo("Asia/Jakarta"))
                        _save_session_result()
                    progress.progress(100, text="Selesai")
                    st.rerun()
                except Exception as exc:
                    st.error("Rekonsiliasi gagal dijalankan. Periksa kembali jenis dan isi file.")
                    st.exception(exc)
            elif dashboard_submit and dashboard_start > dashboard_end:
                st.error("Tanggal Mulai tidak boleh lebih besar dari Tanggal Akhir.")
        elif _load_session_result():
            st.rerun()
        else:
            st.info("Unggah kedua file untuk memulai dashboard.")
    else:
        with st.expander("Ganti File / Periode Data", expanded=True):
            active_order_name = st.session_state.get("uploaded_order_name", "Belum ada")
            active_income_name = st.session_state.get("uploaded_income_name", "Belum ada")
            active_start = st.session_state.get("processed_start_date", "-")
            active_end = st.session_state.get("processed_end_date", "-")
            st.info(f"Data aktif — Order: **{active_order_name}** · Income: **{active_income_name}** · Periode: **{active_start} s.d. {active_end}**")
            with st.form("dashboard_reprocess_form"):
                upload_col_order, upload_col_income, date_col_start, date_col_end = st.columns(4)
                with upload_col_order:
                    replace_order = st.file_uploader("Laporan Order aktif / baru (.xlsx)", type=["xlsx"], key="order_uploader")
                with upload_col_income:
                    replace_income = st.file_uploader("Laporan Income aktif / baru (.xlsx)", type=["xlsx"], key="income_uploader")
                replace_start = active_start if active_start != "-" else datetime.now().date()
                replace_end = active_end if active_end != "-" else replace_start
                if replace_order is not None:
                    try:
                        replace_min, replace_max = get_order_date_bounds(replace_order)
                        with date_col_start:
                            replace_start = st.date_input("Tanggal Mulai", replace_min, min_value=replace_min, max_value=replace_max, key="replace_date_start")
                        with date_col_end:
                            replace_end = st.date_input("Tanggal Akhir", replace_max, min_value=replace_min, max_value=replace_max, key="replace_date_end")
                    except Exception as exc:
                        st.error(f"Rentang tanggal tidak dapat dibaca: {exc}")
                else:
                    with date_col_start:
                        replace_start = st.date_input("Tanggal Mulai", replace_start, key="replace_date_start_active")
                    with date_col_end:
                        replace_end = st.date_input("Tanggal Akhir", replace_end, key="replace_date_end_active")
                replace_submit = st.form_submit_button("Proses Ulang dengan Filter Tanggal", type="primary", use_container_width=True)
            if replace_submit and replace_start <= replace_end:
                try:
                    order_bytes = replace_order.getvalue() if replace_order is not None else st.session_state.uploaded_order_bytes
                    income_bytes = replace_income.getvalue() if replace_income is not None else st.session_state.uploaded_income_bytes
                    if replace_order is not None:
                        st.session_state.uploaded_order_bytes = persist_session_order_upload(replace_order)
                        st.session_state.uploaded_order_name = replace_order.name
                    if replace_income is not None:
                        st.session_state.uploaded_income_name = replace_income.name
                        st.session_state.uploaded_income_bytes = income_bytes
                    with st.spinner("Memproses data baru..."):
                        st.session_state.result = _cached_reconciliation(order_bytes, income_bytes, replace_start, replace_end)
                        st.session_state.processed_start_date = replace_start
                        st.session_state.processed_end_date = replace_end
                        st.session_state.processed_at = datetime.now(ZoneInfo("Asia/Jakarta"))
                        _save_session_result()
                    st.rerun()
                except Exception as exc:
                    st.error("Data baru gagal diproses.")
                    st.exception(exc)
        result = st.session_state.result
        proc_start = st.session_state.get('processed_start_date')
        proc_end = st.session_state.get('processed_end_date')
        processed_at = st.session_state.get('processed_at')
        if not isinstance(processed_at, datetime):
            processed_at = datetime.now(ZoneInfo("Asia/Jakarta"))

        period_text = _format_processed_period(proc_start, proc_end)
        status_text = "✓ Order + Income berhasil diproses" if not result.empty else "Data belum tersedia"
        last_processing_text = processed_at.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%d %b %Y %H:%M")
        hpp_source = st.session_state.get('uploaded_hpp_file') if 'uploaded_hpp_file' in st.session_state else None
        if hpp_source is not None:
            try:
                hpp_source.seek(0)
            except Exception:
                pass
        hpp_lookup = _build_hpp_lookup_for_dashboard(result, hpp_source=hpp_source)
        kpi_g = _compute_dashboard_kpis(result, hpp_lookup)
        total_pending = kpi_g['pending_count']
        total_omzet = kpi_g['total_subtotal']
        total_penghasilan = kpi_g['total_penghasilan']
        total_biaya = kpi_g['total_biaya']
        total_hpp = kpi_g['total_hpp']
        laba_bersih = kpi_g['laba_bersih']
        margin_laba = kpi_g['margin_laba']
        pending_omzet = kpi_g['unsettled_subtotal']
        pending_biaya = abs(kpi_g['estimated_fee'])
        pending_penghasilan = kpi_g['estimated_income']
        pending_hpp = kpi_g['estimated_hpp']
        pending_laba = kpi_g['estimated_profit']

        st.markdown(
            f"""
            <div class="dashboard-hero">
                <div class="dashboard-meta">
                    <div class="dashboard-meta-card">
                        <span class="label">Range Tanggal Penjualan</span>
                        <div class="value">{period_text}</div>
                    </div>
                    <div class="dashboard-meta-card ok">
                        <span class="label">Status Data</span>
                        <div class="value">{status_text}</div>
                    </div>
                    <div class="dashboard-meta-card">
                        <span class="label">Last Processing</span>
                        <div class="value">{last_processing_text}</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Slot dibuat berurutan agar section selalu tampil sesuai alur dashboard.
        overview_slot = st.empty()
        settled_slot = st.empty()
        projection_slot = st.empty()
        anomaly_slot = st.empty()
        top_products_slot = st.empty()
        profitability_slot = st.empty()
        hpp_health_slot = st.empty()

        # Kesehatan mapping HPP agar angka laba selalu memiliki konteks cakupan biaya.
        hpp_health_section = hpp_health_slot.container(border=True)
        hpp_health_section.markdown(
            '<div class="section-parent-card section-order-hpp"><div class="title">HPP Health</div>'
            '<div class="description">Indikator kelengkapan HPP mapping yang menjadi dasar perhitungan laba.</div></div>',
            unsafe_allow_html=True,
        )
        hpp_products = result['Nama Produk'].dropna().astype(str).str.strip().loc[lambda values: values.ne('')].unique()
        hpp_total_products = len(hpp_products)
        confirmed_mapping = load_mapping()
        hpp_confirmed_products = [
            product for product in hpp_products
            if confirmed_mapping.get(product)
            and hpp_lookup.get(product, {}).get('HargaPokok', 0) > 0
        ]
        hpp_missing_count = max(hpp_total_products - len(hpp_confirmed_products), 0)
        hpp_coverage = len(hpp_confirmed_products) / hpp_total_products * 100 if hpp_total_products else 0
        hpp_health_section.markdown(
            f"**HPP Coverage**  \n"
            f"Produk dengan HPP valid **{hpp_coverage:.1f}%** &nbsp;&nbsp; "
            f"Produk belum memiliki HPP **{100 - hpp_coverage:.1f}%**  \n"
            f"⚠️ Profit dihitung hanya untuk produk dengan HPP terkonfirmasi. Produk belum memiliki HPP: **{hpp_missing_count:,}**"
        )
        health_cols = hpp_health_section.columns(2)
        health_cols[0].metric('🟢 HPP Confirmed', len(hpp_confirmed_products))
        health_cols[1].metric('🔴 HPP Missing', hpp_missing_count)

        # Ranking produk: metrik dapat diganti agar produk tidak dinilai dari omzet saja.
        top_products_section = top_products_slot.container(border=True)
        top_products_section.markdown(
            '<div class="section-parent-card section-order-top"><div class="title">Top Products</div>'
            '<div class="description">🏆 Produk Terlaris — omzet tinggi belum tentu laba tinggi.</div></div>',
            unsafe_allow_html=True,
        )
        ranking_metric = top_products_section.radio(
            "Urutkan berdasarkan",
            ["Omzet", "Qty", "Laba", "Margin"],
            horizontal=True,
            key="dashboard_top_products_metric",
        )
        top_settled = result[result['Is_Settled'] == True].copy() if 'Is_Settled' in result.columns else result.copy()
        if 'Nama Produk' in top_settled.columns:
            top_settled = top_settled[top_settled['Nama Produk'].astype(str).isin(hpp_lookup)].copy()
        if not top_settled.empty and 'Nama Produk' in top_settled.columns:
            zero_series = pd.Series(0, index=top_settled.index)
            top_settled['_qty'] = pd.to_numeric(top_settled.get('Jumlah Bersih', zero_series), errors='coerce').fillna(0)
            top_settled['_omzet'] = pd.to_numeric(top_settled.get('Subtotal', zero_series), errors='coerce').fillna(0)
            top_settled['_biaya'] = pd.to_numeric(top_settled.get('Total Biaya', zero_series), errors='coerce').fillna(0)
            top_settled['_income'] = top_settled['_omzet'] + top_settled['_biaya']
            top_settled['_hpp'] = top_settled.apply(
                lambda row: row['_qty'] * (
                    hpp_lookup.get(row['Nama Produk'], {}).get('HargaPokok', 0)
                    / (hpp_lookup.get(row['Nama Produk'], {}).get('Konversi', 1) or 1)
                ), axis=1,
            )
            top_products = top_settled.groupby('Nama Produk', dropna=False).agg(
                Qty=('_qty', 'sum'), Omzet=('_omzet', 'sum'),
                _biaya=('_biaya', 'sum'), _income=('_income', 'sum'), _hpp=('_hpp', 'sum'),
            ).reset_index()
            top_products['Laba'] = top_products['_income'] - top_products['_hpp']
            top_products['Margin'] = top_products.apply(
                lambda row: row['Laba'] / row['Omzet'] * 100 if row['Omzet'] > 0 else 0, axis=1
            )

            sort_column = {'Omzet': 'Omzet', 'Qty': 'Qty', 'Laba': 'Laba', 'Margin': 'Margin'}[ranking_metric]
            top_products = top_products.sort_values(sort_column, ascending=False).head(10).reset_index(drop=True)
            total_top_profit = top_products['Laba'].sum()
            top_products.insert(0, '#', range(1, len(top_products) + 1))
            top_products = top_products.rename(columns={
                'Nama Produk': 'Produk',
                'Omzet': 'Omzet Kotor',
                '_biaya': 'Total Biaya',
                '_income': 'Penghasilan',
                '_hpp': 'HPP',
                'Laba': 'Laba Bersih (Setelah HPP)',
            })
            top_products['Qty'] = top_products['Qty'].map(lambda value: f"{value:,.0f}")
            for amount_column in ['Omzet Kotor', 'Total Biaya', 'Penghasilan', 'HPP', 'Laba Bersih (Setelah HPP)']:
                top_products[amount_column] = top_products[amount_column].map(
                    lambda value: f"Rp {value:,.0f}"
                )
            top_products['Margin'] = top_products['Margin'].map(lambda value: f"{value:,.1f}%")
            top_products_section.dataframe(
                top_products[['#', 'Produk', 'Qty', 'Omzet Kotor', 'Total Biaya', 'Penghasilan', 'HPP', 'Laba Bersih (Setelah HPP)', 'Margin']],
                use_container_width=True,
                hide_index=True,
            )
            top_products_section.caption(f"Total laba bersih Top 10 produk: Rp {total_top_profit:,.0f}")
        else:
            top_products_section.info('Belum ada data produk settled untuk dibuat ranking.')

        settled_section = settled_slot.container(border=True)
        settled_section.markdown('<div class="section-parent-card section-order-settled"><div class="title">Pesanan Settled</div><div class="description">Ringkasan omzet, biaya, penghasilan, HPP, dan laba dari pesanan yang sudah settled.</div></div>', unsafe_allow_html=True)

        # KPI utama pesanan settled
        if 'result' in st.session_state:
            settled_rows = result[result['Is_Settled'] == True] if 'Is_Settled' in result.columns else result
            settled_order_count = settled_rows['No. Pesanan'].dropna().nunique() if 'No. Pesanan' in settled_rows.columns else 0
            fee_detail = {
                'Administrasi': abs(int(settled_rows['Biaya Administrasi'].sum())) if 'Biaya Administrasi' in settled_rows.columns else 0,
                'Ongkir XTRA': abs(int(settled_rows['Biaya Gratis Ongkir XTRA'].sum())) if 'Biaya Gratis Ongkir XTRA' in settled_rows.columns else 0,
                'Promo XTRA': abs(int(settled_rows['Biaya Promo XTRA'].sum())) if 'Biaya Promo XTRA' in settled_rows.columns else 0,
                'Proses': abs(int(settled_rows['Biaya Proses Pesanan'].sum())) if 'Biaya Proses Pesanan' in settled_rows.columns else 0,
                'Pajak': abs(int(settled_rows['Pajak'].sum())) if 'Pajak' in settled_rows.columns else 0,
            }
            laba_kpi_color = "#10b981" if laba_bersih >= 0 else "#f87171"
            settled_section.markdown(
                f"""
                <div class="kpi-grid">
                    <div class="kpi-card kpi-gross">
                        <span class="label">Omzet Kotor</span>
                        <div class="value">Rp {total_omzet:,.0f}</div>
                        <div class="pct">{settled_order_count:,} pesanan settled</div>
                    </div>
                    <div class="kpi-card kpi-fee">
                        <span class="label">Total Biaya Shopee</span>
                        <div class="value">Rp {total_biaya:,.0f}</div>
                        <div class="pct">Admin Rp {fee_detail['Administrasi']:,} · Ongkir Rp {fee_detail['Ongkir XTRA']:,}<br>Promo Rp {fee_detail['Promo XTRA']:,} · Proses Rp {fee_detail['Proses']:,} · Pajak Rp {fee_detail['Pajak']:,}</div>
                    </div>
                    <div class="kpi-card kpi-net">
                        <span class="label">Penghasilan Bersih</span>
                        <div class="value">Rp {total_penghasilan:,.0f}</div>
                        <div class="pct">Setelah biaya Shopee & penyesuaian</div>
                    </div>
                    <div class="kpi-card kpi-hpp">
                        <span class="label">HPP</span>
                        <div class="value">Rp {total_hpp:,.0f}</div>
                        <div class="pct">Modal produk terjual</div>
                    </div>
                    <div class="kpi-card kpi-profit">
                        <span class="label">Laba Bersih</span>
                        <div class="value" style="color: {laba_kpi_color};">Rp {laba_bersih:,.0f}</div>
                        <div class="pct">Penghasilan - HPP = Laba</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            projection_section = projection_slot.container(border=True)
            projection_section.markdown('<div class="section-parent-card section-order-projection"><div class="title">Pesanan Unsettled</div><div class="description">Estimasi pending berdasarkan pola fee settled dan mapping HPP saat ini. Tidak digabung ke KPI aktual.</div></div>', unsafe_allow_html=True)
            pending_laba_class = "metric-green" if pending_laba >= 0 else "metric-red"
            projection_section.markdown(
                f"""
                <div class="section-card-grid projection-card-grid">
                    <div class="section-metric-card metric-blue"><span class="label">Estimasi Omzet Kotor</span><div class="value">Rp {pending_omzet:,.0f}</div><div class="sub">{total_pending:,} pesanan unsettled</div></div>
                    <div class="section-metric-card metric-red"><span class="label">Estimasi Total Biaya Shopee</span><div class="value">Rp {pending_biaya:,.0f}</div><div class="sub">Estimasi fee layanan, admin, dan pajak</div></div>
                    <div class="section-metric-card metric-green"><span class="label">Estimasi Penghasilan</span><div class="value">Rp {pending_penghasilan:,.0f}</div><div class="sub">Omzet setelah biaya</div></div>
                    <div class="section-metric-card metric-orange"><span class="label">Estimasi HPP</span><div class="value">Rp {pending_hpp:,.0f}</div><div class="sub">Modal produk pending</div></div>
                    <div class="section-metric-card {pending_laba_class}"><span class="label">Proyeksi Laba Bersih</span><div class="value">Rp {pending_laba:,.0f}</div><div class="sub">Penghasilan - HPP</div></div>
                </div>
                """, unsafe_allow_html=True,
            )

            daily_order_file = _find_session_order_file()
            cancelled_summary = _build_cancelled_order_summary(
                daily_order_file,
                start_date=proc_start,
                end_date=proc_end,
                settled_fee_ratio=(abs(total_biaya) / total_omzet if total_omzet > 0 else 0.0),
            )
            net_sales_shopee = total_omzet + pending_omzet
            gross_sales_shopee = net_sales_shopee + cancelled_summary['value']
            no_resi_value = 0
            no_resi_count = 0
            no_resi = pd.DataFrame()
            order_audit = pd.DataFrame()
            order_file_total = gross_sales_shopee
            try:
                order_audit = pd.read_excel(daily_order_file, sheet_name='orders') if daily_order_file else pd.DataFrame()
                if not order_audit.empty:
                    order_audit['Waktu Pesanan Dibuat'] = pd.to_datetime(order_audit['Waktu Pesanan Dibuat'], errors='coerce')
                    order_audit = order_audit[
                        (order_audit['Waktu Pesanan Dibuat'] >= pd.to_datetime(proc_start).normalize())
                        & (order_audit['Waktu Pesanan Dibuat'] <= pd.to_datetime(proc_end).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))
                    ]
                    no_resi = order_audit[
                        ~order_audit['Status Pesanan'].isin(['Batal', 'Belum Bayar'])
                        & order_audit['No. Resi'].isna()
                    ].copy()
                    no_resi['audit_value'] = pd.to_numeric(
                        no_resi['Subtotal Pesanan'].astype(str).str.replace('.', '', regex=False).str.replace(',', '', regex=False),
                        errors='coerce',
                    ).fillna(0)
                    no_resi_value = int(no_resi['audit_value'].sum())
                    no_resi_count = int(no_resi['No. Pesanan'].nunique())
                    order_file_total = int(pd.to_numeric(
                        order_audit['Subtotal Pesanan'].astype(str).str.replace('.', '', regex=False).str.replace(',', '', regex=False),
                        errors='coerce',
                    ).fillna(0).sum())
            except Exception:
                pass
            overview_section = overview_slot.container(border=True)
            total_order_count = order_audit['No. Pesanan'].nunique() if not order_audit.empty else 0
            settled_order_count = result.loc[result['Is_Settled'] == True, 'No. Pesanan'].nunique() if 'Is_Settled' in result.columns else 0
            pending_mask = (result['Is_Settled'] == False) & (~result.get('Is_Cancelled', False)) if 'Is_Settled' in result.columns else pd.Series(False, index=result.index)
            pending_order_count = result.loc[pending_mask, 'No. Pesanan'].nunique() if 'No. Pesanan' in result.columns else 0
            seller_cancel_rate = (
                cancelled_summary['seller_count'] / total_order_count * 100
                if total_order_count > 0 else 0
            )
            unresolved_7d_rate = 0.0
            unresolved_7d_count = 0
            seller_cancel_7d_count = 0
            buyer_return_7d_count = 0
            if not order_audit.empty:
                # Shopee mereset metrik ini setiap Minggu; gunakan minggu
                # berjalan (Senin-Minggu), bukan rolling 7 hari kalender.
                period_end = pd.to_datetime(proc_end).normalize()
                seven_day_start = period_end - pd.Timedelta(days=period_end.weekday())
                recent_orders = order_audit[
                    (order_audit['Waktu Pesanan Dibuat'] >= seven_day_start)
                    & (order_audit['Waktu Pesanan Dibuat'] <= pd.to_datetime(proc_end).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))
                ].copy()
                recent_orders['reason_text'] = recent_orders.get('Alasan Pembatalan', pd.Series('', index=recent_orders.index)).astype(str).str.casefold()
                seller_mask = (
                    recent_orders['reason_text'].str.contains(r'dibatalkan oleh penjual|cancelled by seller', regex=True, na=False)
                    | recent_orders['reason_text'].str.contains(r'dibatalkan secara otomatis.*penjual|otomatis.*penjual tidak', regex=True, na=False)
                ) & ~recent_orders['reason_text'].str.contains(r'dibatalkan oleh pembeli|cancelled by buyer', regex=True, na=False)
                return_text = recent_orders.get('Status Pembatalan/ Pengembalian', pd.Series('', index=recent_orders.index)).astype(str).str.casefold()
                returned_qty = pd.to_numeric(recent_orders.get('Returned quantity', pd.Series(0, index=recent_orders.index)), errors='coerce').fillna(0)
                return_mask = return_text.str.contains(r'kembali|return|refund', regex=True, na=False) | returned_qty.gt(0)
                seller_cancel_7d_count = int(recent_orders.loc[seller_mask, 'No. Pesanan'].nunique())
                buyer_return_7d_count = int(recent_orders.loc[return_mask, 'No. Pesanan'].nunique())
                unresolved_7d_count = int(recent_orders.loc[seller_mask | return_mask, 'No. Pesanan'].nunique())
                recent_order_count = int(recent_orders['No. Pesanan'].nunique())
                unresolved_7d_rate = unresolved_7d_count / recent_order_count * 100 if recent_order_count else 0.0
            overview_section.markdown(
                '<div class="section-parent-card section-order-overview"><div class="title">Overview</div>'
                '<div class="description">Ringkasan penjualan, pesanan, dan laba toko pada periode yang dipilih.</div></div>',
                unsafe_allow_html=True,
            )
            overview_section.markdown(
                f'''<div class="section-card-grid risk-card-grid">
                    <div class="section-metric-card metric-blue"><span class="label">Total Penjualan / Gross Sales</span><div class="value">Rp {order_file_total:,.0f}</div><div class="sub">{total_order_count:,} order · Net Sales + Nilai Pesanan Batal</div></div>
                    <div class="section-metric-card metric-green"><span class="label">Net Sales</span><div class="value">Rp {net_sales_shopee:,.0f}</div><div class="sub">{settled_order_count + pending_order_count:,} order · Settled + Pending</div></div>
                    <div class="section-metric-card metric-blue"><span class="label">Pesanan Settled</span><div class="value">Rp {total_omzet:,.0f}</div><div class="sub">{settled_order_count:,} order · Masuk penghasilan aktual</div></div>
                    <div class="section-metric-card metric-orange"><span class="label">Pesanan Pending</span><div class="value">Rp {pending_omzet:,.0f}</div><div class="sub">{pending_order_count:,} order · Masih menunggu settlement</div></div>
                    <div class="section-metric-card metric-green"><span class="label">Laba Bersih Settled</span><div class="value">Rp {laba_bersih:,.0f}</div><div class="sub">Laba aktual pesanan settled</div></div>
                    <div class="section-metric-card metric-orange"><span class="label">Laba Bersih Unsettled</span><div class="value">Rp {pending_laba:,.0f}</div><div class="sub">Proyeksi laba pesanan pending</div></div>
                    <div class="section-metric-card metric-blue"><span class="label">Total Laba Bersih</span><div class="value">Rp {laba_bersih + pending_laba:,.0f}</div><div class="sub">Settled + proyeksi unsettled</div></div>
                    <div class="section-metric-card metric-amber"><span class="label">Valid Tanpa No. Resi</span><div class="value">Rp {no_resi_value:,.0f}</div><div class="sub">{no_resi_count:,} pesanan; termasuk Pending</div></div>
                    <div class="section-metric-card metric-orange"><span class="label">Total Nilai Pembatalan</span><div class="value">Rp {cancelled_summary['value']:,.0f}</div><div class="sub">{cancelled_summary['count']:,} order · Nilai bruto pesanan batal</div></div>
                </div>''',
                unsafe_allow_html=True,
            )
            anomaly_section = anomaly_slot.container(border=True)
            anomaly_section.markdown('<div class="section-parent-card section-order-anomaly"><div class="title">Pesanan Batal / Tidak Terselesaikan</div><div class="description">Seluruh informasi pembatalan, return, risiko penalti, dan estimasi dampak finansial.</div></div>', unsafe_allow_html=True)
            if cancelled_summary['count'] > 0:
                st.warning(
                    f"Terdapat {cancelled_summary['count']:,} pesanan dibatalkan "
                    f"dengan nilai bruto sekitar Rp {cancelled_summary['value']:,.0f}. "
                    f"Tingkat pembatalan: {cancelled_summary['rate']:.2f}%.",
                    icon="⚠️",
                )
            else:
                st.success("Tidak ada pesanan dibatalkan pada periode ini.", icon="✅")
            anomaly_section.markdown(
                f"""
                <div class="section-card-grid risk-card-grid">
                    <div class="section-metric-card metric-red"><span class="label">Pesanan Dibatalkan</span><div class="value">{cancelled_summary['count']:,}</div><div class="sub">Order berstatus batal</div></div>
                    <div class="section-metric-card metric-orange"><span class="label">Nilai Pesanan Batal</span><div class="value">Rp {cancelled_summary['value']:,.0f}</div><div class="sub">Termasuk dalam Gross Sales</div></div>
                    <div class="section-metric-card metric-amber"><span class="label">Tingkat Pembatalan Global</span><div class="value">{cancelled_summary['rate']:.2f}%</div><div class="sub">Seluruh pesanan batal ÷ seluruh order</div></div>
                    <div class="section-metric-card metric-red"><span class="label">Dibatalkan oleh Toko</span><div class="value">{cancelled_summary['seller_count']:,}</div><div class="sub">{seller_cancel_rate:.2f}% dari seluruh order · Rp {cancelled_summary['seller_value']:,.0f}</div></div>
                    <div class="section-metric-card metric-red"><span class="label">Tidak Terselesaikan (Minggu Berjalan)</span><div class="value">{unresolved_7d_rate:.2f}%</div><div class="sub">Senin-Minggu · {unresolved_7d_count:,} order · Toko {seller_cancel_7d_count:,} · Return {buyer_return_7d_count:,}</div></div>
                    <div class="section-metric-card metric-blue">
                        <span class="label">Estimasi Penghasilan Hilang</span>
                        <div class="value">Rp {cancelled_summary['income_lost']:,.0f}</div>
                        <div class="sub">Setelah estimasi fee Shopee</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            returns_section = st.container(border=True)
            returns_section.markdown(
                '<div class="section-parent-card"><div class="title">Retur &amp; Pengembalian Dana</div>'
                '<div class="description">Audit transaksi yang perlu ditelusuri terkait status pengiriman, retur, dan pengembalian dana.</div></div>',
                unsafe_allow_html=True,
            )
            if not no_resi.empty:
                returns_section.markdown("#### Audit Pesanan Valid Tanpa No. Resi")
                audit_columns = [
                    column for column in [
                        'No. Pesanan', 'Status Pesanan', 'Waktu Pesanan Dibuat',
                        'Nama Produk', 'Nama Variasi', 'Jumlah', 'Subtotal Pesanan',
                    ] if column in no_resi.columns
                ]
                audit_detail = no_resi[audit_columns].copy()
                if 'Subtotal Pesanan' in audit_detail.columns:
                    audit_detail['Subtotal Pesanan'] = no_resi['audit_value'].map(lambda value: f"Rp {value:,.0f}")
                returns_section.dataframe(audit_detail, use_container_width=True, hide_index=True)
            else:
                returns_section.info("Tidak ada transaksi valid tanpa No. Resi pada periode ini.")
            if cancelled_summary.get('by_type'):
                anomaly_section.markdown("#### Estimasi Penghasilan Hilang berdasarkan Tipe Pembatalan")
                type_cards = ''.join(
                    f'''<div class="section-metric-card metric-blue">
                        <span class="label">{html.escape(str(item['type']))}</span>
                        <div class="value">Rp {item['income_lost']:,.0f}</div>
                        <div class="sub">{item['count']:,} pesanan</div>
                    </div>'''
                    for item in cancelled_summary['by_type']
                )
                anomaly_section.markdown(f'<div class="section-card-grid" style="grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));">{type_cards}</div>', unsafe_allow_html=True)
            cancelled_details = cancelled_summary.get('details', pd.DataFrame())
            with anomaly_section.expander("Lihat detail transaksi dibatalkan", expanded=False):
                if cancelled_details.empty:
                    st.info("Detail transaksi pembatalan tidak tersedia.")
                else:
                    st.dataframe(
                        cancelled_details,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            'Harga Satuan': st.column_config.NumberColumn(
                                'Harga Satuan', format='Rp %d'
                            ),
                            'Nilai Transaksi': st.column_config.NumberColumn(
                                'Nilai Transaksi', format='Rp %d'
                            ),
                            'Estimasi Penghasilan Hilang': st.column_config.NumberColumn(
                                'Estimasi Penghasilan Hilang', format='Rp %d'
                            ),
                        },
                    )
                    st.caption("Tipe pembatalan diambil dari kolom alasan/jenis pembatalan pada file Order. Nilai transaksi = harga satuan × jumlah.")

            chart_df = _build_daily_chart_data(daily_order_file, result, hpp_lookup)
            if not chart_df.empty:
                with st.container(border=True):
                    st.markdown("### Grafik Omzet vs HPP vs Laba")
                    st.caption("Per hari untuk melihat hari yang omzetnya tinggi tetapi labanya ternyata tipis.")
                    chart_view = chart_df.set_index('TanggalLabel')[['Omzet', 'HPP', 'Laba']]
                    st.line_chart(chart_view, height=320)
                with st.expander("Lihat data harian", expanded=False):
                    daily_display = chart_df[['TanggalLabel', 'Omzet', 'Biaya', 'Penghasilan', 'HPP', 'Laba']].copy()
                    st.dataframe(
                        daily_display,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            'TanggalLabel': st.column_config.TextColumn('Tanggal'),
                            'Omzet': st.column_config.NumberColumn('Omzet', format='%,d'),
                            'Biaya': st.column_config.NumberColumn('Biaya', format='%,d'),
                            'Penghasilan': st.column_config.NumberColumn('Penghasilan', format='%,d'),
                            'HPP': st.column_config.NumberColumn('HPP', format='%,d'),
                            'Laba': st.column_config.NumberColumn('Laba', format='%,d'),
                        },
                    )
                    detail_labels = chart_df['TanggalLabel'].tolist()
                    selected_detail_label = st.selectbox("Detail transaksi per hari", detail_labels, key="daily_detail_day")
                    selected_row = chart_df.loc[chart_df['TanggalLabel'] == selected_detail_label].iloc[0]
                    selected_detail_date = selected_row['Tanggal']
                    detail_df = _build_daily_transaction_detail(daily_order_file, result, selected_detail_date, hpp_lookup)
                    if not detail_df.empty:
                        st.caption(f"Detail transaksi untuk {selected_detail_label}")
                        total_orders_day = len(detail_df['No. Pesanan'].dropna().astype(str).unique()) if 'No. Pesanan' in detail_df.columns else len(detail_df)
                        total_items_day = int(detail_df['Jumlah Bersih'].sum()) if 'Jumlah Bersih' in detail_df.columns else 0
                        omzet_day = int(selected_row.get('Omzet', 0))
                        biaya_day = int(selected_row.get('Biaya', 0))
                        penghasilan_day = int(selected_row.get('Penghasilan', 0))
                        hpp_day = int(selected_row.get('HPP', 0))
                        laba_day = int(selected_row.get('Laba', 0))
                        margin_day = (laba_day / penghasilan_day * 100) if penghasilan_day > 0 else 0.0
                        avg_laba_per_order = (laba_day / total_orders_day) if total_orders_day > 0 else 0.0
                        weakest_product_name = "-"
                        weakest_product_laba = 0
                        if 'Nama Produk' in detail_df.columns and 'Laba Bersih' in detail_df.columns:
                            product_laba = (
                                detail_df.groupby('Nama Produk', dropna=False)['Laba Bersih']
                                .sum()
                                .sort_values()
                            )
                            if not product_laba.empty:
                                weakest_product_name = str(product_laba.index[0])
                                weakest_product_laba = int(product_laba.iloc[0])
                        weakest_product_display = f"{weakest_product_name}<br>Rp {weakest_product_laba:,.0f}"
                        weakest_product_button = (
                            "<div class='kpi-chip'>Fokus ke produk ini</div>"
                            if weakest_product_name != "-" else ""
                        )
                        laba_day_color = "#10b981" if laba_day >= 0 else "#f87171"
                        st.markdown(
                            f"""
                            <div class="kpi-grid daily-summary-grid" style="margin-top:0.75rem;">
                                <div class="kpi-card kpi-gross">
                                    <span class="label">Order Hari Ini</span>
                                    <div class="value">{total_orders_day}</div>
                                    <div class="pct">Jumlah pesanan unik</div>
                                </div>
                                <div class="kpi-card kpi-gross">
                                    <span class="label">Qty Bersih</span>
                                    <div class="value">{total_items_day}</div>
                                    <div class="pct">Unit terjual bersih</div>
                                </div>
                                <div class="kpi-row-triplet">
                                    <div class="kpi-card kpi-gross">
                                        <span class="label">Omzet</span>
                                        <div class="value">Rp {omzet_day:,.0f}</div>
                                        <div class="pct">Subtotal settled hari ini</div>
                                    </div>
                                    <div class="kpi-card kpi-fee">
                                        <span class="label">Biaya</span>
                                        <div class="value">Rp {biaya_day:,.0f}</div>
                                        <div class="pct">Total fee transaksi hari ini</div>
                                    </div>
                                    <div class="kpi-card kpi-net">
                                        <span class="label">Penghasilan</span>
                                        <div class="value">Rp {penghasilan_day:,.0f}</div>
                                        <div class="pct">Setelah biaya Shopee & penyesuaian</div>
                                    </div>
                                </div>
                                <div class="kpi-card kpi-hpp">
                                    <span class="label">HPP</span>
                                    <div class="value">Rp {hpp_day:,.0f}</div>
                                    <div class="pct">Modal barang hari ini</div>
                                </div>
                                <div class="kpi-card kpi-profit">
                                    <span class="label">Laba Bersih</span>
                                    <div class="value" style="color: {laba_day_color};">Rp {laba_day:,.0f}</div>
                                    <div class="pct">Penghasilan - HPP</div>
                                </div>
                                <div class="kpi-card kpi-margin">
                                    <span class="label">Margin Hari Ini</span>
                                    <div class="value">{margin_day:.1f}%</div>
                                    <div class="pct">Laba ÷ penghasilan</div>
                                </div>
                                <div class="kpi-card kpi-fee">
                                    <span class="label">Laba / Order</span>
                                    <div class="value">Rp {avg_laba_per_order:,.0f}</div>
                                    <div class="pct">Rata-rata per pesanan</div>
                                </div>
                                <div class="kpi-card kpi-gross">
                                    <span class="label">Produk Terlemah</span>
                                    <div class="weakest-product-line">
                                        <div class="weakest-product-name">{weakest_product_name}</div>
                                        <div class="weakest-product-laba">Rp {weakest_product_laba:,.0f}</div>
                                    </div>
                                    {weakest_product_button}
                                    <div class="pct">Total laba terendah hari ini</div>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        product_focus_options = ['Semua Produk']
                        if 'Nama Produk' in detail_df.columns:
                            product_focus_options.extend(sorted(detail_df['Nama Produk'].dropna().astype(str).unique().tolist()))
                        default_product_focus = weakest_product_name if weakest_product_name in product_focus_options else 'Semua Produk'
                        if weakest_product_name in product_focus_options and st.button("Fokus ke produk ini", key="focus_weakest_product"):
                            st.session_state.daily_detail_product_focus = weakest_product_name
                            st.rerun()
                        selected_product_focus = st.selectbox(
                            "Fokus produk detail",
                            product_focus_options,
                            index=product_focus_options.index(default_product_focus) if default_product_focus in product_focus_options else 0,
                            key="daily_detail_product_focus",
                            help="Pilih produk untuk menyaring detail transaksi hari terpilih.",
                        )
                        if selected_product_focus != 'Semua Produk' and 'Nama Produk' in detail_df.columns:
                            detail_df = detail_df[detail_df['Nama Produk'].astype(str) == selected_product_focus].copy()
                            if detail_df.empty:
                                st.info("Tidak ada transaksi untuk produk yang dipilih pada hari ini.")
                                st.stop()

                        core_detail_cols = [
                            'No. Pesanan', 'Nama Produk', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Harga (@)',
                            'Subtotal', 'Total Biaya', 'Penghasilan', 'HPP (@)', 'Laba Bersih (@)', 'HPP', 'Laba Bersih', 'Is_Settled'
                        ]
                        fee_detail_cols = [
                            'No. Pesanan', 'Nama Produk', 'Biaya Administrasi', 'Biaya Administrasi (%)',
                            'Biaya Gratis Ongkir XTRA', 'Biaya Gratis Ongkir XTRA (%)',
                            'Biaya Promo XTRA', 'Biaya Promo XTRA (%)',
                            'Subtotal Biaya', 'Subtotal Biaya (%)',
                            'Biaya Proses Pesanan', 'Total Biaya'
                        ]
                        core_detail_df = detail_df[[c for c in core_detail_cols if c in detail_df.columns]].copy()
                        fee_detail_df = detail_df[[c for c in fee_detail_cols if c in detail_df.columns]].copy()

                        laba_warn_threshold = int(st.session_state.get('laba_warn_threshold', 10000))
                        def _style_hpp_cell(value):
                            if pd.isna(value) or value == 0:
                                return 'background-color: rgba(245, 158, 11, 0.18); color: #fde68a; font-weight: 700;'
                            return ''

                        def _style_laba_cell(value):
                            if pd.isna(value):
                                return ''
                            if value < 0:
                                return 'background-color: rgba(239, 68, 68, 0.18); color: #fecaca; font-weight: 700;'
                            if value <= laba_warn_threshold:
                                return 'background-color: rgba(234, 179, 8, 0.18); color: #fef08a; font-weight: 700;'
                            return 'background-color: rgba(16, 185, 129, 0.12); color: #bbf7d0; font-weight: 700;'

                        styled_detail_df = (
                            core_detail_df.style
                            .map(_style_hpp_cell, subset=['HPP (@)'])
                            .map(_style_laba_cell, subset=['Laba Bersih'])
                        )
                        detail_table_event = st.dataframe(
                            styled_detail_df,
                            use_container_width=True,
                            hide_index=True,
                            on_select="rerun",
                            selection_mode="single-row",
                            key="daily_detail_table",
                            column_config={
                                'No. Pesanan': st.column_config.TextColumn('No. Pesanan'),
                                'Nama Produk': st.column_config.TextColumn('Nama Produk'),
                                'Harga (@)': st.column_config.NumberColumn('Harga (@)', format='%,d'),
                                'Jumlah': st.column_config.NumberColumn('Jumlah', format='%,d'),
                                'Returned quantity': st.column_config.NumberColumn('Retur', format='%,d'),
                                'Jumlah Bersih': st.column_config.NumberColumn('Qty Bersih', format='%,d'),
                                'Subtotal': st.column_config.NumberColumn('Omzet', format='%,d'),
                                'Total Biaya': st.column_config.NumberColumn(
                                    'Biaya',
                                    format='%,d',
                                    help='Total biaya layanan Shopee, termasuk komponen fee yang tercatat pada transaksi ini.'
                                ),
                                'Penghasilan': st.column_config.NumberColumn(
                                    'Penghasilan',
                                    format='%,d',
                                    help='Omzet + Total Biaya. Biaya Shopee bernilai negatif sesuai logic accounting aplikasi.'
                                ),
                                'HPP (@)': st.column_config.NumberColumn(
                                    'HPP (@)',
                                    format='%,d',
                                    help='Harga pokok per unit. Nilai 0 berarti produk belum termapping ke master HPP.'
                                ),
                                'Laba Bersih (@)': st.column_config.NumberColumn(
                                    'Laba Bersih (@)',
                                    format='%,d',
                                    help='Laba Bersih dibagi Qty Bersih.'
                                ),
                                'HPP': st.column_config.NumberColumn(
                                    'HPP',
                                    format='%,d',
                                    help='Total modal barang untuk transaksi ini. Dihitung dari Qty Bersih × HPP per unit.'
                                ),
                                'Laba Bersih': st.column_config.NumberColumn(
                                    'Laba Bersih',
                                    format='%,d',
                                    help='Penghasilan - HPP. Ini mengikuti logic accounting aplikasi.'
                                ),
                            },
                        )
                        if not fee_detail_df.empty:
                            with st.expander("Rincian Biaya", expanded=True):
                                selected_fee_order = None
                                try:
                                    selected_fee_rows = detail_table_event.selection.rows
                                    if selected_fee_rows:
                                        selected_fee_index = int(selected_fee_rows[0])
                                        if 0 <= selected_fee_index < len(core_detail_df):
                                            selected_fee_order = str(core_detail_df.iloc[selected_fee_index]['No. Pesanan'])
                                except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                                    selected_fee_order = None

                                show_all_fee_rows = st.checkbox(
                                    "Tampilkan semua rincian biaya",
                                    value=False,
                                    key="show_all_daily_fee_rows",
                                )
                                fee_display_df = fee_detail_df.copy()
                                if selected_fee_order and not show_all_fee_rows:
                                    fee_display_df = fee_display_df[
                                        fee_display_df['No. Pesanan'].astype(str) == selected_fee_order
                                    ].copy()
                                    st.caption(f"Rincian biaya untuk order {selected_fee_order}")
                                elif selected_fee_order:
                                    st.caption(f"Order terpilih: {selected_fee_order}")

                                def _style_selected_fee_row(row):
                                    if selected_fee_order and str(row.get('No. Pesanan', '')) == selected_fee_order:
                                        return ['background-color: rgba(59, 130, 246, 0.18); color: #dbeafe; font-weight: 700;'] * len(row)
                                    return [''] * len(row)

                                styled_fee_df = fee_display_df.style.apply(_style_selected_fee_row, axis=1)
                                st.dataframe(
                                    styled_fee_df,
                                    use_container_width=True,
                                    hide_index=True,
                                    column_config={
                                        'No. Pesanan': st.column_config.TextColumn('No. Pesanan'),
                                        'Nama Produk': st.column_config.TextColumn('Nama Produk'),
                                        'Biaya Administrasi': st.column_config.NumberColumn('Biaya Admin', format='%,d'),
                                        'Biaya Administrasi (%)': st.column_config.NumberColumn('(%)', format='%.2f%%'),
                                        'Biaya Gratis Ongkir XTRA': st.column_config.NumberColumn('Biaya Gratis Ongkir XTRA', format='%,d'),
                                        'Biaya Gratis Ongkir XTRA (%)': st.column_config.NumberColumn('(%)', format='%.2f%%'),
                                        'Biaya Promo XTRA': st.column_config.NumberColumn('Biaya Promo XTRA', format='%,d'),
                                        'Biaya Promo XTRA (%)': st.column_config.NumberColumn('(%)', format='%.2f%%'),
                                        'Subtotal Biaya': st.column_config.NumberColumn('Subtotal Biaya', format='%,d'),
                                        'Subtotal Biaya (%)': st.column_config.NumberColumn('(%)', format='%.2f%%'),
                                        'Biaya Proses Pesanan': st.column_config.NumberColumn('Biaya Per Pesanan', format='%,d'),
                                        'Total Biaya': st.column_config.NumberColumn('Total Biaya', format='%,d', help='Subtotal Biaya + Biaya Per Pesanan'),
                                    },
                                )

        # Detail transaksi tersedia di menu Order agar Dashboard tetap ringkas.


# ==============================================================================
# 📊 MENU 2: REKONSILIASI SHOPEE
# ==============================================================================
elif menu in {"reconciliation", "order"}:
    if 'result' not in st.session_state:
        _load_session_result()
    st.title("📋 Order")
    st.write("Detail transaksi berdasarkan file Order dan Income yang aktif dari Dashboard.")

    with st.sidebar:
        st.subheader("📁 Upload File Transaksi")
        uploaded_order_bytes = st.session_state.get("uploaded_order_bytes")
        uploaded_income_bytes = st.session_state.get("uploaded_income_bytes")
        uploaded_order = io.BytesIO(uploaded_order_bytes) if uploaded_order_bytes else None
        uploaded_income = io.BytesIO(uploaded_income_bytes) if uploaded_income_bytes else None
        uploaded_hpp = st.file_uploader(
            "3. Laporan Master HPP Periode Ini (Opsional)", 
            type=['xlsx'],
            key="hpp_override_uploader",
            help="Unggah jika ingin memakai HPP periode khusus. Jika kosong, sistem otomatis memakai master HPP default toko."
        )
        # Simpan sumber upload agar tab Pemetaan tetap dapat membaca daftar produk
        # walaupun pengguna berpindah menu sebelum menjalankan rekonsiliasi.
        if False:
            # Simpan byte aktual dan salinan fisik terisolasi per session, bukan
            # hanya handle UploadedFile yang dapat hilang setelah navigasi menu.
            st.session_state.uploaded_order_bytes = persist_session_order_upload(uploaded_order)

    if (uploaded_order is None or uploaded_income is None) and 'result' in st.session_state and not st.session_state.result.empty:
        st.info("File sumber tidak tersedia, tetapi hasil rekonsiliasi tersimpan. Menampilkan data transaksi aktif.")
        order_result = st.session_state.result.copy()
        if 'No.' in order_result.columns:
            order_result = order_result.drop(columns=['No.'])
        order_result.insert(0, 'No.', range(1, len(order_result) + 1))
        st.dataframe(order_result, use_container_width=True, hide_index=True)
        st.stop()

    if uploaded_order is None or uploaded_income is None:
        st.info("👈 **Silakan unggah Laporan Order dan Laporan Penghasilan Shopee di sidebar sebelah kiri** untuk memulai rekonsiliasi.")
    else:
        # Menu Order hanya menampilkan filter dan detail transaksi.
        if menu == "order":
            st.subheader("📋 Detail Data Transaksi & Produk")
            order_filter_options = st.session_state.get("filter_options") or {}
            if not order_filter_options:
                order_filter_options = _build_dashboard_filter_options(st.session_state.result)
            filter_cols = st.columns(5)
            with filter_cols[0]:
                order_period = st.multiselect("Periode", order_filter_options.get("Periode", []), key="order_filter_period")
            with filter_cols[1]:
                order_product = st.multiselect("Produk", order_filter_options.get("Produk", []), key="order_filter_product")
            with filter_cols[2]:
                order_sku = st.multiselect("SKU", order_filter_options.get("SKU", []), key="order_filter_sku")
            with filter_cols[3]:
                order_settlement = st.multiselect("Status Settlement", order_filter_options.get("Status Settlement", []), key="order_filter_settlement")
            with filter_cols[4]:
                order_return = st.multiselect("Status Retur", order_filter_options.get("Status Retur", []), key="order_filter_return")
            order_filtered = _apply_dashboard_filters(
                st.session_state.result, order_period, order_product, order_sku,
                order_settlement, order_return
            )
            order_display = order_filtered.copy()
            if "No." in order_display.columns:
                order_display = order_display.drop(columns=["No."])
            order_display.insert(0, "No.", range(1, len(order_display) + 1))
            st.caption(f"Menampilkan {len(order_filtered)} baris dari {len(st.session_state.result)} baris data.")
            st.dataframe(order_display, use_container_width=True, hide_index=True)

            # Rekap produk tetap tersedia di Order untuk kebutuhan audit,
            # tanpa membawa kartu ringkasan finansial Dashboard.
            with st.expander("📦 Rekapitulasi Penjualan & Margin Laba per Produk (Sudah Settlement)", expanded=True):
                st.caption("Grouping berdasarkan Nama Produk dan Harga (@). Total Penjualan (Gross Sales) diambil dari akumulasi subtotal transaksi.")
                recap_table = pd.DataFrame()
                if not order_filtered.empty and "Nama Produk" in order_filtered.columns:
                    recap_hpp_source = uploaded_hpp if uploaded_hpp is not None else None
                    if recap_hpp_source is not None:
                        recap_hpp_source.seek(0)
                    recap_hpp_master = load_hpp_master(file_source=recap_hpp_source)
                    recap_products = order_filtered["Nama Produk"].dropna().unique().tolist()
                    recap_mapping = load_mapping()
                    recap_by_key = {r["ItemKey"]: r.to_dict() for _, r in recap_hpp_master.iterrows()}
                    recap_hpp_lookup = {p: recap_by_key[k] for p, k in recap_mapping.items() if k in recap_by_key}
                    recap_table = generate_product_summary(order_filtered, hpp_lookup=recap_hpp_lookup)
                    recap_table = recap_table.rename(columns={
                        "Total Jumlah Bersih": "Qty Terjual Bersih",
                        "Total Penjualan Bersih": "Total Penjualan (Gross Sales)",
                        "Margin Laba (%)": "Margin (%)",
                    })
                    recap_columns = [
                        "No.", "Nama Produk", "Qty Terjual Bersih", "Satuan", "Harga (@)",
                        "Total Penjualan (Gross Sales)", "HPP (@)", "Total HPP", "Laba Bersih", "Margin (%)"
                    ]
                    recap_table = recap_table.reindex(columns=[c for c in recap_columns if c in recap_table.columns])
                    recap_config = {
                        "Qty Terjual Bersih": st.column_config.NumberColumn("Qty Terjual Bersih", format="%d"),
                        "Harga (@)": st.column_config.NumberColumn("Harga (@)", format="%,d"),
                        "Total Penjualan (Gross Sales)": st.column_config.NumberColumn("Total Penjualan (Gross Sales)", format="%,d"),
                        "HPP (@)": st.column_config.NumberColumn("HPP (@)", format="%,d"),
                        "Total HPP": st.column_config.NumberColumn("Total HPP", format="%,d"),
                        "Laba Bersih": st.column_config.NumberColumn("Laba Bersih", format="%,d"),
                        "Margin (%)": st.column_config.NumberColumn("Margin (%)", format="%.2f%%"),
                    }
                    st.dataframe(recap_table, use_container_width=True, hide_index=True, column_config=recap_config)
                else:
                    st.info("Tidak ada data untuk direkap.")
            export_buffer = io.BytesIO()
            with pd.ExcelWriter(export_buffer, engine="xlsxwriter") as writer:
                order_filtered.to_excel(writer, sheet_name="Detail Transaksi", index=False)
                if not recap_table.empty:
                    recap_table.to_excel(writer, sheet_name="Rekap Produk", index=False)
            export_start = st.session_state.get("processed_start_date")
            export_end = st.session_state.get("processed_end_date")
            if export_start and export_end:
                export_period = f"{export_start:%Y%m%d}-{export_end:%Y%m%d}"
            else:
                export_period = "semua-periode"
            export_timestamp = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y%m%d_%H%M%S")
            st.download_button(
                "📥 Unduh Laporan Excel Lengkap (.xlsx)",
                data=export_buffer.getvalue(),
                file_name=f"hasil_rekonsiliasi_order_{export_period}_{export_timestamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )
            st.stop()

        # ─── Date range picker ───
        if 'date_bounds' not in st.session_state:
            min_date, max_date = get_order_date_bounds(uploaded_order)
            uploaded_order.seek(0)
            st.session_state.date_bounds = (min_date, max_date)
        
        min_date, max_date = st.session_state.date_bounds

        with st.sidebar:
            st.divider()
            if min_date and max_date:
                st.subheader("📅 Rentang Tanggal")
                start_date = st.date_input("Tanggal Mulai", value=min_date, min_value=min_date, max_value=max_date, key="date_start")
                end_date = st.date_input("Tanggal Akhir", value=max_date, min_value=min_date, max_value=max_date, key="date_end")
            else:
                start_date = None
                end_date = None

            btn_proses = st.button("🚀 Proses Rekonsiliasi", key="btn_proses", type="primary", use_container_width=True)

        if btn_proses:
            uploaded_order.seek(0)
            uploaded_income.seek(0)
            with st.spinner('Memproses data transaksi...'):
                st.session_state.result = process_reconciliation(
                    uploaded_order, uploaded_income,
                    start_date=start_date, end_date=end_date
                )
                uploaded_income.seek(0)
                st.session_state.df_adjustments = extract_adjustments(uploaded_income)
                uploaded_order.seek(0)
                st.session_state.filter_options = get_order_filter_options(
                    uploaded_order, start_date=start_date, end_date=end_date
                )
                uploaded_order.seek(0)
                uploaded_income.seek(0)
                st.session_state.settlement_stats = get_settlement_stats(
                    uploaded_order, uploaded_income, start_date=start_date, end_date=end_date
                )
                st.session_state.processed_start_date = start_date
                st.session_state.processed_end_date = end_date
                st.session_state.processed_at = datetime.now(ZoneInfo("Asia/Jakarta"))
                _save_session_result()

            if 'date_bounds' in st.session_state:
                del st.session_state['date_bounds']
            st.success("✅ Rekonsiliasi Selesai!")
        
        if 'result' in st.session_state:
            result = st.session_state.result
            df_adj = st.session_state.get('df_adjustments', pd.DataFrame())
            
            # ─── Helper: Hitung ringkasan finansial dari slice dataframe ───
            def calc_summary(df_slice, df_adj_all, hpp_lookup_map, n_days, history_df=None):
                """Menghitung metrik finansial dari slice data.

                ``history_df`` dipakai khusus sebagai basis estimasi biaya produk
                pending. Dengan begitu drill-down ke satu pesanan tetap memakai
                histori settlement seluruh periode, bukan fallback rasio umum.
                """
                s = {}
                settled = df_slice[df_slice['Is_Settled'] == True].copy() if 'Is_Settled' in df_slice.columns else df_slice.copy()
                unsettled = df_slice[(df_slice['Is_Settled'] == False) & (~df_slice.get('Is_Cancelled', False))].copy() if 'Is_Settled' in df_slice.columns else pd.DataFrame()
                fee_history = history_df if history_df is not None else df_slice
                history_settled = (
                    fee_history[fee_history['Is_Settled'] == True].copy()
                    if 'Is_Settled' in fee_history.columns else fee_history.copy()
                )

                s['total_subtotal'] = int(settled['Subtotal'].sum())
                s['total_biaya'] = int(settled['Total Biaya'].sum())
                s['tot_adm'] = int(settled['Biaya Administrasi'].sum()) if 'Biaya Administrasi' in settled.columns else 0
                s['tot_xtra'] = int(settled['Biaya Gratis Ongkir XTRA'].sum()) if 'Biaya Gratis Ongkir XTRA' in settled.columns else 0
                s['tot_promo'] = int(settled['Biaya Promo XTRA'].sum()) if 'Biaya Promo XTRA' in settled.columns else 0
                s['tot_sub_biaya'] = int(settled['Subtotal Biaya'].sum()) if 'Subtotal Biaya' in settled.columns else (s['tot_adm'] + s['tot_xtra'] + s['tot_promo'])
                s['tot_proses'] = int(settled['Biaya Proses Pesanan'].sum()) if 'Biaya Proses Pesanan' in settled.columns else 0
                s['tot_pajak'] = int(settled['Pajak'].sum()) if 'Pajak' in settled.columns else 0

                sub = s['total_subtotal']
                s['pct_adm'] = abs(s['tot_adm']) / sub * 100 if sub > 0 else 0
                s['pct_xtra'] = abs(s['tot_xtra']) / sub * 100 if sub > 0 else 0
                s['pct_promo'] = abs(s['tot_promo']) / sub * 100 if sub > 0 else 0
                s['pct_sub_biaya'] = abs(s['tot_sub_biaya']) / sub * 100 if sub > 0 else 0

                # Penyesuaian
                if not df_adj_all.empty:
                    active_orders = set(settled['No. Pesanan'].astype(str).unique())
                    rel_adj = df_adj_all[df_adj_all['No. Pesanan'].astype(str).isin(active_orders)]
                    s['total_penyesuaian'] = int(rel_adj['Biaya Penyesuaian'].sum()) if not rel_adj.empty else 0
                    s['adj_orders_list'] = [o for o in rel_adj['No. Pesanan'].unique().tolist() if o and str(o) != 'nan']
                    s['relevant_adj'] = rel_adj
                else:
                    s['total_penyesuaian'] = 0
                    s['adj_orders_list'] = []
                    s['relevant_adj'] = pd.DataFrame()

                s['pct_biaya'] = abs(s['total_biaya']) / sub * 100 if sub > 0 else 0
                s['total_penghasilan'] = s['total_subtotal'] + s['total_biaya'] + s['total_penyesuaian']

                # Estimasi pending
                s['unsettled_result'] = unsettled
                s['settled_result'] = settled
                s['unsettled_subtotal'] = int(unsettled['Subtotal'].sum()) if not unsettled.empty else 0
                def get_item_hpp_inner(row):
                    info = hpp_lookup_map.get(row['Nama Produk'], {})
                    return row['Jumlah Bersih'] * (info.get('HargaPokok', 0) / (info.get('Konversi', 1) or 1))

                # Biaya non-proses mengikuti histori produk yang sudah settled.
                # Estimasi biaya proses memakai rata-rata aktual dari laporan Income;
                # nilai ini hanya proyeksi pending, bukan biaya transaksi aktual.
                history_subtotal = int(history_settled['Subtotal'].sum()) if not history_settled.empty else 0
                history_total_fee = int(history_settled['Total Biaya'].sum()) if 'Total Biaya' in history_settled.columns else 0
                history_process_fee = (
                    int(history_settled['Biaya Proses Pesanan'].sum())
                    if 'Biaya Proses Pesanan' in history_settled.columns else 0
                )
                history_settled_order_count = (
                    history_settled['No. Pesanan'].dropna().nunique()
                    if 'No. Pesanan' in history_settled.columns else 0
                )
                estimated_process_fee_per_order = (
                    abs(history_process_fee) / history_settled_order_count
                    if history_settled_order_count > 0 else 0
                )
                settled_non_process_fee = max(abs(history_total_fee) - abs(history_process_fee), 0)
                global_non_process_fee_ratio = (
                    settled_non_process_fee / history_subtotal if history_subtotal > 0 else 0.15
                )
                if not unsettled.empty and not history_settled.empty:
                    def product_non_process_fee_ratio(rows):
                        product_subtotal = rows['Subtotal'].sum()
                        product_total_fee = abs(rows['Total Biaya'].sum())
                        product_process_fee = abs(rows['Biaya Proses Pesanan'].sum()) if 'Biaya Proses Pesanan' in rows.columns else 0
                        return max(product_total_fee - product_process_fee, 0) / product_subtotal if product_subtotal > 0 else global_non_process_fee_ratio

                    prod_fee_stats = history_settled.groupby('Nama Produk').apply(
                        product_non_process_fee_ratio,
                        include_groups=False
                    ).to_dict()
                else:
                    prod_fee_stats = {}

                s['est_non_process_fee'] = int(round(sum(
                    row['Subtotal'] * prod_fee_stats.get(row['Nama Produk'], global_non_process_fee_ratio)
                    for _, row in unsettled.iterrows()
                ))) if not unsettled.empty else 0
                s['est_process_fee'] = int(round(
                    estimated_process_fee_per_order * len(unsettled['No. Pesanan'].dropna().unique())
                )) if not unsettled.empty else 0
                s['est_unsettled_fee'] = -(s['est_non_process_fee'] + s['est_process_fee'])
                s['est_unsettled_net'] = s['unsettled_subtotal'] + s['est_unsettled_fee']
                s['effective_fee_ratio'] = (
                    abs(s['est_unsettled_fee']) / s['unsettled_subtotal']
                    if s['unsettled_subtotal'] > 0 else global_non_process_fee_ratio
                )

                # HPP & Laba
                hpp_valid_settled = settled[settled['Nama Produk'].astype(str).isin(hpp_lookup_map)].copy() if not settled.empty and 'Nama Produk' in settled.columns else pd.DataFrame()
                if not hpp_valid_settled.empty:
                    s['total_hpp'] = int(round(hpp_valid_settled.apply(get_item_hpp_inner, axis=1).sum()))
                    valid_sub = int(hpp_valid_settled['Subtotal'].sum())
                    valid_income = int(hpp_valid_settled['Subtotal'].sum() + hpp_valid_settled['Total Biaya'].sum())
                    s['laba_bersih'] = valid_income - s['total_hpp']
                    s['margin_laba'] = (s['laba_bersih'] / valid_sub * 100) if valid_sub > 0 else 0.0
                else:
                    s['total_hpp'] = 0
                    s['laba_bersih'] = 0
                    s['margin_laba'] = 0.0

                s['est_hpp_unsettled'] = int(round(unsettled.apply(get_item_hpp_inner, axis=1).sum())) if not unsettled.empty and hpp_lookup_map else 0
                s['total_hpp_proyeksi'] = s['total_hpp'] + s['est_hpp_unsettled']
                s['est_unsettled_profit'] = s['est_unsettled_net'] - s['est_hpp_unsettled']
                s['total_proyeksi'] = s['total_penghasilan'] + s['est_unsettled_net']

                # Harian
                s['avg_per_hari'] = s['total_penghasilan'] / n_days if n_days and n_days > 0 else None

                # Settlement counts
                s['total_orders_valid'] = len(df_slice['No. Pesanan'].dropna().unique())
                s['settled_count'] = len(settled['No. Pesanan'].dropna().unique()) if not settled.empty else 0
                s['unsettled_count'] = len(unsettled['No. Pesanan'].dropna().unique()) if not unsettled.empty else 0
                s['settle_rate'] = (s['settled_count'] / s['total_orders_valid'] * 100) if s['total_orders_valid'] > 0 else 100.0
                s['unsettled_list'] = sorted(unsettled['No. Pesanan'].dropna().unique().tolist()) if not unsettled.empty else []

                return s

            # ─── HPP Lookup (dipakai untuk kedua ringkasan) ───
            hpp_source = uploaded_hpp if uploaded_hpp is not None else None
            if hpp_source is not None:
                hpp_source.seek(0)
            df_hpp_master = load_hpp_master(file_source=hpp_source)
            all_unique_prods = result['Nama Produk'].dropna().unique().tolist()
            mapping_dict = load_mapping()
            hpp_by_key = {r['ItemKey']: r.to_dict() for _, r in df_hpp_master.iterrows()}
            hpp_lookup = {p: hpp_by_key[k] for p, k in mapping_dict.items() if k in hpp_by_key}

            proc_start = st.session_state.get('processed_start_date')
            proc_end = st.session_state.get('processed_end_date')
            num_days = (proc_end - proc_start).days + 1 if proc_start and proc_end else None

            def format_period_label(start_date, end_date):
                """Format rentang tanggal singkat untuk konteks ringkasan global."""
                if not start_date or not end_date:
                    return "Semua tanggal yang diproses"

                month_names = [
                    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
                ]
                if start_date.year == end_date.year and start_date.month == end_date.month:
                    return f"{start_date.day}–{end_date.day} {month_names[start_date.month - 1]} {start_date.year}"
                return (
                    f"{start_date.day} {month_names[start_date.month - 1]} {start_date.year} "
                    f"– {end_date.day} {month_names[end_date.month - 1]} {end_date.year}"
                )

            period_label = format_period_label(proc_start, proc_end)

            # ─── Filter: kontrol di posisi ini agar filtered_result tersedia untuk ringkasan ───
            # (Tapi filter hanya mempengaruhi tabel dan mini-ringkasan, BUKAN ringkasan global)
            filter_options = st.session_state.get('filter_options') or {}
            if not filter_options:
                filter_options = _build_dashboard_filter_options(result)
            allowed_filters = ['No. Pesanan', 'Nama Produk']
            available_filters = [col for col in allowed_filters if col in result.columns]
            # Simpan nilai filter sebelumnya di session_state agar konsisten saat rerun
            if 'tbl_filter_col_val' not in st.session_state:
                st.session_state['tbl_filter_col_val'] = available_filters[0] if available_filters else None
            if 'tbl_filter_selected' not in st.session_state:
                st.session_state['tbl_filter_selected'] = []

            # ─── Kalkulasi ringkasan GLOBAL (seluruh periode, tidak terpengaruh filter) ───
            g = calc_summary(result, df_adj, hpp_lookup, num_days)
            settled_result = g['settled_result']     # Dipakai oleh downstream (adj, product summary)
            unsettled_result = g['unsettled_result']
            relevant_adj = g['relevant_adj']
            adj_orders_list = g['adj_orders_list']
            total_penghasilan = g['total_penghasilan']
            total_subtotal = g['total_subtotal']
            total_biaya = g['total_biaya']
            pct_biaya = g['pct_biaya']
            total_penyesuaian = g['total_penyesuaian']
            unsettled_subtotal = g['unsettled_subtotal']
            est_unsettled_net = g['est_unsettled_net']
            effective_fee_ratio = g['effective_fee_ratio']
            total_proyeksi_keseluruhan = g['total_proyeksi']
            total_hpp_settled = g['total_hpp']
            laba_bersih_settled = g['laba_bersih']
            margin_laba_settled = g['margin_laba']
            est_hpp_unsettled = g['est_hpp_unsettled']
            total_hpp_proyeksi = g['total_hpp_proyeksi']
            avg_per_hari = g['avg_per_hari']
            avg_per_hari_fmt = f"Rp {avg_per_hari:,.0f}" if avg_per_hari is not None else ""
            tot_adm = g['tot_adm']
            tot_xtra = g['tot_xtra']
            tot_promo = g['tot_promo']
            tot_sub_biaya = g['tot_sub_biaya']
            tot_proses = g['tot_proses']
            tot_pajak = g['tot_pajak']
            pct_adm = g['pct_adm']
            pct_xtra = g['pct_xtra']
            pct_promo = g['pct_promo']
            pct_sub_biaya = g['pct_sub_biaya']
            total_orders_valid = g['total_orders_valid']
            settled_count = g['settled_count']
            unsettled_count = g['unsettled_count']
            settle_rate = g['settle_rate']
            unsettled_list = g['unsettled_list']

            # filtered_result = result sementara (dipakai oleh tabel di bawah)
            filtered_result = result.copy()
            if 'No.' in filtered_result.columns:
                filtered_result = filtered_result.drop(columns=['No.'])
            filtered_result.insert(0, 'No.', range(1, len(filtered_result) + 1))


            # ─── Group 2: Realisasi Settled (Dana Sudah Cair) ───
            gross_pct_label = "Subtotal Penjualan (Gross)"
            gross_card = (
                '<div class="summary-card card-gross">'
                f'<div class="label">{gross_pct_label}</div>'
                f'<div class="value">Rp {total_subtotal:,.0f}</div>'
                f'<div class="pct">{settled_count:,} pesanan settled</div>'
                '</div>'
            )
            fees_card = (
                '<div class="summary-card card-fees">'
                '<div class="label">Total Biaya Shopee</div>'
                f'<div class="value">Rp {total_biaya:,.0f}</div>'
                f'<div class="pct">Admin Rp {abs(tot_adm):,.0f} · Ongkir Rp {abs(tot_xtra):,.0f}<br>Promo Rp {abs(tot_promo):,.0f} · Proses Rp {abs(tot_proses):,.0f} · Pajak Rp {abs(tot_pajak):,.0f}</div>'
                '</div>'
            )
            adj_card = ""
            if total_penyesuaian != 0:
                adj_color = "#4ade80" if total_penyesuaian > 0 else "#f87171"
                adj_sign = "+" if total_penyesuaian > 0 else ""
                adj_card = (
                    '<div class="summary-card card-adj">'
                    '<div class="label">Total Penyesuaian</div>'
                    f'<div class="value" style="color: {adj_color};">{adj_sign}Rp {total_penyesuaian:,.0f}</div>'
                    f'<div class="pct">{len(adj_orders_list)} pesanan disesuaikan</div>'
                    '</div>'
                )
            net_card = (
                '<div class="summary-card card-net">'
                '<div class="label">Penghasilan</div>'
                f'<div class="value">Rp {total_penghasilan:,.0f}</div>'
                '<div class="pct">Basis laba: penghasilan real setelah fee & penyesuaian</div>'
                '</div>'
            )
            hpp_card = ""
            laba_card = ""
            if total_hpp_settled > 0:
                hpp_card = (
                    '<div class="summary-card card-hpp">'
                    '<div class="label">Total Modal (HPP)</div>'
                    f'<div class="value">Rp {total_hpp_settled:,.0f}</div>'
                    f'<div class="pct">HPP real produk terjual</div>'
                    '</div>'
                )
                laba_color = "#10b981" if laba_bersih_settled >= 0 else "#f87171"
                laba_card = (
                    '<div class="summary-card card-laba">'
                    '<div class="label">Laba Bersih</div>'
                    f'<div class="value" style="color: {laba_color};">Rp {laba_bersih_settled:,.0f}</div>'
                    f'<div class="pct">Penghasilan - HPP = Laba | Margin: {margin_laba_settled:.1f}%</div>'
                    '</div>'
                )
            daily_card = ""
            if avg_per_hari is not None:
                daily_card = (
                    '<div class="summary-card card-daily">'
                    '<div class="label">Penghasilan Real / Hari</div>'
                    f'<div class="value">{avg_per_hari_fmt}</div>'
                    f'<div class="pct">Real Settled ({num_days} hari)</div>'
                    '</div>'
                )

            # ─── Group 3: Estimasi Pending & Total Proyeksi ───
            potential_card = ""
            grand_total_card = ""
            laba_proyeksi_card = ""
            daily_proj_card = ""

            if not unsettled_result.empty:
                potential_card = (
                    '<div class="summary-card card-potential">'
                    '<div class="label">Estimasi Potensi Pending</div>'
                    f'<div class="value">Rp {est_unsettled_net:,.0f}</div>'
                    f'<div class="pct">{unsettled_count:,} pesanan unsettled · Subtotal Rp {unsettled_subtotal:,.0f}<br>Est. biaya {effective_fee_ratio*100:.1f}%</div>'
                    '</div>'
                )
                grand_total_card = (
                    '<div class="summary-card card-grand">'
                    '<div class="label">Total Proyeksi Bersih</div>'
                    f'<div class="value">Rp {total_proyeksi_keseluruhan:,.0f}</div>'
                    '<div class="pct">Settled + Estimasi Pending</div>'
                    '</div>'
                )
                if total_hpp_proyeksi > 0:
                    laba_proyeksi = total_proyeksi_keseluruhan - total_hpp_proyeksi
                    margin_proyeksi = (laba_proyeksi / total_proyeksi_keseluruhan * 100) if total_proyeksi_keseluruhan > 0 else 0.0
                    laba_proj_color = "#10b981" if laba_proyeksi >= 0 else "#f87171"
                    laba_proyeksi_card = (
                        '<div class="summary-card card-laba">'
                        '<div class="label">Proyeksi Laba Bersih</div>'
                        f'<div class="value" style="color: {laba_proj_color};">Rp {laba_proyeksi:,.0f}</div>'
                        f'<div class="pct">Margin Proyeksi: {margin_proyeksi:.1f}% | Est. HPP Pending: Rp {est_hpp_unsettled:,.0f}</div>'
                        '</div>'
                    )
                if avg_per_hari is not None:
                    avg_proj_per_hari = total_proyeksi_keseluruhan / num_days if num_days and num_days > 0 else total_proyeksi_keseluruhan
                    daily_proj_card = (
                        '<div class="summary-card card-grand">'
                        '<div class="label">Proyeksi Bersih / Hari</div>'
                        f'<div class="value">Rp {avg_proj_per_hari:,.0f}</div>'
                        f'<div class="pct">Proyeksi Total ({num_days} hari)</div>'
                        '</div>'
                    )

            # ─── Render HTML Terstruktur Berdasarkan Settlement ───
            st.markdown("### 💰 Ringkasan Finansial Rekonsiliasi")
            st.caption(f"{period_label} · Semua Produk · Ringkasan periode ini tidak berubah saat tabel difilter.")

            # Bagian 1: Realisasi Pesanan Selesai (Settled)
            settled_cards_html = gross_card + fees_card + net_card + adj_card + hpp_card + laba_card + daily_card
            settled_html = (
                '<div class="section-group">'
                '<div class="section-header">'
                '<div class="section-title"><span>✅</span> Realisasi Penjualan & Laba Selesai (Settled)</div>'
                f'<div class="section-badge badge-settled">{settled_count}/{total_orders_valid} Pesanan Cair ({settle_rate:.1f}%)</div>'
                '</div>'
                f'<div class="summary-container">{settled_cards_html}</div>'
                '</div>'
            )
            st.markdown(settled_html, unsafe_allow_html=True)

            # Bagian 2: Estimasi Pending & Proyeksi Keseluruhan (Hanya jika ada unsettled)
            if not unsettled_result.empty:
                pending_cards_html = potential_card + grand_total_card + laba_proyeksi_card + daily_proj_card
                pending_html = (
                    '<div class="section-group">'
                    '<div class="section-header">'
                    '<div class="section-title"><span>⏳</span> Estimasi Pending & Total Proyeksi Toko</div>'
                    f'<div class="section-badge badge-pending">{unsettled_count} Pesanan Belum Settlement (Dana Tertahan)</div>'
                    '</div>'
                    f'<div class="summary-container">{pending_cards_html}</div>'
                    '</div>'
                )
                st.markdown(pending_html, unsafe_allow_html=True)

            # ─── Rincian Komponen Biaya ───
            with st.expander("📊 Rincian Detail Komponen Biaya", expanded=True):
                st.markdown(f"""
                <div class="breakdown-container">
                    <div class="breakdown-card">
                        <div class="title">Biaya Administrasi</div>
                        <div class="val">Rp {tot_adm:,.0f}</div>
                        <div class="sub">{pct_adm:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Gratis Ongkir XTRA</div>
                        <div class="val">Rp {tot_xtra:,.0f}</div>
                        <div class="sub">{pct_xtra:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Biaya Promo XTRA</div>
                        <div class="val">Rp {tot_promo:,.0f}</div>
                        <div class="sub">{pct_promo:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Subtotal Biaya</div>
                        <div class="val">Rp {tot_sub_biaya:,.0f}</div>
                        <div class="sub">{pct_sub_biaya:.2f}% dari Subtotal</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Proses Pesanan</div>
                        <div class="val">Rp {tot_proses:,.0f}</div>
                    </div>
                    <div class="breakdown-card">
                        <div class="title">Pajak (PPh 22)</div>
                        <div class="val">Rp {tot_pajak:,.0f}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Highlight Pesanan Belum Settlement ───
            if unsettled_list:
                st.markdown("##### ⏳ Pesanan Belum Settlement (Dana Belum Dilepas Shopee)")
                unsettled_badges_html = " ".join([f'<span class="unsettled-badge">⏳ {order}</span>' for order in unsettled_list])
                st.markdown(f"""
                <div style="background: rgba(239, 68, 68, 0.08); border-left: 4px solid #ef4444; padding: 0.8rem 1.2rem; border-radius: 8px; margin-bottom: 1.2rem;">
                    <div style="font-size: 0.88rem; color: #fca5a5; font-weight: 600; margin-bottom: 0.4rem;">
                        Ditemukan {unsettled_count} pesanan ({100 - settle_rate:.1f}%) yang belum settlement:
                    </div>
                    <div style="font-size: 0.82rem; color: #f87171; margin-bottom: 0.6rem;">
                        • Subtotal Gross Pending: <b>Rp {unsettled_subtotal:,.0f}</b><br>
                        • Estimasi Bersih Cair (setelah est. fee {effective_fee_ratio*100:.1f}%): <b>Rp {est_unsettled_net:,.0f}</b>
                    </div>
                    <div>{unsettled_badges_html}</div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Highlight Pesanan dengan Penyesuaian ───
            if adj_orders_list:
                st.markdown("##### ⚠️ Highlight Pesanan dengan Biaya Penyesuaian (Adjustment)")
                badges_html = " ".join([f'<span class="adj-badge">📦 {order}</span>' for order in adj_orders_list])
                st.markdown(f"""
                <div style="background: rgba(234, 179, 8, 0.08); border-left: 4px solid #eab308; padding: 0.8rem 1.2rem; border-radius: 8px; margin-bottom: 1.2rem;">
                    <div style="font-size: 0.88rem; color: #fde047; font-weight: 600; margin-bottom: 0.4rem;">
                        Ditemukan {len(adj_orders_list)} pesanan yang memiliki potongan/penyesuaian saldo setelah dana dilepas:
                    </div>
                    <div>{badges_html}</div>
                </div>
                """, unsafe_allow_html=True)

            # ─── Filter & Mini-Ringkasan Filter (Di Atas Tabel) ───
            st.subheader("📋 Detail Data Transaksi & Produk")

            f_col1, f_col2 = st.columns([1, 2])
            with f_col1:
                filter_col = st.selectbox("🔍 Filter berdasarkan:", available_filters, key="tbl_filter_col") if available_filters else None
            with f_col2:
                if filter_col:
                    unique_values = filter_options.get(filter_col, sorted(result[filter_col].dropna().astype(str).unique().tolist()))
                    selected_values = st.multiselect(f"Pilih nilai:", unique_values, default=[], key="tbl_filter_val", placeholder=f"Semua {filter_col}...")
                else:
                    selected_values = []

            if selected_values and filter_col:
                filtered_result = result[result[filter_col].astype(str).isin([str(v) for v in selected_values])].copy()
                if 'No.' in filtered_result.columns:
                    filtered_result = filtered_result.drop(columns=['No.'])
                filtered_result.insert(0, 'No.', range(1, len(filtered_result) + 1))

                # ─── Mini-Ringkasan Filter: hanya muncul saat filter aktif ───
                f_label = ', '.join([str(v) for v in selected_values[:3]])
                if len(selected_values) > 3:
                    f_label += f" +{len(selected_values)-3} lainnya"
                filt_g = calc_summary(
                    filtered_result,
                    df_adj,
                    hpp_lookup,
                    num_days,
                    history_df=result,
                )

                filt_settled = filt_g['settled_result']
                filt_total_rows = len(filtered_result)
                filt_sub = filt_g['total_subtotal']
                filt_biaya = filt_g['total_biaya']
                filt_net = filt_g['total_penghasilan']
                filt_hpp = filt_g['total_hpp']
                filt_laba = filt_g['laba_bersih']
                filt_margin = filt_g['margin_laba']
                filt_laba_color = "#10b981" if filt_laba >= 0 else "#f87171"
                filt_product_count = filtered_result['Nama Produk'].dropna().nunique() if 'Nama Produk' in filtered_result.columns else 0
                filt_unsettled = filt_g['unsettled_result']
                filt_projection_html = ""

                filt_cards = (
                    '<div class="summary-card card-gross">'
                    f'<div class="label">Subtotal (Gross)</div>'
                    f'<div class="value">Rp {filt_sub:,.0f}</div>'
                    '<div class="pct">Penjualan Produk Filtered</div>'
                    '</div>'
                    '<div class="summary-card card-fees">'
                    f'<div class="label">Total Biaya</div>'
                    f'<div class="value">Rp {filt_biaya:,.0f}</div>'
                    f'<div class="pct">{filt_g["pct_biaya"]:.1f}% dari Subtotal</div>'
                    '</div>'
                    '<div class="summary-card card-net">'
                    '<div class="label">Penghasilan</div>'
                    f'<div class="value">Rp {filt_net:,.0f}</div>'
                    '<div class="pct">Basis laba filter ini</div>'
                    '</div>'
                )
                if filt_hpp > 0:
                    filt_cards += (
                        '<div class="summary-card card-hpp">'
                        '<div class="label">Total HPP</div>'
                        f'<div class="value">Rp {filt_hpp:,.0f}</div>'
                        '</div>'
                        '<div class="summary-card card-laba">'
                        '<div class="label">Laba Bersih</div>'
                        f'<div class="value" style="color:{filt_laba_color};">Rp {filt_laba:,.0f}</div>'
                        f'<div class="pct">Penghasilan - HPP = Laba | Margin: {filt_margin:.1f}%</div>'
                        '</div>'
                        '<div class="summary-card card-laba">'
                        '<div class="label">Margin Laba</div>'
                        f'<div class="value" style="color:{filt_laba_color};">{filt_margin:.1f}%</div>'
                        '<div class="pct">Laba bersih ÷ penghasilan</div>'
                        '</div>'
                    )

                # Proyeksi filter memakai tampilan yang sama dengan proyeksi toko,
                # tetapi dihitung hanya dari transaksi yang lolos filter aktif.
                if not filt_unsettled.empty:
                    filt_est_pending = filt_g['est_unsettled_net']
                    filt_total_projection = filt_g['total_proyeksi']
                    filt_fee_ratio = filt_g['effective_fee_ratio']
                    filt_unsettled_subtotal = filt_g['unsettled_subtotal']
                    filt_estimated_fee = filt_g['est_unsettled_fee']
                    filt_estimated_process_fee = filt_g['est_process_fee']
                    filt_estimated_hpp = filt_g['est_hpp_unsettled']
                    filt_pending_profit = filt_g['est_unsettled_profit']
                    filt_pending_profit_color = "#10b981" if filt_pending_profit >= 0 else "#f87171"
                    filt_projection_cards = (
                        '<div class="summary-card card-gross">'
                        '<div class="label">Subtotal Gross Pending</div>'
                        f'<div class="value">Rp {filt_unsettled_subtotal:,.0f}</div>'
                        '<div class="pct">Nilai transaksi sebelum potongan</div>'
                        '</div>'
                        '<div class="summary-card card-fees">'
                        '<div class="label">Estimasi Total Biaya</div>'
                        f'<div class="value">Rp {filt_estimated_fee:,.0f}</div>'
                        f'<div class="pct">Histori produk + estimasi proses aktual Rp {filt_estimated_process_fee:,.0f}</div>'
                        '</div>'
                        '<div class="summary-card card-potential">'
                        '<div class="label">Estimasi Net Pending</div>'
                        f'<div class="value">Rp {filt_est_pending:,.0f}</div>'
                        f'<div class="pct">Setelah estimasi biaya {filt_fee_ratio * 100:.1f}%</div>'
                        '</div>'
                        '<div class="summary-card card-hpp">'
                        '<div class="label">Estimasi HPP Pending</div>'
                        f'<div class="value">Rp {filt_estimated_hpp:,.0f}</div>'
                        '<div class="pct">HPP produk terkait</div>'
                        '</div>'
                        '<div class="summary-card card-laba">'
                        '<div class="label">Proyeksi Laba Bersih Pending</div>'
                        f'<div class="value" style="color:{filt_pending_profit_color};">Rp {filt_pending_profit:,.0f}</div>'
                        '<div class="pct">Estimasi penghasilan pending - estimasi HPP pending</div>'
                        '</div>'
                        '<div class="summary-card card-grand">'
                        '<div class="label">Total Proyeksi Bersih</div>'
                        f'<div class="value">Rp {filt_total_projection:,.0f}</div>'
                        '<div class="pct">Realisasi settled + estimasi pending</div>'
                        '</div>'
                    )

                    filt_hpp_projection = filt_g['total_hpp_proyeksi']
                    if filt_hpp_projection > 0:
                        filt_projected_profit = filt_total_projection - filt_hpp_projection
                        filt_projected_margin = (
                            filt_projected_profit / filt_total_projection * 100
                            if filt_total_projection > 0 else 0.0
                        )
                        filt_projected_profit_color = "#10b981" if filt_projected_profit >= 0 else "#f87171"
                        filt_projection_cards += (
                        '<div class="summary-card card-laba">'
                        '<div class="label">Proyeksi Laba Bersih</div>'
                        f'<div class="value" style="color:{filt_projected_profit_color};">Rp {filt_projected_profit:,.0f}</div>'
                        f'<div class="pct">Penghasilan proyeksi - HPP proyeksi = Laba | Margin proyeksi: {filt_projected_margin:.1f}%</div>'
                        '</div>'
                    )

                    if num_days and num_days > 0:
                        filt_projection_cards += (
                            '<div class="summary-card card-grand">'
                            '<div class="label">Proyeksi Bersih / Hari</div>'
                            f'<div class="value">Rp {filt_total_projection / num_days:,.0f}</div>'
                            f'<div class="pct">Proyeksi total ({num_days} hari)</div>'
                            '</div>'
                        )

                    filt_pending_count = filt_g['unsettled_count']
                    filt_projection_html = (
                        '<div class="section-group">'
                        '<div class="section-header">'
                        '<div class="section-title"><span>⏳</span> Estimasi Pending & Total Proyeksi Detail</div>'
                        f'<div class="section-badge badge-pending">{filt_pending_count} Pesanan Belum Settlement (sesuai filter)</div>'
                        '</div>'
                        f'<div class="summary-container">{filt_projection_cards}</div>'
                        '</div>'
                    )

                filt_n_ord = filt_g['total_orders_valid']
                filt_n_settled = filt_g['settled_count']
                st.markdown(
                    f'<div class="section-group" style="border-color:rgba(99,102,241,0.35);background:rgba(30,27,75,0.55);">'
                    '<div class="section-header">'
                    f'<div class="section-title"><span>🔍</span> Ringkasan Filter: <em style="color:#a5b4fc;">{f_label}</em></div>'
                    f'<div class="section-badge" style="background:rgba(99,102,241,0.18);color:#c7d2fe;border:1px solid rgba(99,102,241,0.35);">'
                    f'{filt_product_count} produk · {filt_total_rows} transaksi · {filt_n_settled}/{filt_n_ord} pesanan settled</div>'
                    '</div>'
                    f'<div class="summary-container">{filt_cards}</div>'
                    '</div>',
                    unsafe_allow_html=True
                )
                if filt_projection_html:
                    st.markdown(filt_projection_html, unsafe_allow_html=True)
            else:
                selected_values = []


            legends = []
            if 'Returned quantity' in filtered_result.columns and (filtered_result['Returned quantity'] > 0).any():
                legends.append("🟡 **Kuning**: Retur / Penyesuaian (Returned quantity > 0)")
            if 'Is_Settled' in filtered_result.columns and (~filtered_result['Is_Settled']).any():
                legends.append("🔴 **Merah**: Belum Settlement (Dana belum dilepas di Laporan Penghasilan)")
            if legends:
                st.caption(" | ".join(legends))

            display_df = filtered_result.copy()

            # Tampilkan harga pokok per unit di dekat harga jual agar margin produk
            # mudah dibaca langsung pada tabel detail.
            if 'Nama Produk' in display_df.columns and 'Harga (@)' in display_df.columns:
                def get_hpp_per_unit(product_name):
                    hpp_info = hpp_lookup.get(product_name, {})
                    if not hpp_info:
                        return pd.NA
                    return int(round(
                        hpp_info.get('HargaPokok', 0) /
                        (hpp_info.get('Konversi', 1) or 1)
                    ))

                if 'HPP (@)' in display_df.columns:
                    display_df = display_df.drop(columns=['HPP (@)'])
                hpp_position = display_df.columns.get_loc('Harga (@)') + 1
                hpp_values = pd.array(
                    display_df['Nama Produk'].map(get_hpp_per_unit),
                    dtype='Int64'
                )
                display_df.insert(hpp_position, 'HPP (@)', hpp_values)

            def highlight_rows(row):
                is_settled = row.get('Is_Settled', True)
                ret_qty = row.get('Returned quantity', 0)
                
                if not is_settled:
                    return ['background-color: rgba(239, 68, 68, 0.18); color: #fca5a5; font-weight: 600;'] * len(row)
                if pd.notna(ret_qty) and ret_qty > 0:
                    return ['background-color: rgba(234, 179, 8, 0.22); color: #fef08a; font-weight: 600;'] * len(row)
                return [''] * len(row)

            styled_df = display_df.style.apply(highlight_rows, axis=1)

            cols_config = {
                'Is_Settled': None,
                COL_PCT_ADM: st.column_config.NumberColumn("(%)", format="%.2f%%"),
                COL_PCT_XTRA: st.column_config.NumberColumn("(%) ", format="%.2f%%"),
                COL_PCT_PROMO: st.column_config.NumberColumn("(%)  ", format="%.2f%%"),
                COL_PCT_SUB_BIAYA: st.column_config.NumberColumn("(%)   ", format="%.2f%%"),
                'Jumlah': st.column_config.NumberColumn("Jumlah (Gross)", format="%d", help="Jumlah unit yang dipesan pembeli awal"),
                'Returned quantity': st.column_config.NumberColumn("Retur (Qty)", format="%d", help="Jumlah unit yang diretur pembeli"),
                'Jumlah Bersih': st.column_config.NumberColumn("Jumlah Bersih (Unit)", format="%d", help="Kuantitas fisik real terjual (Jumlah - Retur). Dipakai untuk dasar modal HPP."),
                'Subtotal': st.column_config.NumberColumn("Subtotal (Gross Sales)", format="%,d", help="Nilai transaksi kotor awal (Jumlah × Harga). Potongan pengembalian dana retur dicatat pada tabel Penyesuaian (Adjustment)."),
                'HPP (@)': st.column_config.NumberColumn("HPP (@)", format="%,d", help="Harga pokok per unit sesuai mapping master HPP dan konversi satuan produk."),
            }
            
            thousand_cols = [
                'Harga (@)', 'Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal', 'Biaya Administrasi', 
                'Biaya Gratis Ongkir XTRA', 'Biaya Promo XTRA', 'Subtotal Biaya', 
                'Biaya Proses Pesanan', 'Total Biaya', 'Pajak'
            ]
            for col in thousand_cols:
                if col in display_df.columns and col not in ['Jumlah', 'Returned quantity', 'Jumlah Bersih', 'Subtotal']:
                    cols_config[col] = st.column_config.NumberColumn(col, format="%,d")

            st.dataframe(
                styled_df, 
                use_container_width=True, 
                hide_index=True,
                column_config=cols_config
            )
            
            # ─── Tabel Detail Penyesuaian (Adjustment) ───
            if not relevant_adj.empty:
                st.subheader("⚖️ Detail Penyesuaian (Adjustment)")
                st.caption("Penyesuaian saldo / pengembalian dana setelah dana dilepaskan berdasarkan No. Pesanan")
                adj_cols_config = {
                    'Biaya Penyesuaian': st.column_config.NumberColumn("Biaya Penyesuaian", format="%,d")
                }
                st.dataframe(
                    relevant_adj,
                    use_container_width=True,
                    hide_index=True,
                    column_config=adj_cols_config
                )

            # ─── Tabel Rekapitulasi Produk ───
            df_product_summary = generate_product_summary(filtered_result, hpp_lookup=hpp_lookup)
            if not df_product_summary.empty:
                with st.expander("📦 Rekapitulasi Penjualan & Margin Laba per Produk (Sudah Settlement)", expanded=True):
                    st.caption("Grouping berdasarkan Nama Produk dan Harga (@). **Total Penjualan (Gross Sales)** diambil dari akumulasi Subtotal riil transaksi, sedangkan **Total HPP** dihitung dari Kuantitas Bersih fisik.")
                    prod_cols_config = {
                        'Total Jumlah Bersih': st.column_config.NumberColumn("Qty Terjual Bersih", format="%d", help="Total kuantitas barang fisik yang tidak diretur (basis kalkulasi Total HPP)"),
                        'Harga (@)': st.column_config.NumberColumn("Harga (@)", format="%,d", help="Harga jual satuan produk"),
                        'Total Penjualan Bersih': st.column_config.NumberColumn("Total Penjualan (Gross Sales)", format="%,d", help="Total nilai penjualan kotor riil transaksi (akumulasi Subtotal transaksi)"),
                        'HPP (@)': st.column_config.NumberColumn("HPP (@)", format="%,d", help="Harga pokok per unit terjual"),
                        'Total HPP': st.column_config.NumberColumn("Total HPP", format="%,d", help="Total modal barang = Qty Terjual Bersih × HPP (@)"),
                        'Laba Bersih': st.column_config.NumberColumn("Laba Bersih", format="%,d", help="Total Penjualan + Total Biaya Shopee - Total HPP"),
                        'Margin Laba (%)': st.column_config.NumberColumn("Margin (%)", format="%.2f%%")
                    }
                    st.dataframe(
                        df_product_summary,
                        use_container_width=True,
                        hide_index=True,
                        column_config=prod_cols_config
                    )

            # ─── Export Excel ───
            final_result_excel = add_total_row(filtered_result)

            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
                final_result_excel.to_excel(writer, sheet_name='Hasil Rekonsiliasi', index=False)
                if not df_product_summary.empty:
                    df_product_summary.to_excel(writer, sheet_name='Rekap Produk & HPP', index=False)
                if not relevant_adj.empty:
                    relevant_adj.to_excel(writer, sheet_name='Penyesuaian (Adjustment)', index=False)
            
            st.divider()
            st.download_button(
                label="📥 Unduh Laporan Excel Lengkap (.xlsx)",
                data=buffer,
                file_name="hasil_rekonsiliasi.xlsx",
                mime="application/vnd.ms-excel",
                type="primary"
            )


# ==============================================================================
# 📦 MENU 3: KELOLA MASTER HPP
# ==============================================================================
elif menu == "customers":
    if 'result' not in st.session_state:
        _load_session_result()
    st.markdown(
        '<div class="dashboard-hero"><div class="dashboard-kicker">Customer Intelligence</div>'
        '<div class="dashboard-title">👥 Customers</div>'
        '<div class="dashboard-description">Ringkasan pelanggan dari file Order aktif, dengan metrik keuangan mengikuti scope Rekonsiliasi.</div></div>',
        unsafe_allow_html=True,
    )
    customer_file = _find_session_order_file()
    if not customer_file:
        st.info("Belum ada file Order aktif. Unggah dan proses file dari Dashboard terlebih dahulu.")
    else:
        try:
            customer_orders = pd.read_excel(customer_file, sheet_name="orders")
            customer_col = "Username (Pembeli)"
            if customer_col not in customer_orders.columns:
                st.warning("Kolom Username (Pembeli) tidak ditemukan pada file Order.")
            else:
                customer_orders[customer_col] = customer_orders[customer_col].fillna("(Tanpa Username)").astype(str).str.strip()
                customer_orders["Waktu Pesanan Dibuat"] = pd.to_datetime(customer_orders["Waktu Pesanan Dibuat"], errors="coerce")
                if "Subtotal Pesanan" in customer_orders.columns:
                    customer_orders["Subtotal Pesanan"] = _parse_shopee_rupiah_series(customer_orders["Subtotal Pesanan"])
                else:
                    customer_orders["Subtotal Pesanan"] = 0
                if "Subtotal Pesanan Setelah Retur" in customer_orders.columns:
                    customer_orders["Subtotal Pesanan Setelah Retur"] = _parse_shopee_rupiah_series(customer_orders["Subtotal Pesanan Setelah Retur"])
                    customer_orders["_customer_sales"] = customer_orders["Subtotal Pesanan Setelah Retur"]
                else:
                    customer_orders["_customer_sales"] = customer_orders["Subtotal Pesanan"]
                customer_orders["Jumlah"] = pd.to_numeric(customer_orders.get("Jumlah", 0), errors="coerce").fillna(0)
                customer_orders["Returned quantity"] = pd.to_numeric(customer_orders.get("Returned quantity", 0), errors="coerce").fillna(0)
                customer_orders["Jumlah Bersih"] = customer_orders["Jumlah"] - customer_orders["Returned quantity"]
                customer_orders["_completed_spending"] = customer_orders["_customer_sales"].where(customer_orders["Status Pesanan"].astype(str).str.casefold().eq("selesai"), 0)
                customer_orders["_cancelled_spending"] = customer_orders["_customer_sales"].where(customer_orders["Status Pesanan"].astype(str).str.casefold().eq("batal"), 0)
                customer_orders = customer_orders.dropna(subset=["No. Pesanan"])
                customer_orders = customer_orders.drop_duplicates([customer_col, "No. Pesanan", "Status Pesanan"])
                customer_summary = customer_orders.groupby(customer_col, as_index=False).agg(
                    **{
                        "Total Orders": ("No. Pesanan", "nunique"),
                        "Completed Orders": ("No. Pesanan", "nunique"),
                        "Cancelled Orders": ("Status Pesanan", lambda s: int(s.astype(str).str.casefold().eq("batal").sum())),
                        "Completed Value": ("_completed_spending", "sum"),
                        "Cancelled Order Value": ("_cancelled_spending", "sum"),
                        "Pending Sales": ("_customer_sales", lambda s: 0),
                        "First Order": ("Waktu Pesanan Dibuat", "min"),
                        "Last Order": ("Waktu Pesanan Dibuat", "max"),
                    }
                ).rename(columns={customer_col: "Username"})
                completed_order_mask = customer_orders["Status Pesanan"].astype(str).str.casefold().eq("selesai")
                completed_order_counts = customer_orders.loc[completed_order_mask].groupby(customer_col)["No. Pesanan"].nunique()
                customer_summary["Completed Orders"] = customer_summary["Username"].map(completed_order_counts).fillna(0).astype(int)
                pending_mask = ~customer_orders["Status Pesanan"].astype(str).str.casefold().isin({"selesai", "batal"})
                pending_spending = customer_orders.loc[pending_mask].groupby(customer_col)["_customer_sales"].sum()
                customer_summary["Pending Sales"] = customer_summary["Username"].map(pending_spending).fillna(0)
                customer_summary["Pending Orders"] = customer_summary["Username"].map(
                    customer_orders.loc[pending_mask].groupby(customer_col)["No. Pesanan"].nunique()
                ).fillna(0).astype(int)
                customer_summary["Average Order Value"] = (
                    customer_summary["Completed Value"] + customer_summary["Pending Sales"]
                ) / customer_summary["Total Orders"].replace(0, 1)
                customer_column_order = [
                    "Username", "Total Orders", "Completed Orders", "Pending Orders", "Cancelled Orders",
                    "Completed Value", "Pending Sales", "Cancelled Order Value", "Average Order Value",
                    "Repeat Status", "First Order", "Last Order",
                ]
                customer_summary = customer_summary[
                    [column for column in customer_column_order if column in customer_summary.columns]
                ]
                def repeat_status(total_orders):
                    return "Repeat" if total_orders > 1 else "One-time"
                customer_summary["Repeat Status"] = customer_summary["Total Orders"].map(repeat_status)

                overview_cols = st.columns(4)
                overview_cols[0].metric("Customers", len(customer_summary))
                overview_cols[1].metric("Total Orders", int(customer_summary["Total Orders"].sum()))
                overview_cols[2].metric("Repeat Customers", int((customer_summary["Total Orders"] > 1).sum()))
                overview_cols[3].metric("Repeat Rate", f"{(customer_summary['Total Orders'].gt(1).mean() * 100 if len(customer_summary) else 0):.1f}%")

                st.markdown('<div class="section-title">📋 Customer List</div>', unsafe_allow_html=True)
                display_customer = customer_summary.copy()
                list_result = st.session_state.get("result", pd.DataFrame()).copy()
                if not list_result.empty and "No. Pesanan" in list_result.columns:
                    order_customer = customer_orders[["No. Pesanan", customer_col]].copy()
                    order_customer["No. Pesanan"] = order_customer["No. Pesanan"].astype(str)
                    list_result["No. Pesanan"] = list_result["No. Pesanan"].astype(str)
                    list_result = list_result.merge(order_customer.drop_duplicates("No. Pesanan"), on="No. Pesanan", how="left")
                    list_hpp_lookup = _build_hpp_lookup_for_dashboard(list_result)
                    def _as_bool(value):
                        if isinstance(value, str):
                            return value.strip().casefold() in {"true", "1", "yes", "ya"}
                        return bool(value) if pd.notna(value) else False
                    settled = (
                        list_result[list_result["Is_Settled"].map(_as_bool) == True].copy()
                        if "Is_Settled" in list_result.columns else pd.DataFrame()
                    )
                    hist_subtotal = pd.to_numeric(settled.get("Subtotal", 0), errors="coerce").fillna(0).sum()
                    hist_fee = abs(pd.to_numeric(settled.get("Total Biaya", 0), errors="coerce").fillna(0).sum())
                    hist_process = abs(pd.to_numeric(settled.get("Biaya Proses Pesanan", 0), errors="coerce").fillna(0).sum())
                    hist_orders = settled["No. Pesanan"].nunique()
                    process_per_order = hist_process / hist_orders if hist_orders else 0
                    fee_ratio = max(hist_fee - hist_process, 0) / hist_subtotal if hist_subtotal else 0.15
                    product_ratios = {}
                    for product, rows in settled.groupby("Nama Produk") if not settled.empty and "Nama Produk" in settled.columns else []:
                        subtotal = pd.to_numeric(rows.get("Subtotal", 0), errors="coerce").fillna(0).sum()
                        fees = abs(pd.to_numeric(rows.get("Total Biaya", 0), errors="coerce").fillna(0).sum())
                        proc = abs(pd.to_numeric(rows.get("Biaya Proses Pesanan", 0), errors="coerce").fillna(0).sum())
                        product_ratios[product] = max(fees - proc, 0) / subtotal if subtotal else fee_ratio

                    def list_net_profit(row):
                        # Pending tanpa histori settled tidak punya basis estimasi biaya;
                        # jangan tampilkan sebagai laba bersih yang seolah-olah aktual.
                        if not _as_bool(row.get("Is_Settled", True)) and settled.empty:
                            return pd.NA
                        subtotal = pd.to_numeric(row.get("Subtotal", 0), errors="coerce")
                        subtotal = subtotal if pd.notna(subtotal) else 0
                        fee = pd.to_numeric(row.get("Total Biaya", 0), errors="coerce")
                        if not _as_bool(row.get("Is_Settled", True)) and not settled.empty:
                            fee = -round(subtotal * product_ratios.get(row.get("Nama Produk"), fee_ratio) + process_per_order)
                        hpp_info = list_hpp_lookup.get(row.get("Nama Produk"), {})
                        hpp = pd.to_numeric(row.get("Jumlah Bersih", 0), errors="coerce") * hpp_info.get("HargaPokok", 0) / (hpp_info.get("Konversi", 1) or 1)
                        return subtotal + (fee if pd.notna(fee) else 0) - hpp if hpp_info else pd.NA

                    list_result["Total Laba Bersih"] = list_result.apply(list_net_profit, axis=1)
                    profit_by_customer = list_result.groupby(customer_col)["Total Laba Bersih"].sum(min_count=1).rename("Total Laba Bersih")
                    display_customer = display_customer.merge(profit_by_customer, left_on="Username", right_index=True, how="left")
                else:
                    display_customer["Total Laba Bersih"] = pd.NA
                for col in ["First Order", "Last Order"]:
                    display_customer[col] = display_customer[col].dt.strftime("%Y-%m-%d").fillna("-")
                st.dataframe(
                    display_customer.sort_values("Total Orders", ascending=False),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Completed Value": st.column_config.NumberColumn("Completed Value", format="Rp %,d", help="Nilai order selesai setelah retur."),
                        "Cancelled Order Value": st.column_config.NumberColumn("Cancelled Order Value", format="Rp %,d", help="Nilai order batal; tidak dihitung sebagai revenue."),
                        "Pending Sales": st.column_config.NumberColumn("Pending Sales", format="Rp %,d"),
                        "Average Order Value": st.column_config.NumberColumn("Average Order Value", format="Rp %,d"),
                        "Total Laba Bersih": st.column_config.NumberColumn("Total Laba Bersih", format="Rp %,d"),
                    },
                )

                st.markdown('<div class="section-title">📍 Customer Distance</div>', unsafe_allow_html=True)
                st.caption("Jarak garis lurus dari toko (3.582274, 98.717627).")
                latitude_col = next((c for c in ["Latitude", "latitude", "Lat", "lat"] if c in customer_orders.columns), None)
                longitude_col = next((c for c in ["Longitude", "longitude", "Lon", "lon", "Lng", "lng"] if c in customer_orders.columns), None)
                if latitude_col and longitude_col:
                    distance_orders = customer_orders[[customer_col, latitude_col, longitude_col]].copy()
                    distance_orders[latitude_col] = pd.to_numeric(distance_orders[latitude_col], errors="coerce")
                    distance_orders[longitude_col] = pd.to_numeric(distance_orders[longitude_col], errors="coerce")
                    distance_orders = distance_orders.dropna(subset=[latitude_col, longitude_col])
                    if not distance_orders.empty:
                        def distance_km(row):
                            lat1, lon1 = math.radians(3.5822738478321146), math.radians(98.71762676169524)
                            lat2, lon2 = math.radians(row[latitude_col]), math.radians(row[longitude_col])
                            a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
                            return 6371 * 2 * math.asin(math.sqrt(a))
                        distance_summary = distance_orders.assign(**{"Distance (km)": distance_orders.apply(distance_km, axis=1)})
                        distance_summary = distance_summary.groupby(customer_col, as_index=False)["Distance (km)"].min().rename(columns={customer_col: "Username"})
                        distance_summary = distance_summary.merge(display_customer[["Username", "Total Orders", "Completed Value", "Pending Sales", "Total Laba Bersih"]], on="Username", how="left").sort_values("Distance (km)")
                        st.dataframe(distance_summary, use_container_width=True, hide_index=True, column_config={"Distance (km)": st.column_config.NumberColumn("Distance (km)", format="%.2f km")})
                    else:
                        st.markdown('<div class="dashboard-meta-card">Koordinat customer tersedia, tetapi belum memiliki nilai yang valid.</div>', unsafe_allow_html=True)
                else:
                    address_col = next((c for c in ["Alamat Pengiriman", "Alamat Pengiriman (Shipping Address)", "Shipping Address"] if c in customer_orders.columns), None)
                    city_col = address_col or next((c for c in ["Kota/Kabupaten", "Kota", "Kabupaten/Kota", "City"] if c in customer_orders.columns), None)
                    province_col = next((c for c in ["Provinsi", "Province"] if c in customer_orders.columns), None)
                    if city_col:
                        location_label = "alamat pengiriman" if address_col else "kota/provinsi"
                        st.caption(f"Lokasi diperkirakan dari {location_label} menggunakan OpenStreetMap.")
                        if st.button("📍 Cari lokasi customer", key="geocode_customers"):
                            cache = st.session_state.setdefault("customer_geocode_cache", _load_customer_geocode_cache())
                            places = customer_orders[[city_col] + ([province_col] if province_col else [])].drop_duplicates().dropna(subset=[city_col])
                            progress = st.progress(0, text="Mencari lokasi customer...")
                            for index, (_, place_row) in enumerate(places.iterrows(), start=1):
                                place = str(place_row[city_col]).strip()
                                if province_col and pd.notna(place_row[province_col]):
                                    place = f"{place}, {place_row[province_col]}, Indonesia"
                                if place not in cache:
                                    try:
                                        cache[place] = _geocode_place(place)
                                    except Exception:
                                        cache[place] = None
                                    _save_customer_geocode_cache(cache)
                                progress.progress(index / len(places), text=f"Memproses {index}/{len(places)} lokasi...")
                            progress.empty()
                            st.rerun()
                        else:
                            st.markdown(f'<div class="dashboard-meta-card">Tekan tombol untuk mencari koordinat {location_label} customer dan menghitung jarak.</div>', unsafe_allow_html=True)
                    else:
                        st.markdown('<div class="dashboard-meta-card">Kolom kota customer tidak ditemukan pada file Order.</div>', unsafe_allow_html=True)
                    cache = st.session_state.setdefault("customer_geocode_cache", _load_customer_geocode_cache())
                    if city_col and cache:
                        def cached_distance(place_row):
                            place = str(place_row[city_col]).strip()
                            if province_col and pd.notna(place_row[province_col]):
                                place = f"{place}, {place_row[province_col]}, Indonesia"
                            coords = cache.get(place)
                            if not coords:
                                return pd.NA
                            lat, lon = map(math.radians, coords)
                            lat0, lon0 = math.radians(3.5822738478321146), math.radians(98.71762676169524)
                            a = math.sin((lat - lat0) / 2) ** 2 + math.cos(lat0) * math.cos(lat) * math.sin((lon - lon0) / 2) ** 2
                            return 6371 * 2 * math.asin(math.sqrt(a))
                        distance_summary = customer_orders.assign(**{"Distance (km)": customer_orders.apply(cached_distance, axis=1)})
                        distance_summary = distance_summary.groupby(customer_col, as_index=False)["Distance (km)"].min().rename(columns={customer_col: "Username"})
                        distance_summary = display_customer[["Username", "Total Orders", "Completed Value", "Pending Sales", "Total Laba Bersih"]].merge(distance_summary, on="Username", how="left").sort_values("Distance (km)", na_position="last")
                        distance_summary["Distance Status"] = distance_summary["Distance (km)"].map(lambda value: "Lokasi tidak ditemukan" if pd.isna(value) else "OK")
                        st.dataframe(distance_summary, use_container_width=True, hide_index=True, column_config={"Distance (km)": st.column_config.NumberColumn("Distance (km)", format="%.2f km")})

                st.markdown('<div class="section-title">🔎 Customer Detail</div>', unsafe_allow_html=True)
                selected_customer = st.selectbox("Pilih customer", customer_summary["Username"].sort_values().tolist())
                detail = customer_orders[customer_orders[customer_col] == selected_customer].sort_values("Waktu Pesanan Dibuat", ascending=False)
                detail_order_ids = detail["No. Pesanan"].astype(str).unique().tolist()
                reconciliation_result = st.session_state.get("result", pd.DataFrame()).copy()
                detail_result = reconciliation_result.copy()
                detail_result = detail_result[detail_result["No. Pesanan"].astype(str).isin(detail_order_ids)].copy() if not detail_result.empty and "No. Pesanan" in detail_result.columns else pd.DataFrame()
                reconciliation_order_count = (
                    detail_result["No. Pesanan"].astype(str).nunique()
                    if not detail_result.empty and "No. Pesanan" in detail_result.columns
                    else 0
                )
                detail_scope_cols = st.columns(2)
                detail_scope_cols[0].metric("Customer Orders", f"{len(detail_order_ids):,}")
                detail_scope_cols[1].metric("Orders in Reconciliation", f"{reconciliation_order_count:,}")
                reconciliation_period = _format_processed_period(
                    st.session_state.get("processed_start_date"),
                    st.session_state.get("processed_end_date"),
                )
                st.caption(
                    f"Data keuangan customer mengikuti periode Rekonsiliasi aktif ({reconciliation_period}). "
                    "Customer Orders berasal dari seluruh file Order aktif."
                )
                if not detail_result.empty:
                    # Untuk order unsettled, gunakan estimasi fee berbasis histori settled
                    # pada scope rekonsiliasi aktif, sama seperti proyeksi dashboard penjualan.
                    history_settled = (
                        reconciliation_result[reconciliation_result["Is_Settled"] == True].copy()
                        if "Is_Settled" in reconciliation_result.columns else pd.DataFrame()
                    )
                    history_subtotal = pd.to_numeric(history_settled.get("Subtotal", 0), errors="coerce").fillna(0).sum()
                    history_total_fee = abs(pd.to_numeric(history_settled.get("Total Biaya", 0), errors="coerce").fillna(0).sum())
                    history_process_fee = abs(pd.to_numeric(history_settled.get("Biaya Proses Pesanan", 0), errors="coerce").fillna(0).sum())
                    history_order_count = history_settled["No. Pesanan"].astype(str).nunique() if "No. Pesanan" in history_settled.columns else 0
                    process_fee_per_order = history_process_fee / history_order_count if history_order_count else 0
                    global_fee_ratio = max(history_total_fee - history_process_fee, 0) / history_subtotal if history_subtotal else 0.15
                    product_fee_ratio = {}
                    if not history_settled.empty and "Nama Produk" in history_settled.columns:
                        for product, rows in history_settled.groupby("Nama Produk"):
                            subtotal = pd.to_numeric(rows.get("Subtotal", 0), errors="coerce").fillna(0).sum()
                            total_fee = abs(pd.to_numeric(rows.get("Total Biaya", 0), errors="coerce").fillna(0).sum())
                            process_fee = abs(pd.to_numeric(rows.get("Biaya Proses Pesanan", 0), errors="coerce").fillna(0).sum())
                            product_fee_ratio[product] = max(total_fee - process_fee, 0) / subtotal if subtotal else global_fee_ratio

                    def customer_total_fee(row):
                        actual_fee = pd.to_numeric(row.get("Total Biaya", 0), errors="coerce")
                        if bool(row.get("Is_Settled", True)) or history_settled.empty:
                            return actual_fee if pd.notna(actual_fee) else 0
                        subtotal = pd.to_numeric(row.get("Subtotal", 0), errors="coerce")
                        subtotal = subtotal if pd.notna(subtotal) else 0
                        non_process = subtotal * product_fee_ratio.get(row.get("Nama Produk"), global_fee_ratio)
                        return -round(non_process + process_fee_per_order)

                    detail_result["Total Biaya"] = detail_result.apply(customer_total_fee, axis=1).astype(int)
                    detail_hpp_lookup = _build_hpp_lookup_for_dashboard(detail_result)
                    detail_result["Penghasilan"] = detail_result["Subtotal"] + detail_result["Total Biaya"].fillna(0)
                    detail_result["Status Penghasilan"] = detail_result["Is_Settled"].map(
                        lambda value: "Aktual" if _as_bool(value) else "Estimasi / Belum Final"
                    ) if "Is_Settled" in detail_result.columns else "Estimasi / Belum Final"
                    detail_result["Penghasilan Aktual"] = detail_result["Penghasilan"].where(
                        detail_result["Status Penghasilan"].eq("Aktual"), pd.NA
                    )
                    detail_result["Estimasi Penghasilan"] = detail_result["Penghasilan"].where(
                        detail_result["Status Penghasilan"].ne("Aktual"), pd.NA
                    )
                    def detail_hpp(row):
                        info = detail_hpp_lookup.get(row["Nama Produk"], {})
                        return row["Jumlah Bersih"] * info.get("HargaPokok", 0) / (info.get("Konversi", 1) or 1)
                    detail_result["HPP"] = detail_result.apply(detail_hpp, axis=1)
                    detail_result["HPP Status"] = detail_result["Nama Produk"].astype(str).isin(detail_hpp_lookup).map({True: "Confirmed", False: "UNMAPPED"})
                    detail_result["Laba Bersih"] = detail_result.apply(
                        lambda row: row["Penghasilan"] - row["HPP"] if row["HPP Status"] == "Confirmed" else pd.NA,
                        axis=1,
                    )
                    total_customer_net_profit = pd.to_numeric(
                        detail_result["Laba Bersih"], errors="coerce"
                    ).sum(min_count=1)
                    profit_cols = st.columns(1)
                    profit_cols[0].metric(
                        "Total Laba Bersih",
                        f"Rp {int(total_customer_net_profit):,}"
                        if pd.notna(total_customer_net_profit) else "Belum tersedia",
                        help="Total Penghasilan dikurangi HPP untuk order customer dalam scope Rekonsiliasi aktif. Order dengan HPP UNMAPPED tidak dihitung.",
                    )
                    detail_result = detail_result.rename(columns={"Subtotal": "Omzet Kotor", "Jumlah Bersih": "Qty Bersih"})
                    detail_columns = ["No. Pesanan", "Nama Produk", "Qty Bersih", "Omzet Kotor", "Total Biaya", "Penghasilan Aktual", "Estimasi Penghasilan", "HPP", "Laba Bersih", "HPP Status", "Is_Settled"]
                    st.dataframe(
                        detail_result[[c for c in detail_columns if c in detail_result.columns]],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Omzet Kotor": st.column_config.NumberColumn("Omzet Kotor", format="Rp %,d"),
                            "Total Biaya": st.column_config.NumberColumn("Total Biaya", format="Rp %,d"),
                            "Penghasilan Aktual": st.column_config.NumberColumn("Penghasilan Aktual", format="Rp %,d"),
                            "Estimasi Penghasilan": st.column_config.NumberColumn("Estimasi Penghasilan", format="Rp %,d"),
                            "HPP": st.column_config.NumberColumn("HPP", format="Rp %,d"),
                            "Laba Bersih": st.column_config.NumberColumn("Laba Bersih", format="Rp %,d"),
                        },
                    )
                else:
                    st.info("Detail rekonsiliasi customer belum tersedia.")

                st.markdown('<div class="section-title">🔁 Repeat Customer Analysis</div>', unsafe_allow_html=True)
                repeat_only = customer_summary[customer_summary["Completed Orders"] > 1]
                st.caption(f"{len(repeat_only):,} customer melakukan repeat order dari {len(customer_summary):,} customer.")
        except Exception as exc:
            st.error("Gagal membaca data customer.")
            st.exception(exc)

elif menu == "settings":
    st.title("⚙️ Setting")
    st.write("Atur preferensi tampilan dan ambang batas analisis dashboard.")
    if 'laba_warn_threshold' not in st.session_state:
        st.session_state.laba_warn_threshold = 10000
    st.session_state.laba_warn_threshold = st.number_input(
        "Threshold laba kuning", min_value=0, max_value=1000000,
        value=int(st.session_state.laba_warn_threshold), step=1000,
        key="laba_warn_threshold_settings",
        help="Nilai laba bersih sampai batas ini diberi warna kuning; di atasnya hijau.",
    )
    st.caption("Pengaturan ini digunakan pada tampilan kolom Laba Bersih di detail transaksi.")

elif menu == "stock":
    st.title("Valuasi Nilai Stok")
    st.write("Upload file Mass Update Sales Info Shopee untuk menghitung nilai stok berdasarkan HPP.")
    upload = st.file_uploader("Upload data stok terkini (.xlsx)", type=["xlsx"], key="stock_valuation_upload")
    if upload is not None:
        uploaded_bytes = upload.getvalue()
        if uploaded_bytes != st.session_state.get("stock_upload_bytes"):
            st.session_state.stock_valuation_processed = False
        st.session_state.stock_upload_bytes = uploaded_bytes
        process_stock = st.button("⚙️ Proses Valuasi Stok", type="primary", key="process_stock_valuation")
        if process_stock:
            st.session_state.stock_valuation_processed = True
    if st.session_state.get("stock_upload_bytes") and st.session_state.get("stock_valuation_processed", False):
        try:
            stock = _read_shopee_stock_excel(io.BytesIO(st.session_state.stock_upload_bytes))
            if not {"Nama Produk", "Stok"}.issubset(stock.columns):
                st.error("Kolom Nama Produk dan Stok tidak ditemukan.")
            else:
                stock = stock[stock["Nama Produk"].notna()].copy()
                stock["Nama Produk"] = stock["Nama Produk"].astype(str).str.strip()
                stock["Stok"] = pd.to_numeric(stock["Stok"], errors="coerce").fillna(0)
                master = load_hpp_master(file_source="files/hpp_produk.xlsx")
                by_key = {r["ItemKey"]: r.to_dict() for _, r in master.iterrows()}
                mapping = load_mapping()
                stock["HPP / Unit"] = stock["Nama Produk"].map(lambda p: (lambda i: i.get("HargaPokok", 0) / (i.get("Konversi", 1) or 1))(by_key.get(mapping.get(p), {})))
                stock["Nilai Stok"] = stock["Stok"] * stock["HPP / Unit"]
                stock["Status HPP"] = stock["HPP / Unit"].map(lambda v: "Valid" if v > 0 else "Missing")
                valid = stock["Status HPP"].eq("Valid")
                cols = st.columns(4)
                cols[0].metric("Total Variasi", f"{len(stock):,}")
                cols[1].metric("Total Unit Stok", f"{stock['Stok'].sum():,.0f}")
                cols[2].metric("Nilai Stok (HPP Valid)", f"Rp {stock.loc[valid, 'Nilai Stok'].sum():,.0f}")
                cols[3].metric("HPP Coverage", f"{valid.mean() * 100:.1f}%")
                st.subheader("Detail Valuasi Stok")
                show = [c for c in ["Kode Produk", "Nama Produk", "Kode Variasi", "Nama Variasi", "SKU", "Stok", "HPP / Unit", "Nilai Stok", "Status HPP"] if c in stock.columns]
                editable = stock[show].copy()
                edited_stock = st.data_editor(
                    editable,
                    use_container_width=True,
                    hide_index=True,
                    key="stock_valuation_editor",
                    disabled=[c for c in show if c not in {"Stok", "HPP / Unit"}],
                    column_config={
                        "Stok": st.column_config.NumberColumn("Stok", min_value=0, step=1, format="%,.0f"),
                        "HPP / Unit": st.column_config.NumberColumn("HPP / Unit", min_value=0, step=1, format="Rp %,.0f"),
                        "Nilai Stok": st.column_config.NumberColumn("Nilai Stok", format="Rp %,.0f"),
                    },
                )
                if {"Stok", "HPP / Unit"}.issubset(edited_stock.columns):
                    try:
                        edited_stock["Stok"] = pd.to_numeric(edited_stock["Stok"], errors="coerce").fillna(0)
                        edited_stock["HPP / Unit"] = pd.to_numeric(edited_stock["HPP / Unit"], errors="coerce").fillna(0)
                        edited_stock["Nilai Stok"] = edited_stock["Stok"] * edited_stock["HPP / Unit"]
                        edited_stock["Status HPP"] = edited_stock["HPP / Unit"].map(lambda v: "Valid" if v > 0 else "Missing")
                        edited_valid = edited_stock["Status HPP"].eq("Valid")
                        recalculate_clicked = st.button("🔄 Proses Ulang Valuasi", type="primary", key="recalculate_stock_valuation")
                        if recalculate_clicked:
                            st.session_state.stock_valuation_total_edited = float(edited_stock.loc[edited_valid, "Nilai Stok"].sum())
                            st.session_state.stock_valuation_valid_edited = int(edited_valid.sum())
                            st.session_state.stock_valuation_units_edited = float(edited_stock["Stok"].sum())
                            st.session_state.stock_valuation_coverage_edited = float(edited_valid.mean() * 100)
                            st.session_state.stock_valuation_debug = f"Klik diterima; {len(edited_stock):,} baris dihitung."
                        if "stock_valuation_total_edited" in st.session_state:
                            st.success("Valuasi berhasil diproses ulang dari data yang sudah diedit.")
                            st.caption("Tabel berikut menampilkan Nilai Stok terbaru setelah proses ulang.")
                            st.dataframe(
                                edited_stock[show],
                                use_container_width=True,
                                hide_index=True,
                                column_config={
                                    "HPP / Unit": st.column_config.NumberColumn("HPP / Unit", format="Rp %,.0f"),
                                    "Nilai Stok": st.column_config.NumberColumn("Nilai Stok", format="Rp %,.0f"),
                                },
                            )
                            st.subheader("Hasil Proses Ulang")
                            recalculated_cols = st.columns(4)
                            recalculated_cols[0].metric("Total Unit Stok", f"{st.session_state.stock_valuation_units_edited:,.0f}")
                            recalculated_cols[1].metric("Nilai Stok (HPP Valid)", f"Rp {st.session_state.stock_valuation_total_edited:,.0f}")
                            recalculated_cols[2].metric("Variasi HPP Valid", f"{st.session_state.stock_valuation_valid_edited:,}")
                            recalculated_cols[3].metric("HPP Coverage", f"{st.session_state.stock_valuation_coverage_edited:.1f}%")
                        if "stock_valuation_debug" in st.session_state:
                            st.caption(st.session_state.stock_valuation_debug)
                    except Exception as exc:
                        st.error("Gagal menghitung ulang valuasi stok:")
                        st.exception(exc)
        except Exception as exc:
            st.error("File stok tidak dapat dibaca. Detail error:")
            st.exception(exc)

elif menu == "hpp":
    st.title("📦 Kelola Master HPP & Pemetaan Multi-Satuan")
    st.write("Kelola database harga pokok toko, satuan/konversi kemasan, dan relasi pemetaan SKU Shopee.")

    # Load master HPP data
    hpp_excel_path = "files/hpp_produk.xlsx"
    df_hpp_master = load_hpp_master(file_source=hpp_excel_path)
    mapping_dict = load_mapping()

    tab_mapping, tab_master = st.tabs([
        "🔗 Pemetaan SKU Shopee ↔ Master HPP", 
        "📋 Master Database HPP Toko"
    ])

    # ─── TAB 1: PEMETAAN SKU SHOPEE ───
    with tab_mapping:
        # Pemetaan tetap dapat dimulai saat sesi Rekonsiliasi sudah berakhir atau
        # file diunggah dari tab/browser lain. File ini diperlakukan sama dengan
        # unggahan Order pada menu Rekonsiliasi dan tersimpan untuk sesi aktif.
        mapping_order_upload = st.file_uploader(
            "File Order untuk mengambil daftar produk (Excel)",
            type=["xlsx"],
            key="mapping_order_uploader",
            help="Opsional bila file Order sudah diunggah di menu Rekonsiliasi.",
        )
        if mapping_order_upload is not None:
            st.session_state.uploaded_order_bytes = persist_session_order_upload(mapping_order_upload)
            st.session_state.uploaded_order_name = mapping_order_upload.name

        st.subheader("🔗 Pemetaan SKU Shopee ke Satuan Master HPP")
        st.caption("Petakan setiap produk di toko Shopee ke satuan master yang tepat (PCS, RTG, PAK, DUS). Anda juga dapat langsung mengedit nilai HPP/Unit.")

        # Ambil daftar produk yang sudah ada di mapping atau dari file rekonsiliasi jika ada
        all_prods_set = set(mapping_dict.keys())
        if 'result' in st.session_state and not st.session_state.result.empty:
            all_prods_set.update(st.session_state.result['Nama Produk'].dropna().unique().tolist())
        uploaded_order_bytes = st.session_state.get('uploaded_order_bytes')
        uploaded_order_name = st.session_state.get('uploaded_order_name')
        uploaded_order_path = st.session_state.get('uploaded_order_path')
        if uploaded_order_path and Path(uploaded_order_path).is_file():
            uploaded_order_bytes = Path(uploaded_order_path).read_bytes()
        # Fallback untuk sesi yang sudah memiliki widget uploader sebelum byte
        # disimpan (misalnya setelah hot-reload aplikasi).
        if not uploaded_order_bytes:
            active_order_upload = st.session_state.get('order_uploader')
            if active_order_upload is not None:
                uploaded_order_bytes = active_order_upload.getvalue()
                uploaded_order_name = active_order_upload.name
                st.session_state.uploaded_order_bytes = persist_session_order_upload(active_order_upload)
                uploaded_order_path = st.session_state.get('uploaded_order_path')

        if uploaded_order_bytes:
            uploaded_order_name = uploaded_order_name or 'file upload aktif'
            st.caption(f"Sumber produk: **{uploaded_order_name}** (upload session **{st.session_state.session_id[:8]}**; bukan file toko lain)")
            try:
                uploaded_order_for_mapping = io.BytesIO(uploaded_order_bytes)
                uploaded_product_options = get_order_filter_options(uploaded_order_for_mapping)
                all_prods_set.update(uploaded_product_options.get('Nama Produk', []))
            except Exception:
                # Produk dari hasil rekonsiliasi/mapping tetap ditampilkan jika file
                # upload gagal dibaca.
                pass
        else:
            st.info("Belum ada file Order aktif. Unggah file pada bagian di atas atau melalui menu Rekonsiliasi.")
        
        all_prods_list = sorted(list(all_prods_set))

        if not all_prods_list:
            st.info("💡 Belum ada produk Shopee yang tercatat. Unggah file Order untuk mengambil daftar produk.")
        else:
            # Auto-suggestion tidak pernah menjadi mapping aktif. Mapping hanya
            # berubah melalui pilihan user dan tombol simpan di bawah.

            BELUM_DIPETAKAN = "(Belum Dipetakan)"
            hpp_options_list = [BELUM_DIPETAKAN] + [
                f"{r['KodeItem']} - {r['NamaItem']} [{r['Satuan']} (isi {r['Konversi']:g})] (HPP: Rp {r['HargaPokok']:,.0f})"
                for _, r in df_hpp_master.iterrows()
            ]
            key_to_label = {
                r['ItemKey']: f"{r['KodeItem']} - {r['NamaItem']} [{r['Satuan']} (isi {r['Konversi']:g})] (HPP: Rp {r['HargaPokok']:,.0f})"
                for _, r in df_hpp_master.iterrows()
            }
            label_to_key = {v: k for k, v in key_to_label.items()}

            key_to_hpp = {
                r['ItemKey']: round(r['HargaPokok'] / (r['Konversi'] or 1))
                for _, r in df_hpp_master.iterrows()
            }

            table_rows = []
            needs_confirm_count = 0
            for prod in all_prods_list:
                # Source of Truth Akuntansi: Hanya mapping yang SUDAH dikonfirmasi (tercatat di mapping_dict)
                confirmed_key = mapping_dict.get(prod, '')
                
                if confirmed_key:
                    cur_label = key_to_label.get(confirmed_key, BELUM_DIPETAKAN)
                    match_status = "✅ Terpetakan"
                    # HPP dihitung HANYA dari confirmed_key
                    hpp_val = key_to_hpp.get(confirmed_key, None)
                else:
                    # Belum ada mapping terkonfirmasi: Cek apakah ada saran fuzzy matching
                    sugg_key, sugg_score, _ = get_suggestion_with_confidence(prod, df_hpp_master)
                    if sugg_score >= 0.70 and sugg_key:
                        # 70-89%: Pasang suggestion di dropdown agar siap di-review user, TAPI HPP tetap None (tidak masuk akuntansi sebelum disimpan)
                        cur_label = key_to_label.get(sugg_key, BELUM_DIPETAKAN)
                        match_status = f"🔍 Rekomendasi ({int(sugg_score*100)}%)"
                        needs_confirm_count += 1
                        hpp_val = None
                    else:
                        # < 70%: Unmapped
                        cur_label = BELUM_DIPETAKAN
                        match_status = "❌ Belum Terpetakan"
                        hpp_val = None

                table_rows.append({
                    'Status': match_status,
                    'Nama Produk (Shopee)': prod,
                    'Pemetaan HPP (ItemKey & Satuan)': cur_label,
                    'HPP (@)': hpp_val,
                })
            mapping_df = pd.DataFrame(table_rows)

            # Highlight info bar
            unmapped_count = sum(1 for r in table_rows if r['Status'] == "❌ Belum Terpetakan")
            zero_hpp_count = sum(1 for r in table_rows if r['HPP (@)'] == 0 or r['HPP (@)'] is None)

            m_c1, m_c2, m_c3, m_c4 = st.columns(4)
            with m_c1:
                st.metric("Total Produk Terdaftar", f"{len(table_rows)} SKU")
            with m_c2:
                st.metric("Perlu Konfirmasi", f"{needs_confirm_count} SKU", delta=f"{needs_confirm_count} Rekomendasi" if needs_confirm_count > 0 else "0", delta_color="normal")
            with m_c3:
                st.metric("Belum Terpetakan (<70%)", f"{unmapped_count} SKU", delta=f"-{unmapped_count}" if unmapped_count > 0 else "Semua Beres", delta_color="inverse")
            with m_c4:
                st.metric("HPP Kosong / 0", f"{zero_hpp_count} SKU", delta=f"-{zero_hpp_count}" if zero_hpp_count > 0 else "Aman", delta_color="inverse")

            review_df = mapping_df[mapping_df['HPP (@)'].isna()].copy()
            if review_df.empty:
                st.success("✅ Semua SKU sudah memiliki pemetaan HPP.")
            else:
                st.subheader("Perlu ditinjau")
                st.caption("Hanya produk dengan rekomendasi yang belum dikonfirmasi atau belum memiliki pemetaan.")
                st.dataframe(review_df, use_container_width=True, hide_index=True)

            manual_section = st.expander("Koreksi manual (semua SKU)", expanded=False)
            manual_section.caption(
                "Gunakan bila ingin mengganti hasil otomatis, mengisi produk yang belum terpetakan, "
                "atau mengubah HPP per unit."
            )
            edited_df = manual_section.data_editor(
                mapping_df,
                column_config={
                    'Status': st.column_config.TextColumn(
                        "Status", disabled=True, width="small"
                    ),
                    'Nama Produk (Shopee)': st.column_config.TextColumn(
                        "Nama Produk (Shopee)", disabled=True, width="large"
                    ),
                    'Pemetaan HPP (ItemKey & Satuan)': st.column_config.SelectboxColumn(
                        "Pemetaan HPP (Item, Satuan, HPP)",
                        options=hpp_options_list,
                        required=True,
                        width="large",
                    ),
                    'HPP (@)': st.column_config.NumberColumn(
                        "HPP/Unit (Rp) ✏️",
                        format="%,d",
                        min_value=0,
                        step=1,
                        width="small",
                        help="HPP per unit terjual (setelah konversi). Edit langsung untuk update master HPP.",
                    ),
                },
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                key="bulk_mapping_editor_standalone",
            )

            if manual_section.button("💾 Simpan Semua Perubahan Pemetaan", key="btn_save_mapping_page", type="primary"):
                mapping_changed = 0
                hpp_changed = 0

                df_hpp_edit = pd.read_excel(hpp_excel_path)
                df_hpp_edit['ItemKey'] = df_hpp_edit['KodeItem'].astype(str) + '_' + df_hpp_edit['Satuan'].astype(str)

                for _, row in edited_df.iterrows():
                    prod_name = row['Nama Produk (Shopee)']
                    chosen_label = row['Pemetaan HPP (ItemKey & Satuan)']
                    new_hpp_unit = row['HPP (@)']

                    if chosen_label == BELUM_DIPETAKAN:
                        active_key = ''
                    else:
                        active_key = label_to_key.get(chosen_label, mapping_dict.get(prod_name, ''))

                    if chosen_label == BELUM_DIPETAKAN:
                        if prod_name in mapping_dict:
                            del mapping_dict[prod_name]
                            mapping_changed += 1
                    else:
                        if active_key and mapping_dict.get(prod_name) != active_key:
                            mapping_dict[prod_name] = active_key
                            mapping_changed += 1

                    if active_key and pd.notna(new_hpp_unit):
                        new_hpp_unit = int(round(new_hpp_unit))
                        old_hpp_unit = key_to_hpp.get(active_key)
                        if old_hpp_unit is None or new_hpp_unit != int(round(old_hpp_unit)):
                            mask = df_hpp_edit['ItemKey'] == active_key
                            if mask.any():
                                konv = df_hpp_edit.loc[mask, 'Konversi'].iloc[0] or 1
                                new_harga_pokok = round(new_hpp_unit * konv, 2)
                                df_hpp_edit.loc[mask, 'HargaPokok'] = new_harga_pokok
                                hpp_changed += 1

                save_mapping(mapping_dict)

                if hpp_changed > 0:
                    df_hpp_edit.drop(columns=['ItemKey'], inplace=True)
                    df_hpp_edit.to_excel(hpp_excel_path, index=False)

                total_changed = mapping_changed + hpp_changed
                if total_changed > 0:
                    msgs = []
                    if mapping_changed:
                        msgs.append(f"{mapping_changed} pemetaan")
                    if hpp_changed:
                        msgs.append(f"{hpp_changed} nilai HPP di master Excel")
                    st.success(f"✅ Berhasil disimpan: {' dan '.join(msgs)}!")
                    st.rerun()
                else:
                    st.info("Tidak ada perubahan yang terdeteksi.")

    # ─── TAB 2: MASTER DATABASE HPP TOKO ───
    with tab_master:
        st.subheader("📋 Master Database HPP Produk")
        st.caption("Database utama harga pokok multi-satuan toko (`files/hpp_produk.xlsx`). Anda dapat mengedit harga/konversi langsung di tabel atau upload file Excel baru.")

        with st.sidebar:
            st.subheader("📤 Upload Master HPP Baru")
            uploaded_new_master = st.file_uploader("Upload Excel Master HPP Baru", type=['xlsx'], key="new_master_uploader")
            if uploaded_new_master:
                if st.button("📥 Timpa Master HPP dengan File Ini", type="secondary"):
                    df_new = pd.read_excel(uploaded_new_master)
                    df_new.to_excel(hpp_excel_path, index=False)
                    st.success("✅ File master HPP berhasil diperbarui!")
                    st.rerun()

        # Baca raw master Excel
        df_raw_master = pd.read_excel(hpp_excel_path)
        
        col_m_info1, col_m_info2 = st.columns([3, 1])
        with col_m_info1:
            st.write(f"Total baris data: **{len(df_raw_master)} item/satuan**")
        with col_m_info2:
            # Download file master saat ini
            buffer_master = io.BytesIO()
            df_raw_master.to_excel(buffer_master, index=False)
            st.download_button(
                "⬇️ Download Master Excel",
                data=buffer_master.getvalue(),
                file_name="master_hpp_produk.xlsx",
                mime="application/vnd.ms-excel",
                use_container_width=True
            )

        sort_col1, sort_col2 = st.columns([2, 1])
        with sort_col1:
            master_sort_col = st.selectbox(
                "Urutkan berdasarkan",
                options=df_raw_master.columns.tolist(),
                index=df_raw_master.columns.tolist().index('NamaItem') if 'NamaItem' in df_raw_master.columns else 0,
                key="master_hpp_sort_column",
            )
        with sort_col2:
            master_sort_ascending = st.selectbox(
                "Arah urutan",
                options=["Naik (A–Z / kecil ke besar)", "Turun (Z–A / besar ke kecil)"],
                key="master_hpp_sort_direction",
            ) == "Naik (A–Z / kecil ke besar)"

        # Kolom angka memakai kunci numerik agar 9.000 diurutkan sebelum 10.000.
        # Urutan tampilan ini juga menjadi urutan yang disimpan ke master Excel.
        display_master_df = df_raw_master.copy()
        numeric_sort_columns = {'Konversi', 'HargaPokok', 'HargaJual'}
        if master_sort_col in numeric_sort_columns:
            display_master_df['_sort_key'] = pd.to_numeric(
                display_master_df[master_sort_col], errors='coerce'
            )
            display_master_df = display_master_df.sort_values(
                '_sort_key', ascending=master_sort_ascending, na_position='last', kind='stable'
            ).drop(columns=['_sort_key'])
        else:
            display_master_df = display_master_df.sort_values(
                master_sort_col, ascending=master_sort_ascending, na_position='last', kind='stable'
            )

        # Key berbeda untuk setiap konfigurasi urutan agar perubahan urutan tidak
        # menerapkan edit sementara ke baris yang kini berada di posisi lain.
        master_editor_key = f"master_hpp_data_editor_{master_sort_col}_{master_sort_ascending}"

        edited_master_df = st.data_editor(
            display_master_df,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            key=master_editor_key,
            column_config={
                'KodeItem': st.column_config.TextColumn("Kode Item", required=True),
                'NamaItem': st.column_config.TextColumn("Nama Item", required=True, width="large"),
                'Satuan': st.column_config.TextColumn("Satuan", required=True, width="small"),
                'Konversi': st.column_config.NumberColumn("Konversi (Isi)", min_value=1.0, step=1.0, format="%.0f"),
                'HargaPokok': st.column_config.NumberColumn("Harga Pokok (Rp)", format="%,d", min_value=0),
                'HargaJual': st.column_config.NumberColumn("Harga Jual (Rp)", format="%,d", min_value=0),
            }
        )

        if st.button("💾 Simpan Perubahan Master Database", key="btn_save_master_db", type="primary"):
            try:
                edited_master_df.to_excel(hpp_excel_path, index=False)
                st.success("✅ Master Database HPP berhasil disimpan!")
                st.rerun()
            except Exception as e:
                st.error(f"❌ Gagal menyimpan data: {e}")
