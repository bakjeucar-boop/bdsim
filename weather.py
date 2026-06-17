#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""weather.py — 기상 데이터 수집 및 EPW 생성 모듈

포함 기능:
  - fetch_open_meteo_hourly_forecast() : Open-Meteo API 시간별 기상 수집
  - build_epw_year_dataframe()         : 수집 데이터 → 8760h EPW 연간 DataFrame
  - write_epw_8760_from_open_meteo()   : EPW 파일(.epw) 생성
  - calc_supply_temp_series()          : 공급원별 시간별 급수온도 계산

의존성:
  - constants.py (WATER_SOURCE_PARAMS, SUPPLY_TEMP_MIN, OPEN_METEO_URL)
  - 표준 라이브러리: math, typing
  - 외부 패키지  : pandas, requests
"""
import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from constants import WATER_SOURCE_PARAMS, SUPPLY_TEMP_MIN

OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'
OPEN_METEO_ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
OPEN_METEO_FORECAST_DAYS_MAX = 16

OPEN_METEO_HOURLY_VARS = [
    'temperature_2m', 'dew_point_2m', 'relative_humidity_2m',
    'surface_pressure', 'wind_speed_10m', 'wind_direction_10m',
    'cloud_cover', 'shortwave_radiation', 'direct_normal_irradiance',
    'diffuse_radiation', 'precipitation',
    'soil_temperature_54cm',
]


def _local_naive_datetime_index(values) -> pd.DatetimeIndex:
    """Return local wall-clock timestamps without timezone metadata."""
    idx = pd.to_datetime(values)
    if isinstance(idx, pd.Series):
        idx = pd.DatetimeIndex(idx)
    if getattr(idx, 'tz', None) is not None:
        idx = idx.tz_localize(None)
    return pd.DatetimeIndex(idx)


def fetch_open_meteo_hourly_forecast(
    lat: float, lon: float,
    tz: str = 'auto',
    past_days: int = 92,
    forecast_days: int = 16,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    params = {
        'latitude': lat, 'longitude': lon,
        'hourly': ','.join(OPEN_METEO_HOURLY_VARS),
        'timeformat': 'iso8601', 'timezone': tz,
        'wind_speed_unit': 'ms',
        'past_days': max(0, min(92, int(past_days))),
        'forecast_days': max(0, min(16, int(forecast_days))),
    }
    return _fetch_open_meteo(OPEN_METEO_URL, params, lat, lon, tz)


def fetch_open_meteo_hourly_archive(
    lat: float, lon: float,
    start_date: str,
    end_date: str,
    tz: str = 'auto',
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    params = {
        'latitude': lat, 'longitude': lon,
        'hourly': ','.join(OPEN_METEO_HOURLY_VARS),
        'timeformat': 'iso8601', 'timezone': tz,
        'wind_speed_unit': 'ms',
        'start_date': start_date,
        'end_date': end_date,
    }
    return _fetch_open_meteo(OPEN_METEO_ARCHIVE_URL, params, lat, lon, tz)


def _fetch_open_meteo(
    url: str, params: Dict[str, Any], lat: float, lon: float, tz: str
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    h = data['hourly']
    df = pd.DataFrame({
        k: h.get(k, [None] * len(h['time']))
        for k in OPEN_METEO_HOURLY_VARS
    })
    df.index = _local_naive_datetime_index(h['time'])
    df.index.name = 'datetime'
    meta = {
        'latitude': data.get('latitude', lat),
        'longitude': data.get('longitude', lon),
        'elevation': data.get('elevation', 0.0),
        'timezone': data.get('timezone', tz),
        'utc_offset_seconds': data.get('utc_offset_seconds', 0),
        'timezone_abbreviation': data.get('timezone_abbreviation', ''),
    }
    return df, meta


def fetch_open_meteo_weather_for_period(
    lat: float, lon: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    tz: str = 'auto',
    today: Optional[pd.Timestamp] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Fetch [start, end) weather with archive for past and forecast for today/future."""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is not None:
        start = start.tz_localize(None)
    if end.tzinfo is not None:
        end = end.tz_localize(None)
    if end <= start:
        raise ValueError('기상 조회 종료시각은 시작시각보다 뒤여야 합니다.')

    today0 = (
        pd.Timestamp(today).normalize()
        if today is not None else pd.Timestamp.now().normalize()
    )
    forecast_end_exclusive = today0 + pd.Timedelta(days=OPEN_METEO_FORECAST_DAYS_MAX)
    if end > forecast_end_exclusive:
        max_last_day = (forecast_end_exclusive - pd.Timedelta(hours=1)).date()
        raise ValueError(
            f"미래 예측은 오늘부터 {OPEN_METEO_FORECAST_DAYS_MAX}일까지만 지원됩니다. "
            f"지원 가능한 마지막 날짜는 {max_last_day}입니다."
        )

    parts: List[pd.DataFrame] = []
    metas: List[Dict[str, Any]] = []

    def _last_inclusive_date(exclusive_end: pd.Timestamp):
        return (exclusive_end - pd.Timedelta(hours=1)).date()

    if start < today0:
        archive_end = min(end, today0)
        if archive_end > start:
            df_hist, meta_hist = fetch_open_meteo_hourly_archive(
                lat, lon,
                str(start.date()),
                str(_last_inclusive_date(archive_end)),
                tz=tz,
            )
            hist_part = df_hist[(df_hist.index >= start) & (df_hist.index < archive_end)]
            if not hist_part.empty:
                parts.append(hist_part)
            metas.append(meta_hist)

    if end > today0:
        forecast_start = max(start, today0)
        if end > forecast_start:
            last_date = _last_inclusive_date(end)
            forecast_days = (pd.Timestamp(last_date) - today0).days + 1
            df_fcst, meta_fcst = fetch_open_meteo_hourly_forecast(
                lat, lon, tz=tz, past_days=0, forecast_days=forecast_days,
            )
            fcst_part = df_fcst[(df_fcst.index >= forecast_start) & (df_fcst.index < end)]
            if not fcst_part.empty:
                parts.append(fcst_part)
            metas.append(meta_fcst)

    if not parts:
        raise ValueError('요청 기간에 해당하는 기상 데이터가 없습니다.')

    df = pd.concat(parts).sort_index()
    df = df.loc[~df.index.duplicated(keep='last')]
    expected = pd.date_range(start=start, end=end, freq='h', inclusive='left')
    missing = expected.difference(df.index)
    if len(missing) > 0:
        raise ValueError(f'기상 데이터에 누락 시간이 있습니다: {missing[0]}')

    meta = dict(metas[-1])
    if len(metas) > 1:
        meta['source'] = 'archive+forecast'
    elif end <= today0:
        meta['source'] = 'archive'
    else:
        meta['source'] = 'forecast'
    meta['weather_start'] = str(start)
    meta['weather_end'] = str(end)
    return df.reindex(expected), meta


# ============================================================
# EPW 8760h 생성
# ============================================================

def _to_float(x, default=None):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return float(x)
    except Exception:
        return default


def _safe_replace_year(ts: pd.Timestamp, year: int) -> Optional[pd.Timestamp]:
    try:
        return pd.Timestamp(ts).replace(year=year)
    except ValueError:
        return None


def _weekday_matched_replace_year(ts: pd.Timestamp, year: int) -> Optional[pd.Timestamp]:
    """Map a real timestamp to the simulation year while preserving weekday."""
    base = _safe_replace_year(ts, year)
    if base is None:
        return None
    target_dow = pd.Timestamp(ts).dayofweek
    candidates = [base + pd.Timedelta(days=d) for d in range(-3, 4)]
    candidates = [c for c in candidates if c.year == year and c.dayofweek == target_dow]
    if not candidates:
        return base
    return min(candidates, key=lambda c: abs((c - base).days))


def build_epw_year_dataframe(
    df: pd.DataFrame, meta: Dict[str, Any], epw_year: int
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range(f'{epw_year}-01-01 00:00', f'{epw_year}-12-31 23:00', freq='h')
    base = pd.DataFrame(index=idx)
    for c in df.columns:
        base[c] = float('nan')

    mapped = [_weekday_matched_replace_year(ts, epw_year) for ts in df.index]
    dfm = df.copy()
    dfm.index = mapped
    dfm = dfm[~dfm.index.isna()].loc[~dfm.index.duplicated(keep='last')]
    common = [c for c in dfm.columns if c in base.columns]
    base.loc[dfm.index, common] = dfm[common]

    for c in ['temperature_2m','dew_point_2m','relative_humidity_2m',
              'surface_pressure','wind_speed_10m','wind_direction_10m','cloud_cover',
              'soil_temperature_54cm']:
        if c in base.columns:
            base[c] = base[c].ffill().bfill()
    for c in ['shortwave_radiation','direct_normal_irradiance',
              'diffuse_radiation','precipitation']:
        if c in base.columns:
            base[c] = base[c].fillna(0.0)
    return base, dfm


def write_epw_8760_from_open_meteo(
    base: pd.DataFrame, meta: Dict[str, Any], out_path: str,
    compensate_pybui_tz_roll: bool = False,
) -> None:
    tz_h = float(meta.get('utc_offset_seconds', 0)) / 3600.0
    elev = _to_float(meta.get('elevation', 0.0), 0.0)
    epw_base = base
    if compensate_pybui_tz_roll and len(base) > 0:
        # pybuildingenergy rolls every EPW weather column by int(TZ).
        # Open-Meteo already returns local clock-hour data, so pre-shift
        # the EPW rows in the opposite direction to keep HVAC schedules
        # aligned with local time after the engine import step.
        tz_roll = int(tz_h)
        if tz_roll != 0:
            n = len(base)
            offset = tz_roll % n
            values = pd.concat([base.iloc[offset:], base.iloc[:offset]], axis=0)
            epw_base = values.copy()
            epw_base.index = base.index
    first_day_name = pd.Timestamp(epw_base.index[0]).day_name()
    header = [
        f"LOCATION,Forecast,-,-,Open-Meteo,0,"
        f"{meta.get('latitude',0)},{meta.get('longitude',0)},{tz_h},{elev}",
        'DESIGN CONDITIONS,0','TYPICAL/EXTREME PERIODS,0',
        'GROUND TEMPERATURES,0','HOLIDAYS/DAYLIGHT SAVING,No,0,0,0',
        'COMMENTS 1,Generated from Open-Meteo hourly forecast (8760h)',
        f"COMMENTS 2,Timezone={meta.get('timezone','')}",
        f'DATA PERIODS,1,1,Data,{first_day_name},1/1,12/31',
    ]
    rows = []
    for ts, r in epw_base.iterrows():
        hr = int(ts.hour) + 1
        dry = _to_float(r.get('temperature_2m'), 99.9)
        dew = _to_float(r.get('dew_point_2m'), 99.9)
        rh  = _to_float(r.get('relative_humidity_2m'), 999.0)
        ph  = _to_float(r.get('surface_pressure'), None)
        ppa = 999999.0 if ph is None else ph * 100.0
        wd  = _to_float(r.get('wind_direction_10m'), 999.0)
        ws  = _to_float(r.get('wind_speed_10m'), 999.0)
        cl  = _to_float(r.get('cloud_cover'), None)
        tsc = 99 if cl is None else max(0, min(10, int(round(cl/10.0))))
        ghi = max(0.0, _to_float(r.get('shortwave_radiation'), 0.0))
        dni = max(0.0, _to_float(r.get('direct_normal_irradiance'), 0.0))
        dhi = max(0.0, _to_float(r.get('diffuse_radiation'), 0.0))
        prc = _to_float(r.get('precipitation'), 0.0)
        line = [ts.year, ts.month, ts.day, hr, 60, '?',
                dry, dew, rh, ppa,
                9999, 9999, 9999, ghi, dni, dhi,
                999999, 999999, 999999, 9999, wd, ws, tsc, tsc,
                9999, 99999, 9, '999999999', 999, 0.999, 999, 99, 999, prc, 1]
        rows.append(','.join(map(str, line)))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header) + '\n')
        f.write('\n'.join(rows) + '\n')


# ============================================================
# 급수온도 시계열 계산
# ============================================================

def calc_supply_temp_series(
    df_open: pd.DataFrame,
    water_source: str,
) -> Tuple[pd.Series, List[str]]:
    """시간별 급수온도 [°C] + 동파 경고 목록 반환.

    RC 열모델 (지상·탱크):
      T(t) = T(t-1) + (T_air(t) - T(t-1)) × (1/τ) + k_solar × GHI(t)

    지중매설 (soil_temperature_54cm 단일 컬럼 + depth_factor 보정):
      T_mean   = df_open['temperature_2m'].mean()  ← 수집 기간 외기 평균
      T_supply = T_mean + (T_54cm - T_mean) × depth_factor
        ~30cm → depth_factor=1.2 : 54cm보다 외기 변동 약간 크게
        1m+   → depth_factor=0.7 : 54cm보다 연평균에 수렴 (변동 감쇠)

    공통 하한: max(T, 2.0°C)
    """
    sp    = WATER_SOURCE_PARAMS[water_source]
    t_air = df_open['temperature_2m'].ffill().bfill().fillna(15.0)
    ghi   = df_open.get('shortwave_radiation',
                        pd.Series(0.0, index=df_open.index)).fillna(0.0)
    warnings: List[str] = []

    if sp['soil_col'] is not None:
        # ── 지중매설: depth_factor 보정 적용 ──────────────────────────
        col    = sp['soil_col']
        factor = float(sp.get('depth_factor', 1.0))

        t_54 = (df_open[col].ffill().bfill().fillna(t_air)
                if col in df_open.columns else t_air)

        # 연평균 기온으로 기준선(T_mean) 계산
        # → 수집된 기간 전체 평균 사용 (연간 데이터가 없어도 충분한 근사)
        t_mean = float(t_air.mean())

        # depth_factor 보정: 깊을수록 변동 감쇠, 얕을수록 변동 확대
        t_raw  = t_mean + (t_54 - t_mean) * factor
        t_supply = t_raw.clip(lower=SUPPLY_TEMP_MIN)

    else:
        # ── 지상·탱크: RC 열모델 ──────────────────────────────────────
        tau   = float(sp['tau_h'])
        k_s   = float(sp['k_solar'])
        alpha = 1.0 / tau if tau > 0 else 1.0

        t_arr  = t_air.values
        g_arr  = ghi.values
        out    = [0.0] * len(t_arr)
        t_prev = float(t_arr[0])
        for i, (ta, gi) in enumerate(zip(t_arr, g_arr)):
            t_new  = t_prev + (ta - t_prev) * alpha + k_s * float(gi)
            t_new  = max(t_new, SUPPLY_TEMP_MIN)
            out[i] = t_new
            t_prev = t_new
        t_supply = pd.Series(out, index=df_open.index)

        fw = sp['freeze_warn_t']
        if fw is not None:
            n_freeze = int((t_air < fw).sum())
            if n_freeze > 0:
                warnings.append(
                    f"⚠️ 동파 위험: 외기온 {fw}°C 미만 {n_freeze}h "
                    f"({water_source}) — 보온·히팅케이블 조치 필요")

    return t_supply, warnings
