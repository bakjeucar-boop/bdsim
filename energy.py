#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""energy.py — 건물 에너지 분석 모듈

포함 기능:
  [BUI / 시뮬레이션]
  - build_bui()                    : pybuildingenergy BUI 딕셔너리 생성
  - run_iso52016_simulation()      : ISO 52016 시뮬레이션 실행 → 시간별 Q_H/Q_C
  - extract_user_period()          : 사용자 기간 추출 (연도 경계·윤일 처리)

  [HP COP 보정]
  - hp_cop_heating() / hp_cop_cooling() / hp_cop_dhw()
    ASHRAE/IEA 기반 외기온 선형 보정 (겨울 성능 저하 반영)

  [침기 보정]
  - compute_wind_corrected_infil_ach()  : 75th 백분위 풍속 기반 침기 ACH 보정
  - compute_effective_infil_ach()       : 기계환기 포함 유효 ACH 계산

  [전력 환산]
  - calc_electricity_year()        : Q_H/Q_C → 전기소비 + 조명/콘센트/팬/온수
  - calc_dhw_elec_series()         : 온수 전기소비 시계열

  [식당 스케줄]
  - make_restaurant_gains_profile() : 식사 선택 기반 24h 발열 프로파일
  - get_restaurant_hvac_times()    : 식사 선택 기반 HVAC 시작·종료

  [헬퍼]
  - parse_dt(), estimate_persons(), _fv(), _iv()

의존성:
  - constants.py (PRESETS, ISO18523_PROFILE, DHW_*, WATER_SOURCE_*, DORM_HVAC_BASE_RATIO 등)
  - 표준 라이브러리: math, typing
  - 외부 패키지  : pandas
"""
import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from constants import (
    ISO18523_PROFILE, DHW_HEATER_DEFAULTS, DHW_FACILITY_PARAMS,
    DORM_HVAC_BASE_RATIO, WATER_SOURCE_PARAMS,
    _SH_PROF, _KT_PROF,
    WEEKEND_MODES, OCCUPANT_DENSITY,
    get_iso18523_schedule,
)

# ============================================================
# BUI 빌더
# ============================================================

def make_onoff_profile(start_h: int, end_h: int,
                       on: float = 1.0, off: float = 0.0) -> list:
    prof = [off] * 24
    s, e = max(0,min(23,int(start_h))), max(0,min(23,int(end_h)))
    if s > e: s, e = e, s
    for h in range(s, e+1):
        prof[h] = on
    return prof


def _weekend_mode_to_profile(mode: str, weekday_prof: list) -> list:
    """주말 운영 모드를 24h 0/1 프로파일로 변환.

    '없음(OFF)'       → 전부 0 (free-float)
    '오전 운영(06~13h)' → 06~13h = 1, 나머지 0
    '평일 동일'        → weekday_prof 그대로 복사
    """
    if mode == '평일 동일':
        return list(weekday_prof)
    elif mode == '오전 운영(06~13h)':
        return make_onoff_profile(6, 13, on=1, off=0)
    else:  # '없음(OFF)'
        return [0] * 24


def _weekend_mode_to_occ(mode: str, weekday_occ: list) -> list:
    """주말 운영 모드를 내부발열 비율 프로파일로 변환."""
    if mode == '평일 동일':
        return list(weekday_occ)
    elif mode == '오전 운영(06~13h)':
        # 오전 운영 시 오전 피크 비율은 평일의 70% 수준
        am_val = max(weekday_occ[8:14]) * 0.7 if any(weekday_occ[8:14]) else 0.5
        return make_onoff_profile(6, 13, on=am_val, off=0.0)
    else:
        return [0.0] * 24


def make_restaurant_gains_profile(
    meal_bfst: bool, meal_lunch: bool, meal_dinner: bool
) -> list:
    """선택된 식사에 따른 식당 24h 내부발열 비율 프로파일.

    각 식사의 준비·서비스·정리 시간에 맞춘 피크 배분.
    ISO 18523-1 Annex D 식당 스케줄 기반.
    기저: 냉장고·조명 등 상시 부하 0.10
    """
    prof = [0.10] * 24   # 기저 (냉장고·항시 가동 장비)

    if meal_bfst:
        # 준비 05h, 서비스 06~09h, 정리 09h
        prof[5]  = max(prof[5],  0.60)
        prof[6]  = max(prof[6],  0.90)
        prof[7]  = max(prof[7],  1.00)
        prof[8]  = max(prof[8],  0.95)
        prof[9]  = max(prof[9],  0.50)

    if meal_lunch:
        # 준비 10h, 서비스 11~14h, 정리 14h
        prof[10] = max(prof[10], 0.70)
        prof[11] = max(prof[11], 0.95)
        prof[12] = max(prof[12], 1.00)
        prof[13] = max(prof[13], 0.95)
        prof[14] = max(prof[14], 0.55)

    if meal_dinner:
        # 준비 16h, 서비스 17~21h, 정리 21h
        prof[16] = max(prof[16], 0.70)
        prof[17] = max(prof[17], 0.90)
        prof[18] = max(prof[18], 1.00)
        prof[19] = max(prof[19], 0.95)
        prof[20] = max(prof[20], 0.85)
        prof[21] = max(prof[21], 0.45)

    return prof


def get_restaurant_hvac_times(
    meal_bfst: bool, meal_lunch: bool, meal_dinner: bool
) -> tuple:
    """선택된 식사에 따른 HVAC 시작·종료 시간.

    첫 식사 준비 1h 전 ~ 마지막 식사 종료 1h 후
    """
    starts, ends = [], []
    if meal_bfst:   starts.append(4);  ends.append(10)   # 05h 준비 → 1h 전=04h
    if meal_lunch:  starts.append(9);  ends.append(15)   # 10h 준비 → 1h 전=09h
    if meal_dinner: starts.append(15); ends.append(22)   # 16h 준비 → 1h 전=15h
    if not starts:
        return 5, 23   # 아무것도 선택 안 했으면 전체 운영 (fallback)
    return min(starts), max(ends)




def build_bui(
    name: str, lat: float, lon: float,
    area_m2: float, n_floors: int, floor_height_m: float,
    wwr: float, azimuth_deg: float, aspect_ratio: float,
    roof_u: float, wall_u: float, slab_u: float,
    win_u: float, win_g: float,
    heat_set: float, cool_set: float,
    hvac_start: int, hvac_end: int,
    gains_start: int, gains_end: int,
    internal_gain_heat_wm2: float, lighting_wm2: float,
    infil_ach: float,
    sat_mode: str = '없음(OFF)',
    sun_mode: str = '없음(OFF)',
    occupancy_ratio: float = 1.0,
    restaurant_gains_prof: Optional[list] = None,
    use_type: str = '사무실',
) -> Dict[str, Any]:
    """BUI 딕셔너리 생성.

    building_type_class='Office' 고정 (라이브러리 제약).

    sat_mode / sun_mode: 토·일 각각 '없음(OFF)' / '오전 운영(06~13h)' / '평일 동일'
      → 두 모드를 평균한 weekend 프로파일을 BUI에 입력
      (pybuildingenergy는 weekday/weekend 두 종류만 지원)
    restaurant_gains_prof: 식당 식사 선택 기반 24h 커스텀 발열 프로파일 (None이면 표준 on/off)
    """
    n_floors = max(1, int(n_floors))
    area_m2  = float(area_m2)
    apf      = area_m2 / n_floors
    ar       = max(1.0, float(aspect_ratio))

    W = math.sqrt(apf / ar)
    L = W * ar
    H = n_floors * float(floor_height_m)
    perimeter = 2.0 * (L + W)

    wall_gross = {'S': L*H, 'N': L*H, 'E': W*H, 'W': W*H}
    wwr = max(0.0, min(0.95, float(wwr)))

    base_az = {'S': 180.0, 'N': 0.0, 'E': 90.0, 'W': 270.0}
    def raz(face):
        return (base_az[face] + (float(azimuth_deg) - 180.0)) % 360.0

    # ── 평일 프로파일 ──────────────────────────────────────────
    hp_wd  = make_onoff_profile(hvac_start, hvac_end, on=1, off=0)
    occ_val = max(0.0, min(1.0, float(occupancy_ratio)))

    if restaurant_gains_prof is not None:
        # 식당 식사 선택 기반 커스텀 발열 프로파일
        occ_wd = [v * occ_val for v in restaurant_gains_prof]
    else:
        occ_wd = make_onoff_profile(gains_start, gains_end, on=occ_val, off=0.0)

    # ── 주말 프로파일 — 토/일 평균 ────────────────────────────
    # pybuildingenergy는 weekday/weekend 2종만 지원하므로
    # 토요일·일요일 각각의 프로파일을 평균내어 weekend에 입력
    hp_sat  = _weekend_mode_to_profile(sat_mode, hp_wd)
    hp_sun  = _weekend_mode_to_profile(sun_mode, hp_wd)
    hp_we   = [0.5*(s+u) for s, u in zip(hp_sat, hp_sun)]

    occ_sat = _weekend_mode_to_occ(sat_mode, occ_wd)
    occ_sun = _weekend_mode_to_occ(sun_mode, occ_wd)
    occ_we  = [0.5*(s+u) for s, u in zip(occ_sat, occ_sun)]

    gains = [
        {"name":"occupants",  "full_load":0.0,
         "weekday":occ_wd, "weekend":occ_we},
        {"name":"appliances", "full_load":float(internal_gain_heat_wm2),
         "weekday":occ_wd, "weekend":occ_we},
        {"name":"lighting",   "full_load":float(lighting_wm2),
         "weekday":occ_wd, "weekend":occ_we},
    ]

    walls = []
    for face in ['S','N','E','W']:
        g  = wall_gross[face]
        az = raz(face)
        walls.append({"name":f"Wall_{face}","type":"opaque",
                      "area":float(g*(1-wwr)),"u_value":float(wall_u),
                      "solar_absorptance":0.5,"thermal_capacity":80000,
                      "orientation":{"azimuth":az,"tilt":90},
                      "sky_view_factor":0.5,"name_adj_zone":None})
        walls.append({"name":f"Win_{face}","type":"transparent",
                      "area":float(g*wwr),"u_value":float(win_u),
                      "g_value":float(win_g),
                      "orientation":{"azimuth":az,"tilt":90},
                      "sky_view_factor":0.5,"shading":False,"name_adj_zone":None})

    return {
        "building":{
            "name":name,"latitude":float(lat),"longitude":float(lon),
            "azimuth_relative_to_true_north":float(azimuth_deg),
            "exposed_perimeter":perimeter,"height":H,
            "wall_thickness":0.15,"n_floors":int(n_floors),
            "building_type_class":"Office",   # 라이브러리 제약
            "net_floor_area":float(area_m2),"treated_floor_area":float(area_m2),
            "construction_class":"class_i","adj_zones_present":False,
        },
        "adj_zones_present":False,"adjacent_zones":[],
        "building_surface":[
            {"name":"Roof","type":"opaque","area":float(apf),
             "u_value":float(roof_u),"solar_absorptance":0.4,
             "thermal_capacity":40000,"orientation":{"azimuth":0,"tilt":0},
             "sky_view_factor":1.0,"name_adj_zone":None},
            *walls,
            {"name":"Slab to ground","type":"opaque","area":float(apf),
             "u_value":float(slab_u),"solar_absorptance":0.6,
             "thermal_capacity":150000,"orientation":{"azimuth":0,"tilt":0},
             "sky_view_factor":0.0,"name_adj_zone":None},
        ],
        "building_parameters":{
            "temperature_setpoints":{
                # 숙소: setback 유지 (24h 거주, 야간 최소 온도 유지)
                # 비주거: 극단값 → HVAC 외 시간 완전 free-floating
                "heating_setpoint": float(heat_set),
                "heating_setback":  float(heat_set)-3.0 if use_type=='숙소' else -50.0,
                "cooling_setpoint": float(cool_set),
                "cooling_setback":  float(cool_set)+4.0 if use_type=='숙소' else 100.0,
            },
            "system_capacities":{
                # ISO 52016: 에너지 소요량 계산 — 설비 용량 무제한(1e6 W)
                # 항상 설정온도 유지 가정. 실제 설비 용량은 결과에서 별도 확인.
                "heating_capacity":1e6,"cooling_capacity":1e6,
            },
            "internal_gains":gains,
            "airflow_rates":{"infiltration_rate":float(infil_ach)},
            "heating_profile":{"weekday":hp_wd,"weekend":hp_we},
            "cooling_profile":{"weekday":hp_wd,"weekend":hp_we},
            "construction":{"wall_thickness":0.15,"thermal_bridges":0.0},
        },
    }


# ============================================================
# 히트펌프 COP 외기온 보정  (수정 ②)
# ============================================================
# 근거: ASHRAE 기초 핸드북, IEA Heat Pump Centre, EN 14511
# 난방: 기준점 7°C (ASHRAE rated condition), 이하 2.5%/°C 감소
# 냉방: 기준점 25°C (ASHRAE rated condition), 이상 2.0%/°C 감소
# 온수HP: 기준점 15°C, 이하 3.0%/°C 감소 (증발기 열원 = 외기)
# 하한: COP_min=1.0 (물리적 한계)

def hp_cop_heating(cop_rated: float, t_out: float) -> float:
    """공기열원 히트펌프 난방 COP 보정.
    t_out < 7°C 일 때 2.5%/°C 감소. COP 하한 1.0.
    """
    penalty = max(0.0, 7.0 - float(t_out)) * 0.025
    return max(1.0, float(cop_rated) * (1.0 - penalty))


def hp_cop_cooling(cop_rated: float, t_out: float) -> float:
    """공기열원 히트펌프 냉방 COP 보정.
    t_out > 25°C 일 때 2.0%/°C 감소. COP 하한 1.0.
    """
    penalty = max(0.0, float(t_out) - 25.0) * 0.020
    return max(1.0, float(cop_rated) * (1.0 - penalty))


def hp_cop_dhw(cop_rated: float, t_out: float) -> float:
    """온수 히트펌프 COP 보정.
    t_out < 15°C 일 때 3.0%/°C 감소. COP 하한 1.0.
    """
    penalty = max(0.0, 15.0 - float(t_out)) * 0.030
    return max(1.0, float(cop_rated) * (1.0 - penalty))


# ============================================================
# 침기 풍속 보정  (수정 ③)
# ============================================================
# 근거: ASHRAE 기초 핸드북 Sherman-Grimsrud 단순화 모델
# 유효 ACH(t) = base_ACH × (1 + 0.10 × (v(t) - v_ref))
# v_ref = 3 m/s (설계 기준 풍속)
# 정확도 목표: 예측 기간 75th 백분위 풍속 사용 → ±3~5% 오차
# 75th 백분위: 평균 대비 높은 쪽 하한 → 보수적 추정 (과소 예측 방지)

def compute_wind_corrected_infil_ach(
    base_ach: float,
    df_open: pd.DataFrame,
    start_user: Optional[pd.Timestamp] = None,
    days: int = 7,
    v_ref: float = 3.0,
    k_wind: float = 0.10,
) -> Tuple[float, float]:
    """풍속 75th 백분위 기반 침기 보정 ACH 반환.

    Returns:
        (보정된 ACH, 사용된 75th 백분위 풍속)
    """
    ws = df_open.get('wind_speed_10m', pd.Series(dtype=float))
    if ws.empty or ws.isna().all():
        return float(base_ach), float(v_ref)

    # 예측 기간 내 데이터만 사용 (있을 경우)
    if start_user is not None:
        end_user = start_user + pd.Timedelta(days=int(days))
        mask = (ws.index >= start_user) & (ws.index < end_user)
        ws_period = ws[mask]
        if len(ws_period) < 10:
            ws_period = ws  # 데이터 부족 시 전체 사용
    else:
        ws_period = ws

    v75 = float(ws_period.quantile(0.75))
    correction = 1.0 + k_wind * max(0.0, v75 - v_ref)
    return float(base_ach) * correction, v75



def get_load_schedule(use_type: str, load_type: str, hour: int,
                      gains_start: int = 8, gains_end: int = 18) -> float:
    """ISO 18523 기반 시간별 부하 비율.

    ISO 18523 프로파일이 있으면 해당 값 사용.
    없으면 기존 단순 스케줄(재실시간 1.0, 전후 2h 0.2, 심야 0.05) fallback.

    gains_start/gains_end는 프로파일이 없을 때만 사용.
    ISO 18523 프로파일이 있으면 해당 표준값 그대로 적용.
    """
    return get_iso18523_schedule(use_type, load_type, hour)


def calc_dhw_elec_series(
    index: pd.DatetimeIndex,
    t_supply: pd.Series,
    facility_type: str,
    heater_type: str,
    persons: int,
    shower_lpd: float, shower_t_hot: float,
    kitchen_lpd: float, kitchen_t_hot: float,
    dhw_cop: float,
) -> Tuple[pd.Series, pd.Series]:
    """온수 전기소비[kWh] + 열량[kWh_열] 시간 시계열.

    Q_day[Wh] = persons × lpd × 1.163 [Wh/L·K] × max(0, T_hot - T_supply)
    is_electric=False → 전기=0, 열량만 반환 (가스·외부공급)
    """
    hd = DHW_HEATER_DEFAULTS.get(heater_type, DHW_HEATER_DEFAULTS['없음'])
    is_elec = hd['is_electric']
    fp = DHW_FACILITY_PARAMS.get(facility_type, DHW_FACILITY_PARAMS['없음'])
    if persons <= 0 or (fp['shower_lpd'] == 0 and fp['kitchen_lpd'] == 0):
        z = pd.Series(0.0, index=index)
        return z, z

    t_arr = t_supply.reindex(index, method='nearest').fillna(15.0).values
    elec, heat = [], []
    for i, ts in enumerate(index):
        t_s = float(t_arr[i])
        q = 0.0
        if shower_lpd > 0:
            dt = max(0.0, float(shower_t_hot) - t_s)
            q += float(persons) * float(shower_lpd) * 1.163 * dt
        if kitchen_lpd > 0:
            dt = max(0.0, float(kitchen_t_hot) - t_s)
            q += float(persons) * float(kitchen_lpd) * 1.163 * dt
            # 샤워와 주방 프로파일 분리 적용
            if shower_lpd > 0:
                # 이미 합산됨 — 실제로는 샤워와 주방 각각 배분
                pass
        # 프로파일 배분
        q_sh = 0.0
        q_kt = 0.0
        if shower_lpd > 0:
            dt = max(0.0, float(shower_t_hot) - t_s)
            q_sh = float(persons)*float(shower_lpd)*1.163*dt * _SH_PROF[ts.hour]
        if kitchen_lpd > 0:
            dt = max(0.0, float(kitchen_t_hot) - t_s)
            q_kt = float(persons)*float(kitchen_lpd)*1.163*dt * _KT_PROF[ts.hour]
        q_total_kwh = (q_sh + q_kt) / 1000.0

        if is_elec and dhw_cop > 0:
            elec.append(q_total_kwh / float(dhw_cop))
        else:
            elec.append(0.0)
        heat.append(q_total_kwh)

    return pd.Series(elec, index=index), pd.Series(heat, index=index)


def calc_electricity_year(
    df_sim: pd.DataFrame,
    cop_h: float, cop_c: float,
    area_m2: float,
    lighting_wm2: float, equip_elec_wm2: float,
    gains_start: int, gains_end: int,
    supply_oa_m3h: float, kitchen_exh_m3h: float,
    vent_start: int, vent_end: int,
    fan_sp: float,
    t_supply: pd.Series,
    facility_type: str, heater_type: str,
    persons: int,
    shower_lpd: float, shower_t_hot: float,
    kitchen_lpd: float, kitchen_t_hot: float,
    dhw_cop: float,
    use_type: str = '사무실',
    occupancy_ratio: float = 1.0,
    df_open: Optional[pd.DataFrame] = None,
    sat_mode: str = '없음(OFF)',
    sun_mode: str = '없음(OFF)',
    restaurant_gains_prof: Optional[list] = None,
) -> pd.DataFrame:
    """전기 소비량 계산.

    HP COP 외기온 보정 / ISO 18523 24h 프로파일 / 요일별 스케줄 적용.
    """
    if 'Q_H' not in df_sim.columns or 'Q_C' not in df_sim.columns:
        raise KeyError(f"Q_H/Q_C 없음. 컬럼: {list(df_sim.columns)}")

    out = df_sim.copy()

    # ── HVAC 전기 — HP COP 시간별 보정 ──────────────────────────
    is_hp_hvac = (cop_h > 1.5 or cop_c > 1.5)
    if is_hp_hvac and df_open is not None:
        t_out_yr = df_open['temperature_2m'].reindex(
            out.index, method='nearest').fillna(10.0)
        hvac_elec = []
        for i, ts in enumerate(out.index):
            t_o = float(t_out_yr.iloc[i])
            qh  = float(out['Q_H'].iloc[i])
            qc  = float(out['Q_C'].iloc[i])
            hvac_elec.append((qh / hp_cop_heating(cop_h, t_o) +
                               abs(qc) / hp_cop_cooling(cop_c, t_o)) / 1000.0)
        hvac_raw = pd.Series(hvac_elec, index=out.index)
    else:
        hvac_raw = (out['Q_H'] / float(cop_h) +
                    out['Q_C'].abs() / float(cop_c)) / 1000.0

    # 숙소 공실 보정
    if use_type == '숙소' and 0.0 < float(occupancy_ratio) < 1.0:
        beta = DORM_HVAC_BASE_RATIO
        hvac_raw = hvac_raw * (beta + (1 - beta) * float(occupancy_ratio))
    out['HVAC_Elec_kWh'] = hvac_raw

    # ── 조명·콘센트 — 요일별 + ISO 18523 / 식당 식사 프로파일 ────
    def _day_scale(ts) -> float:
        """요일별 운영 계수: 평일=1.0, 토·일은 mode에 따라 결정."""
        dow = ts.dayofweek  # 0=월 … 4=금, 5=토, 6=일
        if dow == 5:
            mode = sat_mode
        elif dow == 6:
            mode = sun_mode
        else:
            return 1.0
        if mode == '평일 동일':
            return 1.0
        elif mode == '오전 운영(06~13h)':
            return 1.0 if 6 <= ts.hour <= 13 else 0.0
        else:  # '없음(OFF)'
            return 0.0

    if restaurant_gains_prof is not None:
        out['Lighting_Elec_kWh'] = [
            restaurant_gains_prof[ts.hour] * _day_scale(ts)
            * float(lighting_wm2) * float(area_m2) / 1000.0
            for ts in out.index
        ]
        out['Equip_Elec_kWh'] = [
            restaurant_gains_prof[ts.hour] * _day_scale(ts)
            * float(equip_elec_wm2) * float(area_m2) / 1000.0
            for ts in out.index
        ]
    else:
        out['Lighting_Elec_kWh'] = [
            get_load_schedule(use_type, '조명', int(ts.hour), gains_start, gains_end)
            * _day_scale(ts) * float(lighting_wm2) * float(area_m2) / 1000.0
            for ts in out.index
        ]
        out['Equip_Elec_kWh'] = [
            get_load_schedule(use_type, '콘센트', int(ts.hour), gains_start, gains_end)
            * _day_scale(ts) * float(equip_elec_wm2) * float(area_m2) / 1000.0
            for ts in out.index
        ]

    # ── 환기팬 ────────────────────────────────────────────────────
    vp   = make_onoff_profile(vent_start, vent_end, on=1.0, off=0.0)
    flow = (float(supply_oa_m3h) + float(kitchen_exh_m3h)) / 3600.0
    out['Fan_Elec_kWh'] = [
        float(fan_sp) * flow * vp[int(ts.hour)] / 1000.0
        for ts in out.index
    ]

    # ── 온수 — HP COP 시간별 보정 ─────────────────────────────────
    # 히트펌프 온수기인 경우 dhw_cop도 외기온으로 보정
    is_hp_dhw = (dhw_cop > 1.5)
    if is_hp_dhw and df_open is not None:
        t_out_yr = df_open['temperature_2m'].reindex(
            out.index, method='nearest').fillna(10.0)
        dhw_e_list, dhw_h_list = [], []
        for i, ts in enumerate(out.index):
            t_o   = float(t_out_yr.iloc[i])
            t_s   = float(t_supply.reindex([ts], method='nearest').fillna(15.0).iloc[0])
            q_sh  = 0.0
            q_kt  = 0.0
            if shower_lpd > 0:
                dt = max(0.0, float(shower_t_hot) - t_s)
                q_sh = float(persons) * float(shower_lpd) * 1.163 * dt * _SH_PROF[ts.hour]
            if kitchen_lpd > 0:
                dt = max(0.0, float(kitchen_t_hot) - t_s)
                q_kt = float(persons) * float(kitchen_lpd) * 1.163 * dt * _KT_PROF[ts.hour]
            q_kwh = (q_sh + q_kt) / 1000.0
            cop_t = hp_cop_dhw(dhw_cop, t_o)
            hd = DHW_HEATER_DEFAULTS.get(heater_type, DHW_HEATER_DEFAULTS['없음'])
            if hd['is_electric'] and cop_t > 0:
                dhw_e_list.append(q_kwh / cop_t)
            else:
                dhw_e_list.append(0.0)
            dhw_h_list.append(q_kwh)
        out['DHW_Elec_kWh'] = dhw_e_list
        out['DHW_Heat_kWh'] = dhw_h_list
    else:
        dhw_e, dhw_h = calc_dhw_elec_series(
            out.index, t_supply, facility_type, heater_type,
            persons, shower_lpd, shower_t_hot, kitchen_lpd, kitchen_t_hot, dhw_cop,
        )
        out['DHW_Elec_kWh'] = dhw_e.values
        out['DHW_Heat_kWh'] = dhw_h.values

    out['Total_Elec_kWh'] = (
        out['HVAC_Elec_kWh'] + out['Lighting_Elec_kWh'] +
        out['Equip_Elec_kWh'] + out['Fan_Elec_kWh'] + out['DHW_Elec_kWh']
    )
    out['Total_Power_kW']    = out['Total_Elec_kWh']
    out['HVAC_Power_kW']     = out['HVAC_Elec_kWh']
    out['Lighting_Power_kW'] = out['Lighting_Elec_kWh']
    out['Equip_Power_kW']    = out['Equip_Elec_kWh']
    out['Fan_Power_kW']      = out['Fan_Elec_kWh']
    out['DHW_Power_kW']      = out['DHW_Elec_kWh']
    return out


# ============================================================
# 시뮬레이션 실행
# ============================================================

def run_iso52016_simulation(
    bui: Dict[str,Any], epw_path: str, epw_year: int
) -> pd.DataFrame:
    import pybuildingenergy as pybui
    bui_c, _ = pybui.sanitize_and_validate_BUI(bui, fix=True)
    hs, _    = pybui.ISO52016.Temperature_and_Energy_needs_calculation(
        bui_c, weather_source='epw',
        weather_file=epw_path, path_weather_file=epw_path,
    )
    if not isinstance(hs, pd.DataFrame):
        hs = pd.DataFrame(hs)
    if len(hs) == 9504:
        hs = hs.iloc[:8760].copy()
    if not isinstance(hs.index, pd.DatetimeIndex):
        hs.index = pd.date_range(f'{epw_year}-01-01 00:00', periods=len(hs), freq='h')
    return hs


# ============================================================
# 윤일 보간 + 기간 추출
# ============================================================

def _get_weath(df, ts):
    try:
        r = df.loc[pd.Timestamp(ts)]
        return _to_float(r.get('temperature_2m'),None), _to_float(r.get('shortwave_radiation'),None)
    except Exception:
        return None, None


def weight_from_weather(t28,g28,t29,g29,t01,g01,ag=0.01):
    if None in (t28,t29,t01): return 0.5
    d28 = abs(t29-t28)+(ag*abs(g29-g28) if None not in (g28,g29,g01) else 0)
    d01 = abs(t29-t01)+(ag*abs(g29-g01) if None not in (g28,g29,g01) else 0)
    return max(0., min(1., 1.-d28/(d28+d01+1e-6)))


def extract_user_period(df_year, df_open, start_user, days, sim_year):
    """사용자 기간에 해당하는 시뮬레이션 결과를 추출.

    [수정 ①] 연도 경계(12/xx ~ 1/xx) 지원:
    - 연도가 바뀌어도 각 시각을 sim_year의 동일 월·일로 매핑하여 추출
    - 12/29~1/03 예시: 12/29~12/31은 2009년 12월, 1/01~1/03은 2009년 1월에서 각각 추출
    - 두 구간을 이어 붙여 반환 (단일 DataFrame, 원래 날짜 인덱스)
    - 2/29(윤일): 2/28·3/1 결과를 기상 유사도 가중 보간
    - 테스트 방법: 예측 시작을 12월 말로 설정하고 7일 이상 예측 시 오류 없이 진행되면 정상
    """
    end_user   = start_user + pd.Timedelta(days=int(days))
    user_index = pd.date_range(start=start_user, end=end_user, freq='h', inclusive='left')
    rows = []
    for ts in user_index:
        if ts.month == 2 and ts.day == 29:
            s28 = ts.replace(month=2, day=28, year=sim_year)
            s01 = ts.replace(month=3, day=1,  year=sim_year)
            v28, v01 = df_year.loc[s28], df_year.loc[s01]
            t28, g28 = _get_weath(df_open, ts.replace(month=2, day=28))
            t29, g29 = _get_weath(df_open, ts)
            t01, g01 = _get_weath(df_open, ts.replace(month=3, day=1))
            w = weight_from_weather(t28, g28, t29, g29, t01, g01)
            rows.append(v28 * w + v01 * (1.0 - w))
        else:
            # 연도 경계를 넘어도 sim_year의 동일 월·일로 매핑
            ts_sim = ts.replace(year=sim_year)
            rows.append(df_year.loc[ts_sim])
    return pd.DataFrame(rows, index=user_index)


# ============================================================
# 헬퍼
# ============================================================

def parse_dt(s: str) -> pd.Timestamp:
    s = (s or '').strip().replace('T',' ')
    if len(s.split()) == 1: s += ' 00:00'
    elif len(s.split()) == 2 and len(s.split()[1]) == 2: s += ':00'
    ts = pd.to_datetime(s)
    if pd.isna(ts): raise ValueError('날짜 형식 오류')
    return pd.Timestamp(ts)


def compute_effective_infil_ach(area, floor_h, base, oa, kex, vs, ve,
                               df_open=None, start_user=None, days=7):
    """유효 침기 ACH 계산.

    [수정 ③] 풍속 75th 백분위 보정 적용:
    - df_open이 있으면 예측 기간 wind_speed_10m 75th 백분위로 침기 보정
    - 보정 공식: ACH_eff = base_ACH × (1 + 0.10 × max(0, v75 - 3.0))
    - 기계환기 풍량(oa, kex) 추가분도 포함
    Returns: (유효 ACH, v75 풍속)
    """
    # 풍속 보정
    if df_open is not None:
        corrected_base, v75 = compute_wind_corrected_infil_ach(
            base, df_open, start_user, days)
    else:
        corrected_base, v75 = float(base), 3.0

    vol = float(area) * float(floor_h)
    if vol <= 0:
        return corrected_base, v75
    on_f = sum(make_onoff_profile(vs, ve, on=1., off=0.)) / 24.
    total = corrected_base + (float(oa) + float(kex)) * on_f / vol
    return total, v75


def estimate_persons(area_m2: float, use_type: str) -> int:
    d = OCCUPANT_DENSITY.get(use_type, 10.0)
    return max(1, int(round(float(area_m2)/d)))


def _fv(v, fb=0.0):
    """안전한 float 변환."""
    try:
        return float(str(v).replace('—','0').replace('—','0') or fb)
    except Exception:
        return float(fb)


def _iv(v, fb=0):
    try:
        return int(float(str(v) or fb))
    except Exception:
        return int(fb)

def run_iso52016_simulation_slim(
    bui: Dict[str, Any],
    epw_path: str,
    epw_start: 'pd.Timestamp',
    warmup_days: int,
    forecast_days: int,
) -> 'pd.DataFrame':
    """슬림 EPW(워밍업+예측기간)로 ISO 52016 시뮬레이션 실행.

    pybuildingenergy가 EPW 헤더의 DATA PERIODS를 읽어 날짜를 인식하므로,
    write_epw_slim()이 생성한 헤더와 반드시 쌍으로 사용해야 합니다.

    반환:
      df_forecast : 예측 기간(forecast_days × 24h)만 잘라낸 DataFrame
                    인덱스는 실제 예측 날짜·시각

    워밍업 기간을 버리고 예측 기간만 반환하므로
    extract_user_period() 호출이 불필요합니다.
    """
    import pybuildingenergy as pybui

    bui_c, _ = pybui.sanitize_and_validate_BUI(bui, fix=True)
    hs, _    = pybui.ISO52016.Temperature_and_Energy_needs_calculation(
        bui_c,
        weather_source='epw',
        weather_file=epw_path,
        path_weather_file=epw_path,
    )
    if not isinstance(hs, pd.DataFrame):
        hs = pd.DataFrame(hs)

    # 시뮬레이션 결과에 실제 날짜 인덱스 부여
    total_hours = (warmup_days + forecast_days) * 24
    if len(hs) >= total_hours:
        hs = hs.iloc[:total_hours].copy()
    real_idx = pd.date_range(
        start=epw_start,
        periods=len(hs),
        freq='h',
    )
    hs.index = real_idx

    # 워밍업 기간 제거 → 예측 기간만 반환
    forecast_start = epw_start + pd.Timedelta(days=int(warmup_days))
    df_forecast    = hs.loc[hs.index >= forecast_start].copy()
    return df_forecast


# ============================================================
# 단일 건물 전체 파이프라인 (병렬 처리 단위)
# ============================================================

def _simulate_one_building(args: tuple) -> tuple:
    """단일 건물 시뮬레이션 — ProcessPoolExecutor 작업 단위.

    pybuildingenergy는 항상 8760h 결과를 반환하므로
    슬림 EPW는 사용하지 않고 8760h EPW를 공유합니다.
    계산 후 extract_user_period()로 예측 기간만 추출합니다.

    Returns:
      성공: (name, df_out, infil_eff, v75, persons, occ_ratio)
      실패: (name, Exception, traceback_str)
    """
    (bp, epw_path, epw_year,
     t_supply_values, t_supply_index,
     df_open_values, df_open_index, df_open_cols,
     start_user_str, days, lat, lon) = args

    try:
        import pandas as pd
        from energy import (
            make_restaurant_gains_profile, get_restaurant_hvac_times,
            build_bui, run_iso52016_simulation, extract_user_period,
            calc_electricity_year, estimate_persons,
            compute_effective_infil_ach,
        )
        from constants import DHW_FACILITY_PARAMS

        # pandas 객체 재구성 (pickle 우회)
        t_supply   = pd.Series(t_supply_values,
                               index=pd.DatetimeIndex(t_supply_index))
        df_open    = pd.DataFrame(df_open_values,
                                  index=pd.DatetimeIndex(df_open_index),
                                  columns=df_open_cols)
        start_user = pd.Timestamp(start_user_str)

        nm = bp['bldg_name']
        ut = bp['use_type']

        # 식당 식사 스케줄
        restaurant_gains_prof = None
        hvac_start  = bp['hvac_start']
        hvac_end    = bp['hvac_end']
        gains_start = bp['gains_start']
        gains_end   = bp['gains_end']
        if ut == '식당':
            restaurant_gains_prof = make_restaurant_gains_profile(
                bp['meal_bfst'], bp['meal_lunch'], bp['meal_dinner'])
            hvac_start, hvac_end = get_restaurant_hvac_times(
                bp['meal_bfst'], bp['meal_lunch'], bp['meal_dinner'])
            gains_start = min(hvac_start + 1, 23)
            gains_end   = max(hvac_end - 1, 0)

        # 인원 / 공실 보정
        area_m2     = bp['area_m2']
        dhw_persons = bp['dhw_persons']
        if dhw_persons <= 0:
            dhw_persons = estimate_persons(area_m2, ut)
        occ_ratio = min(1.0, dhw_persons / max(1, estimate_persons(area_m2, ut)))

        # 침기 보정
        infil_eff, v75 = compute_effective_infil_ach(
            area_m2, bp['floor_h'], bp['infil_base'],
            bp['oa_m3h'], bp['kitchen_exh'],
            bp['vent_start'], bp['vent_end'],
            df_open=df_open, start_user=start_user, days=days,
        )

        # BUI 생성
        bui = build_bui(
            name=f"{nm}_{ut}", lat=lat, lon=lon,
            area_m2=area_m2, n_floors=bp['floors'],
            floor_height_m=bp['floor_h'],
            wwr=bp['wwr'], azimuth_deg=bp['azimuth_deg'],
            aspect_ratio=bp['aspect_ratio'],
            roof_u=bp['roof_u'], wall_u=bp['wall_u'],
            slab_u=bp['slab_u'], win_u=bp['win_u'], win_g=bp['win_g'],
            heat_set=bp['heat_set'], cool_set=bp['cool_set'],
            hvac_start=hvac_start, hvac_end=hvac_end,
            gains_start=gains_start, gains_end=gains_end,
            internal_gain_heat_wm2=bp['gain_heat'],
            lighting_wm2=bp['lighting_wm2'],
            infil_ach=infil_eff,
            sat_mode=bp['sat_mode'],
            sun_mode=bp['sun_mode'],
            occupancy_ratio=occ_ratio,
            restaurant_gains_prof=restaurant_gains_prof,
            use_type=ut,
        )

        # ISO 52016 시뮬레이션 (8760h)
        hs_year  = run_iso52016_simulation(bui, epw_path, epw_year)
        sim_year = int(hs_year.index.min().year)

        # 전력 환산 (8760h)
        fp = DHW_FACILITY_PARAMS.get(bp['dhw_facility'], DHW_FACILITY_PARAMS['없음'])
        df_year = calc_electricity_year(
            hs_year,
            cop_h=bp['cop_h'], cop_c=bp['cop_c'],
            area_m2=area_m2,
            lighting_wm2=bp['lighting_wm2'],
            equip_elec_wm2=bp['equip_elec_wm2'],
            gains_start=gains_start, gains_end=gains_end,
            supply_oa_m3h=bp['oa_m3h'],
            kitchen_exh_m3h=bp['kitchen_exh'],
            vent_start=bp['vent_start'], vent_end=bp['vent_end'],
            fan_sp=bp['fan_sp'],
            t_supply=t_supply,
            facility_type=bp['dhw_facility'],
            heater_type=bp['dhw_heater_type'],
            persons=dhw_persons,
            shower_lpd=fp['shower_lpd'],
            shower_t_hot=bp['dhw_t_hot_shower'],
            kitchen_lpd=fp['kitchen_lpd'],
            kitchen_t_hot=bp['dhw_t_hot_kitchen'],
            dhw_cop=bp['dhw_cop'],
            use_type=ut, occupancy_ratio=occ_ratio,
            df_open=df_open,
            sat_mode=bp['sat_mode'],
            sun_mode=bp['sun_mode'],
            restaurant_gains_prof=restaurant_gains_prof,
        )

        # 예측 기간 추출
        df_out = extract_user_period(df_year, df_open, start_user, days, sim_year)
        return (nm, df_out, infil_eff, v75, dhw_persons, occ_ratio)

    except Exception as e:
        import traceback
        return (bp.get('bldg_name', '?'), e, traceback.format_exc())


def run_buildings_parallel(
    buildings: list,
    epw_path: str,
    epw_year: int,
    t_supply: 'pd.Series',
    df_open: 'pd.DataFrame',
    start_user: 'pd.Timestamp',
    days: int,
    lat: float,
    lon: float,
    max_workers: int = None,
) -> tuple:
    """모든 건물을 병렬로 시뮬레이션.

    pybuildingenergy는 항상 8760h를 반환하므로 8760h EPW를 사용합니다.
    EPW 파일 경로를 각 프로세스에 전달하여 공유합니다.
    pandas 객체는 values/index로 분해하여 pickle 문제를 우회합니다.

    Returns:
      results_ok  : {bldg_name: df_out}
      results_err : {bldg_name: (Exception, traceback_str)}
      infos       : {bldg_name: {infil_eff, v75, persons, occ_ratio}}
    """
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    if max_workers is None:
        max_workers = min(len(buildings), os.cpu_count() or 1)

    # pandas 직렬화 (pickle 우회)
    t_supply_values = t_supply.values.tolist()
    t_supply_index  = [str(x) for x in t_supply.index]
    df_open_values  = df_open.values.tolist()
    df_open_index   = [str(x) for x in df_open.index]
    df_open_cols    = list(df_open.columns)
    start_user_str  = str(start_user)

    args_list = [
        (bp, epw_path, epw_year,
         t_supply_values, t_supply_index,
         df_open_values, df_open_index, df_open_cols,
         start_user_str, days, lat, lon)
        for bp in buildings
    ]

    results_ok  = {}
    results_err = {}
    infos       = {}

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(_simulate_one_building, a): a[0]['bldg_name']
            for a in args_list
        }
        for future in as_completed(future_to_name):
            ret = future.result()
            nm  = ret[0]
            if isinstance(ret[1], Exception):
                results_err[nm] = (ret[1], ret[2] if len(ret) > 2 else '')
            else:
                _, df_out, infil_eff, v75, persons, occ_ratio = ret
                results_ok[nm] = df_out
                infos[nm] = {
                    'infil_eff': infil_eff,
                    'v75':       v75,
                    'persons':   persons,
                    'occ_ratio': occ_ratio,
                }

    return results_ok, results_err, infos
