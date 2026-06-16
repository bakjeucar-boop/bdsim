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

OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'


def fetch_open_meteo_hourly_forecast(
    lat: float, lon: float,
    tz: str = 'auto',
    past_days: int = 92,
    forecast_days: int = 16,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:

    hourly_vars = [
        'temperature_2m', 'dew_point_2m', 'relative_humidity_2m',
        'surface_pressure', 'wind_speed_10m', 'wind_direction_10m',
        'cloud_cover', 'shortwave_radiation', 'direct_normal_irradiance',
        'diffuse_radiation', 'precipitation',
        'soil_temperature_54cm',   # ~54cm 토양온도 — 지중매설 급수온도 계산용
        # depth_factor 보정으로 ~30cm(×1.2)·1m+(×0.7) 두 깊이 모두 근사
        # /v1/forecast API 깊이 포인트 방식: 0/6/18/54cm
    ]
    params = {
        'latitude': lat, 'longitude': lon,
        'hourly': ','.join(hourly_vars),
        'timeformat': 'iso8601', 'timezone': tz,
        'wind_speed_unit': 'ms',
        'past_days': max(0, min(92, int(past_days))),
        'forecast_days': max(0, min(16, int(forecast_days))),
    }
    r = requests.get(OPEN_METEO_URL, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    h = data['hourly']
    df = pd.DataFrame({k: h.get(k, [None]*len(h['time'])) for k in hourly_vars})
    df.index = pd.to_datetime(h['time'])
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


def build_epw_year_dataframe(
    df: pd.DataFrame, meta: Dict[str, Any], epw_year: int
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range(f'{epw_year}-01-01 00:00', f'{epw_year}-12-31 23:00', freq='h')
    base = pd.DataFrame(index=idx)
    for c in df.columns:
        base[c] = float('nan')

    mapped = [_safe_replace_year(ts, epw_year) for ts in df.index]
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
    base: pd.DataFrame, meta: Dict[str, Any], out_path: str
) -> None:
    tz_h = float(meta.get('utc_offset_seconds', 0)) / 3600.0
    elev = _to_float(meta.get('elevation', 0.0), 0.0)
    header = [
        f"LOCATION,Forecast,-,-,Open-Meteo,0,"
        f"{meta.get('latitude',0)},{meta.get('longitude',0)},0,{elev}",  # timezone=0: 현지시각 그대로 사용(pybuildingenergy UTC변환 방지)
        'DESIGN CONDITIONS,0','TYPICAL/EXTREME PERIODS,0',
        'GROUND TEMPERATURES,0','HOLIDAYS/DAYLIGHT SAVING,No,0,0,0',
        'COMMENTS 1,Generated from Open-Meteo hourly forecast (8760h)',
        f"COMMENTS 2,Timezone={meta.get('timezone','')}",
        'DATA PERIODS,1,1,Data,Sunday,1/1,12/31',
    ]
    rows = []
    for ts, r in base.iterrows():
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

# ============================================================
# 슬림 EPW 생성 (워밍업 14일 + 예측기간만)
# ============================================================

WARMUP_DAYS = 14   # 실내온도 수렴에 필요한 최소 워밍업 일수


def build_epw_slim(
    df_open: pd.DataFrame,
    meta: Dict[str, Any],
    start_user: pd.Timestamp,
    days: int,
    warmup_days: int = WARMUP_DAYS,
) -> Tuple[pd.DataFrame, pd.Timestamp, int]:
    """워밍업 + 예측기간만 포함하는 최소 EPW DataFrame 생성.

    반환:
      df_slim   : 슬림 EPW 데이터 (warmup_days + days × 24h 행)
      epw_start : EPW 첫 번째 행의 실제 타임스탬프 (워밍업 시작)
      n_hours   : df_slim의 총 행 수 (= (warmup_days + days) × 24)

    EPW 헤더의 DATA PERIODS 를 실제 시작일로 설정하므로
    pybuildingenergy가 올바른 날짜로 인식합니다.

    워밍업 기간이 수집 범위를 벗어나면 가장 오래된 데이터를 반복 패딩합니다.
    """
    epw_start = start_user - pd.Timedelta(days=int(warmup_days))
    epw_end   = start_user + pd.Timedelta(days=int(days))
    slim_idx  = pd.date_range(start=epw_start, end=epw_end, freq='h', inclusive='left')
    n_hours   = len(slim_idx)

    # df_open 범위 확인
    open_min = df_open.index.min()
    open_max = df_open.index.max()

    rows = {}
    for col in df_open.columns:
        ser = df_open[col].copy()
        # 범위 밖은 가장 가까운 경계값으로 채움
        reindexed = ser.reindex(slim_idx, method='nearest', tolerance='1h')
        # 여전히 NaN 남아있으면 ffill/bfill
        if col in ('shortwave_radiation', 'direct_normal_irradiance',
                   'diffuse_radiation', 'precipitation'):
            reindexed = reindexed.fillna(0.0)
        else:
            reindexed = reindexed.ffill().bfill().fillna(15.0)
        rows[col] = reindexed.values

    df_slim = pd.DataFrame(rows, index=slim_idx)
    return df_slim, epw_start, n_hours


def write_epw_slim(
    df_slim: pd.DataFrame,
    epw_start: pd.Timestamp,
    meta: Dict[str, Any],
    out_path: str,
) -> None:
    """슬림 EPW DataFrame을 .epw 파일로 저장.

    DATA PERIODS 헤더를 실제 시작일(epw_start)로 기록하여
    pybuildingenergy가 날짜를 올바르게 인식하도록 합니다.
    행 수가 8760h 미만이어도 정상 동작합니다.
    """
    tz_h   = float(meta.get('utc_offset_seconds', 0)) / 3600.0
    elev   = _to_float(meta.get('elevation', 0.0), 0.0)
    n_rows = len(df_slim)

    _weekday_en = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    day_name    = _weekday_en[epw_start.weekday()]
    end_ts   = df_slim.index[-1]
    dp_start = f"{epw_start.month}/{epw_start.day}"
    dp_end   = f"{end_ts.month}/{end_ts.day}"

    header = [
        f"LOCATION,Forecast,-,-,Open-Meteo,0,"
        f"{meta.get('latitude',0)},{meta.get('longitude',0)},0,{elev}",
        'DESIGN CONDITIONS,0',
        'TYPICAL/EXTREME PERIODS,0',
        'GROUND TEMPERATURES,0',
        'HOLIDAYS/DAYLIGHT SAVING,No,0,0,0',
        f'COMMENTS 1,Slim EPW ({n_rows}h = warmup+forecast) Generated from Open-Meteo',
        f"COMMENTS 2,Timezone={meta.get('timezone','')}",
        f'DATA PERIODS,1,1,Data,{day_name},{dp_start},{dp_end}',
    ]

    rows_out = []
    for ts, r in df_slim.iterrows():
        hr  = int(ts.hour) + 1
        dry = _to_float(r.get('temperature_2m'), 99.9)
        dew = _to_float(r.get('dew_point_2m'), 99.9)
        rh  = _to_float(r.get('relative_humidity_2m'), 999.0)
        ph  = _to_float(r.get('surface_pressure'), None)
        ppa = 999999.0 if ph is None else ph * 100.0
        wd  = _to_float(r.get('wind_direction_10m'), 999.0)
        ws  = _to_float(r.get('wind_speed_10m'), 999.0)
        cl  = _to_float(r.get('cloud_cover'), None)
        tsc = 99 if cl is None else max(0, min(10, int(round(cl / 10.0))))
        ghi = max(0.0, _to_float(r.get('shortwave_radiation'), 0.0))
        dni = max(0.0, _to_float(r.get('direct_normal_irradiance'), 0.0))
        dhi = max(0.0, _to_float(r.get('diffuse_radiation'), 0.0))
        prc = _to_float(r.get('precipitation'), 0.0)
        line = [ts.year, ts.month, ts.day, hr, 60, '?',
                dry, dew, rh, ppa,
                9999, 9999, 9999, ghi, dni, dhi,
                999999, 999999, 999999, 9999, wd, ws, tsc, tsc,
                9999, 99999, 9, '999999999', 999, 0.999, 999, 99, 999, prc, 1]
        rows_out.append(','.join(map(str, line)))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header) + '\n')
        f.write('\n'.join(rows_out) + '\n')
