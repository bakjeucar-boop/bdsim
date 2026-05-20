#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.py — 가설건물 전력 예측 시스템 (Streamlit 버전)"""

import io
import os
import tempfile
from typing import Any, Dict, List, Optional

import openpyxl
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# 내부 모듈 임포트
from constants import (
    DHW_FACILITY_PARAMS,
    DHW_HEATER_DEFAULTS,
    DHW_HEATER_TYPES,
    ISO18523_PROFILE,
    PRESETS,
    USE_TYPES,
    WATER_SOURCE_PARAMS,
    WEEKEND_MODES,
)
from energy import (
    _fv,
    _iv,
    build_bui,
    calc_electricity_year,
    compute_effective_infil_ach,
    estimate_persons,
    extract_user_period,
    get_restaurant_hvac_times,
    make_restaurant_gains_profile,
    parse_dt,
    run_iso52016_simulation,
)
from weather import (
    _safe_replace_year,
    build_epw_year_dataframe,
    calc_supply_temp_series,
    fetch_open_meteo_hourly_forecast,
    write_epw_8760_from_open_meteo,
)

# --- 0. Streamlit 설정 및 최적화 ---
st.set_page_config(
    page_title="Forecast Energy — 가설건물 전력 예측",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Open-Meteo API 호출 캐싱 (UI 재실행 시 중복 요청 방지)
@st.cache_data(ttl=3600)
def cached_fetch_weather(lat: float, lon: float):
    return fetch_open_meteo_hourly_forecast(lat, lon)


# --- 1. 세션 상태 (Session State) 초기화 ---
if "buildings" not in st.session_state:
    st.session_state.buildings = []
if "last_result" not in st.session_state:
    st.session_state.last_result = None
if "edit_idx" not in st.session_state:
    st.session_state.edit_idx = None

# --- 2. 헬퍼 함수: Excel 다운로드용 Buffer 생성 ---


def generate_excel_buffer(result: Dict[str, Any]) -> io.BytesIO:
    """결과 데이터를 openpyxl을 사용하여 메모리 내 Excel 파일로 생성"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    bldg_results: Dict[str, pd.DataFrame] = result["bldg_results"]
    df_total: pd.DataFrame = result["df_total"]

    HDR_FILL = PatternFill("solid", start_color="1F4E79")
    HDR_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=10)
    SUB_FILL = PatternFill("solid", start_color="BDD7EE")
    SUB_FONT = Font(bold=True, name="Arial", size=10)
    BODY_FONT = Font(name="Arial", size=9)
    NUM_FMT = "#,##0.00"
    AL_C = Alignment(horizontal="center")
    AL_R = Alignment(horizontal="right")

    def hdr(ws, row, col, text, fill=HDR_FILL, font=HDR_FONT):
        c = ws.cell(row=row, column=col, value=text)
        c.fill = fill
        c.font = font
        c.alignment = AL_C
        return c

    def num(ws, row, col, val):
        c = ws.cell(row=row, column=col, value=round(float(val), 3))
        c.font = BODY_FONT
        c.number_format = NUM_FMT
        c.alignment = AL_R

    # 시트 1: 통합 시간별
    ws1 = wb.active
    ws1.title = "통합_시간별"
    bldg_names = list(bldg_results.keys())

    hdr(ws1, 1, 1, "날짜·시각")
    ws1.column_dimensions["A"].width = 18
    col = 2
    for nm in bldg_names:
        hdr(ws1, 1, col, nm)
        ws1.column_dimensions[get_column_letter(col)].width = 14
        col += 1
    hdr(ws1, 1, col, "현장 합계 [kWh]")
    ws1.column_dimensions[get_column_letter(col)].width = 16

    row = 2
    for ts, tr in df_total.iterrows():
        ws1.cell(row=row, column=1, value=str(ts)).font = BODY_FONT
        col = 2
        for nm in bldg_names:
            df_b = bldg_results[nm]
            val = (
                float(df_b.loc[ts, "Total_Elec_kWh"])
                if ts in df_b.index
                else 0.0
            )
            num(ws1, row, col, val)
            col += 1
        num(ws1, row, col, float(tr["Total_Elec_kWh"]))
        row += 1

    # 시트 2: 건물별 시간별 상세
    elec_cols = [
        "HVAC_Elec_kWh",
        "Lighting_Elec_kWh",
        "Equip_Elec_kWh",
        "Fan_Elec_kWh",
        "DHW_Elec_kWh",
        "Total_Elec_kWh",
    ]
    sub_cols = ["HVAC", "조명", "콘센트", "팬", "온수", "합계"]
    for nm, df_b in bldg_results.items():
        ws = wb.create_sheet(title=(nm[:28] if len(nm) > 28 else nm))
        hdr(
            ws,
            1,
            1,
            f"{nm} — 시간별 전력량 [kWh]",
            fill=PatternFill("solid", start_color="1F6B75"),
        )
        ws.merge_cells(
            start_row=1, start_column=1, end_row=1, end_column=7
        )
        ws.column_dimensions["A"].width = 18
        hdr(ws, 2, 1, "날짜·시각", fill=SUB_FILL, font=SUB_FONT)
        for i, col_nm in enumerate(sub_cols, 2):
            hdr(ws, 2, i, col_nm, fill=SUB_FILL, font=SUB_FONT)
            ws.column_dimensions[get_column_letter(i)].width = 12
        row = 3
        for ts, r in df_b.iterrows():
            ws.cell(row=row, column=1, value=str(ts)).font = BODY_FONT
            for i, ec in enumerate(elec_cols, 2):
                num(ws, row, i, r.get(ec, 0.0))
            row += 1

    # 시트 3: 일별 요약
    ws3 = wb.create_sheet(title="일별_요약")
    hdr(ws3, 1, 1, "날짜")
    ws3.column_dimensions["A"].width = 14
    col = 2
    for nm in bldg_names:
        hdr(ws3, 1, col, nm)
        ws3.column_dimensions[get_column_letter(col)].width = 13
        col += 1
    hdr(ws3, 1, col, "현장 합계 [kWh]")
    ws3.column_dimensions[get_column_letter(col)].width = 16

    daily_total = df_total["Total_Elec_kWh"].resample("D").sum()
    row = 2
    for date, total_kwh in daily_total.items():
        ws3.cell(row=row, column=1, value=str(date.date())).font = BODY_FONT
        col = 2
        for nm in bldg_names:
            df_b = bldg_results[nm]
            daily_b = float(
                df_b["Total_Elec_kWh"]
                .resample("D")
                .sum()
                .get(date, 0.0)
            )
            num(ws3, row, col, daily_b)
            col += 1
        num(ws3, row, col, total_kwh)
        row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --- 3. 사이드바 (현장 공통 설정) ---
with st.sidebar:
    st.header("📍 현장 공통 설정")
    site_lat = st.number_input(
        "위도", value=37.5665, format="%.6f", help="현장의 위도"
    )
    site_lon = st.number_input(
        "경도", value=126.9780, format="%.6f", help="현장의 경도"
    )

    site_start = st.date_input(
        "예측 시작일", value=pd.Timestamp.now().date()
    )
    site_start_time = st.time_input(
        "예측 시작 시간", value=pd.Timestamp.now().floor("H").time()
    )
    start_user = pd.Timestamp.combine(site_start, site_start_time)

    site_days = st.slider("예측 일수", min_value=3, max_value=16, value=7)

    water_source = st.selectbox(
        "급수 공급원",
        options=list(WATER_SOURCE_PARAMS.keys()),
        index=1,  # 옥상탱크(무단열)
    )

# --- 4. 메인 화면 레이아웃 ---
st.title("📊 Forecast Energy v3.0 (Streamlit)")
st.caption(
    "공사현장 가설건물 전력 사용량 및 피크 부하 예측 시스템 — ISO 52016-1 물리 엔진 기반"
)

tab_input, tab_list, tab_result = st.tabs(
    ["🏢 건물 정보 입력", "📋 계산 대상 목록", "📈 시뮬레이션 결과"]
)

# --- 탭 1: 건물 정보 입력 ---
with tab_input:
    st.subheader("새로운 건물 추가 또는 수정")

    # 초기값 세팅을 위한 Use Preset 매핑
    use_type = st.selectbox("용도", options=USE_TYPES, index=0)
    preset = PRESETS[use_type]

    # 기본값 설정 (수정 모드 여부에 따라 분기)
    if st.session_state.edit_idx is not None:
        eb = st.session_state.buildings[st.session_state.edit_idx]
    else:
        eb = {}

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("##### 📐 기본 건축 정보")
        bldg_name = st.text_input(
            "건물명", value=eb.get("bldg_name", f"건물_{len(st.session_state.buildings)+1}")
        )
        area_m2 = st.number_input(
            "기준면적 [m²]",
            value=float(eb.get("area_m2", 800.0)),
            step=50.0,
        )
        floors = st.number_input(
            "층수", value=int(eb.get("floors", 2)), min_value=1
        )
        floor_h = st.number_input(
            "층고 [m]", value=float(eb.get("floor_h", 3.5)), step=0.1
        )
        wwr = st.number_input(
            "창면적비 (WWR) [-]",
            value=float(eb.get("wwr", 0.20)),
            min_value=0.0,
            max_value=0.95,
            step=0.05,
        )
        azimuth_deg = st.number_input(
            "방위각 [°]", value=float(eb.get("azimuth_deg", 180.0)), step=10.0
        )
        aspect_ratio = st.number_input(
            "장변/단변 비율",
            value=float(eb.get("aspect_ratio", 1.5)),
            min_value=1.0,
            step=0.1,
        )

    with col2:
        st.markdown("##### ⚙️ 냉난방 및 운영")
        heat_set = st.number_input(
            "난방 설정 [°C]",
            value=float(eb.get("heat_set", preset.heat_set)),
            step=0.5,
        )
        cool_set = st.number_input(
            "냉방 설정 [°C]",
            value=float(eb.get("cool_set", preset.cool_set)),
            step=0.5,
        )

        # 식당의 경우 HVAC 시간을 자동으로 연산하므로 Disable 처리 가능
        hvac_start = st.number_input(
            "HVAC 시작 [h]",
            value=int(eb.get("hvac_start", preset.hvac_start)),
            min_value=0,
            max_value=23,
        )
        hvac_end = st.number_input(
            "HVAC 종료 [h]",
            value=int(eb.get("hvac_end", preset.hvac_end)),
            min_value=0,
            max_value=23,
        )

        sat_mode = st.selectbox(
            "토요일 운영",
            options=WEEKEND_MODES,
            index=WEEKEND_MODES.index(
                eb.get("sat_mode", preset.sat_mode)
            ),
        )
        sun_mode = st.selectbox(
            "일요일 운영",
            options=WEEKEND_MODES,
            index=WEEKEND_MODES.index(
                eb.get("sun_mode", preset.sun_mode)
            ),
        )

    with col3:
        st.markdown("##### 🚰 온수 및 환기")
        dhw_facility = st.selectbox(
            "온수 시설 유형",
            options=list(DHW_FACILITY_PARAMS.keys()),
            index=list(DHW_FACILITY_PARAMS.keys()).index(
                eb.get("dhw_facility", preset.default_dhw_facility)
            ),
        )
        dhw_heater_type = st.selectbox(
            "온수기 종류",
            options=DHW_HEATER_TYPES,
            index=DHW_HEATER_TYPES.index(
                eb.get("dhw_heater_type", preset.default_dhw_heater)
            ),
        )

        est_persons = estimate_persons(area_m2, use_type)
        dhw_persons = st.number_input(
            f"재실 인원 (0 입력시 자동추정: {est_persons}명)",
            value=int(eb.get("dhw_persons", 0)),
            min_value=0,
        )

        oa_m3h = st.number_input(
            "외기공급량 [m³/h]", value=float(eb.get("oa_m3h", preset.oa_m3h))
        )

        # 식당 전용 후드 배기 설정
        if use_type == "식당":
            kitchen_exh = st.number_input(
                "주방 후드 배기 [m³/h]",
                value=float(
                    eb.get("kitchen_exh", preset.kitchen_exh_m3h)
                ),
            )
            meal_bfst = st.checkbox("조식 제공 (05~09h)", value=True)
            meal_lunch = st.checkbox("중식 제공 (10~14h)", value=True)
            meal_dinner = st.checkbox("석식 제공 (16~21h)", value=True)
        else:
            kitchen_exh = 0.0
            meal_bfst, meal_lunch, meal_dinner = False, False, False

    # 고급 설정: 접이식 패널 (Expanders)
    with st.expander("🛠️ 고급 설정 (부하 밀도 및 외피 정보)"):
        ae_col1, ae_col2 = st.columns(2)
        with ae_col1:
            gain_heat = st.number_input(
                "인체+기기 발열 [W/m²]",
                value=float(
                    eb.get("gain_heat", preset.internal_gain_heat_wm2)
                ),
            )
            lighting_wm2 = st.number_input(
                "조명 밀도 [W/m²]",
                value=float(eb.get("lighting_wm2", preset.lighting_wm2)),
            )
            equip_elec_wm2 = st.number_input(
                "콘센트·가전 [W/m²]",
                value=float(
                    eb.get("equip_elec_wm2", preset.equip_elec_wm2)
                ),
            )
            infil_base = st.number_input(
                "기본 침기율 [ACH]",
                value=float(eb.get("infil_base", preset.base_infil_ach)),
            )
        with ae_col2:
            roof_u = st.number_input(
                "지붕 열관류율 [W/m²K]", value=float(eb.get("roof_u", 0.35))
            )
            wall_u = st.number_input(
                "외벽 열관류율 [W/m²K]", value=float(eb.get("wall_u", 0.50))
            )
            slab_u = st.number_input(
                "바닥 열관류율 [W/m²K]", value=float(eb.get("slab_u", 0.80))
            )
            win_u = st.number_input(
                "창호 열관류율 [W/m²K]", value=float(eb.get("win_u", 3.5))
            )
            win_g = st.number_input(
                "창호 SHGC (g-value) [-]",
                value=float(eb.get("win_g", 0.65)),
            )

    # 데이터 수집 및 등록
    building_data = {
        "bldg_name": bldg_name,
        "use_type": use_type,
        "area_m2": area_m2,
        "floors": floors,
        "floor_h": floor_h,
        "wwr": wwr,
        "azimuth_deg": azimuth_deg,
        "aspect_ratio": aspect_ratio,
        "heat_set": heat_set,
        "cool_set": cool_set,
        "hvac_start": hvac_start,
        "hvac_end": hvac_end,
        "sat_mode": sat_mode,
        "sun_mode": sun_mode,
        "dhw_facility": dhw_facility,
        "dhw_heater_type": dhw_heater_type,
        "dhw_persons": dhw_persons,
        "oa_m3h": oa_m3h,
        "kitchen_exh": kitchen_exh,
        "meal_bfst": meal_bfst,
        "meal_lunch": meal_lunch,
        "meal_dinner": meal_dinner,
        "gain_heat": gain_heat,
        "lighting_wm2": lighting_wm2,
        "equip_elec_wm2": equip_elec_wm2,
        "infil_base": infil_base,
        "roof_u": roof_u,
        "wall_u": wall_u,
        "slab_u": slab_u,
        "win_u": win_u,
        "win_g": win_g,
        # 기본 fallback 값 처리
        "gains_start": preset.gains_start,
        "gains_end": preset.gains_end,
        "cop_h": 3.2,
        "cop_c": 3.8,
        "vent_start": 8,
        "vent_end": 18,
        "fan_sp": 0.0,
        "dhw_cop": DHW_HEATER_DEFAULTS.get(dhw_heater_type, {}).get("cop", 0.0),
        "dhw_t_hot_shower": DHW_HEATER_DEFAULTS.get(dhw_heater_type, {}).get(
            "t_hot_shower", 45.0
        ),
        "dhw_t_hot_kitchen": DHW_HEATER_DEFAULTS.get(dhw_heater_type, {}).get(
            "t_hot_kitchen", 80.0
        ),
    }

    # 버튼 UI
    if st.session_state.edit_idx is not None:
        if st.button("✏️ 수정 완료", type="primary"):
            st.session_state.buildings[st.session_state.edit_idx] = building_data
            st.session_state.edit_idx = None
            st.success(f"[{bldg_name}] 수정되었습니다.")
            st.rerun()
    else:
        if st.button("➕ 목록에 추가", type="primary"):
            # 이름 중복 방지
            existing_names = [b["bldg_name"] for b in st.session_state.buildings]
            if bldg_name in existing_names:
                building_data["bldg_name"] = f"{bldg_name}_{len(st.session_state.buildings)+1}"

            st.session_state.buildings.append(building_data)
            st.success(f"[{building_data['bldg_name']}] 목록에 추가되었습니다.")
            st.rerun()

# --- 탭 2: 계산 대상 목록 ---
with tab_list:
    st.subheader("등록된 건물 목록")

    if not st.session_state.buildings:
        st.info("등록된 건물이 없습니다. [건물 정보 입력] 탭에서 건물을 추가해 주세요.")
    else:
        # 데이터프레임 뷰로 전환
        bldg_summary = []
        for i, b in enumerate(st.session_state.buildings):
            bldg_summary.append(
                {
                    "No": i + 1,
                    "건물명": b["bldg_name"],
                    "용도": b["use_type"],
                    "면적 [m²]": b["area_m2"],
                    "층수": b["floors"],
                    "주말운영": f"토:{b['sat_mode'][:3]}/일:{b['sun_mode'][:3]}",
                }
            )

        df_summary = pd.DataFrame(bldg_summary)
        st.dataframe(df_summary, use_container_width=True, hide_index=True)

        col_action1, col_action2, col_action3 = st.columns([1, 1, 4])
        with col_action1:
            mod_idx = st.number_input(
                "수정할 번호",
                min_value=1,
                max_value=len(st.session_state.buildings),
                step=1,
            )
            if st.button("📝 선택 수정"):
                st.session_state.edit_idx = mod_idx - 1
                st.success(
                    f"{mod_idx}번 건물을 수정 모드로 전환했습니다. '건물 정보 입력' 탭을 확인하세요."
                )
                st.rerun()

        with col_action2:
            del_idx = st.number_input(
                "삭제할 번호",
                min_value=1,
                max_value=len(st.session_state.buildings),
                step=1,
            )
            if st.button("🗑️ 선택 삭제", type="secondary"):
                del_name = st.session_state.buildings[del_idx - 1]["bldg_name"]
                st.session_state.buildings.pop(del_idx - 1)
                st.success(f"{del_name} 건물이 삭제되었습니다.")
                st.rerun()

        st.divider()

        # 시뮬레이션 가동 버튼
        if st.button("🚀 전체 시뮬레이션 계산 실행", type="primary", use_container_width=True):
            buildings = st.session_state.buildings
            epw_year = 2009

            # 7. Core Logic - 뷰 및 진행 상황 바
            with st.status("물리 엔진 시뮬레이션 진행 중...", expanded=True) as status:
                try:
                    # Step A: 기상 데이터 수집
                    status.write("📡 Step A: Open-Meteo 기상 수집 중...")
                    df_open, wmeta = cached_fetch_weather(site_lat, site_lon)

                    # Step B: 급수 온도 계산
                    status.write("💧 Step B: 급수 온도 계산 중...")
                    t_supply, freeze_warns = calc_supply_temp_series(
                        df_open, water_source
                    )

                    # Step C: EPW 생성
                    status.write("💾 Step C: EPW 기상 파일 생성 중...")
                    base_8760, df_mapped = build_epw_year_dataframe(
                        df_open, wmeta, epw_year
                    )

                    end_user = start_user + pd.Timedelta(days=int(site_days))
                    wu_start = start_user - pd.Timedelta(days=2)
                    ws_sim = _safe_replace_year(wu_start, epw_year)
                    we_sim = _safe_replace_year(end_user, epw_year)

                    tmp_dir = tempfile.mkdtemp()
                    epw_path = os.path.join(tmp_dir, f"om_{epw_year}.epw")
                    write_epw_8760_from_open_meteo(base_8760, wmeta, epw_path)

                    # Step D: 건물별 시뮬레이션
                    bldg_results = {}
                    n_bldgs = len(buildings)

                    for idx, bp in enumerate(buildings):
                        nm = bp["bldg_name"]
                        ut = bp["use_type"]
                        status.write(
                            f"🏢 Step D-{idx+1}/{n_bldgs}: [{nm}] ISO 52016 계산 중..."
                        )

                        area_m2 = bp["area_m2"]
                        dhw_persons = bp["dhw_persons"]
                        if dhw_persons <= 0:
                            dhw_persons = estimate_persons(area_m2, ut)
                        occ_ratio = min(
                            1.0, dhw_persons / max(1, estimate_persons(area_m2, ut))
                        )

                        # 식당 특화 처리
                        restaurant_gains_prof = None
                        hvac_start, hvac_end = bp["hvac_start"], bp["hvac_end"]
                        gains_start, gains_end = (
                            bp["gains_start"],
                            bp["gains_end"],
                        )

                        if ut == "식당":
                            restaurant_gains_prof = make_restaurant_gains_profile(
                                bp["meal_bfst"], bp["meal_lunch"], bp["meal_dinner"]
                            )
                            hvac_start, hvac_end = get_restaurant_hvac_times(
                                bp["meal_bfst"], bp["meal_lunch"], bp["meal_dinner"]
                            )
                            gains_start = min(hvac_start + 1, 23)
                            gains_end = max(hvac_end - 1, 0)

                        # 풍속 보정
                        infil_eff, v75 = compute_effective_infil_ach(
                            area_m2,
                            bp["floor_h"],
                            bp["infil_base"],
                            bp["oa_m3h"],
                            bp["kitchen_exh"],
                            bp["vent_start"],
                            bp["vent_end"],
                            df_open=df_open,
                            start_user=start_user,
                            days=site_days,
                        )

                        bui = build_bui(
                            name=f"{nm}_{ut}",
                            lat=site_lat,
                            lon=site_lon,
                            area_m2=area_m2,
                            n_floors=bp["floors"],
                            floor_height_m=bp["floor_h"],
                            wwr=bp["wwr"],
                            azimuth_deg=bp["azimuth_deg"],
                            aspect_ratio=bp["aspect_ratio"],
                            roof_u=bp["roof_u"],
                            wall_u=bp["wall_u"],
                            slab_u=bp["slab_u"],
                            win_u=bp["win_u"],
                            win_g=bp["win_g"],
                            heat_set=bp["heat_set"],
                            cool_set=bp["cool_set"],
                            hvac_start=hvac_start,
                            hvac_end=hvac_end,
                            gains_start=gains_start,
                            gains_end=gains_end,
                            internal_gain_heat_wm2=bp["gain_heat"],
                            lighting_wm2=bp["lighting_wm2"],
                            infil_ach=infil_eff,
                            sat_mode=bp["sat_mode"],
                            sun_mode=bp["sun_mode"],
                            occupancy_ratio=occ_ratio,
                            restaurant_gains_prof=restaurant_gains_prof,
                        )

                        hs_year = run_iso52016_simulation(
                            bui, epw_path, epw_year
                        )
                        sim_year = int(hs_year.index.min().year)

                        fp = DHW_FACILITY_PARAMS.get(
                            bp["dhw_facility"],
                            DHW_FACILITY_PARAMS["없음"],
                        )
                        df_year = calc_electricity_year(
                            hs_year,
                            cop_h=bp["cop_h"],
                            cop_c=bp["cop_c"],
                            area_m2=area_m2,
                            lighting_wm2=bp["lighting_wm2"],
                            equip_elec_wm2=bp["equip_elec_wm2"],
                            gains_start=gains_start,
                            gains_end=gains_end,
                            supply_oa_m3h=bp["oa_m3h"],
                            kitchen_exh_m3h=bp["kitchen_exh"],
                            vent_start=bp["vent_start"],
                            vent_end=bp["vent_end"],
                            fan_sp=bp["fan_sp"],
                            t_supply=t_supply,
                            facility_type=bp["dhw_facility"],
                            heater_type=bp["dhw_heater_type"],
                            persons=dhw_persons,
                            shower_lpd=fp["shower_lpd"],
                            shower_t_hot=bp["dhw_t_hot_shower"],
                            kitchen_lpd=fp["kitchen_lpd"],
                            kitchen_t_hot=bp["dhw_t_hot_kitchen"],
                            dhw_cop=bp["dhw_cop"],
                            use_type=ut,
                            occupancy_ratio=occ_ratio,
                            df_open=df_open,
                            sat_mode=bp["sat_mode"],
                            sun_mode=bp["sun_mode"],
                            restaurant_gains_prof=restaurant_gains_prof,
                        )

                        df_out = extract_user_period(
                            df_year, df_open, start_user, site_days, sim_year
                        )
                        bldg_results[nm] = df_out

                    # 임시 파일 정리
                    try:
                        os.remove(epw_path)
                        os.rmdir(tmp_dir)
                    except:
                        pass

                    # 결과 병합
                    df_total = (
                        pd.concat(
                            [
                                df[
                                    [
                                        "Total_Elec_kWh",
                                        "HVAC_Elec_kWh",
                                        "Lighting_Elec_kWh",
                                        "Equip_Elec_kWh",
                                        "Fan_Elec_kWh",
                                        "DHW_Elec_kWh",
                                    ]
                                ]
                                for df in bldg_results.values()
                            ],
                            axis=1,
                            keys=bldg_results.keys(),
                        )
                        .groupby(level=1, axis=1)
                        .sum()
                    )

                    sp_param = WATER_SOURCE_PARAMS.get(water_source, {})
                    df_note = ""
                    if sp_param.get("soil_col") is not None:
                        df_note = f' (depth_factor={sp_param.get("depth_factor",1.0)}, 54cm 기반)'

                    t_s_p = t_supply.reindex(df_total.index, method="nearest")

                    st.session_state.last_result = {
                        "bldg_results": bldg_results,
                        "df_total": df_total,
                        "meta": {
                            "start": start_user,
                            "end": end_user,
                            "timezone": wmeta.get("timezone", ""),
                            "water_source": water_source,
                            "t_sup_mean": float(t_s_p.mean()),
                            "t_sup_min": float(t_s_p.min()),
                            "t_sup_max": float(t_s_p.max()),
                            "df_note": df_note,
                            "n_bldgs": len(buildings),
                        },
                    }

                    status.update(
                        label="✅ 시뮬레이션 계산 완료!", state="complete"
                    )
                    st.toast("모든 건물의 시뮬레이션이 성공적으로 끝났습니다.")

                except Exception as e:
                    status.update(
                        label="❌ 시뮬레이션 실패", state="error"
                    )
                    st.error(f"오류가 발생했습니다: {str(e)}")
                    st.exception(e)


# --- 탭 3: 계산 결과 ---
with tab_result:
    if st.session_state.last_result is None:
        st.info("시뮬레이션을 실행하면 결과가 여기에 표시됩니다.")
    else:
        res = st.session_state.last_result
        meta = res["meta"]
        df_total = res["df_total"]
        bldg_results = res["bldg_results"]

        # KPI 메트릭 카드 상단 배치
        st.subheader("🏁 종합 및 현장 합계 요약")
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        total_site_kwh = df_total["Total_Elec_kWh"].sum()
        peak_site_kw = df_total["Total_Elec_kWh"].max()

        kpi1.metric(
            label="현장 총 소비 전력량", value=f"{total_site_kwh:,.1f} kWh"
        )
        kpi2.metric(label="현장 피크 전력 수요", value=f"{peak_site_kw:,.2f} kW")
        kpi3.metric(
            label="급수 평균 온도", value=f"{meta['t_sup_mean']:.1f} °C"
        )
        kpi4.metric(
            label="최저/최고 급수 온도",
            value=f"{meta['t_sup_min']:.1f} / {meta['t_sup_max']:.1f} °C",
        )

        st.divider()

        # 시각화 영역
        st.subheader("💡 비중 및 시간별 전력 수요 분석")
        chart_col1, chart_col2 = st.columns([1, 2])

        with chart_col1:
            # 1. 파이 차트: 건물별 전력량 비중
            pie_data = {
                nm: df["Total_Elec_kWh"].sum()
                for nm, df in bldg_results.items()
            }
            fig_pie = px.pie(
                values=list(pie_data.values()),
                names=list(pie_data.keys()),
                title="건물별 전력 사용량 비중",
                hole=0.4,
                color_discrete_sequence=px.colors.qualitative.Pastel,
            )
            fig_pie.update_layout(showlegend=True)
            st.plotly_chart(fig_pie, use_container_width=True)

        with chart_col2:
            # 2. 라인 차트: 시간별 현장 합계 전력 수요
            fig_line = go.Figure()
            fig_line.add_trace(
                go.Scatter(
                    x=df_total.index,
                    y=df_total["Total_Elec_kWh"],
                    mode="lines",
                    name="현장 총 전력 (kWh)",
                    fill="tozeroy",
                    line=dict(color="#1F4E79", width=2),
                )
            )
            # 주요 용도별 데이터 추가
            for sub in [
                "HVAC_Elec_kWh",
                "Lighting_Elec_kWh",
                "DHW_Elec_kWh",
            ]:
                fig_line.add_trace(
                    go.Scatter(
                        x=df_total.index,
                        y=df_total[sub],
                        mode="lines",
                        name=sub.replace("_Elec_kWh", ""),
                        line=dict(width=1, dash="dot"),
                    )
                )

            fig_line.update_layout(
                title="예측 기간 시간별 전력 수요 (Load Profile)",
                xaxis_title="날짜 및 시각",
                yaxis_title="전력량 [kWh]",
                hovermode="x unified",
            )
            st.plotly_chart(fig_line, use_container_width=True)

        st.divider()

        st.subheader("🏢 건물별 세부 요약")
        # 건물별 세부 요약 테이블
        bldg_det = []
        for nm, df_b in bldg_results.items():
            bldg_det.append(
                {
                    "건물명": nm,
                    "총 전력량 [kWh]": round(df_b["Total_Elec_kWh"].sum(), 1),
                    "냉난방 (HVAC)": round(df_b["HVAC_Elec_kWh"].sum(), 1),
                    "급탕 (DHW)": round(df_b["DHW_Elec_kWh"].sum(), 1),
                    "피크 부하 [kW]": round(df_b["Total_Power_kW"].max(), 2),
                }
            )
        st.dataframe(
            pd.DataFrame(bldg_det), use_container_width=True, hide_index=True
        )

        st.divider()

        # 다운로드 영역
        st.subheader("📥 데이터 다운로드")
        col_dw1, col_dw2 = st.columns(2)

        with col_dw1:
            # Excel 다운로드
            with st.spinner("Excel 파일을 준비 중입니다..."):
                excel_buf = generate_excel_buffer(res)

            st.download_button(
                label="📥 통합 결과 Excel 파일 다운로드",
                data=excel_buf.getvalue(),
                file_name=f"forecast_energy_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with col_dw2:
            # CSV 다운로드
            csv_site = df_total.to_csv().encode("utf-8")
            st.download_button(
                label="📥 현장 합계 CSV 파일 다운로드",
                data=csv_site,
                file_name="forecast_site_hourly.csv",
                mime="text/csv",
                use_container_width=True,
            )