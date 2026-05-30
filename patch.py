from pathlib import Path
from datetime import datetime
import textwrap

p = Path("app_v2.py")
s = p.read_text(encoding="utf-8")

backup = Path(f"app_v2_backup_before_map_recent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py")
backup.write_text(s, encoding="utf-8")
print(f"백업 생성: {backup}")

def add_once(text, marker, insert, name):
    if name in text:
        print(f"이미 있음: {name}")
        return text
    if marker not in text:
        raise SystemExit(f"마커 못 찾음: {marker}")
    return text.replace(marker, insert + marker, 1)

css = r"""
/* PATCH: recent-analysis-card + knowledge-map */
.recent-card-title {font-weight:900; color:#172033; font-size:15px; line-height:1.35;}
.recent-card-meta {color:#64748b; font-size:12px; margin-top:6px; line-height:1.45;}
.map-mini-card {
    background:#ffffff;
    border:1px solid #e5edf8;
    border-radius:18px;
    padding:16px;
    box-shadow:0 8px 20px rgba(15,23,42,0.04);
    min-height:118px;
}
.map-mini-title {font-weight:900; color:#172033; font-size:16px;}
.map-mini-meta {color:#64748b; font-size:13px; margin-top:6px;}
.toc-box {
    background:#ffffff;
    border:1px solid #e5edf8;
    border-radius:18px;
    padding:16px 18px;
    margin:10px 0;
}
.toc-title {font-weight:900; color:#172033;}
.toc-meta {color:#64748b; font-size:13px; margin-top:5px;}
.pkm-info-box {
    background:#eff6ff;
    border:1px solid #dbeafe;
    border-radius:18px;
    padding:16px 18px;
    margin:12px 0 20px 0;
    color:#1d4ed8;
    line-height:1.65;
    font-size:14px;
    font-weight:650;
}
.pkm-info-box b {color:#172033; font-weight:900;}
.pkm-sidebar-card {
    background:#ffffff;
    border:1px solid #e5edf8;
    border-radius:18px;
    padding:14px 16px;
    margin:8px 0;
    box-shadow:0 6px 16px rgba(15,23,42,0.035);
}
.pkm-sidebar-title {font-weight:900; color:#172033; font-size:15px;}
.pkm-sidebar-meta {color:#64748b; font-size:12px; margin-top:4px;}
.pkm-section-pill {
    display:inline-block;
    background:#eaf2ff;
    color:#2563eb;
    border-radius:999px;
    padding:5px 10px;
    font-size:12px;
    font-weight:900;
    margin:2px 4px 2px 0;
}
"""
s = add_once(s, "</style>", css, "PATCH: recent-analysis-card + knowledge-map")

if '"🧠 지식 맵",' not in s:
    s = s.replace('"🗂️ 지식 아카이브",\n            "🕘 최근 검색 기록",',
                  '"🗂️ 지식 아카이브",\n            "🧠 지식 맵",\n            "🕘 최근 검색 기록",')
    print("메뉴 추가: 🧠 지식 맵")

helpers = r'''
# -----------------------------
# PATCH: Display / Recent Cards / Knowledge Map
# -----------------------------
def display_source_label(value):
    value = str(value or "")
    if value.startswith("pasted://"):
        return "붙여넣은 글"
    return value or "-"


def get_all_knowledge_items():
    items = []

    for item in st.session_state.get("archive_notes", []):
        items.append({
            "kind": "지식 메모",
            "title": item.get("title", "저장 메모"),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "tags": item.get("tags", []),
            "date": item.get("saved_at", ""),
            "memo": item.get("note", ""),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": len(items),
        })

    for item in st.session_state.get("saved_analyses", []):
        items.append({
            "kind": "분석 결과",
            "title": item.get("title", "저장 분석"),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "tags": item.get("tags", []),
            "date": item.get("saved_at", ""),
            "memo": item.get("memo", ""),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": len(items),
        })

    return items


def infer_large_category(item):
    tags = " ".join([str(t) for t in item.get("tags", [])])
    title = str(item.get("title", ""))
    text = f"{tags} {title}"
    if any(word in text for word in ["뉴스", "기사", "정치", "경제", "사회", "국제"]):
        return "뉴스/이슈"
    if any(word in text for word in ["정책", "지원", "청년", "정부", "공공", "신청"]):
        return "정책/지원사업"
    if any(word in text for word in ["맛집", "여행", "후기", "리뷰", "카페", "숙소"]):
        return "후기/리뷰"
    if any(word in text for word in ["공부", "취업", "SQL", "PM", "자격증", "과제"]):
        return "공부/취업"
    return "기타"


def infer_middle_category(item):
    tags = [str(t).replace("#", "").strip() for t in item.get("tags", []) if str(t).strip()]
    if tags:
        return tags[0]
    return item.get("kind", "기타")


def date_color_group(date_text):
    date_text = str(date_text or "")[:10]
    try:
        from datetime import datetime
        saved = datetime.strptime(date_text, "%Y-%m-%d")
        diff = (datetime.now() - saved).days
    except Exception:
        return "날짜 없음"
    if diff <= 1:
        return "오늘/어제"
    if diff <= 7:
        return "최근 7일"
    if diff <= 30:
        return "최근 30일"
    return "오래된 기록"


def restore_item_from_knowledge(item):
    if item.get("kind") == "분석 결과" and item.get("raw_item", {}).get("result"):
        st.session_state.last_result = item.get("raw_item", {}).get("result")
        st.session_state.last_final_url = item.get("url", "")
        st.session_state.last_text = ""
        st.session_state.show_result = True
        st.session_state.result_closed = False
        st.session_state["knowledge_item_restored"] = True
    else:
        st.session_state["knowledge_selected_note"] = item


def render_recent_analysis_cards(limit=5):
    history = st.session_state.get("search_history", [])[:limit]
    if not history:
        st.caption("아직 최근 분석 기록이 없어요.")
        return

    st.markdown("### 🕘 최근 분석 5개")
    st.caption("최근 검색 기록으로 이동하지 않아도 여기서 바로 다시 열 수 있어요.")

    cols = st.columns(len(history))
    for i, item in enumerate(history):
        with cols[i]:
            with st.container(border=True):
                title = item.get("title", "제목 없음")
                score = item.get("score", 0)
                mode = item.get("input_mode", "링크로 조회하기")
                source = display_source_label(item.get("url", ""))
                cache_key = item.get("cache_key") or f'{item.get("url", "")}::{item.get("content_type", "unknown")}'

                st.markdown(f'<div class="recent-card-title">{title}</div>', unsafe_allow_html=True)
                st.markdown(
                    f'<div class="recent-card-meta">{item.get("time","")}<br>{mode}<br>{score}점 · {source}</div>',
                    unsafe_allow_html=True,
                )
                st.button(
                    "다시 보기",
                    key=f"recent_card_restore_{i}_{cache_key}",
                    width="stretch",
                    on_click=restore_analysis_from_history,
                    args=(cache_key,),
                )


def render_knowledge_map_page():
    st.markdown("## 🧠 지식 맵")
    st.markdown(
        """
        <div class="pkm-info-box">
        💡 <b>지식 맵 사용법</b><br>
        목차는 원노트처럼 대분류 → 중분류 → 문서 흐름으로 보고, 보드는 노션처럼 태그·기간·점수로 필터링해요.<br>
        태그 마인드맵은 옵시디언처럼 비슷한 태그가 어떻게 연결되는지 보는 공간이에요. 카드를 누르면 분석 결과를 다시 열 수 있어요.
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.get("knowledge_item_restored"):
        st.success("지식 맵에서 선택한 분석 결과를 불러왔어요. 왼쪽 메뉴의 📊 분석 결과에서 확인할 수 있어요.")
        st.session_state["knowledge_item_restored"] = False

    items = get_all_knowledge_items()
    if not items:
        st.info("아직 지식 맵에 표시할 저장 메모나 분석결과가 없어요.")
        return

    total_items = len(items)
    total_tags = sorted({str(tag).replace("#", "").strip() for item in items for tag in item.get("tags", []) if str(tag).strip()})
    avg_score = round(sum(int(item.get("score", 0) or 0) for item in items) / total_items, 1)

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("저장 항목", f"{total_items}개")
    with m2:
        st.metric("태그 수", f"{len(total_tags)}개")
    with m3:
        st.metric("평균 신뢰도", f"{avg_score}점")

    tab1, tab2, tab3 = st.tabs(["📚 원노트 목차", "🧩 노션 보드", "🕸️ 태그 마인드맵"])

    with tab1:
        st.markdown("### 📚 원노트식 목차")
        st.markdown(
            """
            <div class="pkm-info-box">
            📒 <b>원노트식 구조</b><br>
            날짜만 나열하지 않고, 대분류 → 중분류 → 문서 순서로 정리해요. 문서 버튼을 누르면 분석 결과는 다시 열리고, 지식 메모는 아래에 미리보기로 보여요.
            </div>
            """,
            unsafe_allow_html=True,
        )

        selected_large = st.selectbox(
            "대분류 필터",
            ["전체"] + sorted({infer_large_category(item) for item in items}),
            key="knowledge_toc_large_filter",
        )

        toc_items = items
        if selected_large != "전체":
            toc_items = [item for item in toc_items if infer_large_category(item) == selected_large]

        grouped = {}
        for item in toc_items:
            large = infer_large_category(item)
            middle = infer_middle_category(item)
            grouped.setdefault(large, {}).setdefault(middle, []).append(item)

        for large, middle_groups in sorted(grouped.items()):
            with st.expander(f"📒 {large} · {sum(len(v) for v in middle_groups.values())}개", expanded=True):
                for middle, group in sorted(middle_groups.items()):
                    st.markdown(f'<span class="pkm-section-pill">📑 {middle}</span>', unsafe_allow_html=True)
                    for item in group:
                        tag_text = ", ".join(item.get("tags", [])) or "태그 없음"
                        st.markdown(
                            f"""
                            <div class="toc-box">
                                <div class="toc-title">{item.get("title", "제목 없음")}</div>
                                <div class="toc-meta">{item.get("kind")} · {item.get("score", 0)}점 · {display_source_label(item.get("url", ""))}</div>
                                <div class="toc-meta">태그: {tag_text}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        st.button(
                            "열기",
                            key=f"knowledge_toc_open_{large}_{middle}_{item.get('title','')}_{item.get('date','')}",
                            width="stretch",
                            on_click=restore_item_from_knowledge,
                            args=(item,),
                        )

        selected_note = st.session_state.get("knowledge_selected_note")
        if selected_note:
            with st.expander("📝 선택한 지식 메모 미리보기", expanded=True):
                st.markdown(f"**{selected_note.get('title', '제목 없음')}**")
                st.caption(f"{selected_note.get('kind')} · {selected_note.get('score', 0)}점 · {display_source_label(selected_note.get('url', ''))}")
                st.markdown(selected_note.get("memo", "메모 내용이 없어요.")[:2500])

    with tab2:
        st.markdown("### 🧩 노션식 보드")
        st.markdown(
            """
            <div class="pkm-info-box">
            🧩 <b>노션식 보드</b><br>
            점수별로만 보는 게 아니라, 대분류·태그·기간·최소 점수로 필터를 걸어서 지금 필요한 자료만 모아볼 수 있어요.
            </div>
            """,
            unsafe_allow_html=True,
        )

        f1, f2, f3, f4 = st.columns(4)
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
            board_items = [item for item in board_items if board_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]]
        if board_date != "전체":
            board_items = [item for item in board_items if date_color_group(item.get("date", "")) == board_date]
        board_items = [item for item in board_items if int(item.get("score", 0) or 0) >= min_score]

        col_names = ["뉴스/이슈", "정책/지원사업", "후기/리뷰", "공부/취업", "기타"]
        board_cols = st.columns(len(col_names))

        for col, name in zip(board_cols, col_names):
            with col:
                st.markdown(f"#### {name}")
                group = [item for item in board_items if infer_large_category(item) == name]
                if not group:
                    st.caption("비어 있음")
                for item in group[:12]:
                    tags = ", ".join(item.get("tags", [])[:3]) or "태그 없음"
                    st.markdown(
                        f"""
                        <div class="map-mini-card">
                            <div class="map-mini-title">{item.get("title", "제목 없음")}</div>
                            <div class="map-mini-meta">{item.get("kind")} · {item.get("score", 0)}점 · {date_color_group(item.get("date", ""))}</div>
                            <div class="map-mini-meta">{tags}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    st.button(
                        "열기",
                        key=f"knowledge_board_open_{name}_{item.get('title','')}_{item.get('date','')}",
                        width="stretch",
                        on_click=restore_item_from_knowledge,
                        args=(item,),
                    )

    with tab3:
        st.markdown("### 🕸️ 옵시디언식 태그 마인드맵")
        st.markdown(
            """
            <div class="pkm-info-box">
            🕸️ <b>태그 마인드맵</b><br>
            지금은 태그 사용 빈도를 기준으로 연결해요. 날짜 색상과 유사 태그 묶음은 다음 단계에서 더 정교하게 확장할 수 있어요.
            </div>
            """,
            unsafe_allow_html=True,
        )
        if not total_tags:
            st.info("아직 태그가 없어서 마인드맵을 만들 수 없어요.")
        else:
            import math
            import pandas as pd
            import plotly.express as px

            nodes = [{"label": "TrustLens 지식", "x": 0, "y": 0, "type": "center", "size": 28}]
            edges = []

            tag_counts = {tag: 0 for tag in total_tags}
            for item in items:
                for tag in item.get("tags", []):
                    clean = str(tag).replace("#", "").strip()
                    if clean:
                        tag_counts[clean] = tag_counts.get(clean, 0) + 1

            top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:16]
            n = max(len(top_tags), 1)

            for idx, (tag, count) in enumerate(top_tags):
                angle = 2 * math.pi * idx / n
                x = math.cos(angle) * 2.2
                y = math.sin(angle) * 2.2
                nodes.append({"label": f"#{tag}", "x": x, "y": y, "type": "tag", "size": 14 + count * 3})
                edges.append({"x": 0, "y": 0, "x2": x, "y2": y})

            edge_fig_data = []
            for e in edges:
                edge_fig_data.append(dict(x=e["x"], y=e["y"], x2=e["x2"], y2=e["y2"]))

            df_nodes = pd.DataFrame(nodes)
            df_nodes["date_group"] = "태그 묶음"
            fig = px.scatter(
                df_nodes,
                x="x",
                y="y",
                text="label",
                size="size",
                color="date_group",
                hover_name="label",
                size_max=42,
            )

            for e in edge_fig_data:
                fig.add_shape(
                    type="line",
                    x0=e["x"],
                    y0=e["y"],
                    x1=e["x2"],
                    y1=e["y2"],
                    line=dict(width=1, color="#dbeafe"),
                )

            fig.update_traces(textposition="bottom center")
            fig.update_layout(
                height=620,
                showlegend=False,
                xaxis=dict(visible=False),
                yaxis=dict(visible=False),
                plot_bgcolor="#ffffff",
                paper_bgcolor="#ffffff",
                margin=dict(l=10, r=10, t=20, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)

'''
s = add_once(s, "# -----------------------------\n# Menu Pages", helpers, "PATCH: Display / Recent Cards / Knowledge Map")

# 분석 결과 탭에 최근 5개 카드 추가
old = '''if menu == "📊 분석 결과":
    st.markdown("## 📊 분석 결과")
    if st.session_state.last_result:'''
new = '''if menu == "📊 분석 결과":
    st.markdown("## 📊 분석 결과")
    render_recent_analysis_cards(limit=5)
    st.divider()
    if st.session_state.last_result:'''
if old in s and "render_recent_analysis_cards(limit=5)" not in s:
    s = s.replace(old, new, 1)
    print("분석 결과 탭 최근 5개 카드 추가")

# 지식 맵 메뉴 페이지 추가
marker = '''if menu == "🕘 최근 검색 기록":'''
insert = '''if menu == "🧠 지식 맵":
    render_knowledge_map_page()
    st.stop()

'''
if marker in s and 'if menu == "🧠 지식 맵":' not in s:
    s = s.replace(marker, insert + marker, 1)
    print("지식 맵 페이지 추가")

# pasted:// 표시 개선
# display_source_label 함수는 위 helpers 블록 안에서 app_v2.py에 함께 추가되므로
# 여기서는 화면 표시 문구만 안전하게 바꾼다.
s = s.replace(
    'st.caption(f"분석 URL: {final_url}")',
    'st.caption(f"분석 출처: {display_source_label(final_url)}")'
)
s = s.replace(
    '- URL: {final_url or ""}',
    '- 출처: {display_source_label(final_url)}'
)
s = s.replace(
    '- URL/출처: {final_url or "붙여넣은 글"}',
    '- 출처: {display_source_label(final_url)}'
)
s = s.replace(
    '''                st.markdown(f"**URL:** {item.get('url', '')}")''',
    '''                st.markdown(f"**출처:** {display_source_label(item.get('url', ''))}")'''
)
s = s.replace(
    '<div class="history-meta">{item.get("url", "")}</div>',
    '<div class="history-meta">{display_source_label(item.get("url", ""))}</div>'
)

# 분석 안내 위치: placeholder 추가
old_note = '''    st.markdown("""
    <div class="info-note">
    💡 <b>정확한 답변이 도움이 됩니다</b><br>
    리뷰 글은 본인 경험, 가격, 메뉴, 사진, 재방문 의사를 중심으로 보고<br>
    정책 글은 공식 기관, 날짜, 신청 조건, 출처를 중심으로 분석해요.
    </div>
    """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)'''
new_note = '''    st.markdown("""
    <div class="info-note">
    💡 <b>정확한 답변이 도움이 됩니다</b><br>
    리뷰 글은 본인 경험, 가격, 메뉴, 사진, 재방문 의사를 중심으로 보고<br>
    정책 글은 공식 기관, 날짜, 신청 조건, 출처를 중심으로 분석해요.
    </div>
    """, unsafe_allow_html=True)
    analysis_status_slot = st.empty()
    st.markdown("</div>", unsafe_allow_html=True)'''
if old_note in s and "analysis_status_slot = st.empty()" not in s:
    s = s.replace(old_note, new_note, 1)
    print("분석 안내 위치 placeholder 추가")

# 버튼 누를 때 메시지 초기화
old_btn = '''if analyze_btn:
    if input_mode == "링크로 조회하기" and not url_input.strip():'''
new_btn = '''if analyze_btn:
    st.session_state["analysis_status_message"] = None
    if input_mode == "링크로 조회하기" and not url_input.strip():'''
if old_btn in s and 'st.session_state["analysis_status_message"] = None' not in s:
    s = s.replace(old_btn, new_btn, 1)

# cache info를 placeholder용 메시지로 변경
s = s.replace('st.info("같은 조건의 분석 결과가 있어서 저장된 결과를 다시 불러왔어요.")',
              'st.session_state["analysis_status_message"] = "같은 조건의 분석 결과가 있어서 저장된 결과를 다시 불러왔어요."')

# 분석 완료 메시지 1개만
old_success_point = '''                        st.session_state.show_result = True
                        st.session_state.result_closed = False'''
new_success_point = '''                        st.session_state.show_result = True
                        st.session_state.result_closed = False
                        st.session_state["analysis_status_message"] = "분석 완료했어요. 아래에서 신뢰도 결과와 지식 메모 초안을 확인할 수 있어요."'''
if old_success_point in s and '분석 완료했어요. 아래에서 신뢰도 결과' not in s:
    s = s.replace(old_success_point, new_success_point, 1)

# placeholder 렌더링
marker2 = '''if st.session_state.show_result and st.session_state.last_result:'''
insert2 = '''if st.session_state.get("analysis_status_message"):
    analysis_status_slot.info(st.session_state.get("analysis_status_message"))

'''
if marker2 in s and 'analysis_status_slot.info(st.session_state.get("analysis_status_message"))' not in s:
    s = s.replace(marker2, insert2 + marker2, 1)
    print("상태 안내 박스 위치 이동")

# CSS 오타 보정
s = s.replace('note-action-card h2,', '.note-action-card h2,')
# Streamlit 최신 버전 호환: width="stretch"를 지원하지 않는 환경에서는 use_container_width로 되돌린다.
s = s.replace('width="stretch",', 'use_container_width=True,')

p.write_text(s, encoding="utf-8")
print("✅ 안전 패치 완료")