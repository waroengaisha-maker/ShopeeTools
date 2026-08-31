import streamlit as st
import pandas as pd
from data_processor import (
    process_reconciliation, add_total_row, format_thousands,
    get_order_date_bounds, get_order_filter_options, extract_adjustments,
    get_settlement_stats, generate_product_summary,
    COL_PCT_ADM, COL_PCT_XTRA, COL_PCT_PROMO, COL_PCT_SUB_BIAYA
)
from hpp_manager import (
    load_hpp_master, load_mapping, save_mapping, auto_suggest_mapping,
    get_suggestion_with_confidence
)
import io
import hashlib
import html
import logging
import pickle
import re
import shutil
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

st.set_page_config(layout="wide", page_title="Rekonsiliasi Shopee")

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
    mapping_dict = auto_suggest_mapping(all_unique_prods, df_hpp_master)
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
    empty_summary = {'count': 0, 'value': 0, 'income_lost': 0, 'rate': 0.0, 'details': pd.DataFrame(), 'by_type': []}
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
    unsettled = df[df['Is_Settled'] == False].copy() if 'Is_Settled' in df.columns else pd.DataFrame()

    total_subtotal = int(settled['Subtotal'].sum()) if 'Subtotal' in settled.columns else 0
    total_biaya = int(settled['Total Biaya'].sum()) if 'Total Biaya' in settled.columns else 0
    total_penghasilan = int(settled['Subtotal'].sum()) + int(settled['Total Biaya'].sum()) if not settled.empty else 0

    def get_item_hpp(row):
        info = hpp_lookup_map.get(row.get('Nama Produk'), {})
        harga_pokok = info.get('HargaPokok', 0)
        konversi = info.get('Konversi', 1) or 1
        return row.get('Jumlah Bersih', 0) * (harga_pokok / konversi)

    total_hpp = int(round(settled.apply(get_item_hpp, axis=1).sum())) if not settled.empty and hpp_lookup_map else 0
    laba_bersih = total_penghasilan - total_hpp
    margin_laba = (laba_bersih / total_penghasilan * 100) if total_penghasilan > 0 else 0.0
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
    padding: 1rem 1.05rem;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(30, 41, 59, 0.96) 0%, rgba(15, 23, 42, 0.92) 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.16);
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
    font-size: 1.45rem;
    line-height: 1.1;
    font-weight: 800;
    color: #f8fafc;
}
.kpi-card .pct {
    font-size: 0.76rem;
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
if menu not in {"dashboard", "reconciliation", "hpp"}:
    menu = "dashboard"
session_query = f"session={st.session_state.session_id}"

with st.sidebar:
    st.markdown(
        f'<h2><a class="sidebar-brand-link" href="?{session_query}" target="_self">🏪 Warung Aisha Tool</a></h2>',
        unsafe_allow_html=True,
    )
    st.caption("Alat Analisis & Manajemen Penjualan Shopee")
    st.divider()

    st.markdown("📌 **Navigasi**")
    dashboard_active = " active" if menu == "dashboard" else ""
    reconciliation_active = " active" if menu == "reconciliation" else ""
    hpp_active = " active" if menu == "hpp" else ""
    st.markdown(
        f'<a class="sidebar-nav-link{dashboard_active}" href="?{session_query}" target="_self">🏠 Dashboard</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{reconciliation_active}" href="?page=reconciliation&{session_query}" target="_self">📊 Rekonsiliasi Shopee</a>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a class="sidebar-nav-link{hpp_active}" href="?page=hpp&{session_query}" target="_self">📦 Kelola Master HPP</a>',
        unsafe_allow_html=True,
    )
    st.caption("Gunakan Ctrl/Cmd+klik atau klik kanan → buka di tab baru.")
    st.divider()

    if menu == "dashboard":
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
        if _load_session_result():
            st.rerun()
        else:
            st.info("Jalankan proses rekonsiliasi di menu Rekonsiliasi Shopee agar dashboard ini menampilkan data.")
    else:
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
                <div class="dashboard-kicker">Dashboard Penjualan</div>
                <div class="dashboard-title">DASHBOARD PENJUALAN</div>
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

        settled_section = st.container(border=True)
        settled_section.markdown('<div class="section-parent-card"><div class="title">Pesanan Settled</div><div class="description">Ringkasan omzet, biaya, penghasilan, HPP, dan laba dari pesanan yang sudah settled.</div></div>', unsafe_allow_html=True)

        # KPI utama pesanan settled
        if 'result' in st.session_state:
            laba_kpi_color = "#10b981" if laba_bersih >= 0 else "#f87171"
            settled_section.markdown(
                f"""
                <div class="kpi-grid">
                    <div class="kpi-card kpi-gross">
                        <span class="label">Omzet Kotor</span>
                        <div class="value">Rp {total_omzet:,.0f}</div>
                        <div class="pct">Subtotal penjualan settled</div>
                    </div>
                    <div class="kpi-card kpi-net">
                        <span class="label">Penghasilan Bersih</span>
                        <div class="value">Rp {total_penghasilan:,.0f}</div>
                        <div class="pct">Setelah biaya Shopee & penyesuaian</div>
                    </div>
                    <div class="kpi-card kpi-fee">
                        <span class="label">Total Biaya Shopee</span>
                        <div class="value">Rp {total_biaya:,.0f}</div>
                        <div class="pct">Fee layanan, admin, dan pajak</div>
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
                    <div class="kpi-card kpi-margin">
                        <span class="label">Pending</span>
                        <div class="value">{total_pending}</div>
                        <div class="pct">Pesanan belum settlement</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            projection_section = st.container(border=True)
            projection_section.markdown('<div class="section-parent-card"><div class="title">Proyeksi Pesanan Unsettled</div><div class="description">Estimasi pending berdasarkan pola fee settled dan mapping HPP saat ini. Tidak digabung ke KPI aktual.</div></div>', unsafe_allow_html=True)
            pending_laba_class = "metric-green" if pending_laba >= 0 else "metric-red"
            projection_section.markdown(
                f"""
                <div class="section-card-grid projection-card-grid">
                    <div class="section-metric-card metric-amber"><span class="label">Pending</span><div class="value">{total_pending:,}</div><div class="sub">Belum settlement</div></div>
                    <div class="section-metric-card metric-blue"><span class="label">Estimasi Omzet</span><div class="value">Rp {pending_omzet:,.0f}</div><div class="sub">Subtotal pending</div></div>
                    <div class="section-metric-card metric-red"><span class="label">Estimasi Biaya</span><div class="value">Rp {pending_biaya:,.0f}</div><div class="sub">Estimasi fee Shopee</div></div>
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
            anomaly_section = st.container(border=True)
            anomaly_section.markdown('<div class="section-parent-card"><div class="title">Anomali &amp; Risiko</div><div class="description">Ringkasan dampak pesanan yang dibatalkan.</div></div>', unsafe_allow_html=True)
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
                    <div class="section-metric-card metric-red">
                        <span class="label">Pesanan Dibatalkan</span>
                        <div class="value">{cancelled_summary['count']:,}</div>
                        <div class="sub">Order berstatus batal</div>
                    </div>
                    <div class="section-metric-card metric-orange">
                        <span class="label">Nilai Pesanan Batal</span>
                        <div class="value">Rp {cancelled_summary['value']:,.0f}</div>
                        <div class="sub">Estimasi nilai bruto</div>
                    </div>
                    <div class="section-metric-card metric-amber">
                        <span class="label">Tingkat Pembatalan</span>
                        <div class="value">{cancelled_summary['rate']:.2f}%</div>
                        <div class="sub">Dari seluruh order periode ini</div>
                    </div>
                    <div class="section-metric-card metric-blue">
                        <span class="label">Estimasi Penghasilan Hilang</span>
                        <div class="value">Rp {cancelled_summary['income_lost']:,.0f}</div>
                        <div class="sub">Setelah estimasi fee Shopee</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            anomaly_section.caption("Pesanan dibatalkan tidak masuk perhitungan Omzet, Penghasilan, HPP, maupun Laba Bersih.")
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

        filter_options = _build_dashboard_filter_options(result)
        filter_labels = {
            "Periode": "Periode",
            "Produk": "Produk",
            "SKU": "SKU",
            "Status Settlement": "Status Settlement",
            "Status Retur": "Status Retur",
        }

        st.markdown('<div class="filter-panel">', unsafe_allow_html=True)
        st.subheader("Filter")
        c1, c2, c3 = st.columns(3)
        c4, c5 = st.columns(2)

        with c1:
            periode_vals = st.multiselect(filter_labels["Periode"], filter_options.get("Periode", []), key="dash_filter_periode")
        with c2:
            produk_vals = st.multiselect(filter_labels["Produk"], filter_options.get("Produk", []), key="dash_filter_produk")
        with c3:
            sku_vals = st.multiselect(filter_labels["SKU"], filter_options.get("SKU", []), key="dash_filter_sku")
        with c4:
            settlement_vals = st.multiselect(filter_labels["Status Settlement"], filter_options.get("Status Settlement", []), key="dash_filter_settlement")
        with c5:
            retur_vals = st.multiselect(filter_labels["Status Retur"], filter_options.get("Status Retur", []), key="dash_filter_retur")
        st.markdown('</div>', unsafe_allow_html=True)

        dashboard_filtered = _apply_dashboard_filters(result, periode_vals, produk_vals, sku_vals, settlement_vals, retur_vals)

        display_df = dashboard_filtered.copy()
        if 'No.' in display_df.columns:
            display_df = display_df.drop(columns=['No.'])
        display_df.insert(0, 'No.', range(1, len(display_df) + 1))

        with st.container(border=True):
            st.caption(f"Menampilkan {len(dashboard_filtered)} baris dari {len(result)} baris data.")
            st.dataframe(display_df, use_container_width=True, hide_index=True)


# ==============================================================================
# 📊 MENU 2: REKONSILIASI SHOPEE
# ==============================================================================
elif menu == "reconciliation":
    st.title("📊 Rekonsiliasi Transaksi & Margin Shopee")
    st.write("Upload laporan Order dan Laporan Penghasilan Shopee untuk melihat analisis keuangan, fee, dan margin laba.")

    with st.sidebar:
        st.subheader("📁 Upload File Transaksi")
        uploaded_order = st.file_uploader("1. Laporan Order (Excel) *", type=['xlsx'], key="order_uploader")
        uploaded_income = st.file_uploader("2. Laporan Penghasilan (Excel) *", type=['xlsx'], key="income_uploader")
        uploaded_hpp = st.file_uploader(
            "3. Laporan Master HPP Periode Ini (Opsional)", 
            type=['xlsx'],
            key="hpp_override_uploader",
            help="Unggah jika ingin memakai HPP periode khusus. Jika kosong, sistem otomatis memakai master HPP default toko."
        )
        # Simpan sumber upload agar tab Pemetaan tetap dapat membaca daftar produk
        # walaupun pengguna berpindah menu sebelum menjalankan rekonsiliasi.
        if uploaded_order is not None:
            # Simpan byte aktual dan salinan fisik terisolasi per session, bukan
            # hanya handle UploadedFile yang dapat hilang setelah navigasi menu.
            st.session_state.uploaded_order_bytes = persist_session_order_upload(uploaded_order)

    if not uploaded_order or not uploaded_income:
        st.info("👈 **Silakan unggah Laporan Order dan Laporan Penghasilan Shopee di sidebar sebelah kiri** untuk memulai rekonsiliasi.")
    else:
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
                unsettled = df_slice[df_slice['Is_Settled'] == False].copy() if 'Is_Settled' in df_slice.columns else pd.DataFrame()
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
                if not settled.empty and hpp_lookup_map:
                    s['total_hpp'] = int(round(settled.apply(get_item_hpp_inner, axis=1).sum()))
                    s['laba_bersih'] = s['total_penghasilan'] - s['total_hpp']
                    s['margin_laba'] = (s['laba_bersih'] / sub * 100) if sub > 0 else 0.0
                else:
                    s['total_hpp'] = 0
                    s['laba_bersih'] = s['total_penghasilan']
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
            mapping_dict = auto_suggest_mapping(all_unique_prods, df_hpp_master)
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
            filter_options = st.session_state.get('filter_options', {})
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
                '<div class="pct">Nilai Penjualan Produk Settled</div>'
                '</div>'
            )
            fees_card = (
                '<div class="summary-card card-fees">'
                '<div class="label">Total Biaya Layanan</div>'
                f'<div class="value">Rp {total_biaya:,.0f}</div>'
                f'<div class="pct">Potongan Biaya: {pct_biaya:.1f}%</div>'
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
                    f'<div class="pct">Subtotal Rp {unsettled_subtotal:,.0f} (est. fee {effective_fee_ratio*100:.1f}%)</div>'
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
            settled_cards_html = gross_card + fees_card + adj_card + net_card + hpp_card + laba_card + daily_card
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
            # Pemetaan dengan kecocokan sangat tinggi (>=90%) disimpan otomatis.
            # Produk lain tetap menunggu peninjauan agar angka HPP tidak keliru.
            mapping_before_auto = dict(mapping_dict)
            mapping_dict = auto_suggest_mapping(all_prods_list, df_hpp_master)
            auto_mapped_count = sum(
                1
                for product in all_prods_list
                if not mapping_before_auto.get(product) and mapping_dict.get(product)
            )
            if auto_mapped_count:
                st.success(f"✅ {auto_mapped_count} produk dipetakan otomatis dengan kecocokan tinggi.")

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
