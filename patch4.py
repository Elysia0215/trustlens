"""
PATCH 4:
1. Blue button fix - use unique CSS class via st.markdown wrapper
2. note_concept_links - auto-link concepts when saving memo
3. Concept finder - show note link count
"""
import re

SRC = "/Users/parkcy/Desktop/trustlens-mvp/app_v2.py"

with open(SRC, encoding="utf-8") as f:
    code = f.read()

# ─────────────────────────────────────────────
# FIX 1: Blue button — replace inline CSS + button with wrapper approach
# ─────────────────────────────────────────────
OLD_BTN = """        st.markdown(f'''<style>
        div[data-testid="column"]:first-child div[data-testid="stButton"] button {{
            background: linear-gradient(180deg,#3b82f6,#1d4ed8) !important;
            color: #fff !important;
            border: 1px solid #1d4ed8 !important;
            border-radius: 8px !important;
        }}
        div[data-testid="column"]:first-child div[data-testid="stButton"] button:hover {{
            background: linear-gradient(180deg,#2563eb,#1e3a8a) !important;
        }}
        </style>''', unsafe_allow_html=True)
        if st.button(
            "🔄 지식 메모 초안 다시 만들기",
            key=f"refresh_{draft_key}",
            type="secondary",
            use_container_width=True,
        ):"""

NEW_BTN = """        st.markdown('<div class="knowledge-draft-blue-button"></div>', unsafe_allow_html=True)
        if st.button(
            "🔄 지식 메모 초안 다시 만들기",
            key=f"refresh_{draft_key}",
            type="secondary",
            use_container_width=True,
        ):"""

if OLD_BTN in code:
    code = code.replace(OLD_BTN, NEW_BTN)
    print("FIX 1 applied: Blue button CSS fixed")
else:
    print("FIX 1 SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 2: note_concept_links — add to save_persisted_data
# ─────────────────────────────────────────────
OLD_SAVE = '        "tasks": st.session_state.get("tasks", []),\n    }'
NEW_SAVE = '        "tasks": st.session_state.get("tasks", []),\n        "note_concept_links": st.session_state.get("note_concept_links", []),\n    }'
if OLD_SAVE in code:
    code = code.replace(OLD_SAVE, NEW_SAVE)
    print("FIX 2a applied: note_concept_links in save_persisted_data")
else:
    print("FIX 2a SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 2b: add to init_state
# ─────────────────────────────────────────────
OLD_INIT = '        "tasks": persisted.get("tasks", []),\n    }'
NEW_INIT = '        "tasks": persisted.get("tasks", []),\n        "note_concept_links": persisted.get("note_concept_links", []),\n    }'
if OLD_INIT in code:
    code = code.replace(OLD_INIT, NEW_INIT)
    print("FIX 2b applied: note_concept_links in init_state")
else:
    print("FIX 2b SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 2c: auto-link concepts in save_note_to_archive
# ─────────────────────────────────────────────
OLD_NOTE_SAVE = '''    st.session_state.archive_notes.append(
        {
            "url": final_url or "",
            "title": result.get("archive_title", "TrustLens 메모"),
            "project": st.session_state.get("note_project_name", "기본 프로젝트"),
            "section": st.session_state.get("note_section_name", "일반"),
            "content_type": result.get("content_type", "unknown"),
            "score": result.get("trust_score", 0),
            "favorite": False,
            "tags": selected_tags,
            "note": note_text,
            "original_text": original_text if is_pasted_source else "",
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    st.session_state.note_saved = True
    st.session_state.show_result = True
    save_persisted_data()'''

NEW_NOTE_SAVE = '''    import uuid as _uuid
    note_id = str(_uuid.uuid4())[:8]
    note_title = result.get("archive_title", "TrustLens 메모")
    st.session_state.archive_notes.append(
        {
            "id": note_id,
            "url": final_url or "",
            "title": note_title,
            "project": st.session_state.get("note_project_name", "기본 프로젝트"),
            "section": st.session_state.get("note_section_name", "일반"),
            "content_type": result.get("content_type", "unknown"),
            "score": result.get("trust_score", 0),
            "favorite": False,
            "tags": selected_tags,
            "note": note_text,
            "original_text": original_text if is_pasted_source else "",
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    # 자동 concept 연결: 태그 + AI 핵심개념에서 추출
    _all_concepts = [
        c.get("name") if isinstance(c, dict) else str(c)
        for c in st.session_state.get("pkm_custom_concepts", []) if c
    ]
    _auto_concepts = result.get("key_concepts", result.get("concepts", []))
    _link_concepts = set()
    for tag in selected_tags:
        _link_concepts.add(str(tag).replace("#", "").strip())
    for ac in _auto_concepts:
        if isinstance(ac, str):
            _link_concepts.add(ac.strip())
    for cc in _all_concepts:
        if cc and cc.lower() in note_text.lower():
            _link_concepts.add(cc)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    links = st.session_state.setdefault("note_concept_links", [])
    for concept in _link_concepts:
        if concept:
            links.append({"note_id": note_id, "concept": concept, "linked_at": now_str})
    st.session_state.note_saved = True
    st.session_state.show_result = True
    save_persisted_data()'''

if OLD_NOTE_SAVE in code:
    code = code.replace(OLD_NOTE_SAVE, NEW_NOTE_SAVE)
    print("FIX 2c applied: auto concept linking in save_note_to_archive")
else:
    print("FIX 2c SKIP: pattern not found")

# ─────────────────────────────────────────────
# FIX 3: concept finder — show concept link count from note_concept_links
# ─────────────────────────────────────────────
OLD_CONCEPT_META = '                            <div class="pkm-concept-meta">{get_concept_folder(concept)} · {len(docs)}개 연결</div>'
NEW_CONCEPT_META = '''                            <div class="pkm-concept-meta">{get_concept_folder(concept)} · {len(docs)}개 연결 · {sum(1 for l in st.session_state.get("note_concept_links",[]) if l.get("concept")==concept)}개 메모</div>'''
if OLD_CONCEPT_META in code:
    code = code.replace(OLD_CONCEPT_META, NEW_CONCEPT_META)
    print("FIX 3 applied: concept link count in finder")
else:
    print("FIX 3 SKIP: pattern not found")

# write
with open(SRC, "w", encoding="utf-8") as f:
    f.write(code)
print("Done writing file.")

# syntax check
import ast
with open(SRC, encoding="utf-8") as f:
    src = f.read()
try:
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
