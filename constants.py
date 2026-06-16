#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""constants.py — 가설건물 에너지 예측 시스템 상수/프로파일 모듈

포함 내용:
  - 용도 프리셋 (UsePreset, PRESETS)
  - ISO 18523-1/2 기반 24h 부하 프로파일 (ISO18523_PROFILE)
  - 온수기 상수 (DHW_HEATER_TYPES, DHW_HEATER_DEFAULTS, DHW_FACILITY_*)
  - 급수공급원 상수 (WATER_SOURCE_TYPES, WATER_SOURCE_PARAMS, SUPPLY_TEMP_MIN)
  - 주말/식사 옵션 (WEEKEND_MODES, MEAL_WINDOWS)
  - 용도별 인원밀도 (OCCUPANT_DENSITY)
  - 숙소 HVAC 기저부하 비율 (DORM_HVAC_BASE_RATIO)
"""
from dataclasses import dataclass
from typing import Dict, List

USE_TYPES = ['사무실', '식당', '작업장', '숙소']

# 용도별 인원밀도 (m²/인) — 자동 추정용
# 출처: ISO 18523-1:2016 Annex D (비주거), ISO 18523-2:2018 (주거)
OCCUPANT_DENSITY: Dict[str, float] = {
    '사무실': 10.0,   # ISO 18523-1 Annex D: 10 m²/person (개방형 사무실)
    '숙소':    8.0,   # ISO 18523-2: 8 m²/person (기숙사·다인실)
    '식당':    2.0,   # ISO 18523-1 Annex D: 1.4~2.0 m²/person (좌석 기준)
    '작업장': 15.0,   # ISO 18523-1 Annex D: 10~20 m²/person (중공업 기준)
}

# 주말 운영 모드 (토·일 각각 독립 선택)
WEEKEND_MODES = ['없음(OFF)', '오전 운영(06~13h)', '평일 동일']

# 식당 식사 제공 시간대 (준비 시작h, 서비스 종료h, 피크 시간 리스트)
MEAL_WINDOWS: Dict[str, tuple] = {
    '조식': (5, 9,   [6, 7, 8]),       # 05h 준비, 06~09h 서비스
    '중식': (10, 14, [11, 12, 13]),    # 10h 준비, 11~14h 서비스
    '석식': (16, 21, [17, 18, 19, 20]), # 16h 준비, 17~21h 서비스
}

# 숙소 HVAC 기저부하 비율 β
# 공실 시에도 유지되는 최소 냉난방 비율
# E_adj = E_raw × (β + (1-β) × 재실비율)
DORM_HVAC_BASE_RATIO = 0.45

# ============================================================
# ISO 18523-1/2 기반 24시간 부하 프로파일
# ============================================================
# 출처: ISO 18523-1:2016 Annex D (비주거), ISO 18523-2:2018 Annex C (주거)
# 값: 0.0~1.0 비율 (1.0 = 피크 부하의 100%)
# 조명·콘센트 전기부하 스케줄에 적용 (피크 W/m² × 비율)
# 재실 스케줄(냉난방 내부발열 비율)에도 동일 프로파일 사용

# 사무실 (ISO 18523-1 Annex D Table D.2)
# 특징: 9~18h 업무 피크, 점심(12~13h) 감소, 야간 최저
ISO18523_PROFILE: Dict[str, List[float]] = {
    '사무실_조명': [
        0.05, 0.05, 0.05, 0.05, 0.05, 0.10,   # 0~5h
        0.20, 0.50, 0.90, 0.95, 0.95, 0.95,   # 6~11h
        0.60, 0.95, 0.95, 0.95, 0.95, 0.80,   # 12~17h
        0.40, 0.20, 0.10, 0.10, 0.05, 0.05,   # 18~23h
    ],
    '사무실_콘센트': [
        0.05, 0.05, 0.05, 0.05, 0.05, 0.05,   # 0~5h
        0.10, 0.30, 0.90, 0.95, 0.95, 0.90,   # 6~11h
        0.70, 0.95, 0.95, 0.95, 0.90, 0.70,   # 12~17h
        0.30, 0.15, 0.10, 0.10, 0.05, 0.05,   # 18~23h
    ],
    # 식당 (ISO 18523-1 Annex D: 3식 운영 기준)
    # 특징: 아침(6~9h), 점심(11~14h), 저녁(17~21h) 피크
    '식당_조명': [
        0.05, 0.05, 0.05, 0.05, 0.05, 0.20,   # 0~5h
        0.70, 0.90, 0.80, 0.50, 0.40, 0.90,   # 6~11h
        0.95, 0.80, 0.50, 0.40, 0.60, 0.90,   # 12~17h
        0.95, 0.90, 0.70, 0.40, 0.20, 0.10,   # 18~23h
    ],
    '식당_콘센트': [
        0.05, 0.05, 0.05, 0.05, 0.10, 0.40,   # 0~5h (새벽 냉장고 등)
        0.80, 0.95, 0.80, 0.50, 0.40, 0.90,   # 6~11h
        0.95, 0.85, 0.50, 0.40, 0.70, 0.95,   # 12~17h
        0.95, 0.85, 0.60, 0.30, 0.20, 0.10,   # 18~23h
    ],
    # 작업장 (ISO 18523-1 Annex D: 경공업/중공업 기준)
    # 특징: 작업 시간(7~18h) 균등 피크, 점심 소폭 감소
    '작업장_조명': [
        0.05, 0.05, 0.05, 0.05, 0.05, 0.10,   # 0~5h
        0.30, 0.90, 0.95, 0.95, 0.95, 0.90,   # 6~11h
        0.70, 0.90, 0.95, 0.95, 0.95, 0.80,   # 12~17h
        0.30, 0.10, 0.05, 0.05, 0.05, 0.05,   # 18~23h
    ],
    '작업장_콘센트': [
        0.05, 0.05, 0.05, 0.05, 0.05, 0.10,   # 0~5h
        0.30, 0.85, 0.95, 0.95, 0.95, 0.90,   # 6~11h
        0.70, 0.90, 0.95, 0.95, 0.90, 0.70,   # 12~17h
        0.20, 0.10, 0.05, 0.05, 0.05, 0.05,   # 18~23h
    ],
    # 숙소 (ISO 18523-2:2018 Annex C)
    # 특징: 기상 전(6~8h) + 귀사 후(18~23h) 피크, 낮 최저
    '숙소_조명': [
        0.30, 0.20, 0.15, 0.10, 0.10, 0.30,   # 0~5h
        0.70, 0.90, 0.60, 0.30, 0.20, 0.20,   # 6~11h
        0.20, 0.20, 0.20, 0.20, 0.25, 0.70,   # 12~17h
        0.90, 0.90, 0.80, 0.70, 0.60, 0.40,   # 18~23h
    ],
    '숙소_콘센트': [
        0.40, 0.30, 0.20, 0.15, 0.15, 0.30,   # 0~5h  (충전기·TV 야간)
        0.60, 0.80, 0.50, 0.30, 0.25, 0.25,   # 6~11h
        0.25, 0.25, 0.25, 0.25, 0.30, 0.70,   # 12~17h
        0.90, 0.95, 0.90, 0.80, 0.70, 0.55,   # 18~23h
    ],
}


def get_iso18523_schedule(use_type: str, load_type: str, hour: int) -> float:
    """ISO 18523 기반 시간별 부하 비율 반환.

    Args:
        use_type : '사무실' / '식당' / '작업장' / '숙소'
        load_type: '조명' / '콘센트'
        hour     : 0~23
    Returns:
        0.0~1.0 비율
    """
    key = f'{use_type}_{load_type}'
    prof = ISO18523_PROFILE.get(key)
    if prof is None:
        # fallback: 기존 단순 스케줄
        return 1.0 if 8 <= hour <= 18 else (0.2 if 6 <= hour < 8 or 18 < hour <= 20 else 0.05)
    return prof[int(hour) % 24]


@dataclass
class UsePreset:
    hvac_start: int
    hvac_end:   int
    gains_start: int
    gains_end:   int
    heat_set:  float
    cool_set:  float
    internal_gain_heat_wm2: float   # 인체+기기 열 [W/m²] → 냉난방 열부하
    lighting_wm2:  float            # 조명 [W/m²] → 냉난방+전기
    equip_elec_wm2: float           # 콘센트 [W/m²] → 전기만
    base_infil_ach: float
    oa_m3h:         float
    kitchen_exh_m3h: float
    # 주말 운영 모드 (토·일 독립)
    sat_mode: str   # '없음(OFF)' / '오전 운영(06~13h)' / '평일 동일'
    sun_mode: str   # 동일 선택지
    # 식당 식사 제공 여부 (식당 용도에서만 사용)
    meal_bfst:  bool    # 조식 제공 여부
    meal_lunch: bool    # 중식 제공 여부
    meal_dinner: bool   # 석식 제공 여부
    default_dhw_facility: str       # 온수 시설 기본값
    default_dhw_heater:   str       # 온수기 종류 기본값


PRESETS: Dict[str, UsePreset] = {
    # ── 사무실 ──────────────────────────────────────────────────
    # ISO 18523-1:2016 Annex D Table D.2 기준
    '사무실': UsePreset(
        hvac_start=7,  hvac_end=19,
        gains_start=8, gains_end=18,
        heat_set=20.0, cool_set=26.0,
        internal_gain_heat_wm2=15.0, lighting_wm2=10.0, equip_elec_wm2=8.0,
        base_infil_ach=0.7, oa_m3h=0.0, kitchen_exh_m3h=0.0,
        sat_mode='없음(OFF)', sun_mode='없음(OFF)',
        meal_bfst=False, meal_lunch=False, meal_dinner=False,
        default_dhw_facility='세면', default_dhw_heater='없음',
    ),
    # ── 숙소 ────────────────────────────────────────────────────
    # ISO 18523-2:2018 Annex C 기준 / 24h 7일 연속 운영
    '숙소': UsePreset(
        hvac_start=0,  hvac_end=23,
        gains_start=0, gains_end=23,
        heat_set=20.0, cool_set=26.0,
        internal_gain_heat_wm2=10.0, lighting_wm2=8.0, equip_elec_wm2=5.0,
        base_infil_ach=0.7, oa_m3h=0.0, kitchen_exh_m3h=0.0,
        sat_mode='평일 동일', sun_mode='평일 동일',
        meal_bfst=False, meal_lunch=False, meal_dinner=False,
        default_dhw_facility='샤워', default_dhw_heater='전기저항식(저장)',
    ),
    # ── 작업장 ──────────────────────────────────────────────────
    # ISO 18523-1:2016 Annex D 경공업/중공업 기준
    '작업장': UsePreset(
        hvac_start=5,  hvac_end=21,
        gains_start=6, gains_end=20,
        heat_set=18.0, cool_set=28.0,
        internal_gain_heat_wm2=20.0, lighting_wm2=8.0, equip_elec_wm2=8.0,
        base_infil_ach=1.0, oa_m3h=0.0, kitchen_exh_m3h=0.0,
        sat_mode='없음(OFF)', sun_mode='없음(OFF)',
        meal_bfst=False, meal_lunch=False, meal_dinner=False,
        default_dhw_facility='샤워', default_dhw_heater='전기저항식(순간)',
    ),
    # ── 식당 ────────────────────────────────────────────────────
    # ISO 18523-1:2016 Annex D 레스토랑/구내식당 기준
    # 기본: 3식 모두 제공 (조식·중식·석식 체크)
    '식당': UsePreset(
        hvac_start=5,  hvac_end=23,   # 3식 기준 (meal 선택에 따라 자동 재계산)
        gains_start=6, gains_end=22,
        heat_set=20.0, cool_set=26.0,
        internal_gain_heat_wm2=30.0, lighting_wm2=12.0, equip_elec_wm2=10.0,
        base_infil_ach=1.0, oa_m3h=0.0, kitchen_exh_m3h=500.0,
        sat_mode='평일 동일', sun_mode='평일 동일',
        meal_bfst=True, meal_lunch=True, meal_dinner=True,
        default_dhw_facility='주방', default_dhw_heater='전기저항식(저장)',
    ),
}


# ============================================================
# 온수기 상수
# ============================================================

DHW_HEATER_TYPES = [
    '전기저항식(저장)', '전기저항식(순간)', '히트펌프',
    '가스온수기', '외부공급', '없음',
]

DHW_HEATER_DEFAULTS: Dict[str, Dict] = {
    '전기저항식(저장)': {
        'cop': 0.93, 't_hot_shower': 60.0, 't_hot_kitchen': 80.0,
        'is_electric': True,
        'note': '저장식 60°C 유지 — 레지오넬라균 위생 기준 (WHO)',
    },
    '전기저항식(순간)': {
        'cop': 0.95, 't_hot_shower': 45.0, 't_hot_kitchen': 80.0,
        'is_electric': True,
        'note': '순간식 — 저장 불필요, 사용점 온도 45°C 권장',
    },
    '히트펌프': {
        'cop': 3.0,  't_hot_shower': 55.0, 't_hot_kitchen': 80.0,
        'is_electric': True,
        'note': '히트펌프 — 고효율 (COP 2.5~4.0), 겨울 성능 저하 주의',
    },
    '가스온수기': {
        'cop': 0.88, 't_hot_shower': 45.0, 't_hot_kitchen': 80.0,
        'is_electric': False,
        'note': '가스 — 전기소비=0, 가스 열량만 참고 출력 (효율 0.85~0.92)',
    },
    '외부공급': {
        'cop': 0.0,  't_hot_shower': 60.0, 't_hot_kitchen': 80.0,
        'is_electric': False,
        'note': '지역난방 등 외부 공급 — 전기소비=0',
    },
    '없음': {
        'cop': 0.0,  't_hot_shower': 0.0, 't_hot_kitchen': 0.0,
        'is_electric': False,
        'note': '온수 없음 — 계산 제외',
    },
}

DHW_FACILITY_TYPES = ['세면', '샤워', '주방', '주방+샤워']

# 시설 유형별 온수 사용량 파라미터
# shower_lpd: L/인·일,  kitchen_lpd: L/인·일
DHW_FACILITY_PARAMS: Dict[str, Dict] = {
    '세면':      {'shower_lpd': 10.0, 'kitchen_lpd': 0.0},
    '샤워':      {'shower_lpd': 60.0, 'kitchen_lpd': 0.0},
    '주방':      {'shower_lpd': 0.0,  'kitchen_lpd': 10.0},
    '주방+샤워': {'shower_lpd': 60.0, 'kitchen_lpd': 10.0},
    # 이전 저장값 호환
    '없음':      {'shower_lpd': 0.0,  'kitchen_lpd': 0.0},
    '샤워·세면': {'shower_lpd': 60.0, 'kitchen_lpd': 0.0},
    '샤워+주방': {'shower_lpd': 60.0, 'kitchen_lpd': 10.0},
}

# 온수 사용 시간 프로필 (합=1.0)
_DHW_PROFILE_SHOWER = [
    0.005,0.005,0.005,0.005,0.005,0.010,  # 0~5h
    0.060,0.100,0.080,0.020,0.015,0.015,  # 6~11h
    0.020,0.015,0.015,0.015,0.015,0.060,  # 12~17h
    0.080,0.100,0.090,0.070,0.050,0.030,  # 18~23h
]
_DHW_PROFILE_KITCHEN = [
    0.000,0.000,0.000,0.000,0.000,0.000,  # 0~5h
    0.010,0.050,0.100,0.080,0.020,0.100,  # 6~11h
    0.150,0.080,0.020,0.020,0.020,0.100,  # 12~17h
    0.120,0.080,0.030,0.020,0.000,0.000,  # 18~23h
]

def _norm(p: List[float]) -> List[float]:
    s = sum(p)
    return [v/s for v in p] if s > 0 else p

_SH_PROF = _norm(_DHW_PROFILE_SHOWER)
_KT_PROF = _norm(_DHW_PROFILE_KITCHEN)


# ============================================================
# 급수공급원 상수
# ============================================================

WATER_SOURCE_TYPES = [
    '지상 노출배관',
    '옥상탱크 (무단열)',
    '옥상탱크 (단열)',
    '지중매설 (~30cm)',
    '지중매설 (1m+)',
]

# tau_h         : RC 열모델 시간상수 [h]  (0 = 즉시 추종)
# k_solar       : 일사 보정 [°C per W/m²]
# freeze_warn_t : 동파 경고 임계 외기온 [°C]  (None = 경고 없음)
# soil_col      : None이면 RC모델, 문자열이면 토양온도 컬럼명 직접 사용
# depth_factor  : 토양온도 보정 계수 (1.0=54cm 그대로)
#   산출 공식: T_supply = T_mean + (T_54cm - T_mean) × depth_factor
#   ~30cm: 1.2 → 54cm보다 외기 변동 약간 크게 (얕을수록 외기 영향↑)
#   1m+  : 0.7 → 54cm보다 변동 감쇠 (깊을수록 연평균에 수렴)
#   수집 변수는 soil_temperature_54cm 하나만 사용 (API 요청 간소화)
WATER_SOURCE_PARAMS: Dict[str, Dict] = {
    '지상 노출배관':    {'tau_h': 1.0,  'k_solar': 0.003, 'freeze_warn_t':  2.0, 'soil_col': None,                    'depth_factor': 1.0},
    '옥상탱크 (무단열)':{'tau_h': 4.0,  'k_solar': 0.008, 'freeze_warn_t':  0.0, 'soil_col': None,                    'depth_factor': 1.0},
    '옥상탱크 (단열)':  {'tau_h': 12.0, 'k_solar': 0.003, 'freeze_warn_t': -5.0, 'soil_col': None,                    'depth_factor': 1.0},
    '지중매설 (~30cm)': {'tau_h': 0.0,  'k_solar': 0.0,   'freeze_warn_t': None, 'soil_col': 'soil_temperature_54cm', 'depth_factor': 1.2},
    '지중매설 (1m+)':   {'tau_h': 0.0,  'k_solar': 0.0,   'freeze_warn_t': None, 'soil_col': 'soil_temperature_54cm', 'depth_factor': 0.7},
}

SUPPLY_TEMP_MIN = 2.0  # 급수온도 하한 [°C]


# ============================================================
# Open-Meteo 수집
# ============================================================



