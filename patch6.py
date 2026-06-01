"""
PATCH 6:
1. 날짜 필터 버그 수정 (오늘/어제, 오래된 기록)
2. 사이드바 계층형 메뉴 (그룹 헤더 추가)
3. 개념 수정/병합 UI
4. 개념 일괄 폴더 변경
5. Finder 3열 계층형 뷰
"""

SRC = "/Users/parkcy/Desktop/trustlens-mvp/app_v2.py"

with open(SRC, encoding="utf-8") as f:
    code = f.read()

# ──────────────────────────────────────────────────────────────
# FIX 1: 날짜 필터 버그
# 문제: board_items가 date 기반으로 필터되지만 items에 date가 없을 수도 있음
# 해결: _days_since에서 9999 대신 올바른 fallback + 필터 후 카운트 표시
# ──────────────────────────────────────────────────────────────
OLD_DATE_FILTER = """        if board_date != "전체":
            from datetime import datetime as _dt
            def _days_since(d):
                try:
                    return (_dt.now() - _dt.strptime(str(d or "")[:10], "%Y-%m-%d")).days
                except Exception:
                    return 9999
            if board_date == "오늘/어제":
                board_items = [item for item in board_items if _days_since(item.get("date", "")) <= 1]
            elif board_date == "최근 7일":
                board_items = [item for item in board_items if _days_since(item.get("date", "")) <= 7]
            elif board_date == "최근 30일":
                board_items = [item for item in board_items if _days_since(item.get("date", "")) <= 30]
            elif board_date == "오래된 기록":
                board_items = [item for item in board_items if _days_since(item.get("date", "")) > 30]
        board_items = [item for item in board_items if int(item.get("score", 0) or 0) >= min_score]"""

NEW_DATE_FILTER = """        if board_date != "전체":
            from datetime import datetime as _dt
            def _days_since(d):
                try:
                    _s = str(d or "").strip()[:10]
                    if not _s or _s == "":
                        return -1  # 날짜 없는 항목은 제외
                    return (_dt.now() - _dt.strptime(_s, "%Y-%m-%d")).days
                except Exception:
                    return -1
            if board_date == "오늘/어제":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 1]
            elif board_date == "최근 7일":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 7]
            elif board_date == "최근 30일":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 30]
            elif board_date == "오래된 기록":
                board_items = [item for item in board_items if _days_since(item.get("date", "")) > 30]
        board_items = [item for item in board_items if int(item.get("score", 0) or 0) >= min_score]
        st.caption(f"필터 결과: {len(board_items)}개 항목")"""

if OLD_DATE_FILTER in code:
    code = code.replace(OLD_DATE_FILTER, NEW_DATE_FILTER)
    print("FIX 1 applied: 날짜 필터 버그 수정")
else:
    print("FIX 1 SKIP: pattern not found")


# ──────────────────────────────────────────────────────────────
# FIX 2: 사이드바 계층형 메뉴 (그룹 헤더 구조로 변경)
# radio 대신 st.sidebar에 그룹별 버튼 구조
# ──────────────────────────────────────────────────────────────
OLD_SIDEBAR = """with st.sidebar:
    st.markdown('<div class="sidebar-title">🛡️ TrustLens</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-subtitle">AI가 대신 믿지 않고, 판단을 돕는 도구</div>', unsafe_allow_html=True)
    menu = st.radio(
        "메뉴",
        [
            "▶ 분석 시작하기",
            "📊 분석 결과",
            "🔎 신뢰도 근거",
            "🏷️ 분석결과 아카이브",
            "🏷️ 태그 관리",
            "🗂️ 지식 아카이브",
            "🧠 지식 맵",
            "📁 프로젝트",
            "✅ 작업 관리",
            "🕘 최근 검색 기록",
        ],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption("MVP v3 · URL 분석 + 지식 아카이브")"""

NEW_SIDEBAR = """with st.sidebar:
    st.markdown('<div class="sidebar-title">🛡️ TrustLens</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-subtitle">AI가 대신 믿지 않고, 판단을 돕는 도구</div>', unsafe_allow_html=True)

    _ALL_MENUS = [
        "▶ 분석 시작하기",
        "📊 분석 결과",
        "🔎 신뢰도 근거",
        "🏷️ 분석결과 아카이브",
        "🏷️ 태그 관리",
        "🗂️ 지식 아카이브",
        "🧠 지식 맵",
        "📁 프로젝트",
        "✅ 작업 관리",
        "🕘 최근 검색 기록",
    ]
    _GROUPS = {
        "분석": ["▶ 분석 시작하기", "📊 분석 결과", "🔎 신뢰도 근거"],
        "아카이브": ["🏷️ 분석결과 아카이브", "🏷️ 태그 관리", "🗂️ 지식 아카이브"],
        "지식 관리": ["🧠 지식 맵", "📁 프로젝트", "✅ 작업 관리"],
        "기록": ["🕘 최근 검색 기록"],
    }
    if "menu" not in st.session_state:
        st.session_state["menu"] = "▶ 분석 시작하기"

    st.markdown("""
    <style>
    .sidebar-group-header {
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: rgba(255,255,255,0.45) !important;
        margin: 14px 0 4px 6px;
    }
    div[data-testid="stSidebar"] .stButton button {
        background: transparent !important;
        border: none !important;
        border-radius: 10px !important;
        color: rgba(255,255,255,0.88) !important;
        font-size: 14px !important;
        font-weight: 500 !important;
        text-align: left !important;
        padding: 7px 10px !important;
        width: 100% !important;
        margin: 1px 0 !important;
        transition: background 0.15s !important;
    }
    div[data-testid="stSidebar"] .stButton button:hover {
        background: rgba(255,255,255,0.12) !important;
    }
    div[data-testid="stSidebar"] .active-menu-btn button {
        background: rgba(255,255,255,0.20) !important;
        font-weight: 800 !important;
        color: white !important;
    }
    </style>
    """, unsafe_allow_html=True)

    for group_name, group_items in _GROUPS.items():
        st.markdown(f'<div class="sidebar-group-header">{group_name}</div>', unsafe_allow_html=True)
        for item in group_items:
            _is_active = st.session_state.get("menu") == item
            if _is_active:
                st.markdown('<div class="active-menu-btn">', unsafe_allow_html=True)
            if st.button(item, key=f"sidebar_btn_{item}", use_container_width=True):
                st.session_state["menu"] = item
                st.rerun()
            if _is_active:
                st.markdown('</div>', unsafe_allow_html=True)

    menu = st.session_state.get("menu", "▶ 분석 시작하기")
    st.markdown("---")
    st.caption("MVP v3 · URL 분석 + 지식 아카이브")"""

if OLD_SIDEBAR in code:
    code = code.replace(OLD_SIDEBAR, NEW_SIDEBAR)
    print("FIX 2 applied: 사이드바 계층형 메뉴")
else:
    print("FIX 2 SKIP: sidebar pattern not found")


# ──────────────────────────────────────────────────────────────
# FIX 3: 개념 수정/병합 UI — render_knowledge_map_page의 핵심개념 허브 위에 추가
# ──────────────────────────────────────────────────────────────
OLD_HUB_HEADER = '    st.markdown("### 🧠 핵심 개념 허브")\n    st.caption("폴더별로 묶인 개념이에요. 폴더를 눌러 펼치고, 개념을 클릭하면 연결 문서를 볼 수 있어요.")'
NEW_HUB_HEADER = '''    st.markdown("### 🧠 핵심 개념 허브")
    st.caption("폴더별로 묶인 개념이에요. 폴더를 눌러 펼치고, 개념을 클릭하면 연결 문서를 볼 수 있어요.")

    with st.expander("✏️ 개념 수정 / 병합", expanded=False):
        _custom_concepts = [
            c if isinstance(c, dict) else {"name": str(c), "folder": "내 개념", "created_at": ""}
            for c in st.session_state.get("pkm_custom_concepts", []) if c
        ]
        _concept_names = [c.get("name", "") for c in _custom_concepts]

        ec1, ec2 = st.columns(2)
        with ec1:
            st.markdown("**✏️ 개념 이름 수정**")
            _edit_target = st.selectbox("수정할 개념", _concept_names, key="edit_concept_target") if _concept_names else None
            _edit_new_name = st.text_input("새 이름", placeholder="변경할 이름 입력", key="edit_concept_new_name")
            if st.button("이름 변경", key="do_rename_concept", use_container_width=True):
                if _edit_target and _edit_new_name.strip():
                    _new = _edit_new_name.strip()
                    updated = []
                    for c in _custom_concepts:
                        if c.get("name") == _edit_target:
                            c = dict(c)
                            c["name"] = _new
                        updated.append(c)
                    st.session_state.pkm_custom_concepts = updated
                    # note_concept_links도 업데이트
                    for lk in st.session_state.get("note_concept_links", []):
                        if lk.get("concept") == _edit_target:
                            lk["concept"] = _new
                    save_persisted_data()
                    st.success(f"'{_edit_target}' → '{_new}' 변경 완료!")
                    st.rerun()
                else:
                    st.warning("수정할 개념과 새 이름을 입력해주세요.")

            if st.button("🗑️ 개념 삭제", key="do_delete_concept", use_container_width=True):
                if _edit_target:
                    st.session_state.pkm_custom_concepts = [c for c in _custom_concepts if c.get("name") != _edit_target]
                    st.session_state["note_concept_links"] = [
                        lk for lk in st.session_state.get("note_concept_links", [])
                        if lk.get("concept") != _edit_target
                    ]
                    save_persisted_data()
                    st.success(f"'{_edit_target}' 삭제 완료!")
                    st.rerun()

        with ec2:
            st.markdown("**🔗 개념 병합 (A → B로 합치기)**")
            _merge_from = st.selectbox("병합할 개념 (없앨 것)", _concept_names, key="merge_from") if _concept_names else None
            _merge_to = st.selectbox("합쳐질 개념 (남길 것)", _concept_names, key="merge_to") if _concept_names else None
            if st.button("병합 실행", key="do_merge_concept", use_container_width=True):
                if _merge_from and _merge_to and _merge_from != _merge_to:
                    # from을 삭제, note_concept_links에서 from → to로 교체
                    st.session_state.pkm_custom_concepts = [c for c in _custom_concepts if c.get("name") != _merge_from]
                    for lk in st.session_state.get("note_concept_links", []):
                        if lk.get("concept") == _merge_from:
                            lk["concept"] = _merge_to
                    save_persisted_data()
                    st.success(f"'{_merge_from}'를 '{_merge_to}'로 병합 완료!")
                    st.rerun()
                else:
                    st.warning("서로 다른 두 개념을 선택해주세요.")'''

if OLD_HUB_HEADER in code:
    code = code.replace(OLD_HUB_HEADER, NEW_HUB_HEADER)
    print("FIX 3 applied: 개념 수정/병합 UI")
else:
    print("FIX 3 SKIP: hub header pattern not found")


# ──────────────────────────────────────────────────────────────
# FIX 4: 개념 일괄 폴더 변경 버튼 (허브 상단)
# ──────────────────────────────────────────────────────────────
OLD_HUB_SEARCH = '        hub_search = st.text_input("🔍 개념 검색", placeholder="개념명으로 검색", key="hub_concept_search")'
NEW_HUB_SEARCH = '''        # 일괄 폴더 변경 UI
        _bulk_sel_key = "hub_bulk_selected"
        if _bulk_sel_key not in st.session_state:
            st.session_state[_bulk_sel_key] = []

        _bulk_mode = st.toggle("📦 일괄 폴더 변경 모드", key="hub_bulk_mode")
        if _bulk_mode:
            st.caption("개념 옆 체크박스로 선택 후 아래서 이동할 폴더를 고르세요.")
            _all_folder_names_bulk = sorted(set(concept_folder_map.values()) | {"자동"})
            _bulk_target = st.selectbox("이동할 폴더 선택", _all_folder_names_bulk + ["+ 새 폴더 직접 입력"], key="bulk_target_folder")
            if _bulk_target == "+ 새 폴더 직접 입력":
                _bulk_target = st.text_input("새 폴더명", key="bulk_new_folder_name")
            if st.button("선택 개념 일괄 이동", key="do_bulk_move", type="primary", use_container_width=True):
                _selected = st.session_state.get(_bulk_sel_key, [])
                if _selected and _bulk_target:
                    _updated_cc = []
                    for c in st.session_state.get("pkm_custom_concepts", []):
                        _cn = c.get("name") if isinstance(c, dict) else str(c)
                        if _cn in _selected:
                            c = dict(c) if isinstance(c, dict) else {"name": _cn, "created_at": ""}
                            c["folder"] = _bulk_target
                        _updated_cc.append(c)
                    st.session_state.pkm_custom_concepts = _updated_cc
                    folders = st.session_state.get("pkm_concept_folders", {})
                    for _cn in _selected:
                        folders[_cn] = _bulk_target
                    st.session_state.pkm_concept_folders = folders
                    st.session_state[_bulk_sel_key] = []
                    save_persisted_data()
                    st.success(f"{len(_selected)}개 개념을 '{_bulk_target}'으로 이동했어요!")
                    st.rerun()
                else:
                    st.warning("개념을 선택하고 폴더를 골라주세요.")
            st.divider()

        hub_search = st.text_input("🔍 개념 검색", placeholder="개념명으로 검색", key="hub_concept_search")'''

if OLD_HUB_SEARCH in code:
    code = code.replace(OLD_HUB_SEARCH, NEW_HUB_SEARCH)
    print("FIX 4 applied: 일괄 폴더 변경 UI")
else:
    print("FIX 4 SKIP: hub_search pattern not found")


# ──────────────────────────────────────────────────────────────
# FIX 4b: 허브 concept row에 체크박스 추가 (일괄 선택용)
# ──────────────────────────────────────────────────────────────
OLD_HUB_ROW = '''                    else:
                        col_name, col_count, col_move, col_btn = st.columns([3, 1, 1, 1])
                        with col_name:
                            st.markdown(
                                f\'<div style="padding:6px 0; font-weight:{"700" if is_selected else "400"}; color:{"#2f73ff" if is_selected else "#172033"}">{"▶ " if is_selected else "🧠 "}{concept}</div>\',
                                unsafe_allow_html=True,
                            )'''
NEW_HUB_ROW = '''                    else:
                        _bulk_mode_active = st.session_state.get("hub_bulk_mode", False)
                        if _bulk_mode_active:
                            col_chk, col_name, col_count, col_move, col_btn = st.columns([0.5, 2.5, 1, 1, 1])
                            with col_chk:
                                _is_checked = concept in st.session_state.get("hub_bulk_selected", [])
                                if st.checkbox("", value=_is_checked, key=f"hub_chk_{folder_name[:6]}_{row_idx}_{concept[:12]}", label_visibility="collapsed"):
                                    if concept not in st.session_state.get("hub_bulk_selected", []):
                                        st.session_state.setdefault("hub_bulk_selected", []).append(concept)
                                else:
                                    if concept in st.session_state.get("hub_bulk_selected", []):
                                        st.session_state["hub_bulk_selected"].remove(concept)
                        else:
                            col_name, col_count, col_move, col_btn = st.columns([3, 1, 1, 1])
                        with col_name:
                            st.markdown(
                                f\'<div style="padding:6px 0; font-weight:{"700" if is_selected else "400"}; color:{"#2f73ff" if is_selected else "#172033"}">{"▶ " if is_selected else "🧠 "}{concept}</div>\',
                                unsafe_allow_html=True,
                            )'''

if OLD_HUB_ROW in code:
    code = code.replace(OLD_HUB_ROW, NEW_HUB_ROW)
    print("FIX 4b applied: hub row 체크박스 추가")
else:
    print("FIX 4b SKIP: pattern not found")


# ──────────────────────────────────────────────────────────────
# FIX 5: 핵심개념 파인더에 Finder 3열 계층형 뷰 탭 추가
# render_concept_finder 함수에 뷰 토글 추가
# ──────────────────────────────────────────────────────────────
OLD_FINDER_HEADER = '''    st.markdown("### 🗂️ 핵심개념 파인더")
    st.caption("핵심개념을 Finder처럼 폴더 → 하위폴더 → 개념 구조로 볼 수 있어요.")'''
NEW_FINDER_HEADER = '''    st.markdown("### 🗂️ 핵심개념 파인더")
    st.caption("핵심개념을 Finder처럼 폴더 → 하위폴더 → 개념 구조로 볼 수 있어요.")

    _finder_view = st.radio("뷰 모드", ["📂 Finder 3열", "📋 카드 뷰"], horizontal=True, key=f"{key_prefix}_finder_view_mode")
    _use_finder_col = _finder_view == "📂 Finder 3열"'''

if OLD_FINDER_HEADER in code:
    code = code.replace(OLD_FINDER_HEADER, NEW_FINDER_HEADER)
    print("FIX 5a applied: finder view mode toggle")
else:
    print("FIX 5a SKIP")

# Replace the main concept rendering section with 3-col Finder view
OLD_FINDER_GROUPED = '''    for top, sub_groups in sorted(grouped.items()):
        with st.expander(f"📁 {top}", expanded=True):
            for sub, concepts in sorted(sub_groups.items()):
                st.markdown(f\'<div class="pkm-folder-title">📂 {sub}</div>\', unsafe_allow_html=True)

                cols = st.columns(4)
                for idx, (concept, docs) in enumerate(sorted(concepts, key=lambda x: len(x[1]), reverse=True)):
                    with cols[idx % 4]:
                        st.markdown(
                            f"""
                            <div class="pkm-concept-card">
                                <div class="pkm-concept-name">🧠 {concept}</div>
                                <div class="pkm-concept-meta">{get_concept_folder(concept)} · {len(docs)}개 연결 · {sum(1 for l in st.session_state.get("note_concept_links",[]) if l.get("concept")==concept)}개 메모</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        if st.button(
                            "관련 문서 보기",
                            key=f"{key_prefix}_concept_open_{top}_{sub}_{idx}_{abs(hash(concept))}",
                            use_container_width=True,
                        ):
                            st.session_state["pkm_selected_concept"] = concept'''

NEW_FINDER_GROUPED = '''    if _use_finder_col:
        # ── Finder 3열 뷰: 상위폴더 | 하위폴더 | 개념 ──
        _finder_col1, _finder_col2, _finder_col3 = st.columns([1.2, 1.2, 2], gap="small")
        _sel_top = st.session_state.get(f"{key_prefix}_sel_top_folder")
        _sel_sub = st.session_state.get(f"{key_prefix}_sel_sub_folder")

        with _finder_col1:
            st.markdown("**📁 상위 폴더**")
            for _top in sorted(grouped.keys()):
                _n_concepts = sum(len(v) for v in grouped[_top].values())
                _active = _sel_top == _top
                if st.button(
                    f"{'▶ ' if _active else ''}{_top}  ({_n_concepts})",
                    key=f"{key_prefix}_finder_top_{_top}",
                    use_container_width=True,
                    type="primary" if _active else "secondary",
                ):
                    st.session_state[f"{key_prefix}_sel_top_folder"] = _top
                    st.session_state[f"{key_prefix}_sel_sub_folder"] = None
                    st.rerun()

        with _finder_col2:
            st.markdown("**📂 하위 폴더**")
            if _sel_top and _sel_top in grouped:
                for _sub in sorted(grouped[_sel_top].keys()):
                    _n = len(grouped[_sel_top][_sub])
                    _active_sub = _sel_sub == _sub
                    if st.button(
                        f"{'▶ ' if _active_sub else ''}{_sub}  ({_n})",
                        key=f"{key_prefix}_finder_sub_{_sel_top}_{_sub}",
                        use_container_width=True,
                        type="primary" if _active_sub else "secondary",
                    ):
                        st.session_state[f"{key_prefix}_sel_sub_folder"] = _sub
                        st.rerun()
            else:
                st.caption("← 상위 폴더를 선택하세요")

        with _finder_col3:
            st.markdown("**🧠 개념**")
            if _sel_top and _sel_sub and _sel_top in grouped and _sel_sub in grouped.get(_sel_top, {}):
                _finder_concepts = grouped[_sel_top][_sel_sub]
                for _fc_idx, (_fc, _fc_docs) in enumerate(sorted(_finder_concepts, key=lambda x: len(x[1]), reverse=True)):
                    _memo_cnt = sum(1 for l in st.session_state.get("note_concept_links", []) if l.get("concept") == _fc)
                    _is_sel = st.session_state.get("pkm_selected_concept") == _fc
                    with st.container(border=True):
                        st.markdown(f"**{'🔵 ' if _is_sel else '🧠 '}{_fc}**")
                        st.caption(f"연결 {len(_fc_docs)}개 · 메모 {_memo_cnt}개")
                        if st.button("보기" if not _is_sel else "닫기", key=f"{key_prefix}_finder_concept_{_sel_top[:6]}_{_sel_sub[:6]}_{_fc_idx}", use_container_width=True):
                            st.session_state["pkm_selected_concept"] = None if _is_sel else _fc
                            st.rerun()
            elif _sel_top:
                st.caption("← 하위 폴더를 선택하세요")
            else:
                st.caption("← 폴더를 선택하세요")
    else:
        # ── 기존 카드 뷰 ──
        for top, sub_groups in sorted(grouped.items()):
            with st.expander(f"📁 {top}", expanded=True):
                for sub, concepts in sorted(sub_groups.items()):
                    st.markdown(f\'<div class="pkm-folder-title">📂 {sub}</div>\', unsafe_allow_html=True)

                    cols = st.columns(4)
                    for idx, (concept, docs) in enumerate(sorted(concepts, key=lambda x: len(x[1]), reverse=True)):
                        with cols[idx % 4]:
                            st.markdown(
                                f"""
                                <div class="pkm-concept-card">
                                    <div class="pkm-concept-name">🧠 {concept}</div>
                                    <div class="pkm-concept-meta">{get_concept_folder(concept)} · {len(docs)}개 연결 · {sum(1 for l in st.session_state.get("note_concept_links",[]) if l.get("concept")==concept)}개 메모</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                            if st.button(
                                "관련 문서 보기",
                                key=f"{key_prefix}_concept_open_{top}_{sub}_{idx}_{abs(hash(concept))}",
                                use_container_width=True,
                            ):
                                st.session_state["pkm_selected_concept"] = concept'''

if OLD_FINDER_GROUPED in code:
    code = code.replace(OLD_FINDER_GROUPED, NEW_FINDER_GROUPED)
    print("FIX 5b applied: Finder 3열 뷰")
else:
    print("FIX 5b SKIP: finder grouped pattern not found")


# write
with open(SRC, "w", encoding="utf-8") as f:
    f.write(code)
print("Done writing file.")

import ast
with open(SRC, encoding="utf-8") as f:
    src = f.read()
try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR line {e.lineno}: {e.msg}")
