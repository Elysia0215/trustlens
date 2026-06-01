"""
PATCH 5:
1. Knowledge map board: add Project filter
2. Concept hub: show note_concept_links count in bracket
3. AI Brainstorming tab (Sprint 6) added to knowledge map
"""
SRC = "/Users/parkcy/Desktop/trustlens-mvp/app_v2.py"

with open(SRC, encoding="utf-8") as f:
    code = f.read()

# ─────────────────────────────────────────────
# FIX 1: Add project filter to board view
# ─────────────────────────────────────────────
OLD_BOARD_FILTER = """        f1, f2, f3, f4 = st.columns(4)
        with f1:
            board_large = st.selectbox("대분류", ["전체"] + sorted({infer_large_category(item) for item in items}), key="board_large_filter")
        with f2:
            board_tag = st.selectbox("태그", ["전체"] + total_tags, key="board_tag_filter")
        with f3:
            board_date = st.selectbox("기간", ["전체", "오늘/어제", "최근 7일", "최근 30일", "오래된 기록"], key="board_date_filter")
        with f4:
            min_score = st.slider("최소 점수", 0, 100, 0, 5, key="board_min_score")

        board_items = items
        if board_large != "전체":
            board_items = [item for item in board_items if infer_large_category(item) == board_large]
        if board_tag != "전체":
            board_items = [item for item in board_items if board_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]]"""

NEW_BOARD_FILTER = """        _board_project_names = ["전체"] + sorted({
            item.get("project", "기본 프로젝트") for item in items if item.get("project")
        })
        f0, f1, f2, f3, f4 = st.columns(5)
        with f0:
            board_project = st.selectbox("프로젝트", _board_project_names, key="board_project_filter")
        with f1:
            board_large = st.selectbox("대분류", ["전체"] + sorted({infer_large_category(item) for item in items}), key="board_large_filter")
        with f2:
            board_tag = st.selectbox("태그", ["전체"] + total_tags, key="board_tag_filter")
        with f3:
            board_date = st.selectbox("기간", ["전체", "오늘/어제", "최근 7일", "최근 30일", "오래된 기록"], key="board_date_filter")
        with f4:
            min_score = st.slider("최소 점수", 0, 100, 0, 5, key="board_min_score")

        board_items = items
        if board_project != "전체":
            board_items = [item for item in board_items if item.get("project", "기본 프로젝트") == board_project]
        if board_large != "전체":
            board_items = [item for item in board_items if infer_large_category(item) == board_large]
        if board_tag != "전체":
            board_items = [item for item in board_items if board_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]]"""

if OLD_BOARD_FILTER in code:
    code = code.replace(OLD_BOARD_FILTER, NEW_BOARD_FILTER)
    print("FIX 1 applied: project filter in board view")
else:
    print("FIX 1 SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 2: concept hub row - show memo link count
# ─────────────────────────────────────────────
OLD_HUB_COUNT = '''                        with col_count:
                            st.markdown(
                                f\'<div style="padding:6px 0; color:#888; font-size:0.9em">{count}개</div>\',
                                unsafe_allow_html=True,
                            )'''
NEW_HUB_COUNT = '''                        with col_count:
                            _memo_link_cnt = sum(1 for l in st.session_state.get("note_concept_links", []) if l.get("concept") == concept)
                            _count_str = f"{count}개" + (f" · {_memo_link_cnt}메모" if _memo_link_cnt else "")
                            st.markdown(
                                f\'<div style="padding:6px 0; color:#888; font-size:0.9em">{_count_str}</div>\',
                                unsafe_allow_html=True,
                            )'''
if OLD_HUB_COUNT in code:
    code = code.replace(OLD_HUB_COUNT, NEW_HUB_COUNT)
    print("FIX 2 applied: memo link count in concept hub")
else:
    print("FIX 2 SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 3: Add AI Brainstorming tab (Sprint 6) after concept finder tab
# Look for the tabs definition in render_knowledge_map_page
# ─────────────────────────────────────────────
OLD_TABS = '    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📋 목차", "🧩 보드", "🕸️ 마인드맵", "📄 지식 페이지", "🗂️ 개념 파인더"])'
NEW_TABS = '    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📋 목차", "🧩 보드", "🕸️ 마인드맵", "📄 지식 페이지", "🗂️ 개념 파인더", "🤖 AI 브레인스토밍"])'
if OLD_TABS in code:
    code = code.replace(OLD_TABS, NEW_TABS)
    print("FIX 3a applied: added brainstorming tab to tabs")
else:
    print("FIX 3a SKIP: tabs pattern not found")

# Add the tab6 content - insert after render_concept_finder call
OLD_TAB5_END = '''    with tab5:
        render_concept_finder(items)'''
NEW_TAB5_END = '''    with tab5:
        render_concept_finder(items)

    with tab6:
        st.markdown("### 🤖 AI 브레인스토밍")
        st.caption("저장된 메모나 프로젝트를 기반으로 AI가 새로운 관점과 아이디어를 제안해요.")

        _brain_mode = st.radio("브레인스토밍 기준", ["메모 기반", "프로젝트 기반"], horizontal=True, key="brain_mode")

        if _brain_mode == "메모 기반":
            _notes = st.session_state.get("archive_notes", [])
            if not _notes:
                st.info("저장된 메모가 없어요. 먼저 분석 결과를 저장해보세요.")
            else:
                _note_titles = [n.get("title", "제목 없음") for n in _notes]
                _sel_idx = st.selectbox("메모 선택", range(len(_note_titles)), format_func=lambda i: _note_titles[i], key="brain_note_sel")
                _sel_note = _notes[_sel_idx]
                with st.expander("선택한 메모 미리보기", expanded=False):
                    st.markdown(_sel_note.get("note", "")[:1500])
                _brain_prompt_types = st.multiselect(
                    "원하는 분석 유형",
                    ["확장 주제 제안", "추가 조사 질문", "반대 관점", "발표 문장 초안", "연결 개념 찾기", "다음 할 일"],
                    default=["확장 주제 제안", "다음 할 일"],
                    key="brain_prompt_types"
                )
                if st.button("🤖 AI 브레인스토밍 시작", key="brain_run_note", type="primary", use_container_width=True):
                    if not _brain_prompt_types:
                        st.warning("분석 유형을 하나 이상 선택해주세요.")
                    else:
                        _note_content = _sel_note.get("note", "")[:3000]
                        _system_msg = f"""당신은 지식 관리 전문가입니다. 사용자의 메모를 읽고 요청한 분석 유형별로 구체적인 제안을 해주세요.
분석 유형: {', '.join(_brain_prompt_types)}
각 유형별로 3-5개의 구체적인 항목을 bullet point로 제안하세요. 한국어로 답변하세요."""
                        _user_msg = f"메모 제목: {_sel_note.get('title', '')}\n\n메모 내용:\n{_note_content}"
                        with st.spinner("AI가 브레인스토밍 중..."):
                            try:
                                _brain_result = call_groq_simple(_system_msg, _user_msg)
                                st.session_state["brain_result"] = _brain_result
                            except Exception as e:
                                st.error(f"AI 오류: {e}")

                if st.session_state.get("brain_result"):
                    st.divider()
                    st.markdown("#### 💡 AI 브레인스토밍 결과")
                    st.markdown(st.session_state["brain_result"])
                    if st.button("🗑️ 결과 지우기", key="brain_clear"):
                        st.session_state["brain_result"] = ""
                        st.rerun()

        else:  # 프로젝트 기반
            _projs = st.session_state.get("projects", [])
            if not _projs:
                st.info("저장된 프로젝트가 없어요. 먼저 프로젝트를 만들어보세요.")
            else:
                _proj_names = [p.get("name", "이름 없음") for p in _projs]
                _sel_proj_idx = st.selectbox("프로젝트 선택", range(len(_proj_names)), format_func=lambda i: _proj_names[i], key="brain_proj_sel")
                _sel_proj = _projs[_sel_proj_idx]

                # 이 프로젝트에 연결된 메모 수집
                _proj_notes = [n for n in st.session_state.get("archive_notes", []) if n.get("project") == _sel_proj.get("name")]
                st.caption(f"이 프로젝트에 연결된 메모: {len(_proj_notes)}개")

                _brain_proj_types = st.multiselect(
                    "원하는 분석 유형",
                    ["부족한 자료 파악", "추가 조사 방향", "발표 목차 제안", "예상 질문", "추가 작업 아이디어"],
                    default=["부족한 자료 파악", "추가 작업 아이디어"],
                    key="brain_proj_types"
                )

                if st.button("🤖 프로젝트 AI 분석 시작", key="brain_run_proj", type="primary", use_container_width=True):
                    _proj_summary = f"프로젝트명: {_sel_proj.get('name')}\n설명: {_sel_proj.get('description', '')}\n상태: {_sel_proj.get('status', '')}"
                    _notes_summary = "\n".join([f"- {n.get('title','')}: {n.get('note','')[:200]}" for n in _proj_notes[:5]])
                    _system_msg2 = f"""당신은 프로젝트 관리 전문가입니다. 프로젝트 정보와 연결된 메모를 보고 요청한 유형별 분석을 해주세요.
분석 유형: {', '.join(_brain_proj_types)}
각 유형별로 3-5개의 구체적인 항목을 bullet point로 제안하세요. 한국어로 답변하세요."""
                    _user_msg2 = f"{_proj_summary}\n\n연결된 메모:\n{_notes_summary if _notes_summary else '(연결된 메모 없음)'}"
                    with st.spinner("AI가 프로젝트를 분석 중..."):
                        try:
                            _brain_proj_result = call_groq_simple(_system_msg2, _user_msg2)
                            st.session_state["brain_proj_result"] = _brain_proj_result
                        except Exception as e:
                            st.error(f"AI 오류: {e}")

                if st.session_state.get("brain_proj_result"):
                    st.divider()
                    st.markdown("#### 💡 프로젝트 AI 분석 결과")
                    st.markdown(st.session_state["brain_proj_result"])
                    if st.button("🗑️ 결과 지우기", key="brain_proj_clear"):
                        st.session_state["brain_proj_result"] = ""
                        st.rerun()'''

if OLD_TAB5_END in code:
    code = code.replace(OLD_TAB5_END, NEW_TAB5_END)
    print("FIX 3b applied: brainstorming tab content added")
else:
    print("FIX 3b SKIP: tab5 content pattern not found")

# ─────────────────────────────────────────────
# FIX 4: Add call_groq_simple helper if not present
# ─────────────────────────────────────────────
if "def call_groq_simple" not in code:
    # Insert before analyze_with_groq
    OLD_ANALYZE = "def analyze_with_groq("
    NEW_ANALYZE = '''def call_groq_simple(system_msg: str, user_msg: str, model: str = "llama-3.3-70b-versatile") -> str:
    """단순 Groq API 호출 - 브레인스토밍 등 간단한 텍스트 생성에 사용."""
    api_key = st.session_state.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return "⚠️ API 키가 없어요. 설정에서 Groq API 키를 입력해주세요."
    import requests as _req
    res = _req.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.7,
            "max_tokens": 1500,
        },
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


def analyze_with_groq('''
    if OLD_ANALYZE in code:
        code = code.replace(OLD_ANALYZE, NEW_ANALYZE, 1)
        print("FIX 4 applied: call_groq_simple helper added")
    else:
        print("FIX 4 SKIP: analyze_with_groq not found")
else:
    print("FIX 4 SKIP: call_groq_simple already exists")

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
    print(f"SYNTAX ERROR: {e}")
