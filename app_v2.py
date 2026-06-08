import streamlit as st
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
import json
import re
from urllib.parse import urlparse, urljoin
from datetime import datetime
from pathlib import Path
import pandas as pd
import plotly.express as px

load_dotenv()

st.set_page_config(page_title="JIUM", page_icon="🌍", layout="wide")

DATA_FILE = Path("trustlens_data.json")

# ── 원문 추출/보관 길이 상수 (지식 AI deep-read 품질 좌우) ──
MAX_EXTRACT_TEXT_CHARS = 20000        # extract_text() / 붙여넣기 본문 최대 길이
MAX_ORIGINAL_TEXT_CHARS = 20000       # archive_notes.original_text 저장 한도
MAX_NOTE_INLINE_ORIGINAL_CHARS = 12000  # 메모 본문에 직접 붙이는 "원문 보관" 섹션 한도
MAX_ANALYZE_CHARS = 6000              # 신뢰도 분석 API에 보내는 길이(비용 제한)
EXTRACTION_VERSION = "v4-extract"     # 추출/분석 로직 버전 — 캐시 키에 포함해 구버전 캐시 무효화 (본문 추출 개선: Tistory 잡영역 제거 + study fallback)

# ── Supabase 영구 저장 (설정 없으면 로컬 파일 폴백 — 기존 동작 유지) ──
APP_BUILD = "2026-06-04.22"  # 배포 식별용
_SB_DEBUG = {"stage": "init", "error": None, "url_set": False, "key_set": False}


@st.cache_resource(show_spinner=False)
def _sb_client():
    """st.secrets['supabase'] 설정 + supabase 패키지 있으면 클라이언트 반환, 없으면 None."""
    try:
        try:
            _cfg = st.secrets.get("supabase", {})
        except Exception as _e0:
            _SB_DEBUG.update(stage="no_secrets", error=f"{type(_e0).__name__}: {_e0}")
            return None
        _url, _key = _cfg.get("url"), _cfg.get("key")
        _SB_DEBUG["url_set"] = bool(_url)
        _SB_DEBUG["key_set"] = bool(_key)
        if not _url or not _key:
            _SB_DEBUG.update(stage="missing_url_or_key")
            return None
        try:
            from supabase import create_client
        except Exception as _e1:
            _SB_DEBUG.update(stage="import_failed", error=f"{type(_e1).__name__}: {_e1}")
            return None
        _c = create_client(_url, _key)
        _SB_DEBUG.update(stage="ok", error=None)
        return _c
    except Exception as _e:
        _SB_DEBUG.update(stage="create_failed", error=f"{type(_e).__name__}: {_e}")
        return None


def _sb_load_status():
    """Supabase에서 main 행을 읽고 상태를 함께 반환.
    반환: (status, data)
      - "ok"      : 행이 있고 데이터 정상 (data=dict, 빈 dict일 수도 있음)
      - "empty"   : 연결됐지만 main 행이 아예 없음 (최초 시드 필요)
      - "error"   : 연결됐지만 읽기 실패 (절대 덮어쓰면 안 됨)
      - "noclient": Supabase 미설정/미연결 (오프라인)
    """
    _c = _sb_client()
    if not _c:
        return ("noclient", None)
    import time as _time
    _last_err = None
    for _attempt in range(3):  # transient 네트워크/콜드스타트 대비 재시도
        try:
            _r = _c.table("jium_store").select("data").eq("id", "main").limit(1).execute()
            _rows = _r.data or []
            if _rows:
                return ("ok", _rows[0].get("data") or {})
            return ("empty", None)
        except Exception as _e:
            _last_err = _e
            _time.sleep(0.4 * (_attempt + 1))
    _SB_DEBUG.update(stage="load_failed", error=f"{type(_last_err).__name__}: {_last_err}")
    return ("error", None)


def _sb_load():
    """호환용: 정상이면 data, 아니면 None."""
    _status, _data = _sb_load_status()
    return _data if _status == "ok" else None


def _sb_save(data):
    # ⛔ 읽기 실패한 세션에서는 저장 금지 — 옛 데이터로 클라우드를 덮어쓰는 사고 방지.
    #    단, transient 1회 실패로 세션이 영구 차단되지 않도록 저장 직전 1회 재검증 후 자동 해제.
    try:
        if st.session_state.get("_persist_blocked"):
            _st, _ = _sb_load_status()
            if _st in ("ok", "empty"):
                st.session_state["_persist_blocked"] = False  # 클라우드 다시 읽히면 차단 해제
            else:
                _SB_DEBUG.update(stage="save_blocked",
                                 error="클라우드 읽기 실패 지속 → 저장 차단(데이터 보호)")
                return False
    except Exception:
        pass
    _c = _sb_client()
    if not _c:
        return False
    try:
        from datetime import timezone as _tz
        _c.table("jium_store").upsert({
            "id": "main",
            "data": data,
            "updated_at": datetime.now(_tz.utc).isoformat(),
        }).execute()
        _SB_DEBUG.update(stage="save_ok", error=None)
        return True
    except Exception as _e:
        _SB_DEBUG.update(stage="save_failed", error=f"{type(_e).__name__}: {_e}")
        return False


def _read_local_file():
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_persisted_data():
    _status, _data = _sb_load_status()
    try:
        st.session_state["_persist_source"] = _status
        st.session_state["_persist_blocked"] = False
    except Exception:
        pass

    if _status == "ok":
        # 클라우드가 진실의 원천 — 절대 로컬로 덮어쓰지 않음
        return _data
    if _status == "empty":
        # 행이 정말 없을 때만 1회 시드 이관
        _local = _read_local_file()
        if _local:
            _sb_save(_local)
        return _local
    if _status == "error":
        # 연결됐지만 읽기 실패 → 이번 세션은 저장 차단(클라우드 보호), 화면엔 로컬로 임시 표시
        try:
            st.session_state["_persist_blocked"] = True
        except Exception:
            pass
        return _read_local_file()
    # noclient(오프라인) → 로컬만 사용
    return _read_local_file()


def _clean_text_value(value):
    """JSON에 숫자/None이 섞여도 화면 표시용 문자열 필드는 안전하게 다룬다."""
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:
        pass
    if isinstance(value, str):
        text = value.strip()
        return "" if text.lower() in {"nan", "none", "null", "nat"} else text
    return str(value)


def _clean_number_value(value, default=0):
    if _clean_text_value(value) == "":
        return default
    try:
        return int(value)
    except Exception:
        try:
            return float(value)
        except Exception:
            return default


def _clean_editor_records(records, text_fields=(), number_fields=None):
    number_fields = number_fields or {}
    cleaned = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        for field in text_fields:
            next_item[field] = _clean_text_value(next_item.get(field))
        for field, default in number_fields.items():
            next_item[field] = _clean_number_value(next_item.get(field), default)
        cleaned.append(next_item)
    return cleaned


def _clean_editor_dataframe(df, text_columns=()):
    next_df = df.copy()
    for col in text_columns:
        if col in next_df.columns:
            next_df[col] = next_df[col].map(_clean_text_value)
    return next_df


def _clean_text_fields(items, fields):
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        for field in fields:
            if field in next_item:
                next_item[field] = _clean_text_value(next_item.get(field))
        if "tags" in next_item:
            tags = next_item.get("tags") or []
            next_item["tags"] = [_clean_text_value(tag).strip() for tag in tags if _clean_text_value(tag).strip()]
        cleaned.append(next_item)
    return cleaned


def normalize_persisted_data(persisted):
    """오래된/깨진 JSON 값 때문에 앱 시작 렌더링이 죽지 않도록 표시 필드 정규화."""
    if not isinstance(persisted, dict):
        return {}
    data = dict(persisted)
    data["archive_notes"] = _clean_text_fields(data.get("archive_notes", []), [
        "id", "title", "note", "memo", "full_text", "original_text", "saved_at",
        "updated_at", "project", "section", "source", "url",
    ])
    data["saved_analyses"] = _clean_text_fields(data.get("saved_analyses", []), [
        "id", "title", "summary", "saved_at", "updated_at", "url", "final_url",
    ])
    data["search_history"] = _clean_text_fields(data.get("search_history", []), [
        "title", "url", "saved_at", "created_at",
    ])
    data["projects"] = _clean_text_fields(data.get("projects", []), [
        "id", "name", "description", "category", "status", "priority",
        "owner", "start_date", "due_date", "created_at", "updated_at",
    ])
    data["projects"] = [p for p in data["projects"] if p.get("name")]
    data["project_sections"] = _clean_text_fields(data.get("project_sections", []), [
        "id", "project", "name", "title", "description", "created_at", "updated_at",
    ])
    data["project_steps"] = _clean_text_fields(data.get("project_steps", []), [
        "id", "project", "section", "name", "title", "status", "created_at", "updated_at",
    ])
    data["tasks"] = _clean_text_fields(data.get("tasks", []), [
        "id", "title", "description", "project", "status", "priority",
        "due_date", "created_at", "updated_at",
    ])
    data["tasks"] = [t for t in data["tasks"] if t.get("title")]
    data["note_concept_links"] = _clean_text_fields(data.get("note_concept_links", []), [
        "id", "note_id", "concept", "project", "created_at", "updated_at",
    ])
    data["entities"] = _clean_text_fields(data.get("entities", []), [
        "id", "type", "name", "description", "created_at", "updated_at",
    ])
    data["entities"] = [e for e in data["entities"] if e.get("name") or e.get("id")]
    data["relations"] = _clean_text_fields(data.get("relations", []), [
        "id", "source_id", "target_id", "source_name", "target_name",
        "source_type", "target_type", "type", "relation", "relation_type",
        "created_at", "updated_at",
    ])
    data["folders"] = _clean_text_fields(data.get("folders", []), [
        "id", "name", "type", "parent", "created_at", "updated_at",
    ])
    data["pkm_custom_concepts"] = [
        {**c, "name": _clean_text_value(c.get("name")), "folder": _clean_text_value(c.get("folder")),
         "description": _clean_text_value(c.get("description"))}
        if isinstance(c, dict) else _clean_text_value(c)
        for c in data.get("pkm_custom_concepts", [])
        if c
    ]
    data["pkm_custom_concepts"] = [
        c for c in data["pkm_custom_concepts"]
        if (_clean_text_value(c.get("name")) if isinstance(c, dict) else _clean_text_value(c))
    ]
    data["hidden_concepts"] = [_clean_text_value(c) for c in data.get("hidden_concepts", []) if _clean_text_value(c)]
    data["merge_dismissed"] = [_clean_text_value(c) for c in data.get("merge_dismissed", []) if _clean_text_value(c)]
    data["excluded_concepts_log"] = _clean_text_fields(data.get("excluded_concepts_log", []), [
        "name", "reason", "created_at",
    ])
    data["pkm_concept_folders"] = {
        _clean_text_value(k): (_clean_text_value(v) or "내 개념")
        for k, v in (data.get("pkm_concept_folders", {}) or {}).items()
        if _clean_text_value(k)
    }
    _custom_options = {}
    for k, vals in (data.get("custom_select_options", {}) or {}).items():
        key = _clean_text_value(k)
        if not key:
            continue
        val_list = vals if isinstance(vals, (list, tuple, set)) else [vals]
        _custom_options[key] = [_clean_text_value(v) for v in val_list if _clean_text_value(v)]
    data["custom_select_options"] = _custom_options
    return data

def _flash(msg: str, icon: str = "✅"):
    """rerun 후에도 보이는 알림 큐에 메시지 추가.
    _flash() 직후 st.rerun()을 하면 메시지가 화면에 그려지기 전에
    새로고침돼 사라지므로, 큐에 저장했다가 다음 실행 때 toast로 표시한다."""
    st.session_state.setdefault("_flash_queue", []).append((str(msg), icon))


def _render_flash():
    """큐에 쌓인 알림을 toast로 표시하고 비운다. 매 실행 상단에서 1회 호출."""
    for _msg, _icon in st.session_state.pop("_flash_queue", []):
        try:
            st.toast(_msg, icon=_icon)
        except Exception:
            st.success(_msg)


def render_collection_stepper(current_step, completed_steps=None, error_step=None):
    """새 메모·정보 수집 4단계 진행 표시(상단 고정 느낌)."""
    completed_steps = set(completed_steps or [])
    steps = [
        (1, "정보 가져오기"),
        (2, "원문 확인"),
        (3, "AI 정리"),
        (4, "지식 메모 저장"),
    ]
    chips = []
    for num, label in steps:
        if error_step == num:
            bg, color, fw, mark = "#fee2e2", "#b91c1c", "700", "⚠️ "
        elif num in completed_steps:
            bg, color, fw, mark = "#dbeafe", "#1d4ed8", "700", "✅ "
        elif num == current_step:
            bg, color, fw, mark = "#3b82f6", "#ffffff", "800", ""
        else:
            bg, color, fw, mark = "#f1f5f9", "#94a3b8", "500", ""
        chips.append(
            f'<span style="background:{bg};color:{color};padding:5px 14px;'
            f'border-radius:999px;font-weight:{fw};font-size:0.82rem;white-space:nowrap;">'
            f'{mark}{num} {label}</span>'
        )
    arrow = '<span style="color:#cbd5e1;align-self:center;">→</span>'
    joined = arrow.join(chips)
    pct = int(min(max(current_step, 1), 4) / 4 * 100)
    st.markdown(
        '<div style="position:sticky;top:0;z-index:99;background:#f8fafc;'
        'padding:8px 0 12px;border-bottom:1px solid #e2e8f0;margin-bottom:14px;">'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">'
        + joined +
        '</div>'
        f'<div style="height:6px;background:#e2e8f0;border-radius:999px;margin-top:10px;overflow:hidden;">'
        f'<div style="height:100%;width:{pct}%;background:linear-gradient(90deg,#3b82f6,#6366f1);"></div>'
        '</div></div>',
        unsafe_allow_html=True,
    )


def compute_collection_step():
    """현재 수집 플로우 단계/완료 단계 계산."""
    has_text = bool((st.session_state.get("last_text") or "").strip())
    has_result = bool(st.session_state.get("show_result") and st.session_state.get("last_result"))
    saved = bool(st.session_state.get("note_saved") or st.session_state.get("analysis_archive_saved"))
    if saved:
        return 4, [1, 2, 3]
    if has_result:
        return 3, [1, 2]
    if has_text:
        return 2, [1]
    return 1, []


# ══════════════════════════════════════════════════════════
# 노션식 Select / Multi-select 속성 관리 공통 컴포넌트
# data_editor의 SelectboxColumn은 셀 클릭 시 드롭다운이 펼쳐진다(노션과 동일).
# 다만 셀 안에서 "새 옵션 추가"는 Streamlit이 지원하지 않으므로,
# 표 위/옆에 이 컴포넌트를 두어 색상 칩 + 새 옵션 추가 UX를 제공한다.
# 향후 Entity DB / Project DB / Semantic Search에서도 재사용한다.
# ══════════════════════════════════════════════════════════
_SELECT_OPTION_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#ec4899", "#0ea5e9", "#14b8a6", "#f97316", "#6366f1",
]


def _option_color(value: str) -> str:
    """옵션 값 → 안정적인 색상 (노션처럼 값마다 고정 색)."""
    if not value:
        return "#94a3b8"
    return _SELECT_OPTION_PALETTE[sum(ord(c) for c in str(value)) % len(_SELECT_OPTION_PALETTE)]


def _render_option_chips(options):
    """옵션 목록을 색상 칩으로 렌더."""
    if not options:
        st.caption("아직 옵션이 없어요. 아래에서 새 옵션을 추가하세요.")
        return
    _chips = "".join(
        f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 10px;'
        f'border-radius:12px;font-size:12px;font-weight:600;color:#fff;'
        f'background:{_option_color(o)};">{o}</span>'
        for o in options
    )
    st.markdown(f'<div style="margin:4px 0 8px;">{_chips}</div>', unsafe_allow_html=True)


def render_select_property_editor(label, options, *, key,
                                  on_add=None, allow_add=True, help=None):
    """노션식 단일선택(Select) 속성 옵션 관리 UI.

    - 현재 옵션을 색상 칩으로 표시
    - 하단에서 새 옵션 추가 (추가 즉시 저장 + 토스트 + rerun)
    - on_add(new_value): 추가 시 호출되는 콜백(없으면 custom_select_options[key]에 저장)
    - 반환: 최신 옵션 리스트 (SelectboxColumn options 로 그대로 사용)
    """
    opts = list(dict.fromkeys([str(o) for o in options if str(o).strip()]))
    # 커스텀으로 추가된 옵션 병합
    _custom = st.session_state.get("custom_select_options", {}).get(key, [])
    for c in _custom:
        if c and c not in opts:
            opts.append(c)

    with st.expander(f"🎨 {label} 옵션 관리 ({len(opts)})", expanded=False):
        if help:
            st.caption(help)
        _render_option_chips(opts)
        if allow_add:
            _c1, _c2 = st.columns([3, 1])
            with _c1:
                _new_val = st.text_input(
                    f"새 {label} 추가", key=f"opt_add_{key}",
                    label_visibility="collapsed",
                    placeholder=f"새 {label} 입력 후 추가",
                )
            with _c2:
                if st.button("➕ 추가", key=f"opt_add_btn_{key}", use_container_width=True):
                    _nv = _new_val.strip()
                    if not _nv:
                        st.warning("값을 입력해주세요.")
                    elif _nv in opts:
                        st.warning("이미 있는 옵션이에요.")
                    else:
                        if on_add is not None:
                            on_add(_nv)
                        else:
                            _store = st.session_state.setdefault("custom_select_options", {})
                            _store.setdefault(key, []).append(_nv)
                            save_persisted_data()
                        _flash(f"새 {label} '{_nv}'를 추가했어요.", "✅")
                        st.rerun()
    return opts


def render_multiselect_property_editor(label, options, *, key,
                                       on_add=None, allow_add=True, help=None):
    """노션식 다중선택(Multi-select) 속성 옵션 관리 UI.
    내부 동작은 select와 동일하나 의미상 구분해 재사용성을 높인다."""
    return render_select_property_editor(
        label, options, key=key, on_add=on_add, allow_add=allow_add, help=help
    )


# ══════════════════════════════════════════════════════════
# Entity Layer — 엔터티 생성/조회/관계 통합 헬퍼
# 미래 PostgreSQL 이전 시 이 함수들의 내부만 바꾸면 UI는 그대로 유지됨.
# ══════════════════════════════════════════════════════════
def _new_id(prefix: str) -> str:
    import uuid as _u
    return f"{prefix}_{_u.uuid4().hex[:8]}"


def create_project(name, description="", category="기타", status="예정",
                   priority="보통", start_date="", due_date=""):
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    proj = {
        "id": _new_id("project"), "user_id": "local_user",
        "name": name.strip(), "description": description.strip(),
        "category": category, "status": status, "priority": priority,
        "owner": "채연", "start_date": start_date, "due_date": due_date,
        "progress": 0, "created_at": _now, "updated_at": _now, "deleted_at": None,
    }
    st.session_state.setdefault("projects", []).append(proj)
    save_persisted_data()
    return proj


def create_task(title, project="", status="시작 전", priority="보통",
                due_date="", summary="", source_note_id="", source_note_title=""):
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _proj_obj = next((p for p in st.session_state.get("projects", []) if p.get("name") == project), {})
    task = {
        "id": _new_id("task"), "user_id": "local_user",
        "title": title.strip(), "project_id": _proj_obj.get("id", ""), "project": project,
        "status": status, "priority": priority, "due_date": due_date,
        "summary": summary.strip(), "linked_note_ids": [], "linked_concepts": [],
        "source_note_id": source_note_id, "source_note_title": source_note_title,
        "created_at": _now, "updated_at": _now, "deleted_at": None,
    }
    st.session_state.setdefault("tasks", []).append(task)
    save_persisted_data()
    return task


def create_concept(name, folder="내 개념", description="", aliases=None):
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    con = {
        "name": name.strip(), "folder": folder or "내 개념",
        "description": description.strip(), "aliases": aliases or [],
        "created_at": _now,
    }
    st.session_state.setdefault("pkm_custom_concepts", []).append(con)
    _fds = st.session_state.setdefault("pkm_concept_folders", {})
    _fds[name.strip()] = folder or "내 개념"
    save_persisted_data()
    return con


def create_folder(folder_name):
    _flist = st.session_state.setdefault("folders", [])
    if folder_name.strip() and folder_name.strip() not in _flist:
        _flist.append(folder_name.strip())
    save_persisted_data()
    return folder_name.strip()


def create_memo(title, note="", project="기본 프로젝트", section="일반",
                tags=None, original_text="", url="", concepts=None):
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    note_id = _new_id("note")[5:]  # 짧은 id
    memo = {
        "id": note_id, "url": url,
        "title": title.strip(), "project": project, "section": section,
        "content_type": "manual_note", "score": 0, "favorite": False,
        "tags": tags or [], "note": note, "original_text": original_text,
        "saved_at": _now,
    }
    st.session_state.setdefault("archive_notes", []).append(memo)
    # 개념 자동 연결 (품질 게이트 통과한 개념만)
    _links = st.session_state.setdefault("note_concept_links", [])
    _clean_concepts = filter_concepts(concepts)
    memo["concepts"] = _clean_concepts
    for _c in _clean_concepts:
        _links.append({"note_id": note_id, "concept": _c, "linked_at": _now})
    save_persisted_data()
    return memo


def get_relations(entity_name: str):
    """해당 엔터티가 source 또는 target인 모든 관계 반환."""
    return [r for r in st.session_state.get("relations", [])
            if r.get("source_name") == entity_name or r.get("target_name") == entity_name]


def _backfill_db_fields():
    """PostgreSQL 마이그레이션 대비: 모든 레코드에 표준 DB 필드 자동 보강.
    entity_id, parent_id, created_by, updated_by, status, priority,
    vector_id, embedding_status 를 누락 시 기본값으로 채운다."""
    import uuid as _bfuuid
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _ensure(rec, defaults):
        if not isinstance(rec, dict):
            return
        for k, v in defaults.items():
            if k not in rec:
                rec[k] = v() if callable(v) else v

    _common = {
        "entity_id": lambda: str(_bfuuid.uuid4())[:12],
        "parent_id": None,
        "created_by": "local_user",
        "updated_by": "local_user",
        "vector_id": None,
        "embedding_status": "pending",  # pending | done | skipped
    }

    for _n in st.session_state.get("archive_notes", []):
        _ensure(_n, {**_common, "status": "saved", "priority": "보통"})
    for _t in st.session_state.get("tasks", []):
        _ensure(_t, {**_common, "status": _t.get("status", "시작 전") if isinstance(_t, dict) else "시작 전", "priority": _t.get("priority", "보통") if isinstance(_t, dict) else "보통"})
    for _p in st.session_state.get("projects", []):
        _ensure(_p, {**_common, "status": _p.get("status", "진행중") if isinstance(_p, dict) else "진행중", "priority": _p.get("priority", "보통") if isinstance(_p, dict) else "보통"})
    for _c in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(_c, dict):
            _ensure(_c, {**_common, "status": "active", "priority": "보통"})
    for _e in st.session_state.get("entities", []):
        _ensure(_e, {**_common, "status": "active", "priority": "보통"})
    for _r in st.session_state.get("relations", []):
        _ensure(_r, {"entity_id": lambda: str(_bfuuid.uuid4())[:12], "created_by": "local_user",
                     "updated_by": "local_user", "vector_id": None, "embedding_status": "skipped"})


# ── ⚙️ 설정 Control Center 기본값 ──────────────────────────────
APP_SETTINGS_DEFAULTS = {
    # 🧪 고급 모드 (전문가용 메뉴 노출)
    "show_advanced": False,
    # 🎨 화면
    "ui_density": "보통",          # 여유 / 보통 / 촘촘
    "ui_font_scale": "보통",       # 작게 / 보통 / 크게
    "ui_animations": True,
    # ✨ 인터페이스
    "ui_toasts": True,
    "ui_auto_expand": False,
    # 🤖 루미 페르소나
    "lumi_persona": "친구형",      # 친구형 / 분석가형 / 코치형 / 철학자형
    "lumi_guide_level": "보통",    # 적게 / 보통 / 자세히
    # 🧠 Second Brain 기능 토글
    "feat_auto_concepts": True,
    "feat_tfidf": True,
    "feat_related_notes": True,
    "feat_quality_gate": True,
    # 🔔 알림
    "notif_due": True,
    "notif_task_done": True,
    "notif_project_summary": True,
    # 📊 실험실 (베타 플래그)
    "beta_project_map": True,
    "beta_tfidf": True,
    "beta_alias": True,
    "beta_semantic_merge": False,
    "beta_philosophy": False,
}


def get_setting(key, default=None):
    """app_settings 단일 진입점. 기본값은 APP_SETTINGS_DEFAULTS에서 보강."""
    _s = st.session_state.get("app_settings", {}) or {}
    if key in _s:
        return _s[key]
    return APP_SETTINGS_DEFAULTS.get(key, default)


# ── 공통 액션 버튼 시스템 — "보기 → 다음 행동" 통일 ──────────────
_ACTION_SPECS = {
    "concept": [("📄 개념 상세", "entity"), ("🕸 관계 보기", "relmap"), ("🤖 브레인스토밍", "brain")],
    "project": [("📁 프로젝트 열기", "project"), ("🗺 프로젝트맵", "projmap"),
                ("✅ 작업 보기", "tasks"), ("🤖 브레인스토밍", "brain")],
    "note":    [("📖 노트 열기", "note"), ("🧠 지식맵", "map"),
                ("📁 프로젝트", "project"), ("🤖 브레인스토밍", "brain")],
    "task":    [("📁 프로젝트", "project"), ("📚 아카이브", "archive"), ("🤖 브레인스토밍", "brain")],
    "report":  [("🧠 개념 보기", "map"), ("📚 아카이브 보기", "archive"),
                ("📅 데일리 노트", "daily"), ("🤖 브레인스토밍", "brain")],
}


def _action_navigate(act, name=None, target_id=None, project=None):
    """액션 코드 → page/query_params/session_state 이동 (기존 방식 재사용)."""
    if act == "entity" and name:
        st.session_state["ep_jump_entity"] = name; st.query_params["page"] = "entity"
    elif act == "relmap" and name:
        st.session_state["rg_sel_node"] = name; st.query_params["page"] = "map"
    elif act == "projmap" and name:
        st.session_state["pm_sel_project"] = name; st.query_params["page"] = "map"
    elif act == "project":
        _p = project or name
        if _p:
            st.session_state["ep_jump_entity"] = _p
        st.query_params["page"] = "projects"
    elif act == "tasks":
        st.query_params["page"] = "tasks"
    elif act == "note":
        if target_id:
            st.session_state["archive_open_note_id"] = target_id
        st.query_params["page"] = "archive"
    elif act == "map":
        st.query_params["page"] = "map"
    elif act == "archive":
        st.query_params["page"] = "archive"
    elif act == "daily":
        st.query_params["page"] = "daily"
    elif act == "brain":
        st.query_params["page"] = "brain"


def render_action_buttons(context_type, target_name=None, target_id=None,
                          project=None, key_prefix="act", title="🚀 다음 행동"):
    """화면 공통 '다음 행동' 버튼 세트. context_type: concept/project/note/task/report."""
    _acts = _ACTION_SPECS.get(context_type, [])
    if not _acts:
        return
    if title:
        st.markdown(f"**{title}**")
    _cols = st.columns(len(_acts))
    for _i, (_label, _act) in enumerate(_acts):
        with _cols[_i]:
            if st.button(_label, key=f"{key_prefix}_{_act}", use_container_width=True):
                _action_navigate(_act, target_name, target_id, project)
                st.rerun()


# ── 테마 정의: 같은 데이터, 다른 세계관 ──
# 설정(Control Center)·홈 대시보드 양쪽에서 쓰여서 파일 상단에 정의.
# 각 테마: levels[(임계점, 레벨명, 이모지)] / gradient / shadow / 성장단계 이모지 / 5요소 라벨
_BRAIN_THEMES = {
    "default": {
        "name": "📚 기본",
        "levels": [(0,"입문","📄"),(40,"수집가","🗂️"),(120,"정리자","📚"),(300,"연결자","🔗"),(650,"기획자","📁"),(1300,"전문가","🧠"),(2600,"마스터","🏆")],
        "gradient": "linear-gradient(135deg,#1e3a8a,#3b82f6 70%,#60a5fa)",
        "shadow": "rgba(59,130,246,0.28)", "accent": "#3b82f6",
        "stages": [(10,"📄"),(50,"🗂️"),(120,"📚"),(300,"📚📁"),(10**9,"🧠📚📁")],
        "elements": [("📄","메모"),("🧠","개념"),("📁","프로젝트"),("🔗","관계"),("🏆","작업")],
    },
    "forest": {
        "name": "🌳 지식의 숲",
        "levels": [(0,"씨앗","🌱"),(40,"새싹","🌿"),(120,"나무","🌳"),(300,"숲","🌲🌳🌲"),(650,"도시","🏙️"),(1300,"행성","🪐"),(2600,"은하","🌌")],
        "gradient": "linear-gradient(135deg,#064e3b,#10b981 70%,#34d399)",
        "shadow": "rgba(16,185,129,0.25)", "accent": "#10b981",
        "stages": [(10,"🌱"),(50,"🌿"),(120,"🌳"),(300,"🌲🌳🌲"),(10**9,"🌲🌳🌲🌳🌲")],
        "elements": [("🍃","잎사귀"),("🌿","가지"),("🪵","줄기"),("🌱","뿌리"),("🍎","열매")],
    },
    "space": {
        "name": "🌌 우주 개척",
        "levels": [(0,"운석","☄️"),(40,"위성","🌑"),(120,"행성","🪐"),(300,"항성계","☀️"),(650,"성단","✨"),(1300,"은하","🌌"),(2600,"우주","🌠")],
        "gradient": "linear-gradient(135deg,#1e1b4b,#6366f1 70%,#a78bfa)",
        "shadow": "rgba(99,102,241,0.3)", "accent": "#6366f1",
        "stages": [(10,"🌑"),(50,"🪐"),(120,"🪐🌍"),(300,"☀️🪐🌍"),(10**9,"🌌✨🪐")],
        "elements": [("🌍","행성"),("🛰️","위성"),("☀️","항성"),("🌀","궤도"),("🚀","탐사")],
    },
    "lab": {
        "name": "🏰 연구소",
        "levels": [(0,"메모지","📝"),(40,"책상","🗄️"),(120,"서재","📚"),(300,"연구실","🔬"),(650,"연구동","🏢"),(1300,"캠퍼스","🏛️"),(2600,"연구단지","🌐")],
        "gradient": "linear-gradient(135deg,#0f172a,#0ea5e9 70%,#38bdf8)",
        "shadow": "rgba(14,165,233,0.28)", "accent": "#0ea5e9",
        "stages": [(10,"📝"),(50,"📚"),(120,"🔬"),(300,"🔬🏢"),(10**9,"🏛️🔬🏢")],
        "elements": [("📄","자료"),("🧪","실험"),("🔬","연구실"),("🔗","네트워크"),("🏆","성과")],
    },
}


def get_brain_theme_config(theme_key):
    """테마 설정 dict 반환 (없으면 기본 테마). 향후 캐릭터/마이룸 확장 시 단일 진입점."""
    return _BRAIN_THEMES.get(theme_key, _BRAIN_THEMES["default"])


def collect_persisted_data():
    """현재 session_state를 영속 데이터 dict로 모은다 (저장·내보내기 공용)."""
    _backfill_db_fields()
    data = {
        "archive_notes": st.session_state.get("archive_notes", []),
        "search_history": st.session_state.get("search_history", []),
        "saved_analyses": st.session_state.get("saved_analyses", []),
        "feedback_history": st.session_state.get("feedback_history", []),
        "analysis_cache": st.session_state.get("analysis_cache", {}),
        "result_closed": st.session_state.get("result_closed", False),
        "draft_cache": st.session_state.get("draft_cache", {}),
        "auto_feedback_stats": st.session_state.get(
            "auto_feedback_stats", {}
        ),
        "custom_trust_criteria": st.session_state.get("custom_trust_criteria", []),
        "active_custom_criteria_titles": st.session_state.get("active_custom_criteria_titles", []),
        "pkm_category_overrides": st.session_state.get("pkm_category_overrides", {}),
        "pkm_custom_concepts": st.session_state.get("pkm_custom_concepts", []),
        "concept_aliases": st.session_state.get("concept_aliases", {}),
        "pkm_concept_folders": st.session_state.get("pkm_concept_folders", {}),
        "projects": st.session_state.get("projects", []),
        "project_sections": st.session_state.get("project_sections", []),
        "project_steps": st.session_state.get("project_steps", []),
        "tasks": st.session_state.get("tasks", []),
        "note_concept_links": st.session_state.get("note_concept_links", []),
        "hidden_concepts": st.session_state.get("hidden_concepts", []),
        "merge_dismissed": st.session_state.get("merge_dismissed", []),
        "excluded_concepts_log": st.session_state.get("excluded_concepts_log", []),
        "custom_select_options": st.session_state.get("custom_select_options", {}),
        "saved_searches": st.session_state.get("saved_searches", []),
        "brain_theme": st.session_state.get("brain_theme", "default"),
        "lumi_avatar": st.session_state.get("lumi_avatar", "sparkle"),
        "app_settings": st.session_state.get("app_settings", {}),
        "brain_last_level": st.session_state.get("brain_last_level", 1),
        "brain_growth_state": st.session_state.get("brain_growth_state", {}),
        "nav_group_states": {k: v for k, v in st.session_state.items() if k.startswith("nav_grp_open_")},
        # ── DB 마이그레이션 대비 확장 구조 ──
        "entities": st.session_state.get("entities", []),
        "relations": st.session_state.get("relations", []),
        "folders": st.session_state.get("folders", []),
    }
    return normalize_persisted_data(data)


def save_persisted_data():
    data = collect_persisted_data()
    # 1) Supabase 우선 저장 (영구 — 재배포/재시작에도 생존)
    _connected = bool(_sb_client())
    _ok = _sb_save(data)
    # ⚠️ 클라우드 연결돼 있는데 저장 실패하면 '조용한 손실'이므로 분명히 경고
    try:
        if _connected and not _ok:
            st.session_state["_cloud_save_failed"] = True
            st.toast("⚠️ 클라우드 저장 실패! 데이터가 위험해요. 설정→🛠 개발자 진단을 확인하세요.", icon="⚠️")
        elif _connected and _ok:
            st.session_state["_cloud_save_failed"] = False
    except Exception:
        pass
    # 2) 로컬 파일 백업(폴백) — Supabase 미설정/실패 시 기존 동작 유지
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        backup_dir = Path("trustlens_backups")
        backup_dir.mkdir(exist_ok=True)
        backup_file = backup_dir / f"trustlens_backup_{datetime.now().strftime('%Y%m%d')}.json"
        with backup_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 로컬 쓰기 실패해도 Supabase에 저장됐으면 OK

st.markdown("""
<style>
:root {
    --main-blue: #1e3a8a;
    --soft-blue: #eff6ff;
    --point-blue: #2563eb;
    --text-main: #0f172a;
    --text-sub: #64748b;
    --card-border: #e2e8f0;
    --bg-main: #f8fafc;
    --sidebar-bg: #1e3a8a;
    --radius-lg: 16px;
    --radius-md: 12px;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.07), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.06), 0 2px 4px rgba(0,0,0,0.04);
}

/* ── 기본 레이아웃 ── */
.stApp {
    background: #f8fafc !important;
    color: #0f172a !important;
}
.block-container {
    padding-top: 3rem !important;
    padding-bottom: 3rem !important;
    max-width: 1200px !important;
}

/* ── 메인 텍스트 색상 보장 (배포 환경 CSS 변수 미지원 대비) ── */
.main, .main .block-container,
.main .block-container p,
.main .block-container span,
.main .block-container div,
.main .block-container label,
.main .block-container h1,
.main .block-container h2,
.main .block-container h3,
.main .block-container li,
.main .block-container td,
.main .block-container th,
.stMarkdown p,
.stMarkdown span,
.stMarkdown li {
    color: #0f172a !important;
}
/* Streamlit 위젯 라벨 */
.stTextInput label, .stTextArea label, .stSelectbox label,
.stMultiSelect label, .stSlider label, .stRadio label,
.stCheckbox label, .stNumberInput label, .stDateInput label {
    color: #0f172a !important;
}
/* 탭 텍스트 */
.stTabs [data-baseweb="tab"] { color: #0f172a !important; }
/* expander 헤더 */
.streamlit-expanderHeader { color: #0f172a !important; }
/* metric */
[data-testid="stMetricLabel"], [data-testid="stMetricValue"],
[data-testid="stMetricDelta"] { color: #0f172a !important; }
/* caption */
.stCaption { color: #64748b !important; }
@media (max-width: 768px) {
    .main .block-container { color: #0f172a !important; }
}

/* ══ 사이드바 전체 흰색 강제 — 다크 override 완전 차단 ══ */
section[data-testid="stSidebar"] { background: #1a2f6e !important; }

/* 모든 자식 요소 흰색 */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] *,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3,
section[data-testid="stSidebar"] li,
section[data-testid="stSidebar"] a {
    color: rgba(255,255,255,0.88) !important;
}

/* Streamlit 버튼 내부 텍스트 — p 태그 구조 */
section[data-testid="stSidebar"] .stButton button,
section[data-testid="stSidebar"] .stButton button p,
section[data-testid="stSidebar"] .stButton button span,
section[data-testid="stSidebar"] .stButton button div,
section[data-testid="stSidebar"] [data-testid="stButton"] button,
section[data-testid="stSidebar"] [data-testid="stButton"] button *,
section[data-testid="stSidebar"] button[kind="secondary"],
section[data-testid="stSidebar"] button[kind="secondary"] *,
section[data-testid="stSidebar"] button[kind="tertiary"],
section[data-testid="stSidebar"] button[kind="tertiary"] * {
    color: rgba(255,255,255,0.9) !important;
    background: transparent !important;
}

/* 그룹 버튼 (nav_grp_btn_*) 텍스트 명시적 흰색 */
section[data-testid="stSidebar"] button > div > p,
section[data-testid="stSidebar"] button > p {
    color: rgba(255,255,255,0.9) !important;
}

/* stMarkdown 내부 */
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] .stMarkdown * {
    color: rgba(255,255,255,0.88) !important;
}

/* 네비게이션 아이템 */
section[data-testid="stSidebar"] .tl-nav-item,
section[data-testid="stSidebar"] .tl-nav-item *,
section[data-testid="stSidebar"] .ni-label,
section[data-testid="stSidebar"] .ni-icon {
    color: rgba(255,255,255,0.88) !important;
}
section[data-testid="stSidebar"] .tl-nav-item.active,
section[data-testid="stSidebar"] .tl-nav-item.active * {
    color: #bfdbfe !important;
}

/* 브랜드 */
section[data-testid="stSidebar"] .tl-brand-name { color: #ffffff !important; }
section[data-testid="stSidebar"] .tl-brand-sub  { color: rgba(255,255,255,0.5) !important; }

/* caption / divider */
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stCaption * {
    color: rgba(255,255,255,0.4) !important;
}

/* ── 카드 컴포넌트 ── */
.tl-card {
    background: white;
    border: 1px solid var(--card-border);
    border-radius: var(--radius-lg);
    padding: 20px 24px;
    box-shadow: var(--shadow-sm);
    margin-bottom: 16px;
}
.tl-card-title {
    font-size: 16px;
    font-weight: 700;
    color: var(--text-main);
    margin-bottom: 4px;
}
.tl-card-sub {
    font-size: 13px;
    color: var(--text-sub);
}

/* 기존 호환 */
.input-shell, .side-help-card, .result-shell, .page-card {
    background: white;
    border: 1px solid var(--card-border);
    border-radius: var(--radius-lg);
    padding: 22px 24px;
    box-shadow: var(--shadow-sm);
    margin-bottom: 16px;
}

/* ── 히어로 영역 ── */
.hero-area {
    padding: 8px 0 20px 0;
}
.hero-title {
    font-size: 26px;
    font-weight: 800;
    color: var(--text-main);
    margin-bottom: 6px;
    line-height: 1.3;
}
.hero-sub {
    font-size: 14px;
    color: var(--text-sub);
}

/* ── 레거시 호환 ── */
.hero-title { font-size: 26px; font-weight: 800; color: var(--text-main); margin-bottom: 6px; }
.hero-subtitle { color: var(--text-sub); font-size: 14px; margin-bottom: 20px; }

.input-shell, .side-help-card, .result-shell, .page-card {
    background: white;
    border: 1px solid var(--card-border);
    border-radius: var(--radius-lg);
    padding: 20px 24px;
    box-shadow: var(--shadow-sm);
    margin-bottom: 16px;
}
.result-inner-box {
    background: #f8fbff;
    border: 1px solid #e5edf8;
    border-radius: 20px;
    padding: 18px;
    margin: 12px 0;
}
.memo-shell {
    background: #f8fbff;
    border: 2px dashed #93c5fd;
    border-radius: 22px;
    padding: 22px;
    margin-top: 16px;
}

.progress-pill { display: inline-block; background: #eaf2ff; color: #2563eb; font-weight: 800; padding: 5px 12px; border-radius: 9px; font-size: 14px; }
.progress-line { height: 10px; background: #edf2f7; border-radius: 99px; margin: 12px 0 28px 0; overflow: hidden; }
.progress-fill { height: 100%; width: 100%; background: linear-gradient(90deg, #2f73ff, #60a5fa); }
.question-title { font-size: 22px; font-weight: 850; color: #172033; margin: 18px 0 6px 0; }
.question-subtitle { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
.choice-box { background: #f8fcff; border: 1px solid #e5eef8; border-radius: 18px; padding: 16px 18px; margin: 12px 0; }
.choice-box-title { font-weight: 800; color: #172033; font-size: 15px; }
.choice-box-desc { color: #94a3b8; font-size: 13px; margin-top: 4px; }
.info-note { background: #eff6ff; border: 1px solid #dbeafe; border-radius: 16px; padding: 16px 18px; color: #2563eb; font-size: 14px; line-height: 1.55; }

.metric-card {
    background: #ffffff;
    border: 1px solid #e5edf8;
    border-radius: 18px;
    padding: 18px;
    min-height: 116px;
    box-shadow: 0 6px 16px rgba(15, 23, 42, 0.035);
}
.metric-label { color: #64748b; font-size: 13px; margin-bottom: 8px; }
.metric-value { color: #172033; font-size: 26px; font-weight: 850; line-height: 1.15; }
.metric-sub { color: #94a3b8; font-size: 12px; margin-top: 8px; }

.tag-badge { display: inline-block; background: #dbeafe; color: #1d4ed8; padding: 6px 12px; border-radius: 999px; font-size: 13px; font-weight: 700; margin: 4px; }
.tag-warn-badge { display: inline-block; background: #ffedd5; color: #c2410c; padding: 6px 12px; border-radius: 999px; font-size: 13px; font-weight: 700; margin: 4px; }
.summary-box { background:#f8fbff; border:1px solid #dbeafe; border-radius:18px; padding:18px; margin:8px 0 14px 0; }
.summary-title { font-size:18px; font-weight:850; color:#172033; margin-bottom:10px; }
.debug-box { background:#f7f7f7; padding:12px; border-radius:8px; font-size:13px; color:#555; }

.chart-dashboard {
    background:#ffffff;
    border:1px solid #dbeafe;
    border-radius:22px;
    padding:24px 24px 22px 24px;
    margin-top:18px;
    box-shadow:0 12px 28px rgba(15,23,42,0.06);
}
.chart-title { color:#172033; font-size:21px; font-weight:900; margin-bottom:6px; }
.chart-subtitle { color:#64748b; font-size:14px; margin-bottom:22px; font-weight:600; }
.feedback-shell {
    background:#fff7ed;
    border:1px solid #fed7aa;
    border-radius:22px;
    padding:22px;
    margin-top:18px;
}
.feedback-chip {
    display:inline-block;
    background:#ffedd5;
    color:#c2410c;
    border:1px solid #fdba74;
    padding:6px 11px;
    border-radius:999px;
    font-size:12px;
    font-weight:800;
    margin:4px;
}
.learning-box {
    background:#f8fbff;
    border:1px solid #bfdbfe;
    border-radius:16px;
    padding:14px 16px;
    margin-top:12px;
    color:#1e3a8a;
    font-size:14px;
    line-height:1.55;
}
.compare-box {
    background:#f8fbff;
    border:1px solid #dbeafe;
    border-radius:18px;
    padding:18px;
    margin:12px 0;
}
.compare-number {
    font-size:26px;
    font-weight:900;
    color:#172033;
}
.official-card {
    background:#ecfdf5;
    border:1px solid #bbf7d0;
    border-radius:18px;
    padding:16px 18px;
    margin:12px 0;
    color:#14532d;
    line-height:1.55;
}
.official-warning-card {
    background:#fff7ed;
    border:1px solid #fed7aa;
    border-radius:18px;
    padding:16px 18px;
    margin:12px 0;
    color:#9a3412;
    line-height:1.55;
}
.reason-chip {
    display:inline-block;
    background:#eef4ff;
    color:#1d4ed8;
    border:1px solid #bfdbfe;
    padding:6px 10px;
    border-radius:999px;
    font-size:12px;
    font-weight:800;
    margin:4px;
}

.help-grid-card { background:#fbfdff; border:1px solid #e5edf8; border-radius:18px; padding:18px; min-height:118px; }
.help-grid-title { font-weight:850; color:#172033; margin-bottom:6px; }
.help-grid-desc { color:#8fa1b8; font-size:13px; line-height:1.45; }

.history-item { background:#f8fbff; border:1px solid #e5edf8; border-radius:14px; padding:14px 16px; margin:8px 0; }
.history-title { font-weight:800; color:#172033; }
.history-meta { color:#64748b; font-size:13px; margin-top:4px; }

.archive-action-card {
    background: #ffffff;
    border: 2px solid #ef4444;
    border-radius: 28px;
    padding: 26px;
    box-shadow: 0 10px 24px rgba(239,68,68,0.10);
    min-height: 150px;
}
.note-action-card {
    background: #ffffff;
    border: 2px solid #2563eb;
    border-radius: 28px;
    padding: 26px;
    box-shadow: 0 10px 24px rgba(37,99,235,0.10);
    min-height: 150px;
}
.note-action-card,
.archive-action-card {
    min-height: 112px !important;
    padding: 18px 22px !important;
}
.....note-action-card h2,
.archive-action-card h2 {
    font-size: clamp(22px, 1.65vw, 30px) !important;
    line-height: 1.22 !important;
    white-space: nowrap !important;
    margin: 0 0 14px 0 !important;
}
.note-action-card p,
.archive-action-card p {
    font-size: clamp(15px, 1.05vw, 19px) !important;
    line-height: 1.45 !important;
    margin: 0 !important;
}

.ai-draft-button-scope + div[data-testid="stButton"] button,
.big-action-button.blue-action + div[data-testid="stButton"] button,
div[data-testid="stVerticalBlock"] > div:has(.big-action-button.blue-action) + div[data-testid="stButton"] button {
    background: linear-gradient(180deg,#3b82f6,#1d4ed8) !important;
    border: 1px solid #1d4ed8 !important;
    color: #ffffff !important;
    border-radius: 14px !important;
    font-weight: 900 !important;
    height: 58px !important;
    font-size: 18px !important;
}

.ai-draft-button-scope + div[data-testid="stButton"] button:hover,
.big-action-button.blue-action + div[data-testid="stButton"] button:hover,
div[data-testid="stVerticalBlock"] > div:has(.big-action-button.blue-action) + div[data-testid="stButton"] button:hover {
    background: linear-gradient(180deg,#2563eb,#1e40af) !important;
    border-color: #1e40af !important;
    color: #ffffff !important;
}

.big-action-button + div[data-testid="stButton"] button {
    height: 58px !important;
    border-radius: 14px !important;
    font-size: 22px !important;
    font-weight: 800 !important;
}

.blue-action + div[data-testid="stButton"] button {
    background: linear-gradient(180deg,#3b82f6,#1d4ed8) !important;
    color: white !important;
    border: none !important;
}

 .red-action + div[data-testid="stButton"] button,
.big-action-button.red-action + div[data-testid="stButton"] button,
div[data-testid="stVerticalBlock"] > div:has(.big-action-button.red-action) + div[data-testid="stButton"] button {
    background: linear-gradient(180deg,#ef4444,#dc2626) !important;
    color: white !important;
    border: none !important;
    border-radius: 14px !important;
    font-weight: 900 !important;
    height: 58px !important;
    font-size: 18px !important;
}

.red-action + div[data-testid="stButton"] button:hover,
.big-action-button.red-action + div[data-testid="stButton"] button:hover,
div[data-testid="stVerticalBlock"] > div:has(.big-action-button.red-action) + div[data-testid="stButton"] button:hover {
    background: linear-gradient(180deg,#f43f5e,#b91c1c) !important;
    color: white !important;
}

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

.knowledge-draft-blue-button + div[data-testid="stButton"] button {
    background: linear-gradient(180deg,#3b82f6,#1d4ed8) !important;
    border: 1px solid #1d4ed8 !important;
    color: #ffffff !important;
    border-radius: 14px !important;
    font-weight: 900 !important;
    height: 58px !important;
    font-size: 18px !important;
}
.knowledge-draft-blue-button + div[data-testid="stButton"] button:hover {
    background: linear-gradient(180deg,#2563eb,#1e40af) !important;
    color: #ffffff !important;
}

/* PATCH: concept finder */
.pkm-folder-title {
    font-weight: 900;
    color: #172033;
    font-size: 18px;
    margin: 12px 0 6px 0;
}
.pkm-concept-card {
    background:#ffffff;
    border:1px solid #e5edf8;
    border-radius:18px;
    padding:14px 16px;
    min-height:92px;
    box-shadow:0 6px 16px rgba(15,23,42,0.035);
}
.pkm-concept-name {
    font-weight:900;
    color:#172033;
    font-size:16px;
}
.pkm-concept-meta {
    color:#64748b;
    font-size:13px;
    margin-top:6px;
}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    # ─── query param 기반 네비게이션 ───
    _qp = st.query_params.get("page", "home")
    if "menu" not in st.session_state:
        st.session_state["menu"] = _qp

    _SIDEBAR_CSS = """
<style>
/* ══ TrustLens 다크 네이비 사이드바 ══ */

section[data-testid="stSidebar"] {
    background: #1a2f6e !important;
}
section[data-testid="stSidebar"] > div:first-child { padding: 0 !important; }

/* 브랜드 */
.tl-brand {
    padding: 20px 20px 16px 20px;
    border-bottom: 1px solid rgba(255,255,255,0.1);
}
.tl-brand-logo { display: flex; align-items: center; gap: 10px; }
.tl-brand-icon {
    width: 36px; height: 36px; border-radius: 10px;
    background: linear-gradient(135deg, #3b82f6, #60a5fa);
    display: flex; align-items: center; justify-content: center;
    font-size: 19px; flex-shrink: 0;
}
.tl-brand-name { font-size: 16px; font-weight: 800; color: #ffffff; }
.tl-brand-sub { font-size: 10px; color: rgba(255,255,255,0.5); letter-spacing: 0.5px; margin-top: 1px; }

/* 그룹 헤더 버튼 — wrapper 투명화 */
section[data-testid="stSidebar"] [data-testid="stButton"],
section[data-testid="stSidebar"] [data-testid="stButton"] > div {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 2px !important;
    margin: 0 !important;
}
/* 그룹 헤더 버튼 — nav 아이템과 동일한 크기/스타일 */
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: rgba(255,255,255,0.92) !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    letter-spacing: 0 !important;
    text-transform: none !important;
    padding: 9px 16px 9px 16px !important;
    width: 100% !important;
    justify-content: flex-start !important;
    text-align: left !important;
    border-radius: 8px !important;
    margin: 1px 0 !important;
    min-height: unset !important;
    transition: background 0.15s !important;
}
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover {
    background: rgba(255,255,255,0.12) !important;
    color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:active,
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:focus {
    background: rgba(96,165,250,0.25) !important;
    color: #bfdbfe !important;
}
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] p,
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] div {
    font-size: 14px !important;
    font-weight: 600 !important;
    color: inherit !important;
    text-align: left !important;
    letter-spacing: 0 !important;
    text-transform: none !important;
    margin: 0 !important;
    width: 100% !important;
}

/* 세로 간격 제거 + 배경 투명화 */
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: 0 !important; }
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {
    margin: 0 !important; padding: 0 !important;
    background: transparent !important;
}
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
    color: rgba(255,255,255,0.85) !important; margin: 0 !important;
}
/* Streamlit element 컨테이너 배경 투명화 */
section[data-testid="stSidebar"] .element-container,
section[data-testid="stSidebar"] .stMarkdown {
    background: transparent !important;
}

/* 메뉴 아이템 */
.tl-nav-item {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 16px 9px 20px;
    margin: 1px 6px;
    border-radius: 8px;
    cursor: pointer;
    text-decoration: none !important;
    transition: background 0.15s;
    color: rgba(255,255,255,0.88) !important;
    font-size: 14px;
    font-weight: 400;
}
.tl-nav-item:hover {
    background: rgba(255,255,255,0.1) !important;
    color: #ffffff !important;
    text-decoration: none !important;
}
.tl-nav-item.active {
    background: rgba(59,130,246,0.35) !important;
    color: #bfdbfe !important;
    font-weight: 600;
    border-left: 3px solid #60a5fa;
    padding-left: 17px;
}
.tl-nav-item .ni-icon { font-size: 15px; flex-shrink: 0; width: 22px; opacity: 0.85; }
.tl-nav-item.active .ni-icon { opacity: 1; }
.tl-nav-item .ni-label { font-size: 15px; }
.tl-nav-item .ni-soon {
    margin-left: auto; font-size: 9px; font-weight: 600;
    background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.5) !important;
    padding: 1px 6px; border-radius: 4px;
}

/* 구분선 */
.tl-nav-divider { height: 1px; background: rgba(255,255,255,0.07); margin: 3px 10px; }

/* ── details/summary 기반 nav 그룹 (새로고침 없는 토글) ── */
.tl-nav-group {
    margin-bottom: 2px;
}
.tl-nav-group summary {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    cursor: pointer;
    border-radius: 8px;
    list-style: none;
    user-select: none;
    font-size: 16px;
    font-weight: 700;
    color: rgba(255,255,255,0.78) !important;
    letter-spacing: 0.2px;
    transition: background 0.15s;
}
.tl-nav-group summary::-webkit-details-marker { display: none; }
.tl-nav-group summary::marker { display: none; }
.tl-nav-group summary:hover {
    background: rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.95) !important;
}
.tl-nav-group[open] summary {
    color: rgba(255,255,255,0.95) !important;
}
.tl-grp-arrow {
    margin-left: auto;
    font-size: 11px;
    opacity: 0.6;
    transition: transform 0.2s;
    display: inline-block;
}
.tl-nav-group[open] .tl-grp-arrow {
    transform: rotate(90deg);
}
.tl-nav-group-items {
    padding: 2px 0 4px 0;
}

/* 📘 가이드북 — 그룹 헤더(summary)와 동일한 모양의 메인급 독립 메뉴 */
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone {
    padding: 10px 16px;
    margin: 2px 0;          /* 그룹 헤더(summary)처럼 좌우 마진 0 → 좌측 정렬 일치 */
    gap: 8px;
    letter-spacing: 0.2px;
    border-radius: 8px;
}
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone,
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone * {
    color: rgba(255,255,255,0.78) !important;
}
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone:hover,
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone:hover * {
    color: rgba(255,255,255,0.95) !important;
}
.tl-nav-item.tl-nav-standalone .ni-icon { font-size: 18px; width: auto; opacity: 1; }
.tl-nav-item.tl-nav-standalone .ni-label { font-size: 16px; font-weight: 700; }
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone.active,
section[data-testid="stSidebar"] .tl-nav-item.tl-nav-standalone.active * {
    color: #bfdbfe !important;
}
.tl-nav-item.tl-nav-standalone.active {
    background: rgba(59,130,246,0.30) !important;
    border-left: 3px solid #60a5fa;
}

/* ── 그룹별 포인트 색 (--grp는 각 그룹 인라인 style로 주입) ── */
.tl-nav-group[open] > summary {
    color: var(--grp, rgba(255,255,255,0.95)) !important;
    box-shadow: inset 3px 0 0 var(--grp, transparent);
}
.tl-nav-group > summary:hover {
    box-shadow: inset 3px 0 0 var(--grp, transparent);
}
.tl-nav-group[open] .tl-grp-arrow { color: var(--grp, inherit); opacity: 0.9; }
/* 활성 메뉴 항목 — 그룹 색으로 강조 (기본 파랑 규칙보다 뒤에 와서 우선) */
.tl-nav-group .tl-nav-item.active {
    background: color-mix(in srgb, var(--grp, #60a5fa) 24%, transparent) !important;
    color: color-mix(in srgb, var(--grp, #bfdbfe) 60%, #ffffff) !important;
    border-left: 3px solid var(--grp, #60a5fa);
}

/* hr / caption */
section[data-testid="stSidebar"] hr { display: none !important; }
section[data-testid="stSidebar"] .stCaption p {
    color: rgba(255,255,255,0.4) !important; font-size: 10px !important;
    padding: 6px 16px !important;
}

/* << 접기 버튼 더 잘 보이게 */
button[data-testid="collapsedControl"],
[data-testid="collapsedControl"] {
    background: rgba(255,255,255,0.18) !important;
    border-radius: 8px !important;
    color: white !important;
    opacity: 1 !important;
}
</style>"""
    st.markdown(_SIDEBAR_CSS, unsafe_allow_html=True)

    # ─── 메뉴 구조 ───
    # (그룹 이모지, 그룹명, 포인트색, [항목들])
    # 🌍 행동 중심 IA — 사용자가 보는 1차 객체는 메모·프로젝트·작업.
    #    개념·태그·관계·분석 등은 '🧪 고급 모드'로 숨김(설정에서 켬). 기능은 그대로 유지.
    _show_adv = bool(st.session_state.get("app_settings", {}).get("show_advanced", False))
    _NAV_STRUCTURE = [
        ("🌍", "내 세계", "#34d399", [        # 초록
            ("home",      "🏠", "대시보드"),
            ("archive",   "📚", "지식 라이브러리"),
            ("projects",  "📁", "프로젝트"),
            ("tasks",     "✅", "작업"),
            ("map",       "🕸️", "지식 지도"),
            ("search",    "🔍", "통합 검색"),
        ]),
        ("✍️", "기록하기", "#38bdf8", [       # 하늘
            ("new",       "➕", "새 메모"),
            ("daily",     "📅", "데일리 노트"),
        ]),
        ("🤖", "루미", "#fb923c", [           # 주황
            ("ai",        "🧠", "지식 AI"),
            ("brain",     "💡", "브레인스토밍"),
            ("pattern",   "📈", "패턴 분석"),
        ]),
        ("⚙️", "설정", "#94a3b8", [           # 회색
            ("settings",  "⚙️", "설정"),
            ("data",      "💾", "데이터·백업"),
            ("guide",     "📘", "가이드북"),
            ("changelog", "🆕", "패치 노트"),
        ]),
    ]
    if _show_adv:
        _NAV_STRUCTURE.append(
            ("🧪", "고급 모드", "#a78bfa", [   # 보라 — AI가 관리하는 객체/전문가 기능
                ("concept_lib", "🧠", "개념 라이브러리"),
                ("tags",        "🏷️", "태그 관리"),
                ("entity",      "🔎", "엔터티 상세"),
                ("result",      "📊", "분석 결과"),
                ("criteria",    "🔍", "신뢰도 근거"),
                ("saved",       "🗃️", "분석결과 아카이브"),
                ("history",     "🕒", "최근 검색 기록"),
            ])
        )

    # 가이드북은 설정 그룹에 포함됨(아래 standalone 주입 제거)
    _NAV_GUIDE = ("guide", "📘", "가이드북")

    _cur_page = st.query_params.get("page", "home")

    # ─── 브랜드 ───
    st.markdown("""<div class="tl-brand">
  <div class="tl-brand-logo">
    <div class="tl-brand-icon">🛡️</div>
    <div>
      <div class="tl-brand-name">JIUM</div>
      <div class="tl-brand-sub">생각을 잇다 · 세계를 짓다</div>
    </div>
  </div>
</div>
<div style="height:8px"></div>""", unsafe_allow_html=True)

    # ─── 네비게이션 (details/summary 기반 — 새로고침 없음) ───
    _nav_html_parts = []
    for _grp_icon, _grp_name, _grp_color, _grp_items in _NAV_STRUCTURE:
        # 현재 페이지가 이 그룹에 속하면 기본 열림
        _grp_page_keys = [i[0] for i in _grp_items]
        _open_attr = "open" if _cur_page in _grp_page_keys else ""

        _items_html = ""
        for _item in _grp_items:
            _page_key = _item[0]
            _icon = _item[1]
            _label = _item[2]
            _soon = len(_item) > 3
            _is_active = _cur_page == _page_key
            _active_cls = "active" if _is_active else ""
            _soon_badge = '<span class="ni-soon">곧 출시</span>' if _soon else ""
            _items_html += (
                f'<a href="?page={_page_key}" target="_self" class="tl-nav-item {_active_cls}">'
                f'<span class="ni-icon">{_icon}</span>'
                f'<span class="ni-label">{_label}</span>{_soon_badge}</a>'
            )

        _nav_html_parts.append(
            f'<details class="tl-nav-group" {_open_attr} style="--grp:{_grp_color}">'
            f'<summary>'
            f'<span style="font-size:18px">{_grp_icon}</span>'
            f'<span>{_grp_name}</span>'
            f'<span class="tl-grp-arrow">▶</span>'
            f'</summary>'
            f'<div class="tl-nav-group-items">{_items_html}</div>'
            f'</details>'
            f'<div class="tl-nav-divider"></div>'
        )

    st.markdown("\n".join(_nav_html_parts), unsafe_allow_html=True)

    # ─── query param → menu 동기화 ───
    _PAGE_TO_MENU = {
        "home":     "분석 시작하기",
        "daily":    "데일리 노트",
        "search":   "통합 검색",
        "new":      "새 엔터티",
        "result":   "분석 결과",
        "criteria": "신뢰도 근거",
        "archive":  "지식 라이브러리",
        "concept_lib": "개념 라이브러리",
        "map":      "지식 맵",
        "tags":     "태그 관리",
        "projects": "프로젝트",
        "tasks":    "작업 관리",
        "data":     "데이터 관리",
        "saved":    "분석결과 아카이브",
        "history":  "최근 검색 기록",
        "ai":       "지식 AI",
        "brain":    "AI 브레인스토밍",
        "pattern":  "패턴 분석",
        "entity":   "엔터티 상세",
        "guide":    "가이드북",
        "changelog": "패치 노트",
        "settings": "설정",
    }
    menu = _PAGE_TO_MENU.get(_cur_page, "분석 시작하기")
    st.session_state["menu"] = menu
    st.caption("MVP v3 · Grow Your Knowledge World")

# -----------------------------
# Session State
# -----------------------------
def init_state():
    persisted = normalize_persisted_data(load_persisted_data())
    defaults = {
        "last_result": None,
        "last_final_url": None,
        "last_text": "",
        "archive_notes": persisted.get("archive_notes", []),
        "saved_analyses": persisted.get("saved_analyses", []),
        "search_history": persisted.get("search_history", []),
        "show_result": False,
        "result_closed": persisted.get("result_closed", False),
        "note_saved": False,
        "feedback_history": persisted.get("feedback_history", []),
        "analysis_cache": persisted.get("analysis_cache", {}),
        "draft_cache": persisted.get("draft_cache", {}),
        "auto_feedback_stats": persisted.get("auto_feedback_stats", {}),
        "custom_trust_criteria": persisted.get("custom_trust_criteria", []),
        "active_custom_criteria_titles": persisted.get("active_custom_criteria_titles", []),
        "pkm_category_overrides": persisted.get("pkm_category_overrides", {}),
        "pkm_custom_concepts": [
            c if isinstance(c, dict) else {"name": str(c), "folder": persisted.get("pkm_concept_folders", {}).get(str(c), "내 개념"), "created_at": ""}
            for c in persisted.get("pkm_custom_concepts", []) if c
        ],
        "pkm_concept_folders": persisted.get("pkm_concept_folders", {}),
        "concept_aliases": persisted.get("concept_aliases", {}),
        "projects": persisted.get("projects", []),
        "project_sections": persisted.get("project_sections", []),
        "project_steps": persisted.get("project_steps", []),
        "tasks": persisted.get("tasks", []),
        "note_concept_links": persisted.get("note_concept_links", []),
        "hidden_concepts": persisted.get("hidden_concepts", []),
        "merge_dismissed": persisted.get("merge_dismissed", []),
        "excluded_concepts_log": persisted.get("excluded_concepts_log", []),
        "custom_select_options": persisted.get("custom_select_options", {}),
        "saved_searches": persisted.get("saved_searches", []),
        "brain_theme": persisted.get("brain_theme", "default"),
        "lumi_avatar": persisted.get("lumi_avatar", "sparkle"),
        "app_settings": {**APP_SETTINGS_DEFAULTS, **(persisted.get("app_settings", {}) or {})},
        "brain_last_level": persisted.get("brain_last_level", 1),
        "brain_growth_state": persisted.get("brain_growth_state", {
            "last_level": 1, "unlocked_rewards": [], "last_checked_at": ""
        }),
        # ── DB 마이그레이션 대비 확장 구조 ──
        "entities": persisted.get("entities", []),
        "relations": persisted.get("relations", []),
        "folders": persisted.get("folders", []),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    # 네비게이션 그룹 상태 복원 (JSON에서 로드)
    for k, v in persisted.get("nav_group_states", {}).items():
        if k.startswith("nav_grp_open_") and k not in st.session_state:
            st.session_state[k] = v


def normalize_custom_concepts():
    """pkm_custom_concepts 안에 문자열이 섞여 있으면 dict로 정규화. init_state 이후 실행."""
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    normalized = []
    for c in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(c, dict):
            c["name"] = _clean_text_value(c.get("name")).strip()
            c["folder"] = _clean_text_value(c.get("folder")).strip() or "내 개념"
            c["description"] = _clean_text_value(c.get("description"))
            # 필수 필드 보강
            c.setdefault("id", f"concept_{c.get('name', '')[:8]}_{id(c)}")
            c.setdefault("user_id", "local_user")
            c.setdefault("created_at", _now)
            c.setdefault("updated_at", _now)
            c.setdefault("deleted_at", None)
            normalized.append(c)
        elif c:
            normalized.append({
                "id": f"concept_{str(c)[:8]}",
                "user_id": "local_user",
                "name": str(c),
                "folder": "내 개념",
                "description": "",
                "aliases": [],
                "created_at": _now,
                "updated_at": _now,
                "deleted_at": None,
            })
    st.session_state.pkm_custom_concepts = normalized


def sync_legacy_data_to_entities():
    """기존 projects/tasks/notes/concepts 데이터를 entities 리스트에 참조용으로 동기화.
    원본 데이터는 건드리지 않고, entities에 없는 항목만 추가함."""
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing_entities = st.session_state.get("entities", [])
    existing_ids = {e.get("id") for e in existing_entities}

    new_entities = []

    # projects → entity type=project
    for p in st.session_state.get("projects", []):
        eid = p.get("id", "")
        if eid and eid not in existing_ids:
            new_entities.append({
                "id": eid,
                "user_id": "local_user",
                "type": "project",
                "name": p.get("name", ""),
                "folder": p.get("category", ""),
                "parent_id": "",
                "description": p.get("description", ""),
                "created_at": p.get("created_at", _now),
                "updated_at": p.get("updated_at", _now),
                "deleted_at": None,
            })
            existing_ids.add(eid)

    # tasks → entity type=task
    for t in st.session_state.get("tasks", []):
        eid = t.get("id", "")
        if eid and eid not in existing_ids:
            new_entities.append({
                "id": eid,
                "user_id": "local_user",
                "type": "task",
                "name": t.get("title", ""),
                "folder": "",
                "parent_id": t.get("project_id", ""),
                "description": t.get("summary", ""),
                "created_at": t.get("created_at", _now),
                "updated_at": t.get("updated_at", _now),
                "deleted_at": None,
            })
            existing_ids.add(eid)

    # archive_notes → entity type=note
    for n in st.session_state.get("archive_notes", []):
        eid = n.get("id", "")
        if eid and eid not in existing_ids:
            new_entities.append({
                "id": eid,
                "user_id": "local_user",
                "type": "note",
                "name": n.get("title", ""),
                "folder": n.get("section", ""),
                "parent_id": n.get("project_id", ""),
                "description": "",
                "created_at": n.get("saved_at", _now),
                "updated_at": n.get("saved_at", _now),
                "deleted_at": None,
            })
            existing_ids.add(eid)

    # pkm_custom_concepts → entity type=concept
    for c in st.session_state.get("pkm_custom_concepts", []):
        if not isinstance(c, dict):
            continue
        eid = c.get("id", "")
        if eid and eid not in existing_ids:
            new_entities.append({
                "id": eid,
                "user_id": "local_user",
                "type": "concept",
                "name": c.get("name", ""),
                "folder": c.get("folder", "자동"),
                "parent_id": "",
                "description": c.get("description", ""),
                "created_at": c.get("created_at", _now),
                "updated_at": c.get("updated_at", _now),
                "deleted_at": None,
            })
            existing_ids.add(eid)

    if new_entities:
        st.session_state.entities = existing_entities + new_entities


def hydrate_last_result_from_cache():
    """앱 재실행 후에도 최근 분석 결과 탭이 비지 않도록 저장된 캐시에서 마지막 결과를 복구한다."""
    if st.session_state.get("result_closed"):
        return
    if st.session_state.get("last_result"):
        return

    for item in st.session_state.get("search_history", []):
        url = item.get("url", "")
        content_type = item.get("content_type", "unknown")
        cache_key = item.get("cache_key") or f"{url}::{content_type}"
        cached = st.session_state.get("analysis_cache", {}).get(cache_key)

        if cached:
            st.session_state.last_result = cached
            st.session_state.last_final_url = url
            st.session_state.last_text = ""
            st.session_state.show_result = True
            return


init_state()
normalize_custom_concepts()
sync_legacy_data_to_entities()
hydrate_last_result_from_cache()

# ⚠️ 직전 저장이 클라우드까지 못 갔으면 모든 화면 상단에 경고 (조용한 손실 방지)
if st.session_state.get("_cloud_save_failed"):
    st.error("⚠️ **방금 저장이 클라우드(Supabase)까지 가지 못했어요.** 데이터가 재시작 시 사라질 수 있어요. "
             "잠시 후 다시 저장하거나, **설정 → 🛠 개발자 진단 → 🔁 왕복 테스트**로 연결을 확인하세요.")

st.markdown(
    '''
    <div style="padding: 8px 0 24px 0;">
        <div style="font-size:26px; font-weight:800; color:#0f172a; margin-bottom:6px; line-height:1.3;">
            🌍 내 지식 세계
        </div>
        <div style="font-size:14px; color:#64748b;">
            지식은 쌓는 것이 아니라 연결하는 것입니다. 오늘도 당신의 지식 세계를 성장시켜보세요.
        </div>
    </div>
    ''',
    unsafe_allow_html=True,
)

# -----------------------------
# Basic Helpers
# -----------------------------
def multiselect_with_all(label, options, key, format_func=None, help=None, hint=None):
    """기존 항목을 추가·이동·연결·삭제할 때 쓰는 multiselect.
    '전체 선택 / 선택 해제' 버튼을 함께 제공해 한 번에 또는 개별로 선택 가능.
    options 변동으로 인한 stale 세션값은 자동 정리."""
    options = list(options)
    # stale 세션값 정리 (옵션에 없는 값 제거 → multiselect 에러 방지)
    if key in st.session_state:
        st.session_state[key] = [v for v in st.session_state[key] if v in options]
    if options:
        _msa1, _msa2 = st.columns(2)
        with _msa1:
            if st.button(f"☑️ 전체 선택 ({len(options)})", key=f"{key}__all_btn", use_container_width=True):
                st.session_state[key] = list(options)
                st.rerun()
        with _msa2:
            if st.button("⬜ 선택 해제", key=f"{key}__none_btn", use_container_width=True):
                st.session_state[key] = []
                st.rerun()
    _kw = {"key": key}
    if format_func is not None:
        _kw["format_func"] = format_func
    if help is not None:
        _kw["help"] = help
    return st.multiselect(label, options, **_kw)


def link_checkbox_picker(items, current_selected, key_prefix, id_func, label_func,
                         type_func=None, priority_set=None, search_label="🔍 검색",
                         empty_text="후보가 없어요.", height=220):
    """체크박스 기반 연결 선택 리스트 (메모/개념 등을 작업·프로젝트에 연결할 때 재사용).
    - 검색 + (선택) 유형 필터 + 전체선택/해제 + 체크박스 + 선택 개수
    - 필터로 일부가 숨겨져도 선택 상태는 유지 (전용 selset이 진실의 원천)
    반환: 선택된 id 리스트.
    숨겨진 항목의 선택을 잃지 않도록, selset에는 후보에 더는 없는 id도 보존된다.
    """
    items = list(items)
    _selkey = f"{key_prefix}__selset"
    if _selkey not in st.session_state:
        st.session_state[_selkey] = list(current_selected or [])
    sel = set(st.session_state[_selkey])

    q = st.text_input(search_label, key=f"{key_prefix}__q", placeholder="제목으로 검색").strip().lower()
    cands = items
    if type_func is not None:
        _types = ["전체"] + sorted({type_func(it) for it in items})
        _tsel = st.selectbox("유형", _types, key=f"{key_prefix}__tf")
        if _tsel != "전체":
            cands = [it for it in cands if type_func(it) == _tsel]
    if q:
        cands = [it for it in cands if q in label_func(it).lower()]
    if priority_set:
        cands = sorted(cands, key=lambda it: (id_func(it) not in priority_set, label_func(it)))

    if cands:
        _pa, _pb = st.columns(2)
        with _pa:
            if st.button(f"☑️ 전체 선택 ({len(cands)})", key=f"{key_prefix}__all", use_container_width=True):
                for it in cands:
                    _i = id_func(it)
                    sel.add(_i)
                    st.session_state[f"{key_prefix}__cb_{_i}"] = True
                st.session_state[_selkey] = list(sel)
                st.rerun()
        with _pb:
            if st.button("⬜ 보이는 항목 해제", key=f"{key_prefix}__none", use_container_width=True):
                for it in cands:
                    _i = id_func(it)
                    sel.discard(_i)
                    st.session_state[f"{key_prefix}__cb_{_i}"] = False
                st.session_state[_selkey] = list(sel)
                st.rerun()

    with st.container(height=height, border=True):
        if not cands:
            st.caption(empty_text)
        for it in cands:
            _iid = id_func(it)
            _cbkey = f"{key_prefix}__cb_{_iid}"
            if _cbkey not in st.session_state:
                st.session_state[_cbkey] = _iid in sel
            _label = label_func(it)
            if priority_set and _iid in priority_set:
                _label = f"⭐ {_label}"
            _checked = st.checkbox(_label, key=_cbkey)
            if _checked:
                sel.add(_iid)
            else:
                sel.discard(_iid)

    st.session_state[_selkey] = list(sel)
    st.caption(f"✅ 선택 {len(sel)}개")
    return list(sel)


def clear_link_picker(key_prefix):
    """link_checkbox_picker 가 만든 세션 상태(selset/검색/체크박스) 일괄 정리."""
    for k in [_k for _k in st.session_state.keys() if _k.startswith(f"{key_prefix}__")]:
        del st.session_state[k]


def task_link_editor(key_prefix, project_name, cur_note_ids, cur_concepts):
    """작업에 연결할 메모/개념을 고르는 2-탭 체크박스 picker. (note_ids, concepts) 반환.
    현재 프로젝트의 메모/개념을 ⭐로 우선 표시."""
    notes = st.session_state.get("archive_notes", [])
    concepts = st.session_state.get("pkm_custom_concepts", [])
    proj_note_ids = {n.get("id") for n in notes if project_name and n.get("project") == project_name}
    _links = st.session_state.get("note_concept_links", [])
    proj_concepts = {l.get("concept") for l in _links
                     if l.get("note_id") in proj_note_ids and l.get("concept")}

    _pt1, _pt2 = st.tabs([f"📎 메모 ({len(cur_note_ids or [])})", f"🧠 개념 ({len(cur_concepts or [])})"])
    with _pt1:
        sel_notes = link_checkbox_picker(
            notes, cur_note_ids, f"{key_prefix}_note",
            id_func=lambda n: n.get("id", ""),
            label_func=lambda n: (n.get("title") or "(제목 없음)"),
            priority_set=proj_note_ids, empty_text="저장된 메모가 없어요.")
    with _pt2:
        sel_cons = link_checkbox_picker(
            concepts, cur_concepts, f"{key_prefix}_con",
            id_func=lambda c: c.get("name", ""),
            label_func=lambda c: (c.get("name", "") + (f"  · {c.get('folder','')}" if c.get("folder") else "")),
            priority_set=proj_concepts, empty_text="등록된 개념이 없어요.")
    return sel_notes, sel_cons


def task_link_caption(task):
    """작업 카드 하단에 표시할 연결 요약 문자열. 연결 없으면 빈 문자열."""
    _n = len(task.get("linked_note_ids", []) or [])
    _c = len(task.get("linked_concepts", []) or [])
    parts = []
    if _n:
        parts.append(f"📎 메모 {_n}개")
    if _c:
        parts.append(f"🧠 개념 {_c}개")
    return " · ".join(parts)


def task_concept_chips(task, limit=6):
    """연결된 개념을 #chip 문자열로. 없으면 빈 문자열."""
    cons = [c for c in (task.get("linked_concepts", []) or []) if c]
    if not cons:
        return ""
    shown = cons[:limit]
    chips = " ".join(f"#{c}" for c in shown)
    if len(cons) > limit:
        chips += f" +{len(cons) - limit}"
    return chips


# ── 개념 병합 안전화 헬퍼 ───────────────────────────────────
# 조사/어미: 긴 것부터 (짧은 것 먼저 자르면 오인식)
# 주의: "이네/이네는/네"는 고유명사·가게명(예: 진영이네)을 과도하게 자르므로 제외.
#       "진영이네는"은 "는"만 떨어져 "진영이네"로 보존된다.
_CONCEPT_JOSA = ["에서는", "으로는", "에게서", "이라는", "라는",
                 "에서", "으로", "에게", "한테", "들이", "들의", "들",
                 "은", "는", "이", "가", "을", "를", "에", "로", "와", "과",
                 "도", "만", "의", "랑", "께"]
# 너무 일반적인 단어 (병합 후보에서 🔴 비추천)
_GENERIC_CONCEPTS = {
    "사회", "정보", "제목", "최근", "회원", "여기", "내용", "설명", "자료",
    "관련", "주제", "오늘", "경우", "사람", "문제", "방법", "사용", "생각",
    "이번", "대상", "결과", "시작", "진행", "상황", "부분", "정도", "다음",
    # 관측된 잡개념·기능어 (보수적 확장)
    "테스트", "위해", "통해", "또한", "그것", "이것", "저것", "무엇", "때문",
    "여러", "각각", "모두", "전체", "일부", "기타", "내일", "어제", "지금",
    # 사용자 튜닝 기준(2026-06-04): 일반어/추상어는 개념에서 제외
    "외부", "내부", "구조", "하단", "상단", "정리", "분리", "고민", "필요",
    "중요", "처럼", "우선", "기준", "방향", "느낌", "상태", "수준", "관점",
}


def normalize_concept_token(name):
    """개념명에서 조사/어미를 제거한 정규화 값. (남는 길이가 2자 미만이면 원본 유지)"""
    s = str(name).strip()
    for _suf in _CONCEPT_JOSA:
        if s.endswith(_suf) and len(s) - len(_suf) >= 2:
            return s[: -len(_suf)]
    return s


def grade_merge_pair(a, b, ratio):
    """병합 후보 쌍 (a, b)을 등급화. 반환: (grade, reason)
    grade ∈ {"green","yellow","red"}. 보수적으로 동작(애매하면 낮은 등급)."""
    a, b = str(a).strip(), str(b).strip()
    na, nb = normalize_concept_token(a), normalize_concept_token(b)
    # 너무 일반적인 단어
    if a in _GENERIC_CONCEPTS or b in _GENERIC_CONCEPTS or na in _GENERIC_CONCEPTS or nb in _GENERIC_CONCEPTS:
        return ("red", "너무 일반적인 단어라 병합 비추천")
    if a == b:
        return ("green", "동일한 개념")
    # 조사/어미만 다른 경우 → 거의 같은 개념
    if na == nb:
        return ("green", "조사/어미 제거 후 일치")
    # 접두/접미 포함 → 하위개념일 수 있어 검토 필요
    if (na in nb or nb in na) and min(len(na), len(nb)) >= 2:
        return ("yellow", "접두/접미 포함 — 하위개념일 수 있어요")
    # 문자열 유사도
    if ratio >= 0.9:
        return ("green", "문자열 유사도 매우 높음")
    if ratio >= 0.75:
        return ("yellow", "문자열 유사도 높음")
    return ("red", "의미 차이가 클 수 있음")


def _classify_concept(raw):
    """개념 품질 게이트 분류기 (경량 규칙 기반 — 외부 NLP 의존성 없음).
    반환: (정제값 또는 None, 사유). 통과 시 사유는 빈 문자열.
    순서: 기호/공백 정리 → 조사·어미 제거(normalize) → 불용어 → 숫자/기호 → 1글자."""
    if not raw:
        return (None, "빈 값")
    s = str(raw).replace("#", "").strip()
    s = s.strip(" \t\n\r\"'`·,.!?()[]{}<>「」『』“”‘’…").strip()
    if not s:
        return (None, "기호/공백만")
    # 1) 조사/어미 제거
    s = normalize_concept_token(s).strip()
    if not s:
        return (None, "정규화 후 빈 값")
    # 2) 불용어 (로그에서 먼저 잡히도록 길이검사보다 앞)
    if s in _GENERIC_CONCEPTS:
        return (None, "불용어")
    # 3) 숫자/기호만
    if not any(ch.isalnum() for ch in s):
        return (None, "기호만")
    if s.isdigit():
        return (None, "숫자만")
    # 4) 1글자
    if len(s) <= 1:
        return (None, "1글자")
    return (s, "")


def clean_concept(raw):
    """개념 저장 전 품질 게이트. 통과하면 정제된 개념명, 탈락하면 None."""
    return _classify_concept(raw)[0]


def _log_excluded_concept(original, reason):
    """게이트에서 제외된 개념을 기록 (품질 리포트·stopword 개선용)."""
    _log = st.session_state.setdefault("excluded_concepts_log", [])
    _orig = str(original).replace("#", "").strip()
    if not _orig:
        return
    _log.append({"concept": _orig, "reason": reason,
                 "at": datetime.now().strftime("%Y-%m-%d %H:%M")})
    if len(_log) > 2000:                 # 무한 증가 방지
        del _log[: len(_log) - 2000]


def filter_concepts(names):
    """개념명 리스트를 품질 게이트로 정제 + 중복 제거. 제외된 개념은 로그에 기록."""
    out, seen = [], set()
    for n in (names or []):
        c, reason = _classify_concept(n)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
        elif not c and str(n).replace("#", "").strip():
            _log_excluded_concept(n, reason)
    return out


# ── 개념 별칭(alias) 시스템 ─────────────────────────────────────
# concept_aliases = {대표개념: [별칭1, 별칭2, ...]} — 비파괴적. 집계 시점에만 대표로 합산.
def _alias_key(s):
    """별칭 매칭용 정규화 키 (정제 + 소문자)."""
    c = clean_concept(s)
    return (c or str(s).strip()).lower()


def _alias_reverse_map():
    """별칭키 -> 대표개념 매핑. concept_aliases 변경 시에만 재생성(캐시)."""
    amap = st.session_state.get("concept_aliases", {}) or {}
    sig = (len(amap), sum(len(v or []) for v in amap.values()))
    cache = st.session_state.get("_alias_rev_cache")
    if cache and cache.get("sig") == sig:
        return cache["rev"]
    rev = {}
    for canon, aliases in amap.items():
        for a in (aliases or []):
            if a:
                rev[_alias_key(a)] = canon
    st.session_state["_alias_rev_cache"] = {"sig": sig, "rev": rev}
    return rev


def canonical_concept(name):
    """개념명을 대표 개념으로 정규화.
    clean_concept 적용 → 별칭이면 대표 개념, 아니면 정제된 원래 개념 반환."""
    c = clean_concept(name)
    if not c:
        return c
    return _alias_reverse_map().get(_alias_key(name), c)


def canonical_concepts(names):
    """리스트를 대표 개념으로 정규화 + 순서 보존 중복 제거."""
    out, seen = [], set()
    for n in (names or []):
        c = canonical_concept(n)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ── 개념 scope (owned/shared) — 비파괴: 저장된 scope 우선, 없으면 연결 프로젝트 수로 자동 판정 ──
def concept_projects(name):
    """이 개념(canonical 기준)이 연결된 서로 다른 프로젝트 집합."""
    _canon = canonical_concept(name)
    if not _canon:
        return set()
    _projs = set()
    _notes = st.session_state.get("archive_notes", [])
    _note_proj = {
        n.get("id"): _clean_text_value(n.get("project")).strip()
        for n in _notes if isinstance(n, dict)
    }
    for _l in st.session_state.get("note_concept_links", []):
        if canonical_concept(_l.get("concept")) == _canon:
            _p = (_clean_text_value(_l.get("project")).strip()
                  or _note_proj.get(_l.get("note_id"), ""))
            if _p:
                _projs.add(_p)
    for _c in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(_c, dict) and canonical_concept(_c.get("name")) == _canon:
            _p = _clean_text_value(_c.get("project")).strip()
            if _p:
                _projs.add(_p)
    return _projs


def concept_scope(name):
    """개념 scope 반환. 저장된 scope(owned/shared/global) 있으면 우선,
    없으면 연결 프로젝트 ≥2 → 'shared', 아니면 'owned' (비파괴 자동 판정)."""
    _canon = canonical_concept(name)
    for _c in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(_c, dict) and canonical_concept(_c.get("name")) == _canon:
            _s = _clean_text_value(_c.get("scope")).strip().lower()
            if _s in ("owned", "shared", "global"):
                return _s
    return "shared" if len(concept_projects(name)) >= 2 else "owned"


def add_concept_aliases(canonical, aliases):
    """대표 개념에 별칭 등록 (비파괴적). 자기 자신/중복/빈값 제외. 등록 수 반환."""
    canonical = (clean_concept(canonical) or str(canonical).strip())
    if not canonical:
        return 0
    amap = st.session_state.setdefault("concept_aliases", {})
    cur = amap.get(canonical, [])
    _seen = {_alias_key(x) for x in cur}
    added = 0
    for a in (aliases or []):
        a = str(a).strip()
        if not a or _alias_key(a) == _alias_key(canonical) or _alias_key(a) in _seen:
            continue
        cur.append(a)
        _seen.add(_alias_key(a))
        added += 1
    if cur:
        amap[canonical] = cur
    return added


def remove_concept_alias(canonical, alias):
    """특정 별칭 삭제. 별칭이 없어지면 대표 키도 제거."""
    amap = st.session_state.get("concept_aliases", {})
    if canonical in amap:
        amap[canonical] = [a for a in amap[canonical] if _alias_key(a) != _alias_key(alias)]
        if not amap[canonical]:
            del amap[canonical]


def concept_frequency(top_n=None):
    """개념 등장 빈도 Counter (note_concept_links + 작업 linked_concepts + 메모 concepts).
    most_common 리스트 반환 [(개념, 횟수), ...]."""
    from collections import Counter
    _c = Counter()
    for l in st.session_state.get("note_concept_links", []):
        _cn = canonical_concept(l.get("concept"))
        if _cn:
            _c[_cn] += 1
    for t in st.session_state.get("tasks", []):
        for x in (t.get("linked_concepts", []) or []):
            _cx = canonical_concept(x)
            if _cx:
                _c[_cx] += 1
    for n in st.session_state.get("archive_notes", []):
        for x in (n.get("concepts", []) or []):
            _cx = canonical_concept(x)
            if _cx:
                _c[_cx] += 1
    return _c.most_common(top_n) if top_n else _c.most_common()


def concept_importance(top_n=None, half_life_days=30):
    """개념 중요도 = 빈도 × 최근성 가중치.
    최근성 가중치 = 0.5 ** (경과일수 / half_life_days) 의 등장별 합산.
    날짜 출처: note_concept_links.linked_at / 작업.updated_at / 메모.saved_at.
    프로젝트 맵(노드 크기=빈도, 노드 색=최근성)에서 활용 예정. 지금은 계산만 제공.
    반환: [(개념, {"frequency": f, "importance": imp, "recency": r}), ...]
    importance 내림차순. r(최근성)=가중치합/빈도(0~1, 1=오늘)."""
    from collections import defaultdict

    def _days_since(date_str):
        if not date_str:
            return None
        try:
            d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
            return max(0.0, (datetime.now() - d).total_seconds() / 86400.0)
        except Exception:
            return None

    def _weight(date_str):
        days = _days_since(date_str)
        if days is None:
            return 0.5  # 날짜 불명 → 중립 가중치
        return 0.5 ** (days / max(1, half_life_days))

    freq = defaultdict(int)
    wsum = defaultdict(float)

    def _add(name, date_str):
        if not name:
            return
        freq[name] += 1
        wsum[name] += _weight(date_str)

    for l in st.session_state.get("note_concept_links", []):
        _add(l.get("concept"), l.get("linked_at"))
    for t in st.session_state.get("tasks", []):
        for x in (t.get("linked_concepts", []) or []):
            _add(x, t.get("updated_at") or t.get("created_at"))
    for n in st.session_state.get("archive_notes", []):
        for x in (n.get("concepts", []) or []):
            _add(x, n.get("saved_at") or n.get("created_at"))

    out = []
    for name, f in freq.items():
        rec = wsum[name] / f if f else 0.0
        out.append((name, {"frequency": f, "recency": round(rec, 4),
                           "importance": round(f * rec, 4)}))
    out.sort(key=lambda kv: kv[1]["importance"], reverse=True)
    return out[:top_n] if top_n else out


def concept_tfidf(note_filter=None, top_n=None):
    """메모 단위 문서 기준 TF-IDF 개념 중요도 (경량·표준 라이브러리만).
    - 문서 = 지식 메모 1개 (concepts ∪ note_concept_links)
    - IDF는 전체 메모(코퍼스) 기준 → 전체에서 흔한 개념은 점수↓
    - TF는 note_filter로 좁힌 범위(프로젝트 등)에서 합산 → 그 범위에 특화된 개념 점수↑
    idf = log((1 + N) / (1 + df)) + 1,  tfidf = tf × idf
    note_filter: 메모 dict -> bool. None이면 전체 메모.
    반환: [(concept, {"tf","df","idf","tfidf"}), ...] tfidf 내림차순."""
    import math
    from collections import Counter, defaultdict
    notes = st.session_state.get("archive_notes", [])

    _links_by_note = defaultdict(Counter)
    for _lk in st.session_state.get("note_concept_links", []):
        _nid, _c = _lk.get("note_id"), _lk.get("concept")
        if _nid and _c:
            _links_by_note[_nid][_c] += 1

    def _doc_counter(n):
        c = Counter()
        for x in (n.get("concepts", []) or []):
            _cx = canonical_concept(x)
            if _cx:
                c[_cx] += 1
        for x, cnt in _links_by_note.get(n.get("id"), {}).items():
            _cx = canonical_concept(x)
            if _cx:
                c[_cx] += cnt
        return c

    # 글로벌 코퍼스 (IDF용) — concepts 없는 메모는 자동 제외
    _global_docs = []
    for n in notes:
        dc = _doc_counter(n)
        if dc:
            _global_docs.append(dc)
    _N = len(_global_docs)
    if _N == 0:
        return []
    _df = Counter()
    for dc in _global_docs:
        for concept in dc:
            _df[concept] += 1
    _idf = {c: math.log((1 + _N) / (1 + d)) + 1 for c, d in _df.items()}

    # 범위 문서 (TF용)
    _scope_docs = []
    for n in notes:
        if note_filter is not None and not note_filter(n):
            continue
        dc = _doc_counter(n)
        if dc:
            _scope_docs.append(dc)
    if not _scope_docs:
        return []
    _tf = Counter()
    for dc in _scope_docs:
        for c, cnt in dc.items():
            _tf[c] += cnt

    rows = []
    for c, t in _tf.items():
        i = _idf.get(c, math.log((1 + _N) / 1) + 1)
        rows.append((c, {"tf": t, "df": _df.get(c, 0),
                         "idf": round(i, 4), "tfidf": round(t * i, 4)}))
    rows.sort(key=lambda kv: kv[1]["tfidf"], reverse=True)
    return rows[:top_n] if top_n else rows


def excluded_concepts_report(top_n=15):
    """제외된 개념 로그 집계 → (개념별 TOP, 사유별 집계, 총건수).
    개념 품질 리포트/불용어 개선용."""
    from collections import Counter
    log = st.session_state.get("excluded_concepts_log", [])
    by_concept = Counter(e.get("concept", "") for e in log if e.get("concept"))
    by_reason = Counter(e.get("reason", "기타") for e in log)
    return by_concept.most_common(top_n), by_reason.most_common(), len(log)


def concept_impact_counts(cands):
    """병합 대상 개념들이 연결된 메모/작업/관계/엔터티 수를 센다."""
    cands_set = set(cands)
    _note_ids = {l.get("note_id") for l in st.session_state.get("note_concept_links", [])
                 if l.get("concept") in cands_set}
    _notes = sum(1 for n in st.session_state.get("archive_notes", [])
                 if n.get("id") in _note_ids
                 or any(c in cands_set for c in (n.get("concepts", []) or [])))
    _tasks = sum(1 for t in st.session_state.get("tasks", [])
                 if any(c in cands_set for c in (t.get("linked_concepts", []) or [])))
    _rels = sum(1 for r in st.session_state.get("relations", [])
                if r.get("source_name") in cands_set or r.get("target_name") in cands_set)
    _ents = sum(1 for e in st.session_state.get("entities", []) if e.get("name") in cands_set)
    return {"notes": _notes, "tasks": _tasks, "rels": _rels, "ents": _ents}


def normalize_date_str(value) -> str:
    """다양한 날짜 입력(2026.06.16 / 2026/6/16 / 2026년 6월 16일 / date객체)을 'YYYY-MM-DD'로 통일.
    파싱 불가하면 빈 문자열 반환 (캘린더/타임라인에서 안전하게 무시)."""
    if value is None:
        return ""
    # date/datetime 객체
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(value).strip()
    if not s:
        return ""
    # 한글/구분자 정리: '2026년 6월 16일' → '2026 6 16'
    s2 = re.sub(r"[년월]", "-", s)
    s2 = re.sub(r"일", "", s2)
    s2 = s2.replace(".", "-").replace("/", "-").replace(" ", "")
    s2 = re.sub(r"-+", "-", s2).strip("-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s2)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return ""
    return ""


def parse_date_for_input(value):
    """저장된 날짜 문자열을 st.date_input 초기값(date 객체)으로 변환. 실패 시 None."""
    norm = normalize_date_str(value)
    if not norm:
        return None
    try:
        return datetime.strptime(norm, "%Y-%m-%d").date()
    except Exception:
        return None


def clean_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = []
    seen = set()
    for line in lines:
        if len(line) <= 1:
            continue
        if line in seen:
            continue
        seen.add(line)
        cleaned.append(line)
    return "\n".join(cleaned)


def convert_naver_mobile_url(url: str) -> str:
    if "blog.naver.com" not in url:
        return url
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2:
        blog_id = path_parts[0]
        post_id = path_parts[1]
        return f"https://m.blog.naver.com/{blog_id}/{post_id}"
    return url


# 본문이 아닌 잡영역(사이드바/댓글/최근글/푸터 등)을 통째로 제거하기 위한 셀렉터
_JUNK_SELECTORS = [
    "script", "style", "nav", "footer", "header", "aside", "form", "noscript",
    # Tistory / 블로그 공통 잡영역
    ".another_category", ".container_postbtn", ".area_sympathy", ".tt_box_subscribe",
    ".comment", ".comments", ".reply", ".replyArea", "#comment", "#comments",
    ".recent", ".recentPost", ".recent_post", ".popular", ".related",
    ".related_post", ".relatedArticle", ".sidebar", "#sidebar", ".aside",
    ".widget", ".tt_category", ".category", ".tags", ".tagTrail", ".tag_label",
    ".paging", ".pagination", ".blogview_comment", ".revenue_unit_wrap",
    ".coverInfo", ".sponsor", ".ad", ".ad_wrap", ".adsbygoogle",
]

# 본문 추출 품질이 낮을 때 경고에 쓰는 잡텍스트 키워드
_JUNK_KEYWORDS = [
    "Recent Posts", "Recent Comments", "Related Articles", "Related Posts",
    "Comments", "댓글쓰기", "댓글을", "공감", "구독하기", "최근 글", "최근글",
    "최근 댓글", "인기 글", "카테고리", "태그", "TISTORY", "Powered by",
    "Blog is powered", "Copyright", "공지사항", "이전 글", "다음 글",
    "본문 바로가기", "로그아웃", "RSS",
]


def assess_extract_quality(text: str):
    """추출 본문에서 잡텍스트(블로그 사이드바/댓글 등) 비율을 추정. (junk_ratio, junk_hits)"""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return 1.0, 0
    junk_hits = 0
    for line in lines:
        if any(kw in line for kw in _JUNK_KEYWORDS):
            junk_hits += 1
    return junk_hits / max(len(lines), 1), junk_hits


def extract_text(url):
    try:
        target_url = convert_naver_mobile_url(url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        res = requests.get(target_url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""

        # ① 잡영역(사이드바/댓글/최근글/푸터/광고)을 먼저 통째로 제거 → 어떤 셀렉터를 쓰든 깨끗
        for selector in _JUNK_SELECTORS:
            for tag in soup.select(selector):
                tag.decompose()

        # ② 본문 전용 셀렉터를 "우선순위 순서대로" 시도하고, 충분히 길면 그걸로 확정 (body로 새지 않음)
        #    Tistory: .entry-content / .tt_article_useless_p_margin / .article_view / .contents_style
        priority_selectors = [
            "div.se-main-container",          # 네이버 스마트에디터
            "div#postViewArea",               # 네이버 구버전
            "div.post_ct",
            "div.post-view",
            "div.entry-content",              # Tistory/워드프레스
            "div.tt_article_useless_p_margin",# Tistory 본문
            "div.article_view",               # Tistory
            "div.contents_style",             # Tistory
            "div.article_content",
            "div#content .article",
            "article",
        ]
        text = ""
        for selector in priority_selectors:
            selected = soup.select_one(selector)
            if selected:
                cand = clean_text(selected.get_text("\n", strip=True))
                # 본문으로 인정할 최소 길이 (사이드바 조각 방지)
                if len(cand) >= 400:
                    text = cand
                    break

        # ③ 본문 전용 셀렉터로 못 찾으면 main → body 순서로 fallback (잡영역은 이미 제거됨)
        if not text:
            for selector in ["main", "body"]:
                selected = soup.select_one(selector)
                if selected:
                    cand = clean_text(selected.get_text("\n", strip=True))
                    if len(cand) > len(text):
                        text = cand

        text = clean_text(text)
        if title:
            text = f"[페이지 제목]\n{title}\n\n[본문]\n{text}"
        return text[:MAX_EXTRACT_TEXT_CHARS], "", target_url
    except requests.exceptions.Timeout:
        return "", "페이지 로딩 시간이 초과됐어요.", url
    except Exception as e:
        return "", f"본문 추출 실패: {e}", url


def safe_json_parse(raw: str):
    raw = raw.strip()
    if "```" in raw:
        raw = raw.replace("```json", "```")
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)
    return json.loads(raw)

CONTENT_TYPE_LABELS = {
    "review": "후기/리뷰",
    "policy": "정책/공공정보",
    "info": "일반 정보글",
    "study": "공부자료",
    "unknown": "판단 어려움",
}

SCORE_META = {
    "official_source": ("공식 출처", 20),
    "recency": ("최신성", 15),
    "source_diversity": ("출처 다양성", 12),
    "ad_free": ("광고 안전성", 15),
    "info_density": ("정보 밀도", 20),
    "experience_specificity": ("경험 구체성", 25),
    "balanced_review": ("장단점 균형", 15),
    "revisit_mention": ("재방문/사용 경험", 10),
}


SCORE_KEYS_BY_TYPE = {
    "review": ["recency", "ad_free", "info_density", "experience_specificity", "balanced_review", "revisit_mention"],
    "policy": ["official_source", "recency", "source_diversity", "ad_free", "info_density"],
    "info": ["official_source", "recency", "source_diversity", "ad_free", "info_density", "experience_specificity", "balanced_review"],
    "study": ["official_source", "recency", "source_diversity", "ad_free", "info_density"],
    "unknown": list(SCORE_META.keys()),
}


# --- Added review/policy/info signal helpers and normalization ---
AD_KEYWORDS = [
    "협찬", "체험단", "제공받았습니다", "제공 받았습니다", "쿠팡파트너스",
    "파트너스 활동", "소정의 수수료", "원고료", "업체로부터"
]

REVIEW_HINTS = [
    "맛집", "후기", "리뷰", "여행", "메뉴", "추천 메뉴", "재방문", "한줄평", "총 평",
    "웨이팅", "테이블링", "배달", "리필", "가격", "먹었", "시킴", "주문",
    "단새우", "문어", "붓카케", "생면"
]

POLICY_HINTS = [
    "신청기간", "지원대상", "지원금", "모집공고", "공고문", "정부", "서울시",
    "고용노동부", "사업", "정책"
]

OFFICIAL_DOMAIN_HINTS = {
    "go.kr": "정부/공공기관",
    "or.kr": "공공·협회·기관",
    "ac.kr": "교육기관",
    "seoul.go.kr": "서울시",
    "work24.go.kr": "고용24",
    "hrd.go.kr": "HRD-Net",
    "moel.go.kr": "고용노동부",
    "molit.go.kr": "국토교통부",
    "bokjiro.go.kr": "복지로",
    "korea.kr": "대한민국 정책브리핑",
}

FEEDBACK_REASON_OPTIONS = [
    "실제 경험에 도움 됨",
    "광고 같음",
    "공식 정보와 일치",
    "정보가 오래됨",
    "출처 없음",
    "요약이 정확함",
    "점수가 어색함",
]

DEFAULT_TRUST_CRITERIA = [
    ("공식 출처", "정책/공공정보나 일반 정보글에서 정부·기관·공식 도메인처럼 원출처가 분명한지 확인해요."),
    ("최신성", "작성일, 업데이트 시점, 신청기간처럼 정보가 지금도 유효한지 확인해요."),
    ("출처 다양성", "하나의 주장에 대해 여러 근거 또는 참고 출처가 있는지 확인해요."),
    ("광고 안전성", "협찬, 체험단, 파트너스, 원고료 등 광고성 표현이 있는지 확인해요."),
    ("정보 밀도", "가격, 메뉴, 조건, 위치, 신청방법처럼 판단에 필요한 구체 정보가 충분한지 봐요."),
    ("경험 구체성", "직접 방문·구매·사용한 흔적, 상황 묘사, 사진 언급, 세부 경험이 있는지 확인해요."),
    ("장단점 균형", "좋은 점만 말하는지, 아쉬운 점·조건·주의점도 함께 말하는지 확인해요."),
    ("재방문/사용 경험", "다시 갈 의향, 반복 사용, 재구매처럼 경험 이후의 판단이 있는지 봐요."),
]

def detect_official_source(final_url: str, text: str):
    combined = f"{final_url or ''}\n{text or ''}".lower()
    matched = []
    for domain, label in OFFICIAL_DOMAIN_HINTS.items():
        if domain in combined:
            matched.append({"domain": domain, "label": label})
    return matched

def summarize_user_feedback_for_url(final_url: str):
    feedbacks = [f for f in st.session_state.get("feedback_history", []) if f.get("url") == (final_url or "")]
    if not feedbacks:
        return {
            "total": 0,
            "trust": 0,
            "distrust": 0,
            "hold": 0,
            "trust_pct": 0,
            "distrust_pct": 0,
            "hold_pct": 0,
            "reason_counts": {},
        }

    total = len(feedbacks)
    trust = sum(1 for f in feedbacks if f.get("trust_vote") == "신뢰함")
    distrust = sum(1 for f in feedbacks if f.get("trust_vote") == "신뢰 안함")
    hold = sum(1 for f in feedbacks if f.get("trust_vote") == "판단 보류")
    reason_counts = {}
    for f in feedbacks:
        for reason in f.get("feedback_reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "total": total,
        "trust": trust,
        "distrust": distrust,
        "hold": hold,
        "trust_pct": round(trust / total * 100),
        "distrust_pct": round(distrust / total * 100),
        "hold_pct": round(hold / total * 100),
        "reason_counts": reason_counts,
    }


def infer_content_type_from_text(text: str, selected_type: str = "unknown") -> str:
    if selected_type and selected_type != "unknown":
        return selected_type
    review_count = sum(1 for word in REVIEW_HINTS if word in text)
    policy_count = sum(1 for word in POLICY_HINTS if word in text)
    if policy_count >= 3:
        return "policy"
    if review_count >= 3:
        return "review"
    return "info"


def has_recent_date_signal(text: str) -> bool:
    patterns = [
        r"20\d{2}\.\s*\d{1,2}\.\s*\d{1,2}",
        r"20\d{2}-\d{1,2}-\d{1,2}",
        r"20\d{2}년\s*\d{1,2}월\s*\d{1,2}일",
        r"\d{1,2}월\s*\d{1,2}일",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def normalize_review_breakdown(breakdown: dict, text: str) -> dict:
    """
    후기/리뷰 점수는 AI 응답을 그대로 쓰지 않고 고정 규칙으로 재산정한다.
    맛집/후기 기준 총점 100점:
    최신성 15 + 광고 안전성 15 + 정보 밀도 20 + 경험 구체성 25 + 장단점 균형 15 + 재방문 10
    """
    text = text or ""
    fixed = {
        "official_source": 0,
        "source_diversity": 0,
        "recency": 0,
        "ad_free": 0,
        "info_density": 0,
        "experience_specificity": 0,
        "balanced_review": 0,
        "revisit_mention": 0,
    }

    ad_found = any(keyword in text for keyword in AD_KEYWORDS)
    experience_signals = [
        "먹었", "시킴", "주문", "방문", "웨이팅", "가격", "원", "리필", "재방문",
        "사진", "숙소", "배달", "테이블링", "국물", "면", "메뉴", "단새우", "문어", "붓카케"
    ]
    detail_signals = [
        "주소", "위치", "추천 메뉴", "한줄평", "총 평", "가격", "원", "메뉴", "웨이팅",
        "배달", "테이블링", "리필", "단새우", "문어", "붓카케", "생면", "강원특별자치도"
    ]
    weak_or_limit_signals = [
        "웨이팅", "가능하면 매장", "비 올 때", "길어도", "배부른데", "아쉬", "단점", "하지만", "원래 웨이팅"
    ]
    revisit_signals = ["재방문", "다음에", "또", "추천", "한 번쯤", "1000%"]

    experience_count = sum(1 for word in experience_signals if word in text)
    detail_count = sum(1 for word in detail_signals if word in text)
    weak_count = sum(1 for word in weak_or_limit_signals if word in text)
    revisit_count = sum(1 for word in revisit_signals if word in text)

    if has_recent_date_signal(text):
        fixed["recency"] = 15
    elif "최근" in text or "이번" in text:
        fixed["recency"] = 10
    else:
        fixed["recency"] = 6

    fixed["ad_free"] = 4 if ad_found else 15

    if detail_count >= 9 or len(text) >= 1800:
        fixed["info_density"] = 20
    elif detail_count >= 6 or len(text) >= 1200:
        fixed["info_density"] = 16
    elif detail_count >= 3:
        fixed["info_density"] = 12
    else:
        fixed["info_density"] = 7

    if experience_count >= 10:
        fixed["experience_specificity"] = 25
    elif experience_count >= 7:
        fixed["experience_specificity"] = 21
    elif experience_count >= 4:
        fixed["experience_specificity"] = 16
    else:
        fixed["experience_specificity"] = 9

    if weak_count >= 4:
        fixed["balanced_review"] = 15
    elif weak_count >= 2:
        fixed["balanced_review"] = 12
    elif weak_count == 1:
        fixed["balanced_review"] = 8
    else:
        fixed["balanced_review"] = 5

    if revisit_count >= 3:
        fixed["revisit_mention"] = 10
    elif revisit_count >= 1:
        fixed["revisit_mention"] = 8
    else:
        fixed["revisit_mention"] = 4

    return fixed


def get_int_score(breakdown: dict, key: str) -> int:
    _, max_val = SCORE_META[key]
    try:
        val = int(breakdown.get(key, 0))
    except Exception:
        val = 0
    return max(0, min(val, max_val))



def calculate_score_by_type(breakdown: dict, content_type: str) -> int:
    keys = SCORE_KEYS_BY_TYPE.get(content_type, SCORE_KEYS_BY_TYPE["unknown"])
    if content_type == "review":
        return max(0, min(sum(get_int_score(breakdown, key) for key in keys), 100))

    raw_score = sum(get_int_score(breakdown, key) for key in keys)
    max_score = sum(SCORE_META[key][1] for key in keys)
    if max_score == 0:
        return 0
    return max(0, min(round(raw_score / max_score * 100), 100))


def get_score_items_for_type(content_type: str):
    keys = SCORE_KEYS_BY_TYPE.get(content_type, SCORE_KEYS_BY_TYPE["unknown"])
    return [(key, SCORE_META[key][0], SCORE_META[key][1]) for key in keys]


def get_score_dataframe(breakdown: dict, content_type: str) -> pd.DataFrame:
    chart_items = []
    for key, label, max_val in get_score_items_for_type(content_type):
        score = get_int_score(breakdown, key)
        ratio = score / max_val if max_val else 0
        chart_items.append((label, score, max_val, round(ratio * 100, 1)))
    return pd.DataFrame(chart_items, columns=["항목", "점수", "최대점수", "달성률"])


def build_custom_criteria_text() -> str:
    criteria = st.session_state.get("custom_trust_criteria", [])
    if not criteria:
        return "사용자 커스텀 기준 없음"

    active_titles = st.session_state.get("active_custom_criteria_titles", [])
    if active_titles:
        criteria = [c for c in criteria if c.get("title") in active_titles]

    if not criteria:
        return "이번 분석에 선택된 사용자 커스텀 기준 없음"

    return "\n".join(
        [
            f"{i+1}. {c.get('title','')} / 중요도: {c.get('weight','보통')} / 설명: {c.get('description','')}"
            for i, c in enumerate(criteria)
            if c.get("title")
        ]
    )


def save_custom_trust_criterion(title_key, desc_key, weight_key):
    title = st.session_state.get(title_key, "").strip()
    desc = st.session_state.get(desc_key, "").strip()
    weight = st.session_state.get(weight_key, "보통")
    if not title:
        st.session_state["custom_criterion_error"] = "기준 이름을 입력해주세요."
        return
    st.session_state.custom_trust_criteria.append({
        "title": title,
        "description": desc,
        "weight": weight,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    active_titles = st.session_state.get("active_custom_criteria_titles", [])
    if title not in active_titles:
        active_titles.append(title)
    st.session_state.active_custom_criteria_titles = active_titles

    st.session_state["custom_criterion_saved"] = True
    save_persisted_data()


def delete_custom_trust_criterion(index):
    if 0 <= index < len(st.session_state.custom_trust_criteria):
        removed = st.session_state.custom_trust_criteria.pop(index)
        removed_title = removed.get("title")
        st.session_state.active_custom_criteria_titles = [
            title for title in st.session_state.get("active_custom_criteria_titles", [])
            if title != removed_title
        ]
        st.session_state["custom_criterion_deleted"] = True
        save_persisted_data()

# -----------------------------
# AI Functions
# -----------------------------
def call_groq_simple(system_msg: str, user_msg: str, model: str = "llama-3.3-70b-versatile") -> str:
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


def analyze_with_groq(text, url, selected_type):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        content_type = infer_content_type_from_text(text, selected_type)
        # 사용자 명시 선택(공부자료) > 자동 추론 — 키 없는 fallback 경로에서도 study 유지
        if selected_type == "study":
            content_type = "study"
        breakdown = normalize_review_breakdown({}, text) if content_type == "review" else {
            "official_source": 5,
            "recency": 10,
            "source_diversity": 4,
            "ad_free": 12,
            "info_density": 14,
            "experience_specificity": 10,
            "balanced_review": 8,
            "revisit_mention": 5,
        }
        return {
            "content_type": content_type,
            "score_breakdown": breakdown,
            "ad_risk": "low",
            "ad_risk_reason": "Mock 모드: API 없이 테스트용으로 생성된 결과입니다.",
            "author_type": "기록형",
            "author_reason": "Mock 모드에서 기본 작성자 유형으로 분류했습니다.",
            "is_official": False,
            "official_org": "",
            "tags_positive": {"review": ["후기", "리뷰", "테스트"], "policy": ["정책", "지원", "테스트"], "info": ["정보", "테스트"]}.get(content_type, ["테스트", "Mock모드"]),
            "tags_warning": ["Mock모드"],
            "summary": [
                "API 없이 UI 테스트를 위해 생성된 분석 결과입니다.",
                "실제 점수와 요약은 Groq API 연결 후 달라질 수 있습니다.",
                "레이아웃, 저장, 메모, 태그, 피드백 기능 테스트용입니다."
            ],
            "evidence": {
                "official_source": "Mock 모드",
                "ad_signal": "Mock 모드",
                "experience_signal": "Mock 모드",
                "negative_signal": "Mock 모드"
            },
            "archive_title": "Mock 테스트 분석",
            "trust_score": calculate_score_by_type(breakdown, content_type),
            "_debug": {
                "selected_type": selected_type,
                "ai_content_type": "(mock)",
                "final_content_type": content_type,
                "source_text_len": len(text or ""),
            },
        }

    prompt = f"""
너는 TrustLens라는 정보 신뢰도 분석 서비스의 AI 분석 엔진이다.
아래 웹페이지 본문을 분석하고 반드시 순수 JSON만 반환해라.
마크다운, 설명문, 코드블록을 절대 붙이지 마라.

URL:
{url}

사용자가 선택한 콘텐츠 유형:
{selected_type}

사용자가 추가한 커스텀 신뢰도 기준:
{build_custom_criteria_text()}

본문:
{text[:5000]}

[중요한 분석 원칙]
1. 글의 유형을 먼저 판단해라.
- policy: 정책/지원사업/공공정보
- review: 맛집/제품/장소/개인 후기
- info: 일반 정보성 글
- unknown: 판단 어려움

2. review 유형에서는 공식 출처가 없어도 불리하게 판단하지 마라.
- 후기글은 공식출처보다 경험 구체성, 장단점 균형, 광고 위험도가 더 중요하다.
- 맛집/제품/장소 후기 기준 총점은 반드시 100점이다.
- review 유형에서는 공식 출처와 출처 다양성을 최종 점수 계산에서 제외한다.
- review 점수 항목과 최대점수는 반드시 아래 범위만 사용해라.
  * recency: 0~15
  * ad_free: 0~15
  * info_density: 0~20
  * experience_specificity: 0~25
  * balanced_review: 0~15
  * revisit_mention: 0~10
- official_source와 source_diversity는 review 유형에서는 반드시 0으로 반환해라.
- 각 항목은 반드시 0 이상, 최대점수 이하의 정수로 반환해라.
- 네이버 블로그 후기에서 작성일이 보이면 최신성 점수에 반영해라.
- 가격, 메뉴, 위치, 주문한 음식, 웨이팅, 배달, 테이블링, 리필, 재방문 의사가 있으면 정보 밀도와 경험 구체성을 높게 줘라.
- "웨이팅 있음", "가능하면 매장 추천", "비 올 때 방문"처럼 조건/주의점이 있으면 장단점 균형 신호로 봐라.
- 사진이 많다는 직접 언급이 있으면 실제 경험 신호로 본다.

3. policy 유형에서는 공식 출처, 최신성, 기관명, 신청기간, 조건 정보가 중요하다.

4. 광고 판단 기준
- high: 협찬, 체험단, 제공받았습니다, 쿠팡파트너스, 파트너스 활동, 소정의 수수료, 원고료, 업체로부터 등 명시적 광고 문구가 있을 때
- mid: 명시 광고 문구는 없지만 장점만 있고 단점이 전혀 없으며 과도하게 홍보성 표현이 많을 때
- low: 개인 경험, 구체적 상황, 단점/한계, 비용, 재방문 의사 등이 자연스럽게 포함될 때

5. 네이버 블로그라는 이유만으로 광고로 판단하지 마라.
6. 정리된 포맷은 광고가 아니라 성실한 후기일 수 있다.
7. "웨이팅", "가격", "주문한 메뉴", "직접 먹어봄", "재방문", "아쉬운 점" 같은 표현은 실제 경험 신호다.
8. 태그에는 # 기호를 붙이지 말고 단어만 넣어라.
9. summary는 실제 본문 내용을 바탕으로 3문장으로 써라. "네이버 블로그 포스팅입니다" 같은 일반 문장은 금지한다.
10. 사용자가 추가한 커스텀 신뢰도 기준이 있으면 해당 기준도 판단에 참고해라.
11. 단, 커스텀 기준은 보조 기준이며 기본 TrustLens 기준을 완전히 대체하지 않는다.

반환 JSON 형식:
{{
  "content_type": "policy 또는 review 또는 info 또는 unknown",
  "score_breakdown": {{
    "official_source": "정수, review면 0, policy/info면 0~20",
    "recency": "정수 0~15",
    "source_diversity": "정수, review면 0, policy/info면 0~12",
    "ad_free": "정수 0~15",
    "info_density": "정수 0~20",
    "experience_specificity": "정수 0~25",
    "balanced_review": "정수 0~15",
    "revisit_mention": "정수 0~10"
  }},
  "ad_risk": "low 또는 mid 또는 high",
  "ad_risk_reason": "광고 위험도 판단 이유 한 문장",
  "author_type": "기록형 또는 객관형 또는 비판형 또는 홍보형",
  "author_reason": "작성자 유형 판단 이유 한 문장",
  "is_official": true 또는 false,
  "official_org": "공식 기관명 또는 빈 문자열",
  "tags_positive": ["긍정 태그 최대 5개"],
  "tags_warning": ["주의 태그 최대 4개"],
  "summary": ["핵심 요약 문장 1", "핵심 요약 문장 2", "핵심 요약 문장 3"],
  "evidence": {{
    "official_source": "근거 문장 또는 없음",
    "ad_signal": "근거 문장 또는 없음",
    "experience_signal": "근거 문장 또는 없음",
    "negative_signal": "근거 문장 또는 없음"
  }},
  "archive_title": "아카이브에 저장할 짧은 제목"
}}
"""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900,
        "temperature": 0.1,
    }
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=45)
    if res.status_code == 429:
        raise RuntimeError("Groq API 사용량 제한에 걸렸어요. 잠시 후 다시 시도하거나, 무료 한도를 확인해주세요.")
    res.raise_for_status()

    raw = res.json()["choices"][0]["message"]["content"].strip()
    result = safe_json_parse(raw)
    ai_content_type = result.get("content_type", selected_type or "unknown")
    inferred_content_type = infer_content_type_from_text(text, selected_type)
    content_type = inferred_content_type if selected_type == "unknown" else ai_content_type
    # study는 AI 프롬프트 선택지(policy/review/info/unknown)에 없어 AI가 절대 못 돌려줌
    # → 사용자가 공부자료를 명시적으로 골랐으면 study로 강제 유지 (초안 분기 보장)
    if selected_type == "study":
        content_type = "study"

    if content_type == "review":
        result["score_breakdown"] = normalize_review_breakdown(result.get("score_breakdown", {}), text)

    result["content_type"] = content_type
    result["trust_score"] = calculate_score_by_type(result.get("score_breakdown", {}), content_type)
    if not result.get("archive_title"):
        result["archive_title"] = "TrustLens 분석 메모"
    # 🛠️ 디버그: study 분기 추적용 (render_result 하단 expander에서 표시)
    result["_debug"] = {
        "selected_type": selected_type,
        "ai_content_type": ai_content_type,
        "final_content_type": content_type,
        "source_text_len": len(text or ""),
    }
    return result


def make_basic_note_draft(result, final_url=None, selected_tags=None):
    content_type = result.get("content_type", "unknown")
    score = result.get("trust_score", 0)
    ad_risk = result.get("ad_risk", "mid")
    author_type = result.get("author_type", "-")
    summary = result.get("summary", [])
    evidence = result.get("evidence", {})
    summary_text = "\n".join([f"- {s}" for s in summary]) if isinstance(summary, list) else str(summary)
    selected_tags = selected_tags or []
    tag_text = ", ".join([str(t).replace("#", "").strip() for t in selected_tags if str(t).strip()])
    ad_text = {"low": "낮음", "mid": "주의", "high": "위험"}.get(ad_risk, ad_risk)
    content_label = CONTENT_TYPE_LABELS.get(content_type, content_type)

    return f"""# {result.get('archive_title', 'TrustLens 정보 정리 노트')}

## 1. 기본 정보
- 출처: {display_source_label(final_url)}
- 콘텐츠 유형: {content_label}
- 신뢰도 점수: {score}점
- 광고 위험도: {ad_text}
- 작성자 유형: {author_type}

## 2. 핵심 요약
{summary_text}

## 3. 판단 근거
- 광고 판단 근거: {evidence.get("ad_signal", "없음")}
- 경험 신호 근거: {evidence.get("experience_signal", "없음")}
- 단점/비판 신호: {evidence.get("negative_signal", "없음")}

## 4. 내가 선택한 태그
{tag_text}

## 5. 내 메모
- 내가 추가로 확인할 점:
- 나중에 다시 볼 이유:
- 최종 판단:
"""


# --- Local fallback draft generator when no GROQ_API_KEY is present ---
def make_local_content_note_draft(original_text, result, final_url=None, template_type="보고서 형식", user_prompt=""):
    """GROQ_API_KEY가 없을 때도 붙여넣은 본문을 메모용으로 정리해주는 로컬 fallback."""
    content_type = result.get("content_type", "unknown")
    score = result.get("trust_score", 0)
    ad_risk = result.get("ad_risk", "mid")
    author_type = result.get("author_type", "-")
    content_label = CONTENT_TYPE_LABELS.get(content_type, content_type)
    ad_text = {"low": "낮음", "mid": "주의", "high": "위험"}.get(ad_risk, ad_risk)

    cleaned = clean_text(original_text or "")
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    title_candidates = [line for line in lines[:20] if len(line) >= 8]
    title = result.get("archive_title") or (title_candidates[0] if title_candidates else "붙여넣은 글 정리")

    useful_lines = []
    skip_words = [
        "NAVER", "본문 바로가기", "로그아웃", "서비스", "댓글", "함께 볼만한 뉴스",
        "랭킹", "Copyright", "Recent Posts", "Recent Comments", "Related Articles",
        "Related Posts", "Comments", "댓글쓰기", "구독하기", "최근 글", "최근글",
        "최근 댓글", "인기 글", "카테고리", "Powered by", "Blog is powered",
        "TISTORY", "공지사항", "이전 글", "다음 글", "RSS",
    ]
    for line in lines:
        if any(word in line for word in skip_words):
            continue
        if len(line) < 12:
            continue
        if line not in useful_lines:
            useful_lines.append(line)
        if len(useful_lines) >= 16:
            break

    key_points = useful_lines[:6]
    detail_points = useful_lines[6:14]

    key_text = "\n".join([f"- {item}" for item in key_points]) if key_points else "- 핵심 문장을 충분히 추출하지 못했어요. 원문을 확인해주세요."
    detail_text = "\n".join([f"- {item}" for item in detail_points]) if detail_points else "- 추가 세부 내용은 원문 확인이 필요해요."

    request_text = user_prompt.strip() if user_prompt else "없음"

    # ── study(공부자료) 전용 fallback 구조 — API 키가 없거나 401이어도 학습노트 뼈대는 보장 ──
    _is_study_fallback = (content_type == "study") or (template_type == "공부용 설명")
    if _is_study_fallback:
        study_core = key_points[:8]
        core_text = "\n".join([f"- {item}" for item in study_core]) if study_core else "- 본문에서 핵심 문장을 충분히 추출하지 못했어요. 원문을 확인해주세요."
        concepts = result.get("key_concepts") or result.get("concepts") or []
        concept_text = ", ".join([str(c).strip() for c in concepts if str(c).strip()]) if concepts else "(추출된 개념 없음 — 원문 확인 필요)"
        return f"""# {title} — 학습 노트

> ⚠️ AI 연결 없이 로컬 규칙으로 만든 학습노트 **뼈대**입니다. API가 연결되면 본문 이해 기반으로 자동 완성됩니다.

## 📌 한 줄 핵심
- (본문 핵심을 한 줄로 정리하세요)

## 🤔 왜 필요한가
- 이 개념/내용이 어떤 문제를 풀기 위해 등장했는지 적어보세요.

## ⚙️ 단계별 동작 원리
{core_text}

## 🧠 핵심 개념
- {concept_text}

## 🚧 한계 / 주의점
- 어떤 상황에서 안 통하거나 헷갈리는지 적어보세요.

## 💡 기억법
- 나만의 비유나 암기 포인트를 적어보세요.

## 📝 시험 대비 요약
- 시험/복습 때 꼭 떠올려야 할 1~3가지를 적어보세요.

---
- 출처: {display_source_label(final_url)} · 콘텐츠 유형: {content_label}
- 추가 요청: {request_text}
"""

    return f"""# {title}

## 1. 기본 정보
- 출처: {display_source_label(final_url)}
- 콘텐츠 유형: {content_label}
- 신뢰도 점수: {score}점
- 광고 위험도: {ad_text}
- 작성자 유형: {author_type}
- 초안 형식: {template_type}
- 추가 요청: {request_text}

## 2. 핵심 내용 정리
{key_text}

## 3. 세부 내용
{detail_text}

## 4. 신뢰도 관점 메모
- 이 초안은 GROQ_API_KEY 없이 로컬 규칙으로 만든 기본 정리입니다.
- 실제 API를 켜면 본문 전체를 더 자연스럽게 재구성하고, 보고서/블로그/체크리스트 형식에 맞춰 다시 작성할 수 있어요.
- 현재는 본문에서 의미 있는 문장을 추려 메모용 뼈대를 만든 상태입니다.

## 5. 내가 추가로 확인할 점
- 원문에서 중요한 수치, 날짜, 출처가 정확한지 확인하기
- 공식 출처 또는 다른 기사와 내용이 일치하는지 확인하기
- 나중에 다시 볼 때 필요한 태그 붙이기
"""


def generate_note_draft_with_groq(original_text, result, final_url, template_type, user_prompt):
    api_key = os.getenv("GROQ_API_KEY")
    _ct = result.get("content_type", "unknown")
    _is_study = (_ct == "study") or (template_type == "공부용 설명")
    if not api_key:
        # 🛠️ 디버그: 키 없음 → fallback 경로(이게 study 구조로 나오는지 추적)
        st.session_state["_study_draft_debug"] = {
            "api_key_present": False,
            "path": "local_fallback",
            "study_prompt_used": bool(_is_study),
            "template_type": template_type,
            "source_context_len": len(original_text or ""),
        }
        return make_local_content_note_draft(original_text, result, final_url, template_type, user_prompt)

    # 🛠️ 디버그: 어떤 초안 분기를 탔는지 기록 (render_result Study Debug expander에서 표시)
    st.session_state["_study_draft_debug"] = {
        "api_key_present": True,
        "path": "groq_api",
        "study_prompt_used": bool(_is_study),
        "template_type": template_type,
        "source_context_len": len((original_text or "")[:9000]) if _is_study else len(original_text or ""),
    }

    # ── 공부자료(study) 전용: 이해 중심 학습 노트 프롬프트 ──
    if _is_study:
        prompt = f"""
너는 어려운 글을 학생이 이해할 수 있게 풀어주는 학습 노트 작성 전문가다.
아래 원문 전체를 깊게 읽고, 제목만 보고 쓰는 얕은 요약이 아니라
원문의 실제 내용을 바탕으로 '이해를 돕는 학습 노트'를 만들어라.
신뢰도/광고/작성자 판단은 하지 마라. 오직 '이해'와 '복습'이 목적이다.

[URL]
{final_url or ""}

[사용자 추가 요청]
{user_prompt or "없음"}

[원문 전체]
{original_text[:9000]}

[작성 규칙]
- 한국어, Markdown으로 작성.
- 초등학생도 이해할 쉬운 설명 + 전공자 복습용 정리의 중간 수준.
- 원문에 실제로 나온 개념/단계/용어/예시를 빠짐없이 반영. 없는 내용은 지어내지 마라.
- 아래 구조를 반드시 모두 채워라:

# (원문 핵심 주제 제목)

## 한 줄 핵심
(가장 중요한 한 문장)

## 왜 필요한가 / 왜 중요한가
(배경과 동기를 쉽게)

## 단계별 동작 원리
1. ...
2. ...
(원문 흐름대로 순서있게)

## 핵심 개념 정리
### 개념1
(쉬운 설명)
### 개념2
...

## 중요한 포인트
- (헷갈리기 쉬운 점, 오해 방지)

## 한계점 / 주의할 점
- ...

## 기억법
(짧은 연상/비유로 외우기 쉽게)

## 시험·복습용 요약
- (핵심만 압축)

## 추천 태그
#태그1 #태그2
"""
    else:
        template_instruction = {
            "보고서 형식": "제목, 핵심 요약, 세부 내용, 판단 근거, 활용 메모가 있는 보고서 형식으로 정리해라.",
            "일기 형식": "개인이 나중에 다시 읽는 일기처럼 자연스럽고 주관적 메모가 가능한 형식으로 정리해라.",
            "블로그 초안 형식": "블로그에 옮기기 쉬운 흐름으로 제목, 도입, 본문, 정리, 한줄평을 포함해 정리해라.",
            "체크리스트 형식": "핵심 정보를 체크리스트와 항목별 메모 중심으로 정리해라.",
            "자유 형식": "사용자 요청에 맞춰 자유롭게 정리해라.",
        }.get(template_type, "보고서 형식으로 정리해라.")

        prompt = f"""
너는 TrustLens의 지식 아카이브 메모 작성 보조 AI다.
아래 원문 전체를 보고, 사용자가 나중에 다시 열람하기 좋은 메모 초안을 만들어라.
단순 요약이 아니라 원문의 중요한 내용을 최대한 빠짐없이 구조화해서 정리해라.
광고성 판단, 신뢰도 판단은 이미 끝났으므로 여기서는 '내용 정리'에 집중해라.

[URL]
{final_url or ""}

[분석 결과]
- 콘텐츠 유형: {CONTENT_TYPE_LABELS.get(result.get("content_type", "unknown"), result.get("content_type", "unknown"))}
- 신뢰도 점수: {result.get("trust_score", 0)}점
- 광고 위험도: {result.get("ad_risk", "mid")}
- 작성자 유형: {result.get("author_type", "-")}

[초안 템플릿]
{template_type}

[템플릿 지시]
{template_instruction}

[사용자 추가 요청]
{user_prompt or "없음"}

[원문 전체]
{original_text[:6000]}

[작성 규칙]
- 한국어로 작성해라.
- Markdown 형식으로 작성해라.
- 원문에 있는 구체 정보, 가격, 메뉴, 장소, 팁, 장단점, 재방문 의사 등을 최대한 반영해라.
- 없는 정보는 지어내지 마라.
- 지식 아카이브에서 다시 읽기 좋은 완성형 초안으로 써라.
- 태그는 본문 맨 아래에 '추천 태그'로만 정리해라.
"""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=60)
    if res.status_code == 429:
        raise RuntimeError("Groq API 사용량 제한에 걸렸어요. AI 초안은 잠시 후 다시 만들어주세요.")
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()

# -----------------------------
# Archive Functions
# -----------------------------
def save_note_to_archive(note_key, result, final_url, selected_tags):
    note_text = st.session_state.get(note_key, "")
    original_text = st.session_state.get("last_text", "")
    is_pasted_source = str(final_url or "").startswith("pasted://")

    if is_pasted_source and original_text and "## 원문 보관" not in note_text:
        note_text = (
            note_text.rstrip()
            + "\n\n---\n\n"
            + "## 원문 보관\n"
            + "> 글 붙여넣기로 분석한 자료라 원문 링크가 없어, 저장 시점의 원문을 함께 보관합니다.\n\n"
            + "```text\n"
            + original_text[:MAX_NOTE_INLINE_ORIGINAL_CHARS]
            + "\n```\n"
        )

    import uuid as _uuid
    note_id = str(_uuid.uuid4())[:8]
    note_title = result.get("archive_title", "TrustLens 메모")
    _now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # project_id 조회
    _note_proj_name = st.session_state.get("note_project_name", "기본 프로젝트")
    _note_proj_obj = next((p for p in st.session_state.get("projects", [])
                           if p.get("name") == _note_proj_name), {})
    _note_proj_id = _note_proj_obj.get("id", "")
    # task_id 조회 (note_task_name이 있으면)
    _note_task_name = st.session_state.get("note_task_name", "")
    _note_task_obj = next((t for t in st.session_state.get("tasks", [])
                           if t.get("title") == _note_task_name and
                           t.get("project") == _note_proj_name), {})
    _note_task_id = _note_task_obj.get("id", "")
    # 한 줄 핵심: AI summary 첫 문장 → 향후 P5(지식 아카이브 UX)에서 카드/상세 머리말로 사용
    _summary_val = result.get("summary", [])
    if isinstance(_summary_val, list):
        _one_line = next((str(s).strip() for s in _summary_val if str(s).strip()), "")
    else:
        _one_line = str(_summary_val).split(".")[0].strip()
    # 자동 추출 핵심 개념 목록 (P2 품질 게이트: 1글자·불용어·조사 정제)
    _note_concepts = filter_concepts(result.get("key_concepts", result.get("concepts", [])))
    st.session_state.archive_notes.append(
        {
            "id": note_id,
            "user_id": "local_user",
            "url": final_url or "",
            "title": note_title,
            "project": _note_proj_name,
            "project_id": _note_proj_id,
            "task": _note_task_name,
            "task_id": _note_task_id,
            "section": st.session_state.get("note_section_name", "일반"),
            "step": st.session_state.get("note_step_name", "없음"),
            "content_type": result.get("content_type", "unknown"),
            "score": result.get("trust_score", 0),
            "favorite": False,
            "tags": selected_tags,
            "note": note_text,
            # 원문은 붙여넣기/크롤링 모두 보관 (지식 AI가 깊게 읽을 수 있게)
            "original_text": (original_text or "")[:MAX_ORIGINAL_TEXT_CHARS],
            # ── P5(지식 아카이브 UX) 대비 사전 필드 — 현재는 채워만 두고 UI 없음 ──
            "one_line_summary": _one_line,          # 📌 한 줄 핵심
            "concepts": _note_concepts,             # 🧠 핵심 개념 (P2에서 정제)
            "related_note_ids": [],                 # 🔗 관련 메모 (추후 추천)
            "note_type": result.get("content_type", "unknown"),  # study/review/policy/info
            "last_reviewed_at": None,               # 복습 추적용
            "saved_at": _now_str,
            "created_at": _now_str,
            "updated_at": _now_str,
            "deleted_at": None,
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
    for concept in filter_concepts(_link_concepts):
        links.append({"note_id": note_id, "concept": concept, "linked_at": now_str})
    st.session_state.note_saved = True
    st.session_state.show_result = True
    # 저장 결과를 명확히 (사용자가 신규 저장/총 개수를 바로 확인 가능)
    _total_now = len(st.session_state.archive_notes)
    st.session_state["note_saved_info"] = {
        "title": note_title,
        "total": _total_now,
    }
    save_persisted_data()
    # rerun에도 사라지지 않게 즉시 토스트 + 영구 확인 메시지
    try:
        st.toast(f"✅ 지식 메모 저장 완료 (총 {_total_now}개)", icon="🗂️")
    except Exception:
        pass


# -----------------------------
# 분석결과 아카이브 Functions
# -----------------------------

def save_current_analysis_to_archive(result, final_url, selected_tags=None, memo=""):
    selected_tags = selected_tags or []
    saved_item = {
        "url": final_url or "",
        "title": result.get("archive_title", "TrustLens 분석"),
        "content_type": result.get("content_type", "unknown"),
        "score": result.get("trust_score", 0),
        "ad_risk": result.get("ad_risk", "mid"),
        "author_type": result.get("author_type", "-"),
        "summary": result.get("summary", []),
        "evidence": result.get("evidence", {}),
        "tags": selected_tags,
        "memo": memo,
        "favorite": False,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "result": result,
    }
    st.session_state.saved_analyses.insert(0, saved_item)
    st.session_state["analysis_archive_saved"] = True
    save_persisted_data()
    _flash("분석결과 아카이브에 저장했어요.", "📌")
    st.rerun()


def restore_analysis_from_archive(index):
    if 0 <= index < len(st.session_state.saved_analyses):
        item = st.session_state.saved_analyses[index]
        st.session_state.last_result = item.get("result")
        st.session_state.last_final_url = item.get("url", "")
        st.session_state.last_text = ""
        st.session_state.show_result = True
        st.session_state.result_closed = False
        st.session_state["analysis_archive_restored"] = True


def update_saved_analysis(index, memo_key, tags_key, new_tags_key=None, title_key=None):
    if 0 <= index < len(st.session_state.saved_analyses):
        st.session_state.saved_analyses[index]["memo"] = st.session_state.get(memo_key, "")
        selected_tags = st.session_state.get(tags_key, [])
        new_tags_text = st.session_state.get(new_tags_key, "") if new_tags_key else ""
        st.session_state.saved_analyses[index]["tags"] = merge_selected_and_new_tags(selected_tags, new_tags_text)

        if title_key:
            new_title = st.session_state.get(title_key, "").strip()
            if new_title:
                st.session_state.saved_analyses[index]["title"] = new_title

        st.session_state.saved_analyses[index]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        st.session_state[f"saved_analysis_updated_{index}"] = True
        save_persisted_data()


def delete_saved_analysis(index):
    if 0 <= index < len(st.session_state.saved_analyses):
        st.session_state.saved_analyses.pop(index)
        st.session_state["saved_analysis_deleted"] = True
        save_persisted_data()


def toggle_saved_analysis_favorite(index):
    if 0 <= index < len(st.session_state.saved_analyses):
        current = st.session_state.saved_analyses[index].get("favorite", False)
        st.session_state.saved_analyses[index]["favorite"] = not current
        save_persisted_data()
        st.session_state["saved_analysis_favorite_toggled"] = True

# -----------------------------
# Archive Note Update Function
# -----------------------------
def update_archive_note(index, note_key):
    edited_note = st.session_state.get(note_key, "")
    if 0 <= index < len(st.session_state.archive_notes):
        st.session_state.archive_notes[index]["note"] = edited_note
        st.session_state.archive_notes[index]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        st.session_state[f"archive_updated_{index}"] = True
        save_persisted_data()


def update_archive_note_and_tags(index, note_key, tags_key, new_tags_key=None, title_key=None):
    edited_note = st.session_state.get(note_key, "")
    selected_tags = st.session_state.get(tags_key, [])
    new_tags_text = st.session_state.get(new_tags_key, "") if new_tags_key else ""

    if 0 <= index < len(st.session_state.archive_notes):
        st.session_state.archive_notes[index]["note"] = edited_note
        st.session_state.archive_notes[index]["tags"] = merge_selected_and_new_tags(selected_tags, new_tags_text)

        if title_key:
            new_title = st.session_state.get(title_key, "").strip()
            if new_title:
                st.session_state.archive_notes[index]["title"] = new_title

        st.session_state.archive_notes[index]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        st.session_state[f"archive_updated_{index}"] = True
        save_persisted_data()


def collect_all_existing_tags():
    all_tags = []
    for source in [st.session_state.get("archive_notes", []), st.session_state.get("saved_analyses", [])]:
        for item in source:
            for tag in item.get("tags", []):
                clean = str(tag).replace("#", "").strip()
                if clean and clean not in all_tags:
                    all_tags.append(clean)
    return sorted(all_tags)


def parse_tag_input(raw_text):
    if not raw_text:
        return []
    parts = re.split(r"[,，\n]", raw_text)
    cleaned = []
    for part in parts:
        tag = str(part).replace("#", "").strip()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def merge_selected_and_new_tags(selected_tags, new_tags_text):
    merged = []
    for tag in selected_tags or []:
        clean = str(tag).replace("#", "").strip()
        if clean and clean not in merged:
            merged.append(clean)
    for tag in parse_tag_input(new_tags_text):
        if tag not in merged:
            merged.append(tag)
    return merged


def get_tag_edit_options(item):
    return sorted(set(collect_all_existing_tags() + item.get("tags", [])))

# -----------------------------
# User Feedback Save Function
# -----------------------------
def save_user_feedback(result, final_url, rating_key, useful_key, wrong_key, missing_key, memo_key):
    rating = st.session_state.get(rating_key, 3)
    useful_points = st.session_state.get(useful_key, [])
    wrong_points = st.session_state.get(wrong_key, "")
    missing_points = st.session_state.get(missing_key, "")
    feedback_memo = st.session_state.get(memo_key, "")
    trust_vote = st.session_state.get(f"trust_vote_{final_url or 'current'}", "판단 보류")
    feedback_reasons = st.session_state.get(f"feedback_reasons_{final_url or 'current'}", [])

    feedback = {
        "url": final_url or "",
        "title": result.get("archive_title", "TrustLens 분석"),
        "content_type": result.get("content_type", "unknown"),
        "score": result.get("trust_score", 0),
        "rating": rating,
        "trust_vote": trust_vote,
        "feedback_reasons": feedback_reasons,
        "useful_points": useful_points,
        "wrong_points": wrong_points,
        "missing_points": missing_points,
        "feedback_memo": feedback_memo,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    url_key = final_url or "unknown"

    if url_key not in st.session_state.auto_feedback_stats:
        st.session_state.auto_feedback_stats[url_key] = {
            "ratings": [],
            "trust_votes": []
        }

    st.session_state.auto_feedback_stats[url_key]["ratings"].append(rating)
    st.session_state.auto_feedback_stats[url_key]["trust_votes"].append(trust_vote)

    st.session_state.feedback_history.insert(0, feedback)
    st.session_state["feedback_saved"] = True
    save_persisted_data()
    
def close_current_result():
    st.session_state.show_result = False
    st.session_state.result_closed = True
    st.session_state.last_result = None
    st.session_state.last_final_url = None
    st.session_state.last_text = ""
    save_persisted_data()

    st.rerun()


# -----------------------------
# Restore/Delete/Clear Functions
# -----------------------------

def restore_analysis_from_history(cache_key):
    cached = st.session_state.analysis_cache.get(cache_key)
    # 정확히 일치하는 캐시가 없으면(예: EXTRACTION_VERSION 변경으로 키가 달라짐)
    # 같은 URL(첫 조각)로 시작하는 최신 캐시로 폴백
    if not cached:
        _url_prefix = cache_key.split("::")[0]
        for _k, _v in st.session_state.analysis_cache.items():
            if _k.split("::")[0] == _url_prefix:
                cached = _v
                cache_key = _k
                break
    if cached:
        st.session_state.last_result = cached
        parts = cache_key.split("::")
        st.session_state.last_final_url = parts[0] if parts else ""
        st.session_state.last_text = ""
        st.session_state.show_result = True
        st.session_state.result_closed = False
        st.session_state["history_restored"] = True
        # 결과 패널은 'home'과 '분석 결과(result)' 탭에서 렌더링된다.
        # 그 외 페이지(최근 기록/아카이브 등)에서 눌렀을 때만 home으로 이동시키고,
        # 결과를 그릴 수 있는 탭에 있으면 현재 탭 안에서 그대로 펼친다.
        if st.query_params.get("page", "home") not in ("home", "result"):
            st.query_params["page"] = "home"
    else:
        # 캐시가 사라진 항목 — 조용히 토스트만 뜨고 안 열리던 문제 → 명확히 안내
        st.session_state["history_restore_failed"] = True
        if st.query_params.get("page", "home") not in ("home", "result"):
            st.query_params["page"] = "home"


def delete_archive_note(index):
    if 0 <= index < len(st.session_state.archive_notes):
        st.session_state.archive_notes.pop(index)
        save_persisted_data()
        st.session_state["archive_deleted"] = True


def toggle_archive_favorite(index):
    if 0 <= index < len(st.session_state.archive_notes):
        current = st.session_state.archive_notes[index].get("favorite", False)
        st.session_state.archive_notes[index]["favorite"] = not current
        save_persisted_data()
        st.session_state["favorite_toggled"] = True


def delete_feedback_item(index):
    if 0 <= index < len(st.session_state.feedback_history):
        st.session_state.feedback_history.pop(index)
        save_persisted_data()
        st.session_state["feedback_deleted"] = True


def clear_all_saved_data():
    st.session_state.archive_notes = []
    st.session_state.search_history = []
    st.session_state.saved_analyses = []
    st.session_state.feedback_history = []
    st.session_state.analysis_cache = {}
    st.session_state.draft_cache = {}
    st.session_state.last_result = None
    st.session_state.last_final_url = None
    st.session_state.last_text = ""
    st.session_state.show_result = False
    save_persisted_data()
    st.session_state["all_data_cleared"] = True

# -----------------------------
# Visualization
# -----------------------------
def render_score_dashboard(breakdown: dict, content_type: str):
    df = get_score_dataframe(breakdown, content_type)
    if df.empty:
        st.info("표시할 점수 항목이 없어요.")
        return

    total_score = int(df["점수"].sum())
    total_max = int(df["최대점수"].sum())
    converted_score = round(total_score / total_max * 100) if total_max else 0
    top_row = df.sort_values("점수", ascending=False).iloc[0]
    weak_row = df.sort_values("달성률", ascending=True).iloc[0]

    st.markdown('<div class="chart-dashboard">', unsafe_allow_html=True)
    st.markdown(
        f'<div class="chart-title">📊 {CONTENT_TYPE_LABELS.get(content_type, "콘텐츠")} 신뢰도 대시보드</div>'
        f'<div class="chart-subtitle">각 기준이 자기 최대점수 대비 몇 % 채워졌는지 한눈에 확인해요.</div>',
        unsafe_allow_html=True,
    )

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("환산 신뢰도", f"{converted_score}점")
    with m2:
        st.metric("가장 강한 근거", str(top_row["항목"]), f'+{int(top_row["점수"])}점')
    with m3:
        st.metric("보완 필요", str(weak_row["항목"]), f'{weak_row["달성률"]}%')

    bar_fig = px.bar(
        df,
        x="항목",
        y="점수",
        color="달성률",
        text="점수",
        title="항목별 점수",
        color_continuous_scale="Blues",
        range_color=[0, 100],
    )
    bar_fig.update_layout(
        template="plotly_white",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font=dict(color="#172033", size=14),
        title=dict(font=dict(size=18, color="#172033"), x=0.02),
        coloraxis_colorbar=dict(title="달성률", title_font=dict(color="#172033"), tickfont=dict(color="#172033")),
        yaxis_title="점수",
        xaxis_title="평가 항목",
        height=360,
        margin=dict(l=20, r=20, t=70, b=40),
    )
    bar_fig.update_traces(textposition="outside")
    st.plotly_chart(bar_fig, use_container_width=True)

    c1, c2 = st.columns([1.12, 1])
    with c1:
        ratio_df = df.sort_values("달성률", ascending=True).copy()
        ratio_fig = px.bar(
            ratio_df,
            x="달성률",
            y="항목",
            orientation="h",
            text="달성률",
            title="항목별 달성률",
            range_x=[0, 100],
            color="달성률",
            color_continuous_scale="Blues",
        )
        ratio_fig.update_layout(
            template="plotly_white",
            paper_bgcolor="#ffffff",
            plot_bgcolor="#ffffff",
            font=dict(color="#172033", size=13),
            title=dict(font=dict(size=18, color="#172033"), x=0.02),
            coloraxis_showscale=False,
            xaxis_title="최대점수 대비 달성률(%)",
            yaxis_title="평가 항목",
            height=360,
            margin=dict(l=20, r=55, t=70, b=40),
        )
        ratio_fig.update_traces(
            texttemplate="%{text:.1f}%",
            textposition="outside",
            cliponaxis=False,
        )
        st.plotly_chart(ratio_fig, use_container_width=True)
    with c2:
        table_df = df.copy()
        table_df["점수"] = table_df.apply(lambda row: f"{int(row['점수'])} / {int(row['최대점수'])}점", axis=1)
        table_df["달성률"] = table_df["달성률"].apply(lambda x: f"{x}%")
        st.markdown("#### 세부 점수표")
        st.caption("점수와 달성률을 같이 보면 어떤 기준이 부족한지 바로 보여요.")
        st.dataframe(table_df[["항목", "점수", "달성률"]], use_container_width=True, hide_index=True)

    st.markdown('</div>', unsafe_allow_html=True)

# -----------------------------
# Result Renderer
# -----------------------------
def render_feedback_section(result, final_url, score):
    """사용자 피드백 + AI vs 사용자 비교 (신뢰도 보조 영역)."""
    st.markdown('<div class="feedback-shell">', unsafe_allow_html=True)
    st.markdown("### ⭐ 사용자 피드백으로 TrustLens 개선하기")
    st.caption("AI 분석에 사용자의 집단 검증을 더해요. AI 점수와 사람의 신뢰 판단 차이가 이후 보정 데이터가 됩니다.")

    feedback_base = final_url or "current"
    rating_key = f"feedback_rating_{feedback_base}"
    useful_key = f"feedback_useful_{feedback_base}"
    wrong_key = f"feedback_wrong_{feedback_base}"
    missing_key = f"feedback_missing_{feedback_base}"
    memo_key = f"feedback_memo_{feedback_base}"

    quick_col1, quick_col2, quick_col3 = st.columns([0.8, 1.1, 1.1])
    with quick_col1:
        st.slider("만족도", min_value=1, max_value=5, value=4, key=rating_key)
        st.radio(
            "AI 분석에 대한 내 판단",
            ["신뢰함", "신뢰 안함", "판단 보류"],
            horizontal=False,
            key=f"trust_vote_{final_url or 'current'}",
        )
    with quick_col2:
        st.multiselect(
            "도움 된 부분",
            ["신뢰도 점수", "광고 위험도", "작성자 유형", "핵심 요약", "AI 메모 초안", "태그 추천", "차트 시각화"],
            default=["핵심 요약", "차트 시각화"],
            key=useful_key,
        )
        st.multiselect(
            "평가 이유",
            FEEDBACK_REASON_OPTIONS,
            default=[],
            key=f"feedback_reasons_{final_url or 'current'}",
        )
    with quick_col3:
        st.text_area("추가 필요/아쉬운 점", placeholder="예: 사진 개수 반영, 점수 기준 설명 강화 등", height=140, key=missing_key)

    # 상위에서 expander 안에 렌더되므로 중첩 expander 금지 → 체크박스 토글로 대체
    if st.checkbox("✍️ 자세한 피드백 남기기", key=f"detail_fb_{feedback_base}"):
        st.text_area("틀렸거나 어색한 부분", placeholder="예: 맛집 후기인데 공식 출처 기준이 보이면 어색함 / 점수가 너무 낮음", height=90, key=wrong_key)
        st.text_area("자유 피드백", placeholder="TrustLens가 다음 분석에서 더 잘 판단했으면 하는 기준을 적어주세요.", height=90, key=memo_key)
    else:
        # 키가 항상 존재하도록 기본값 보장 (저장 시 KeyError 방지)
        st.session_state.setdefault(wrong_key, "")
        st.session_state.setdefault(memo_key, "")

    if st.button("📩 피드백 저장하기", key=f"save_feedback_{feedback_base}", use_container_width=True, type="primary"):
        save_user_feedback(result, final_url, rating_key, useful_key, wrong_key, missing_key, memo_key)

    if st.session_state.get("feedback_saved"):
        st.success("피드백을 저장했어요. 최근 검색 기록 메뉴에서 피드백 기록도 확인할 수 있어요.")
        st.session_state["feedback_saved"] = False

    if st.session_state.feedback_history:
        recent_feedback = st.session_state.feedback_history[0]
        st.markdown(
            f'''
            <div class="learning-box">
            🧠 <b>누적 피드백 기반 개선 신호</b><br>
            최근 만족도: {recent_feedback.get("rating", "-")} / 5<br>
            도움 된 부분: {", ".join(recent_feedback.get("useful_points", [])) or "없음"}<br>
            보완 요청: {recent_feedback.get("missing_points", "없음") or "없음"}<br><br>
            <span class="feedback-chip">사용자 피드백</span>
            <span class="feedback-chip">점수 기준 보정</span>
            <span class="feedback-chip">AI 초안 개선</span>
            <span class="feedback-chip">태그 학습 데이터</span>
            </div>
            ''',
            unsafe_allow_html=True,
        )

    feedback_summary = summarize_user_feedback_for_url(final_url or "")
    st.markdown("### 🤝 AI vs 사용자 의견 비교")
    if feedback_summary["total"] == 0:
        st.info("아직 이 URL에 대한 사용자 평가가 없어요. 첫 평가를 남기면 비교 데이터가 시작돼요.")
    else:
        a, b, c = st.columns(3)
        with a:
            st.markdown(f'<div class="compare-box"><div class="metric-label">사용자 신뢰함</div><div class="compare-number">{feedback_summary["trust_pct"]}%</div><div class="metric-sub">{feedback_summary["trust"]}명 / 총 {feedback_summary["total"]}명</div></div>', unsafe_allow_html=True)
        with b:
            st.markdown(f'<div class="compare-box"><div class="metric-label">사용자 신뢰 안함</div><div class="compare-number">{feedback_summary["distrust_pct"]}%</div><div class="metric-sub">{feedback_summary["distrust"]}명 / 총 {feedback_summary["total"]}명</div></div>', unsafe_allow_html=True)
        with c:
            gap = abs(score - feedback_summary["trust_pct"])
            st.markdown(f'<div class="compare-box"><div class="metric-label">AI-사용자 차이</div><div class="compare-number">{gap}p</div><div class="metric-sub">AI {score}점 vs 사용자 신뢰 {feedback_summary["trust_pct"]}%</div></div>', unsafe_allow_html=True)

        if feedback_summary["reason_counts"]:
            reason_html = "".join([f'<span class="reason-chip">{reason} {count}</span>' for reason, count in sorted(feedback_summary["reason_counts"].items(), key=lambda x: x[1], reverse=True)])
            st.markdown(f"**사용자 평가 이유 Top 신호**<br>{reason_html}", unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


def render_result(result, extracted_text=None, final_url=None):
    score = result.get("trust_score", 0)
    breakdown = result.get("score_breakdown", {})
    ad_risk = result.get("ad_risk", "mid")
    author_type = result.get("author_type", "-")
    is_official = result.get("is_official", False)
    official_org = result.get("official_org", "")
    tags_pos = result.get("tags_positive", [])
    tags_warn = result.get("tags_warning", [])
    summary = result.get("summary", [])
    content_type = result.get("content_type", "unknown")
    evidence = result.get("evidence", {})

    ad_emoji = {"low": "🟢", "mid": "🟡", "high": "🔴"}.get(ad_risk, "⚪")
    ad_text = {"low": "낮음", "mid": "주의", "high": "위험"}.get(ad_risk, "-")
    content_label = CONTENT_TYPE_LABELS.get(content_type, content_type)

    # result-shell wrapper removed
    st.markdown("## 📊 신뢰도 분석 결과")

    url_feedback = st.session_state.get(
        "auto_feedback_stats",
        {}
    ).get(final_url or "", {})

    ratings = url_feedback.get("ratings", [])

    avg_rating = (
        round(sum(ratings) / len(ratings), 1)
        if ratings else 0
    )

    if ratings:
        st.info(
            f"⭐ 사용자 평균 만족도 "
            f"{avg_rating}/5 · "
            f"누적 평가 {len(ratings)}건"
        )
    if final_url:
        st.caption(f"분석 출처: {display_source_label(final_url)}")

    st.divider()

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.markdown(f'<div class="metric-card"><div class="metric-label">신뢰도 점수</div><div class="metric-value">{score}점</div><div class="metric-sub">콘텐츠 유형별 환산 점수</div></div>', unsafe_allow_html=True)
    with k2:
        st.markdown(f'<div class="metric-card"><div class="metric-label">광고 위험도</div><div class="metric-value">{ad_emoji} {ad_text}</div><div class="metric-sub">광고/협찬 신호 기반</div></div>', unsafe_allow_html=True)
    with k3:
        st.markdown(f'<div class="metric-card"><div class="metric-label">작성자 유형</div><div class="metric-value">{author_type}</div><div class="metric-sub">글쓰기 성향 분류</div></div>', unsafe_allow_html=True)
    with k4:
        st.markdown(f'<div class="metric-card"><div class="metric-label">콘텐츠 유형</div><div class="metric-value">{content_label}</div><div class="metric-sub">분석 기준 자동 적용</div></div>', unsafe_allow_html=True)

    if is_official and official_org:
        st.success(f"✅ 공식 출처 확인됨 — {official_org}")
    elif content_type == "policy":
        st.warning("⚠️ 정책/지원사업 정보인데 공식 출처가 확인되지 않았어요. 공식 사이트 추가 확인을 추천해요.")

    official_matches = detect_official_source(final_url or "", st.session_state.get("last_text", ""))
    if content_type == "policy":
        if official_matches:
            official_html = "".join([f'<span class="reason-chip">{item["label"]} · {item["domain"]}</span>' for item in official_matches])
            st.markdown(
                f'''
                <div class="official-card">
                ✅ <b>공식 출처 우선 필터</b><br>
                정책/지원사업 정보에서 공식 기관 신호를 감지했어요.<br>
                {official_html}<br>
                공식 페이지 기준으로 신청기간, 자격조건, 제출서류를 최종 확인하는 것을 추천해요.
                </div>
                ''',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '''
                <div class="official-warning-card">
                ⚠️ <b>공식 출처 우선 확인 필요</b><br>
                정책/지원사업 정보인데 URL 또는 본문에서 공식 기관 도메인 신호가 약해요.<br>
                go.kr, seoul.go.kr, work24.go.kr, hrd.go.kr 같은 공식 사이트에서 한 번 더 확인해보세요.
                </div>
                ''',
                unsafe_allow_html=True,
            )

    st.divider()

    step3_tabs = st.tabs(["📌 핵심 요약", "🔍 신뢰도 판단", "🏷️ 개념·태그 후보", "▶️ 다음 행동"])

    # ── 탭 1: 핵심 요약 ──
    with step3_tabs[0]:
        st.markdown('<div class="summary-box">', unsafe_allow_html=True)
        st.markdown('<div class="summary-title">💡 AI 핵심 요약</div>', unsafe_allow_html=True)
        if isinstance(summary, list):
            for s in summary:
                st.markdown(f"- {s}")
        else:
            for s in [x.strip() for x in str(summary).split(".") if x.strip()]:
                st.markdown(f"- {s}.")
        st.markdown('</div>', unsafe_allow_html=True)
        st.caption("이 메모의 핵심만 빠르게 확인하세요. 자세한 신뢰도 근거는 옆 탭에 있어요.")

    # ── 탭 2: 신뢰도 판단 ──
    with step3_tabs[1]:
        st.markdown("### 📋 점수 근거")
        st.caption("각 항목은 실제 점수 / 최대 점수 비율로 표시돼요.")
        items = get_score_items_for_type(content_type)
        for key, label, max_val in items:
            val = get_int_score(breakdown, key)
            ratio = val / max_val if max_val else 0
            st.markdown(f"**{label}** · {val}/{max_val}점 · {round(ratio * 100)}%")
            st.progress(float(ratio))

        st.markdown("#### 🔎 판단 근거")
        jtab1, jtab2, jtab3 = st.tabs(["원문 근거", "작성자 분석", "광고 판단"])
        with jtab1:
            st.markdown(f"**공식 출처 근거:** {evidence.get('official_source', '없음')}")
            st.markdown(f"**경험 신호 근거:** {evidence.get('experience_signal', '없음')}")
            st.markdown(f"**단점/비판 신호:** {evidence.get('negative_signal', '없음')}")
        with jtab2:
            st.markdown(f"**유형:** {author_type}")
            st.markdown(f"**판단 이유:** {result.get('author_reason', '')}")
        with jtab3:
            st.markdown(f"**위험도:** {ad_emoji} {ad_text}")
            st.markdown(f"**판단 이유:** {result.get('ad_risk_reason', '')}")
            st.markdown(f"**광고 신호:** {evidence.get('ad_signal', '없음')}")

        with st.expander("📊 신뢰도 점수 상세 차트 보기", expanded=False):
            render_score_dashboard(breakdown, content_type)
        with st.expander("⭐ 사용자 피드백 남기기 / AI vs 사용자 비교", expanded=False):
            render_feedback_section(result, final_url, score)

    # ── 탭 3: 개념·태그 후보 ──
    with step3_tabs[2]:
        st.markdown("### 🏷️ AI 태그")
        tag_html = ""
        for tag in tags_pos:
            clean_tag = str(tag).replace("#", "").strip()
            if clean_tag:
                tag_html += f'<span class="tag-badge">#{clean_tag}</span>'
        for tag in tags_warn:
            clean_tag = str(tag).replace("#", "").strip()
            if clean_tag:
                tag_html += f'<span class="tag-warn-badge">⚠ #{clean_tag}</span>'
        st.markdown(tag_html if tag_html else "생성된 태그가 없어요.", unsafe_allow_html=True)

        _key_concepts = result.get("key_concepts", result.get("concepts", []))
        if _key_concepts:
            st.markdown("### 🧩 핵심 개념 후보")
            st.caption("AI가 본문에서 뽑은 개념 후보예요. 메모 저장 시 자동으로 연결돼요.")
            concept_html = "".join(
                f'<span class="tag-badge">{str(c).strip()}</span>'
                for c in _key_concepts if str(c).strip()
            )
            st.markdown(concept_html, unsafe_allow_html=True)
        else:
            st.caption("아직 추출된 핵심 개념 후보가 없어요.")

        st.info(
            "🚧 지금은 AI가 추천한 태그·개념을 **그대로** 보여줘요. "
            "다음 단계(MVP3)에서 **후보 품질 평가 → 추천/검토필요/제외 → 사용자 승인** 구조로 개선될 예정이에요."
        )

    # ── 탭 4: 다음 행동 ──
    with step3_tabs[3]:
        st.markdown("### ▶️ 다음 행동")
        st.markdown("**지금 할 수 있어요**")
        st.markdown("- 🗒️ 아래 **‘지식 메모 만들기’** 영역에서 AI 초안을 정리해 저장하기")
        st.markdown("- 📌 신뢰도 **분석결과만** 따로 아카이브에 저장하기 (STEP4 저장 옵션)")
        st.markdown("- 📁 저장할 때 **프로젝트 / 섹션 / 단계**에 연결하기")
        st.info("🚧 **추후 예정** — 이 분석에서 바로 **작업(Task) 생성**, **연구노트 연결**로 이어지는 기능이 추가될 예정이에요.")
        if ratings:
            st.success(f"🧠 사용자들이 이 분석을 평균 {avg_rating}/5 로 평가했어요.")
        else:
            st.caption("🧠 아직 사용자 평가 데이터가 없어요.")

    _dbg = result.get("_debug", {})
    _draft_dbg = st.session_state.get("_study_draft_debug", {})
    with st.expander("🛠️ Study Debug (개발용)", expanded=False):
        st.code(
            "selected_type      = {}\n"
            "ai_content_type    = {}\n"
            "final_content_type = {}\n"
            "study_prompt_used  = {}\n"
            "draft_template     = {}\n"
            "source_context_len = {}\n"
            "analysis_text_len  = {}\n"
            "analysis_source    = {}\n"
            "draft_key          = {}".format(
                _dbg.get("selected_type", "-"),
                _dbg.get("ai_content_type", "-"),
                _dbg.get("final_content_type", "-"),
                _draft_dbg.get("study_prompt_used", "(초안 미생성)"),
                _draft_dbg.get("template_type", "-"),
                _draft_dbg.get("source_context_len", "-"),
                _dbg.get("source_text_len", "-"),
                st.session_state.get("_last_cache_key", "-"),
                f"note_draft_{final_url or 'current'}_{result.get('content_type', 'unknown')}",
            ),
            language="text",
        )
        if _dbg.get("selected_type") == "study" and _dbg.get("final_content_type") != "study":
            st.error("⚠️ 공부자료를 골랐는데 final_content_type이 study가 아니에요 — 분기 점검 필요")

    if extracted_text:
        with st.expander("🧪 추출된 본문 확인 / 디버그"):
            st.markdown(f"본문 길이: **{len(extracted_text)}자**")
            st.text(extracted_text[:2500])

    st.divider()
    st.markdown('<div class="memo-shell">', unsafe_allow_html=True)
    st.markdown("### 🗂️ 저장 및 메모 만들기")
    st.caption("왼쪽은 긴 지식 메모 작성, 오른쪽은 분석결과 자체 저장용이에요.")

    tag_options = []
    for tag in result.get("tags_positive", []) + result.get("tags_warning", []):
        clean_tag = str(tag).replace("#", "").strip()
        if clean_tag and clean_tag not in tag_options:
            tag_options.append(clean_tag)

    # 캐시 키에 content_type 포함 — 같은 URL을 일반정보글/공부자료 등 다른 유형으로
    # 재분석할 때 이전 유형의 초안이 그대로 재사용되던 버그 방지
    _ct_for_key = result.get("content_type", "unknown")
    draft_key = f"note_draft_{final_url or 'current'}_{_ct_for_key}"
    note_key = f"edited_{draft_key}"

    _is_study_content = result.get("content_type") == "study"
    _default_template = "공부용 설명" if _is_study_content else "보고서 형식"

    if draft_key not in st.session_state:
        original_text_for_draft = st.session_state.get("last_text", "")
        if original_text_for_draft:
            if _is_study_content:
                # 공부자료는 AI 학습노트 초안을 시도하되, 429/네트워크 등으로
                # 실패해도 패널 전체가 죽지 않도록 로컬 학습노트 뼈대로 폴백한다.
                try:
                    st.session_state[draft_key] = generate_note_draft_with_groq(
                        original_text_for_draft,
                        result,
                        final_url,
                        _default_template,
                        "",
                    )
                except Exception as _draft_err:
                    st.session_state["draft_fallback_reason"] = str(_draft_err)
                    st.session_state[draft_key] = make_local_content_note_draft(
                        original_text_for_draft,
                        result,
                        final_url,
                        _default_template,
                        "",
                    )
            else:
                st.session_state[draft_key] = make_local_content_note_draft(
                    original_text_for_draft,
                    result,
                    final_url,
                    _default_template,
                    "",
                )
        else:
            st.session_state[draft_key] = make_basic_note_draft(
                result,
                final_url,
                st.session_state.get(f"selected_tags_{final_url or 'current'}", []),
            )
    if note_key not in st.session_state:
        st.session_state[note_key] = st.session_state[draft_key]

    if st.session_state.get("draft_fallback_reason"):
        st.info(
            "⚠️ AI 학습노트 초안 생성에 실패해서 로컬 규칙으로 만든 뼈대를 보여드려요. "
            f"(사유: {st.session_state['draft_fallback_reason']}) "
            "초안은 직접 수정해서 그대로 저장할 수 있어요."
        )
        del st.session_state["draft_fallback_reason"]

    note_panel, save_panel = st.columns(2, gap="large")

    with note_panel:
        st.markdown(
            '<div class="note-action-card"><h2>🗒️ 지식 메모 만들기</h2><p>AI 초안을 만들고 수정해서 긴 메모로 저장해요.</p></div>',
            unsafe_allow_html=True,
        )
        _template_options = ["공부용 설명", "보고서 형식", "일기 형식", "블로그 초안 형식", "체크리스트 형식", "자유 형식"]
        template_type = st.selectbox(
            "AI 초안 템플릿 선택",
            _template_options,
            index=_template_options.index(_default_template),
            key=f"template_{final_url or 'current'}_{_ct_for_key}",
        )
        user_draft_prompt = st.text_area(
            "초안에 반영할 추가 요청",
            placeholder="예: 가격과 팁을 표로 정리해줘 / 블로그에 올릴 수 있게 정리해줘 / 내 말투처럼 자연스럽게 정리해줘",
            height=110,
            key=f"draft_prompt_{final_url or 'current'}",
        )
        st.markdown('<div class="knowledge-draft-blue-button"></div>', unsafe_allow_html=True)
        if st.button(
            "🔄 지식 메모 초안 다시 만들기",
            key=f"refresh_{draft_key}",
            type="secondary",
            use_container_width=True,
        ):
            original_text = st.session_state.get("last_text", "")
            if not original_text:
                st.warning("원문이 저장되어 있지 않아요.")
            else:
                draft_cache_key = f"{final_url or 'current'}::{_ct_for_key}::{template_type}::{user_draft_prompt.strip()}"
                if draft_cache_key in st.session_state.draft_cache:
                    new_draft = st.session_state.draft_cache[draft_cache_key]
                    st.session_state[draft_key] = new_draft
                    st.session_state[note_key] = new_draft
                    st.info("같은 조건의 AI 초안이 있어 다시 불러왔어요.")
                else:
                    with st.spinner("원문 전체를 보고 AI가 메모 초안을 만드는 중..."):
                        try:
                            new_draft = generate_note_draft_with_groq(
                                original_text,
                                result,
                                final_url,
                                template_type,
                                user_draft_prompt,
                            )
                            st.session_state.draft_cache[draft_cache_key] = new_draft
                            st.session_state[draft_key] = new_draft
                            st.session_state[note_key] = new_draft
                            save_persisted_data()
                            st.success("AI 초안을 만들었어요. 아래에서 수정 후 저장할 수 있어요.")
                        except Exception as e:
                            st.error(f"AI 초안 생성 중 오류 발생: {e}")

    with save_panel:
        if st.session_state.get("analysis_archive_saved"):
            st.success("분석결과 아카이브에 저장했어요.")
            st.session_state["analysis_archive_saved"] = False

        st.markdown(
            '<div class="archive-action-card"><h2>📌 분석결과 바로 저장</h2><p>지금 분석한 결과를 아카이브에 저장해요.</p></div>',
            unsafe_allow_html=True,
        )
        selected_tags = st.multiselect(
            "저장할 태그 선택",
            options=tag_options,
            default=tag_options,
            key=f"selected_tags_{final_url or 'current'}",
        )
        analysis_archive_memo_key = f"analysis_archive_memo_{final_url or 'current'}"
        st.text_area(
            "분석결과에 남길 짧은 메모",
            placeholder="예: 속초 맛집 후보 / 정책 정보 재확인 필요 / 광고성 낮아 보임",
            height=120,
            key=analysis_archive_memo_key,
        )
        st.markdown('<div class="big-action-button red-action"></div>', unsafe_allow_html=True)
        if st.button(
            "🔴 현재 분석결과 아카이브에 저장",
            key=f"save_analysis_archive_{final_url or 'current'}",
            use_container_width=True,
            type="primary",
        ):
            save_current_analysis_to_archive(
                result,
                final_url,
                selected_tags=selected_tags,
                memo=st.session_state.get(analysis_archive_memo_key, ""),
            )

    # 전체 너비 메모 편집 영역
    st.divider()
    st.markdown(
        "<div style='background:linear-gradient(135deg,#eff6ff,#e0e7ff);border:1px solid #bfdbfe;"
        "border-left:5px solid #2563eb;border-radius:12px;padding:12px 16px;margin:4px 0 12px;'>"
        "<b style='color:#1d4ed8;font-size:1.02rem;'>📁 저장 위치 정하기</b>"
        "<div style='color:#475569;font-size:0.86rem;margin-top:2px;'>"
        "이 메모를 <b>어느 프로젝트·섹션·단계</b>에 넣을지 골라요. (지식맵 연결에 쓰여요)</div></div>",
        unsafe_allow_html=True)
    proj_col, sec_col, step_col = st.columns(3)
    _projects = st.session_state.get("projects", [])
    _proj_names = [p["name"] for p in _projects]
    with proj_col:
        if _proj_names:
            _sel_proj = st.selectbox(
                "📁 프로젝트",
                ["기본 프로젝트"] + _proj_names,
                key="note_project_name",
                help="저장할 프로젝트를 선택하세요."
            )
        else:
            st.text_input("📁 프로젝트", value="기본 프로젝트", key="note_project_name")
            st.caption("프로젝트를 먼저 만들면 여기서 선택할 수 있어요.")
    with sec_col:
        _sel_proj_name = st.session_state.get("note_project_name", "기본 프로젝트")
        _sel_proj_obj = next((p for p in _projects if p["name"] == _sel_proj_name), None)
        _sections = [s for s in st.session_state.get("project_sections", [])
                     if _sel_proj_obj and s.get("project_id") == _sel_proj_obj.get("id")]
        _section_names = [s["name"] for s in _sections]
        if _section_names:
            st.selectbox(
                "📂 섹션",
                ["일반"] + _section_names,
                key="note_section_name",
                help="저장할 섹션을 선택하세요."
            )
        else:
            st.text_input("📂 섹션", value="일반", key="note_section_name")
            if _proj_names:
                st.caption("프로젝트에 섹션을 추가하면 여기서 선택 가능해요.")
    with step_col:
        _sel_sec_name = st.session_state.get("note_section_name", "일반")
        _sel_sec_obj = next((s for s in _sections if s["name"] == _sel_sec_name), None)
        _steps = [stp for stp in st.session_state.get("project_steps", [])
                  if _sel_sec_obj and stp.get("section_id") == _sel_sec_obj.get("id")]
        _step_names = [stp["name"] for stp in _steps]
        if _step_names:
            st.selectbox(
                "🔖 단계",
                ["없음"] + _step_names,
                key="note_step_name",
                help="저장할 단계를 선택하세요."
            )
        else:
            st.text_input("🔖 단계", value="없음", key="note_step_name")
            if _sel_sec_obj:
                st.caption("섹션에 단계를 추가하면 여기서 선택 가능해요.")

    st.markdown("### ✍️ 메모 초안 편집")
    st.caption("AI 초안을 기반으로 내 메모를 정리한 뒤, 아래에서 지식 메모로 저장해요.")
    if str(final_url or "").startswith("pasted://"):
        st.info(
            "붙여넣기로 분석한 글은 원문 링크가 없어서, "
            "지식 메모 저장 시 원문이 메모 맨 아래에 자동 보관돼요."
        )

    st.text_area(
        "AI 초안 기반으로 내 메모 정리하기",
        height=600,
        key=note_key,
        help="마크다운으로 정리하면 아카이브 읽기 화면에서 그대로 렌더링돼요. 예: ## 질문, - 목록, - [ ] 체크, **강조**, > 인용",
    )

    st.divider()
    st.markdown("### 4️⃣ 저장 방식 선택")
    st.caption("이 자료를 어떻게 보관할지 골라주세요. 보통은 ‘지식 메모로 저장’이면 충분해요.")

    _sel_tags = st.session_state.get(f"selected_tags_{final_url or 'current'}", [])
    save_a_col, save_b_col, save_c_col = st.columns(3)
    with save_a_col:
        if st.button(
            "🗂️ 지식 메모로 저장",
            key=f"save_note_only_{draft_key}",
            use_container_width=True,
            type="primary",
            help="AI 초안을 정리한 지식 메모를 아카이브에 저장해요. (가장 많이 쓰는 방식)",
        ):
            try:
                save_note_to_archive(note_key, result, final_url, _sel_tags)
            except Exception as _e:
                st.error(f"지식 메모 저장 실패: {_e}")
    with save_b_col:
        if st.button(
            "📌 분석결과만 저장",
            key=f"save_analysis_only_{draft_key}",
            use_container_width=True,
            help="신뢰도 분석결과(점수/근거)만 분석 아카이브에 저장해요.",
        ):
            save_current_analysis_to_archive(
                result,
                final_url,
                selected_tags=_sel_tags,
                memo=st.session_state.get(f"analysis_archive_memo_{final_url or 'current'}", ""),
            )
    with save_c_col:
        if st.button(
            "🧩 둘 다 저장",
            key=f"save_both_{draft_key}",
            use_container_width=True,
            help="지식 메모와 분석결과를 모두 저장해요.",
        ):
            # 주의: save_current_analysis_to_archive()는 끝에서 st.rerun()을 호출하므로
            # 반드시 지식 메모 저장을 '먼저' 실행해야 한다. 순서가 바뀌면 rerun 때문에
            # 메모 저장이 통째로 건너뛰어진다(둘 다 저장 토스트/저장 누락 버그의 원인).
            _both_note_ok = False
            try:
                save_note_to_archive(note_key, result, final_url, _sel_tags)
                _both_note_ok = True
            except Exception as _e:
                st.error(f"지식 메모 저장 실패: {_e}")
            # rerun 후에도 살아남도록 통합 저장 결과 플래그 세팅
            st.session_state["both_saved_info"] = {
                "note_ok": _both_note_ok,
                "notes_total": len(st.session_state.get("archive_notes", [])),
                "analyses_total": len(st.session_state.get("saved_analyses", [])) + 1,
            }
            save_current_analysis_to_archive(
                result,
                final_url,
                selected_tags=_sel_tags,
                memo=st.session_state.get(f"analysis_archive_memo_{final_url or 'current'}", ""),
            )

    st.button(
        "닫기 / 나가기",
        key=f"close_{draft_key}",
        use_container_width=True,
        on_click=close_current_result,
    )

    if st.session_state.get("both_saved_info"):
        _bi = st.session_state.get("both_saved_info") or {}
        if _bi.get("note_ok"):
            st.success(
                f"✅ 지식 메모와 분석결과를 모두 저장했어요. "
                f"(지식 메모 총 {_bi.get('notes_total','?')}개 · 분석결과 총 {_bi.get('analyses_total','?')}개)"
            )
            try:
                st.toast("지식 메모 + 분석결과 저장 완료", icon="🧩")
            except Exception:
                pass
        else:
            st.warning(
                f"분석결과는 저장했지만 지식 메모 저장에 실패했어요. "
                f"(분석결과 총 {_bi.get('analyses_total','?')}개)"
            )
        st.session_state["both_saved_info"] = None
        st.session_state.note_saved = False
        st.session_state["note_saved_info"] = None
    elif st.session_state.get("note_saved"):
        _si = st.session_state.get("note_saved_info") or {}
        if _si:
            st.success(f"✅ 신규 저장 완료 — 「{_si.get('title','')}」 (지식 아카이브 총 {_si.get('total','?')}개)")
        else:
            st.success("지식 아카이브에 저장했어요.")
        st.session_state.note_saved = False
        st.session_state["note_saved_info"] = None

    st.markdown('</div>', unsafe_allow_html=True)


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

    for idx, item in enumerate(st.session_state.get("archive_notes", [])):
        items.append({
            "kind": "지식 메모",
            "title": item.get("title", "저장 메모"),
            "project": item.get("project", "기본 프로젝트"),
            "section": item.get("section", "일반"),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "tags": item.get("tags", []),
            "date": item.get("saved_at", ""),
            "memo": item.get("note", ""),
            "full_text": "\n".join([str(item.get("note", "")), str(item.get("original_text", ""))]),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": f"item_{idx}_{len(items)}",
        })

    for idx, item in enumerate(st.session_state.get("saved_analyses", [])):
        _res = item.get("result", item)  # 분석 결과 dict
        # AI가 추출한 key_concepts, tags 를 tags 필드에 합산
        _ai_tags = list(item.get("tags", []))
        for _kc in _res.get("key_concepts", _res.get("concepts", [])):
            _kc_str = _kc.strip() if isinstance(_kc, str) else str(_kc).strip()
            if _kc_str and _kc_str not in _ai_tags:
                _ai_tags.append(_kc_str)
        for _tp in _res.get("tags_positive", []):
            _tp_str = str(_tp).replace("#","").strip()
            if _tp_str and _tp_str not in _ai_tags:
                _ai_tags.append(_tp_str)
        items.append({
            "kind": "분석 결과",
            "title": item.get("title", "저장 분석"),
            "project": item.get("project", "분석결과"),
            "section": item.get("section", item.get("content_type", "일반")),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "tags": _ai_tags,
            "date": item.get("saved_at", ""),
            "memo": item.get("memo", ""),
            "full_text": "\n".join(item.get("summary", [])) if isinstance(item.get("summary", []), list) else str(item.get("summary", "")),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": f"item_{idx}_{len(items)}",
        })

    return items


def get_knowledge_uid(item):
    base = "|".join([
        str(item.get("raw_index", "")),
        str(item.get("kind", "")),
        str(item.get("title", "")),
        str(item.get("date", "")),
    ])
    return str(abs(hash(base)))


def get_overridden_category(item, level, fallback):
    uid = get_knowledge_uid(item)
    overrides = st.session_state.get("pkm_category_overrides", {})
    return overrides.get(uid, {}).get(level, fallback)


def save_knowledge_category_override(item, large_key, middle_key):
    uid = get_knowledge_uid(item)
    overrides = st.session_state.get("pkm_category_overrides", {})
    overrides[uid] = {
        "large": st.session_state.get(large_key, infer_large_category(item)),
        "middle": st.session_state.get(middle_key, infer_middle_category(item)),
    }
    st.session_state.pkm_category_overrides = overrides
    save_persisted_data()


def infer_large_category(item):
    uid = get_knowledge_uid(item) if "get_knowledge_uid" in globals() else ""
    if uid and st.session_state.get("pkm_category_overrides", {}).get(uid, {}).get("large"):
        return st.session_state.pkm_category_overrides[uid]["large"]
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
    uid = get_knowledge_uid(item) if "get_knowledge_uid" in globals() else ""
    if uid and st.session_state.get("pkm_category_overrides", {}).get(uid, {}).get("middle"):
        return st.session_state.pkm_category_overrides[uid]["middle"]
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
    if 0 <= diff <= 1:
        return "오늘/어제"
    if 0 <= diff <= 7:
        return "최근 7일"
    if 0 <= diff <= 30:
        return "최근 30일"
    return "오래된 기록"


def extract_local_concepts(text, tags=None, limit=18):
    """저장된 메모에서 반복적으로 등장하는 핵심 개념을 간단한 로컬 규칙으로 추출한다."""
    tags = tags or []
    text = str(text or "")
    candidates = []

    for tag in tags:
        clean = str(tag).replace("#", "").strip()
        if len(clean) >= 2 and clean not in candidates:
            candidates.append(clean)

    keyword_pool = [
        "CREST", "STP", "4P", "SWOT", "OAP", "ESG", "CSR", "O2O", "B2B", "B2C", "B2G",
        "개인정보보호법", "전자금융거래법", "전자서명법", "식품위생법", "사회적기업", "공공데이터",
        "블록체인", "위치기반", "결제시스템", "기부", "후원", "소액기부", "마케팅", "경쟁사",
        "시장규모", "시장세분화", "포지셔닝", "정량적 목표", "사용자", "소상공인", "공공기관",
        "결식아동", "급식카드", "지역상권", "기술", "규제", "경제", "사회", "발표대본", "자료조사"
    ]
    for word in keyword_pool:
        if word in text and word not in candidates:
            candidates.append(word)

    # 한글/영문 혼합 명사 후보를 추가로 추출한다.
    for word in re.findall(r"[A-Za-z]{2,}|[가-힣]{2,12}", text):
        if word in candidates:
            continue
        if word in ["그리고", "하지만", "있는", "없는", "관련", "내용", "부분", "확인", "필요", "분석", "자료"]:
            continue
        if len(word) >= 2:
            candidates.append(word)
        if len(candidates) >= limit:
            break

    return candidates[:limit]


# ════════════════════════════════════════════════════════════════
# 🔗 AI 연결 추천 (로컬 규칙 기반) — 1단계: 계산 + 미리보기만(데이터 반영 X)
# ════════════════════════════════════════════════════════════════
# 명확한 행동만 작업으로 (정리/작성/조사 등 문맥 의존어는 기본 제외 — 오탐 줄이기)
_TASK_KEYWORDS = ("예약", "확인", "신청", "등록", "구매", "사기", "보내", "제출",
                  "예매", "결제", "신고", "문의", "발송", "접수", "신청하", "회신")
# 비행동 표현 — 이런 단어가 들어간 줄은 작업으로 안 잡음
_NON_ACTION_HINTS = ("고민", "필요", "중요", "생각", "같다", "같아", "듯", "인 것",
                     "려고", "할까", "어떨", "느낌")
# 진짜 행동 어미 — 작업으로 인정하려면 액션 키워드 + 이런 형태여야
_ACTION_SUFFIX = ("하기", "하고", "해야", "하자", "할", "했", "해", "예약", "확인")

def build_reco(note):
    """메모 하나에 대한 연결 후보를 로컬 규칙으로 계산한다. (순수 함수, 데이터 변경 없음)
    반환: {concepts, tags, project, related_notes, routes, tasks} — 각 항목에 reason 포함."""
    if not isinstance(note, dict):
        return {}
    _body = " ".join([str(note.get("title", "")), str(note.get("note", "")),
                      str(note.get("original_text", ""))]).strip()
    _own_tags = [str(t).replace("#", "").strip() for t in (note.get("tags", []) or []) if str(t).strip()]
    _own_id = note.get("id")
    _own_proj = _clean_text_value(note.get("project")).strip()

    _notes = st.session_state.get("archive_notes", [])
    _links = st.session_state.get("note_concept_links", [])
    _projects = [p for p in st.session_state.get("projects", [])
                 if isinstance(p, dict) and _clean_text_value(p.get("name")).strip()]

    # 성장형 엔진: 이미 적용(applied)·무시(ignored)한 추천은 다시 제안하지 않음
    _applied = note.get("reco_applied", {}) or {}
    _ignored = note.get("reco_ignored", {}) or {}
    def _excluded(_type, _val):
        return _val in (_applied.get(_type, []) or []) or _val in (_ignored.get(_type, []) or [])
    _ex_con = {canonical_concept(x) for x in (_applied.get("concepts", []) or []) + (_ignored.get("concepts", []) or [])}
    _ex_tag = {str(x).replace("#", "").strip() for x in (_applied.get("tags", []) or []) + (_ignored.get("tags", []) or [])}
    _ex_task = set((_applied.get("tasks", []) or []) + (_ignored.get("tasks", []) or []))
    _ex_rel = set((_applied.get("related_notes", []) or []) + (_ignored.get("related_notes", []) or []))

    # ── 🧠 개념 추천 ── 자동 추출 → canonical → 이미 연결/적용/무시된 건 제외
    _auto = canonical_concepts(extract_local_concepts(_body, _own_tags, limit=10))
    _already = {canonical_concept(c) for c in (note.get("concepts", []) or [])} | _ex_con
    _existing_all = {canonical_concept(l.get("concept")) for l in _links if l.get("concept")}
    _con_reco = []
    for _c in _auto:
        if not _c or _c in _already:
            continue
        _is_existing = _c in _existing_all
        _con_reco.append({
            "name": _c,
            "reason": "기존 개념과 일치" if _is_existing else "본문에서 자동 추출",
            "existing": _is_existing,
        })

    # ── 🏷 태그 추천 ── 자주 쓰는 태그 중 본문에 등장 + 자체 태그 제외
    _tag_freq = {}
    for _n in _notes:
        for _t in (_n.get("tags", []) or []):
            _tc = str(_t).replace("#", "").strip()
            if _tc:
                _tag_freq[_tc] = _tag_freq.get(_tc, 0) + 1
    # 본문에 실제 등장한 태그 먼저, 그다음 빈출 태그(보조). 최대 5개.
    _tag_reco = []
    _cand = sorted(_tag_freq.items(), key=lambda x: (0 if x[0] in _body else 1, -x[1]))
    for _t, _f in _cand:
        if _t in _own_tags or _t in _ex_tag:
            continue
        if _t in _body or _f >= 3:
            _tag_reco.append({"name": _t,
                              "reason": ("본문에 등장" if _t in _body else f"자주 쓰는 태그({_f}회)")})
        if len(_tag_reco) >= 5:
            break

    # ── 📁 프로젝트 추천 ── 이 메모의 개념·태그가 가장 많이 겹치는 프로젝트
    _my_cset = {_c["name"] for _c in _con_reco} | _already
    _my_tset = set(_own_tags) | {t["name"] for t in _tag_reco}
    _proj_score = []
    for _p in _projects:
        _pn = _p.get("name")
        if _pn == _own_proj:
            continue
        _pcs = set()
        _pts = set()
        _pnote_ids = {n.get("id") for n in _notes if _clean_text_value(n.get("project")).strip() == _pn}
        for _l in _links:
            if _l.get("note_id") in _pnote_ids or _clean_text_value(_l.get("project")).strip() == _pn:
                _cc = canonical_concept(_l.get("concept"))
                if _cc:
                    _pcs.add(_cc)
        for _n in _notes:
            if _clean_text_value(_n.get("project")).strip() == _pn:
                for _t in (_n.get("tags", []) or []):
                    _tc = str(_t).replace("#", "").strip()
                    if _tc:
                        _pts.add(_tc)
        _shared_c = _my_cset & _pcs
        _shared_t = _my_tset & _pts
        _score = len(_shared_c) * 2 + len(_shared_t)
        if _score > 0:
            _proj_score.append((_pn, _score, sorted(_shared_c), sorted(_shared_t)))
    _proj_score.sort(key=lambda x: -x[1])
    _project = None
    if _proj_score:
        _best = _proj_score[0]
        _rz = []
        if _best[2]:
            _rz.append("개념 " + ", ".join(_best[2][:3]))
        if _best[3]:
            _rz.append("태그 " + ", ".join("#" + t for t in _best[3][:3]))
        _project = {"best": _best[0], "reason": " · ".join(_rz) + " 겹침",
                    "alts": [p[0] for p in _proj_score[1:4]]}

    # ── 🔗 관련 메모 추천 ── 개념/태그를 공유하는 다른 메모
    _related = []
    for _n in _notes:
        if _n.get("id") == _own_id:
            continue
        if (_clean_text_value(_n.get("title")).strip() or "제목 없음") in _ex_rel:
            continue  # 이미 적용/무시한 관련 메모는 제외
        _ncs = {canonical_concept(c) for c in (_n.get("concepts", []) or [])}
        _nts = {str(t).replace("#", "").strip() for t in (_n.get("tags", []) or [])}
        _sc = _my_cset & _ncs
        _stg = _my_tset & _nts
        if _sc or _stg:
            _rz = []
            if _sc:
                _rz.append("개념 " + ", ".join(list(_sc)[:2]))
            if _stg:
                _rz.append("태그 " + ", ".join("#" + t for t in list(_stg)[:2]))
            _related.append({"id": _n.get("id"),
                             "title": _clean_text_value(_n.get("title")).strip() or "제목 없음",
                             "reason": " · ".join(_rz) + " 공유",
                             "score": len(_sc) * 2 + len(_stg)})
    _related.sort(key=lambda x: -x["score"])
    _related = _related[:5]

    # ── ✅ 작업 추천 ── 명확한 행동만 (비행동 표현 제외 + 액션 어미 요구)
    _tasks = []
    for _line in re.split(r"[\n.·•\-]", _body):
        _ls = _line.strip()
        if not (4 <= len(_ls) <= 40):
            continue
        if any(_x in _ls for _x in _NON_ACTION_HINTS):  # 고민/필요/중요 등은 작업 아님
            continue
        if _ls in _ex_task:  # 이미 적용/무시한 작업은 제외
            continue
        if any(_k in _ls for _k in _TASK_KEYWORDS) and any(_s in _ls for _s in _ACTION_SUFFIX):
            _tasks.append({"title": _ls, "reason": "본문에서 할 일(행동) 발견"})
        if len(_tasks) >= 4:
            break

    return {"concepts": _con_reco, "tags": _tag_reco, "project": _project,
            "related_notes": _related, "tasks": _tasks}


def _reco_record(note, kind, values, status):
    """추천 상태(applied/ignored)를 메모에 기록 — 성장형 엔진이 재추천 안 하게."""
    _key = "reco_applied" if status == "applied" else "reco_ignored"
    _store = note.setdefault(_key, {})
    _cur = _store.setdefault(kind, [])
    for _v in values:
        if _v not in _cur:
            _cur.append(_v)


def _reco_apply(note, kind, values):
    """선택된 추천을 실제 데이터에 반영 (비파괴·승인 기반)."""
    _now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if kind == "concepts":
        _links = st.session_state.setdefault("note_concept_links", [])
        _exist = {(l.get("note_id"), canonical_concept(l.get("concept"))) for l in _links}
        _nc = note.setdefault("concepts", [])
        for _c in values:
            if (note.get("id"), canonical_concept(_c)) not in _exist:
                _links.append({"note_id": note.get("id"), "concept": _c, "linked_at": _now})
            if _c not in _nc:
                _nc.append(_c)
    elif kind == "tags":
        _t = note.setdefault("tags", [])
        for _v in values:
            if _v not in _t:
                _t.append(_v)
    elif kind == "project":
        if values:
            note["project"] = values[0]
    elif kind == "related_notes":
        _rels = st.session_state.setdefault("relations", [])
        _src = _clean_text_value(note.get("title")).strip() or "제목 없음"
        for _v in values:
            if not any(r.get("source_name") == _src and r.get("target_name") == _v for r in _rels):
                _rels.append({"id": _new_id("rel")[4:], "source_name": _src, "target_name": _v,
                              "source_type": "note", "target_type": "note",
                              "relation_type": "관련", "created_at": _now})
    elif kind == "tasks":
        for _v in values:
            create_task(_v, project=_clean_text_value(note.get("project")).strip())
    _reco_record(note, kind, values, "applied")


def render_reco_center(note, key_prefix="reco"):
    """🔗 AI 연결 추천 센터 — 후보 표시 + 선택 적용/무시 + 실제 반영.
    성장형 엔진: 적용·무시한 건 build_reco가 자동 제외."""
    _r = build_reco(note)
    if not _r:
        return
    _total = (len(_r.get("concepts", [])) + len(_r.get("tags", []))
              + (1 if _r.get("project") else 0) + len(_r.get("related_notes", []))
              + len(_r.get("tasks", [])))
    _grown = bool(note.get("reco_applied") or note.get("reco_ignored"))
    st.markdown("#### 🔄 변경된 연결 제안" if _grown else "#### 🔗 JIUM이 찾은 연결")
    if _total == 0:
        st.caption("새로 발견된 연결이 없어요. 내용을 더 적으면 JIUM이 새 연결을 찾아줘요.")
        return
    st.caption("체크한 항목을 적용하거나 무시할 수 있어요. (무시한 건 다시 추천 안 해요)")
    _nid = note.get("id")
    _sel = {"concepts": [], "tags": [], "project": [], "related_notes": [], "tasks": []}

    def _esc(_v):
        import html as _h
        return _h.escape(str(_v))

    if _r["concepts"]:
        st.markdown("**🧠 개념**")
        _cc = st.columns(2)
        for _i, _c in enumerate(_r["concepts"]):
            with _cc[_i % 2]:
                if st.checkbox(f"{_c['name']}  ·  {_c['reason']}",
                               value=True, key=f"{key_prefix}_con_{_nid}_{_i}"):
                    _sel["concepts"].append(_c["name"])
    if _r["tags"]:
        st.markdown("**🏷 태그**")
        _tc = st.columns(2)
        for _i, _t in enumerate(_r["tags"]):
            with _tc[_i % 2]:
                if st.checkbox(f"#{_t['name']}  ·  {_t['reason']}",
                               value=True, key=f"{key_prefix}_tag_{_nid}_{_i}"):
                    _sel["tags"].append(_t["name"])
    if _r["project"]:
        _p = _r["project"]
        if st.checkbox(f"📁 「{_p['best']}」 프로젝트에 넣기  ·  {_p['reason']}",
                       value=True, key=f"{key_prefix}_proj_{_nid}"):
            _sel["project"].append(_p["best"])
    if _r["related_notes"]:
        st.markdown("**🔗 관련 메모**")
        for _i, _n in enumerate(_r["related_notes"]):
            if st.checkbox(f"📝 {_n['title']}  ·  {_n['reason']}",
                           value=True, key=f"{key_prefix}_rel_{_nid}_{_i}"):
                _sel["related_notes"].append(_n["title"])
    if _r["tasks"]:
        st.markdown("**✅ 작업**")
        for _i, _t in enumerate(_r["tasks"]):
            if st.checkbox(f"☑ {_t['title']}  ·  {_t['reason']}",
                           value=True, key=f"{key_prefix}_task_{_nid}_{_i}"):
                _sel["tasks"].append(_t["title"])

    _all = {"concepts": [c["name"] for c in _r["concepts"]],
            "tags": [t["name"] for t in _r["tags"]],
            "project": [_r["project"]["best"]] if _r["project"] else [],
            "related_notes": [n["title"] for n in _r["related_notes"]],
            "tasks": [t["title"] for t in _r["tasks"]]}

    def _do_apply(_picked):
        _n = 0
        for _k, _vals in _picked.items():
            if _vals:
                _reco_apply(note, _k, _vals)
                _n += len(_vals)
        save_persisted_data()
        _flash(f"✅ {_n}개 연결을 내 세계에 반영했어요!")
        st.rerun()

    _b1, _b2, _b3 = st.columns(3)
    with _b1:
        if st.button("🚀 모두 적용", key=f"{key_prefix}_apply_all_{_nid}",
                     type="primary", use_container_width=True):
            _do_apply(_all)
    with _b2:
        if st.button("✅ 선택 적용", key=f"{key_prefix}_apply_sel_{_nid}",
                     use_container_width=True):
            _do_apply(_sel)
    with _b3:
        if st.button("🙈 선택 무시", key=f"{key_prefix}_ignore_{_nid}",
                     use_container_width=True):
            for _k, _vals in _sel.items():
                if _vals:
                    _reco_record(note, _k, _vals, "ignored")
            save_persisted_data()
            _flash("무시한 항목은 다시 추천하지 않아요.")
            st.rerun()


# 하위호환: 기존 호출명 유지
def render_reco_preview(note):
    render_reco_center(note)


def render_readable_markdown(text, *, empty="메모 내용이 없어요.", max_chars=None):
    """메모/분석 결과를 읽기 모드로 렌더링한다. 저장 원문은 바꾸지 않는다."""
    body = _clean_text_value(text)
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + "\n\n…(이하 생략)"
    if not body.strip():
        st.caption(empty)
        return
    st.markdown(body)


def infer_thinking_chapters(item):
    """하나의 긴 메모를 페이지 안의 장/섹션처럼 나눠 보여주기 위한 간단한 구조화 함수."""
    text = str(item.get("full_text") or item.get("memo") or "")
    concepts = extract_local_concepts(text, item.get("tags", []), limit=12)
    chapters = []

    chapter_rules = [
        ("회사/배경", ["회사", "기업", "서비스", "운영", "설립", "비전", "미션"]),
        ("시장/경쟁", ["시장", "경쟁", "경쟁사", "CREST", "시장규모", "산업"]),
        ("규제/리스크", ["규제", "법", "개인정보", "전자금융", "식품위생", "리스크"]),
        ("기술/데이터", ["기술", "데이터", "O2O", "블록체인", "위치기반", "결제"]),
        ("마케팅/전략", ["마케팅", "STP", "4P", "SWOT", "포지셔닝", "목표"]),
        ("제안/활용", ["제안", "개선", "활용", "컨설팅", "아이디어", "보완"]),
    ]

    for chapter_name, words in chapter_rules:
        matched = [word for word in words if word in text]
        if matched:
            chapters.append({"name": chapter_name, "matched": matched[:4]})

    if not chapters:
        chapters.append({"name": "핵심 메모", "matched": concepts[:4]})

    return chapters, concepts


def render_thinking_page_preview(item):
    """선택한 지식 메모를 개인 지식 페이지처럼 요약해서 보여준다."""
    chapters, concepts = infer_thinking_chapters(item)
    st.markdown("### 📄 지식 페이지 미리보기")
    st.caption("원노트의 페이지처럼 한 메모 안의 장과 연결 개념을 한눈에 보여줘요.")

    st.markdown(
        f"""
        <div class="toc-box">
            <div class="toc-title">{item.get("title", "제목 없음")}</div>
            <div class="toc-meta">프로젝트: {item.get("project", "기본 프로젝트")} · 섹션: {item.get("section", "일반")} · {item.get("kind", "지식")}</div>
            <div class="toc-meta">출처: {display_source_label(item.get("url", ""))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("#### 🧩 자동 목차")
        for chapter in chapters:
            matched = ", ".join(chapter.get("matched", [])) or "핵심 문장 기반"
            st.markdown(f"- **{chapter.get('name')}** · {matched}")
    with c2:
        st.markdown("#### 🧠 핵심 개념")
        if concepts:
            concept_html = "".join([f'<span class="reason-chip">{concept}</span>' for concept in concepts[:14]])
            st.markdown(concept_html, unsafe_allow_html=True)
        else:
            st.caption("추출된 개념이 아직 없어요.")

    with st.expander("원문/메모 일부 보기", expanded=False):
        render_readable_markdown(item.get("memo") or item.get("full_text"), max_chars=3500)


def restore_item_from_knowledge(item):
    if item.get("kind") == "분석 결과" and item.get("raw_item", {}).get("result"):
        st.session_state.last_result = item.get("raw_item", {}).get("result")
        st.session_state.last_final_url = item.get("url", "")
        st.session_state.last_text = ""
        st.session_state.show_result = True
        st.session_state.result_closed = False
        st.session_state["knowledge_item_restored"] = True
        st.query_params["page"] = "home"
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
                # URL이 길면 카드 밖으로 넘치므로 잘라서 표시
                _src_short = source if len(str(source)) <= 26 else str(source)[:24] + "…"
                cache_key = item.get("cache_key") or f'{item.get("url", "")}::{item.get("content_type", "unknown")}'

                st.markdown(f'<div class="recent-card-title">{title}</div>', unsafe_allow_html=True)
                st.markdown(
                    f'<div class="recent-card-meta" style="overflow-wrap:anywhere;word-break:break-all;">'
                    f'{item.get("time","")}<br>{mode}<br>{score}점 · {_src_short}</div>',
                    unsafe_allow_html=True,
                )
                st.button(
                    "다시 보기",
                    key=f"recent_card_restore_{i}_{cache_key}",
                    use_container_width=True,
                    on_click=restore_analysis_from_history,
                    args=(cache_key,),
                )



# -----------------------------
# PATCH: Concept Finder Helpers
# -----------------------------
def build_concept_index(items):
    concept_docs = {}
    _hidden = set(st.session_state.get("hidden_concepts", []))

    for item in items:
        text = " ".join([
            str(item.get("title", "")),
            str(item.get("memo", "")),
            str(item.get("full_text", "")),
            " ".join([str(t) for t in item.get("tags", [])]),
        ])

        concepts = extract_local_concepts(
            text,
            item.get("tags", []),
            limit=24,
        )

        for concept in concepts:
            clean = str(concept).replace("#", "").strip()
            if not clean or clean in _hidden:
                continue
            concept_docs.setdefault(clean, []).append(item)

    for custom in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(custom, dict):
            concept = str(custom.get("name", "")).strip()
        else:
            concept = str(custom).strip()

        if concept and concept not in _hidden:
            concept_docs.setdefault(concept, [])

    return concept_docs


def get_concept_folder(concept):
    folders = st.session_state.get("pkm_concept_folders", {})
    return folders.get(concept, "자동/미분류")


def save_custom_concept_to_finder(name_key, folder_key):
    name = st.session_state.get(name_key, "").strip().replace("#", "")
    folder = st.session_state.get(folder_key, "").strip() or "내 개념/미분류"

    if not name:
        st.session_state["pkm_concept_error"] = "개념 이름을 입력해주세요."
        return

    concepts = [
        c if isinstance(c, dict) else {"name": str(c), "folder": "내 개념", "created_at": ""}
        for c in st.session_state.get("pkm_custom_concepts", []) if c
    ]
    if not any(c.get("name") == name for c in concepts):
        concepts.append({
            "name": name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    folders = st.session_state.get("pkm_concept_folders", {})
    folders[name] = folder

    st.session_state.pkm_custom_concepts = concepts
    st.session_state.pkm_concept_folders = folders
    st.session_state["pkm_concept_saved"] = True
    save_persisted_data()


def render_concept_finder(items):
    key_prefix = "concept_finder"

    st.markdown("### 🗂️ 핵심개념 파인더")
    st.caption("폴더 칩으로 필터링하고 카드를 클릭해 관련 문서를 확인하세요.")

    concept_docs = build_concept_index(items)

    # ── 개념 추가 폼 ──
    add_col1, add_col2, add_col3 = st.columns([1, 1, 0.7])
    with add_col1:
        st.text_input("개념 추가", placeholder="예: ESG, CREST, 전자금융거래법",
            key=f"{key_prefix}_pkm_new_concept_name")
    with add_col2:
        st.text_input("폴더 경로", placeholder="예: 마케팅/프레임워크",
            key=f"{key_prefix}_pkm_new_concept_folder")
    with add_col3:
        st.write(""); st.write("")
        st.button("개념 저장", use_container_width=True,
            on_click=save_custom_concept_to_finder,
            args=(f"{key_prefix}_pkm_new_concept_name", f"{key_prefix}_pkm_new_concept_folder"))

    if st.session_state.get("pkm_concept_saved"):
        st.success("핵심개념을 저장했어요."); st.session_state["pkm_concept_saved"] = False
    if st.session_state.get("pkm_concept_error"):
        st.warning(st.session_state["pkm_concept_error"]); st.session_state["pkm_concept_error"] = ""

    st.divider()

    if not concept_docs:
        st.info("아직 표시할 핵심개념이 없어요.")
        return

    # ── 검색 + 최소 문서 수 ──
    _sf1, _sf2 = st.columns([3, 1])
    with _sf1:
        q = st.text_input("🔍 개념 검색", placeholder="개념명, 폴더명으로 검색",
            key=f"{key_prefix}_pkm_concept_search", label_visibility="collapsed")
    with _sf2:
        min_docs = st.number_input("최소 연결 수", 0, 20, 0, 1, key=f"{key_prefix}_pkm_concept_min_docs")

    # ── 전체 목록 구성 ──
    _all_items = []  # (concept, docs, top_folder, sub_folder)
    _all_tops = set()
    for concept, docs in concept_docs.items():
        if len(docs) < min_docs:
            continue
        folder_path = get_concept_folder(concept)
        if q.strip() and q.strip().lower() not in f"{concept} {folder_path}".lower():
            continue
        parts = [p.strip() for p in folder_path.split("/") if p.strip()]
        top = parts[0] if parts else "자동"
        sub = parts[1] if len(parts) >= 2 else "미분류"
        _all_items.append((concept, docs, top, sub))
        _all_tops.add(top)

    if not _all_items:
        st.info("조건에 맞는 개념이 없어요.")
        return

    # ── 폴더 칩 필터 ──
    _folder_options = ["전체"] + sorted(_all_tops)
    _sel_folder = st.session_state.get(f"{key_prefix}_chip_folder", "전체")
    if _sel_folder not in _folder_options:
        _sel_folder = "전체"

    st.markdown("**📁 폴더 필터**")
    _chip_cols = st.columns(min(len(_folder_options), 8))
    for _ci, _fo in enumerate(_folder_options):
        _cnt_fo = len(_all_items) if _fo == "전체" else sum(1 for x in _all_items if x[2] == _fo)
        with _chip_cols[_ci % len(_chip_cols)]:
            _is_active = (_sel_folder == _fo)
            if st.button(f"{'✓ ' if _is_active else ''}{_fo} ({_cnt_fo})",
                         key=f"{key_prefix}_chip_{_fo[:12]}",
                         type="primary" if _is_active else "secondary",
                         use_container_width=True):
                st.session_state[f"{key_prefix}_chip_folder"] = _fo
                st.rerun()

    st.divider()

    # ── 필터 적용 ──
    _display_items = _all_items if _sel_folder == "전체" else [x for x in _all_items if x[2] == _sel_folder]
    _display_items.sort(key=lambda x: len(x[1]), reverse=True)

    # ── 열 수 선택 ──
    _grid_n = st.select_slider("열 수", options=[3, 4, 5, 6], value=4, key=f"{key_prefix}_grid_cols")
    st.caption(f"총 {len(_display_items)}개 개념")

    # ── 카드 그리드 ──
    _grid_cols = st.columns(_grid_n)
    for _gi, (concept, docs, top, sub) in enumerate(_display_items):
        _mc = sum(1 for l in st.session_state.get("note_concept_links",[]) if l.get("concept")==concept)
        _is_sel = st.session_state.get("pkm_selected_concept") == concept
        _edit_key = f"finder_edit_{_gi}_{concept[:8]}"
        _is_editing = st.session_state.get(_edit_key, False)

        with _grid_cols[_gi % _grid_n]:
            with st.container(border=True):
                if _is_editing:
                    # ── 편집 모드 ──
                    _new_name = st.text_input("개념명", value=concept,
                        key=f"finder_rename_inp_{_gi}_{concept[:10]}", label_visibility="collapsed")
                    _all_fnames_f = ["자동"] + sorted(set(get_concept_folder(c) for c in concept_docs.keys()) - {"자동"})
                    _cur_fold = get_concept_folder(concept)
                    _cur_fi2 = _all_fnames_f.index(_cur_fold) if _cur_fold in _all_fnames_f else 0
                    _new_fold = st.selectbox("폴더", _all_fnames_f + ["➕ 새 폴더"],
                        index=_cur_fi2, key=f"finder_fold_{_gi}_{concept[:10]}", label_visibility="collapsed")
                    if _new_fold == "➕ 새 폴더":
                        _new_fold = st.text_input("새 폴더명", key=f"finder_newfold_{_gi}_{concept[:6]}", label_visibility="collapsed")
                    _ea, _eb, _ec = st.columns(3)
                    with _ea:
                        if st.button("💾", key=f"finder_save_{_gi}_{concept[:10]}", use_container_width=True, type="primary"):
                            _save_name = _new_name.strip() if _new_name.strip() else concept
                            _save_fold = (_new_fold if _new_fold and _new_fold != "➕ 새 폴더" else "자동")
                            _ucc = []; _found_in_custom = False
                            for _c2 in st.session_state.get("pkm_custom_concepts", []):
                                _n2 = _c2.get("name") if isinstance(_c2, dict) else str(_c2)
                                if _n2 == concept:
                                    _c2 = dict(_c2) if isinstance(_c2, dict) else {"name": _n2}
                                    _c2["name"] = _save_name; _c2["folder"] = _save_fold
                                    _found_in_custom = True
                                _ucc.append(_c2)
                            if not _found_in_custom:
                                _ucc.append({"name": _save_name, "folder": _save_fold, "created_at": ""})
                                st.session_state["hidden_concepts"] = list(set(st.session_state.get("hidden_concepts", [])) | {concept})
                            st.session_state.pkm_custom_concepts = _ucc
                            for _lk in st.session_state.get("note_concept_links", []):
                                if _lk.get("concept") == concept: _lk["concept"] = _save_name
                            _fds = st.session_state.get("pkm_concept_folders", {})
                            _fds[_save_name] = _save_fold
                            if concept != _save_name and concept in _fds: del _fds[concept]
                            st.session_state.pkm_concept_folders = _fds
                            save_persisted_data()
                            st.session_state[_edit_key] = False; st.rerun()
                    with _eb:
                        if st.button("✕", key=f"finder_cancel_{_gi}_{concept[:10]}", use_container_width=True):
                            st.session_state[_edit_key] = False; st.rerun()
                    with _ec:
                        if st.button("🗑️", key=f"finder_del_{_gi}_{concept[:10]}", use_container_width=True):
                            st.session_state.pkm_custom_concepts = [
                                c for c in st.session_state.get("pkm_custom_concepts", [])
                                if (c.get("name") if isinstance(c,dict) else str(c)) != concept]
                            st.session_state["hidden_concepts"] = list(set(st.session_state.get("hidden_concepts", [])) | {concept})
                            st.session_state["note_concept_links"] = [
                                l for l in st.session_state.get("note_concept_links", []) if l.get("concept") != concept]
                            _fds2 = st.session_state.get("pkm_concept_folders", {})
                            _fds2.pop(concept, None)
                            st.session_state.pkm_concept_folders = _fds2
                            save_persisted_data()
                            st.session_state[_edit_key] = False; st.rerun()
                else:
                    # ── 카드 보기 모드 ──
                    _badge_color = "#3b82f6" if _is_sel else "#6b7280"
                    st.markdown(
                        f'<div style="font-weight:600;font-size:0.95em;margin-bottom:2px">'
                        f'{"🔵" if _is_sel else "🧠"} {concept}</div>'
                        f'<div style="font-size:0.78em;color:#888">{top}/{sub}</div>'
                        f'<div style="font-size:0.78em;color:#aaa">연결 {len(docs)}개 · 메모 {_mc}개</div>',
                        unsafe_allow_html=True
                    )
                    _ba, _bb = st.columns(2)
                    with _ba:
                        if st.button("닫기" if _is_sel else "보기",
                                     key=f"{key_prefix}_card_view_{_gi}_{concept[:10]}", use_container_width=True,
                                     type="primary" if _is_sel else "secondary"):
                            st.session_state["pkm_selected_concept"] = None if _is_sel else concept; st.rerun()
                    with _bb:
                        if st.button("✏️", key=f"finder_edit_btn_{_gi}_{concept[:10]}", use_container_width=True):
                            st.session_state[_edit_key] = True; st.rerun()

    # ── 선택된 개념 관련 문서 ──
    selected = st.session_state.get("pkm_selected_concept")
    if selected:
        st.divider()
        st.markdown(f"### 🔗 `{selected}` 관련 문서")
        docs = concept_docs.get(selected, [])
        if not docs:
            st.info("아직 이 개념과 연결된 문서가 없어요.")
        else:
            cols = st.columns(4)
            for idx, item in enumerate(docs[:16]):
                with cols[idx % 4]:
                    with st.container(border=True):
                        st.markdown(f"**{item.get('title', '제목 없음')}**")
                        st.caption(f"{item.get('project', '기본 프로젝트')} · {item.get('section', '일반')} · {item.get('kind', '')}")
                        st.button("열기", key=f"{key_prefix}_concept_doc_open_{selected}_{idx}_{item.get('raw_index', idx)}",
                            use_container_width=True, on_click=restore_item_from_knowledge, args=(item,))



def render_knowledge_map_page():
    key_prefix = "knowledge_map"
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

    from collections import Counter

    with st.expander("🛠️ 핵심 개념 직접 추가", expanded=False):
        st.caption("자동으로 안 잡히는 개념은 직접 추가할 수 있어요.")
        _existing_folders = sorted(set(
            (c.get("folder") or st.session_state.get("pkm_concept_folders", {}).get(c.get("name",""), ""))
            for c in st.session_state.get("pkm_custom_concepts", []) if isinstance(c, dict)
        ) - {""})

        ca1, ca2 = st.columns(2)
        with ca1:
            new_concept = st.text_input("개념명 *", placeholder="예: ESG, CREST, 결제시스템", key="pkm_new_concept")
            new_folder_sel = st.selectbox("상위 폴더", ["직접 입력"] + _existing_folders,
                key="pkm_new_folder_sel")
            if new_folder_sel == "직접 입력":
                new_folder = st.text_input("상위 폴더명 입력", placeholder="예: 마케팅, 취업, 기술",
                    key="pkm_new_folder_input")
            else:
                new_folder = new_folder_sel
        with ca2:
            new_subfolder = st.text_input("하위 폴더 (선택)", placeholder="예: 프레임워크, 자격증, 스킬",
                key="pkm_new_subfolder")
            new_desc = st.text_input("설명 (선택)", placeholder="예: 시장환경 분석 프레임워크",
                key="pkm_new_desc")
            new_aliases = st.text_input("동의어 (선택, 쉼표 구분)", placeholder="예: CSR, 지속가능경영",
                key="pkm_new_aliases")

        if st.button("➕ 개념 저장", key="add_pkm_custom_concept", type="primary", use_container_width=True):
            clean = new_concept.strip().replace("#", "")
            folder_val = (new_folder or "내 개념").strip()
            if clean:
                concepts = [
                    cc if isinstance(cc, dict) else {"name": str(cc), "folder": "내 개념", "created_at": ""}
                    for cc in st.session_state.get("pkm_custom_concepts", []) if cc
                ]
                if not any(cc.get("name") == clean for cc in concepts):
                    concepts.append({
                        "name": clean,
                        "folder": folder_val,
                        "subfolder": new_subfolder.strip(),
                        "description": new_desc.strip(),
                        "aliases": [a.strip() for a in new_aliases.split(",") if a.strip()],
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                    st.session_state.pkm_custom_concepts = concepts
                    folders = st.session_state.get("pkm_concept_folders", {})
                    folders[clean] = folder_val
                    st.session_state.pkm_concept_folders = folders
                    save_persisted_data()
                    _flash(f"'{clean}' 개념을 '{folder_val}' 폴더에 추가했어요!")
                    st.rerun()
                else:
                    st.warning(f"'{clean}' 개념이 이미 있어요.")
            else:
                st.warning("개념명을 입력해주세요.")

    st.markdown("### 🧠 핵심 개념 탐색")
    st.caption("자주 등장하는 개념과 중요 개념을 보고, 아래에서 개념을 탐색해요. 관리 도구(별칭·병합·품질)는 ⚙️ 버튼에 모아뒀어요.")

    # ── 자주 등장하는 개념 Top N (빈도 기반 중요도) ──
    _freq_rank = concept_frequency(top_n=10)
    if _freq_rank:
        with st.expander(f"🔝 자주 등장하는 개념 Top {len(_freq_rank)}", expanded=False):
            _max_freq = _freq_rank[0][1] or 1
            for _fi, (_fname, _fcnt) in enumerate(_freq_rank, 1):
                _bar_w = int(_fcnt / _max_freq * 100)
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:3px'>"
                    f"<span style='min-width:24px;color:#64748b;font-size:0.85em'>{_fi}.</span>"
                    f"<span style='min-width:130px;font-weight:600'>{_fname}</span>"
                    f"<div style='flex:1;background:#e2e8f0;border-radius:4px;height:8px'>"
                    f"<div style='width:{_bar_w}%;background:#3b82f6;height:8px;border-radius:4px'></div></div>"
                    f"<span style='min-width:42px;text-align:right;color:#3b82f6;font-weight:700'>{_fcnt}회</span>"
                    f"</div>",
                    unsafe_allow_html=True)
            st.caption("메모·작업·분석에 연결된 횟수예요. 프로젝트 맵에서 노드 크기로 활용할 예정이에요.")

    # ── ⭐ TF-IDF 중요 개념 Top N (전체에서 흔하지 않은 특화 개념) ──
    _tfidf_rank = concept_tfidf(top_n=10) if get_setting("feat_tfidf") else []
    if _tfidf_rank:
        with st.expander(f"⭐ TF-IDF 중요 개념 Top {len(_tfidf_rank)}", expanded=False):
            _max_tfidf = _tfidf_rank[0][1]["tfidf"] or 1
            for _ti, (_tname, _tinfo) in enumerate(_tfidf_rank, 1):
                _t_tf, _t_df, _t_idf, _t_score = _tinfo["tf"], _tinfo["df"], _tinfo["idf"], _tinfo["tfidf"]
                _tw = int(_t_score / _max_tfidf * 100)
                _ttip = f"빈도 {_t_tf} · 등장 문서 {_t_df} · idf {_t_idf}"
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:3px'>"
                    f"<span style='min-width:24px;color:#64748b;font-size:0.85em'>{_ti}.</span>"
                    f"<span style='min-width:130px;font-weight:600' title='{_ttip}'>{_tname}</span>"
                    f"<div style='flex:1;background:#fef3c7;border-radius:4px;height:8px'>"
                    f"<div style='width:{_tw}%;background:#f59e0b;height:8px;border-radius:4px'></div></div>"
                    f"<span style='min-width:48px;text-align:right;color:#d97706;font-weight:700'>{_t_score}</span>"
                    f"</div>",
                    unsafe_allow_html=True)
            st.caption("전체 메모에서 흔한 개념은 낮게, 특정 메모·프로젝트에 특화된 개념은 높게 평가해요. (TF-IDF, API 비용 없음)")

    # ── ⚙️ 개념 관리 도구 (탐색과 분리 — 평소엔 접힘) ──
    st.divider()
    _show_concept_mgmt = st.toggle(
        "⚙️ 개념 관리 도구 (별칭 · 병합 · 품질 리포트)", value=False, key="hub_show_mgmt",
        help="자주 쓰지 않는 관리 기능이에요. 필요할 때만 펼쳐서 사용하세요.")

    # ── 개념 품질 리포트 (제외된 개념 로그) ──
    _ex_top, _ex_reasons, _ex_total = excluded_concepts_report(top_n=15)
    if _show_concept_mgmt and _ex_total:
        with st.expander(f"🚫 개념 품질 리포트 · 제외 {_ex_total}건", expanded=False):
            st.caption("저장 단계에서 걸러진 개념이에요. 자주 걸러지는 단어는 불용어 사전 개선에 참고하세요.")
            qr1, qr2 = st.columns(2)
            with qr1:
                st.markdown("**🔤 자주 제외된 개념 TOP**")
                if _ex_top:
                    _emax = _ex_top[0][1] or 1
                    for _en2, _ec in _ex_top:
                        _ew = int(_ec / _emax * 100)
                        st.markdown(
                            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:3px'>"
                            f"<span style='min-width:120px'>{_en2}</span>"
                            f"<div style='flex:1;background:#fee2e2;border-radius:4px;height:7px'>"
                            f"<div style='width:{_ew}%;background:#ef4444;height:7px;border-radius:4px'></div></div>"
                            f"<span style='min-width:38px;text-align:right;color:#ef4444;font-weight:700'>{_ec}회</span>"
                            f"</div>",
                            unsafe_allow_html=True)
                else:
                    st.caption("아직 없어요.")
            with qr2:
                st.markdown("**📊 제외 사유별**")
                for _rn, _rc in _ex_reasons:
                    st.markdown(f"- {_rn} · **{_rc}건**")
            if st.button("🧹 제외 로그 비우기", key="clear_excluded_log"):
                st.session_state["excluded_concepts_log"] = []
                save_persisted_data()
                st.rerun()

    if _show_concept_mgmt:
        # ── 🔗 별칭(alias) 관리 — 비파괴적 개념 연결 ──
        with st.expander("🔗 개념 별칭 관리", expanded=False):
            st.caption("같은 개념의 다른 표기를 대표 개념으로 묶어요. (예: BackPropagation·역전파 알고리즘 → 역전파) "
                       "원본 메모는 바뀌지 않고, 검색·랭킹·관련 메모 추천·프로젝트 맵에서만 대표 개념으로 합산돼요.")
            _alias_map = st.session_state.setdefault("concept_aliases", {})

            _ac1, _ac2 = st.columns([1, 1])
            with _ac1:
                _new_canon = st.text_input("대표 개념", key="alias_canon", placeholder="예: 역전파")
            with _ac2:
                _new_aliases = st.text_input("별칭 (쉼표로 여러 개)", key="alias_inputs",
                                             placeholder="예: BackPropagation, 역전파 알고리즘")
            if st.button("🔗 별칭 등록", key="alias_add_btn", type="primary"):
                if _new_canon.strip() and _new_aliases.strip():
                    _n = add_concept_aliases(
                        _new_canon, [a for a in _new_aliases.split(",") if a.strip()])
                    st.session_state.pop("_alias_rev_cache", None)
                    save_persisted_data()
                    _flash(f"별칭 {_n}개를 '{clean_concept(_new_canon) or _new_canon.strip()}'에 등록했어요." if _n
                           else "추가된 별칭이 없어요 (중복/자기 자신 제외).")
                    st.rerun()
                else:
                    st.warning("대표 개념과 별칭을 모두 입력해주세요.")

            if _alias_map:
                st.markdown("**등록된 별칭**")
                for _canon in sorted(_alias_map.keys()):
                    _aliases = _alias_map.get(_canon, [])
                    if not _aliases:
                        continue
                    st.markdown(f"**{_canon}** <span style='color:#94a3b8'>· alias {len(_aliases)}</span>",
                                unsafe_allow_html=True)
                    for _al in list(_aliases):
                        _dc1, _dc2 = st.columns([5, 1])
                        with _dc1:
                            st.markdown(f"&nbsp;&nbsp;↳ `{_al}`", unsafe_allow_html=True)
                        with _dc2:
                            if st.button("삭제", key=f"alias_del_{_canon}_{_al}"):
                                remove_concept_alias(_canon, _al)
                                st.session_state.pop("_alias_rev_cache", None)
                                save_persisted_data()
                                st.rerun()
            else:
                st.caption("아직 등록된 별칭이 없어요.")

        with st.expander("✏️ 개념 수정 / 병합", expanded=False):
            _cc_list = [c if isinstance(c,dict) else {"name":str(c),"folder":"내 개념"} for c in st.session_state.get("pkm_custom_concepts",[]) if c]
            _cc_names = [c.get("name","") for c in _cc_list]
            ec1, ec2 = st.columns(2)
            with ec1:
                st.markdown("**✏️ 이름 수정 / 삭제**")
                _et = st.selectbox("수정할 개념", _cc_names, key="edit_c_target") if _cc_names else None
                _en = st.text_input("새 이름", key="edit_c_new")
                if st.button("이름 변경", key="do_rename_c", use_container_width=True):
                    if _et and _en.strip():
                        for c in st.session_state.pkm_custom_concepts:
                            if (c.get("name") if isinstance(c,dict) else str(c)) == _et:
                                if isinstance(c,dict): c["name"] = _en.strip()
                        for lk in st.session_state.get("note_concept_links",[]):
                            if lk.get("concept") == _et: lk["concept"] = _en.strip()
                        save_persisted_data(); _flash(f"'{_et}' → '{_en.strip()}'"); st.rerun()
                if st.button("🗑️ 삭제", key="do_delete_c", use_container_width=True):
                    if _et:
                        st.session_state.pkm_custom_concepts = [c for c in _cc_list if c.get("name") != _et]
                        st.session_state["note_concept_links"] = [lk for lk in st.session_state.get("note_concept_links",[]) if lk.get("concept") != _et]
                        save_persisted_data(); _flash(f"'{_et}' 삭제됨"); st.rerun()
            with ec2:
                st.markdown("**🔗 병합 (A → B로)**")
                _mf = st.selectbox("없앨 개념 (A)", _cc_names, key="merge_from_c") if _cc_names else None
                _mt = st.selectbox("남길 개념 (B)", _cc_names, key="merge_to_c") if _cc_names else None
                if st.button("병합", key="do_merge_c", use_container_width=True):
                    if _mf and _mt and _mf != _mt:
                        st.session_state.pkm_custom_concepts = [c for c in _cc_list if c.get("name") != _mf]
                        for lk in st.session_state.get("note_concept_links",[]):
                            if lk.get("concept") == _mf: lk["concept"] = _mt
                        save_persisted_data(); _flash(f"'{_mf}' → '{_mt}' 병합 완료"); st.rerun()
        st.divider()

    concept_counter = Counter()
    concept_source_items = get_all_knowledge_items()

    _hidden_set = set(st.session_state.get("hidden_concepts", []))
    for item in concept_source_items:
        for tag in item.get("tags", []):
            clean = str(tag).replace("#", "").strip()
            if clean and clean not in _hidden_set:
                concept_counter[clean] += 1
        for concept in extract_local_concepts(
            str(item.get("full_text", "")) + " " + str(item.get("memo", "")),
            item.get("tags", []),
            limit=8,
        ):
            if concept and concept not in _hidden_set:
                concept_counter[concept] += 1

    # 커스텀 개념 폴더 매핑
    concept_folder_map = {}
    for c in st.session_state.get("pkm_custom_concepts", []):
        if isinstance(c, dict) and c.get("name"):
            folder_val = c.get("folder") or st.session_state.get("pkm_concept_folders", {}).get(c["name"], "내 개념")
            concept_folder_map[c["name"]] = folder_val
            concept_counter[c["name"]] = max(concept_counter.get(c["name"], 0), 1) + 3

    _hub_show_all = st.checkbox("전체 개념 보기 (AI 추출 포함)", value=False, key="hub_show_all")
    _hub_limit = len(concept_counter) if _hub_show_all else st.select_slider(
        "최대 표시 개수", options=[20, 40, 60, 80, 100, 150, 200], value=40, key="hub_limit"
    ) if not _hub_show_all else len(concept_counter)
    top_concepts = concept_counter.most_common(_hub_limit)

    if top_concepts:
        # 폴더별 그룹핑
        folders_grouped = {}
        for concept, count in top_concepts:
            folder = concept_folder_map.get(concept) or st.session_state.get("pkm_concept_folders", {}).get(concept, "자동")
            folders_grouped.setdefault(folder, []).append((concept, count))

        _all_fnames = sorted(set(concept_folder_map.values()) | {"자동"})
        _bulk_mode = st.toggle("📦 일괄 폴더 변경 모드", key="hub_bulk_mode", value=False)
        if _bulk_mode:
            if "hub_bulk_sel" not in st.session_state:
                st.session_state["hub_bulk_sel"] = []
            _sel_cnt = len(st.session_state.get("hub_bulk_sel", []))
            _bm_c1, _bm_c2 = st.columns([2, 1])
            with _bm_c1:
                _bulk_tgt = st.selectbox(
                    "이동할 폴더 선택",
                    _all_fnames + ["+ 새 폴더"],
                    key="bulk_tgt",
                    label_visibility="collapsed"
                )
                if _bulk_tgt == "+ 새 폴더":
                    _bulk_tgt = st.text_input("새 폴더명", key="bulk_new_fname", placeholder="새 폴더명 입력")
            with _bm_c2:
                st.markdown(f"<div style='margin-top:6px;font-size:0.85em;color:#666'>선택된 개념: <b>{_sel_cnt}개</b></div>", unsafe_allow_html=True)
                if st.button(f"📦 {_sel_cnt}개 이동", key="do_bulk_mv", type="primary", use_container_width=True, disabled=(_sel_cnt == 0)):
                    _sel = st.session_state.get("hub_bulk_sel", [])
                    if _sel and _bulk_tgt and _bulk_tgt != "+ 새 폴더":
                        _ucc = []
                        for c in st.session_state.get("pkm_custom_concepts", []):
                            _cn = c.get("name") if isinstance(c,dict) else str(c)
                            if _cn in _sel:
                                c = dict(c) if isinstance(c,dict) else {"name":_cn}
                                c["folder"] = _bulk_tgt
                            _ucc.append(c)
                        st.session_state.pkm_custom_concepts = _ucc
                        fds = st.session_state.get("pkm_concept_folders",{})
                        for _cn in _sel: fds[_cn] = _bulk_tgt
                        st.session_state.pkm_concept_folders = fds
                        st.session_state["hub_bulk_sel"] = []
                        save_persisted_data(); _flash(f"{len(_sel)}개 '{_bulk_tgt}'로 이동 완료!"); st.rerun()
            st.divider()
        # ── 뷰 모드 + 열 수 선택 ──
        _hub_vc1, _hub_vc2 = st.columns([2, 3])
        with _hub_vc1:
            hub_search = st.text_input("🔍 개념 검색", placeholder="개념명으로 검색", key="hub_concept_search", label_visibility="collapsed")
        with _hub_vc2:
            _view_cols_options = {"3열": 3, "4열": 4, "5열": 5, "6열": 6, "8열": 8, "10열": 10}
            _view_mode = st.radio("보기 방식", ["📋 목록", "🔲 그리드"], horizontal=True, key="hub_view_mode", label_visibility="collapsed")
            if _view_mode == "🔲 그리드":
                _grid_col_n = st.select_slider("열 수", options=[3,4,5,6,8,10], value=st.session_state.get("hub_grid_cols",5), key="hub_grid_col_slider", label_visibility="collapsed")
                st.session_state["hub_grid_cols"] = _grid_col_n

        if hub_search.strip():
            _all_concept_names = [concept for folder_concepts_tmp in folders_grouped.values() for concept, _ in folder_concepts_tmp]
            _candidates = [c for c in _all_concept_names if hub_search.strip().lower() in c.lower()]
            if _candidates:
                st.caption(f"검색 결과 {len(_candidates)}개 — 클릭하면 바로 이동")
                _cand_cols = st.columns(min(len(_candidates), 5))
                for _ci, _cname in enumerate(_candidates[:10]):
                    with _cand_cols[_ci % 5]:
                        if st.button(f"🧠 {_cname}", key=f"hub_cand_{_ci}_{_cname[:10]}", use_container_width=True):
                            st.session_state["selected_concept_v2"] = _cname
                            st.rerun()
            else:
                st.caption("일치하는 개념이 없어요.")
        selected_concept_v2 = st.session_state.get("selected_concept_v2")

        all_folder_names = sorted(set(folders_grouped.keys()) - {"자동"})
        folder_move_options = ["자동"] + all_folder_names

        for folder_name, folder_concepts in sorted(folders_grouped.items()):
            if hub_search.strip():
                folder_concepts = [(con, n) for con, n in folder_concepts if hub_search.strip().lower() in con.lower()]
            if not folder_concepts:
                continue

            is_custom = folder_name != "자동"
            folder_icon = "📂" if is_custom else "🗂️"
            with st.expander(f"{folder_icon} {folder_name}  ·  {len(folder_concepts)}개 개념", expanded=is_custom):
                _is_grid = (_view_mode == "🔲 그리드")
                _gcols = st.session_state.get("hub_grid_cols", 5) if _is_grid else None

                if _is_grid and not st.session_state.get("hub_bulk_mode"):
                    # ── 그리드 뷰 ──
                    _grid_rows = [folder_concepts[i:i+_gcols] for i in range(0, len(folder_concepts), _gcols)]
                    for _grow in _grid_rows:
                        _gcol_objs = st.columns(_gcols)
                        for _gi, (concept, count) in enumerate(_grow):
                            with _gcol_objs[_gi]:
                                is_selected = (selected_concept_v2 == concept)
                                _memo_link_cnt = sum(1 for l in st.session_state.get("note_concept_links", []) if l.get("concept") == concept)
                                _card_bg = "#dbeafe" if is_selected else "#f8fafc"
                                _card_border = "#3b82f6" if is_selected else "#e2e8f0"
                                _card_color = "#1d4ed8" if is_selected else "#374151"
                                st.markdown(
                                    f'<div style="background:{_card_bg};border:1.5px solid {_card_border};border-radius:10px;'
                                    f'padding:10px 10px 8px;margin-bottom:6px;cursor:pointer;text-align:center">'
                                    f'<div style="font-weight:{"700" if is_selected else "600"};color:{_card_color};font-size:0.9em">🧠 {concept}</div>'
                                    f'<div style="font-size:0.75em;color:#888;margin-top:2px">{count}개{"  "+str(_memo_link_cnt)+"메모" if _memo_link_cnt else ""}</div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                                if st.button("▶" if is_selected else "보기", key=f"hub_g_{folder_name[:6]}_{_gi}_{concept[:12]}", use_container_width=True):
                                    st.session_state["selected_concept_v2"] = None if is_selected else concept
                                    st.rerun()
                else:
                    for row_idx, (concept, count) in enumerate(folder_concepts):
                        is_selected = (selected_concept_v2 == concept)

                        # ── 일괄 선택 모드 ──
                        if st.session_state.get("hub_bulk_mode"):
                            _bc1, _bc2 = st.columns([5, 1])
                            with _bc1:
                                _chk_key = f"chk_{folder_name[:6]}_{row_idx}_{concept[:10]}"
                                _is_chk = concept in st.session_state.get("hub_bulk_sel", [])
                                _new_chk = st.checkbox(f"🧠 **{concept}**  ·  {count}개", value=_is_chk, key=_chk_key)
                                if _new_chk and concept not in st.session_state.setdefault("hub_bulk_sel", []):
                                    st.session_state["hub_bulk_sel"].append(concept)
                                elif not _new_chk and concept in st.session_state.get("hub_bulk_sel", []):
                                    st.session_state["hub_bulk_sel"].remove(concept)
                            with _bc2:
                                st.caption(folder_name)
                        else:
                            # ── 목록 모드: 이름 | 폴더 선택 | 보기 ──
                            _memo_link_cnt = sum(1 for l in st.session_state.get("note_concept_links", []) if l.get("concept") == concept)
                            _hc1, _hc2, _hc3 = st.columns([3, 2, 1])
                            with _hc1:
                                _name_color = "#2f73ff" if is_selected else "#172033"
                                _name_prefix = "▶ " if is_selected else "🧠 "
                                _memo_badge = f' <span style="font-size:0.78em;background:#e8f0fe;color:#1f3f91;padding:1px 6px;border-radius:10px">{_memo_link_cnt}메모</span>' if _memo_link_cnt else ""
                                st.markdown(
                                    f'<div style="padding:5px 0;font-weight:{"700" if is_selected else "500"};color:{_name_color}">'
                                    f'{_name_prefix}{concept}'
                                    f'<span style="font-size:0.8em;color:#999;margin-left:6px">{count}개</span>'
                                    f'{_memo_badge}</div>',
                                    unsafe_allow_html=True,
                                )
                            with _hc2:
                                _cur_fidx = (folder_move_options + ["➕ 새 폴더"]).index(folder_name) if folder_name in folder_move_options else 0
                                _sel_folder = st.selectbox(
                                    "폴더", folder_move_options + ["➕ 새 폴더"],
                                    index=_cur_fidx,
                                    key=f"hub_fsel_{row_idx}_{concept[:12]}",
                                    label_visibility="collapsed",
                                )
                                if _sel_folder == "➕ 새 폴더":
                                    _sel_folder = st.text_input("새폴더명", placeholder="폴더명 입력 후 Enter",
                                        key=f"hub_newfsel_{row_idx}_{concept[:12]}", label_visibility="collapsed")
                                if _sel_folder and _sel_folder != folder_name and _sel_folder != "➕ 새 폴더":
                                    _fds = st.session_state.get("pkm_concept_folders", {})
                                    _fds[concept] = _sel_folder
                                    _ucc2 = []
                                    _found2 = False
                                    for _cc2 in st.session_state.get("pkm_custom_concepts", []):
                                        _nm2 = _cc2.get("name") if isinstance(_cc2, dict) else str(_cc2)
                                        if _nm2 == concept:
                                            _cc2 = dict(_cc2) if isinstance(_cc2, dict) else {"name": _nm2}
                                            _cc2["folder"] = _sel_folder
                                            _found2 = True
                                        _ucc2.append(_cc2)
                                    if not _found2:
                                        _ucc2.append({"name": concept, "folder": _sel_folder, "created_at": ""})
                                    st.session_state.pkm_custom_concepts = _ucc2
                                    st.session_state.pkm_concept_folders = _fds
                                    save_persisted_data()
                                    _flash("폴더를 변경했어요")
                                    st.rerun()
                            with _hc3:
                                _btn_lbl = "닫기" if is_selected else "보기"
                                if st.button(_btn_lbl, key=f"hub_open_{folder_name[:8]}_{row_idx}_{concept[:15]}", use_container_width=True):
                                    st.session_state["selected_concept_v2"] = None if is_selected else concept
                                    st.rerun()
    else:
        st.info("아직 추출된 핵심 개념이 없어요. 문서를 분석하거나 직접 추가해보세요.")

    selected_concept_v2 = st.session_state.get("selected_concept_v2")
    if selected_concept_v2:
        st.divider()
        st.markdown(f"### 🔗 **{selected_concept_v2}** 관련 문서")
        related_items_v2 = []
        selected_norm = str(selected_concept_v2).replace("#", "").strip().lower()

        for item in concept_source_items:
            tags = [str(t).replace("#", "").strip().lower() for t in item.get("tags", [])]
            combined_text = " ".join([
                str(item.get("title", "")),
                str(item.get("memo", "")),
                str(item.get("full_text", "")),
                " ".join(tags),
            ]).lower()
            if selected_norm in tags or selected_norm in combined_text:
                related_items_v2.append(item)

        if related_items_v2:
            st.success(f"{len(related_items_v2)}개 문서가 연결되어 있어요.")
            rcols = st.columns(3)
            for related_idx, item in enumerate(related_items_v2[:12]):
                with rcols[related_idx % 3]:
                    with st.container(border=True):
                        st.markdown(f"**{item.get('title', '제목 없음')}**")
                        st.caption(f"{item.get('kind')} · {item.get('score', 0)}점")
                        st.caption(f"{item.get('project', '기본 프로젝트')} / {item.get('section', '일반')}")
                        st.button(
                            "열기",
                            key=f"concept_doc_open_{related_idx}_{abs(hash(str(item.get('title', '')) + str(item.get('date', ''))))}",
                            use_container_width=True,
                            on_click=restore_item_from_knowledge,
                            args=(item,),
                        )
        else:
            st.warning("연결된 문서를 못 찾았어요.")

        st.divider()

    # ── 지식맵 IA: 개념 관리 → 🔹 기본 탐색 도구 → 🔬 고급 분석 도구 (3구역) ──
    def _km_section(_label, _color="#3b82f6"):
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:10px;margin:14px 0 8px;'>"
            f"<div style='flex:1;height:3px;border-radius:3px;"
            f"background:linear-gradient(90deg,{_color}00,{_color});'></div>"
            f"<span style='font-weight:800;color:{_color};white-space:nowrap'>{_label}</span>"
            f"<div style='flex:1;height:3px;border-radius:3px;"
            f"background:linear-gradient(90deg,{_color},{_color}00);'></div></div>",
            unsafe_allow_html=True)

    # 🕸️ 지식맵 = 도구 목록이 아니라 '탐색 공간'. 진입하면 바로 그래프.
    st.caption("내 생각이 어떻게 연결되는지 보는 공간이에요. **🕸️ 지식그래프**가 기본, **🪐 프로젝트맵**은 보조예요.")
    _km_adv = st.toggle(
        "🔬 다른 보기 더 보기 (개념·관계·성장·노션보드·지식페이지·타임라인)",
        value=False, key="km_adv_view",
        help="개념·관계·성장 등은 같은 지식의 다른 표현이라 부가 뷰로 접어뒀어요. 기본은 지식그래프·노트·프로젝트맵·브레인스토밍 4개예요.")
    if not _km_adv:
        # 기본 모드: 5번째 이후 탭 버튼만 숨김 — 탭/본문은 그대로(기능 삭제 없음)
        st.markdown(
            """<style>
            div[data-testid="stTabs"] div[data-baseweb="tab-list"] > button:nth-child(n+5){display:none !important;}
            </style>""",
            unsafe_allow_html=True)
    # 대표 시각화(지식그래프)를 맨 앞에 — 변수명은 그대로 두고 표시 순서·이름만 재배치
    (tab3, tab1, tab7, tab6,
     tab5, tab8, tab9, tab2, tab4, tab10) = st.tabs([
        "🕸️ 지식그래프", "📚 노트", "🪐 프로젝트맵", "🤖 브레인스토밍",
        "🧠 개념", "🕸 관계", "📈 성장", "🧩 노션 보드", "🧠 지식 페이지", "🕰️ 타임라인"])

    with tab1:
        _toc_mode = st.radio(
            "목차 보기 방식",
            ["📂 프로젝트 트리", "📒 원노트식 목차"],
            horizontal=True, key="toc_view_mode"
        )

        if _toc_mode == "📂 프로젝트 트리":
            st.markdown("### 📂 프로젝트 트리")
            st.caption("프로젝트 → 섹션 → 단계 → 메모 전체 연결 구조를 한눈에 볼 수 있어요.")
            _all_projects = st.session_state.get("projects", [])
            _all_sections = st.session_state.get("project_sections", [])
            _all_steps = st.session_state.get("project_steps", [])
            _all_notes = st.session_state.get("archive_notes", [])

            if not _all_projects:
                st.info("프로젝트가 없어요. 📁 프로젝트 메뉴에서 먼저 프로젝트를 만들어보세요.")
            else:
                for _proj in _all_projects:
                    _pid = _proj["id"]
                    _pname = _proj["name"]
                    _psecs = [s for s in _all_sections if s.get("project_id") == _pid]
                    _pnotes_all = [n for n in _all_notes if n.get("project") == _pname]
                    _sc = {"진행 중": "🟢", "예정": "🔵", "완료": "⚫", "보류": "🟡"}.get(_proj.get("status",""), "⚪")
                    with st.expander(
                        f"{_sc} **{_pname}** · {_proj.get('category','')} · {_proj.get('status','')} · 메모 {len(_pnotes_all)}개",
                        expanded=True
                    ):
                        if not _psecs:
                            # 섹션 없는 메모
                            _lone_notes = _pnotes_all
                            if _lone_notes:
                                for _n in _lone_notes:
                                    _step_info = _n.get("step","")
                                    _step_str = f" 🔖{_step_info}" if _step_info and _step_info != "없음" else ""
                                    st.markdown(
                                        f"&nbsp;&nbsp;📝 **{_n.get('title','')[:50]}**"
                                        f"{_step_str} · {_n.get('score',0)}점 · {_n.get('saved_at','')[:10]}"
                                    )
                            else:
                                st.caption("연결된 메모 없음")
                        else:
                            for _sec in _psecs:
                                _sec_steps = [s for s in _all_steps if s.get("section_id") == _sec["id"]]
                                _sec_notes = [n for n in _pnotes_all if n.get("section") == _sec["name"]]
                                st.markdown(f"📂 **{_sec['name']}** · 단계 {len(_sec_steps)}개 · 메모 {len(_sec_notes)}개")
                                if _sec_steps:
                                    for _stp in _sec_steps:
                                        _stp_notes = [n for n in _sec_notes if n.get("step") == _stp["name"]]
                                        st.markdown(f"&nbsp;&nbsp;🔖 **{_stp['name']}** ({len(_stp_notes)}개)")
                                        for _n in _stp_notes:
                                            st.markdown(
                                                f"&nbsp;&nbsp;&nbsp;&nbsp;📝 {_n.get('title','')[:45]}"
                                                f" · {_n.get('score',0)}점 · {_n.get('saved_at','')[:10]}"
                                            )
                                # 단계 미분류 메모
                                _unsorted = [n for n in _sec_notes if not n.get("step") or n.get("step") == "없음"]
                                if _unsorted:
                                    st.markdown(f"&nbsp;&nbsp;📌 **단계 미분류** ({len(_unsorted)}개)")
                                    for _n in _unsorted:
                                        st.markdown(
                                            f"&nbsp;&nbsp;&nbsp;&nbsp;📝 {_n.get('title','')[:45]}"
                                            f" · {_n.get('score',0)}점 · {_n.get('saved_at','')[:10]}"
                                        )
                        # 개념 연결 요약
                        _proj_links = [l for l in st.session_state.get("note_concept_links", [])
                                       for _n in _pnotes_all if l.get("note_id") == _n.get("id")]
                        _concept_set = {l["concept"] for l in _proj_links}
                        if _concept_set:
                            st.caption("🧠 연결된 개념: " + " · ".join(sorted(_concept_set)[:10]))

        else:
            st.markdown("### 📚 원노트식 목차")
            st.markdown(
                """
                <div class="pkm-info-box">
                📒 <b>원노트식 구조</b><br>
                날짜만 나열하지 않고, 대분류 → 중분류 → 문서 순서로 정리해요.
                </div>
                """,
                unsafe_allow_html=True,
            )

        if _toc_mode == "📒 원노트식 목차":
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

            for large_idx, (large, middle_groups) in enumerate(sorted(grouped.items())):
                with st.expander(f"📒 {large} · {sum(len(v) for v in middle_groups.values())}개", expanded=True):
                    for middle_idx, (middle, group) in enumerate(sorted(middle_groups.items())):
                        st.markdown(f'<span class="pkm-section-pill">📑 {middle}</span>', unsafe_allow_html=True)
                        for item_i, item in enumerate(group):
                            tag_text = ", ".join(item.get("tags", [])) or "태그 없음"
                            item_unique_key = item.get("raw_index", f"{large_idx}_{middle_idx}_{item_i}")
                            _toc_html = (
                                '<div class="toc-box">'
                                '<div class="toc-title">' + str(item.get("title", "제목 없음")) + '</div>'
                                '<div class="toc-meta">' + str(item.get("kind","")) + " · " + str(item.get("score",0)) + "점 · " + display_source_label(item.get("url","")) + '</div>'
                                '<div class="toc-meta">태그: ' + tag_text + '</div>'
                                '</div>'
                            )
                            st.markdown(_toc_html, unsafe_allow_html=True)
                            st.button(
                                "열기",
                                key=f"knowledge_toc_open_{large_idx}_{middle_idx}_{item_i}_{item_unique_key}",
                                use_container_width=True,
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

        _board_project_names = ["전체"] + sorted({
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
            board_items = [item for item in board_items if board_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]]
        if board_date != "전체":
            from datetime import datetime as _dt
            def _days_since(d):
                try:
                    _s = str(d or "").strip()[:10]
                    if not _s:
                        return -1
                    return max(0, (_dt.now() - _dt.strptime(_s, "%Y-%m-%d")).days)
                except Exception:
                    return -1
            if board_date == "오늘/어제":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 1]
            elif board_date == "최근 7일":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 7]
            elif board_date == "최근 30일":
                board_items = [item for item in board_items if 0 <= _days_since(item.get("date", "")) <= 30]
            elif board_date == "오래된 기록":
                # -1 = 날짜 없음 → 오래된 기록으로 포함
                board_items = [item for item in board_items if _days_since(item.get("date", "")) > 30 or _days_since(item.get("date", "")) == -1]
        board_items = [item for item in board_items if int(item.get("score", 0) or 0) >= min_score]
        st.caption(f"🔎 필터 결과: {len(board_items)}개 항목")

        col_names_all = ["뉴스/이슈", "정책/지원사업", "후기/리뷰", "공부/취업", "기타"]

        # ── 👁 표시 설정 (노션식: 보고 싶은 열·항목만) ──
        with st.expander("👁 표시 설정 — 보고 싶은 열·정보만 골라요"):
            _vshow_cols = st.multiselect(
                "표시할 분류(열)", col_names_all,
                default=st.session_state.get("bd_show_cols_v", col_names_all),
                key="bd_show_cols_v",
                help="체크된 분류만 보드에 열로 표시돼요. (이동 기능은 5개 분류 그대로 동작)",
            )
            _vc1, _vc2, _vc3 = st.columns(3)
            _bd_show_meta = _vc1.checkbox("종류·점수", value=st.session_state.get("bd_show_meta_v", True), key="bd_show_meta_v")
            _bd_show_tags = _vc2.checkbox("태그", value=st.session_state.get("bd_show_tags_v", True), key="bd_show_tags_v")
            _bd_show_proj = _vc3.checkbox("프로젝트", value=st.session_state.get("bd_show_proj_v", False), key="bd_show_proj_v")

        display_cols = [n for n in col_names_all if n in _vshow_cols] or col_names_all
        board_cols = st.columns(len(display_cols))

        def _board_set_large(item, new_cat):
            """지식 아이템의 large 카테고리를 변경하고 저장"""
            uid = get_knowledge_uid(item) if "get_knowledge_uid" in globals() else str(item.get("raw_index",""))
            if uid:
                _ov = st.session_state.setdefault("pkm_category_overrides", {})
                _ov.setdefault(uid, {})["large"] = new_cat
            # archive_notes / saved_analyses 원본에도 반영
            _raw = item.get("raw", {})
            if isinstance(_raw, dict):
                _raw["large_category"] = new_cat
            save_persisted_data()

        for col, name in zip(board_cols, display_cols):
            col_idx = col_names_all.index(name)
            with col:
                st.markdown(f"#### {name}")
                group = [item for item in board_items if infer_large_category(item) == name]
                if not group:
                    st.caption("비어 있음")

                for board_item_idx, item in enumerate(group[:12]):
                    _meta_html = ""
                    if _bd_show_meta:
                        _meta_html += f'<div class="map-mini-meta">{item.get("kind")} · {item.get("score", 0)}점</div>'
                    if _bd_show_proj and item.get("project"):
                        _meta_html += f'<div class="map-mini-meta">📁 {item.get("project")}</div>'
                    if _bd_show_tags:
                        tags = ", ".join(item.get("tags", [])[:3]) or "태그 없음"
                        _meta_html += f'<div class="map-mini-meta">{tags}</div>'
                    st.markdown(
                        f"""
                        <div class="map-mini-card">
                            <div class="map-mini-title">{item.get("title", "제목 없음")}</div>
                            {_meta_html}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    board_unique_key = item.get("raw_index", f"{name}_{board_item_idx}")
                    _bk = f"{name}_{board_item_idx}_{board_unique_key}"
                    # 4칸 균등 그리드 — 모든 버튼 동일 너비로 정렬
                    _bc1, _bc2, _bc3, _bc4 = st.columns(4)
                    with _bc1:
                        if st.button("←", key=f"bd_left_{_bk}", help=(f"{col_names_all[col_idx-1]}로 이동" if col_idx > 0 else "맨 왼쪽"),
                                     disabled=(col_idx == 0), use_container_width=True):
                            _board_set_large(item, col_names_all[col_idx - 1])
                            st.rerun()
                    with _bc2:
                        if st.button("→", key=f"bd_right_{_bk}", help=(f"{col_names_all[col_idx+1]}로 이동" if col_idx < len(col_names_all)-1 else "맨 오른쪽"),
                                     disabled=(col_idx == len(col_names_all) - 1), use_container_width=True):
                            _board_set_large(item, col_names_all[col_idx + 1])
                            st.rerun()
                    with _bc3:
                        st.button(
                            "📂", key=f"knowledge_board_open_{_bk}", help="열기",
                            use_container_width=True,
                            on_click=restore_item_from_knowledge, args=(item,),
                        )
                    with _bc4:
                        _bd_pending = st.session_state.get("_bd_delete_pending")
                        if _bd_pending == _bk:
                            if st.button("✅", key=f"bd_del_yes_{_bk}", help="삭제 확정", use_container_width=True):
                                _bd_kind = item.get("kind")
                                _bd_raw = item.get("raw_item")
                                _bd_list_key = "archive_notes" if _bd_kind == "지식 메모" else "saved_analyses"
                                _bd_list = st.session_state.get(_bd_list_key, [])
                                _bd_new = [x for x in _bd_list if x is not _bd_raw]
                                st.session_state[_bd_list_key] = _bd_new
                                st.session_state.pop("_bd_delete_pending", None)
                                save_persisted_data()
                                _flash(f"'{item.get('title','항목')}'을(를) 삭제했어요.", icon="🗑️")
                                st.rerun()
                        else:
                            if st.button("🗑️", key=f"bd_del_{_bk}", help="삭제", use_container_width=True):
                                st.session_state["_bd_delete_pending"] = _bk
                                st.rerun()

    with tab3:
        st.markdown("### 🕸️ 지식 그래프")

        # ── 필터 패널 (사이드바 스타일) ──
        mm_col_main, mm_col_filter = st.columns([3, 1], gap="medium")

        with mm_col_filter:
            st.markdown("**🔧 필터**")
            _mm_show_tags = st.toggle("태그 노드", value=True, key="mm_show_tags")
            _mm_show_projects = st.toggle("프로젝트 노드", value=True, key="mm_show_projects")
            _mm_show_tasks = st.toggle("할일 노드", value=True, key="mm_show_tasks")
            _mm_show_concepts = st.toggle("개념 노드", value=False, key="mm_show_concepts")
            st.divider()
            _mm_mode = st.radio("배치 모드", ["🌐 전체 관계", "프로젝트별 행성", "태그 중심"], key="mm_mode")
            st.divider()
            _mm_max_tags = st.slider("태그 최대 개수", 5, 30, 16, 1, key="mm_max_tags")
            _mm_min_count = st.slider("최소 연결 수", 1, 10, 1, 1, key="mm_min_count")
            _mm_memos_per_proj = st.slider("프로젝트당 메모 수 (전체 관계)", 3, 20, 5, 1,
                                           key="mm_memos_per_proj",
                                           help="🌐 전체 관계 모드에서 프로젝트별로 보여줄 최근 메모 수")
            _mm_show_reledges = st.toggle("메모↔메모 관계선", value=True, key="mm_show_reledges")
            st.divider()
            _mm_proj_filter = st.multiselect(
                "프로젝트 필터",
                sorted({item.get("project", "기본 프로젝트") for item in items}),
                key="mm_proj_filter"
            )

        with mm_col_main:
            if not total_tags:
                st.info("아직 태그가 없어서 마인드맵을 만들 수 없어요.")
            else:
                import math
                import pandas as pd
                import plotly.graph_objects as go

                # 프로젝트 필터 적용
                _mm_items = items
                if _mm_proj_filter:
                    _mm_items = [it for it in items if it.get("project", "기본 프로젝트") in _mm_proj_filter]

                # 태그 카운트
                tag_counts = {}
                for item in _mm_items:
                    for tag in item.get("tags", []):
                        clean = str(tag).replace("#", "").strip()
                        if clean:
                            tag_counts[clean] = tag_counts.get(clean, 0) + 1

                tag_counts = {k: v for k, v in tag_counts.items() if v >= _mm_min_count}
                top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:_mm_max_tags]

                # 프로젝트 목록
                _all_projects = sorted({item.get("project", "기본 프로젝트") for item in _mm_items})

                node_x, node_y, node_text, node_size, node_color, node_hover = [], [], [], [], [], []
                edge_x, edge_y = [], []
                _rel_edge_x, _rel_edge_y = [], []

                # ── 배치 모드 ──
                if _mm_mode == "🌐 전체 관계":
                    # 메모 중심 ERD: JIUM → 프로젝트 → 메모/연구노트 → (개념·태그·작업)
                    _C_PROJ, _C_MEMO = "#16a34a", "#2563eb"
                    _C_CONCEPT, _C_TAG, _C_TASK = "#8b5cf6", "#f59e0b", "#eab308"
                    _links_all = st.session_state.get("note_concept_links", [])
                    _tasks_all = [t for t in st.session_state.get("tasks", []) if isinstance(t, dict)]
                    _projs = _all_projects
                    n_proj = max(len(_projs), 1)
                    _memo_pos = {}   # 메모 id/제목 → (x,y) : 관계선 그릴 때 사용
                    # 중심 JIUM
                    node_x.append(0); node_y.append(0)
                    node_text.append("🌌 JIUM"); node_size.append(34)
                    node_color.append("#1f3f91"); node_hover.append("내 지식 세계")
                    _R_PROJ = 5.0
                    for _pi, _pn in enumerate(_projs):
                        _pa = 2 * math.pi * _pi / n_proj
                        _ppx, _ppy = math.cos(_pa) * _R_PROJ, math.sin(_pa) * _R_PROJ
                        edge_x += [0, _ppx, None]; edge_y += [0, _ppy, None]
                        _pmemos = [it for it in _mm_items if it.get("project", "기본 프로젝트") == _pn]
                        node_x.append(_ppx); node_y.append(_ppy)
                        node_text.append(f"📁 {_pn}"); node_size.append(20)
                        node_color.append(_C_PROJ); node_hover.append(f"프로젝트: {_pn} · 메모 {len(_pmemos)}개")
                        # 프로젝트의 메모(연구노트) — 최근 N개 (슬라이더)
                        _pmemos_sorted = sorted(
                            _pmemos, key=lambda it: str(it.get("saved_at", "")), reverse=True)
                        _pmemos_top = _pmemos_sorted[:_mm_memos_per_proj]
                        n_m = max(len(_pmemos_top), 1)
                        for _mi, _mo in enumerate(_pmemos_top):
                            _ma = _pa + (2 * math.pi * _mi / n_m) * 0.45 - math.pi * 0.22
                            _mr = 2.6
                            _mx = _ppx + math.cos(_ma) * _mr
                            _my = _ppy + math.sin(_ma) * _mr
                            edge_x += [_ppx, _mx, None]; edge_y += [_ppy, _my, None]
                            _mid = _mo.get("id")
                            _mtitle = str(_mo.get("title", "") or "메모")[:14]
                            node_x.append(_mx); node_y.append(_my)
                            node_text.append(f"📝 {_mtitle}"); node_size.append(13)
                            node_color.append(_C_MEMO)
                            node_hover.append(f"메모: {_mo.get('title','')} — {_pn}")
                            # 관계선용 위치 기록 (id·제목 둘 다 키로)
                            if _mid:
                                _memo_pos[str(_mid)] = (_mx, _my)
                            _mt_full = str(_mo.get("title", "")).strip()
                            if _mt_full:
                                _memo_pos[_mt_full] = (_mx, _my)
                            # 이 메모의 잎: 개념·태그·작업 (합쳐서 최대 6개)
                            _leaves = []
                            for _c in (_mo.get("concepts", []) or [])[:3]:
                                if _c: _leaves.append(("concept", str(_c)))
                            for _tg in (_mo.get("tags", []) or [])[:3]:
                                _tgc = str(_tg).replace("#", "").strip()
                                if _tgc: _leaves.append(("tag", _tgc))
                            for _tk in _tasks_all:
                                if _tk.get("source_note_id") == _mid and _mid:
                                    _leaves.append(("task", str(_tk.get("title", "") or "할일")))
                            _leaves = _leaves[:6]
                            n_l = max(len(_leaves), 1)
                            for _li, (_lt, _lv) in enumerate(_leaves):
                                _la = _ma + (2 * math.pi * _li / n_l) * 0.5 - math.pi * 0.25
                                _lx = _mx + math.cos(_la) * 1.2
                                _ly = _my + math.sin(_la) * 1.2
                                edge_x += [_mx, _lx, None]; edge_y += [_my, _ly, None]
                                node_x.append(_lx); node_y.append(_ly)
                                if _lt == "concept":
                                    node_text.append(f"🧠 {_lv[:10]}"); node_color.append(_C_CONCEPT)
                                    node_hover.append(f"개념: {_lv}")
                                elif _lt == "tag":
                                    node_text.append(f"#{_lv[:10]}"); node_color.append(_C_TAG)
                                    node_hover.append(f"태그: #{_lv}")
                                else:
                                    node_text.append(f"⬜ {_lv[:10]}"); node_color.append(_C_TASK)
                                    node_hover.append(f"작업: {_lv}")
                                node_size.append(9)

                    # ── 메모 ↔ 메모 관계선 (relations) ──
                    if _mm_show_reledges:
                        for _rel in st.session_state.get("relations", []):
                            if not isinstance(_rel, dict):
                                continue
                            _s = str(_rel.get("source_id") or _rel.get("source_name") or "").strip()
                            _t = str(_rel.get("target_id") or _rel.get("target_name") or "").strip()
                            _sn = str(_rel.get("source_name") or "").strip()
                            _tn = str(_rel.get("target_name") or "").strip()
                            _sp = _memo_pos.get(_s) or _memo_pos.get(_sn)
                            _tp = _memo_pos.get(_t) or _memo_pos.get(_tn)
                            if _sp and _tp:
                                _rel_edge_x += [_sp[0], _tp[0], None]
                                _rel_edge_y += [_sp[1], _tp[1], None]

                elif _mm_mode == "프로젝트별 행성":
                    # 프로젝트를 행성처럼 원형 배치, 각 행성 주변에 태그 위성
                    _proj_colors = ["#2563eb","#16a34a","#dc2626","#d97706","#7c3aed","#0891b2","#be185d"]
                    n_proj = max(len(_all_projects), 1)
                    proj_pos = {}
                    CENTER_R = 3.5

                    # 중심 노드
                    if _mm_show_projects:
                        node_x.append(0); node_y.append(0)
                        node_text.append("🌌 JIUM"); node_size.append(35)
                        node_color.append("#1f3f91"); node_hover.append("전체 지식 허브")

                    for pidx, proj_name in enumerate(_all_projects):
                        angle = 2 * math.pi * pidx / n_proj
                        px_ = math.cos(angle) * CENTER_R
                        py_ = math.sin(angle) * CENTER_R
                        proj_pos[proj_name] = (px_, py_)
                        proj_color = _proj_colors[pidx % len(_proj_colors)]

                        # 프로젝트 노드
                        if _mm_show_projects:
                            # 중심 → 프로젝트 엣지
                            edge_x += [0, px_, None]
                            edge_y += [0, py_, None]
                            node_x.append(px_); node_y.append(py_)
                            node_text.append(f"📁 {proj_name}"); node_size.append(22)
                            node_color.append(proj_color)
                            proj_items = [it for it in _mm_items if it.get("project", "기본 프로젝트") == proj_name]
                            node_hover.append(f"{proj_name}: {len(proj_items)}개 문서")

                        # 이 프로젝트의 태그들
                        if _mm_show_tags:
                            proj_tags = {}
                            for it in _mm_items:
                                if it.get("project", "기본 프로젝트") == proj_name:
                                    for tg in it.get("tags", []):
                                        c = str(tg).replace("#","").strip()
                                        if c:
                                            proj_tags[c] = proj_tags.get(c, 0) + 1

                            top_proj_tags = sorted(proj_tags.items(), key=lambda x: x[1], reverse=True)[:8]
                            n_pt = max(len(top_proj_tags), 1)
                            _pc_r = int(proj_color[1:3], 16)
                            _pc_g = int(proj_color[3:5], 16)
                            _pc_b = int(proj_color[5:7], 16)
                            for tidx, (tag, cnt) in enumerate(top_proj_tags):
                                t_angle = angle + 2*math.pi*tidx/n_pt * 0.5 - math.pi*0.25
                                t_r = 1.5 + cnt * 0.15
                                tx = px_ + math.cos(t_angle) * t_r
                                ty = py_ + math.sin(t_angle) * t_r
                                edge_x += [px_, tx, None]
                                edge_y += [py_, ty, None]
                                node_x.append(tx); node_y.append(ty)
                                node_text.append(f"#{tag}"); node_size.append(10 + cnt * 2)
                                node_color.append(f"rgba({_pc_r},{_pc_g},{_pc_b},0.55)")
                                node_hover.append(f"#{tag} ({cnt}회) — {proj_name}")

                        # 이 프로젝트의 개념 노드
                        if _mm_show_concepts:
                            _proj_notes = [it for it in _mm_items if it.get("project", "기본 프로젝트") == proj_name]
                            _proj_concept_set = {}
                            for _pn in _proj_notes:
                                for _tg in _pn.get("tags", []):
                                    _c = str(_tg).replace("#","").strip()
                                    if _c: _proj_concept_set[_c] = _proj_concept_set.get(_c,0)+1
                            # pkm_custom_concepts 중 이 프로젝트 아이템 텍스트에 등장하는 것
                            _all_ccs = st.session_state.get("pkm_custom_concepts", [])
                            _all_text = " ".join([_pn.get("note","") + " " + _pn.get("title","") for _pn in _proj_notes]).lower()
                            for _cc in _all_ccs:
                                _ccn = _cc.get("name","") if isinstance(_cc, dict) else str(_cc)
                                if _ccn and _ccn.lower() in _all_text:
                                    _proj_concept_set[_ccn] = _proj_concept_set.get(_ccn,0)+1
                            _top_concepts = sorted(_proj_concept_set.items(), key=lambda x: x[1], reverse=True)[:6]
                            n_cc = max(len(_top_concepts), 1)
                            for ccidx, (cname, ccnt) in enumerate(_top_concepts):
                                _cc_angle = angle + math.pi + 2*math.pi*ccidx/n_cc * 0.4 - math.pi*0.2
                                _cc_r = 1.8
                                ccx = px_ + math.cos(_cc_angle) * _cc_r
                                ccy = py_ + math.sin(_cc_angle) * _cc_r
                                edge_x += [px_, ccx, None]
                                edge_y += [py_, ccy, None]
                                node_x.append(ccx); node_y.append(ccy)
                                node_text.append(f"🧠 {cname}"); node_size.append(13)
                                node_color.append("#8b5cf6")
                                node_hover.append(f"개념: {cname} ({ccnt}회) — {proj_name}")

                        # 이 프로젝트의 할일 노드 (프로젝트-할일 관계)
                        if _mm_show_tasks:
                            _proj_tasks = [t for t in st.session_state.get("tasks", [])
                                           if isinstance(t, dict) and t.get("project", "") == proj_name][:6]
                            n_tk = max(len(_proj_tasks), 1)
                            for tkidx, _tk in enumerate(_proj_tasks):
                                _tk_angle = angle - math.pi * 0.55 + (2 * math.pi * tkidx / n_tk) * 0.4 - math.pi * 0.2
                                _tk_r = 1.6
                                tkx = px_ + math.cos(_tk_angle) * _tk_r
                                tky = py_ + math.sin(_tk_angle) * _tk_r
                                edge_x += [px_, tkx, None]
                                edge_y += [py_, tky, None]
                                _tk_done = str(_tk.get("status", "")) in ("완료", "보관됨")
                                _tk_title = str(_tk.get("title", "") or "할일")[:12]
                                node_x.append(tkx); node_y.append(tky)
                                node_text.append(("✅ " if _tk_done else "⬜ ") + _tk_title)
                                node_size.append(12)
                                node_color.append("#94a3b8" if _tk_done else "#f59e0b")
                                node_hover.append(
                                    f"할일: {_tk.get('title','')} · {_tk.get('status','')} — {proj_name}")

                else:
                    # 태그 중심 배치
                    if _mm_show_projects:
                        node_x.append(0); node_y.append(0)
                        node_text.append("🌌 JIUM"); node_size.append(35)
                        node_color.append("#1f3f91"); node_hover.append("전체 지식 허브")

                    if _mm_show_tags:
                        n = max(len(top_tags), 1)
                        for idx, (tag, count) in enumerate(top_tags):
                            angle = 2 * math.pi * idx / n
                            x = math.cos(angle) * 2.5
                            y = math.sin(angle) * 2.5
                            edge_x += [0, x, None]
                            edge_y += [0, y, None]
                            node_x.append(x); node_y.append(y)
                            node_text.append(f"#{tag}"); node_size.append(12 + count * 3)
                            node_color.append("#3b82f6")
                            node_hover.append(f"#{tag}: {count}개 문서에서 사용")

                    if _mm_show_concepts:
                        custom_concepts = st.session_state.get("pkm_custom_concepts", [])
                        for cidx, cc in enumerate(custom_concepts[:10]):
                            cname = cc.get("name","") if isinstance(cc, dict) else str(cc)
                            angle = 2 * math.pi * cidx / max(len(custom_concepts[:10]), 1) + 0.3
                            x = math.cos(angle) * 1.2
                            y = math.sin(angle) * 1.2
                            edge_x += [0, x, None]; edge_y += [0, y, None]
                            node_x.append(x); node_y.append(y)
                            node_text.append(f"🧠 {cname}"); node_size.append(12)
                            node_color.append("#8b5cf6"); node_hover.append(f"개념: {cname}")

                # plotly figure
                fig = go.Figure()
                # 엣지
                fig.add_trace(go.Scatter(
                    x=edge_x, y=edge_y, mode="lines",
                    line=dict(width=1, color="#c7d9f5"),
                    hoverinfo="none", showlegend=False
                ))
                # 메모↔메모 관계선 (점선 보라 — 구조선과 구분)
                if _rel_edge_x:
                    fig.add_trace(go.Scatter(
                        x=_rel_edge_x, y=_rel_edge_y, mode="lines",
                        line=dict(width=1.4, color="#a855f7", dash="dot"),
                        hoverinfo="none", showlegend=False
                    ))
                # 노드
                fig.add_trace(go.Scatter(
                    x=node_x, y=node_y,
                    mode="markers+text",
                    text=node_text,
                    textposition="bottom center",
                    textfont=dict(size=11, color="#172033"),
                    marker=dict(size=node_size, color=node_color, line=dict(width=1, color="white")),
                    hovertext=node_hover,
                    hoverinfo="text",
                    showlegend=False
                ))
                fig.update_layout(
                    height=680, showlegend=False,
                    xaxis=dict(visible=False), yaxis=dict(visible=False),
                    plot_bgcolor="#f8fbff", paper_bgcolor="#f8fbff",
                    margin=dict(l=10, r=10, t=20, b=20),
                )
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"노드 {len(node_x)}개 · 연결선 {len([x for x in edge_x if x is None])}개")
                st.markdown(
                    "<div style='font-size:0.82em;color:#64748b;'>"
                    "<span style='color:#16a34a'>● 프로젝트</span> · "
                    "<span style='color:#2563eb'>● 메모/연구노트</span> · "
                    "<span style='color:#8b5cf6'>● 개념</span> · "
                    "<span style='color:#f59e0b'>● 태그</span> · "
                    "<span style='color:#eab308'>● 작업</span> · "
                    "<span style='color:#a855f7'>┈ 메모↔메모 관계</span> — "
                    "노드에 마우스를 올리면 상세가 보여요.</div>",
                    unsafe_allow_html=True)

    with tab4:
        st.markdown("### 🧠 개인 지식 페이지")
        st.markdown(
            """
            <div class="pkm-info-box">
            🧠 <b>나만의 사고 데이터베이스</b><br>
            긴 메모를 하나의 페이지처럼 보고, 그 안에서 자동 목차와 핵심 개념을 뽑아 연결해요.<br>
            나중에는 ESG, CREST, STP처럼 반복 등장하는 개념을 여러 프로젝트 사이에서 자동으로 연결할 수 있어요.
            </div>
            """,
            unsafe_allow_html=True,
        )

        page_options = [
            f"{idx+1}. {item.get('project', '기본 프로젝트')} / {item.get('section', '일반')} / {item.get('title', '제목 없음')}"
            for idx, item in enumerate(items)
        ]
        selected_page_label = st.selectbox(
            "지식 페이지 선택",
            page_options,
            key="knowledge_page_selector",
        )
        selected_page_index = page_options.index(selected_page_label)
        selected_item = items[selected_page_index]
        render_thinking_page_preview(selected_item)

        st.divider()
        st.markdown("### 🔗 연결된 개념")
        selected_concepts = extract_local_concepts(
            selected_item.get("full_text") or selected_item.get("memo", ""),
            selected_item.get("tags", []),
            limit=10,
        )
        if selected_concepts:
            related_items = []
            for other in items:
                if other is selected_item:
                    continue
                other_text = " ".join([
                    str(other.get("title", "")),
                    str(other.get("memo", "")),
                    " ".join([str(t) for t in other.get("tags", [])]),
                ])
                matched = [concept for concept in selected_concepts if concept and concept in other_text]
                if matched:
                    related_items.append((other, matched))

            if related_items:
                for related_idx, (related, matched) in enumerate(related_items[:8]):
                    with st.container(border=True):
                        st.markdown(f"**{related.get('title', '제목 없음')}**")
                        st.caption(f"{related.get('project', '기본 프로젝트')} · {related.get('section', '일반')} · 공통 개념: {', '.join(matched[:5])}")
                        st.button(
                            "이 지식 열기",
                            key=f"knowledge_page_related_open_{related_idx}_{related.get('raw_index', related_idx)}",
                            use_container_width=True,
                            on_click=restore_item_from_knowledge,
                            args=(related,),
                        )
            else:
                st.info("아직 같은 개념으로 연결된 다른 지식이 없어요. 메모가 쌓이면 자동으로 연결돼요.")
        else:
            st.info("이 메모에서 아직 연결할 핵심 개념을 찾지 못했어요.")


    with tab5:
        # ── 개념 수정 / 병합 / 삭제 ──────────────────────────
        with st.expander("✏️ 개념 수정 · 삭제 · 병합", expanded=False):
            _cf_cc_list = [
                c if isinstance(c, dict) else {"name": str(c), "folder": "내 개념"}
                for c in st.session_state.get("pkm_custom_concepts", []) if c
            ]
            _cf_cc_names = [c.get("name", "") for c in _cf_cc_list]
            if not _cf_cc_names:
                st.info("아직 직접 추가한 개념이 없어요. 위 '핵심 개념 직접 추가' 에서 추가할 수 있어요.")
            else:
                _cf_e1, _cf_e2, _cf_e3 = st.columns(3)
                with _cf_e1:
                    st.markdown("**✏️ 이름 수정**")
                    _cf_et = st.selectbox("수정할 개념", _cf_cc_names, key="cf_edit_target")
                    _cf_en = st.text_input("새 이름", key="cf_edit_new", placeholder="바꿀 이름 입력")
                    if st.button("변경 저장", key="cf_do_rename", use_container_width=True, type="primary"):
                        if _cf_et and _cf_en.strip():
                            for c in st.session_state.pkm_custom_concepts:
                                if (c.get("name") if isinstance(c, dict) else str(c)) == _cf_et:
                                    if isinstance(c, dict):
                                        c["name"] = _cf_en.strip()
                            for lk in st.session_state.get("note_concept_links", []):
                                if lk.get("concept") == _cf_et:
                                    lk["concept"] = _cf_en.strip()
                            save_persisted_data()
                            _flash(f"'{_cf_et}' → '{_cf_en.strip()}'")
                            st.rerun()
                with _cf_e2:
                    st.markdown("**🗑️ 삭제**")
                    _cf_del = st.selectbox("삭제할 개념", _cf_cc_names, key="cf_del_target")
                    st.caption("삭제하면 이 개념과 연결된 메모 링크도 제거돼요.")
                    if st.button("🗑️ 삭제 확인", key="cf_do_delete", use_container_width=True):
                        if _cf_del:
                            st.session_state.pkm_custom_concepts = [
                                c for c in _cf_cc_list if c.get("name") != _cf_del
                            ]
                            st.session_state["note_concept_links"] = [
                                lk for lk in st.session_state.get("note_concept_links", [])
                                if lk.get("concept") != _cf_del
                            ]
                            save_persisted_data()
                            _flash(f"'{_cf_del}' 삭제 완료")
                            st.rerun()
                with _cf_e3:
                    st.markdown("**🔗 병합 (A → B)**")
                    _cf_mf = st.selectbox("없앨 개념 A", _cf_cc_names, key="cf_merge_from")
                    _cf_mt = st.selectbox("남길 개념 B", _cf_cc_names, key="cf_merge_to")
                    st.caption("A의 메모 연결이 모두 B로 합쳐지고 A는 삭제돼요.")
                    if st.button("병합 실행", key="cf_do_merge", use_container_width=True, type="primary"):
                        if _cf_mf and _cf_mt and _cf_mf != _cf_mt:
                            st.session_state.pkm_custom_concepts = [
                                c for c in _cf_cc_list if c.get("name") != _cf_mf
                            ]
                            for lk in st.session_state.get("note_concept_links", []):
                                if lk.get("concept") == _cf_mf:
                                    lk["concept"] = _cf_mt
                            save_persisted_data()
                            _flash(f"'{_cf_mf}' → '{_cf_mt}' 병합 완료")
                            st.rerun()
        st.divider()
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
                    render_readable_markdown(_sel_note.get("note", ""), max_chars=1500)
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
                        _user_msg = "메모 제목: " + _sel_note.get("title", "") + "\n\n메모 내용:\n" + _note_content
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
                    _proj_summary = "프로젝트명: " + str(_sel_proj.get("name","")) + "\n설명: " + str(_sel_proj.get("description","")) + "\n상태: " + str(_sel_proj.get("status",""))
                    _notes_summary = "\n".join([f"- {n.get('title','')}: {n.get('note','')[:200]}" for n in _proj_notes[:5]])
                    _system_msg2 = f"""당신은 프로젝트 관리 전문가입니다. 프로젝트 정보와 연결된 메모를 보고 요청한 유형별 분석을 해주세요.
분석 유형: {', '.join(_brain_proj_types)}
각 유형별로 3-5개의 구체적인 항목을 bullet point로 제안하세요. 한국어로 답변하세요."""
                    _user_msg2 = _proj_summary + "\n\n연결된 메모:\n" + (_notes_summary if _notes_summary else "(연결된 메모 없음)")
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
                        st.rerun()

    # ══════════════════════════════════════════════
    # TAB 7 — 프로젝트 지식맵
    # ══════════════════════════════════════════════
    with tab7:
        st.markdown("### 🗺️ 프로젝트 지식맵")
        st.caption("프로젝트 하나를 선택하면 연결된 메모·작업·개념이 마인드맵으로 표시돼요.")

        _pm_projs = st.session_state.get("projects", [])
        _pm_notes = st.session_state.get("archive_notes", [])
        _pm_tasks = st.session_state.get("tasks", [])
        _pm_links = st.session_state.get("note_concept_links", [])

        if not _pm_projs:
            st.info("프로젝트가 없어요. 먼저 📁 프로젝트 메뉴에서 프로젝트를 만들어보세요.")
        else:
            # ── 상단 컨트롤 ──────────────────────────────
            _pmctrl1, _pmctrl2, _pmctrl3, _pmctrl4 = st.columns([2, 1, 1, 1])
            with _pmctrl1:
                _pm_proj_names = [p.get("name", "이름 없음") for p in _pm_projs]
                _pm_sel_idx = st.selectbox("📁 프로젝트 선택", range(len(_pm_proj_names)),
                    format_func=lambda i: _pm_proj_names[i], key="pm_proj_sel")
                _pm_sel_proj = _pm_projs[_pm_sel_idx]
                _pm_sel_name = _pm_sel_proj.get("name", "")
            with _pmctrl2:
                _pm_show_memo  = st.toggle("📝 메모 노드",    value=True,  key="pm_show_memo")
            with _pmctrl3:
                _pm_show_task  = st.toggle("✅ 작업 노드",    value=True,  key="pm_show_task")
            with _pmctrl4:
                _pm_show_con   = st.toggle("🧠 개념 노드",    value=True,  key="pm_show_con")

            # ── 데이터 수집 ──────────────────────────────
            _pm_proj_notes = [n for n in _pm_notes if n.get("project") == _pm_sel_name]
            _pm_proj_tasks = [t for t in _pm_tasks if t.get("project") == _pm_sel_name]

            # 개념: note_concept_links + pkm_custom_concepts에서 수집
            _pm_note_ids = {n.get("id","") for n in _pm_proj_notes}
            _pm_linked_cons = {}
            for _lk in _pm_links:
                if _lk.get("note_id","") in _pm_note_ids:
                    _cn = _lk.get("concept","")
                    if _cn: _pm_linked_cons[_cn] = _pm_linked_cons.get(_cn, 0) + 1
            # 프로젝트 직접 연결 개념
            for _c in _pm_sel_proj.get("concepts", []):
                _pm_linked_cons[_c] = _pm_linked_cons.get(_c, 0) + 2

            # ── 메트릭 요약 ──────────────────────────────
            _ms1, _ms2, _ms3, _ms4, _ms5 = st.columns(5)
            with _ms1: st.metric("📝 메모", f"{len(_pm_proj_notes)}개")
            with _ms2: st.metric("✅ 작업", f"{len(_pm_proj_tasks)}개")
            with _ms3: st.metric("🧠 개념", f"{len(_pm_linked_cons)}개")
            with _ms4:
                _done = sum(1 for t in _pm_proj_tasks if t.get("status") == "완료")
                st.metric("✔ 완료 작업", f"{_done}/{len(_pm_proj_tasks)}")
            with _ms5:
                st.metric("📊 진행률", f"{_pm_sel_proj.get('progress', 0)}%")

            st.divider()

            # ── Plotly 마인드맵 ───────────────────────────
            import math as _pm_math
            import plotly.graph_objects as _pmgo

            _pm_nx, _pm_ny = [], []
            _pm_text, _pm_size, _pm_color, _pm_hover = [], [], [], []
            _pm_ex, _pm_ey = [], []

            def _pm_add_node(x, y, text, size, color, hover):
                _pm_nx.append(x); _pm_ny.append(y)
                _pm_text.append(text); _pm_size.append(size)
                _pm_color.append(color); _pm_hover.append(hover)

            def _pm_add_edge(x1, y1, x2, y2):
                _pm_ex.extend([x1, x2, None])
                _pm_ey.extend([y1, y2, None])

            # 중심 노드 — 프로젝트
            _pm_add_node(0, 0,
                f"📁 {_pm_sel_name[:14]}",
                45, "#1e3a8a",
                f"프로젝트: {_pm_sel_name}\n상태: {_pm_sel_proj.get('status','')}\n진행률: {_pm_sel_proj.get('progress',0)}%")

            # ── 메모 노드 (위쪽 반원) ─────────────────────
            if _pm_show_memo and _pm_proj_notes:
                _n_memo = len(_pm_proj_notes)
                _memo_r = max(2.8, 1.0 + _n_memo * 0.3)
                for _mi, _mn in enumerate(_pm_proj_notes[:12]):
                    _angle = _pm_math.pi * (0.1 + 0.8 * _mi / max(_n_memo - 1, 1))
                    _mx = _pm_math.cos(_angle) * _memo_r
                    _my = _pm_math.sin(_angle) * _memo_r
                    _score = _mn.get("score", 0)
                    _score_color = "#10b981" if _score >= 75 else "#f59e0b" if _score >= 50 else "#ef4444"
                    _title_short = _mn.get("title", "제목 없음")[:16]
                    _pm_add_edge(0, 0, _mx, _my)
                    _pm_add_node(_mx, _my,
                        f"📝 {_title_short}",
                        18 + min(_score // 10, 10),
                        _score_color,
                        f"메모: {_mn.get('title','')}\n신뢰도: {_score}점\n저장일: {_mn.get('saved_at','')[:10]}\n섹션: {_mn.get('section','')}")

            # ── 작업 노드 (아래쪽 반원) ────────────────────
            if _pm_show_task and _pm_proj_tasks:
                _n_task = len(_pm_proj_tasks)
                _task_r = max(2.8, 1.0 + _n_task * 0.3)
                _task_status_color = {"시작전": "#94a3b8", "진행중": "#3b82f6", "완료": "#10b981", "보류": "#f59e0b"}
                for _ti, _tn in enumerate(_pm_proj_tasks[:12]):
                    _angle = _pm_math.pi * (1.1 + 0.8 * _ti / max(_n_task - 1, 1))
                    _tx = _pm_math.cos(_angle) * _task_r
                    _ty = _pm_math.sin(_angle) * _task_r
                    _t_color = _task_status_color.get(_tn.get("status", "시작전"), "#94a3b8")
                    _t_title = _tn.get("title", "")[:16]
                    _pm_add_edge(0, 0, _tx, _ty)
                    _pm_add_node(_tx, _ty,
                        f"✅ {_t_title}",
                        16,
                        _t_color,
                        f"작업: {_tn.get('title','')}\n상태: {_tn.get('status','')}\n우선순위: {_tn.get('priority','')}\n마감: {_tn.get('due_date','없음')}")

            # ── 개념 노드 (오른쪽) ─────────────────────────
            if _pm_show_con and _pm_linked_cons:
                _con_sorted = sorted(_pm_linked_cons.items(), key=lambda x: x[1], reverse=True)[:10]
                _n_con = len(_con_sorted)
                _con_r = 3.2
                for _ci, (_cname, _ccnt) in enumerate(_con_sorted):
                    _angle = -_pm_math.pi * 0.4 + _pm_math.pi * 0.8 * _ci / max(_n_con - 1, 1)
                    _cx = _pm_math.cos(_angle) * _con_r + 1.0
                    _cy = _pm_math.sin(_angle) * _con_r
                    _pm_add_edge(0, 0, _cx, _cy)
                    _pm_add_node(_cx, _cy,
                        f"🧠 {_cname[:14]}",
                        13 + min(_ccnt * 2, 8),
                        "#8b5cf6",
                        f"개념: {_cname}\n연결 메모 수: {_ccnt}개")

            # ── 태그 클러스터 (중심 주변 작은 노드) ──────────
            from collections import Counter as _PMCnt
            _pm_tag_cnt = _PMCnt()
            for _pn in _pm_proj_notes:
                for _tg in _pn.get("tags", []):
                    _pm_tag_cnt[str(_tg).replace("#","").strip()] += 1
            _top_pm_tags = _pm_tag_cnt.most_common(8)
            if _top_pm_tags:
                _n_tag = len(_top_pm_tags)
                for _tgi, (_tgname, _tgcnt) in enumerate(_top_pm_tags):
                    _t_angle = 2 * _pm_math.pi * _tgi / _n_tag
                    _tgx = _pm_math.cos(_t_angle) * 1.4
                    _tgy = _pm_math.sin(_t_angle) * 1.4
                    _pm_add_edge(0, 0, _tgx, _tgy)
                    _pm_add_node(_tgx, _tgy,
                        f"#{_tgname[:10]}",
                        9 + min(_tgcnt * 2, 6),
                        "#60a5fa",
                        f"태그: #{_tgname} ({_tgcnt}개 메모)")

            # ── 렌더링 ─────────────────────────────────────
            _pm_fig = _pmgo.Figure()

            # 엣지
            _pm_fig.add_trace(_pmgo.Scatter(
                x=_pm_ex, y=_pm_ey, mode="lines",
                line=dict(width=1.2, color="rgba(148,163,184,0.4)"),
                hoverinfo="none", showlegend=False
            ))

            # 노드
            _pm_fig.add_trace(_pmgo.Scatter(
                x=_pm_nx, y=_pm_ny,
                mode="markers+text",
                text=_pm_text,
                textposition="top center",
                textfont=dict(size=10, color="#1e293b"),
                marker=dict(
                    size=_pm_size,
                    color=_pm_color,
                    line=dict(width=1.5, color="white"),
                    opacity=0.9,
                ),
                hovertext=_pm_hover,
                hoverinfo="text",
                showlegend=False
            ))

            _pm_fig.update_layout(
                height=620,
                showlegend=False,
                xaxis=dict(visible=False, range=[-5.5, 5.5]),
                yaxis=dict(visible=False, range=[-4.5, 4.5]),
                plot_bgcolor="#f0f4ff",
                paper_bgcolor="#f0f4ff",
                margin=dict(l=10, r=10, t=20, b=10),
            )
            st.plotly_chart(_pm_fig, use_container_width=True)

            # ── 범례 ──────────────────────────────────────
            _lg1, _lg2, _lg3, _lg4, _lg5 = st.columns(5)
            with _lg1: st.markdown('<span style="color:#1e3a8a;font-size:1.2em">●</span> **프로젝트**', unsafe_allow_html=True)
            with _lg2: st.markdown('<span style="color:#10b981;font-size:1.2em">●</span> **메모 (신뢰↑)**', unsafe_allow_html=True)
            with _lg3: st.markdown('<span style="color:#f59e0b;font-size:1.2em">●</span> **메모 (신뢰중)**', unsafe_allow_html=True)
            with _lg4: st.markdown('<span style="color:#8b5cf6;font-size:1.2em">●</span> **개념**', unsafe_allow_html=True)
            with _lg5: st.markdown('<span style="color:#60a5fa;font-size:1.2em">●</span> **태그**', unsafe_allow_html=True)

            st.divider()

            # ── 선택 프로젝트 상세 패널 ────────────────────
            _detail_t1, _detail_t2, _detail_t3 = st.tabs(["📝 연결 메모 목록", "✅ 작업 현황", "🧠 개념 목록"])

            with _detail_t1:
                if not _pm_proj_notes:
                    st.info("이 프로젝트에 연결된 메모가 없어요.")
                else:
                    for _ni, _pn in enumerate(_pm_proj_notes):
                        _score = _pn.get("score", 0)
                        _score_emoji = "🟢" if _score >= 75 else "🟡" if _score >= 50 else "🔴"
                        with st.expander(f"{_score_emoji} {_pn.get('title','제목 없음')} — {_pn.get('saved_at','')[:10]}", expanded=False):
                            st.markdown(f"**섹션:** {_pn.get('section','')} | **신뢰도:** {_score}점")
                            _tags = [str(t) for t in _pn.get("tags",[])]
                            if _tags: st.markdown(f"**태그:** {' '.join(_tags)}")
                            render_readable_markdown(_pn.get("note", ""), max_chars=500)

            with _detail_t2:
                if not _pm_proj_tasks:
                    st.info("이 프로젝트에 연결된 작업이 없어요.")
                else:
                    _status_order = ["진행중", "시작전", "보류", "완료"]
                    _status_emoji = {"시작전": "⬜", "진행중": "🔵", "완료": "✅", "보류": "⏸️"}
                    for _st_grp in _status_order:
                        _grp_tasks = [t for t in _pm_proj_tasks if t.get("status") == _st_grp]
                        if _grp_tasks:
                            st.markdown(f"**{_status_emoji.get(_st_grp,'')} {_st_grp}** ({len(_grp_tasks)}개)")
                            for _gt in _grp_tasks:
                                _pri = _gt.get("priority","")
                                _due = _gt.get("due_date","")
                                _due_str = f" · 마감 {_due}" if _due else ""
                                _pri_str = f" · {_pri}" if _pri else ""
                                st.markdown(f"  - {_gt.get('title','')}{_pri_str}{_due_str}")

            with _detail_t3:
                if not _pm_linked_cons:
                    st.info("연결된 개념이 없어요. 메모를 저장하면 자동으로 개념이 연결돼요.")
                else:
                    _con_sorted2 = sorted(_pm_linked_cons.items(), key=lambda x: x[1], reverse=True)
                    _cg1, _cg2, _cg3 = st.columns(3)
                    for _ci2, (_cname2, _ccnt2) in enumerate(_con_sorted2):
                        with [_cg1, _cg2, _cg3][_ci2 % 3]:
                            st.markdown(f"""<div style="background:#ede9fe;border-radius:8px;
                                padding:8px 12px;margin-bottom:6px;">
                                <span style="font-weight:700;color:#5b21b6;">🧠 {_cname2}</span>
                                <span style="color:#7c3aed;font-size:0.8em;float:right;">{_ccnt2}회</span>
                            </div>""", unsafe_allow_html=True)

    # TAB 8 — 관계형 지식맵
    with tab8:
        st.markdown("### 🔗 관계형 지식맵")
        st.caption("저장된 관계(Relations)와 개념·메모·프로젝트를 옵시디언 스타일 네트워크로 시각화해요.")

        import math as _rg_math

        _rg_relations = st.session_state.get("relations", [])
        _rg_notes = st.session_state.get("archive_notes", [])
        _rg_projects = st.session_state.get("projects", [])
        _rg_concepts = [
            c.get("name") if isinstance(c, dict) else str(c)
            for c in st.session_state.get("pkm_custom_concepts", []) if c
        ]
        _rg_nclinks = st.session_state.get("note_concept_links", [])

        # ── filter controls ──
        _rg_fc1, _rg_fc2 = st.columns([3, 1])
        with _rg_fc1:
            _rg_show_types = st.multiselect(
                "노드 유형 표시",
                ["📁 프로젝트", "📝 메모", "🧠 개념", "🏷️ 태그"],
                default=["📁 프로젝트", "📝 메모", "🧠 개념"],
                key="rg_show_types"
            )
        with _rg_fc2:
            _rg_min_degree = st.number_input("최소 연결 수", 0, 20, 0, 1, key="rg_min_degree")

        _rg_proj_names = [p.get("name", "") for p in _rg_projects]
        _rg_proj_filter = st.multiselect("프로젝트 필터 (비어있으면 전체)", _rg_proj_names, key="rg_proj_filter")

        # ── build graph ──
        _rg_nodes = {}  # name → {type, label, connections}
        _rg_edges = []  # {src, tgt, rtype}

        def _rg_add_node(name, ntype):
            if name and name not in _rg_nodes:
                _rg_nodes[name] = {"type": ntype, "label": name, "degree": 0}

        def _rg_add_edge(src, tgt, rtype="연결"):
            if src and tgt and src != tgt:
                _rg_edges.append({"src": src, "tgt": tgt, "rtype": rtype})
                if src in _rg_nodes:
                    _rg_nodes[src]["degree"] = _rg_nodes[src].get("degree", 0) + 1
                if tgt in _rg_nodes:
                    _rg_nodes[tgt]["degree"] = _rg_nodes[tgt].get("degree", 0) + 1

        # Add nodes from each type
        if "📁 프로젝트" in _rg_show_types:
            for _rp in _rg_projects:
                _rpn = _rp.get("name", "")
                if not _rg_proj_filter or _rpn in _rg_proj_filter:
                    _rg_add_node(_rpn, "project")

        if "📝 메모" in _rg_show_types:
            for _rn in _rg_notes:
                _rnn = _rn.get("title", "")
                _rnp = _rn.get("project", "")
                if not _rg_proj_filter or _rnp in _rg_proj_filter:
                    _rg_add_node(_rnn, "note")
                    if "📁 프로젝트" in _rg_show_types and _rnp:
                        _rg_add_edge(_rnp, _rnn, "포함")

        if "🧠 개념" in _rg_show_types:
            for _rc in _rg_concepts:
                _rg_add_node(_rc, "concept")

        if "🏷️ 태그" in _rg_show_types:
            for _rn2 in _rg_notes:
                _rnp2 = _rn2.get("project", "")
                if _rg_proj_filter and _rnp2 not in _rg_proj_filter:
                    continue
                for _rtag in _rn2.get("tags", []):
                    _rtagname = str(_rtag).replace("#", "").strip()
                    if _rtagname:
                        _rg_add_node(_rtagname, "tag")

        # Add edges from relations DB
        _rtype_colors = {
            "포함": "#3b82f6", "참조": "#8b5cf6", "반박": "#ef4444",
            "지지": "#22c55e", "확장": "#f59e0b", "연결": "#64748b",
            "유사": "#06b6d4", "선행": "#ec4899",
        }
        for _rrel in _rg_relations:
            _rsrc = _rrel.get("source_name", "")
            _rtgt = _rrel.get("target_name", "")
            _rrt = _rrel.get("relation_type", "연결")
            if _rsrc in _rg_nodes and _rtgt in _rg_nodes:
                _rg_add_edge(_rsrc, _rtgt, _rrt)

        # Add edges from note_concept_links
        if "🧠 개념" in _rg_show_types and "📝 메모" in _rg_show_types:
            _ncl_map = {}
            for _nl in _rg_nclinks:
                _nid = _nl.get("note_id", "")
                _nc = _nl.get("concept", "")
                _ncl_map.setdefault(_nid, []).append(_nc)
            for _rn3 in _rg_notes:
                _rnn3 = _rn3.get("title", "")
                _rnid3 = _rn3.get("id", "")
                for _rnc in _ncl_map.get(_rnid3, []):
                    if _rnn3 in _rg_nodes and _rnc in _rg_nodes:
                        _rg_add_edge(_rnn3, _rnc, "연결")

        # ── filter by min_degree ──
        if _rg_min_degree > 0:
            _rg_nodes = {k: v for k, v in _rg_nodes.items() if v.get("degree", 0) >= _rg_min_degree}
            _rg_edges = [e for e in _rg_edges if e["src"] in _rg_nodes and e["tgt"] in _rg_nodes]

        if not _rg_nodes:
            st.info("표시할 노드가 없어요. 메모/프로젝트/개념을 추가하고 관계를 설정해보세요.")
        else:
            # ── circular layout ──
            _rg_node_list = list(_rg_nodes.keys())
            _rg_n = len(_rg_node_list)
            _rg_pos = {}
            for _rgi, _rgname in enumerate(_rg_node_list):
                _angle = 2 * _rg_math.pi * _rgi / max(_rg_n, 1)
                _deg = _rg_nodes[_rgname].get("degree", 0)
                _r = 1.0 + 0.05 * _deg
                _rg_pos[_rgname] = (_r * _rg_math.cos(_angle), _r * _rg_math.sin(_angle))

            import plotly.graph_objects as _rg_go

            _rg_fig = _rg_go.Figure()

            # draw edges grouped by relation type
            _edges_by_type = {}
            for _re in _rg_edges:
                _edges_by_type.setdefault(_re["rtype"], []).append(_re)

            for _rtype, _redges in _edges_by_type.items():
                _ex, _ey, _etext = [], [], []
                for _re2 in _redges:
                    if _re2["src"] in _rg_pos and _re2["tgt"] in _rg_pos:
                        sx, sy = _rg_pos[_re2["src"]]
                        tx, ty = _rg_pos[_re2["tgt"]]
                        _ex += [sx, tx, None]
                        _ey += [sy, ty, None]
                        _etext.append(_rtype)
                _rg_fig.add_trace(_rg_go.Scatter(
                    x=_ex, y=_ey, mode="lines",
                    line=dict(color=_rtype_colors.get(_rtype, "#94a3b8"), width=1.5),
                    hoverinfo="none",
                    showlegend=True,
                    name=_rtype,
                    legendgroup=_rtype,
                ))

            # draw nodes by type
            _ntype_cfg = {
                "project": ("📁", "#3b82f6", 22),
                "note": ("📝", "#10b981", 16),
                "concept": ("🧠", "#8b5cf6", 14),
                "tag": ("🏷️", "#f59e0b", 12),
            }
            _nodes_by_type = {}
            for _rnk, _rnv in _rg_nodes.items():
                _nodes_by_type.setdefault(_rnv["type"], []).append(_rnk)

            for _nt, _nlist in _nodes_by_type.items():
                _icon, _color, _size = _ntype_cfg.get(_nt, ("⚪", "#64748b", 12))
                _nx = [_rg_pos[n][0] for n in _nlist if n in _rg_pos]
                _ny = [_rg_pos[n][1] for n in _nlist if n in _rg_pos]
                _nlabels = [f"{_icon} {n}" for n in _nlist if n in _rg_pos]
                _rg_fig.add_trace(_rg_go.Scatter(
                    x=_nx, y=_ny, mode="markers+text",
                    marker=dict(size=_size, color=_color, line=dict(color="#fff", width=2)),
                    text=_nlabels,
                    textposition="top center",
                    textfont=dict(size=10),
                    hovertext=_nlabels,
                    hoverinfo="text",
                    name=_icon + " " + _nt,
                    legendgroup=_nt,
                    showlegend=True,
                ))

            _rg_fig.update_layout(
                height=600,
                showlegend=True,
                legend=dict(orientation="v", x=1.01, y=1),
                xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                plot_bgcolor="#f8fafc",
                paper_bgcolor="#f8fafc",
                margin=dict(l=20, r=160, t=20, b=20),
                hovermode="closest",
            )
            st.plotly_chart(_rg_fig, use_container_width=True)

            # ── node detail panel ──
            st.divider()
            st.markdown("#### 🔍 노드 상세")
            _rg_sel = st.selectbox("노드 선택", ["(선택 안 함)"] + _rg_node_list, key="rg_sel_node")
            if _rg_sel and _rg_sel != "(선택 안 함)":
                _rg_ninfo = _rg_nodes.get(_rg_sel, {})
                _rg_ntype = _rg_ninfo.get("type", "")
                _type_labels = {"project": "📁 프로젝트", "note": "📝 메모", "concept": "🧠 개념", "tag": "🏷️ 태그"}
                with st.container(border=True):
                    st.markdown(f"**{_type_labels.get(_rg_ntype, _rg_ntype)}** — `{_rg_sel}`")
                    _rg_connected = [e for e in _rg_edges if e["src"] == _rg_sel or e["tgt"] == _rg_sel]

                    # 관련 메모(개념/태그일 때)
                    _rel_memos = []
                    if _rg_ntype in ("concept", "tag"):
                        _rel_memos = [n for n in _rg_notes if
                                      _rg_sel in [str(t).replace("#","").strip() for t in n.get("tags",[])] or
                                      any(l.get("concept") == _rg_sel for l in _rg_nclinks if l.get("note_id") == n.get("id",""))]
                    # 연결 노드 유형별 집계
                    _conn_types = {"project": 0, "note": 0, "concept": 0, "tag": 0}
                    for _rce in _rg_connected:
                        _other = _rce["tgt"] if _rce["src"] == _rg_sel else _rce["src"]
                        _ot = _rg_nodes.get(_other, {}).get("type", "")
                        if _ot in _conn_types:
                            _conn_types[_ot] += 1
                    st.caption(
                        f"🔗 연결 {len(_rg_connected)} · 📝 관련 메모 {len(_rel_memos)} · "
                        f"📁 {_conn_types['project']} · 🧠 {_conn_types['concept']} · 🏷️ {_conn_types['tag']}")

                    # 공통 다음 행동 버튼 (노드 유형별)
                    _rg_ctx = {"project": "project", "note": "note"}.get(_rg_ntype, "concept")
                    render_action_buttons(_rg_ctx, target_name=_rg_sel,
                                          key_prefix=f"rg_act_{_rg_sel}")

                    if _rg_connected:
                        st.markdown("**연결된 항목**")
                        for _rce in _rg_connected[:15]:
                            _other = _rce["tgt"] if _rce["src"] == _rg_sel else _rce["src"]
                            _dir = "→" if _rce["src"] == _rg_sel else "←"
                            st.markdown(f"- {_dir} **{_other}** ({_rce['rtype']})")
                    else:
                        st.info("이 노드에 연결된 관계가 없어요. 데이터 관리 → 관계 관리에서 연결해보세요.")

                    # 관련 메모 — 클릭 이동
                    if _rel_memos:
                        st.markdown(f"**관련 메모 ({len(_rel_memos)}개)**")
                        for _rmi, _rm in enumerate(_rel_memos[:5]):
                            _rmc1, _rmc2 = st.columns([5, 1])
                            with _rmc1:
                                st.markdown(f"📝 {_rm.get('title','제목 없음')}")
                            with _rmc2:
                                if _rm.get("id") and st.button("열기", key=f"rg_open_memo_{_rm.get('id')}_{_rmi}",
                                                               use_container_width=True):
                                    st.session_state["archive_open_note_id"] = _rm.get("id")
                                    st.query_params["page"] = "archive"
                                    st.rerun()

    # ══════════════════════════════════════════════════════════
    # TAB 9 — 📈 지식 성장 (Graph Evolution)
    # ══════════════════════════════════════════════════════════
    with tab9:
        st.markdown("### 📈 지식 성장 (Graph Evolution)")
        st.caption("시간 슬라이더를 움직이면 그 시점까지의 지식맵이 어떻게 자라왔는지 보여줘요.")

        import math as _ge_math

        def _ge_month(dstr):
            """'2026-06-01 13:20' / '2026-06-01' → '2026-06'. 실패 시 None."""
            if not dstr or not isinstance(dstr, str):
                return None
            s = dstr.strip()[:7]
            if len(s) == 7 and s[4] == "-" and s[:4].isdigit():
                return s
            return None

        _ge_notes = st.session_state.get("archive_notes", [])
        _ge_projects = st.session_state.get("projects", [])
        _ge_relations = st.session_state.get("relations", [])
        _ge_concepts_raw = st.session_state.get("pkm_custom_concepts", [])
        _ge_nclinks = st.session_state.get("note_concept_links", [])

        # 각 엔터티의 (월, 종류) 수집
        _ge_events = []  # (month, kind)
        for _n in _ge_notes:
            m = _ge_month(_n.get("saved_at") or _n.get("created_at"))
            if m: _ge_events.append((m, "note"))
        for _p in _ge_projects:
            m = _ge_month(_p.get("created_at"))
            if m: _ge_events.append((m, "project"))
        for _r in _ge_relations:
            m = _ge_month(_r.get("created_at"))
            if m: _ge_events.append((m, "relation"))
        for _c in _ge_concepts_raw:
            if isinstance(_c, dict):
                m = _ge_month(_c.get("created_at"))
                if m: _ge_events.append((m, "concept"))

        _ge_months = sorted({m for m, _ in _ge_events})

        if len(_ge_months) < 1:
            st.info("아직 날짜가 기록된 데이터가 부족해요. 메모·프로젝트·개념을 더 추가하면 성장 그래프가 만들어져요.")
        else:
            # ── 시간 슬라이더 ──
            if len(_ge_months) == 1:
                _ge_cutoff = _ge_months[0]
                st.markdown(f"**기준 시점:** `{_ge_cutoff}` (데이터가 한 달치라 슬라이더 생략)")
            else:
                _ge_cutoff = st.select_slider(
                    "기준 시점 (이 달까지 누적)",
                    options=_ge_months,
                    value=_ge_months[-1],
                    key="ge_cutoff",
                )

            _ge_idx = _ge_months.index(_ge_cutoff)
            _ge_prev = _ge_months[_ge_idx - 1] if _ge_idx > 0 else None

            # ── 누적 카운트 (cutoff까지) ──
            def _cum_count(kind, upto):
                return sum(1 for m, k in _ge_events if k == kind and m <= upto)

            _m1, _m2, _m3, _m4 = st.columns(4)
            def _delta(kind):
                if not _ge_prev:
                    return None
                return _cum_count(kind, _ge_cutoff) - _cum_count(kind, _ge_prev)
            _m1.metric("📁 프로젝트", _cum_count("project", _ge_cutoff), _delta("project"))
            _m2.metric("📝 메모", _cum_count("note", _ge_cutoff), _delta("note"))
            _m3.metric("🧠 개념", _cum_count("concept", _ge_cutoff), _delta("concept"))
            _m4.metric("🔗 관계", _cum_count("relation", _ge_cutoff), _delta("relation"))

            st.divider()

            # ── 성장 곡선 (월별 누적) ──
            import plotly.graph_objects as _ge_go
            _ge_kinds = [("project", "📁 프로젝트", "#3b82f6"),
                         ("note", "📝 메모", "#10b981"),
                         ("concept", "🧠 개념", "#8b5cf6"),
                         ("relation", "🔗 관계", "#f59e0b")]
            _ge_line = _ge_go.Figure()
            for _k, _lbl, _col in _ge_kinds:
                _ys = [_cum_count(_k, m) for m in _ge_months]
                _ge_line.add_trace(_ge_go.Scatter(
                    x=_ge_months, y=_ys, mode="lines+markers", name=_lbl,
                    line=dict(color=_col, width=2.5), marker=dict(size=6),
                ))
            # cutoff 세로선
            _ge_line.add_vline(x=_ge_cutoff, line_dash="dash", line_color="#ef4444")
            _ge_line.update_layout(
                height=320, margin=dict(l=20, r=20, t=20, b=20),
                plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
                legend=dict(orientation="h", y=1.12),
                yaxis=dict(title="누적 개수"),
            )
            st.plotly_chart(_ge_line, use_container_width=True)

            # ── 개념 등장 추이 분석 ──
            st.divider()
            st.markdown("#### 🧠 개념 변화")

            # 메모 id → 월 매핑
            _note_month = {}
            for _n in _ge_notes:
                m = _ge_month(_n.get("saved_at") or _n.get("created_at"))
                if m:
                    _note_month[_n.get("id", "")] = m
            # 개념별 월별 등장 횟수 (note_concept_links 기반)
            _concept_month_cnt = {}  # concept → {month: count}
            for _l in _ge_nclinks:
                _cn = _l.get("concept", "")
                _nid = _l.get("note_id", "")
                _lm = _ge_month(_l.get("linked_at")) or _note_month.get(_nid)
                if _cn and _lm:
                    _concept_month_cnt.setdefault(_cn, {}).setdefault(_lm, 0)
                    _concept_month_cnt[_cn][_lm] += 1

            # cutoff 시점 기준: 새로 등장 / 성장 / 사라진(이전엔 있었는데 cutoff 달엔 없음)
            def _cnt_upto(cn, upto):
                return sum(v for mm, v in _concept_month_cnt.get(cn, {}).items() if mm <= upto)

            _ge_c1, _ge_c2, _ge_c3 = st.columns(3)
            with _ge_c1:
                st.markdown("**🆕 이 달 새로 등장**")
                _newc = []
                for _cn, _mm in _concept_month_cnt.items():
                    _first = min(_mm.keys()) if _mm else None
                    if _first == _ge_cutoff:
                        _newc.append((_cn, sum(_mm.values())))
                _newc.sort(key=lambda x: -x[1])
                if _newc:
                    for _cn, _v in _newc[:8]:
                        st.markdown(f"- {_cn}")
                else:
                    st.caption("없음")
            with _ge_c2:
                st.markdown("**📈 성장한 개념**")
                _growc = []
                if _ge_prev:
                    for _cn in _concept_month_cnt:
                        _now_c = _cnt_upto(_cn, _ge_cutoff)
                        _prev_c = _cnt_upto(_cn, _ge_prev)
                        if _now_c > _prev_c and _prev_c > 0:
                            _growc.append((_cn, _prev_c, _now_c))
                _growc.sort(key=lambda x: -(x[2] - x[1]))
                if _growc:
                    for _cn, _pv, _nv in _growc[:8]:
                        st.markdown(f"- {_cn} ({_pv}→{_nv})")
                else:
                    st.caption("없음")
            with _ge_c3:
                st.markdown("**💤 잠잠한 개념**")
                # cutoff 달에는 등장 안 했지만 이전에 있던 개념
                _dormant = []
                for _cn, _mm in _concept_month_cnt.items():
                    _had_before = any(m < _ge_cutoff for m in _mm)
                    _in_now = _ge_cutoff in _mm
                    if _had_before and not _in_now:
                        _dormant.append(_cn)
                if _dormant:
                    for _cn in _dormant[:8]:
                        st.markdown(f"- {_cn}")
                else:
                    st.caption("없음")

            # ── cutoff 시점 네트워크 스냅샷 ──
            st.divider()
            st.markdown(f"#### 🕸️ `{_ge_cutoff}` 시점의 지식맵")
            st.caption("이 달까지 만들어진 노드·관계만 표시해요.")

            # 노드: cutoff까지 생성된 프로젝트/메모/개념
            _ge_nodes = {}
            _ge_edges = []
            def _ge_addn(name, ntype):
                if name and name not in _ge_nodes:
                    _ge_nodes[name] = {"type": ntype, "degree": 0}
            def _ge_adde(s, t, rt="연결"):
                if s and t and s != t and s in _ge_nodes and t in _ge_nodes:
                    _ge_edges.append({"src": s, "tgt": t, "rtype": rt})
                    _ge_nodes[s]["degree"] += 1
                    _ge_nodes[t]["degree"] += 1

            for _p in _ge_projects:
                if (_ge_month(_p.get("created_at")) or "9999") <= _ge_cutoff:
                    _ge_addn(_p.get("name", ""), "project")
            for _n in _ge_notes:
                if (_ge_month(_n.get("saved_at") or _n.get("created_at")) or "9999") <= _ge_cutoff:
                    _ge_addn(_n.get("title", ""), "note")
            for _c in _ge_concepts_raw:
                _cn = _c.get("name") if isinstance(_c, dict) else str(_c)
                _cm = _ge_month(_c.get("created_at")) if isinstance(_c, dict) else None
                # 개념은 created_at 없는 경우도 많아 일단 포함
                if _cm is None or _cm <= _ge_cutoff:
                    _ge_addn(_cn, "concept")
            # 메모-프로젝트 포함
            for _n in _ge_notes:
                if (_ge_month(_n.get("saved_at") or _n.get("created_at")) or "9999") <= _ge_cutoff:
                    _ge_adde(_n.get("project", ""), _n.get("title", ""), "포함")
            # 관계 DB (cutoff까지)
            for _r in _ge_relations:
                if (_ge_month(_r.get("created_at")) or "9999") <= _ge_cutoff:
                    _ge_adde(_r.get("source_name", ""), _r.get("target_name", ""), _r.get("relation_type", "연결"))
            # 메모-개념 링크
            for _l in _ge_nclinks:
                _lm = _ge_month(_l.get("linked_at")) or _note_month.get(_l.get("note_id", ""))
                if _lm and _lm <= _ge_cutoff:
                    _nt = next((nn.get("title", "") for nn in _ge_notes if nn.get("id", "") == _l.get("note_id", "")), "")
                    _ge_adde(_nt, _l.get("concept", ""), "연결")

            if not _ge_nodes:
                st.info("이 시점엔 아직 노드가 없어요.")
            else:
                _gnlist = list(_ge_nodes.keys())
                _gn = len(_gnlist)
                _gpos = {}
                for _gi, _gname in enumerate(_gnlist):
                    _ang = 2 * _ge_math.pi * _gi / max(_gn, 1)
                    _gr = 1.0 + 0.05 * _ge_nodes[_gname]["degree"]
                    _gpos[_gname] = (_gr * _ge_math.cos(_ang), _gr * _ge_math.sin(_ang))
                _gfig = _ge_go.Figure()
                _gex, _gey = [], []
                for _e in _ge_edges:
                    if _e["src"] in _gpos and _e["tgt"] in _gpos:
                        sx, sy = _gpos[_e["src"]]; tx, ty = _gpos[_e["tgt"]]
                        _gex += [sx, tx, None]; _gey += [sy, ty, None]
                _gfig.add_trace(_ge_go.Scatter(x=_gex, y=_gey, mode="lines",
                    line=dict(color="#cbd5e1", width=1), hoverinfo="none", showlegend=False))
                _gcfg = {"project": ("📁", "#3b82f6", 20), "note": ("📝", "#10b981", 15), "concept": ("🧠", "#8b5cf6", 13)}
                _gbt = {}
                for _k, _v in _ge_nodes.items():
                    _gbt.setdefault(_v["type"], []).append(_k)
                for _t, _lst in _gbt.items():
                    _ic, _co, _sz = _gcfg.get(_t, ("⚪", "#64748b", 12))
                    _gfig.add_trace(_ge_go.Scatter(
                        x=[_gpos[n][0] for n in _lst], y=[_gpos[n][1] for n in _lst],
                        mode="markers+text", marker=dict(size=_sz, color=_co, line=dict(color="#fff", width=2)),
                        text=[f"{_ic} {n}" for n in _lst], textposition="top center", textfont=dict(size=9),
                        hoverinfo="text", name=f"{_ic} {_t}",
                    ))
                _gfig.update_layout(height=480, showlegend=True,
                    legend=dict(orientation="h", y=1.08),
                    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                    plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
                    margin=dict(l=20, r=20, t=20, b=20))
                st.plotly_chart(_gfig, use_container_width=True)
                st.caption(f"노드 {len(_ge_nodes)}개 · 연결 {len(_ge_edges)}개")

    # ══════════════════════════════════════════════════════════
    # TAB 10 — 🕰️ 타임라인 (Knowledge Timeline)
    # ══════════════════════════════════════════════════════════
    with tab10:
        st.markdown("### 🕰️ 지식 타임라인")
        st.caption("메모·프로젝트·개념·관계·작업이 만들어진 순서를 한 줄로 모아서 보여줘요.")

        _tl_notes = st.session_state.get("archive_notes", [])
        _tl_projects = st.session_state.get("projects", [])
        _tl_concepts = st.session_state.get("pkm_custom_concepts", [])
        _tl_relations = st.session_state.get("relations", [])
        _tl_tasks = st.session_state.get("tasks", [])

        # ── 이벤트 수집: (datetime_str, kind, icon, title, subtitle) ──
        _tl_events = []
        for _n in _tl_notes:
            _d = _n.get("saved_at") or _n.get("created_at") or ""
            if _d:
                _tl_events.append((_d, "메모", "📝", _n.get("title", "제목 없음"),
                                   _n.get("project", "")))
        for _p in _tl_projects:
            _d = _p.get("created_at") or ""
            if _d:
                _tl_events.append((_d, "프로젝트", "📁", _p.get("name", ""),
                                   _p.get("category", "")))
        for _c in _tl_concepts:
            if isinstance(_c, dict) and _c.get("created_at"):
                _tl_events.append((_c.get("created_at"), "개념", "🧠", _c.get("name", ""),
                                   _c.get("folder", "")))
        for _r in _tl_relations:
            _d = _r.get("created_at") or ""
            if _d:
                _tl_events.append((_d, "관계", "🔗",
                                   f"{_r.get('source_name','')} → {_r.get('target_name','')}",
                                   _r.get("relation_type", "")))
        for _t in _tl_tasks:
            _d = _t.get("created_at") or ""
            if _d:
                _tl_events.append((_d, "작업", "✅", _t.get("title", ""),
                                   _t.get("project", "")))

        if not _tl_events:
            st.info("아직 타임라인에 표시할 기록이 없어요. 메모·프로젝트·개념을 만들어보세요.")
        else:
            # ── 필터 ──
            _tlf1, _tlf2 = st.columns([3, 2])
            with _tlf1:
                _tl_kinds = st.multiselect(
                    "표시할 종류",
                    ["메모", "프로젝트", "개념", "관계", "작업"],
                    default=["메모", "프로젝트", "개념", "관계", "작업"],
                    key="tl_kinds",
                )
            with _tlf2:
                _tl_order = st.radio("정렬", ["최신순", "오래된순"], horizontal=True, key="tl_order")

            _tl_filtered = [e for e in _tl_events if e[1] in _tl_kinds]
            _tl_filtered.sort(key=lambda x: x[0], reverse=(_tl_order == "최신순"))

            st.markdown(f"**총 {len(_tl_filtered)}개 기록**")
            st.divider()

            _tl_kind_color = {
                "메모": "#10b981", "프로젝트": "#3b82f6", "개념": "#8b5cf6",
                "관계": "#f59e0b", "작업": "#ec4899",
            }

            # ── 날짜(일)별 그룹핑 ──
            _tl_by_day = {}
            for _ev in _tl_filtered:
                _day = _ev[0][:10]
                _tl_by_day.setdefault(_day, []).append(_ev)

            _day_keys = sorted(_tl_by_day.keys(), reverse=(_tl_order == "최신순"))
            for _day in _day_keys:
                st.markdown(
                    f'<div style="font-weight:800;color:#1e3a8a;font-size:1.05rem;'
                    f'margin:6px 0 4px;border-left:4px solid #3b82f6;padding-left:10px;">'
                    f'📅 {_day}</div>',
                    unsafe_allow_html=True,
                )
                _day_events = _tl_by_day[_day]
                _day_events.sort(key=lambda x: x[0], reverse=(_tl_order == "최신순"))
                for _d, _kind, _icon, _title, _sub in _day_events:
                    _col = _tl_kind_color.get(_kind, "#64748b")
                    _time = _d[11:16] if len(_d) >= 16 else ""
                    _sub_html = (f' · <span style="color:#94a3b8">{_sub}</span>' if _sub else "")
                    st.markdown(
                        f'<div style="display:flex;align-items:center;gap:10px;'
                        f'padding:7px 12px;margin:3px 0 3px 14px;background:#fff;'
                        f'border-radius:8px;border-left:3px solid {_col};">'
                        f'<span style="font-size:1.1rem">{_icon}</span>'
                        f'<span style="background:{_col};color:#fff;font-size:0.72rem;'
                        f'padding:1px 8px;border-radius:10px;font-weight:700">{_kind}</span>'
                        f'<span style="font-weight:600;flex:1">{_title}</span>'
                        f'{_sub_html}'
                        f'<span style="color:#cbd5e1;font-size:0.8rem">{_time}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            # ── 일자별 활동량 미니 차트 ──
            st.divider()
            st.markdown("#### 📊 일자별 활동량")
            import plotly.graph_objects as _tl_go
            _tl_daycount = {}
            for _ev in _tl_filtered:
                _tl_daycount.setdefault(_ev[0][:10], 0)
                _tl_daycount[_ev[0][:10]] += 1
            _tl_days_sorted = sorted(_tl_daycount.keys())
            _tlfig = _tl_go.Figure(_tl_go.Bar(
                x=_tl_days_sorted, y=[_tl_daycount[d] for d in _tl_days_sorted],
                marker_color="#3b82f6",
            ))
            _tlfig.update_layout(
                height=240, margin=dict(l=20, r=20, t=10, b=20),
                plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
                yaxis=dict(title="활동 수"),
            )
            st.plotly_chart(_tlfig, use_container_width=True)


# ─────────────────────────────────────────
# 📁 프로젝트 페이지
# ─────────────────────────────────────────
def render_project_page():
    import uuid

    st.markdown("## 📁 프로젝트")
    st.caption("프로젝트별로 메모와 작업을 묶어서 관리해요.")

    projects = st.session_state.get("projects", [])

    # ── 상단: 새 프로젝트 추가 ──────────────────────────────
    with st.expander("➕ 새 프로젝트 만들기", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            p_name = st.text_input("프로젝트명 *", key="new_proj_name", placeholder="예: 팀플 1, TrustLens, SQLD")
            p_category = st.selectbox("대분류", ["학교/팀플", "개인개발", "자격증", "취업준비", "리서치", "기타"], key="new_proj_cat")
            p_status = st.selectbox("상태", ["예정", "진행 중", "완료", "보류"], key="new_proj_status")
        with c2:
            p_priority = st.selectbox("우선순위", ["높음", "보통", "낮음"], key="new_proj_priority")
            p_start = st.date_input("시작일", key="new_proj_start", value=None)
            p_due = st.date_input("마감일", key="new_proj_due", value=None)
        p_desc = st.text_input("설명", key="new_proj_desc", placeholder="간단한 설명")

        if st.button("✅ 프로젝트 저장", key="save_new_project", type="primary", use_container_width=True):
            if p_name.strip():
                _pnow = datetime.now().strftime("%Y-%m-%d %H:%M")
                new_proj = {
                    "id": f"project_{uuid.uuid4().hex[:8]}",
                    "user_id": "local_user",
                    "name": p_name.strip(),
                    "description": p_desc.strip(),
                    "category": p_category,
                    "status": p_status,
                    "priority": p_priority,
                    "owner": "채연",
                    "start_date": str(p_start) if p_start else "",
                    "due_date": str(p_due) if p_due else "",
                    "progress": 0,
                    "created_at": _pnow,
                    "updated_at": _pnow,
                    "deleted_at": None,
                }
                projects.append(new_proj)
                st.session_state.projects = projects
                save_persisted_data()
                _flash(f"'{p_name}' 프로젝트를 만들었어요!")
                st.rerun()
            else:
                st.warning("프로젝트명을 입력해주세요.")

    st.divider()

    if not projects:
        st.info("아직 프로젝트가 없어요. 위에서 첫 프로젝트를 만들어보세요.")
        return

    # ── 뷰 선택 ──────────────────────────────────────────────
    view = st.radio("보기 방식", ["📋 테이블", "🗂️ 카드", "📊 보드"], horizontal=True, key="project_view_mode")

    STATUS_COLOR = {"진행 중": "🟢", "예정": "🔵", "완료": "⚫", "보류": "🟡"}
    PRIORITY_COLOR = {"높음": "🔴", "보통": "🟠", "낮음": "⚪"}

    if view == "📋 테이블":
        st.markdown("""
        <style>
        .proj-table { width:100%; border-collapse:collapse; font-size:0.93em; }
        .proj-table th { background:#eef4ff; color:#1f3f91; padding:8px 12px; text-align:left; border-bottom:2px solid #c7d9f5; }
        .proj-table td { padding:8px 12px; border-bottom:1px solid #e7edf7; vertical-align:middle; }
        .proj-table tr:hover td { background:#f5f8ff; }
        </style>""", unsafe_allow_html=True)

        rows = ""
        for p in projects:
            sc = STATUS_COLOR.get(p.get("status",""), "⚪")
            pc = PRIORITY_COLOR.get(p.get("priority",""), "⚪")
            prog = p.get("progress", 0)
            rows += f"""<tr>
                <td><b>{p.get('name','')}</b><br><span style='color:#888;font-size:0.85em'>{p.get('description','')}</span></td>
                <td>{p.get('category','')}</td>
                <td>{sc} {p.get('status','')}</td>
                <td>{p.get('due_date','—')}</td>
                <td>{pc} {p.get('priority','')}</td>
                <td>
                    <div style='background:#e7edf7;border-radius:8px;height:8px;width:100%'>
                        <div style='background:#2f73ff;border-radius:8px;height:8px;width:{prog}%'></div>
                    </div>
                    <span style='font-size:0.8em;color:#888'>{prog}%</span>
                </td>
            </tr>"""
        st.markdown(f"""<table class='proj-table'>
            <thead><tr><th>프로젝트</th><th>대분류</th><th>상태</th><th>마감일</th><th>우선순위</th><th>진행률</th></tr></thead>
            <tbody>{rows}</tbody></table>""", unsafe_allow_html=True)

    elif view == "🗂️ 카드":
        cols = st.columns(3)
        for idx, p in enumerate(projects):
            with cols[idx % 3]:
                sc = STATUS_COLOR.get(p.get("status",""), "⚪")
                prog = p.get("progress", 0)
                _pid = p.get("id","")
                _pedit_key = f"proj_edit_{_pid}"
                _is_pedit = st.session_state.get(_pedit_key, False)
                with st.container(border=True):
                    if _is_pedit:
                        # ── 프로젝트 편집 모드 ──
                        _pe_name = st.text_input("프로젝트명", value=p.get("name",""), key=f"pe_name_{_pid}")
                        _pe_desc = st.text_input("설명", value=p.get("description",""), key=f"pe_desc_{_pid}")
                        _pe_ca, _pe_cb = st.columns(2)
                        _cat_opts = ["학업/연구", "취업/커리어", "프로젝트", "자기계발", "기타"]
                        _cur_cat = p.get("category","기타")
                        with _pe_ca:
                            _pe_cat = st.selectbox("대분류", _cat_opts,
                                index=_cat_opts.index(_cur_cat) if _cur_cat in _cat_opts else 4,
                                key=f"pe_cat_{_pid}")
                            _stat_opts2 = ["예정","진행 중","완료","보류"]
                            _cur_st2 = p.get("status","예정")
                            _pe_stat = st.selectbox("상태", _stat_opts2,
                                index=_stat_opts2.index(_cur_st2) if _cur_st2 in _stat_opts2 else 0,
                                key=f"pe_stat_{_pid}")
                        with _pe_cb:
                            _pri_opts2 = ["높음","보통","낮음"]
                            _cur_pri2 = p.get("priority","보통")
                            _pe_pri = st.selectbox("우선순위", _pri_opts2,
                                index=_pri_opts2.index(_cur_pri2) if _cur_pri2 in _pri_opts2 else 1,
                                key=f"pe_pri_{_pid}")
                            _pe_due = st.date_input("마감일", value=parse_date_for_input(p.get("due_date","")), key=f"pe_due_{_pid}")
                        _pe_prog = st.slider("진행률", 0, 100, prog, 5, key=f"pe_prog_{_pid}")
                        _psv, _pcl, _pdel = st.columns(3)
                        with _psv:
                            if st.button("💾 저장", key=f"pe_save_{_pid}", type="primary", use_container_width=True):
                                p["name"] = _pe_name.strip() or p["name"]
                                p["description"] = _pe_desc.strip()
                                p["category"] = _pe_cat
                                p["status"] = _pe_stat
                                p["priority"] = _pe_pri
                                p["due_date"] = normalize_date_str(_pe_due)
                                p["progress"] = _pe_prog
                                p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                st.session_state[_pedit_key] = False
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                        with _pcl:
                            if st.button("취소", key=f"pe_cancel_{_pid}", use_container_width=True):
                                st.session_state[_pedit_key] = False; st.rerun()
                        with _pdel:
                            if st.button("🗑️ 삭제", key=f"pe_del_{_pid}", use_container_width=True):
                                st.session_state["projects"] = [x for x in projects if x.get("id") != _pid]
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                    else:
                        # ── 보기 모드 ──
                        _ph1, _ph2 = st.columns([4,1])
                        with _ph1:
                            st.markdown(f"**{p.get('name','')}**")
                            st.caption(f"{p.get('category','')} · {sc} {p.get('status','')}")
                        with _ph2:
                            if st.button("✏️", key=f"pe_editbtn_{_pid}", help="프로젝트 수정"):
                                st.session_state[_pedit_key] = True; st.rerun()
                        st.progress(prog / 100, text=f"{prog}%")
                        if p.get("due_date"):
                            st.caption(f"📅 {p['due_date']}")
                        # 연결된 작업 수
                        _ptasks = [t for t in st.session_state.get("tasks",[]) if t.get("project")==p.get("name")]
                        _done = sum(1 for t in _ptasks if t.get("status")=="완료")
                        if _ptasks:
                            st.caption(f"✅ 작업 {len(_ptasks)}개 · 완료 {_done}개")
                        # 진행률 슬라이더
                        new_prog = st.slider("진행률", 0, 100, prog, 5,
                            key=f"proj_prog_{_pid}", label_visibility="collapsed")
                        if new_prog != prog:
                            p["progress"] = new_prog
                            p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                            save_persisted_data()
                        # ── 인라인 작업 빠른 추가 ──
                        _qakey = f"proj_qa_{_pid}"
                        if st.button("➕ 작업 추가", key=f"proj_addtask_{_pid}", use_container_width=True):
                            st.session_state[_qakey] = not st.session_state.get(_qakey, False)
                        if st.session_state.get(_qakey):
                            _qt = st.text_input("작업명", key=f"proj_qt_{_pid}", placeholder="할 일 입력", label_visibility="collapsed")
                            _qs, _qp = st.columns(2)
                            with _qs:
                                _qstatus = st.selectbox("상태", ["시작 전","진행 중","완료","보류"], key=f"proj_qst_{_pid}", label_visibility="collapsed")
                            with _qp:
                                _qpri = st.selectbox("우선순위", ["높음","보통","낮음"], key=f"proj_qpr_{_pid}", label_visibility="collapsed")
                            if st.button("저장", key=f"proj_qsave_{_pid}", type="primary", use_container_width=True):
                                if _qt.strip():
                                    _new_t = {
                                        "id": f"task_{uuid.uuid4().hex[:8]}",
                                        "title": _qt.strip(),
                                        "project": p.get("name",""),
                                        "project_id": _pid,
                                        "status": _qstatus,
                                        "priority": _qpri,
                                        "due_date": "",
                                        "summary": "",
                                        "linked_note_ids": [],
                                        "linked_concepts": [],
                                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    }
                                    st.session_state.setdefault("tasks",[]).append(_new_t)
                                    st.session_state[_qakey] = False
                                    save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

    elif view == "📊 보드":
        statuses = ["예정", "진행 중", "완료", "보류"]
        cols = st.columns(4)
        for _psi, (col, status) in enumerate(zip(cols, statuses)):
            with col:
                sc = STATUS_COLOR.get(status, "⚪")
                group = [p for p in projects if p.get("status") == status]
                st.markdown(f"#### {sc} {status} ({len(group)})")
                for _pbi, p in enumerate(group):
                    _pid2 = p.get("id", f"p_{_pbi}")
                    with st.container(border=True):
                        st.markdown(f"**{p.get('name','')}**")
                        st.caption(f"{p.get('category','')} · {PRIORITY_COLOR.get(p.get('priority',''),'')} {p.get('priority','')}")
                        if p.get("due_date"):
                            st.caption(f"📅 {p['due_date']}")
                        _pl, _pr = st.columns(2)
                        with _pl:
                            if _psi > 0 and st.button("←", key=f"pb_left_{_pid2}", help=f"{statuses[_psi-1]}로"):
                                p["status"] = statuses[_psi - 1]
                                p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                        with _pr:
                            if _psi < len(statuses) - 1 and st.button("→", key=f"pb_right_{_pid2}", help=f"{statuses[_psi+1]}로"):
                                p["status"] = statuses[_psi + 1]
                                p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

    # ── 프로젝트 상세 (클릭한 프로젝트) ────────────────────────
    st.divider()
    proj_names_list = [p["name"] for p in projects]
    sel_proj_name = st.selectbox("🔍 프로젝트 선택", proj_names_list, key="detail_proj_select")
    sel_proj_obj = next((p for p in projects if p["name"] == sel_proj_name), None)

    if sel_proj_obj:
        proj_id = sel_proj_obj["id"]

        # ── E-1. 프로젝트 허브 요약 대시보드 ──────────────────────
        _hub_notes = [n for n in st.session_state.get("archive_notes", []) if n.get("project") == sel_proj_name]
        _hub_analyses = [a for a in st.session_state.get("saved_analyses", []) if a.get("project") == sel_proj_name]
        _hub_tasks = [t for t in st.session_state.get("tasks", []) if t.get("project") == sel_proj_name]
        _hub_done = sum(1 for t in _hub_tasks if t.get("status") == "완료")
        # 개념: 이 프로젝트 메모에 연결된 고유 개념 수
        _hub_note_ids = {n.get("id") for n in _hub_notes if n.get("id")}
        _hub_concepts = {lk.get("concept") for lk in st.session_state.get("note_concept_links", [])
                         if lk.get("note_id") in _hub_note_ids and lk.get("concept")}
        _hub_doc_cnt = len(_hub_notes) + len(_hub_analyses)
        _hub_prog = sel_proj_obj.get("progress", 0) or 0

        # D-Day 계산
        _hub_dday_str = ""
        _hub_due = sel_proj_obj.get("due_date")
        if _hub_due:
            try:
                from datetime import date as _date_cls
                _due_d = datetime.strptime(normalize_date_str(_hub_due), "%Y-%m-%d").date()
                _delta = (_due_d - _date_cls.today()).days
                if _delta > 0:
                    _hub_dday_str = f"D-{_delta}"
                elif _delta == 0:
                    _hub_dday_str = "D-DAY"
                else:
                    _hub_dday_str = f"D+{abs(_delta)}"
            except Exception:
                _hub_dday_str = ""

        with st.container(border=True):
            _hc_top1, _hc_top2 = st.columns([3, 1])
            with _hc_top1:
                st.markdown(f"### 📁 {sel_proj_name}")
                _cat = sel_proj_obj.get("category", "")
                _sts = sel_proj_obj.get("status", "")
                st.caption(f"{_cat}{' · ' if _cat and _sts else ''}{_sts}")
            with _hc_top2:
                if _hub_dday_str:
                    _dd_color = "#dc2626" if _hub_dday_str.startswith("D+") else "#2563eb"
                    st.markdown(
                        f"<div style='text-align:right;'><span style='font-size:1.6em;font-weight:800;color:{_dd_color};'>{_hub_dday_str}</span>"
                        f"<br><span style='font-size:0.8em;color:#64748b;'>📅 {normalize_date_str(_hub_due)}</span></div>",
                        unsafe_allow_html=True)
            st.progress(_hub_prog / 100, text=f"진행률 {_hub_prog}%")
            _hm1, _hm2, _hm3, _hm4 = st.columns(4)
            _hm1.metric("📄 자료", f"{_hub_doc_cnt}개")
            _hm2.metric("🧠 개념", f"{len(_hub_concepts)}개")
            _hm3.metric("✅ 작업", f"{len(_hub_tasks)}개")
            _hm4.metric("🎉 완료", f"{_hub_done}개")
            # 작업-메모/개념 연결 지표
            _hub_task_w_note = sum(1 for t in _hub_tasks if t.get("linked_note_ids"))
            _hub_task_w_con = sum(1 for t in _hub_tasks if t.get("linked_concepts"))
            if _hub_tasks:
                st.caption(
                    f"🔗 메모 연결 작업 {_hub_task_w_note}/{len(_hub_tasks)} · "
                    f"개념 연결 작업 {_hub_task_w_con}/{len(_hub_tasks)}")

        # 탭: 메모/분석결과 | 섹션 | 캘린더 | 타임라인
        dt1, dt2, dt3, dt4, dt5 = st.tabs(["📄 연결된 자료", "📂 섹션 관리", "📅 캘린더", "📊 타임라인", "🗺️ 프로젝트 맵"])

        # 이 프로젝트의 메모 + 분석결과
        _proj_notes = [n for n in st.session_state.get("archive_notes", []) if n.get("project") == sel_proj_name]
        _proj_analyses = [a for a in st.session_state.get("saved_analyses", []) if a.get("project") == sel_proj_name]
        _proj_tasks = [t for t in st.session_state.get("tasks", []) if t.get("project") == sel_proj_name]

        with dt1:
            st.markdown(f"**{sel_proj_name}** 에 연결된 자료")

            # ── 자료 연결 액션 바 ──────────────────────────
            _link_c1, _link_c2 = st.columns(2)
            _proj_link_key = f"dt1_linkpanel_{proj_id}"
            with _link_c1:
                if st.button("➕ 기존 자료 연결", key=f"dt1_linkbtn_{proj_id}", use_container_width=True):
                    st.session_state[_proj_link_key] = not st.session_state.get(_proj_link_key, False)
            with _link_c2:
                if st.button("➕ 새 자료 만들기", key=f"dt1_newdoc_{proj_id}", use_container_width=True):
                    st.query_params["page"] = "home"
                    _flash("새 분석을 시작한 뒤 저장할 때 이 프로젝트를 선택하면 연결돼요.")
                    st.rerun()

            # ── 기존 자료 연결 패널 (체크박스 선택 리스트) ──────────────
            if st.session_state.get(_proj_link_key):
                with st.container(border=True):
                    st.markdown("**📎 기존 자료를 이 프로젝트에 연결**")
                    # 이 프로젝트가 아닌 자료들만 후보로
                    _cand_notes = [n for n in st.session_state.get("archive_notes", [])
                                   if n.get("project") != sel_proj_name]
                    _cand_analyses = [a for a in st.session_state.get("saved_analyses", [])
                                      if a.get("project") != sel_proj_name]
                    # 후보 목록 구성: (uid, kind, title, cur_proj, obj)
                    _cands = []
                    for _ni, _n in enumerate(_cand_notes):
                        _cands.append((f"n{_ni}", "note", "📝 지식 메모",
                                       _n.get("title") or "제목 없음",
                                       _n.get("project") or "미연결", _n))
                    for _ai, _a in enumerate(_cand_analyses):
                        _cands.append((f"a{_ai}", "analysis", "📊 분석 결과",
                                       _a.get("title") or "제목 없음",
                                       _a.get("project") or "미연결", _a))

                    if not _cands:
                        st.caption("연결할 수 있는 다른 자료가 없어요. 새 자료를 만들어보세요.")
                    else:
                        _kind_label = {"note": "지식 메모", "analysis": "분석 결과"}
                        # 왼쪽 70%: 후보 고르기 / 오른쪽 30%: 선택 요약 + 연결
                        _lk_left, _lk_right = st.columns([7, 3])

                        with _lk_left:
                            # 검색 + 유형 필터
                            _fc1, _fc2 = st.columns([2, 1])
                            with _fc1:
                                _link_q = st.text_input("🔍 검색", key=f"dt1_linkq_{proj_id}",
                                                        placeholder="제목으로 검색", label_visibility="collapsed")
                            with _fc2:
                                _link_filter = st.selectbox("유형", ["전체", "지식 메모", "분석 결과"],
                                                            key=f"dt1_linkfilter_{proj_id}", label_visibility="collapsed")
                            _q_low = (_link_q or "").strip().lower()
                            _filtered = [c for c in _cands
                                         if (_link_filter == "전체" or _kind_label[c[1]] == _link_filter)
                                         and (not _q_low or _q_low in c[3].lower())]

                            # 전체 선택 / 해제
                            _ac1, _ac2 = st.columns(2)
                            with _ac1:
                                if st.button(f"☑️ 전체 선택 ({len(_filtered)})", key=f"dt1_linkselall_{proj_id}", use_container_width=True):
                                    for c in _filtered:
                                        st.session_state[f"dt1_linkcb_{proj_id}_{c[0]}"] = True
                                    st.rerun()
                            with _ac2:
                                if st.button("⬜ 선택 해제", key=f"dt1_linkclr_{proj_id}", use_container_width=True):
                                    for c in _cands:
                                        st.session_state[f"dt1_linkcb_{proj_id}_{c[0]}"] = False
                                    st.rerun()

                            if not _filtered:
                                st.caption("검색/필터에 맞는 자료가 없어요.")

                            # 체크박스 리스트 (스크롤 컨테이너)
                            with st.container(height=300, border=False):
                                for _uid, _kind, _kicon, _title, _curp, _obj in _filtered:
                                    st.checkbox(f"{_kicon[:2]} **{_title[:50]}**",
                                                key=f"dt1_linkcb_{proj_id}_{_uid}")
                                    st.caption(f"&nbsp;&nbsp;&nbsp;&nbsp;{_kind_label[_kind]} · {_curp}")

                        # 전체 후보 중 체크된 것 집계 (필터로 안 보여도 유지)
                        _sel_cands = [c for c in _cands
                                      if st.session_state.get(f"dt1_linkcb_{proj_id}_{c[0]}")]
                        _all_selected = [c[5] for c in _sel_cands]

                        with _lk_right:
                            st.markdown(f"**🧺 선택한 자료 {len(_sel_cands)}개**")
                            if not _sel_cands:
                                st.caption("왼쪽에서 연결할 자료를 선택해주세요.")
                            else:
                                for c in _sel_cands[:5]:
                                    st.markdown(f"- {c[2][:2]} {c[3][:24]}")
                                if len(_sel_cands) > 5:
                                    st.caption(f"+{len(_sel_cands) - 5}개 더")
                            st.divider()
                            # 섹션 선택
                            _proj_secs = [s.get("name") for s in st.session_state.get("project_sections", [])
                                          if s.get("project_id") == proj_id and s.get("name")]
                            _sec_opts = ["일반"] + _proj_secs
                            _link_sec = st.selectbox("연결할 섹션", _sec_opts, key=f"dt1_linksec_{proj_id}")
                            st.caption(f"📁 {sel_proj_name}")
                            if st.button(f"🔗 연결 ({len(_all_selected)})", key=f"dt1_linkrun_{proj_id}",
                                         type="primary", use_container_width=True, disabled=not _all_selected):
                                for _obj in _all_selected:
                                    _obj["project"] = sel_proj_name
                                    _obj["project_id"] = proj_id
                                    _obj["section"] = _link_sec
                                for c in _cands:
                                    st.session_state.pop(f"dt1_linkcb_{proj_id}_{c[0]}", None)
                                save_persisted_data()
                                st.session_state[_proj_link_key] = False
                                _flash(f"✅ {len(_all_selected)}개 자료를 '{sel_proj_name}'에 연결했어요.")
                                st.rerun()

            _note_view = st.radio("보기", ["📋 테이블", "🗂️ 카드"], horizontal=True, key="proj_note_view")

            all_proj_items = (
                [{"kind": "지식 메모", "title": n.get("title",""), "date": n.get("saved_at",""),
                  "tags": n.get("tags",[]), "score": n.get("score",0), "section": n.get("section",""),
                  "raw": n} for n in _proj_notes] +
                [{"kind": "분석 결과", "title": a.get("title",""), "date": a.get("saved_at",""),
                  "tags": a.get("tags",[]), "score": a.get("score",0), "section": a.get("content_type",""),
                  "raw": a} for a in _proj_analyses]
            )
            all_proj_items.sort(key=lambda x: x.get("date",""), reverse=True)

            if not all_proj_items:
                st.info("이 프로젝트에 연결된 자료가 없어요. 분석 결과 저장 시 프로젝트를 연결해보세요.")
            elif _note_view == "📋 테이블":
                import pandas as pd
                _df = pd.DataFrame([{
                    "종류": it["kind"], "제목": it["title"][:40],
                    "섹션": it["section"], "점수": it["score"],
                    "태그": " ".join([f"#{t}" for t in it["tags"][:3]]),
                    "저장일": it["date"][:10] if it["date"] else ""
                } for it in all_proj_items])
                st.dataframe(_df, use_container_width=True, hide_index=True)
            else:
                _cols = st.columns(3)
                for i, it in enumerate(all_proj_items):
                    with _cols[i % 3]:
                        with st.container(border=True):
                            _kind_icon = "📝" if it["kind"] == "지식 메모" else "📊"
                            st.markdown(f"{_kind_icon} **{it['title'][:35]}**")
                            st.caption(f"{it['section']} · {it['score']}점 · {it['date'][:10] if it['date'] else ''}")
                            _tag_str = " ".join([f"#{t}" for t in it["tags"][:3]])
                            if _tag_str:
                                st.caption(_tag_str)
                            _unlink_key = f"dt1_unlinkconfirm_{proj_id}_{i}"
                            if st.button("🔗 연결 해제", key=f"dt1_unlink_{proj_id}_{i}",
                                         help="이 프로젝트와의 연결만 해제해요 (자료는 삭제되지 않아요)"):
                                st.session_state[_unlink_key] = True
                            if st.session_state.get(_unlink_key):
                                st.warning("이 자료를 현재 프로젝트에서 연결 해제할까요? 자료 자체는 삭제되지 않습니다.")
                                _ulc1, _ulc2 = st.columns(2)
                                with _ulc1:
                                    if st.button("✅ 연결 해제", key=f"dt1_unlinkok_{proj_id}_{i}", type="primary", use_container_width=True):
                                        _obj = it["raw"]
                                        _obj["project"] = "기본 프로젝트"
                                        _obj["project_id"] = ""
                                        _obj["section"] = ""
                                        st.session_state.pop(_unlink_key, None)
                                        save_persisted_data()
                                        _flash("연결을 해제했어요. 자료는 '기본 프로젝트'로 이동했어요.")
                                        st.rerun()
                                with _ulc2:
                                    if st.button("취소", key=f"dt1_unlinkcancel_{proj_id}_{i}", use_container_width=True):
                                        st.session_state.pop(_unlink_key, None)
                                        st.rerun()

            # ── 작업 목록 + 인라인 추가 ──────────────────────────
            st.divider()
            _task_header_c1, _task_header_c2 = st.columns([3,1])
            with _task_header_c1:
                st.markdown(f"**✅ 연결된 작업 {len(_proj_tasks)}개**")
            with _task_header_c2:
                _proj_dt1_addkey = f"dt1_add_{proj_id}"
                if st.button("➕ 작업 추가", key=f"dt1_addtask_{proj_id}", use_container_width=True):
                    st.session_state[_proj_dt1_addkey] = not st.session_state.get(_proj_dt1_addkey, False)

            if not _proj_tasks:
                st.caption("이 프로젝트에 연결된 작업이 없어요.")
            else:
                _SE = {"시작 전":"⬜","진행 중":"🔄","완료":"✅","보류":"⏸️"}
                _PE = {"높음":"🔴","보통":"🟠","낮음":"⚪"}
                _status_opts2 = ["시작 전","진행 중","완료","보류"]
                for _t in _proj_tasks:
                    if "id" not in _t:
                        _t["id"] = f"task_{_t.get('title','t')[:8]}_{id(_t)}"
                    _se2 = _SE.get(_t.get("status",""),"⬜")
                    _pe2 = _PE.get(_t.get("priority",""),"⚪")
                    _tc1, _tc2, _tc3 = st.columns([4, 1, 1])
                    with _tc1:
                        st.markdown(f"{_se2} **{_t.get('title','')}**")
                        st.caption(f"{_pe2} {_t.get('priority','')} · 📅 {_t.get('due_date','—')}")
                    with _tc2:
                        _cur_s = _t.get("status","시작 전")
                        _smap = {"진행중":"진행 중","시작전":"시작 전","보류중":"보류","완료됨":"완료"}
                        _cur_s = _smap.get(_cur_s, _cur_s)
                        if _cur_s not in _status_opts2: _cur_s = "시작 전"
                        _ns = st.selectbox("", _status_opts2, index=_status_opts2.index(_cur_s),
                            key=f"dt1_tstatus_{_t['id']}", label_visibility="collapsed")
                        if _ns != _t.get("status"):
                            _t["status"] = _ns
                            _t["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                            save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                    with _tc3:
                        if st.button("🗑️", key=f"dt1_tdel_{_t['id']}", help="삭제"):
                            st.session_state["tasks"] = [x for x in st.session_state.get("tasks",[]) if x.get("id") != _t["id"]]
                            save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

            # 인라인 추가 폼
            if st.session_state.get(_proj_dt1_addkey):
                with st.container(border=True):
                    st.markdown("**새 작업 추가**")
                    _dt1_title = st.text_input("작업명 *", key=f"dt1_qtitle_{proj_id}", placeholder="할 일을 입력하세요")
                    _dt1_c1, _dt1_c2, _dt1_c3 = st.columns(3)
                    with _dt1_c1:
                        _dt1_status = st.selectbox("상태", ["시작 전","진행 중","완료","보류"], key=f"dt1_qst_{proj_id}")
                    with _dt1_c2:
                        _dt1_pri = st.selectbox("우선순위", ["높음","보통","낮음"], index=1, key=f"dt1_qpr_{proj_id}")
                    with _dt1_c3:
                        _dt1_due = st.date_input("마감일", value=None, key=f"dt1_qdue_{proj_id}")
                    _dt1_sv, _dt1_cl = st.columns(2)
                    with _dt1_sv:
                        if st.button("저장", key=f"dt1_qsave_{proj_id}", type="primary", use_container_width=True):
                            if _dt1_title.strip():
                                import uuid as _uuid2
                                _nt = {
                                    "id": f"task_{_uuid2.uuid4().hex[:8]}",
                                    "title": _dt1_title.strip(),
                                    "project": sel_proj_name,
                                    "project_id": proj_id,
                                    "status": _dt1_status,
                                    "priority": _dt1_pri,
                                    "due_date": normalize_date_str(_dt1_due),
                                    "summary": "",
                                    "linked_note_ids": [],
                                    "linked_concepts": [],
                                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                }
                                st.session_state.setdefault("tasks",[]).append(_nt)
                                st.session_state[_proj_dt1_addkey] = False
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                            else:
                                st.warning("작업명을 입력해주세요.")
                    with _dt1_cl:
                        if st.button("취소", key=f"dt1_qcancel_{proj_id}", use_container_width=True):
                            st.session_state[_proj_dt1_addkey] = False
                            st.rerun()

        with dt2:
            st.markdown("### 📂 섹션 · 단계 관리")
            sections = [s for s in st.session_state.get("project_sections", []) if s.get("project_id") == proj_id]
            all_steps_list = st.session_state.get("project_steps", [])
            _proj_notes_all = st.session_state.get("archive_notes", [])

            if not sections:
                st.info("아직 섹션이 없어요. 아래에서 섹션을 만들어보세요.")
            else:
                for sec in sections:
                    steps = [stp for stp in all_steps_list if stp.get("section_id") == sec["id"]]
                    _sec_notes = [n for n in _proj_notes_all
                                  if n.get("project") == sel_proj_name and n.get("section") == sec["name"]]
                    _sec_note_cnt = len(_sec_notes)
                    with st.expander(
                        f"📂 **{sec['name']}** — 단계 {len(steps)}개 · 메모 {_sec_note_cnt}개",
                        expanded=True
                    ):
                        # ── 섹션 이름 변경 / 삭제 ──────────────────
                        _sech1, _sech2, _sech3 = st.columns([3, 1, 1])
                        with _sech1:
                            _sec_rename = st.text_input(
                                "섹션 이름", value=sec["name"], key=f"sec_rename_{sec['id']}",
                                label_visibility="collapsed")
                        with _sech2:
                            if st.button("✏️ 이름 변경", key=f"sec_rename_btn_{sec['id']}", use_container_width=True):
                                _new_name = _sec_rename.strip()
                                if _new_name and _new_name != sec["name"]:
                                    _old_name = sec["name"]
                                    sec["name"] = _new_name
                                    # 이 섹션에 속한 메모/분석결과의 section 값도 갱신
                                    for _n in st.session_state.get("archive_notes", []):
                                        if _n.get("project") == sel_proj_name and _n.get("section") == _old_name:
                                            _n["section"] = _new_name
                                    for _a in st.session_state.get("saved_analyses", []):
                                        if _a.get("project") == sel_proj_name and _a.get("section") == _old_name:
                                            _a["section"] = _new_name
                                    save_persisted_data()
                                    _flash(f"섹션 이름을 '{_new_name}'(으)로 바꿨어요.")
                                    st.rerun()
                        with _sech3:
                            _secdel_key = f"sec_delconfirm_{sec['id']}"
                            if st.button("🗑️ 삭제", key=f"sec_del_btn_{sec['id']}", use_container_width=True):
                                st.session_state[_secdel_key] = True
                            if st.session_state.get(_secdel_key):
                                st.warning("섹션을 삭제할까요? 안의 단계도 함께 삭제되고, 연결된 자료는 '일반'으로 이동해요. (자료 자체는 삭제되지 않아요)")
                                if st.button("✅ 삭제 확정", key=f"sec_delok_{sec['id']}", type="primary"):
                                    # 단계 삭제
                                    st.session_state["project_steps"] = [
                                        s for s in all_steps_list if s.get("section_id") != sec["id"]]
                                    # 자료 섹션 → 일반
                                    for _n in st.session_state.get("archive_notes", []):
                                        if _n.get("project") == sel_proj_name and _n.get("section") == sec["name"]:
                                            _n["section"] = "일반"
                                    for _a in st.session_state.get("saved_analyses", []):
                                        if _a.get("project") == sel_proj_name and _a.get("section") == sec["name"]:
                                            _a["section"] = "일반"
                                    # 섹션 삭제
                                    st.session_state["project_sections"] = [
                                        s for s in st.session_state.get("project_sections", []) if s.get("id") != sec["id"]]
                                    st.session_state.pop(_secdel_key, None)
                                    save_persisted_data()
                                    _flash(f"'{sec['name']}' 섹션을 삭제했어요.", icon="🗑️")
                                    st.rerun()
                                if st.button("취소", key=f"sec_delcancel_{sec['id']}"):
                                    st.session_state.pop(_secdel_key, None)
                                    st.rerun()
                        st.divider()
                        # 단계별 메모 트리
                        if steps:
                            for stp in steps:
                                _step_notes = [n for n in _sec_notes if n.get("step") == stp["name"]]
                                st.markdown(f"🔖 **{stp['name']}** ({len(_step_notes)}개 메모)")
                                for n in _step_notes:
                                    _score_badge = f"**{n.get('score',0)}점**" if n.get("score") else ""
                                    st.markdown(
                                        f"  &nbsp;&nbsp;&nbsp;📝 {n.get('title','')[:45]} "
                                        f"{_score_badge} · {n.get('saved_at','')[:10]}"
                                    )
                                if not _step_notes:
                                    st.caption("  &nbsp;&nbsp;&nbsp;(메모 없음)")
                        # 단계 없음 메모
                        _no_step_notes = [n for n in _sec_notes if not n.get("step") or n.get("step") == "없음"]
                        if _no_step_notes:
                            st.markdown(f"📌 **단계 미분류** ({len(_no_step_notes)}개)")
                            for n in _no_step_notes:
                                st.markdown(f"  &nbsp;&nbsp;&nbsp;📝 {n.get('title','')[:45]} · {n.get('saved_at','')[:10]}")
                        # 단계 추가
                        _step_col1, _step_col2 = st.columns([3, 1])
                        with _step_col1:
                            _new_step_name = st.text_input(
                                "단계명", key=f"new_step_{sec['id']}", placeholder="예: 1차 자료조사, 초안 작성"
                            )
                        with _step_col2:
                            st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
                            if st.button("➕ 단계 추가", key=f"add_step_{sec['id']}"):
                                if _new_step_name.strip():
                                    new_stp = {
                                        "id": f"step_{uuid.uuid4().hex[:8]}",
                                        "section_id": sec["id"],
                                        "name": _new_step_name.strip(),
                                        "order": len(steps) + 1,
                                        "status": "시작 전",
                                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    }
                                    all_steps_list.append(new_stp)
                                    st.session_state.project_steps = all_steps_list
                                    save_persisted_data()
                                    _flash(f"'{_new_step_name}' 단계를 추가했어요!")
                                    st.rerun()
                        # 단계 삭제
                        if steps:
                            _del_step = st.selectbox(
                                "단계 삭제", ["선택"] + [s["name"] for s in steps],
                                key=f"del_step_{sec['id']}"
                            )
                            if _del_step != "선택" and st.button("🗑️ 삭제", key=f"del_step_btn_{sec['id']}"):
                                st.session_state.project_steps = [
                                    s for s in all_steps_list if not (s.get("section_id") == sec["id"] and s["name"] == _del_step)
                                ]
                                save_persisted_data()
                                _flash(f"'{_del_step}' 단계를 삭제했어요", icon="🗑️")
                                st.rerun()

                        # ── 이 섹션에 자료 바로 연결 ──────────────────
                        _seclink_panel = f"seclink_panel_{sec['id']}"
                        if st.button("📎 이 섹션에 자료 연결", key=f"seclink_toggle_{sec['id']}", use_container_width=True):
                            st.session_state[_seclink_panel] = not st.session_state.get(_seclink_panel, False)
                        if st.session_state.get(_seclink_panel):
                          with st.container(border=True):
                            _seclink_cands = []
                            for _ni, _n in enumerate(st.session_state.get("archive_notes", [])):
                                if not (_n.get("project") == sel_proj_name and _n.get("section") == sec["name"]):
                                    _seclink_cands.append((f"n{_ni}", "📝", _n.get("title") or "제목 없음",
                                                           _n.get("project") or "미연결", _n))
                            for _ai, _a in enumerate(st.session_state.get("saved_analyses", [])):
                                if not (_a.get("project") == sel_proj_name and _a.get("section") == sec["name"]):
                                    _seclink_cands.append((f"a{_ai}", "📊", _a.get("title") or "제목 없음",
                                                           _a.get("project") or "미연결", _a))
                            if not _seclink_cands:
                                st.caption("이 섹션에 연결할 다른 자료가 없어요.")
                            else:
                                _slq = st.text_input("🔍 검색", key=f"seclink_q_{sec['id']}",
                                                     placeholder="제목으로 검색", label_visibility="collapsed")
                                _slq_low = (_slq or "").strip().lower()
                                _sl_filtered = [c for c in _seclink_cands if not _slq_low or _slq_low in c[2].lower()]
                                with st.container(height=220, border=False):
                                    for _uid, _icon, _title, _curp, _obj in _sl_filtered:
                                        st.checkbox(f"{_icon} **{_title[:45]}**  ·  {_curp}",
                                                    key=f"seclink_cb_{sec['id']}_{_uid}")
                                _sl_selected = [c[4] for c in _seclink_cands
                                                if st.session_state.get(f"seclink_cb_{sec['id']}_{c[0]}")]
                                if st.button(f"🔗 이 섹션에 연결 ({len(_sl_selected)})", key=f"seclink_run_{sec['id']}",
                                             type="primary", use_container_width=True, disabled=not _sl_selected):
                                    for _obj in _sl_selected:
                                        _obj["project"] = sel_proj_name
                                        _obj["project_id"] = proj_id
                                        _obj["section"] = sec["name"]
                                    for c in _seclink_cands:
                                        st.session_state.pop(f"seclink_cb_{sec['id']}_{c[0]}", None)
                                    save_persisted_data()
                                    _flash(f"✅ {len(_sl_selected)}개 자료를 '{sec['name']}' 섹션에 연결했어요.")
                                    st.rerun()

            st.divider()
            with st.expander("➕ 섹션 추가"):
                sec_name = st.text_input("섹션명", key="new_section_name", placeholder="예: 자료조사, 발표대본")
                if st.button("섹션 저장", key="save_new_section"):
                    if sec_name.strip():
                        new_sec = {
                            "id": f"section_{uuid.uuid4().hex[:8]}",
                            "project_id": proj_id,
                            "name": sec_name.strip(),
                            "order": len(sections) + 1,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        }
                        secs = st.session_state.get("project_sections", [])
                        secs.append(new_sec)
                        st.session_state.project_sections = secs
                        save_persisted_data()
                        _flash(f"'{sec_name}' 섹션을 추가했어요!")
                        st.rerun()

        with dt3:
            import calendar as _cal_mod
            from datetime import date as _cal_date

            # ── 이벤트 수집: {YYYY-MM-DD: [(icon, title, type)]} ──
            _events = {}
            def _add_event(_raw_date, _icon, _title, _etype, _ref=None):
                _d = normalize_date_str(_raw_date)
                if not _d:
                    return
                _events.setdefault(_d, []).append((_icon, _title, _etype, _ref))
            # 프로젝트 시작/마감
            if sel_proj_obj.get("created_at"):
                _add_event(sel_proj_obj["created_at"][:10], "📁", f"{sel_proj_name} 시작", "project", ("project", sel_proj_name))
            if sel_proj_obj.get("due_date"):
                _add_event(sel_proj_obj["due_date"], "🏁", f"{sel_proj_name} 마감", "project", ("project", sel_proj_name))
            # 작업 마감일
            for _t in _proj_tasks:
                if _t.get("due_date"):
                    _add_event(_t["due_date"], "✅", _t.get("title", "작업"), "task", None)
            # 메모 / 연구노트 저장일
            for _n in _proj_notes:
                if _n.get("saved_at"):
                    _is_research = ("연구노트" in str(_n.get("title", ""))
                                    or "연구노트" in [str(x) for x in _n.get("tags", [])])
                    _add_event(_n["saved_at"][:10], "🔬" if _is_research else "📝",
                               _n.get("title", "메모"), "research" if _is_research else "note",
                               ("note", _n.get("id")))
            # 분석결과 저장일
            for _a in _proj_analyses:
                if _a.get("saved_at"):
                    _add_event(_a["saved_at"][:10], "📊", _a.get("title", "분석결과"), "analysis", None)

            # ── 월 상태 ──
            _ym_key = f"cal_ym_{proj_id}"
            if _ym_key not in st.session_state:
                _today = _cal_date.today()
                st.session_state[_ym_key] = (_today.year, _today.month)
            _cy, _cm = st.session_state[_ym_key]

            # ── 헤더 + 월 이동 ──
            _nav1, _nav2, _nav3, _nav4 = st.columns([1, 1, 3, 1])
            with _nav1:
                if st.button("◀ 이전", key=f"cal_prev_{proj_id}", use_container_width=True):
                    st.session_state[_ym_key] = (_cy - 1, 12) if _cm == 1 else (_cy, _cm - 1)
                    st.rerun()
            with _nav2:
                if st.button("이번 달", key=f"cal_today_{proj_id}", use_container_width=True):
                    _t = _cal_date.today()
                    st.session_state[_ym_key] = (_t.year, _t.month)
                    st.rerun()
            with _nav3:
                st.markdown(f"<h3 style='text-align:center;margin:0'>📅 {_cy}년 {_cm}월</h3>", unsafe_allow_html=True)
            with _nav4:
                if st.button("다음 ▶", key=f"cal_next_{proj_id}", use_container_width=True):
                    st.session_state[_ym_key] = (_cy + 1, 1) if _cm == 12 else (_cy, _cm + 1)
                    st.rerun()

            # ── 유형 메타 (아이콘/라벨/색/순서) ──
            _TYPE_META = {
                "project":  ("📁", "프로젝트", "#6366f1"),
                "task":     ("✅", "작업",     "#16a34a"),
                "note":     ("📝", "메모",     "#f59e0b"),
                "research": ("🔬", "연구노트", "#0ea5e9"),
                "analysis": ("📊", "분석결과", "#8b5cf6"),
            }
            _TYPE_ORDER = ["project", "task", "note", "research", "analysis"]
            # 캘린더 셀 표시 우선순위 (메모/노트가 가장 중요 — '그날 무슨 생각을 했나')
            _CAL_PRIORITY = {"note": 0, "research": 1, "task": 2, "project": 3, "analysis": 4}

            # ── 이번 달 KPI 요약 ──
            _month_prefix = f"{_cy:04d}-{_cm:02d}-"
            _month_counts = {k: 0 for k in _TYPE_META}
            for _dk, _evlist in _events.items():
                if _dk.startswith(_month_prefix):
                    for _icon, _t2, _ty, _ref in _evlist:
                        if _ty in _month_counts:
                            _month_counts[_ty] += 1
            _kpi_cols = st.columns(len(_TYPE_ORDER))
            for _ki, _ty in enumerate(_TYPE_ORDER):
                _ic, _lb, _ = _TYPE_META[_ty]
                _kpi_cols[_ki].metric(f"{_ic} {_lb}", f"{_month_counts[_ty]}개")

            # ── 요일 헤더 ──
            _wd_cols = st.columns(7)
            for _i, _wd in enumerate(["월", "화", "수", "목", "금", "토", "일"]):
                _wd_color = "#dc2626" if _wd == "일" else "#2563eb" if _wd == "토" else "#475569"
                _wd_cols[_i].markdown(f"<div style='text-align:center;font-weight:700;color:{_wd_color}'>{_wd}</div>", unsafe_allow_html=True)

            # ── 달력 그리드 (카드형 셀) ──
            _today_str = _cal_date.today().strftime("%Y-%m-%d")
            _cal_obj = _cal_mod.Calendar(firstweekday=0)  # 월요일 시작
            _sel_date_key = f"cal_seldate_{proj_id}"
            _sel_d = st.session_state.get(_sel_date_key)
            _CARD_H = 108
            for _week in _cal_obj.monthdayscalendar(_cy, _cm):
                _day_cols = st.columns(7, gap="small")
                for _di, _day in enumerate(_week):
                    with _day_cols[_di]:
                        if _day == 0:
                            st.markdown(
                                f"<div style='min-height:{_CARD_H}px'></div>",
                                unsafe_allow_html=True)
                            st.markdown("<div style='height:38px'></div>", unsafe_allow_html=True)
                            continue
                        _dstr = f"{_cy:04d}-{_cm:02d}-{_day:02d}"
                        _evs = _events.get(_dstr, [])
                        _is_today = (_dstr == _today_str)
                        _is_sel = (_dstr == _sel_d)
                        # 유형별 개수 집계 (순서 고정)
                        _tcnt = {}
                        for _icon, _t2, _ty, _ref in _evs:
                            _tcnt[_ty] = _tcnt.get(_ty, 0) + 1
                        # 날짜 숫자 (HTML — 마크다운 ** 누출 방지)
                        if _is_today:
                            _num_html = (f"<span style='display:inline-block;min-width:22px;height:22px;line-height:22px;"
                                         f"text-align:center;background:#2563eb;color:#fff;border-radius:50%;"
                                         f"font-weight:700;font-size:0.82rem'>{_day}</span>")
                        else:
                            _dow_color = "#dc2626" if _di == 6 else "#2563eb" if _di == 5 else "#0f172a"
                            _num_html = f"<span style='font-weight:700;font-size:0.85rem;color:{_dow_color}'>{_day}</span>"
                        # 제목 미리보기 (숫자 대신 제목 — 캘린더를 '기억 지도'로)
                        def _esc(_s):
                            return str(_s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        # 우선순위 정렬 (메모 > 연구 > 작업 > 프로젝트 > 분석)
                        _evs_sorted = sorted(_evs, key=lambda e: _CAL_PRIORITY.get(e[2], 9))
                        _PREVIEW_N = 3
                        _title_html = ""
                        for _icon, _t2, _ty, _ref in _evs_sorted[:_PREVIEW_N]:
                            _col = _TYPE_META.get(_ty, ("", "", "#64748b"))[2]
                            _name = str(_t2).strip() or _TYPE_META.get(_ty, ("", "항목", ""))[1]
                            _tt = (_name[:9] + "…") if len(_name) > 10 else _name
                            _title_html += (f"<div style='font-size:0.68rem;color:{_col};white-space:nowrap;"
                                            f"overflow:hidden;text-overflow:ellipsis;max-width:100%'>{_icon} {_esc(_tt)}</div>")
                        if len(_evs) > _PREVIEW_N:
                            _title_html += f"<div style='font-size:0.66rem;color:#94a3b8'>+{len(_evs) - _PREVIEW_N}</div>"
                        # 총 개수 보조 배지 (날짜 옆)
                        _cnt_badge = (f"<span style='font-size:0.62rem;color:#94a3b8;margin-left:4px'>·{len(_evs)}</span>"
                                      if _evs else "")
                        _border = "2px solid #2563eb" if _is_sel else "1px solid #e2e8f0"
                        _bg = "#eff6ff" if _is_today else "#ffffff"
                        st.markdown(
                            f"<div style='border:{_border};border-radius:10px;background:{_bg};"
                            f"padding:6px 7px;min-height:{_CARD_H}px;overflow:hidden;'>"
                            f"<div style='margin-bottom:3px'>{_num_html}{_cnt_badge}</div>"
                            f"<div style='line-height:1.45'>{_title_html or '&nbsp;'}</div>"
                            f"</div>",
                            unsafe_allow_html=True)
                        if _evs:
                            if st.button(f"열기 ·{len(_evs)}", key=f"cal_day_{proj_id}_{_dstr}", use_container_width=True):
                                st.session_state[_sel_date_key] = _dstr
                                st.rerun()
                        else:
                            st.markdown("<div style='height:38px'></div>", unsafe_allow_html=True)

            # ── 선택 날짜 상세 (탐색 허브: 제목 클릭 이동 + 개념칩) ──
            if _sel_d and _sel_d in _events:
                st.divider()
                _sevs = _events[_sel_d]
                st.markdown(f"#### 📌 {_sel_d} · 일정 {len(_sevs)}개")
                _notes_by_id_cal = {n.get("id"): n for n in st.session_state.get("archive_notes", [])}

                def _cal_render_note(_i2, _t2, _rid, _gi, _indent="&nbsp;&nbsp;&nbsp;&nbsp;"):
                    _bc1, _bc2 = st.columns([5, 1])
                    with _bc1:
                        st.markdown(f"{_indent}└ {_i2} {_t2}", unsafe_allow_html=True)
                        _ncs = [c for c in (_notes_by_id_cal.get(_rid, {}).get("concepts", []) or []) if c]
                        if _ncs:
                            st.markdown(_indent + "&nbsp;&nbsp;&nbsp;개념: " + " ".join(f"`{c}`" for c in _ncs[:6]),
                                        unsafe_allow_html=True)
                    with _bc2:
                        if _rid and st.button("열기", key=f"cal_open_note_{_rid}_{_gi}", use_container_width=True):
                            st.session_state["archive_open_note_id"] = _rid
                            st.query_params["page"] = "archive"
                            st.rerun()

                # 계층: 📁 프로젝트 → (시작/마감 · 작업 · 메모 · 분석)  — expander 미사용, 들여쓰기/카드
                with st.container(border=True):
                    _ph1, _ph2 = st.columns([5, 1])
                    with _ph1:
                        st.markdown(f"**📁 {sel_proj_name}**")
                    with _ph2:
                        if st.button("프로젝트", key=f"cal_proj_hdr_{_sel_d}", use_container_width=True):
                            st.session_state["ep_jump_entity"] = sel_proj_name
                            st.query_params["page"] = "projects"
                            st.rerun()
                    # 프로젝트 시작/마감
                    for _i2, _t2, _yt, _ref2 in _sevs:
                        if _yt == "project":
                            st.markdown(f"&nbsp;&nbsp;└ {_i2} {_t2}", unsafe_allow_html=True)
                    # ✅ 작업
                    _task_evs = [(_i2, _t2) for _i2, _t2, _yt, _ref2 in _sevs if _yt == "task"]
                    if _task_evs:
                        st.markdown(f"&nbsp;&nbsp;**✅ 작업 ({len(_task_evs)})**", unsafe_allow_html=True)
                        for _i2, _t2 in _task_evs:
                            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;└ {_i2} {_t2}", unsafe_allow_html=True)
                    # 📝 메모 / 🔬 연구노트
                    _memo_evs = [(_i2, _t2, _ref2) for _i2, _t2, _yt, _ref2 in _sevs if _yt in ("note", "research")]
                    if _memo_evs:
                        st.markdown(f"&nbsp;&nbsp;**📝 메모 ({len(_memo_evs)})**", unsafe_allow_html=True)
                        for _gi, (_i2, _t2, _ref2) in enumerate(_memo_evs):
                            _cal_render_note(_i2, _t2, (_ref2[1] if _ref2 else None), _gi)
                    # 📊 분석결과
                    _ana_evs = [(_i2, _t2) for _i2, _t2, _yt, _ref2 in _sevs if _yt == "analysis"]
                    if _ana_evs:
                        st.markdown(f"&nbsp;&nbsp;**📊 분석결과 ({len(_ana_evs)})**", unsafe_allow_html=True)
                        for _i2, _t2 in _ana_evs:
                            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;└ {_i2} {_t2}", unsafe_allow_html=True)
            elif _events:
                st.caption("📅 날짜 아래 **열기** 버튼을 누르면 그 날의 상세 일정을 볼 수 있어요.")

            # ── 기존 리스트 보기 (마감일/저장일 순) ──
            if _events:
                st.divider()
                with st.expander("📋 리스트로 보기", expanded=False):
                    for _dk in sorted(_events.keys()):
                        st.markdown(f"**📅 {_dk}**")
                        for _icon, _t2, _ty, _ref in _events[_dk]:
                            st.markdown(f"&nbsp;&nbsp;{_icon} {_t2}")
            else:
                st.info("이 프로젝트에 표시할 일정(작업 마감일·저장일 등)이 없어요.")

        with dt4:
            st.markdown("### 📊 타임라인")
            _timeline_items = (
                [{"title": t["title"], "date": t.get("due_date",""), "kind": "작업", "status": t.get("status","")} for t in _proj_tasks if t.get("due_date")] +
                [{"title": n.get("title",""), "date": n.get("saved_at","")[:10], "kind": "메모", "status": ""} for n in _proj_notes if n.get("saved_at")]
            )
            _timeline_items.sort(key=lambda x: x["date"])

            if not _timeline_items:
                st.info("타임라인에 표시할 항목이 없어요.")
            else:
                try:
                    import plotly.express as _px2
                    import pandas as _pd2
                    _tdf = _pd2.DataFrame(_timeline_items)
                    _tdf["날짜"] = _pd2.to_datetime(_tdf["date"], errors="coerce")
                    _tdf = _tdf.dropna(subset=["날짜"])
                    _tdf["완료"] = _tdf["날짜"] + _pd2.Timedelta(hours=2)
                    if not _tdf.empty:
                        _tl_fig = _px2.timeline(
                            _tdf, x_start="날짜", x_end="완료",
                            y="title", color="kind",
                            hover_name="title", hover_data={"status": True},
                            color_discrete_map={"작업": "#3b82f6", "메모": "#10b981"},
                            height=max(300, len(_tdf) * 35),
                        )
                        _tl_fig.update_layout(
                            showlegend=True,
                            xaxis_title="날짜",
                            yaxis_title="",
                            plot_bgcolor="#f8fbff",
                            paper_bgcolor="#f8fbff",
                        )
                        st.plotly_chart(_tl_fig, use_container_width=True)
                    else:
                        st.info("날짜 파싱 가능한 항목이 없어요.")
                except Exception as e:
                    st.warning(f"타임라인 그리기 오류: {e}")

        with dt5:
            # ── E-3. 프로젝트 맵 (MVP: 트리/카드형 관계맵) ──────────────
            st.markdown("### 🗺️ 프로젝트 맵")
            st.caption("프로젝트를 중심으로 작업·자료·개념·관계가 어떻게 이어져 있는지 한 화면에서 봐요.")

            from collections import defaultdict as _ddict
            _today_map = datetime.now()

            def _recency_w(ds, half=30):
                """최근성 가중치 0~1 (1=오늘). 날짜 불명 → 0.5 중립."""
                if not ds:
                    return 0.5
                try:
                    _d = datetime.strptime(str(ds)[:10], "%Y-%m-%d")
                    _days = max(0.0, (_today_map - _d).total_seconds() / 86400.0)
                    return 0.5 ** (_days / max(1, half))
                except Exception:
                    return 0.5

            def _recency_badge(rec):
                if rec >= 0.65:
                    return ("🔴 최근", "#fee2e2", "#b91c1c")
                if rec >= 0.4:
                    return ("🟡 보통", "#fef3c7", "#b45309")
                return ("⚪ 오래됨", "#e2e8f0", "#475569")

            # 프로젝트 범위 개념 빈도 + 최근성 (메모 concepts + 링크 + 작업 개념)
            _map_freq, _map_w = _ddict(int), _ddict(float)
            _pn_ids = {n.get("id") for n in _proj_notes if n.get("id")}
            for _mn in _proj_notes:
                for _mc in (_mn.get("concepts", []) or []):
                    _mc = canonical_concept(_mc)
                    if _mc:
                        _map_freq[_mc] += 1
                        _map_w[_mc] += _recency_w(_mn.get("saved_at") or _mn.get("created_at"))
            for _lk in st.session_state.get("note_concept_links", []):
                if _lk.get("note_id") in _pn_ids and _lk.get("concept"):
                    _lc = canonical_concept(_lk["concept"])
                    if _lc:
                        _map_freq[_lc] += 1
                        _map_w[_lc] += _recency_w(_lk.get("linked_at"))
            for _mt in _proj_tasks:
                for _mc in (_mt.get("linked_concepts", []) or []):
                    _mc = canonical_concept(_mc)
                    if _mc:
                        _map_freq[_mc] += 1
                        _map_w[_mc] += _recency_w(_mt.get("updated_at") or _mt.get("created_at"))
            # importance = frequency × recency (concept_importance와 동일 규칙)
            _map_rows = []
            for _c, _f in _map_freq.items():
                _rec = _map_w[_c] / _f if _f else 0.0
                _map_rows.append((_c, {"frequency": _f, "recency": round(_rec, 4),
                                       "importance": round(_f * _rec, 4)}))
            _map_concepts = sorted(_map_rows, key=lambda kv: kv[1]["frequency"], reverse=True)

            # 연결 관계 수: 작업→메모 + 작업→개념 + 메모→개념(링크)
            _rel_task_note = sum(len(t.get("linked_note_ids", []) or []) for t in _proj_tasks)
            _rel_task_con = sum(len(t.get("linked_concepts", []) or []) for t in _proj_tasks)
            _rel_note_con = sum(1 for _lk in st.session_state.get("note_concept_links", [])
                                if _lk.get("note_id") in _pn_ids and _lk.get("concept"))
            _rel_total = _rel_task_note + _rel_task_con + _rel_note_con

            # ── 상단 요약 (프로젝트 = 중심 노드) ──
            _doc_n = len(_proj_notes) + len(_proj_analyses)
            _linked_note_ids = set()
            for _mt in _proj_tasks:
                _linked_note_ids |= set(_mt.get("linked_note_ids", []) or [])
            st.markdown(
                f"<div style='text-align:center;padding:12px;border:2px solid #6366f1;border-radius:12px;background:#eef2ff;margin-bottom:10px'>"
                f"<span style='font-size:1.25em;font-weight:800;color:#4338ca'>📁 {sel_proj_name}</span></div>",
                unsafe_allow_html=True)
            _sm1, _sm2, _sm3, _sm4 = st.columns(4)
            _sm1.metric("✅ 작업", f"{len(_proj_tasks)}개")
            _sm2.metric("📄 연결 메모", f"{len(_proj_notes)}개")
            _sm3.metric("🧠 연결 개념", f"{len(_map_concepts)}개")
            _sm4.metric("🔗 연결 관계", f"{_rel_total}개")

            if not _proj_tasks and _doc_n == 0 and not _map_concepts:
                st.info("아직 작업·메모·개념이 연결되지 않았어요. 작업을 만들고 **작업 수정**에서 관련 메모와 개념을 연결해보세요.")
            else:
                # 🧠 핵심 개념 (빈도 기준 / 중요도(TF-IDF) 기준 토글)
                st.markdown("#### 🧠 핵심 개념 Top N")
                if _map_concepts:
                    _rank_opts = ["빈도순", "중요도순 (TF-IDF)"] if get_setting("feat_tfidf") else ["빈도순"]
                    _rank_mode = st.radio(
                        "정렬 기준", _rank_opts,
                        horizontal=True, key=f"map_rank_{sel_proj_name}",
                        help="빈도순: 자주 등장한 개념 / 중요도순: 전체에선 흔치 않지만 이 프로젝트에 특화된 개념")

                    if _rank_mode.startswith("중요도"):
                        # 이 프로젝트 메모를 문서로, IDF는 전체 메모 기준
                        _tfidf_rows = concept_tfidf(
                            note_filter=lambda n: n.get("project") == sel_proj_name, top_n=30)
                        if _tfidf_rows:
                            _mx_t = _tfidf_rows[0][1]["tfidf"] or 1
                            _chips = ""
                            for _c, _td in _tfidf_rows:
                                _t_tf, _t_df, _t_score = _td["tf"], _td["df"], _td["tfidf"]
                                _rec = (_map_w[_c] / _map_freq[_c]) if _map_freq.get(_c) else 0.5
                                _sz = 0.85 + (_t_score / _mx_t) * 0.95   # 크기 = TF-IDF
                                _lbl, _bg, _fg = _recency_badge(_rec)
                                _chips += (
                                    f"<span title='빈도 {_t_tf} · 최근성 {round(_rec,2)} · TF-IDF {_t_score} (df {_t_df})' "
                                    f"style='display:inline-block;margin:3px;padding:3px 11px;border-radius:14px;"
                                    f"background:{_bg};color:{_fg};font-size:{_sz:.2f}em;font-weight:700'>"
                                    f"{_c} <span style='opacity:.65;font-size:.65em'>{_t_score}</span></span>")
                            st.markdown(_chips, unsafe_allow_html=True)
                            st.caption("크기 = TF-IDF 중요도(이 프로젝트에 특화될수록 큼) · 색 = 최근성 · 마우스를 올리면 빈도/최근성/df가 보여요.")
                        else:
                            st.info("TF-IDF를 계산할 메모 개념이 부족해요. 메모에 개념이 더 쌓이면 정확해져요.")
                    else:
                        _mx = _map_concepts[0][1]["frequency"] or 1
                        _chips = ""
                        for _c, _d in _map_concepts[:30]:
                            _f, _rec, _imp = _d["frequency"], _d["recency"], _d["importance"]
                            _sz = 0.85 + (_f / _mx) * 0.95          # 0.85~1.8em (노드 크기=빈도)
                            _lbl, _bg, _fg = _recency_badge(_rec)
                            _chips += (
                                f"<span title='빈도 {_f} · 최근성 {_rec} · 중요도 {_imp}' "
                                f"style='display:inline-block;margin:3px;padding:3px 11px;border-radius:14px;"
                                f"background:{_bg};color:{_fg};font-size:{_sz:.2f}em;font-weight:700'>"
                                f"{_c} <span style='opacity:.65;font-size:.65em'>×{_f}</span></span>")
                        st.markdown(_chips, unsafe_allow_html=True)
                        st.caption("크기 = 등장 빈도(frequency) · 색 = 최근성(recency 🔴최근 🟡보통 ⚪오래됨) · 마우스를 올리면 중요도(importance)가 보여요.")
                else:
                    st.info("이 프로젝트에 연결된 개념이 아직 없어요.")

                # ✅ 작업 중심 관계맵
                st.markdown("#### ✅ 작업 → 연결된 자료·개념")
                if not _proj_tasks:
                    st.info("연결된 작업이 없어요. 작업을 만들고 메모·개념을 연결해보세요.")
                else:
                    _notes_by_id = {n.get("id"): n for n in st.session_state.get("archive_notes", [])}
                    for _mt in _proj_tasks:
                        _nids = list(_mt.get("linked_note_ids", []) or [])
                        _cons = [c for c in (_mt.get("linked_concepts", []) or []) if c]
                        _strength = len(_nids) + len(_cons)        # 연결 강도(선 굵기 대용)
                        _bar = min(100, _strength * 20)
                        _due = _mt.get("due_date", "") or "—"
                        with st.container(border=True):
                            _tc1, _tc2 = st.columns([4, 1])
                            with _tc1:
                                st.markdown(
                                    f"**✅ {_mt.get('title','(제목 없음)')}** "
                                    f"<span style='color:#94a3b8;font-size:0.82em'>· {_mt.get('status','')} · 📅 {_due}</span><br>"
                                    f"<span style='color:#64748b;font-size:0.8em'>📄 메모 {len(_nids)}개 · 🧠 개념 {len(_cons)}개</span>",
                                    unsafe_allow_html=True)
                            with _tc2:
                                st.markdown(
                                    f"<div style='margin-top:6px;background:#e7edf7;border-radius:6px;height:7px'>"
                                    f"<div style='width:{_bar}%;background:#6366f1;height:7px;border-radius:6px'></div></div>"
                                    f"<div style='text-align:right;font-size:0.7em;color:#94a3b8'>연결 {_strength}</div>",
                                    unsafe_allow_html=True)
                            if _nids:
                                _titles = [_notes_by_id.get(i, {}).get("title", "(삭제된 메모)") for i in _nids]
                                st.markdown(
                                    "&nbsp;&nbsp;📄 " + " · ".join(f"`{t}`" for t in _titles),
                                    unsafe_allow_html=True)
                            if _cons:
                                st.markdown(
                                    "&nbsp;&nbsp;🧠 " + " ".join(f"`{c}`" for c in _cons),
                                    unsafe_allow_html=True)
                            if not _nids and not _cons:
                                st.caption("아직 연결된 메모·개념이 없어요.")

                # 📄 자료 ↔ 개념 관계
                st.markdown("#### 📄 자료 → 연결된 개념")
                if not _proj_notes:
                    st.info("이 프로젝트에 연결된 메모가 아직 없어요.")
                else:
                    # 메모별 개념: note concepts ∪ note_concept_links
                    _links_by_note = _ddict(set)
                    for _lk in st.session_state.get("note_concept_links", []):
                        if _lk.get("note_id") in _pn_ids and _lk.get("concept"):
                            _links_by_note[_lk["note_id"]].add(_lk["concept"])
                    # 메모를 참조하는 작업 수
                    _tasks_by_note = _ddict(int)
                    for _mt in _proj_tasks:
                        for _i in (_mt.get("linked_note_ids", []) or []):
                            _tasks_by_note[_i] += 1
                    for _mn in _proj_notes:
                        _nid = _mn.get("id")
                        _ncons = sorted(set(_mn.get("concepts", []) or []) | _links_by_note.get(_nid, set()))
                        _ntask = _tasks_by_note.get(_nid, 0)
                        with st.container(border=True):
                            st.markdown(
                                f"**📄 {_mn.get('title','(제목 없음)')}** "
                                f"<span style='color:#94a3b8;font-size:0.8em'>· 🧠 개념 {len(_ncons)}개 · ✅ 연결 작업 {_ntask}개</span>",
                                unsafe_allow_html=True)
                            if _ncons:
                                st.markdown(
                                    "&nbsp;&nbsp;🧠 " + " ".join(f"`{c}`" for c in _ncons),
                                    unsafe_allow_html=True)
                            else:
                                st.caption("아직 추출된 개념이 없어요.")


# ─────────────────────────────────────────
# ✅ 작업 관리 페이지
# ─────────────────────────────────────────
def render_task_page():
    import uuid

    st.markdown("## ✅ 작업 관리")
    st.caption("프로젝트별 할 일을 보드뷰/목록으로 관리해요.")

    tasks = st.session_state.get("tasks", [])
    projects = st.session_state.get("projects", [])
    proj_names = ["전체"] + [p["name"] for p in projects]

    # ── 새 작업 추가 ──────────────────────────────────────────
    with st.expander("➕ 새 작업 만들기", expanded=False):
        t1, t2 = st.columns(2)
        with t1:
            t_title = st.text_input("작업명 *", key="new_task_title", placeholder="예: CREST 자료조사 정리")
            t_proj = st.selectbox("프로젝트", [p["name"] for p in projects] if projects else ["없음"], key="new_task_proj")
            t_status = st.selectbox("상태", ["시작 전", "진행 중", "검토 중", "완료", "보류"], key="new_task_status")
        with t2:
            t_priority = st.selectbox("우선순위", ["높음", "보통", "낮음"], key="new_task_priority")
            t_due = st.date_input("마감일", key="new_task_due", value=None)
            t_summary = st.text_input("메모", key="new_task_summary", placeholder="간단한 설명")

        st.markdown("**🔗 연결 (선택)**")
        _nt_notes, _nt_cons = task_link_editor("newtask", t_proj, [], [])

        if st.button("✅ 작업 저장", key="save_new_task", type="primary", use_container_width=True):
            if t_title.strip():
                proj_obj = next((p for p in projects if p["name"] == t_proj), {})
                _tnow = datetime.now().strftime("%Y-%m-%d %H:%M")
                new_task = {
                    "id": f"task_{uuid.uuid4().hex[:8]}",
                    "user_id": "local_user",
                    "title": t_title.strip(),
                    "project_id": proj_obj.get("id", ""),
                    "project": t_proj,
                    "status": t_status,
                    "priority": t_priority,
                    "due_date": str(t_due) if t_due else "",
                    "summary": t_summary.strip(),
                    "linked_note_ids": list(_nt_notes),
                    "linked_concepts": list(_nt_cons),
                    "created_at": _tnow,
                    "updated_at": _tnow,
                    "deleted_at": None,
                }
                tasks.append(new_task)
                st.session_state.tasks = tasks
                clear_link_picker("newtask_note")
                clear_link_picker("newtask_con")
                save_persisted_data()
                _flash(f"'{t_title}' 작업을 추가했어요!")
                st.rerun()
            else:
                st.warning("작업명을 입력해주세요.")

    st.divider()

    if not tasks:
        st.info("아직 작업이 없어요. 위에서 첫 작업을 추가해보세요.")
        return

    # ── 필터 + 뷰 선택 ──────────────────────────────────────
    f1, f2, f3 = st.columns([2, 1, 1])
    with f1:
        filter_proj = st.selectbox("프로젝트 필터", proj_names, key="task_filter_proj")
    with f2:
        filter_status = st.selectbox("상태 필터", ["전체", "시작 전", "진행 중", "검토 중", "완료", "보류"], key="task_filter_status")
    with f3:
        task_view = st.radio("뷰", ["📋 목록", "🗂️ 보드", "📅 캘린더", "📊 타임라인"], horizontal=True, key="task_view_mode")

    filtered = tasks
    if filter_proj != "전체":
        filtered = [t for t in filtered if t.get("project") == filter_proj]
    if filter_status != "전체":
        filtered = [t for t in filtered if t.get("status") == filter_status]

    PRIORITY_EMOJI = {"높음": "🔴", "보통": "🟠", "낮음": "⚪"}
    STATUS_EMOJI = {"시작 전": "⬜", "진행 중": "🔄", "검토 중": "🔍", "완료": "✅", "보류": "⏸️"}

    if task_view == "📋 목록":
        _status_opts = ["시작 전","진행 중","검토 중","완료","보류"]
        _pri_opts = ["높음","보통","낮음"]
        _smap = {"진행중":"진행 중","시작전":"시작 전","보류중":"보류","완료됨":"완료","검토중":"검토 중"}
        for task in filtered:
            if "id" not in task:
                task["id"] = f"task_{task.get('title','t')[:8]}_{id(task)}"
            _tid = task["id"]
            _edit_key = f"task_edit_{_tid}"
            _is_editing = st.session_state.get(_edit_key, False)
            se = STATUS_EMOJI.get(task.get("status",""), "⬜")
            pe = PRIORITY_EMOJI.get(task.get("priority",""), "⚪")
            with st.container(border=True):
                if _is_editing:
                    # ── 편집 모드 ──
                    _ea, _eb = st.columns(2)
                    with _ea:
                        _e_title = st.text_input("작업명", value=task.get("title",""), key=f"task_et_{_tid}")
                        _proj_opts = [p["name"] for p in projects] if projects else ["없음"]
                        _cur_proj = task.get("project","")
                        _proj_idx = _proj_opts.index(_cur_proj) if _cur_proj in _proj_opts else 0
                        _e_proj = st.selectbox("프로젝트", _proj_opts, index=_proj_idx, key=f"task_ep_{_tid}")
                    with _eb:
                        _cur_s = _smap.get(task.get("status","시작 전"), task.get("status","시작 전"))
                        if _cur_s not in _status_opts: _cur_s = "시작 전"
                        _e_status = st.selectbox("상태", _status_opts, index=_status_opts.index(_cur_s), key=f"task_es_{_tid}")
                        _cur_p = task.get("priority","보통")
                        _pi = _pri_opts.index(_cur_p) if _cur_p in _pri_opts else 1
                        _e_pri = st.selectbox("우선순위", _pri_opts, index=_pi, key=f"task_epr_{_tid}")
                    _e_due = st.date_input("마감일", value=parse_date_for_input(task.get("due_date","")), key=f"task_ed_{_tid}")
                    _e_sum = st.text_area("메모", value=task.get("summary",""), key=f"task_em_{_tid}", height=60)
                    st.markdown("**🔗 연결**")
                    _et_notes, _et_cons = task_link_editor(
                        f"edittask_{_tid}", _e_proj,
                        task.get("linked_note_ids", []), task.get("linked_concepts", []))
                    _sv_col, _cl_col = st.columns(2)
                    with _sv_col:
                        if st.button("💾 저장", key=f"task_esave_{_tid}", type="primary", use_container_width=True):
                            task["title"] = _e_title.strip() or task["title"]
                            task["project"] = _e_proj
                            task["status"] = _e_status
                            task["priority"] = _e_pri
                            task["due_date"] = normalize_date_str(_e_due)
                            task["summary"] = _e_sum.strip()
                            task["linked_note_ids"] = list(_et_notes)
                            task["linked_concepts"] = list(_et_cons)
                            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                            st.session_state[_edit_key] = False
                            clear_link_picker(f"edittask_{_tid}_note")
                            clear_link_picker(f"edittask_{_tid}_con")
                            save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                    with _cl_col:
                        if st.button("취소", key=f"task_ecancel_{_tid}", use_container_width=True):
                            st.session_state[_edit_key] = False
                            clear_link_picker(f"edittask_{_tid}_note")
                            clear_link_picker(f"edittask_{_tid}_con")
                            st.rerun()
                else:
                    # ── 보기 모드 ──
                    # 좌: 작업 정보 / 우: 액션 그룹(상태+수정+삭제)을 하나로 묶어 오른쪽 끝에 밀착
                    c_left, c_actions = st.columns([6, 2.6])
                    with c_left:
                        st.markdown(f"{se} **{task.get('title','')}**")
                        st.caption(f"📁 {task.get('project','없음')} · {pe} {task.get('priority','')} · 📅 {task.get('due_date','—')}")
                        if task.get("summary"):
                            st.caption(task["summary"])
                    with c_actions:
                        a_status, a_edit, a_del = st.columns([1.7, 0.5, 0.5])
                        with a_status:
                            _cur_status = _smap.get(task.get("status","시작 전"), task.get("status","시작 전"))
                            if _cur_status not in _status_opts: _cur_status = "시작 전"
                            new_status = st.selectbox("", _status_opts, index=_status_opts.index(_cur_status),
                                key=f"task_status_{_tid}", label_visibility="collapsed")
                            if new_status != task.get("status"):
                                task["status"] = new_status
                                task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                        with a_edit:
                            if st.button("✏️", key=f"task_edit_btn_{_tid}", help="수정", use_container_width=True):
                                st.session_state[_edit_key] = True; st.rerun()
                        with a_del:
                            if st.button("🗑️", key=f"del_task_{_tid}", help="삭제", use_container_width=True):
                                st.session_state.tasks = [t for t in tasks if t.get("id") != _tid]
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                    _lk_cap = task_link_caption(task)
                    _lk_chips = task_concept_chips(task)
                    if _lk_cap:
                        st.caption(f"🔗 {_lk_cap}" + (f"  ·  {_lk_chips}" if _lk_chips else ""))
                        _all_notes_v = st.session_state.get("archive_notes", [])
                        _note_titles = [
                            (next((n.get("title") or "(제목 없음)" for n in _all_notes_v
                                   if n.get("id") == nid), None))
                            for nid in task.get("linked_note_ids", [])
                        ]
                        _note_titles = [t for t in _note_titles if t]
                        with st.expander("🔎 연결 상세 보기", expanded=False):
                            if _note_titles:
                                st.markdown("**📎 관련 메모**")
                                for _ntl in _note_titles:
                                    st.markdown(f"- {_ntl}")
                            if task.get("linked_concepts"):
                                st.markdown("**🧠 관련 개념**")
                                st.markdown(" ".join(f"`{c}`" for c in task.get("linked_concepts", [])))
                            st.caption(f"📁 프로젝트: {task.get('project','없음')}  ·  📅 마감: {task.get('due_date','—')}")

    elif task_view == "🗂️ 보드":
        statuses = ["시작 전", "진행 중", "검토 중", "완료", "보류"]
        _col_bg = {"시작 전":"#f1f5f9","진행 중":"#dbeafe","검토 중":"#fef3c7","완료":"#dcfce7","보류":"#fee2e2"}
        _smap_b = {"진행중":"진행 중","시작전":"시작 전","보류중":"보류","완료됨":"완료","검토중":"검토 중"}
        # 상태 정규화
        for _tn in filtered:
            _sb = _tn.get("status","")
            if _sb in _smap_b:
                _tn["status"] = _smap_b[_sb]
        cols = st.columns(5)
        for _tsi, (col, status) in enumerate(zip(cols, statuses)):
            with col:
                se = STATUS_EMOJI.get(status, "")
                group = [t for t in filtered if t.get("status") == status]
                st.markdown(
                    f'<div style="background:{_col_bg.get(status,"#f1f5f9")};border-radius:8px;'
                    f'padding:8px 10px;margin-bottom:8px;text-align:center;">'
                    f'<b>{se} {status}</b><br><span style="font-size:0.8rem;color:#64748b">{len(group)}개</span></div>',
                    unsafe_allow_html=True
                )
                for task in group:
                    if "id" not in task:
                        task["id"] = f"task_{task.get('title','t')[:8]}_{id(task)}"
                    _tid2 = task["id"]
                    pe = PRIORITY_EMOJI.get(task.get("priority",""), "")
                    with st.container(border=True):
                        st.markdown(f"**{task.get('title','')}**")
                        st.caption(f"📁 {task.get('project','—')}")
                        st.caption(f"{pe} {task.get('priority','')} · 📅 {task.get('due_date','—')}")
                        _bcap = task_link_caption(task)
                        if _bcap:
                            st.caption(f"🔗 {_bcap}")
                        _tl, _tr = st.columns(2)
                        with _tl:
                            if _tsi > 0 and st.button("←", key=f"tb_left_{_tid2}", help=f"{statuses[_tsi-1]}로"):
                                task["status"] = statuses[_tsi - 1]
                                task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                        with _tr:
                            if _tsi < len(statuses) - 1 and st.button("→", key=f"tb_right_{_tid2}", help=f"{statuses[_tsi+1]}로"):
                                task["status"] = statuses[_tsi + 1]
                                task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

    if task_view == "📅 캘린더":
        from itertools import groupby as _gb
        import calendar as _cal
        from datetime import datetime as _dt2
        st.markdown("### 📅 캘린더")
        _now = _dt2.now()
        _cal_month = st.selectbox("월 선택", [f"{_now.year}-{m:02d}" for m in range(1,13)],
            index=_now.month - 1, key="task_cal_month")
        _yr, _mo = int(_cal_month.split("-")[0]), int(_cal_month.split("-")[1])
        _, _days_in_month = _cal.monthrange(_yr, _mo)
        _weeks = []
        _cur_week = []
        _first_dow = _cal.monthrange(_yr, _mo)[0]
        for _ in range(_first_dow):
            _cur_week.append(None)
        for d in range(1, _days_in_month + 1):
            _cur_week.append(d)
            if len(_cur_week) == 7:
                _weeks.append(_cur_week)
                _cur_week = []
        if _cur_week:
            while len(_cur_week) < 7:
                _cur_week.append(None)
            _weeks.append(_cur_week)
        _dow_labels = ["일","월","화","수","목","금","토"]
        _header_cols = st.columns(7)
        for i, d in enumerate(_dow_labels):
            _header_cols[i].markdown(f"<div style='text-align:center;font-weight:700;color:#888;font-size:12px'>{d}</div>", unsafe_allow_html=True)
        for week in _weeks:
            week_cols = st.columns(7)
            for ci, day in enumerate(week):
                with week_cols[ci]:
                    if day is None:
                        st.markdown("<div style='min-height:60px'></div>", unsafe_allow_html=True)
                    else:
                        _date_str = f"{_yr}-{_mo:02d}-{day:02d}"
                        _day_tasks = [t for t in filtered if t.get("due_date","") == _date_str]
                        _is_today = (_dt2.now().strftime("%Y-%m-%d") == _date_str)
                        _day_style = "background:#2563eb;color:white;border-radius:50%;width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-weight:800" if _is_today else ""
                        st.markdown(f"<div style='min-height:60px;border:1px solid #e7edf7;border-radius:8px;padding:4px'><span style='{_day_style}'>{day}</span>", unsafe_allow_html=True)
                        for _t in _day_tasks[:2]:
                            se2 = STATUS_EMOJI.get(_t.get("status",""), "")
                            st.markdown(f"<div style='font-size:10px;background:#dbeafe;border-radius:4px;padding:1px 4px;margin:1px 0'>{se2} {_t.get('title','')[:12]}</div>", unsafe_allow_html=True)
                        if len(_day_tasks) > 2:
                            st.markdown(f"<div style='font-size:10px;color:#888'>+{len(_day_tasks)-2}개</div>", unsafe_allow_html=True)
                        st.markdown("</div>", unsafe_allow_html=True)

    elif task_view == "📊 타임라인":
        st.markdown("### 📊 타임라인")
        _tl_tasks = [t for t in filtered if t.get("due_date")]
        if not _tl_tasks:
            st.info("마감일이 있는 작업이 없어요.")
        else:
            try:
                import plotly.express as _px3
                import pandas as _pd3
                _tdf2 = _pd3.DataFrame([{
                    "제목": t["title"][:30],
                    "시작": t.get("created_at","")[:10] or t["due_date"],
                    "마감": t["due_date"],
                    "프로젝트": t.get("project","없음"),
                    "상태": t.get("status",""),
                } for t in _tl_tasks])
                _tdf2["시작_dt"] = _pd3.to_datetime(_tdf2["시작"], errors="coerce")
                _tdf2["마감_dt"] = _pd3.to_datetime(_tdf2["마감"], errors="coerce")
                _tdf2 = _tdf2.dropna(subset=["시작_dt","마감_dt"])
                _tdf2.loc[_tdf2["시작_dt"] == _tdf2["마감_dt"], "마감_dt"] += _pd3.Timedelta(days=1)
                if not _tdf2.empty:
                    _proj_list = sorted(_tdf2["프로젝트"].unique())
                    _colors = ["#3b82f6","#10b981","#f59e0b","#8b5cf6","#ef4444","#06b6d4"]
                    _color_map = {p: _colors[i % len(_colors)] for i, p in enumerate(_proj_list)}
                    _tl_fig2 = _px3.timeline(
                        _tdf2, x_start="시작_dt", x_end="마감_dt",
                        y="제목", color="프로젝트",
                        color_discrete_map=_color_map,
                        hover_data={"상태": True, "프로젝트": True},
                        height=max(350, len(_tdf2) * 40),
                    )
                    _tl_fig2.update_layout(
                        xaxis_title="날짜", yaxis_title="",
                        plot_bgcolor="#f8fbff", paper_bgcolor="#f8fbff",
                    )
                    st.plotly_chart(_tl_fig2, use_container_width=True)
            except Exception as e:
                st.warning(f"타임라인 오류: {e}")


# -----------------------------
# Menu Pages
# -----------------------------
# rerun 후에도 보이도록 큐에 쌓인 알림을 먼저 표시
_render_flash()

if menu == "새 엔터티":
    # ══════════════════════════════════════════════════════════
    # ➕ Entity Wizard — 통합 생성창
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="background:linear-gradient(135deg,#1e3a8a,#3b82f6);border-radius:16px;
     padding:26px 32px 20px;margin-bottom:24px;color:white;">
  <div style="font-size:1.9rem;font-weight:900;margin-bottom:4px;">➕ 새 엔터티 만들기</div>
  <div style="opacity:0.85;line-height:1.6;">
    프로젝트·작업·개념·메모·폴더를 한 곳에서 만들어요. 만들면 지식맵·관계·엔터티 상세에 자동 반영돼요.
  </div>
</div>""", unsafe_allow_html=True)

    _wz_type = st.radio(
        "무엇을 만들까요?",
        ["📝 메모", "✅ 작업", "🧠 개념", "📁 프로젝트", "📂 폴더"],
        index=0, horizontal=True, key="wz_type"
    )
    st.divider()

    _wz_projects = st.session_state.get("projects", [])
    _wz_proj_names = [p.get("name","") for p in _wz_projects]
    _wz_folders = sorted(set(st.session_state.get("folders", [])) |
                         set(st.session_state.get("pkm_concept_folders", {}).values()))

    # ─── 프로젝트 ───
    if _wz_type == "📁 프로젝트":
        c1, c2 = st.columns(2)
        with c1:
            _wp_name = st.text_input("프로젝트명 *", key="wz_p_name", placeholder="예: 결식아동 지원 리서치")
            _wp_cat = st.selectbox("대분류", ["학교/팀플","개인개발","자격증","취업준비","리서치","기타"], key="wz_p_cat")
            _wp_status = st.selectbox("상태", ["예정","진행 중","완료","보류"], key="wz_p_status")
        with c2:
            _wp_priority = st.selectbox("우선순위", ["높음","보통","낮음"], key="wz_p_pri")
            _wp_start = st.date_input("시작일", key="wz_p_start", value=None)
            _wp_due = st.date_input("마감일", key="wz_p_due", value=None)
        _wp_desc = st.text_area("설명", key="wz_p_desc", height=80)
        if st.button("✅ 프로젝트 만들기", key="wz_p_save", type="primary", use_container_width=True):
            if _wp_name.strip():
                create_project(_wp_name, _wp_desc, _wp_cat, _wp_status, _wp_priority,
                               str(_wp_start) if _wp_start else "", str(_wp_due) if _wp_due else "")
                _flash(f"'{_wp_name}' 프로젝트를 만들었어요!")
                st.rerun()
            else:
                st.warning("프로젝트명을 입력해주세요.")

    # ─── 작업 ───
    elif _wz_type == "✅ 작업":
        c1, c2 = st.columns(2)
        with c1:
            _wt_title = st.text_input("작업명 *", key="wz_t_title", placeholder="예: 자료조사 정리")
            _wt_proj = st.selectbox("프로젝트", ["(없음)"] + _wz_proj_names, key="wz_t_proj")
            _wt_status = st.selectbox("상태", ["시작 전","진행 중","검토 중","완료","보류"], key="wz_t_status")
        with c2:
            _wt_pri = st.selectbox("우선순위", ["높음","보통","낮음"], key="wz_t_pri")
            _wt_due = st.date_input("마감일", key="wz_t_due", value=None)
            _wt_sum = st.text_input("메모", key="wz_t_sum", placeholder="간단한 설명")
        if st.button("✅ 작업 만들기", key="wz_t_save", type="primary", use_container_width=True):
            if _wt_title.strip():
                create_task(_wt_title, "" if _wt_proj=="(없음)" else _wt_proj,
                            _wt_status, _wt_pri, str(_wt_due) if _wt_due else "", _wt_sum)
                _flash(f"'{_wt_title}' 작업을 만들었어요!")
                st.rerun()
            else:
                st.warning("작업명을 입력해주세요.")

    # ─── 개념 ───
    elif _wz_type == "🧠 개념":
        c1, c2 = st.columns(2)
        with c1:
            _wc_name = st.text_input("개념명 *", key="wz_c_name", placeholder="예: CREST 프레임워크")
            _wc_folder_mode = st.radio("폴더", ["기존 선택","새로 입력"], horizontal=True, key="wz_c_fmode")
        with c2:
            if _wc_folder_mode == "기존 선택":
                _wc_folder = st.selectbox("폴더 선택", (_wz_folders or ["내 개념"]), key="wz_c_folder_sel")
            else:
                _wc_folder = st.text_input("새 폴더명", key="wz_c_folder_new", placeholder="예: 마케팅/프레임워크")
        _wc_desc = st.text_area("설명", key="wz_c_desc", height=70)
        _wc_alias = st.text_input("별칭 (쉼표 구분)", key="wz_c_alias", placeholder="예: 인공지능, GenAI")
        if st.button("✅ 개념 만들기", key="wz_c_save", type="primary", use_container_width=True):
            if _wc_name.strip():
                _aliases = [a.strip() for a in _wc_alias.split(",") if a.strip()]
                create_concept(_wc_name, _wc_folder or "내 개념", _wc_desc, _aliases)
                _flash(f"'{_wc_name}' 개념을 만들었어요!")
                st.rerun()
            else:
                st.warning("개념명을 입력해주세요.")

    # ─── 메모 ───
    elif _wz_type == "📝 메모":
        c1, c2 = st.columns(2)
        with c1:
            _wm_title = st.text_input("메모 제목 *", key="wz_m_title", placeholder="예: 속초 여행 정리")
            _wm_proj = st.selectbox("프로젝트", ["기본 프로젝트"] + _wz_proj_names, key="wz_m_proj")
        with c2:
            _wm_section = st.text_input("섹션", key="wz_m_section", value="일반")
        # ── 태그: 기존 태그 선택 + 새 태그 추가 (중복 방지) ──
        _wm_existing_tags = sorted({
            str(t).replace("#", "").strip()
            for n in st.session_state.get("archive_notes", [])
            for t in n.get("tags", []) if str(t).strip()
        })
        _wm_sel_tags = multiselect_with_all("태그 (기존에서 선택)", _wm_existing_tags, key="wz_m_tags_sel",
                                      help="이미 쓰던 태그를 골라 쓰면 중복이 안 생겨요")
        _wm_new_tags = st.text_input("새 태그 추가 (쉼표 구분)", key="wz_m_tags_new",
                                     placeholder="목록에 없는 태그만. 예: 여행, 맛집")
        _wm_note = st.text_area(
            "메모 내용 *",
            key="wz_m_note",
            height=180,
            placeholder="## 핵심 정리\n- 항목\n- [ ] 확인할 일\n> 인용이나 참고",
            help="마크다운을 지원해요. 저장 후 읽기 화면에서 제목/목록/체크박스가 적용돼요.",
        )
        # 개념 연결: 기존에서 고르거나 + 새로 입력 (태그와 동일 패턴 — 개념이 없어도 막히지 않게)
        _wm_all_cons = [c.get("name") if isinstance(c,dict) else str(c) for c in st.session_state.get("pkm_custom_concepts",[]) if c]
        _wm_link_cons = multiselect_with_all("연결할 개념 (기존에서 선택)", _wm_all_cons, key="wz_m_cons",
                                             help="이미 있는 개념을 고르면 같은 개념을 쓰는 다른 메모·프로젝트와 이어져요.")
        _wm_new_cons = st.text_input("새 개념 추가 (쉼표 구분)", key="wz_m_cons_new",
                                     placeholder="목록에 없는 개념을 바로 입력. 예: 그래프 구조, 임베딩")
        st.caption("💡 비워둬도 저장 시 메모 내용에서 핵심 개념이 자동으로 뽑혀 연결돼요.")
        with st.expander("🔗 링크·글 가져와서 AI 초안 만들기 (펼치기)"):
            st.caption("URL이나 붙여넣은 글을 AI가 요약·신뢰도·개념 후보로 정리해 메모 초안을 만들어줘요.")
            if st.button("🔗 링크·글 가져오기 열기", key="wz_m_import", use_container_width=True):
                st.session_state["home_show_import"] = True
                st.query_params["page"] = "home"
                st.rerun()
        if st.button("✅ 메모 만들기", key="wz_m_save", type="primary", use_container_width=True):
            if _wm_title.strip() and _wm_note.strip():
                _tags = list(dict.fromkeys(
                    list(_wm_sel_tags) +
                    [t.strip() for t in _wm_new_tags.split(",") if t.strip()]
                ))
                # 사용자가 고른/입력한 개념 + 메모 내용에서 자동 추출한 개념 합치기
                _auto_cons = extract_local_concepts(_wm_note, _tags, limit=8)
                _cons = list(dict.fromkeys(
                    list(_wm_link_cons)
                    + [c.strip() for c in _wm_new_cons.split(",") if c.strip()]
                    + list(_auto_cons)
                ))
                _new_memo = create_memo(_wm_title, _wm_note, _wm_proj, _wm_section or "일반",
                                        _tags, original_text=_wm_note, concepts=_cons)
                st.session_state["_reco_preview_note_id"] = _new_memo.get("id")
                _flash(f"'{_wm_title}' 메모를 '{_wm_proj}'에 저장했어요! (개념 {len(_cons)}개 연결)")
                st.rerun()
            else:
                st.warning("제목과 내용을 입력해주세요.")

        # 저장 직후 AI 연결 추천 미리보기 (1단계: 후보만, 반영 X)
        _rp_id = st.session_state.get("_reco_preview_note_id")
        if _rp_id:
            _rp_note = next((n for n in st.session_state.get("archive_notes", [])
                             if n.get("id") == _rp_id), None)
            if _rp_note:
                st.divider()
                render_reco_preview(_rp_note)
                if st.button("닫기", key="reco_preview_close"):
                    st.session_state.pop("_reco_preview_note_id", None)
                    st.rerun()

    # ─── 폴더 ───
    else:
        _wf_name = st.text_input("폴더명 *", key="wz_f_name", placeholder="예: 마케팅")
        st.caption("개념을 분류할 폴더를 만들어요. 개념 생성 시 이 폴더를 선택할 수 있어요.")
        if _wz_folders:
            st.markdown("**기존 폴더:** " + " · ".join(f"📂 {f}" for f in _wz_folders))
        if st.button("✅ 폴더 만들기", key="wz_f_save", type="primary", use_container_width=True):
            if _wf_name.strip():
                create_folder(_wf_name)
                _flash(f"'{_wf_name}' 폴더를 만들었어요!", icon="📂")
                st.rerun()
            else:
                st.warning("폴더명을 입력해주세요.")

    # ── 최근 생성 현황 ──
    st.divider()
    st.markdown("##### 📊 현재 엔터티 현황")
    _wz_m1, _wz_m2, _wz_m3, _wz_m4, _wz_m5 = st.columns(5)
    _wz_m1.metric("📁 프로젝트", f"{len(_wz_projects)}")
    _wz_m2.metric("✅ 작업", f"{len(st.session_state.get('tasks',[]))}")
    _wz_m3.metric("🧠 개념", f"{len(st.session_state.get('pkm_custom_concepts',[]))}")
    _wz_m4.metric("📝 메모", f"{len(st.session_state.get('archive_notes',[]))}")
    _wz_m5.metric("📂 폴더", f"{len(_wz_folders)}")

    st.stop()


if menu == "분석 결과":
    st.markdown("## 📊 분석 결과")
    if st.session_state.get("history_restore_failed"):
        st.warning("이 기록의 분석 캐시가 만료됐어요. 같은 URL을 다시 분석하면 최신 결과로 열려요.")
        st.session_state["history_restore_failed"] = False
    if st.session_state.get("history_restored"):
        st.success("✅ 분석 결과를 불러왔어요. 아래에서 확인할 수 있어요.")
        try:
            st.toast("분석 결과를 불러왔어요.", icon="📂")
        except Exception:
            pass
        st.session_state["history_restored"] = False
    render_recent_analysis_cards(limit=5)
    st.divider()
    if st.session_state.last_result:
        render_result(st.session_state.last_result, extracted_text=None, final_url=st.session_state.last_final_url)
    else:
        st.info("아직 열려 있는 분석 결과가 없어요. 최근 검색 기록 탭에서 저장된 분석 결과를 다시 불러올 수 있어요.")
    st.divider()

    st.markdown(
        "## 📈 누적 사용자 학습 데이터"
    )

    stats = st.session_state.get(
        "auto_feedback_stats",
        {}
    )

    if stats:
        st.write(
            f"저장된 URL 평가 수: {len(stats)}"
        )
    else:
        st.caption(
            "아직 누적 학습 데이터가 없어요."
        )
    st.stop()

if menu == "신뢰도 근거":
    st.markdown("## 🔎 신뢰도 근거")
    st.caption("TrustLens가 어떤 기준으로 신뢰도를 판단하는지 보고, 나만의 기준도 추가할 수 있어요.")

    st.markdown("### 🧭 TrustLens 기본 신뢰도 기준")
    for name, desc in DEFAULT_TRUST_CRITERIA:
        with st.expander(name, expanded=False):
            st.write(desc)

    st.divider()
    st.markdown("### 🛠️ 나만의 커스텀 신뢰도 기준")
    st.caption("예: 사진 많은 후기 더 신뢰 / 가격 공개 필수 / 정책 글은 신청기간 명확해야 함")

    t_key = "custom_criterion_title"
    d_key = "custom_criterion_desc"
    w_key = "custom_criterion_weight"

    c1, c2 = st.columns([1, 1])
    with c1:
        st.text_input("기준 이름", placeholder="예: 실제 사진 근거", key=t_key)
    with c2:
        st.selectbox("중요도", ["낮음", "보통", "높음"], index=1, key=w_key)

    st.text_area("기준 설명", placeholder="예: 사진이 많고 상황 설명이 구체적인 글을 더 신뢰한다.", height=90, key=d_key)

    st.button("➕ 커스텀 기준 추가", use_container_width=True, on_click=save_custom_trust_criterion, args=(t_key, d_key, w_key))

    if st.session_state.get("custom_criterion_saved"):
        st.success("커스텀 기준을 저장했어요. 다음 분석부터 반영돼요.")
        st.session_state["custom_criterion_saved"] = False

    if st.session_state.get("custom_criterion_error"):
        st.warning(st.session_state["custom_criterion_error"])
        st.session_state["custom_criterion_error"] = ""

    if st.session_state.custom_trust_criteria:
        for idx, item in enumerate(st.session_state.custom_trust_criteria):
            st.markdown(f"**{idx+1}. {item.get('title')}** · 중요도 {item.get('weight')}")
            st.caption(item.get("description", ""))
            st.button("🗑️ 삭제", key=f"delete_custom_criterion_{idx}", on_click=delete_custom_trust_criterion, args=(idx,))

    st.divider()
    st.markdown("### 📊 현재 분석 결과의 신뢰도 근거")

    if st.session_state.last_result:
        result = st.session_state.last_result
        evidence = result.get("evidence", {})
        breakdown = result.get("score_breakdown", {})
        content_type = result.get("content_type", "unknown")
        score = result.get("trust_score", 0)
        ad_risk = result.get("ad_risk", "mid")
        ad_text = {"low": "낮음", "mid": "주의", "high": "위험"}.get(ad_risk, ad_risk)

        a, b, c = st.columns(3)
        with a:
            st.metric("현재 신뢰도", f"{score}점")
        with b:
            st.metric("콘텐츠 유형", CONTENT_TYPE_LABELS.get(content_type, content_type))
        with c:
            st.metric("광고 위험도", ad_text)

        st.markdown("#### 📌 점수 산정 근거")
        for key, label, max_val in get_score_items_for_type(content_type):
            val = get_int_score(breakdown, key)
            st.markdown(f"**{label}** · {val}/{max_val}점")
            st.progress(float(val / max_val if max_val else 0))

        st.markdown("#### 🧾 원문 기반 판단 근거")
        st.markdown(f"**공식 출처 근거:** {evidence.get('official_source', '없음')}")
        st.markdown(f"**광고 판단 근거:** {evidence.get('ad_signal', '없음')}")
        st.markdown(f"**경험 신호 근거:** {evidence.get('experience_signal', '없음')}")
        st.markdown(f"**단점/비판 신호:** {evidence.get('negative_signal', '없음')}")
    else:
        st.info("현재 열려 있는 신뢰도 근거가 없어요.")

    st.stop()

if menu == "분석결과 아카이브":
    st.markdown("## 🏷️ 분석결과 아카이브")
    st.caption("최근 검색기록은 단순 이력이고, 이 탭은 내가 저장한 분석 결과를 태그·메모·즐겨찾기로 관리하는 공간이에요.")

    if st.session_state.get("analysis_archive_restored"):
        st.success("저장된 분석 결과를 다시 불러왔어요. 왼쪽 메뉴의 📊 분석 결과에서 확인할 수 있어요.")
        st.session_state["analysis_archive_restored"] = False
    if st.session_state.get("saved_analysis_deleted"):
        st.success("저장된 분석 결과를 삭제했어요.")
        st.session_state["saved_analysis_deleted"] = False

    archive_search = st.text_input("🔍 저장된 분석 검색", placeholder="제목, URL, 태그, 메모로 검색")
    only_favorite_analysis = st.checkbox("⭐ 즐겨찾기 분석만 보기", value=False)

    all_archive_tags = []
    for item in st.session_state.saved_analyses:
        for tag in item.get("tags", []):
            clean = str(tag).replace("#", "").strip()
            if clean and clean not in all_archive_tags:
                all_archive_tags.append(clean)

    selected_archive_tag = st.selectbox("태그 필터", ["전체"] + all_archive_tags) if all_archive_tags else "전체"

    analyses_to_show = list(enumerate(st.session_state.saved_analyses))

    if archive_search.strip():
        q = archive_search.strip().lower()
        analyses_to_show = [
            (idx, item) for idx, item in analyses_to_show
            if q in str(item.get("title", "")).lower()
            or q in str(item.get("url", "")).lower()
            or q in str(item.get("memo", "")).lower()
            or q in str(item.get("project", "")).lower()
            or q in str(item.get("section", "")).lower()
            or q in " ".join([str(t) for t in item.get("tags", [])]).lower()
            or q in " ".join(item.get("summary", []) if isinstance(item.get("summary", []), list) else [str(item.get("summary", ""))]).lower()
        ]

    if only_favorite_analysis:
        analyses_to_show = [(idx, item) for idx, item in analyses_to_show if item.get("favorite", False)]

    if selected_archive_tag != "전체":
        analyses_to_show = [
            (idx, item) for idx, item in analyses_to_show
            if selected_archive_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]
        ]

    if analyses_to_show:
        _archive_open_idx = st.session_state.pop("_saved_analysis_open_idx", None)
        for display_idx, (original_index, item) in enumerate(analyses_to_show, start=1):
            star = "⭐" if item.get("favorite", False) else "☆"
            tags_text = ", ".join([str(t) for t in item.get("tags", [])]) or "태그 없음"
            with st.expander(f"{display_idx}. {star} {item.get('title', '저장 분석')} · {item.get('score', 0)}점 · {tags_text}", expanded=(_archive_open_idx == original_index)):
                st.markdown(f"**출처:** {display_source_label(item.get('url', ''))}")
                st.markdown(f"**저장일:** {item.get('saved_at', '')}")
                st.markdown(f"**콘텐츠 유형:** {CONTENT_TYPE_LABELS.get(item.get('content_type', 'unknown'), item.get('content_type', 'unknown'))}")
                st.markdown(f"**광고 위험도:** {item.get('ad_risk', '-')}")
                st.markdown(f"**작성자 유형:** {item.get('author_type', '-')}")

                summary = item.get("summary", [])
                if summary:
                    st.markdown("**요약**")
                    if isinstance(summary, list):
                        for s in summary:
                            st.markdown(f"- {s}")
                    else:
                        st.markdown(str(summary))

                title_key = f"saved_analysis_title_{original_index}"
                memo_key = f"saved_analysis_memo_{original_index}"
                tags_key = f"saved_analysis_tags_{original_index}"
                new_tags_key = f"saved_analysis_new_tags_{original_index}"

                st.text_input("제목 수정", value=item.get("title", ""), key=title_key)
                st.text_area("분석 메모 수정", value=item.get("memo", ""), height=100, key=memo_key)
                existing_tag_options = get_tag_edit_options(item)
                st.multiselect(
                    "기존 태그 선택/삭제",
                    options=existing_tag_options,
                    default=[tag for tag in item.get("tags", []) if tag in existing_tag_options],
                    key=tags_key,
                )
                st.text_input(
                    "새 태그 추가",
                    placeholder="예: 맛집후보, 재확인필요 처럼 쉼표/엔터로 여러 개 입력/태그 입력 후 아래 버튼(태그수정 저장)을 누른 뒤 위 기존 태그 선택칸을 누르세요.",
                    key=new_tags_key,
                    help="입력 후 아래의 제목/태그/메모 수정 저장 버튼을 눌러야 반영돼요.",
                )

                b1, b2, b3, b4 = st.columns(4)
                with b1:
                    st.button("🔁 분석결과 불러오기", key=f"restore_saved_analysis_{original_index}", use_container_width=True, on_click=restore_analysis_from_archive, args=(original_index,))
                with b2:
                    fav_label = "⭐ 즐겨찾기 해제" if item.get("favorite", False) else "☆ 즐겨찾기"
                    st.button(fav_label, key=f"fav_saved_analysis_{original_index}", use_container_width=True, on_click=toggle_saved_analysis_favorite, args=(original_index,))
                with b3:
                    if st.button("💾 제목/태그/메모 수정 저장", key=f"update_saved_analysis_{original_index}", use_container_width=True):
                        update_saved_analysis(original_index, memo_key, tags_key, new_tags_key, title_key)
                        st.session_state["_saved_analysis_open_idx"] = original_index
                        _flash("분석 메모와 태그를 저장했어요.")
                        st.rerun()
                with b4:
                    st.button("🗑️ 삭제", key=f"delete_saved_analysis_{original_index}", use_container_width=True, on_click=delete_saved_analysis, args=(original_index,))
    else:
        st.info("아직 저장된 분석결과가 없어요. 분석 결과 하단의 '현재 분석결과 저장' 버튼으로 저장해보세요.")
    st.stop()

if menu == "태그 관리":
    st.markdown("## 🏷️ 태그 관리")
    st.caption("태그 이름 변경, 병합, 삭제를 할 수 있어요. 변경 사항은 연결된 모든 메모에 바로 반영돼요.")

    # 전체 태그 수집 (태그명: 사용 횟수)
    from collections import Counter as _TagCounter
    _tag_counter = _TagCounter()
    for _n in st.session_state.archive_notes:
        for _t in _n.get("tags", []):
            _clean = str(_t).replace("#", "").strip()
            if _clean:
                _tag_counter[_clean] += 1

    all_tags = sorted(_tag_counter.keys())

    if not all_tags:
        st.info("아직 저장된 태그가 없어요. 분석 결과에서 메모를 저장하면 태그가 생겨요.")
        st.stop()

    # ── 상단: 태그 관리 액션 ────────────────────────────────
    st.markdown("### 🔧 태그 직접 관리")
    _tm_c1, _tm_c2, _tm_c3 = st.columns(3)

    with _tm_c1:
        st.markdown("""
        <div style='background:#eef4ff;border-radius:12px;padding:14px 16px 6px;margin-bottom:8px'>
        <div style='font-size:0.85em;font-weight:700;color:#1f3f91;margin-bottom:8px'>✏️ 이름 변경</div>
        """, unsafe_allow_html=True)
        _tm_rename_src = st.selectbox(
            "변경할 태그", all_tags,
            key="tm_rename_src",
            label_visibility="collapsed"
        )
        _tm_rename_dst = st.text_input(
            "새 이름", placeholder="새 태그명 입력",
            key="tm_rename_dst",
            label_visibility="collapsed"
        )
        st.caption(f"현재: **#{_tm_rename_src}** — {_tag_counter[_tm_rename_src]}개 메모")
        if st.button("변경 저장", key="tm_do_rename", use_container_width=True, type="primary"):
            if _tm_rename_dst.strip() and _tm_rename_dst.strip() != _tm_rename_src:
                _renamed = 0
                for _n in st.session_state.archive_notes:
                    _new_tags = []
                    for _t in _n.get("tags", []):
                        _ct = str(_t).replace("#", "").strip()
                        _new_tags.append(_tm_rename_dst.strip() if _ct == _tm_rename_src else _ct)
                    _n["tags"] = _new_tags
                    _renamed += 1
                save_persisted_data()
                _flash(f"#{_tm_rename_src} → #{_tm_rename_dst.strip()} 변경 완료")
                st.rerun()
            else:
                st.warning("새 태그명을 입력해주세요.")
        st.markdown("</div>", unsafe_allow_html=True)

    with _tm_c2:
        st.markdown("""
        <div style='background:#fff8ee;border-radius:12px;padding:14px 16px 6px;margin-bottom:8px'>
        <div style='font-size:0.85em;font-weight:700;color:#b45309;margin-bottom:8px'>🔗 병합 (여러 태그 → 하나로)</div>
        """, unsafe_allow_html=True)
        # 남길 대상 태그 선택
        _tm_merge_to = st.selectbox(
            "남길 태그 (병합 대상)", all_tags,
            key="tm_merge_to",
            label_visibility="collapsed"
        )
        st.caption(f"아래에서 **#{_tm_merge_to}** 로 합칠 태그들을 선택하세요")
        # 체크박스 목록 — 대상 제외한 전체 태그
        _merge_from_selected = []
        _other_tags = [t for t in all_tags if t != _tm_merge_to]
        if _other_tags:
            _mg_cols = st.columns(2)
            for _mgi, _mgt in enumerate(_other_tags):
                with _mg_cols[_mgi % 2]:
                    if st.checkbox(f"#{_mgt} ({_tag_counter.get(_mgt,0)}개)",
                                   key=f"tm_mg_chk_{_mgt[:15]}"):
                        _merge_from_selected.append(_mgt)
        else:
            st.caption("병합할 다른 태그가 없어요.")

        if _merge_from_selected:
            st.caption(f"선택: {', '.join(['#'+t for t in _merge_from_selected])} → **#{_tm_merge_to}**")
        if st.button("병합 실행", key="tm_do_merge", use_container_width=True,
                     type="primary", disabled=not _merge_from_selected):
            for _mf in _merge_from_selected:
                for _n in st.session_state.archive_notes:
                    _new_tags = []
                    for _t in _n.get("tags", []):
                        _ct = str(_t).replace("#", "").strip()
                        if _ct == _mf:
                            if _tm_merge_to not in _new_tags:
                                _new_tags.append(_tm_merge_to)
                        else:
                            _new_tags.append(_ct)
                    _n["tags"] = _new_tags
            save_persisted_data()
            _flash(f"{[('#'+t) for t in _merge_from_selected]} → #{_tm_merge_to} 병합 완료!")
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    with _tm_c3:
        st.markdown("""
        <div style='background:#fff1f1;border-radius:12px;padding:14px 16px 6px;margin-bottom:8px'>
        <div style='font-size:0.85em;font-weight:700;color:#b91c1c;margin-bottom:8px'>🗑️ 태그 삭제</div>
        """, unsafe_allow_html=True)
        _tm_del_tag = st.selectbox(
            "삭제할 태그", all_tags,
            key="tm_del_tag",
            label_visibility="collapsed"
        )
        st.caption(f"**#{_tm_del_tag}** 를 전체 메모에서 제거해요")
        st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        if st.button("🗑️ 삭제 확인", key="tm_do_delete", use_container_width=True):
            for _n in st.session_state.archive_notes:
                _n["tags"] = [
                    str(_t).replace("#", "").strip()
                    for _t in _n.get("tags", [])
                    if str(_t).replace("#", "").strip() != _tm_del_tag
                ]
            save_persisted_data()
            _flash(f"#{_tm_del_tag} 삭제 완료")
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # ── 태그 전체 목록 ────────────────────────────────────────
    st.divider()
    st.markdown("### 📋 전체 태그 목록")
    st.caption(f"총 {len(all_tags)}개 태그 · 태그 클릭 → 해당 메모만 보기")

    # 태그 클라우드 (버튼형)
    _tag_cols = st.columns(5)
    for _ti, _tag in enumerate(sorted(_tag_counter.items(), key=lambda x: -x[1])):
        _tname, _tcnt = _tag
        with _tag_cols[_ti % 5]:
            _is_sel = st.session_state.get("tm_selected_tag") == _tname
            _btn_style = "primary" if _is_sel else "secondary"
            if st.button(f"#{_tname} ({_tcnt})", key=f"tm_tagbtn_{_ti}", type=_btn_style, use_container_width=True):
                if _is_sel:
                    st.session_state["tm_selected_tag"] = None
                else:
                    st.session_state["tm_selected_tag"] = _tname
                st.rerun()

    # ── 태그별 메모 보기 ────────────────────────────────────────
    _sel_tag = st.session_state.get("tm_selected_tag")
    if _sel_tag:
        st.divider()
        st.markdown(f"### 🔖 #{_sel_tag} 태그 메모")
        _filtered = [
            n for n in st.session_state.archive_notes
            if _sel_tag in [str(t).replace("#", "").strip() for t in n.get("tags", [])]
        ]
        st.caption(f"{len(_filtered)}개 메모")
        for _idx, _item in enumerate(_filtered, 1):
            with st.expander(f"{_idx}. {_item.get('title', '저장 메모')} · {_item.get('score', 0)}점", expanded=False):
                st.markdown(f"**출처:** {display_source_label(_item.get('url', ''))}")
                st.markdown(f"**저장일:** {_item.get('saved_at', '')}")
                _oi = st.session_state.archive_notes.index(_item)
                _ek = f"tmv_note_{_oi}_{_sel_tag}"
                _tk = f"tmv_tags_{_oi}_{_sel_tag}"
                _ntk = f"tmv_ntags_{_oi}_{_sel_tag}"
                _titk = f"tmv_title_{_oi}_{_sel_tag}"
                st.markdown("**읽기 미리보기**")
                render_readable_markdown(_item.get("note", ""), max_chars=1200)
                st.divider()
                st.text_input("제목", value=_item.get("title", ""), key=_titk)
                st.multiselect("태그", options=get_tag_edit_options(_item),
                    default=[t for t in _item.get("tags", []) if t in get_tag_edit_options(_item)], key=_tk)
                st.text_input("새 태그 추가", placeholder="쉼표로 구분", key=_ntk)
                st.text_area("메모", value=_item.get("note", ""), height=200, key=_ek)
                if st.button("💾 저장", key=f"tmv_save_{_oi}_{_sel_tag}", use_container_width=True, type="primary"):
                    update_archive_note_and_tags(_oi, _ek, _tk, _ntk, _titk)
                    st.rerun()
    st.stop()

if menu == "지식 라이브러리":
    _NOTE_TYPE_META = {
        "study": ("📘", "공부자료"), "review": ("⭐", "후기/리뷰"),
        "policy": ("📜", "정책"), "info": ("📰", "정보"),
        "research": ("🔬", "연구"), "news": ("📰", "뉴스"),
        "essay": ("✍️", "칼럼"), "unknown": ("📝", "메모"),
    }

    def _note_meta(n):
        return _NOTE_TYPE_META.get(n.get("note_type", "unknown"), ("📝", "메모"))

    def _note_concepts(n):
        """메모 concepts ∪ note_concept_links 의 개념 집합 (대표 개념 정규화, 순서 보존).
        별칭은 대표 개념으로 합쳐져 관련 메모 추천·검색이 같은 개념군으로 인식됨."""
        out, seen = [], set()
        _nid = n.get("id")
        _raw = list(n.get("concepts", []) or [])
        for lk in st.session_state.get("note_concept_links", []):
            if lk.get("note_id") == _nid and lk.get("concept"):
                _raw.append(lk["concept"])
        for c in _raw:
            cc = canonical_concept(c)
            if cc and cc not in seen:
                seen.add(cc); out.append(cc)
        return out

    def _note_one_line(n):
        _ol = (n.get("one_line_summary") or "").strip()
        if _ol:
            return _ol
        _src = (n.get("summary") or n.get("note") or "").strip()
        if not _src:
            return ""
        for _sep in ["다.", ".", "\n"]:
            if _sep in _src:
                return _src.split(_sep)[0].strip()[:120] + ("…" if len(_src) > 120 else "")
        return _src[:120] + ("…" if len(_src) > 120 else "")

    def _crumb_of(n):
        parts = [p for p in [n.get("project", ""), n.get("section", ""), n.get("step", "")]
                 if p and p not in ("기본 프로젝트", "일반", "없음")]
        return " › ".join(parts)

    _all_notes = st.session_state.archive_notes

    # ════════════════ 노트 상세 보기 모드 ════════════════
    _open_id = st.session_state.get("archive_open_note_id")
    _open_note = next((n for n in _all_notes if n.get("id") == _open_id), None) if _open_id else None

    if _open_id and _open_note is None:
        st.session_state["archive_open_note_id"] = None  # 삭제된 노트
    elif _open_note is not None:
        item = _open_note
        _icon, _label = _note_meta(item)
        if st.button("← 목록으로", key="archive_back"):
            st.session_state["archive_open_note_id"] = None
            st.rerun()

        _crumb = _crumb_of(item)
        st.markdown(f"## {_icon} {item.get('title', '제목 없음')}")
        st.caption(
            f"{_label} · 📅 {item.get('saved_at', '')[:16]}"
            + (f" · 📁 {_crumb}" if _crumb else "")
            + (f" · 🔗 {display_source_label(item.get('url', ''))}" if item.get('url') else "")
            + (f" · {item.get('score', 0)}점" if item.get('score') else "")
        )

        # 📌 한 줄 핵심
        _one = _note_one_line(item)
        if _one:
            st.markdown(
                f"<div style='background:#eff6ff;border-left:4px solid #2563eb;border-radius:8px;"
                f"padding:12px 16px;margin:8px 0;font-size:1.05em;'>📌 {_one}</div>",
                unsafe_allow_html=True)

        # 🧠 핵심 개념
        _cons = _note_concepts(item)
        if _cons:
            st.markdown("#### 🧠 핵심 개념")
            st.markdown(" ".join(f"`{c}`" for c in _cons), unsafe_allow_html=True)

        # 🔗 연결된 지식
        st.markdown("#### 🔗 연결된 지식")
        _lk1, _lk2 = st.columns(2)
        with _lk1:
            _proj = item.get("project", "")
            st.markdown(f"**📁 프로젝트:** {_proj or '없음'}")
            if _proj and _proj not in ("기본 프로젝트", "없음"):
                if st.button("프로젝트 상세 열기", key="archive_goto_proj"):
                    st.session_state["ep_jump_entity"] = _proj
                    st.query_params["page"] = "projects"
                    st.rerun()
        with _lk2:
            _nid = item.get("id")
            _rel_tasks = [t for t in st.session_state.get("tasks", [])
                          if _nid in (t.get("linked_note_ids", []) or [])
                          or t.get("source_note_id") == _nid]
            st.markdown(f"**✅ 연결된 작업:** {len(_rel_tasks)}개")
            for _t in _rel_tasks[:5]:
                st.caption(f"· {_t.get('title', '')} ({_t.get('status', '')})")

        # 🪢 관련 메모 추천 (공유 개념 기반)
        if _cons and get_setting("feat_related_notes"):
            _conset = set(_cons)
            _related = []
            for _on in _all_notes:
                if _on.get("id") == _nid:
                    continue
                _shared = _conset & set(_note_concepts(_on))
                if _shared:
                    _related.append((_on, len(_shared), _shared))
            _related.sort(key=lambda x: x[1], reverse=True)
            if _related:
                st.markdown("#### 🪢 관련 메모 추천")
                for _on, _sc, _sh in _related[:5]:
                    _oicon, _ = _note_meta(_on)
                    rc1, rc2 = st.columns([5, 1])
                    with rc1:
                        st.markdown(f"{_oicon} **{_on.get('title', '제목 없음')}**")
                        st.caption("공유 개념: " + " ".join(f"`{c}`" for c in list(_sh)[:5]))
                    with rc2:
                        if st.button("열기", key=f"archive_rel_{_on.get('id')}"):
                            st.session_state["archive_open_note_id"] = _on.get("id")
                            st.rerun()

        # 📚 원문 (접힘)
        _orig = (item.get("original_text") or "").strip()
        _body = (item.get("note") or "").strip()
        with st.expander("📚 원문 / 메모 본문 보기", expanded=False):
            if _body:
                st.markdown("**📝 내 메모**")
                render_readable_markdown(_body)
            if _orig:
                st.markdown("**📄 원문**")
                render_readable_markdown(_orig, max_chars=8000)
            if not _body and not _orig:
                st.caption("저장된 본문이 없어요.")

        # 🤖 지식 AI 질문 연결
        if st.button("🤖 이 메모로 지식 AI에 질문하기", key="archive_goto_ai", type="primary"):
            st.session_state["pka_query"] = f"'{item.get('title', '')}' 메모 내용을 정리해줘"
            st.query_params["page"] = "ai"
            st.rerun()

        # ✏️ 편집 / 🗑️ 삭제
        with st.expander("✏️ 편집 / 🗑️ 삭제", expanded=False):
            original_index = st.session_state.archive_notes.index(item)
            title_key = f"archive_note_title_{original_index}"
            edit_key = f"archive_note_{original_index}"
            tags_key = f"archive_note_tags_{original_index}"
            new_tags_key = f"archive_note_new_tags_{original_index}"
            tag_options_for_edit = get_tag_edit_options(item)

            st.text_input("제목 수정", value=item.get("title", ""), key=title_key)
            st.multiselect(
                "기존 태그 선택/삭제", options=tag_options_for_edit,
                default=[tag for tag in item.get("tags", []) if tag in tag_options_for_edit],
                key=tags_key, help="기존 기록의 태그를 선택/해제할 수 있어요.")
            st.text_input("새 태그 추가", placeholder="예: 맛집후보, 재확인필요 (쉼표로 여러 개)",
                          key=new_tags_key, help="입력 후 아래 저장 버튼을 눌러야 반영돼요.")
            st.text_area(
                "저장된 메모 수정",
                value=item.get("note", ""),
                height=280,
                key=edit_key,
                help="마크다운을 지원해요. 예: ## 제목, - 목록, - [ ] 체크, **강조**, > 인용"
            )
            fav_label = "⭐ 즐겨찾기 해제" if item.get("favorite", False) else "☆ 즐겨찾기"
            st.button(fav_label, key=f"favorite_archive_note_{original_index}",
                      use_container_width=True, on_click=toggle_archive_favorite, args=(original_index,))
            if st.button("💾 제목/태그/메모 수정 저장", key=f"save_archive_note_{original_index}",
                         use_container_width=True):
                update_archive_note_and_tags(original_index, edit_key, tags_key, new_tags_key, title_key)
                _flash("수정한 메모를 저장했어요.")
                st.rerun()
            if st.button("🗑️ 이 메모 삭제", key=f"delete_archive_note_{original_index}",
                         use_container_width=True):
                delete_archive_note(original_index)
                st.session_state["archive_open_note_id"] = None
                st.rerun()
        st.stop()

    # ════════════════ 카드 목록 모드 ════════════════
    st.markdown("## 📚 지식 라이브러리")
    st.caption("쌓인 메모·연구노트를 한 곳에서 둘러보는 곳이에요. "
               "전체·📁프로젝트별·📅날짜별·🏷태그별·🧠개념별로 탐색하고, 카드를 열면 한 줄 핵심·개념·연결·관련 메모를 봐요.")
    if st.session_state.get("archive_deleted"):
        st.success("저장된 메모를 삭제했어요.")
        st.session_state["archive_deleted"] = False

    # ── 📊 라이브러리 대시보드 — '내 모든 기록이 모이는 도서관' 느낌 ──
    _lib_n_notes = len(_all_notes)
    _lib_n_proj = len([p for p in st.session_state.get("projects", [])
                       if isinstance(p, dict) and _clean_text_value(p.get("name")).strip()])
    _lib_n_con = len({
        _clean_text_value(l.get("concept")).strip()
        for l in st.session_state.get("note_concept_links", [])
        if _clean_text_value(l.get("concept")).strip()
    } | {
        _clean_text_value(c.get("name")).strip()
        for c in st.session_state.get("pkm_custom_concepts", [])
        if isinstance(c, dict) and _clean_text_value(c.get("name")).strip()
    })
    _lib_n_tag = len({
        str(t).replace("#", "").strip()
        for n in _all_notes for t in (n.get("tags", []) or []) if str(t).strip()
    })
    _ls = [("📚", "메모", _lib_n_notes), ("🪐", "프로젝트", _lib_n_proj),
           ("🧠", "개념", _lib_n_con), ("🏷", "태그", _lib_n_tag)]
    _ls_cols = st.columns(4)
    for _lc, (_lem, _lnm, _lcnt) in zip(_ls_cols, _ls):
        with _lc:
            st.markdown(
                f"<div style='text-align:center;background:#f8fafc;border:1px solid #e2e8f0;"
                f"border-radius:12px;padding:12px 8px;'>"
                f"<div style='font-size:1.3em'>{_lem}</div>"
                f"<div style='font-size:1.5rem;font-weight:900;color:#6366f1'>{_lcnt}</div>"
                f"<div style='color:#64748b;font-size:0.85em'>{_lnm}</div></div>",
                unsafe_allow_html=True)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    _fc1, _fc2 = st.columns([3, 1])
    with _fc1:
        search_query = st.text_input("🔍 아카이브 검색", placeholder="제목, URL, 태그, 메모 내용, 개념으로 검색")
    with _fc2:
        only_fav = st.checkbox("⭐ 즐겨찾기만", value=False)

    notes_to_show = list(_all_notes)
    if search_query.strip():
        q = search_query.strip().lower()
        notes_to_show = [
            note for note in notes_to_show
            if q in str(note.get("title", "")).lower()
            or q in str(note.get("url", "")).lower()
            or q in str(note.get("note", "")).lower()
            or q in str(note.get("project", "")).lower()
            or q in str(note.get("section", "")).lower()
            or q in str(note.get("step", "")).lower()
            or q in str(note.get("original_text", "")).lower()
            or q in " ".join([str(t) for t in note.get("tags", [])]).lower()
            or q in " ".join([str(c) for c in _note_concepts(note)]).lower()
        ]
    if only_fav:
        notes_to_show = [note for note in notes_to_show if note.get("favorite", False)]

    if not notes_to_show:
        st.info("아직 저장된 메모가 없어요. 분석 결과 하단에서 메모를 저장해보세요.")
        st.stop()

    st.caption(f"총 {len(notes_to_show)}개")

    # 카드 렌더 헬퍼 (탐색 축마다 재사용)
    def _render_note_cards(_notes, _kp=""):
        _notes = sorted(_notes, key=lambda n: str(n.get("saved_at", "")), reverse=True)
        _cards_per_row = 2
        for _row_start in range(0, len(_notes), _cards_per_row):
            _row_notes = _notes[_row_start:_row_start + _cards_per_row]
            _cols = st.columns(_cards_per_row)
            for _col, item in zip(_cols, _row_notes):
                with _col:
                    with st.container(border=True):
                        _icon, _label = _note_meta(item)
                        _star = "⭐ " if item.get("favorite") else ""
                        st.markdown(f"{_star}{_icon} **{item.get('title', '제목 없음')}**")
                        _crumb = _crumb_of(item)
                        st.caption(
                            f"{_label} · 📅 {str(item.get('saved_at', ''))[:10]}"
                            + (f" · 📁 {_crumb}" if _crumb else ""))
                        _one = _note_one_line(item)
                        if _one:
                            st.markdown(f"<div style='color:#475569;font-size:0.9em;min-height:38px'>{_one}</div>",
                                        unsafe_allow_html=True)
                        _cons = _note_concepts(item)
                        if _cons:
                            st.markdown(
                                " ".join(f"`{c}`" for c in _cons[:5])
                                + (f" +{len(_cons) - 5}" if len(_cons) > 5 else ""))
                        if st.button("📖 열기", key=f"libcard_{_kp}_{item.get('id', _row_start)}_{_row_start}",
                                     use_container_width=True):
                            st.session_state["archive_open_note_id"] = item.get("id")
                            st.rerun()

    # ── 탐색 축 (읽기 허브의 핵심) ──
    _lib_axis = st.radio(
        "탐색 축", ["▶ 전체", "📁 프로젝트별", "📅 날짜별", "🏷 태그별", "🧠 개념별"],
        horizontal=True, key="lib_axis", label_visibility="collapsed",
    )
    if _lib_axis == "▶ 전체":
        _render_note_cards(notes_to_show, "all")

    elif _lib_axis == "📁 프로젝트별":
        _by_proj = {}
        for _n in notes_to_show:
            _pk = _clean_text_value(_n.get("project")).strip() or "미배정"
            _by_proj.setdefault(_pk, []).append(_n)
        # 메모 많은 프로젝트 먼저, 미배정은 맨 뒤
        _proj_order = sorted(_by_proj.keys(), key=lambda k: (k == "미배정", -len(_by_proj[k])))
        for _pk in _proj_order:
            st.markdown(f"#### 🪐 {_pk} · {len(_by_proj[_pk])}개")
            _render_note_cards(_by_proj[_pk], f"proj_{_pk}")
            st.divider()

    elif _lib_axis == "📅 날짜별":
        _by_month = {}
        for _n in notes_to_show:
            _mk = str(_n.get("saved_at", ""))[:7] or "날짜 없음"
            _by_month.setdefault(_mk, []).append(_n)
        for _mk in sorted(_by_month.keys(), reverse=True):
            st.markdown(f"#### 📅 {_mk} · {len(_by_month[_mk])}개")
            _render_note_cards(_by_month[_mk], f"month_{_mk}")
            st.divider()

    elif _lib_axis == "🏷 태그별":
        _all_tags = sorted({
            str(t).replace("#", "").strip()
            for _n in notes_to_show for t in (_n.get("tags", []) or [])
            if str(t).strip()
        })
        if not _all_tags:
            st.info("아직 태그가 달린 메모가 없어요.")
        else:
            _pick_tag = st.selectbox("🏷 태그 선택", _all_tags, key="lib_tag_pick")
            _tag_notes = [
                _n for _n in notes_to_show
                if _pick_tag in [str(t).replace("#", "").strip() for t in (_n.get("tags", []) or [])]
            ]
            st.caption(f"#{_pick_tag} · {len(_tag_notes)}개")
            _render_note_cards(_tag_notes, f"tag_{_pick_tag}")

    elif _lib_axis == "🧠 개념별":
        _all_cons = sorted({
            _c for _n in notes_to_show for _c in _note_concepts(_n) if str(_c).strip()
        })
        if not _all_cons:
            st.info("아직 개념이 연결된 메모가 없어요.")
        else:
            _pick_con = st.selectbox("🧠 개념 선택", _all_cons, key="lib_con_pick")
            _con_notes = [_n for _n in notes_to_show if _pick_con in _note_concepts(_n)]
            st.caption(f"🧠 {_pick_con} · {len(_con_notes)}개")
            _render_note_cards(_con_notes, f"con_{_pick_con}")
    st.stop()

if menu == "개념 라이브러리":
    st.markdown("## 🧠 개념 라이브러리")
    st.caption("내 지식을 잇는 개념을 한 곳에서 관리해요. "
               "이름 변경은 **별칭**으로, 삭제는 **숨기기**로 — 비파괴적으로 안전하게 정리해요.")

    _cl_links = st.session_state.get("note_concept_links", [])
    _cl_pkm = [c for c in st.session_state.get("pkm_custom_concepts", []) if isinstance(c, dict)]
    _cl_notes = st.session_state.get("archive_notes", [])
    _cl_hidden = set(st.session_state.get("hidden_concepts", []))

    # canonical 기준 집계
    _cl_info = {}
    def _cl_get(_canon):
        return _cl_info.setdefault(_canon, {"note_ids": set(), "created": ""})
    for _l in _cl_links:
        _canon = canonical_concept(_l.get("concept"))
        if _canon and _l.get("note_id"):
            _cl_get(_canon)["note_ids"].add(_l.get("note_id"))
    for _n in _cl_notes:
        for _cc in (_n.get("concepts", []) or []):
            _canon = canonical_concept(_cc)
            if _canon and _n.get("id"):
                _cl_get(_canon)["note_ids"].add(_n.get("id"))
    for _c in _cl_pkm:
        _canon = canonical_concept(_c.get("name"))
        if _canon:
            _ci = _cl_get(_canon)
            if not _ci["created"]:
                _ci["created"] = _clean_text_value(_c.get("created_at")).strip()

    _note_by_id = {n.get("id"): n for n in _cl_notes if isinstance(n, dict)}

    def _cl_projects(_canon):
        return concept_projects(_canon)
    def _cl_scope_label(_canon):
        _sc = concept_scope(_canon)
        if _sc in ("shared", "global"):
            return "🌍 여러 프로젝트에서 사용 중", _sc
        return "⚪ 한 프로젝트", _sc

    # ── 개념 상세 (허브 뷰) ──
    _cl_open = _clean_text_value(st.session_state.get("concept_lib_open")).strip()
    if _cl_open and _cl_open in _cl_info:
        _ci = _cl_info[_cl_open]
        _projs = sorted(_cl_projects(_cl_open))
        _lbl, _sc = _cl_scope_label(_cl_open)
        if st.button("← 목록으로", key="cl_back"):
            st.session_state["concept_lib_open"] = None
            st.rerun()
        st.markdown(f"### 🧠 {_cl_open}")
        _d1, _d2, _d3 = st.columns(3)
        _d1.metric("연결 메모", f"{len(_ci['note_ids'])}개")
        _d2.metric("연결 프로젝트", f"{len(_projs)}개")
        _d3.metric("상태", "공유" if _sc != "owned" else "단독")
        st.caption(_lbl + f" · scope = {_sc}")
        if _projs:
            st.markdown("**🪐 사용 중인 프로젝트**")
            st.markdown(" ".join(
                f"<span style='display:inline-block;background:#ede9fe;color:#6d28d9;"
                f"border-radius:999px;padding:3px 10px;margin:2px;font-size:0.85em'>🪐 {p}</span>"
                for p in _projs), unsafe_allow_html=True)
        st.markdown("**📝 연결 메모 보기**")
        _linked = [_note_by_id[i] for i in _ci["note_ids"] if i in _note_by_id]
        _linked = sorted(_linked, key=lambda n: str(n.get("saved_at", "")), reverse=True)
        if _linked:
            for _li, _ln in enumerate(_linked[:20]):
                with st.container(border=True):
                    st.markdown(f"📝 **{_clean_text_value(_ln.get('title')).strip() or '제목 없음'}**")
                    st.caption(f"📁 {_clean_text_value(_ln.get('project')).strip() or '미배정'} · 📅 {str(_ln.get('saved_at',''))[:10]}")
                    if st.button("열기", key=f"cl_open_note_{_ln.get('id')}_{_li}", use_container_width=True):
                        st.session_state["archive_open_note_id"] = _ln.get("id")
                        st.query_params["page"] = "archive"
                        st.rerun()
            if len(_linked) > 20:
                st.caption(f"외 {len(_linked)-20}개 메모가 더 있어요.")
        else:
            st.caption("아직 연결된 메모가 없어요.")
        st.stop()

    # ── 목록 모드 ──
    _cl_show_hidden = st.toggle("🙈 숨긴 개념 보기", value=False, key="cl_show_hidden")
    _cl_names = [c for c in _cl_info.keys()
                 if _cl_show_hidden or c not in _cl_hidden]
    st.caption(f"총 {len(_cl_names)}개 개념" + (f" · 숨김 {len(_cl_hidden)}개" if _cl_hidden else ""))

    _f1, _f2, _f3 = st.columns([2, 1, 1])
    with _f1:
        _cl_q = st.text_input("🔍 개념 검색", key="cl_search", placeholder="개념명으로 검색")
    with _f2:
        _cl_sort = st.selectbox("정렬", ["연결 많은순", "이름순", "프로젝트 많은순"], key="cl_sort")
    with _f3:
        _cl_scope_f = st.selectbox("범위", ["전체", "🌍 공유", "⚪ 단독"], key="cl_scope_f")

    # 필터·정렬
    _rows = []
    for _canon in _cl_names:
        if _cl_q.strip() and _cl_q.strip().lower() not in _canon.lower():
            continue
        _projs = _cl_projects(_canon)
        _sc = concept_scope(_canon)
        if _cl_scope_f == "🌍 공유" and _sc == "owned":
            continue
        if _cl_scope_f == "⚪ 단독" and _sc != "owned":
            continue
        _rows.append((_canon, len(_cl_info[_canon]["note_ids"]), len(_projs), _sc))
    if _cl_sort == "연결 많은순":
        _rows.sort(key=lambda r: -r[1])
    elif _cl_sort == "이름순":
        _rows.sort(key=lambda r: r[0])
    else:
        _rows.sort(key=lambda r: -r[2])

    if not _rows:
        st.info("표시할 개념이 없어요. 메모를 쓰면 개념이 자동으로 쌓여요.")
        st.stop()

    _LIMIT = 50
    _selected = []
    for _canon, _nc, _pc, _sc in _rows[:_LIMIT]:
        _c1, _c2 = st.columns([0.06, 0.94])
        with _c1:
            if st.checkbox("", key=f"cl_chk_{_canon}", label_visibility="collapsed"):
                _selected.append(_canon)
        with _c2:
            _scope_txt = "🌍 여러 프로젝트에서 사용 중" if _sc != "owned" else "⚪ 한 프로젝트"
            _hid = " · 🙈 숨김" if _canon in _cl_hidden else ""
            _lc, _rc = st.columns([0.8, 0.2])
            with _lc:
                st.markdown(
                    f"**🧠 {_canon}**  \n"
                    f"<span style='color:#64748b;font-size:0.85em'>메모 {_nc} · 프로젝트 {_pc} · {_scope_txt}{_hid}</span>",
                    unsafe_allow_html=True)
            with _rc:
                if st.button("🔍 상세", key=f"cl_detail_{_canon}", use_container_width=True):
                    st.session_state["concept_lib_open"] = _canon
                    st.rerun()
    if len(_rows) > _LIMIT:
        st.caption(f"상위 {_LIMIT}개만 표시 중 · 검색으로 좁혀보세요 (총 {len(_rows)}개)")

    # ── 선택 항목 일괄 액션 ──
    st.divider()
    if not _selected:
        st.caption("☑ 개념을 선택하면 이름 변경·병합·숨기기·공유 전환을 할 수 있어요.")
    else:
        st.markdown(f"**선택한 {len(_selected)}개 개념 작업**")
        _act = st.radio("작업 선택", ["✏️ 이름 변경(별칭)", "🔗 병합", "🙈 숨기기", "🌍 공유로 전환"],
                        horizontal=True, key="cl_action")
        if _act == "✏️ 이름 변경(별칭)":
            st.caption("비파괴 — 새 이름을 대표로 하고, 기존 이름은 별칭으로 연결돼요. (1개 선택 시 권장)")
            _newname = st.text_input("새 이름", key="cl_rename_new")
            if st.button("이름 변경", key="cl_do_rename", type="primary", disabled=not _newname.strip()):
                _n = 0
                for _old in _selected:
                    _n += add_concept_aliases(_newname.strip(), [_old])
                save_persisted_data()
                _flash(f"'{_newname.strip()}'(으)로 이름 변경(별칭 {_n}개 연결)했어요.")
                st.rerun()
        elif _act == "🔗 병합":
            _tgt = st.text_input("이 이름으로 합치기 (대표 개념)", key="cl_merge_tgt")
            if _tgt.strip():
                _imp = concept_merge_impact(_selected)
                st.caption(f"미리보기 — 영향: 메모 {_imp['notes']} · 작업 {_imp['tasks']} · 관계 {_imp['rels']}")
            if st.button("병합", key="cl_do_merge", type="primary", disabled=not _tgt.strip()):
                for _old in _selected:
                    add_concept_aliases(_tgt.strip(), [_old])
                save_persisted_data()
                _flash(f"{len(_selected)}개를 '{_tgt.strip()}'(으)로 병합했어요.")
                st.rerun()
        elif _act == "🙈 숨기기":
            st.caption("삭제가 아니라 숨김이에요. 위 '숨긴 개념 보기'에서 언제든 복구할 수 있어요.")
            if st.button("선택 개념 숨기기", key="cl_do_hide", type="primary"):
                _h = set(st.session_state.get("hidden_concepts", []))
                _h |= set(_selected)
                st.session_state["hidden_concepts"] = list(_h)
                save_persisted_data()
                _flash(f"{len(_selected)}개 개념을 숨겼어요.")
                st.rerun()
        else:  # 공유로 전환
            st.caption("이 개념을 여러 프로젝트의 연결 근거(공유 개념)로 고정해요. (이동 시 한 프로젝트로 끌려가지 않음)")
            if st.button("공유 개념으로 전환", key="cl_do_shared", type="primary"):
                _names = {canonical_concept(x) for x in _selected}
                for _c in st.session_state.get("pkm_custom_concepts", []):
                    if isinstance(_c, dict) and canonical_concept(_c.get("name")) in _names:
                        _c["scope"] = "shared"
                # pkm에 없던 개념은 새로 등록(scope만)
                _existing = {canonical_concept(c.get("name")) for c in st.session_state.get("pkm_custom_concepts", []) if isinstance(c, dict)}
                for _nm in _names:
                    if _nm and _nm not in _existing:
                        st.session_state.setdefault("pkm_custom_concepts", []).append(
                            {"name": _nm, "folder": "내 개념", "scope": "shared",
                             "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
                save_persisted_data()
                _flash(f"{len(_selected)}개를 공유 개념으로 전환했어요.")
                st.rerun()
    st.stop()

if menu == "프로젝트":
    render_project_page()
    st.stop()

if menu == "작업 관리":
    render_task_page()
    st.stop()

if menu == "지식 맵":
    render_knowledge_map_page()
    st.stop()

if menu == "데이터 관리":
    import plotly.graph_objects as _pgo
    st.markdown("## 🗄️ 데이터 백업·관리 · ERD")
    st.caption("내 데이터를 백업/복원하고, 엔티티(프로젝트·개념·작업·메모·태그)를 연결·시각화해요.")

    st.markdown("### 💾 백업 · 내보내기 / 가져오기")
    if _sb_client():
        st.success(
            "☁️ **Supabase 클라우드 저장 연결됨** — 작성한 데이터는 자동으로 보존돼요. "
            "그래도 중요한 시점엔 아래 '내보내기'로 내 PC에 한 부 받아두면 안전해요."
        )
        st.caption("🛠 데이터 저장/로드 진단은 **⚙️ 관리 → 설정 → 맨 아래 개발자 진단**에서 볼 수 있어요.")
    else:
        st.error(
            "⚠️ **중요** — 클라우드 저장(Supabase)이 아직 연결되지 않았어요. "
            "이 상태에서는 **새로 배포될 때마다 서버 데이터가 초기화**될 수 있어요.  \n"
            "**내 PC로 '내보내기(다운로드)' 해두는 게 유일하게 안전한 방법이에요.**"
        )
        if st.button("🔄 연결 다시 시도 (캐시 비우기)", key="sb_retry"):
            _sb_client.clear()
            st.rerun()

    import json as _bk_json
    _bk_data = collect_persisted_data()
    _bk_notes = len(_bk_data.get("archive_notes", []))
    _bk_str = _bk_json.dumps(_bk_data, ensure_ascii=False, indent=2)
    _bk_c1, _bk_c2 = st.columns(2)
    with _bk_c1:
        st.markdown("**⬇️ 내보내기 (내 PC로 저장)**")
        st.caption(f"현재 메모 {_bk_notes}개 · 개념·태그·프로젝트 전체 포함")
        st.download_button(
            "💾 백업 파일 다운로드",
            data=_bk_str.encode("utf-8"),
            file_name=f"jium_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
            type="primary",
        )
        st.caption("💡 배포(업데이트) 전에 항상 한 번 받아두세요.")
    with _bk_c2:
        st.markdown("**⬆️ 가져오기 (백업 복원)**")
        _bk_up = st.file_uploader("백업 .json 파일 선택", type=["json"], key="bk_restore_file")
        _bk_merge = st.checkbox("기존 데이터에 합치기(merge) — 끄면 통째로 교체", value=False, key="bk_merge")
        if _bk_up is not None and st.button("📥 이 파일로 복원", key="bk_do_restore", use_container_width=True):
            try:
                _loaded = _bk_json.loads(_bk_up.getvalue().decode("utf-8"))
                _loaded = normalize_persisted_data(_loaded)
                _list_keys = ("archive_notes", "tasks", "projects", "note_concept_links",
                              "relations", "saved_analyses", "search_history",
                              "pkm_custom_concepts", "project_sections", "project_steps",
                              "entities", "folders")
                if _bk_merge:
                    for _k in _list_keys:
                        _cur = st.session_state.get(_k, []) or []
                        _new = _loaded.get(_k, []) or []
                        _seen = {(_x.get("id") if isinstance(_x, dict) else _x) for _x in _cur}
                        for _x in _new:
                            _xid = _x.get("id") if isinstance(_x, dict) else _x
                            if _xid not in _seen:
                                _cur.append(_x)
                        st.session_state[_k] = _cur
                else:
                    for _k, _v in _loaded.items():
                        st.session_state[_k] = _v
                save_persisted_data()
                st.success(f"복원했어요! 메모 {len(st.session_state.get('archive_notes', []))}개")
                st.rerun()
            except Exception as _e:
                st.error(f"복원 실패: {_e}")

    with st.expander("⚠️ 테스트 데이터 전체 초기화"):
        st.warning("지식 아카이브, 검색 기록, 피드백, 분석 캐시, 초안 캐시가 모두 삭제돼요.")
        st.button(
            "🧹 전체 저장 데이터 초기화",
            key="clear_all_saved_data",
            type="primary",
            use_container_width=True,
            on_click=clear_all_saved_data,
        )
    st.divider()
    st.markdown("### 🔗 엔티티 연결 · ERD")

    # ── 데이터 로드 ──
    _dm_projs    = st.session_state.get("projects", [])
    _dm_notes    = st.session_state.get("archive_notes", [])
    _dm_tasks    = st.session_state.get("tasks", [])
    _dm_links    = st.session_state.get("note_concept_links", [])

    # 개념: build_concept_index + concept_counter 방식 (개념 허브와 동일)
    from collections import Counter as _DMCnt
    _dm_hidden = set(st.session_state.get("hidden_concepts", []))
    _dm_custom_map = {}
    for _c0 in st.session_state.get("pkm_custom_concepts", []):
        _c0d = _c0 if isinstance(_c0, dict) else {"name": str(_c0), "folder": "내 개념"}
        _n0 = _c0d.get("name", "").strip()
        if _n0: _dm_custom_map[_n0] = _c0d

    # concept_counter: 태그 + extract_local_concepts (개념 허브와 동일 로직)
    _dm_con_counter = _DMCnt()
    _dm_items_all = get_all_knowledge_items()
    for _ki in _dm_items_all:
        for _tg in _ki.get("tags", []):
            _tgc = str(_tg).replace("#","").strip()
            if _tgc and _tgc not in _dm_hidden:
                _dm_con_counter[_tgc] += 1
        for _ec in extract_local_concepts(
            str(_ki.get("full_text","")) + " " + str(_ki.get("memo","")),
            _ki.get("tags",[]), limit=8
        ):
            if _ec and _ec not in _dm_hidden:
                _dm_con_counter[_ec] += 1
    # 커스텀 개념 카운트 보정
    for _n0, _c0d in _dm_custom_map.items():
        if _n0 not in _dm_hidden:
            _dm_con_counter[_n0] = max(_dm_con_counter.get(_n0, 0), 1) + 3

    _pfmap = st.session_state.get("pkm_concept_folders", {})
    _dm_concepts = []
    for _cname, _ccnt in _dm_con_counter.most_common(500):
        _existing = _dm_custom_map.get(_cname)
        _dm_concepts.append({
            "name": _cname,
            "folder": _pfmap.get(_cname, _existing.get("folder","자동") if _existing else "자동"),
            "description": _existing.get("description","") if _existing else "",
            "count": _ccnt,
            "is_custom": _cname in _dm_custom_map,
        })

    _dm_tag_cnt = _DMCnt()
    for _n in _dm_notes:
        for _t in _n.get("tags", []):
            _dm_tag_cnt[str(_t).replace("#","").strip()] += 1
    _dm_tags = [{"name": k, "count": v} for k, v in _dm_tag_cnt.most_common()]

    _dm_tab1, _dm_tab2, _dm_tab3, _dm_tab4, _dm_tab5, _dm_tab6 = st.tabs(
        ["📋 테이블 편집", "🔗 관계 관리", "🕸️ ERD 뷰", "⚡ 빠른 작업", "🗄️ 엔터티 DB", "🧠 개념 병합"])

    # ═══════════════════════════════════════════
    # TAB 1 — 테이블 편집 (st.data_editor)
    # ═══════════════════════════════════════════
    with _dm_tab1:
        _ent_sel = st.radio("엔티티 선택", ["📁 프로젝트", "📝 지식 메모", "✅ 작업", "🧠 개념", "🏷️ 태그"],
                            horizontal=True, key="dm_ent_sel")
        st.divider()

        # ── 선택형 컬럼 옵션 (앱 전역 값과 통일) ──
        # 기본값 + 커스텀(custom_select_options) 병합은 컴포넌트가 처리
        _OPT_PROJ_STATUS = ["예정", "진행 중", "완료", "보류"]
        _OPT_TASK_STATUS = ["보류", "시작 전", "진행 중", "검토 중", "완료"]
        _OPT_PRIORITY    = ["높음", "보통", "낮음"]
        _dm_proj_name_opts = [p.get("name", "") for p in _dm_projs if p.get("name")]
        # 기존 메모·작업에 적힌 프로젝트명도 옵션에 포함 (표시 시 빈칸 방지)
        for _src in (_dm_notes, _dm_tasks):
            for _it in _src:
                _pn = _it.get("project", "")
                if _pn and _pn not in _dm_proj_name_opts:
                    _dm_proj_name_opts.append(_pn)

        if _ent_sel == "📁 프로젝트":
            st.markdown("#### 📁 프로젝트")
            st.caption("셀을 클릭하면 드롭다운이 펼쳐져요. 프로젝트명이 비어 있는 새 행은 저장할 때 자동으로 무시돼요.")
            _opt_proj_status = render_select_property_editor(
                "프로젝트 상태", _OPT_PROJ_STATUS, key="proj_status",
                help="프로젝트가 가질 수 있는 상태 목록")
            _opt_priority = render_select_property_editor(
                "우선순위", _OPT_PRIORITY, key="priority",
                help="모든 엔터티 공통 우선순위")
            import pandas as _pd
            _proj_df = _pd.DataFrame(_dm_projs or [{"name":"(없음)","status":"","priority":"","description":""}])
            _proj_cols = ["name","status","priority","description"]
            for _pc in _proj_cols:
                if _pc not in _proj_df.columns: _proj_df[_pc] = ""
            _proj_df = _clean_editor_dataframe(_proj_df, _proj_cols)
            _edited_proj = st.data_editor(
                _proj_df[_proj_cols].rename(columns={"name":"프로젝트명","status":"상태","priority":"우선순위","description":"설명"}),
                num_rows="dynamic", use_container_width=True, key="dm_proj_editor",
                column_config={
                    "상태": st.column_config.SelectboxColumn("상태", options=_opt_proj_status, help="클릭하면 목록이 펼쳐져요"),
                    "우선순위": st.column_config.SelectboxColumn("우선순위", options=_opt_priority, help="클릭하면 목록이 펼쳐져요"),
                }
            )
            if st.button("💾 프로젝트 저장", key="dm_save_proj", type="primary"):
                _new_projs = _edited_proj.rename(columns={"프로젝트명":"name","상태":"status","우선순위":"priority","설명":"description"}).to_dict("records")
                _new_projs = _clean_editor_records(
                    _new_projs,
                    text_fields=("id", "name", "status", "priority", "description"),
                )
                _new_projs = [p for p in _new_projs if p.get("name") and p.get("name") != "(없음)"]
                for _np in _new_projs:
                    if not any(p.get("id") == _np.get("id") for p in _dm_projs):
                        import uuid as _uuid2
                        _np["id"] = str(_uuid2.uuid4())[:8]
                st.session_state["projects"] = _new_projs
                save_persisted_data(); _flash("저장 완료!"); st.rerun()

        elif _ent_sel == "📝 지식 메모":
            st.markdown("#### 📝 지식 메모")
            import pandas as _pd
            _note_rows = [{"제목": n.get("title",""), "프로젝트": n.get("project",""),
                           "섹션": n.get("section",""), "단계": n.get("step",""),
                           "점수": n.get("score",0), "저장일": n.get("saved_at","")} for n in _dm_notes]
            if not _note_rows: _note_rows = [{"제목":"(없음)","프로젝트":"","섹션":"","단계":"","점수":0,"저장일":""}]
            _opt_note_proj = render_select_property_editor(
                "프로젝트", (_dm_proj_name_opts or [""]), key="note_project",
                on_add=lambda v: create_project(v),
                help="여기서 추가하면 실제 프로젝트로 생성되고 드롭다운에 바로 반영돼요")
            _note_df = _clean_editor_dataframe(_pd.DataFrame(_note_rows), ["제목", "프로젝트", "섹션", "단계", "저장일"])
            _edited_note = st.data_editor(_note_df, num_rows="fixed", use_container_width=True, key="dm_note_editor",
                column_config={
                    "점수": st.column_config.NumberColumn("점수", min_value=0, max_value=100),
                    "프로젝트": st.column_config.SelectboxColumn("프로젝트", options=(_opt_note_proj or [""]), required=False, help="클릭하면 프로젝트 목록이 펼쳐져요"),
                })
            if st.button("💾 메모 저장", key="dm_save_note", type="primary"):
                for _i, _row in _edited_note.iterrows():
                    if _i < len(_dm_notes):
                        _dm_notes[_i]["title"]   = _clean_text_value(_row["제목"])
                        _dm_notes[_i]["project"] = _clean_text_value(_row["프로젝트"])
                        _dm_notes[_i]["section"] = _clean_text_value(_row["섹션"])
                        _dm_notes[_i]["step"]    = _clean_text_value(_row["단계"])
                st.session_state["archive_notes"] = _dm_notes
                save_persisted_data(); _flash("저장 완료!"); st.rerun()

        elif _ent_sel == "✅ 작업":
            st.markdown("#### ✅ 작업")
            import pandas as _pd
            _task_rows = [{"작업명": t.get("title",""), "프로젝트": t.get("project",""),
                           "상태": t.get("status",""), "우선순위": t.get("priority",""),
                           "마감일": t.get("due_date","")} for t in _dm_tasks]
            if not _task_rows: _task_rows = [{"작업명":"(없음)","프로젝트":"","상태":"","우선순위":"","마감일":""}]
            st.caption("셀을 클릭하면 드롭다운이 펼쳐져요. 작업명이 비어 있는 새 행은 저장할 때 자동으로 무시돼요.")
            _opt_task_proj = render_select_property_editor(
                "프로젝트", (_dm_proj_name_opts or [""]), key="task_project",
                on_add=lambda v: create_project(v),
                help="여기서 추가하면 실제 프로젝트로 생성되고 드롭다운에 바로 반영돼요")
            _opt_task_status = render_select_property_editor(
                "작업 상태", _OPT_TASK_STATUS, key="task_status",
                help="작업이 가질 수 있는 상태 목록")
            _opt_task_priority = render_select_property_editor(
                "우선순위", _OPT_PRIORITY, key="priority",
                help="모든 엔터티 공통 우선순위")
            _task_df = _clean_editor_dataframe(_pd.DataFrame(_task_rows), ["작업명", "프로젝트", "상태", "우선순위", "마감일"])
            _edited_task = st.data_editor(_task_df, num_rows="dynamic", use_container_width=True, key="dm_task_editor",
                column_config={
                    "프로젝트": st.column_config.SelectboxColumn("프로젝트", options=(_opt_task_proj or [""]), required=False, help="클릭하면 프로젝트 목록이 펼쳐져요"),
                    "상태": st.column_config.SelectboxColumn("상태", options=_opt_task_status, help="클릭하면 목록이 펼쳐져요"),
                    "우선순위": st.column_config.SelectboxColumn("우선순위", options=_opt_task_priority, help="클릭하면 목록이 펼쳐져요"),
                })
            if st.button("💾 작업 저장", key="dm_save_task", type="primary"):
                _new_tasks = _edited_task.rename(columns={"작업명":"title","프로젝트":"project","상태":"status","우선순위":"priority","마감일":"due_date"}).to_dict("records")
                _new_tasks = _clean_editor_records(
                    _new_tasks,
                    text_fields=("id", "title", "project", "status", "priority", "due_date"),
                )
                _new_tasks = [t for t in _new_tasks if t.get("title") and t.get("title") != "(없음)"]
                st.session_state["tasks"] = _new_tasks
                save_persisted_data(); _flash("저장 완료!"); st.rerun()

        elif _ent_sel == "🧠 개념":
            st.markdown("#### 🧠 개념 관리")
            import pandas as _pd

            _con_subtab1, _con_subtab2, _con_subtab3 = st.tabs(
                ["✅ 내 개념 (직접 추가)", "🤖 AI 추출 개념 (선택 등록)", "🔀 병합 뷰 (전체)"]
            )

            # ── 서브탭 1: 내 개념 ──────────────────────
            with _con_subtab1:
                st.caption(f"직접 추가한 개념 {sum(1 for c in _dm_concepts if c.get('is_custom'))}개. 자유롭게 수정·삭제·추가 가능.")
                _my_con_rows = [{"개념명": c.get("name",""), "폴더": c.get("folder","내 개념"),
                                  "설명": c.get("description","")}
                                 for c in _dm_concepts if c.get("is_custom")]
                if not _my_con_rows:
                    _my_con_rows = [{"개념명":"","폴더":"내 개념","설명":""}]
                # 폴더 옵션 = 기존 개념 폴더 + 폴더 DB 병합
                _folder_opts = ["내 개념"]
                for _fv in st.session_state.get("pkm_concept_folders", {}).values():
                    if _fv and _fv not in _folder_opts:
                        _folder_opts.append(_fv)
                for _fo in st.session_state.get("folders", []):
                    _fon = _fo.get("name") if isinstance(_fo, dict) else str(_fo)
                    if _fon and _fon not in _folder_opts:
                        _folder_opts.append(_fon)
                _opt_con_folder = render_select_property_editor(
                    "개념 폴더", _folder_opts, key="concept_folder",
                    on_add=lambda v: create_folder(v),
                    help="여기서 추가하면 폴더가 생성되고 드롭다운에 바로 반영돼요")
                _my_con_df = _clean_editor_dataframe(_pd.DataFrame(_my_con_rows), ["개념명", "폴더", "설명"])
                _edited_my = st.data_editor(
                    _my_con_df, num_rows="dynamic", use_container_width=True, key="dm_mycon_editor",
                    column_config={
                        "폴더": st.column_config.SelectboxColumn("폴더", options=(_opt_con_folder or ["내 개념"]), required=False, help="클릭하면 폴더 목록이 펼쳐져요"),
                        "설명": st.column_config.TextColumn("설명"),
                    }
                )
                if st.button("💾 내 개념 저장", key="dm_save_mycon", type="primary"):
                    _edited_my_records = _clean_editor_records(
                        _edited_my.to_dict("records"),
                        text_fields=("개념명", "폴더", "설명"),
                    )
                    _existing_ai = [c for c in st.session_state.get("pkm_custom_concepts",[])
                                    if c.get("name","") not in [r.get("개념명","") for r in _edited_my_records]
                                    and any(x.get("name")==c.get("name") for x in _dm_concepts if not x.get("is_custom"))]
                    _new_my = []
                    _new_fds2 = dict(st.session_state.get("pkm_concept_folders", {}))
                    for _r in _edited_my_records:
                        _nm = _clean_text_value(_r.get("개념명"))
                        if _nm:
                            _fold = _clean_text_value(_r.get("폴더")) or "내 개념"
                            _new_fds2[_nm] = _fold
                            _new_my.append({"name":_nm,"folder":_fold,"description":_clean_text_value(_r.get("설명")),"created_at":""})
                    # AI에서 이미 등록된 개념은 유지
                    _keep_ai = [c for c in st.session_state.get("pkm_custom_concepts",[])
                                if c.get("name") not in [x["name"] for x in _new_my]]
                    st.session_state["pkm_custom_concepts"] = _new_my + _keep_ai
                    st.session_state["pkm_concept_folders"] = _new_fds2
                    save_persisted_data(); _flash(f"{len(_new_my)}개 저장!"); st.rerun()

            # ── 서브탭 2: AI 추출 개념 선택 등록 ──────
            with _con_subtab2:
                _ai_cons = [c for c in _dm_concepts if not c.get("is_custom")]
                st.caption(f"AI가 메모·분석에서 자동 추출한 개념 {len(_ai_cons)}개. 체크해서 내 개념으로 등록하세요.")
                if not _ai_cons:
                    st.info("AI 추출 개념이 없어요. 분석을 실행하면 자동으로 추출돼요.")
                else:
                    _ai_rows = [{"✓ 등록": False,
                                  "개념명": c.get("name",""),
                                  "폴더": c.get("folder","자동"),
                                  "설명": "",
                                  "연결수": c.get("count",0)}
                                 for c in _ai_cons]
                    _ai_df = _clean_editor_dataframe(_pd.DataFrame(_ai_rows), ["개념명", "폴더", "설명"])
                    _edited_ai = st.data_editor(
                        _ai_df, num_rows="fixed", use_container_width=True, key="dm_aicon_editor",
                        disabled=["연결수"],
                        column_config={
                            "✓ 등록": st.column_config.CheckboxColumn("등록", help="체크하면 내 개념으로 저장"),
                            "폴더": st.column_config.TextColumn("폴더"),
                            "설명": st.column_config.TextColumn("설명"),
                            "연결수": st.column_config.NumberColumn("연결수", disabled=True),
                        }
                    )
                    _sel_ai = _edited_ai[_edited_ai["✓ 등록"] == True]
                    _ca1, _ca2, _ca3 = st.columns(3)
                    with _ca1:
                        if st.button(f"✅ 선택한 {len(_sel_ai)}개 내 개념으로 등록", key="dm_add_ai_sel",
                                     type="primary", disabled=len(_sel_ai)==0):
                            _existing_custom = list(st.session_state.get("pkm_custom_concepts",[]))
                            _existing_names = [c.get("name") if isinstance(c,dict) else str(c) for c in _existing_custom]
                            _new_fds3 = dict(st.session_state.get("pkm_concept_folders",{}))
                            _added = 0
                            for _, _ar in _sel_ai.iterrows():
                                _anm = _clean_text_value(_ar["개념명"])
                                _afold = _clean_text_value(_ar["폴더"]) or "내 개념"
                                if _anm and _anm not in _existing_names:
                                    _existing_custom.append({"name":_anm,"folder":_afold,"description":_clean_text_value(_ar.get("설명")),"created_at":""})
                                    _new_fds3[_anm] = _afold
                                    _added += 1
                                elif _anm in _existing_names:
                                    for _ec in _existing_custom:
                                        if isinstance(_ec,dict) and _ec.get("name")==_anm:
                                            _ec["folder"] = _afold
                            st.session_state["pkm_custom_concepts"] = _existing_custom
                            st.session_state["pkm_concept_folders"] = _new_fds3
                            save_persisted_data(); _flash(f"{_added}개 등록 완료!"); st.rerun()
                    with _ca2:
                        if st.button(f"🗑️ 선택한 {len(_sel_ai)}개 숨기기 (AI에서 제외)", key="dm_hide_ai_sel",
                                     disabled=len(_sel_ai)==0):
                            _h3 = list(set(st.session_state.get("hidden_concepts",[])) | set(_sel_ai["개념명"].tolist()))
                            st.session_state["hidden_concepts"] = _h3
                            save_persisted_data(); _flash("숨김 처리 완료!"); st.rerun()
                    with _ca3:
                        if st.button("🔄 전체 AI 개념 모두 등록", key="dm_add_all_ai"):
                            _existing_c2 = list(st.session_state.get("pkm_custom_concepts",[]))
                            _ex_names2 = [c.get("name") if isinstance(c,dict) else str(c) for c in _existing_c2]
                            _fds4 = dict(st.session_state.get("pkm_concept_folders",{}))
                            for _ac2 in _ai_cons:
                                _an2 = _clean_text_value(_ac2.get("name"))
                                if _an2 and _an2 not in _ex_names2:
                                    _fold2 = _clean_text_value(_ac2.get("folder")) or "내 개념"
                                    _existing_c2.append({"name":_an2,"folder":_fold2,"description":"","created_at":""})
                                    _fds4[_an2] = _fold2
                            st.session_state["pkm_custom_concepts"] = _existing_c2
                            st.session_state["pkm_concept_folders"] = _fds4
                            save_persisted_data(); _flash("전체 등록 완료!"); st.rerun()

            # ── 서브탭 3: 병합 뷰 (전체) ────────────────
            with _con_subtab3:
                st.caption(f"내 개념 + AI 개념 전체 {len(_dm_concepts)}개. 모두 수정 가능.")
                _all_rows = [{"개념명": c.get("name",""), "폴더": c.get("folder","자동"),
                               "설명": c.get("description",""),
                               "출처": "직접" if c.get("is_custom") else "AI",
                               "연결수": c.get("count",0)}
                              for c in _dm_concepts]
                if not _all_rows: _all_rows = [{"개념명":"","폴더":"자동","설명":"","출처":"","연결수":0}]
                _all_df = _clean_editor_dataframe(_pd.DataFrame(_all_rows), ["개념명", "폴더", "설명", "출처"])
                _edited_all = st.data_editor(
                    _all_df, num_rows="dynamic", use_container_width=True, key="dm_allcon_editor",
                    disabled=["출처","연결수"],
                    column_config={
                        "출처": st.column_config.TextColumn("출처", disabled=True),
                        "연결수": st.column_config.NumberColumn("연결수", disabled=True),
                    }
                )
                if st.button("💾 전체 개념 저장", key="dm_save_all_con", type="primary"):
                    _new_all = []
                    _fds5 = dict(st.session_state.get("pkm_concept_folders",{}))
                    for _r5 in _clean_editor_records(_edited_all.to_dict("records"), text_fields=("개념명", "폴더", "설명", "출처")):
                        _nm5 = _clean_text_value(_r5.get("개념명"))
                        if _nm5:
                            _f5 = _clean_text_value(_r5.get("폴더")) or "자동"
                            _fds5[_nm5] = _f5
                            _new_all.append({"name":_nm5,"folder":_f5,"description":_clean_text_value(_r5.get("설명")),"created_at":""})
                    # 삭제된 것들은 hidden 처리
                    _prev_names = {c.get("name") for c in _dm_concepts}
                    _new_names  = {r["name"] for r in _new_all}
                    _removed = _prev_names - _new_names
                    if _removed:
                        _h5 = list(set(st.session_state.get("hidden_concepts",[])) | _removed)
                        st.session_state["hidden_concepts"] = _h5
                    st.session_state["pkm_custom_concepts"] = _new_all
                    st.session_state["pkm_concept_folders"] = _fds5
                    save_persisted_data(); _flash(f"{len(_new_all)}개 저장! {len(_removed)}개 숨김 처리"); st.rerun()

        else:  # 태그
            st.markdown("#### 🏷️ 태그")
            import pandas as _pd
            _tag_df = _pd.DataFrame(_dm_tags or [{"name":"(없음)","count":0}])
            st.dataframe(_tag_df.rename(columns={"name":"태그명","count":"사용 횟수"}), use_container_width=True)
            st.caption("태그는 메모에서 직접 수정해요. 아래에서 태그 일괄 이름 변경·삭제 가능.")
            _rt1, _rt2, _rt3 = st.columns(3)
            with _rt1:
                _old_tag = st.selectbox("변경할 태그", [t["name"] for t in _dm_tags], key="dm_tag_old")
                _new_tag_name = st.text_input("새 이름", key="dm_tag_new")
                if st.button("🔄 이름 변경", key="dm_tag_rename"):
                    if _new_tag_name.strip():
                        for _n2 in st.session_state.get("archive_notes",[]):
                            _n2["tags"] = [_new_tag_name if str(t).replace("#","").strip()==_old_tag else t for t in _n2.get("tags",[])]
                        save_persisted_data(); _flash("변경 완료!"); st.rerun()
            with _rt2:
                _del_tag = st.selectbox("삭제할 태그", [t["name"] for t in _dm_tags], key="dm_tag_del_sel")
                if st.button("🗑️ 태그 삭제", key="dm_tag_del"):
                    for _n2 in st.session_state.get("archive_notes",[]):
                        _n2["tags"] = [t for t in _n2.get("tags",[]) if str(t).replace("#","").strip() != _del_tag]
                    save_persisted_data(); _flash("삭제 완료!"); st.rerun()
            with _rt3:
                _mg1 = st.selectbox("병합 원본", [t["name"] for t in _dm_tags], key="dm_tag_mg1")
                _mg2 = st.selectbox("병합 대상 (원본→대상)", [t["name"] for t in _dm_tags], key="dm_tag_mg2")
                if st.button("🔗 병합", key="dm_tag_merge"):
                    if _mg1 != _mg2:
                        for _n2 in st.session_state.get("archive_notes",[]):
                            _n2["tags"] = [_mg2 if str(t).replace("#","").strip()==_mg1 else t for t in _n2.get("tags",[])]
                        save_persisted_data(); _flash(f"'{_mg1}' → '{_mg2}' 병합 완료!"); st.rerun()

    # ═══════════════════════════════════════════
    # TAB 2 — 관계 관리
    # ═══════════════════════════════════════════
    with _dm_tab2:
        st.markdown("#### 🔗 엔티티 관계 관리")

        # ── AI 관계 추천 ──────────────────────────────────────
        with st.expander("🤖 AI 관계 추천 — 개념들 사이 숨은 연결 찾기", expanded=False):
            st.caption("저장된 개념·메모를 AI가 분석해서 '관련 있을 법한' 관계를 자동 제안해요.")
            _air_cons = [c.get("name") if isinstance(c,dict) else str(c) for c in _dm_concepts if c]
            # 메모 연결 개념도 후보에 포함
            _air_ncl_cons = list({l.get("concept","") for l in st.session_state.get("note_concept_links",[]) if l.get("concept")})
            _air_all = list(dict.fromkeys(_air_cons + _air_ncl_cons))

            if len(_air_all) < 2:
                st.info("개념이 2개 이상 있어야 추천이 가능해요. 메모를 저장하거나 개념을 추가해보세요.")
            else:
                _air_scope = st.multiselect(
                    "분석할 개념 (비어있으면 전체, 최대 20개 권장)",
                    _air_all, key="dm_air_scope"
                )
                _air_targets = _air_scope if _air_scope else _air_all[:20]

                if st.button("🤖 AI로 관계 추천받기", key="dm_air_run", type="primary"):
                    # 개념별 등장 메모 컨텍스트 수집
                    _air_ctx_lines = []
                    for _ac in _air_targets:
                        _ac_notes = []
                        for _nl in st.session_state.get("note_concept_links",[]):
                            if _nl.get("concept") == _ac:
                                _nt = next((n for n in _dm_notes if n.get("id")==_nl.get("note_id")), None)
                                if _nt:
                                    _ac_notes.append(_nt.get("title",""))
                        _air_ctx_lines.append(f"- {_ac}" + (f" (관련 메모: {', '.join(_ac_notes[:3])})" if _ac_notes else ""))
                    _air_sys = """당신은 지식 그래프 전문가입니다. 주어진 개념 목록을 보고 서로 관련 있을 법한 개념 쌍과 관계 유형을 추천하세요.
관계 유형은 반드시 다음 중 하나: 포함, 참조, 반박, 지지, 확장, 연결, 유사, 선행
출력 형식 (각 줄):
개념A | 관계유형 | 개념B | 한줄이유
최대 10개. 한국어로. 설명 없이 위 형식만 출력하세요."""
                    _air_user = "개념 목록:\n" + "\n".join(_air_ctx_lines)
                    with st.spinner("AI가 관계를 분석 중..."):
                        try:
                            _air_result = call_groq_simple(_air_sys, _air_user)
                            # 파싱
                            _air_suggestions = []
                            for _line in _air_result.split("\n"):
                                _parts = [p.strip() for p in _line.split("|")]
                                if len(_parts) >= 3 and _parts[0] and _parts[2]:
                                    _air_suggestions.append({
                                        "source": _parts[0], "rtype": _parts[1],
                                        "target": _parts[2],
                                        "reason": _parts[3] if len(_parts) > 3 else "",
                                    })
                            st.session_state["dm_air_suggestions"] = _air_suggestions
                        except Exception as e:
                            st.error(f"AI 오류: {e}")

                _air_valid_types = {"포함","참조","반박","지지","확장","연결","유사","선행"}

                def _air_rtype(_sg):
                    return _sg["rtype"] if _sg.get("rtype") in _air_valid_types else "연결"

                def _air_rel_exists(_src, _tgt, _rt):
                    # 중복 판단: source + target + relation_type (공통 로직)
                    return any(
                        r.get("source_name") == _src and r.get("target_name") == _tgt
                        and r.get("relation_type") == _rt
                        for r in st.session_state.get("relations", [])
                    )

                def _air_make_rel(_sg):
                    import uuid as _aiuuid
                    return {
                        "id": str(_aiuuid.uuid4())[:8],
                        "source_type": "concept", "source_name": _sg["source"],
                        "target_type": "concept", "target_name": _sg["target"],
                        "relation_type": _air_rtype(_sg),
                        "created_by": "ai_suggestion",
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }

                # 이미 relations에 있는 추천은 화면에서 제외 (개별/전체 동일 기준)
                _air_all = st.session_state.get("dm_air_suggestions", [])
                _air_sugg = [s for s in _air_all
                             if not _air_rel_exists(s["source"], s["target"], _air_rtype(s))]
                # 저장소도 정리(이미 추가된 항목 제거)
                if len(_air_sugg) != len(_air_all):
                    st.session_state["dm_air_suggestions"] = _air_sugg
                if _air_sugg:
                    st.divider()
                    st.markdown(f"**💡 AI 추천 관계 {len(_air_sugg)}개** — 추가할 항목을 선택하세요")
                    for _si, _sg in enumerate(_air_sugg):
                        _sg_rt = _air_rtype(_sg)
                        _ac1, _ac2 = st.columns([5,1])
                        with _ac1:
                            st.markdown(
                                f'<div style="background:#f0f9ff;border-radius:8px;padding:8px 12px;margin-bottom:4px;">'
                                f'<b>{_sg["source"]}</b> '
                                f'<span style="background:#3b82f6;color:white;border-radius:8px;padding:1px 8px;font-size:0.8rem;margin:0 6px;">{_sg_rt}</span> '
                                f'<b>{_sg["target"]}</b>'
                                + (f'<br><span style="font-size:0.8rem;color:#64748b;">{_sg["reason"]}</span>' if _sg.get("reason") else "")
                                + '</div>', unsafe_allow_html=True
                            )
                        with _ac2:
                            if st.button("➕", key=f"dm_air_add_{_sg['source']}_{_sg['target']}_{_sg_rt}", help="이 관계 추가"):
                                if not _air_rel_exists(_sg["source"], _sg["target"], _sg_rt):
                                    st.session_state.setdefault("relations", []).append(_air_make_rel(_sg))
                                    save_persisted_data()
                                # 추천 목록에서 이 항목 제거 (개별 추가 시 카드 즉시 사라짐)
                                st.session_state["dm_air_suggestions"] = [
                                    s for s in st.session_state.get("dm_air_suggestions", [])
                                    if not (s["source"] == _sg["source"] and s["target"] == _sg["target"]
                                            and _air_rtype(s) == _sg_rt)
                                ]
                                _flash(f"'{_sg['source']} → {_sg['target']}' 추가!")
                                st.rerun()
                    if st.button("✅ 추천 전체 추가", key="dm_air_add_all"):
                        _added = 0
                        for _sg in _air_sugg:
                            if not _air_rel_exists(_sg["source"], _sg["target"], _air_rtype(_sg)):
                                st.session_state.setdefault("relations", []).append(_air_make_rel(_sg))
                                _added += 1
                        save_persisted_data()
                        st.session_state["dm_air_suggestions"] = []
                        _flash(f"{_added}개 관계 추가 완료!")
                        st.rerun()

        st.divider()
        _rel_type = st.radio("관계 종류", ["📁 프로젝트 → 🧠 개념", "📁 프로젝트 → ✅ 작업",
                                           "📝 메모 → 🧠 개념", "🧠 개념 → 🧠 개념"],
                             horizontal=True, key="dm_rel_type")

        # 관계 타입 (공통)
        _REL_TYPES = ["참고", "연속", "파생", "반박", "연결", "포함", "기타"]
        _rel_kind = st.selectbox("관계 유형", _REL_TYPES, key="dm_rel_kind",
            help="체크해서 연결 시 이 유형으로 relations 테이블에 저장돼요.")
        st.divider()

        _proj_names = [p.get("name","") for p in _dm_projs]
        _con_names  = [c.get("name","") for c in _dm_concepts]
        _note_titles= [n.get("title","제목 없음") for n in _dm_notes]

        def _add_relation(src_type, src_name, tgt_type, tgt_name, rtype):
            """relations 리스트에 중복 없이 추가."""
            _rels = st.session_state.setdefault("relations", [])
            _exists = any(
                r.get("source_name")==src_name and r.get("target_name")==tgt_name
                for r in _rels
            )
            if not _exists:
                import uuid as _ruuid
                _rels.append({
                    "id": str(_ruuid.uuid4())[:8],
                    "source_type": src_type, "source_name": src_name,
                    "target_type": tgt_type, "target_name": tgt_name,
                    "relation_type": rtype,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })

        def _del_relation(src_name, tgt_name):
            """relations 리스트에서 해당 항목 제거."""
            st.session_state["relations"] = [
                r for r in st.session_state.get("relations", [])
                if not (r.get("source_name")==src_name and r.get("target_name")==tgt_name)
            ]

        _rl_left, _rl_right = st.columns([1, 2], gap="large")

        if _rel_type == "📁 프로젝트 → 🧠 개념":
            with _rl_left:
                st.markdown("**📁 프로젝트 선택**")
                _sel_proj_r = st.selectbox("", _proj_names or ["(프로젝트 없음)"],
                    key="dm_r_proj", label_visibility="collapsed")
                _proj_obj = next((p for p in _dm_projs if p.get("name")==_sel_proj_r), {})
                _linked_cons = set(_proj_obj.get("concepts", []))
                st.divider()
                st.markdown("**연결된 개념**")
                if _linked_cons:
                    for _lc in sorted(_linked_cons):
                        st.markdown(f"🧠 {_lc}")
                else:
                    st.caption("연결된 개념 없음")
            with _rl_right:
                st.markdown("**🧠 개념 목록 — 체크해서 연결/해제**")
                if not _con_names:
                    st.caption("개념이 없어요. 먼저 개념을 추가하세요.")
                else:
                    _changed = False
                    _new_linked = set(_linked_cons)
                    _cb_cols = st.columns(2)
                    for _ci, _cn in enumerate(sorted(_con_names)):
                        with _cb_cols[_ci % 2]:
                            _checked = st.checkbox(_cn, value=(_cn in _linked_cons),
                                key=f"dm_rel_pc_{_sel_proj_r[:8]}_{_ci}_{_cn[:12]}")
                            if _checked and _cn not in _linked_cons:
                                _new_linked.add(_cn); _changed = True
                            elif not _checked and _cn in _linked_cons:
                                _new_linked.discard(_cn); _changed = True
                    if _changed:
                        _proj_obj["concepts"] = list(_new_linked)
                        # 추가된 것 → relations 동기화
                        for _cn_a in _new_linked - _linked_cons:
                            _add_relation("project", _sel_proj_r, "concept", _cn_a, _rel_kind)
                        # 제거된 것 → relations에서 제거
                        for _cn_r in _linked_cons - _new_linked:
                            _del_relation(_sel_proj_r, _cn_r)
                        save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

        elif _rel_type == "📁 프로젝트 → ✅ 작업":
            with _rl_left:
                st.markdown("**📁 프로젝트 선택**")
                _sel_proj_t = st.selectbox("", _proj_names or ["(없음)"],
                    key="dm_r_proj_t", label_visibility="collapsed")
                _proj_task_names = {t.get("title","") for t in _dm_tasks if t.get("project")==_sel_proj_t}
                st.divider()
                st.markdown("**연결된 작업**")
                if _proj_task_names:
                    for _ptn in sorted(_proj_task_names):
                        st.markdown(f"✅ {_ptn}")
                else:
                    st.caption("연결된 작업 없음")
            with _rl_right:
                st.markdown("**✅ 작업 목록 — 체크해서 연결/해제**")
                _all_task_titles = [t.get("title","") for t in _dm_tasks]
                if not _all_task_titles:
                    st.caption("작업이 없어요.")
                else:
                    _cb_cols2 = st.columns(2)
                    for _ti2, _tt2 in enumerate(sorted(_all_task_titles)):
                        _task_obj = next((t for t in _dm_tasks if t.get("title")==_tt2), None)
                        _is_linked = (_task_obj and _task_obj.get("project")==_sel_proj_t)
                        with _cb_cols2[_ti2 % 2]:
                            _tc = st.checkbox(_tt2, value=bool(_is_linked),
                                key=f"dm_rel_pt_{_sel_proj_t[:8]}_{_ti2}_{_tt2[:12]}")
                            if _task_obj:
                                if _tc and not _is_linked:
                                    _task_obj["project"] = _sel_proj_t
                                    _add_relation("project", _sel_proj_t, "task", _tt2, _rel_kind)
                                    save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                                elif not _tc and _is_linked:
                                    _task_obj["project"] = ""
                                    _del_relation(_sel_proj_t, _tt2)
                                    save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

        elif _rel_type == "📝 메모 → 🧠 개념":
            with _rl_left:
                st.markdown("**📝 메모 선택**")
                _sel_note_r = st.selectbox("", _note_titles or ["(없음)"],
                    key="dm_r_note", label_visibility="collapsed")
                _note_obj = next((n for n in _dm_notes if n.get("title","제목 없음")==_sel_note_r), {})
                _note_id = _note_obj.get("id","")
                _linked_note_cons = {l.get("concept") for l in _dm_links if l.get("note_id")==_note_id}
                st.divider()
                st.markdown("**연결된 개념**")
                if _linked_note_cons:
                    for _lnc in sorted(_linked_note_cons):
                        st.markdown(f"🧠 {_lnc}")
                else:
                    st.caption("연결된 개념 없음")
            with _rl_right:
                st.markdown("**🧠 개념 목록 — 체크해서 연결/해제**")
                if not _con_names:
                    st.caption("개념이 없어요.")
                else:
                    _cb_cols3 = st.columns(2)
                    for _ci3, _cn3 in enumerate(sorted(_con_names)):
                        with _cb_cols3[_ci3 % 2]:
                            _tc3 = st.checkbox(_cn3, value=(_cn3 in _linked_note_cons),
                                key=f"dm_rel_nc_{_note_id[:8]}_{_ci3}_{_cn3[:12]}")
                            if _tc3 and _cn3 not in _linked_note_cons and _note_id:
                                from datetime import datetime as _dtnow
                                st.session_state["note_concept_links"].append(
                                    {"note_id":_note_id,"concept":_cn3,"linked_at":_dtnow.now().strftime("%Y-%m-%d %H:%M")})
                                _add_relation("note", _sel_note_r, "concept", _cn3, _rel_kind)
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()
                            elif not _tc3 and _cn3 in _linked_note_cons and _note_id:
                                st.session_state["note_concept_links"] = [
                                    l for l in _dm_links if not (l.get("note_id")==_note_id and l.get("concept")==_cn3)]
                                _del_relation(_sel_note_r, _cn3)
                                save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

        else:  # 개념 → 개념
            with _rl_left:
                st.markdown("**🧠 기준 개념 선택**")
                _sel_con_r = st.selectbox("", _con_names or ["(없음)"],
                    key="dm_r_con_base", label_visibility="collapsed")
                _con_obj = next((c for c in _dm_concepts if c.get("name")==_sel_con_r), {})
                _linked_to = set(_con_obj.get("linked_concepts", []))
                st.divider()
                st.markdown("**연결된 개념**")
                if _linked_to:
                    for _ltc in sorted(_linked_to):
                        st.markdown(f"🧠 {_ltc}")
                else:
                    st.caption("연결된 개념 없음")
            with _rl_right:
                st.markdown("**🧠 개념 목록 — 체크해서 연결/해제**")
                _other_cons = [c for c in _con_names if c != _sel_con_r]
                if not _other_cons:
                    st.caption("연결할 개념이 없어요.")
                else:
                    _cb_cols4 = st.columns(2)
                    _new_linked_cc = set(_linked_to)
                    _cc_changed = False
                    for _ci4, _cn4 in enumerate(sorted(_other_cons)):
                        with _cb_cols4[_ci4 % 2]:
                            _tc4 = st.checkbox(_cn4, value=(_cn4 in _linked_to),
                                key=f"dm_rel_cc_{_sel_con_r[:8]}_{_ci4}_{_cn4[:12]}")
                            if _tc4 and _cn4 not in _linked_to:
                                _new_linked_cc.add(_cn4); _cc_changed = True
                            elif not _tc4 and _cn4 in _linked_to:
                                _new_linked_cc.discard(_cn4); _cc_changed = True
                    if _cc_changed:
                        _con_obj["linked_concepts"] = list(_new_linked_cc)
                        for _cn_a2 in _new_linked_cc - _linked_to:
                            _add_relation("concept", _sel_con_r, "concept", _cn_a2, _rel_kind)
                        for _cn_r2 in _linked_to - _new_linked_cc:
                            _del_relation(_sel_con_r, _cn_r2)
                        save_persisted_data(); _flash("변경사항을 저장했어요"); st.rerun()

    # ═══════════════════════════════════════════
    # TAB 3 — ERD 뷰 (plotly network)
    # ═══════════════════════════════════════════
    with _dm_tab3:
        st.markdown("#### 🕸️ 지식 ERD")
        _erd_center = st.selectbox("중심 프로젝트 선택 (전체=모두 표시)",
                                   ["전체"] + _proj_names, key="dm_erd_center")
        _show_concepts = st.checkbox("개념 노드 표시", value=True, key="dm_erd_con")
        _show_tasks    = st.checkbox("작업 노드 표시", value=True, key="dm_erd_task")
        _show_notes    = st.checkbox("메모 노드 표시", value=False, key="dm_erd_note")

        import plotly.graph_objects as _pgo2
        import math as _math

        _erd_nodes_x, _erd_nodes_y, _erd_labels, _erd_colors, _erd_sizes = [], [], [], [], []
        _erd_edge_x, _erd_edge_y = [], []
        _erd_node_idx = {}

        def _erd_add_node(name, color, size):
            if name not in _erd_node_idx:
                _erd_node_idx[name] = len(_erd_labels)
                _erd_labels.append(name)
                _erd_colors.append(color)
                _erd_sizes.append(size)
                _erd_nodes_x.append(0.0)
                _erd_nodes_y.append(0.0)

        def _erd_add_edge(a, b):
            if a in _erd_node_idx and b in _erd_node_idx:
                ai, bi = _erd_node_idx[a], _erd_node_idx[b]
                _erd_edge_x.extend([_erd_nodes_x[ai], _erd_nodes_x[bi], None])
                _erd_edge_y.extend([_erd_nodes_y[ai], _erd_nodes_y[bi], None])

        # 노드 배치 (원형 레이아웃)
        _erd_projs_show = _dm_projs if _erd_center == "전체" else [p for p in _dm_projs if p.get("name")==_erd_center]
        _np = len(_erd_projs_show) or 1
        for _pi, _proj in enumerate(_erd_projs_show):
            _pname = _proj.get("name","?")
            _angle = 2 * _math.pi * _pi / _np
            _erd_add_node(_pname, "#3b82f6", 30)
            _erd_nodes_x[_erd_node_idx[_pname]] = _math.cos(_angle) * 2
            _erd_nodes_y[_erd_node_idx[_pname]] = _math.sin(_angle) * 2
            # 연결된 개념
            if _show_concepts:
                for _ci2, _cc2 in enumerate(_proj.get("concepts",[])):
                    _erd_add_node(_cc2, "#10b981", 18)
                    _a2 = _angle + 0.4 * (_ci2 - len(_proj.get("concepts",[]))/2)
                    _erd_nodes_x[_erd_node_idx[_cc2]] = _math.cos(_angle)*3.5 + _math.cos(_a2)*0.8
                    _erd_nodes_y[_erd_node_idx[_cc2]] = _math.sin(_angle)*3.5 + _math.sin(_a2)*0.8
            # 연결된 작업
            if _show_tasks:
                _ptasks = [t for t in _dm_tasks if t.get("project")==_pname]
                for _ti2, _tt2 in enumerate(_ptasks):
                    _tn2 = _tt2.get("title","?")
                    _erd_add_node(_tn2, "#f59e0b", 18)
                    _a3 = _angle - 0.4 * (_ti2 - len(_ptasks)/2)
                    _erd_nodes_x[_erd_node_idx[_tn2]] = _math.cos(_angle)*3.5 + _math.cos(_a3)*0.8
                    _erd_nodes_y[_erd_node_idx[_tn2]] = _math.sin(_angle)*3.5 + _math.sin(_a3)*0.8
            # 연결된 메모
            if _show_notes:
                _pnotes = [n for n in _dm_notes if n.get("project")==_pname][:5]
                for _ni2, _nn2 in enumerate(_pnotes):
                    _ntitle = _nn2.get("title","?")[:20]
                    _erd_add_node(_ntitle, "#8b5cf6", 14)
                    _a4 = _angle + _math.pi/2 + 0.3 * (_ni2 - len(_pnotes)/2)
                    _erd_nodes_x[_erd_node_idx[_ntitle]] = _math.cos(_angle)*4.5
                    _erd_nodes_y[_erd_node_idx[_ntitle]] = _math.sin(_angle)*4.5 + _ni2 * 0.5

        # 엣지 생성 (노드 배치 후)
        for _proj in _erd_projs_show:
            _pname = _proj.get("name","?")
            if _show_concepts:
                for _cc2 in _proj.get("concepts",[]):
                    _erd_add_edge(_pname, _cc2)
            if _show_tasks:
                for _tt2 in [t for t in _dm_tasks if t.get("project")==_pname]:
                    _erd_add_edge(_pname, _tt2.get("title","?"))
            if _show_notes:
                for _nn2 in [n for n in _dm_notes if n.get("project")==_pname][:5]:
                    _erd_add_edge(_pname, _nn2.get("title","?")[:20])
        # 개념→개념 연결
        if _show_concepts:
            for _cc3 in _dm_concepts:
                for _lcc in _cc3.get("linked_concepts",[]):
                    _erd_add_edge(_cc3.get("name",""), _lcc)

        if _erd_labels:
            _erd_fig = _pgo2.Figure()
            _erd_fig.add_trace(_pgo2.Scatter(x=_erd_edge_x, y=_erd_edge_y, mode="lines",
                line=dict(color="rgba(148,163,184,0.5)", width=1.5), hoverinfo="none"))
            _erd_fig.add_trace(_pgo2.Scatter(
                x=_erd_nodes_x, y=_erd_nodes_y, mode="markers+text",
                marker=dict(size=_erd_sizes, color=_erd_colors,
                            line=dict(color="white", width=1.5)),
                text=_erd_labels, textposition="top center",
                textfont=dict(size=11, color="#1e293b"),
                hovertext=[f"<b>{l}</b>" for l in _erd_labels], hoverinfo="text",
            ))
            _erd_fig.update_layout(
                showlegend=False, height=520,
                margin=dict(l=10, r=10, t=10, b=10),
                plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
                xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            )
            st.plotly_chart(_erd_fig, use_container_width=True)
            # 범례
            _lg1, _lg2, _lg3, _lg4 = st.columns(4)
            with _lg1: st.markdown('<span style="color:#3b82f6">●</span> **프로젝트**', unsafe_allow_html=True)
            with _lg2: st.markdown('<span style="color:#10b981">●</span> **개념**', unsafe_allow_html=True)
            with _lg3: st.markdown('<span style="color:#f59e0b">●</span> **작업**', unsafe_allow_html=True)
            with _lg4: st.markdown('<span style="color:#8b5cf6">●</span> **메모**', unsafe_allow_html=True)
        else:
            st.info("프로젝트와 개념/작업을 먼저 생성하고 관계를 연결하면 ERD가 표시돼요.")

    # ═══════════════════════════════════════════
    # TAB 4 — 빠른 작업 (병합·이동·삭제·메모추가)
    # ═══════════════════════════════════════════
    with _dm_tab4:
        st.markdown("#### ⚡ 빠른 작업")
        _qa_type = st.radio("작업 유형", ["📝 빠른 메모 추가", "🔗 개념 병합", "📦 개념 폴더 이동", "📁 메모 프로젝트 이동", "🗑️ 일괄 삭제"],
                            horizontal=True, key="dm_qa_type")
        st.divider()

        if _qa_type == "📝 빠른 메모 추가":
            st.caption("텍스트를 붙여넣어 메모로 바로 저장하거나, AI가 제목·태그·개념을 자동 추출해 저장해요.")
            # ── 공통 데이터 사전 준비 ──
            _qm_proj_opts = ["기본 프로젝트"] + _proj_names
            # 기존 섹션 목록 (프로젝트별)
            _all_sections = sorted({s.get("name","") for s in st.session_state.get("project_sections",[]) if s.get("name")}) or ["일반"]
            # 기존 태그 목록
            _all_existing_tags = sorted({
                str(t).replace("#","").strip()
                for n in st.session_state.get("archive_notes",[])
                for t in n.get("tags",[]) if str(t).strip()
            })
            # 기존 개념 목록
            _all_con_names_qm = sorted({
                (c.get("name") if isinstance(c,dict) else str(c))
                for c in st.session_state.get("pkm_custom_concepts",[]) if c
            })
            # 기존 폴더 목록
            _all_folders_qm = sorted(set(
                list(st.session_state.get("pkm_concept_folders",{}).values()) +
                [(c.get("folder","자동") if isinstance(c,dict) else "자동")
                 for c in st.session_state.get("pkm_custom_concepts",[])]
            ) - {"자동", ""}) or []
            # 기존 작업 목록
            _all_task_titles_qm = [t.get("title","") for t in st.session_state.get("tasks",[]) if t.get("title")]

            _memo_tab1, _memo_tab2 = st.tabs(["💾 단순 저장", "🤖 AI 분석 후 저장"])

            with _memo_tab1:
                _qm_c1, _qm_c2 = st.columns([2, 1])
                with _qm_c1:
                    _qm_title = st.text_input("제목 *", key="qm_s_title", placeholder="메모 제목을 입력하세요")
                    _qm_body = st.text_area(
                        "본문 (붙여넣기)",
                        key="qm_s_body",
                        height=200,
                        placeholder="## 핵심 정리\n- 항목\n- [ ] 확인할 일\n> 참고 내용",
                        help="마크다운을 지원해요. 저장 후 읽기 화면에서 가독성 있게 표시돼요.",
                    )
                with _qm_c2:
                    # 프로젝트 선택
                    _qm_proj = st.selectbox("📁 프로젝트", _qm_proj_opts, key="qm_s_proj")
                    # 섹션: 기존 목록 + 직접 입력
                    _qm_sec_opts = ["일반"] + _all_sections + ["✏️ 직접 입력"]
                    _qm_sec_sel = st.selectbox("📂 섹션", _qm_sec_opts, key="qm_s_sec_sel")
                    _qm_sec = st.text_input("섹션명 직접 입력", key="qm_s_sec_custom",
                        label_visibility="collapsed", placeholder="섹션명") if _qm_sec_sel == "✏️ 직접 입력" else _qm_sec_sel
                    # 태그: 기존 목록 멀티셀렉트 + 추가 직접 입력
                    _qm_tag_sel = multiselect_with_all("🏷️ 태그 선택", _all_existing_tags, key="qm_s_tag_sel")
                    _qm_tag_extra = st.text_input("태그 추가 (쉼표 구분)", key="qm_s_tag_extra",
                        placeholder="새 태그 입력 (예: ESG, 정책)")
                    # 개념 연결
                    _qm_con_sel = multiselect_with_all("🧠 개념 연결", _all_con_names_qm, key="qm_s_con_sel")
                    # 연결 작업
                    _qm_task_sel = multiselect_with_all("✅ 관련 작업", _all_task_titles_qm, key="qm_s_task_sel")
                    _qm_score = st.slider("신뢰도 점수", 0, 100, 70, 5, key="qm_s_score")

                if st.button("💾 메모 저장", key="qm_s_save", type="primary", use_container_width=True):
                    if not _qm_title.strip():
                        st.warning("제목을 입력해주세요.")
                    elif not _qm_body.strip():
                        st.warning("본문을 입력해주세요.")
                    else:
                        import uuid as _uuid3
                        _extra_tags = [t.strip() for t in _qm_tag_extra.split(",") if t.strip()]
                        _qm_tag_list = list(dict.fromkeys(_qm_tag_sel + _extra_tags))
                        _note_id3 = f"note_{_uuid3.uuid4().hex[:8]}"
                        st.session_state.setdefault("archive_notes", []).append({
                            "id": _note_id3, "url": "",
                            "title": _qm_title.strip(),
                            "project": _qm_proj,
                            "section": _qm_sec or "일반",
                            "content_type": "manual",
                            "score": _qm_score,
                            "favorite": False,
                            "tags": _qm_tag_list,
                            "note": _qm_body.strip(),
                            "original_text": _qm_body.strip(),
                            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        })
                        _now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
                        # 태그 + 선택 개념 → note_concept_links
                        for _lc in list(dict.fromkeys(_qm_tag_list + _qm_con_sel)):
                            st.session_state.setdefault("note_concept_links",[]).append(
                                {"note_id": _note_id3, "concept": _lc, "linked_at": _now_s})
                        # 작업에 메모 note_id 연결
                        for _t5 in st.session_state.get("tasks",[]):
                            if _t5.get("title") in _qm_task_sel:
                                _t5.setdefault("linked_note_ids",[]).append(_note_id3)
                        save_persisted_data()
                        _flash(f"✅ '{_qm_title.strip()}' 메모가 저장됐어요!")
                        st.rerun()

            with _memo_tab2:
                st.caption("텍스트를 붙여넣으면 AI가 제목·태그·핵심개념·신뢰도를 자동 추출해요. 저장 전 기존 데이터와 연결 설정도 가능해요.")
                _qai_body = st.text_area("텍스트 붙여넣기 *", key="qm_ai_body", height=200,
                    placeholder="분석할 텍스트를 여기에 붙여넣으세요 (URL 본문, 기사, 논문 요약 등)")
                _qai_c1, _qai_c2 = st.columns(2)
                with _qai_c1:
                    _qai_proj = st.selectbox("📁 프로젝트", _qm_proj_opts, key="qm_ai_proj")
                    _qai_sec_opts = ["일반"] + _all_sections + ["✏️ 직접 입력"]
                    _qai_sec_sel = st.selectbox("📂 섹션", _qai_sec_opts, key="qm_ai_sec_sel")
                    _qai_sec = st.text_input("섹션 직접 입력", key="qm_ai_sec_custom",
                        label_visibility="collapsed", placeholder="섹션명") if _qai_sec_sel == "✏️ 직접 입력" else _qai_sec_sel
                with _qai_c2:
                    _qai_task_sel = multiselect_with_all("✅ 관련 작업 연결", _all_task_titles_qm, key="qm_ai_task_sel")

                if st.button("🤖 AI 분석 시작", key="qm_ai_run", type="primary", use_container_width=True):
                    if not _qai_body.strip():
                        st.warning("텍스트를 입력해주세요.")
                    else:
                        _api_key = st.session_state.get("groq_api_key") or os.environ.get("GROQ_API_KEY","")
                        if not _api_key:
                            st.error("⚠️ Groq API 키가 없어요. 설정에서 API 키를 입력해주세요.")
                        else:
                            with st.spinner("AI가 분석 중..."):
                                try:
                                    _sys = """당신은 정보 분석 전문가입니다. 주어진 텍스트를 분석하고 반드시 아래 JSON 형식으로만 응답하세요:
{
  "title": "메모 제목 (20자 이내)",
  "summary": "핵심 요약 (100자 이내)",
  "tags": ["태그1", "태그2", "태그3"],
  "key_concepts": ["핵심개념1", "핵심개념2"],
  "trust_score": 75,
  "content_type": "news|policy|review|research|other"
}"""
                                    _usr = f"다음 텍스트를 분석해주세요:\n\n{_qai_body.strip()[:3000]}"
                                    _ai_raw = call_groq_simple(_sys, _usr)
                                    import json as _json2
                                    _ai_raw_clean = _ai_raw.strip()
                                    if "```" in _ai_raw_clean:
                                        _ai_raw_clean = _ai_raw_clean.split("```")[1]
                                        if _ai_raw_clean.startswith("json"):
                                            _ai_raw_clean = _ai_raw_clean[4:]
                                    _ai_result = _json2.loads(_ai_raw_clean.strip())
                                    st.session_state["qm_ai_result"] = _ai_result
                                    st.session_state["qm_ai_body_saved"] = _qai_body.strip()
                                except Exception as _e:
                                    st.error(f"AI 분석 오류: {_e}")

                if st.session_state.get("qm_ai_result"):
                    _r = st.session_state["qm_ai_result"]
                    st.divider()
                    st.markdown("#### ✅ AI 분석 결과 — 수정·연결 후 저장")
                    _ra, _rb = st.columns([2, 1])
                    with _ra:
                        _qai_title_edit = st.text_input("제목", value=_r.get("title",""), key="qm_ai_title_edit")
                        _qai_sum_edit = st.text_area("요약/메모", value=_r.get("summary",""), key="qm_ai_sum_edit", height=100)
                    with _rb:
                        _qai_score_edit = st.slider("신뢰도", 0, 100, int(_r.get("trust_score", 70)), 5, key="qm_ai_score_edit")
                        # AI 추천 태그 + 기존 태그 멀티셀렉트
                        _ai_suggested_tags = _r.get("tags", [])
                        _ai_tag_opts = sorted(set(_all_existing_tags + _ai_suggested_tags))
                        _qai_tags_sel = st.multiselect("🏷️ 태그",
                            options=_ai_tag_opts,
                            default=[t for t in _ai_suggested_tags if t in _ai_tag_opts],
                            key="qm_ai_tags_sel")
                        _qai_tag_extra2 = st.text_input("태그 추가 입력", key="qm_ai_tag_extra",
                            placeholder="새 태그 (쉼표 구분)")
                        # AI 추천 개념 + 기존 개념 멀티셀렉트
                        _ai_suggested_cons = _r.get("key_concepts", [])
                        _ai_con_opts = sorted(set(_all_con_names_qm + _ai_suggested_cons))
                        _qai_con_sel = st.multiselect("🧠 개념 연결",
                            options=_ai_con_opts,
                            default=[c for c in _ai_suggested_cons if c in _ai_con_opts],
                            key="qm_ai_con_sel")
                        # 새 개념 폴더 설정
                        _new_cons_to_add = [c for c in _ai_suggested_cons if c not in _all_con_names_qm]
                        if _new_cons_to_add:
                            _qai_new_con_folder = st.selectbox(
                                f"새 개념 폴더 ({', '.join(_new_cons_to_add[:2])}{'...' if len(_new_cons_to_add)>2 else ''})",
                                ["자동"] + _all_folders_qm + ["✏️ 직접 입력"],
                                key="qm_ai_new_con_folder")
                            if _qai_new_con_folder == "✏️ 직접 입력":
                                _qai_new_con_folder = st.text_input("폴더명", key="qm_ai_new_folder_custom",
                                    label_visibility="collapsed")
                        else:
                            _qai_new_con_folder = "자동"

                    _sv2, _cl2 = st.columns(2)
                    with _sv2:
                        if st.button("💾 저장", key="qm_ai_save", type="primary", use_container_width=True):
                            import uuid as _uuid4
                            _note_id4 = f"note_{_uuid4.uuid4().hex[:8]}"
                            _extra_tags2 = [t.strip() for t in _qai_tag_extra2.split(",") if t.strip()]
                            _tag_list2 = list(dict.fromkeys(_qai_tags_sel + _extra_tags2))
                            _con_list2 = list(dict.fromkeys(_qai_con_sel))
                            st.session_state.setdefault("archive_notes", []).append({
                                "id": _note_id4, "url": "",
                                "title": _qai_title_edit.strip(),
                                "project": _qai_proj,
                                "section": _qai_sec or "일반",
                                "content_type": _r.get("content_type", "manual"),
                                "score": _qai_score_edit,
                                "favorite": False,
                                "tags": _tag_list2,
                                "note": _qai_sum_edit.strip(),
                                "original_text": st.session_state.get("qm_ai_body_saved",""),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            })
                            _now_s2 = datetime.now().strftime("%Y-%m-%d %H:%M")
                            # note_concept_links
                            for _lc2 in list(dict.fromkeys(_tag_list2 + _con_list2)):
                                st.session_state.setdefault("note_concept_links",[]).append(
                                    {"note_id": _note_id4, "concept": _lc2, "linked_at": _now_s2})
                            # 새 AI 개념을 pkm_custom_concepts에 등록
                            _existing_cnames = {(c.get("name") if isinstance(c,dict) else str(c))
                                                for c in st.session_state.get("pkm_custom_concepts",[])}
                            for _nc in _new_cons_to_add:
                                if _nc and _nc not in _existing_cnames:
                                    st.session_state.setdefault("pkm_custom_concepts",[]).append(
                                        {"name": _nc, "folder": _qai_new_con_folder, "created_at": _now_s2})
                            # 작업 연결
                            for _t6 in st.session_state.get("tasks",[]):
                                if _t6.get("title") in _qai_task_sel:
                                    _t6.setdefault("linked_note_ids",[]).append(_note_id4)
                            save_persisted_data()
                            st.session_state.pop("qm_ai_result", None)
                            _flash(f"✅ '{_qai_title_edit.strip()}' 메모가 저장됐어요!")
                            st.rerun()
                    with _cl2:
                        if st.button("🗑️ 결과 지우기", key="qm_ai_clear", use_container_width=True):
                            st.session_state.pop("qm_ai_result", None)
                            st.rerun()

        elif _qa_type == "🔗 개념 병합":
            _qa1, _qa2 = st.columns(2)
            with _qa1:
                _mg_src = multiselect_with_all("병합할 개념 (원본들)", [c.get("name") for c in _dm_concepts], key="dm_mg_src")
            with _qa2:
                _mg_tgt = st.selectbox("합칠 대상 개념", [c.get("name") for c in _dm_concepts], key="dm_mg_tgt")
            if _mg_src and _mg_tgt and st.button("🔗 병합 실행", type="primary", key="dm_do_merge"):
                for _sn in _mg_src:
                    for _lk in st.session_state.get("note_concept_links",[]):
                        if _lk.get("concept") == _sn: _lk["concept"] = _mg_tgt
                    st.session_state["pkm_custom_concepts"] = [c for c in st.session_state.get("pkm_custom_concepts",[]) if (c.get("name") if isinstance(c,dict) else str(c)) != _sn]
                    _h = list(set(st.session_state.get("hidden_concepts",[])) | {_sn})
                    st.session_state["hidden_concepts"] = _h
                save_persisted_data(); _flash(f"{_mg_src} → '{_mg_tgt}'로 병합 완료!"); st.rerun()

        elif _qa_type == "📦 개념 폴더 이동":
            _qa1, _qa2 = st.columns(2)
            with _qa1:
                _mv_cons = multiselect_with_all("이동할 개념들", [c.get("name") for c in _dm_concepts], key="dm_mv_cons")
            with _qa2:
                _mv_fold = st.text_input("이동할 폴더명", key="dm_mv_fold")
            if _mv_cons and _mv_fold and st.button("📦 이동 실행", type="primary", key="dm_do_mv"):
                _fds = st.session_state.get("pkm_concept_folders",{})
                for _mc in _mv_cons: _fds[_mc] = _mv_fold
                for _c3 in st.session_state.get("pkm_custom_concepts",[]):
                    if isinstance(_c3,dict) and _c3.get("name") in _mv_cons:
                        _c3["folder"] = _mv_fold
                st.session_state["pkm_concept_folders"] = _fds
                save_persisted_data(); _flash(f"'{_mv_fold}'로 이동 완료!"); st.rerun()

        elif _qa_type == "📁 메모 프로젝트 이동":
            _qa1, _qa2 = st.columns(2)
            with _qa1:
                _mv_notes = multiselect_with_all("이동할 메모", [n.get("title","제목 없음") for n in _dm_notes], key="dm_mv_notes")
            with _qa2:
                _mv_proj = st.selectbox("이동할 프로젝트", _proj_names or ["(없음)"], key="dm_mv_proj")
            if _mv_notes and st.button("📁 이동 실행", type="primary", key="dm_do_mv_note"):
                for _n3 in st.session_state.get("archive_notes",[]):
                    if _n3.get("title","제목 없음") in _mv_notes:
                        _n3["project"] = _mv_proj
                save_persisted_data(); _flash("이동 완료!"); st.rerun()

        else:  # 일괄 삭제
            st.warning("⚠️ 삭제는 되돌릴 수 없어요.")
            _del_ent = st.radio("삭제할 엔티티", ["프로젝트", "개념", "작업", "태그"], horizontal=True, key="dm_del_ent")
            if _del_ent == "프로젝트":
                _del_sel = multiselect_with_all("삭제할 프로젝트", _proj_names, key="dm_del_proj_sel")
                if _del_sel and st.button("🗑️ 삭제 실행", type="primary", key="dm_do_del_proj"):
                    st.session_state["projects"] = [p for p in _dm_projs if p.get("name") not in _del_sel]
                    save_persisted_data(); _flash("삭제 완료!"); st.rerun()
            elif _del_ent == "개념":
                _del_sel = multiselect_with_all("삭제할 개념", [c.get("name") for c in _dm_concepts], key="dm_del_con_sel")
                if _del_sel and st.button("🗑️ 삭제 실행", type="primary", key="dm_do_del_con"):
                    st.session_state["pkm_custom_concepts"] = [c for c in st.session_state.get("pkm_custom_concepts",[]) if (c.get("name") if isinstance(c,dict) else str(c)) not in _del_sel]
                    _h2 = list(set(st.session_state.get("hidden_concepts",[])) | set(_del_sel))
                    st.session_state["hidden_concepts"] = _h2
                    st.session_state["note_concept_links"] = [l for l in _dm_links if l.get("concept") not in _del_sel]
                    save_persisted_data(); _flash("삭제 완료!"); st.rerun()
            elif _del_ent == "작업":
                _del_sel = multiselect_with_all("삭제할 작업", [t.get("title","") for t in _dm_tasks], key="dm_del_task_sel")
                if _del_sel and st.button("🗑️ 삭제 실행", type="primary", key="dm_do_del_task"):
                    st.session_state["tasks"] = [t for t in _dm_tasks if t.get("title","") not in _del_sel]
                    save_persisted_data(); _flash("삭제 완료!"); st.rerun()
            else:
                _del_sel = multiselect_with_all("삭제할 태그", [t["name"] for t in _dm_tags], key="dm_del_tag_sel")
                if _del_sel and st.button("🗑️ 삭제 실행", type="primary", key="dm_do_del_tag"):
                    for _n4 in st.session_state.get("archive_notes",[]):
                        _n4["tags"] = [t for t in _n4.get("tags",[]) if str(t).replace("#","").strip() not in _del_sel]
                    save_persisted_data(); _flash("삭제 완료!"); st.rerun()

    # ═══════════════════════════════════════════
    # TAB 5 — 엔터티 DB (entities + relations 뷰)
    # ═══════════════════════════════════════════
    with _dm_tab5:
        st.markdown("#### 🗄️ 엔터티 DB")
        st.caption("sync_legacy_data_to_entities()로 동기화된 엔터티와 관계 목록이에요. 직접 삭제·타입 변경 가능.")

        _ent5a, _ent5b = st.tabs(["📋 엔터티 목록", "🔗 관계 목록"])

        with _ent5a:
            _entities_all = st.session_state.get("entities", [])
            _ent_type_filter = st.radio("타입 필터", ["전체", "project", "task", "note", "concept"],
                                        horizontal=True, key="dm_ent5_type")
            _ents_show = _entities_all if _ent_type_filter == "전체" else [
                e for e in _entities_all if e.get("type") == _ent_type_filter
            ]
            if not _ents_show:
                st.info("엔터티가 없어요. 프로젝트/작업/메모/개념을 만들면 자동으로 여기에 동기화돼요.")
            else:
                import pandas as _pd5
                _ent_rows = [{
                    "ID": e.get("id",""),
                    "타입": e.get("type",""),
                    "이름": e.get("name",""),
                    "설명": str(e.get("description", ""))[:50] if e.get("description") is not None else "",
                    "생성일": e.get("created_at",""),
                } for e in _ents_show]
                st.dataframe(_pd5.DataFrame(_ent_rows), use_container_width=True, height=320)
                st.caption(f"총 {len(_ents_show)}개 엔터티")

                with st.expander("🗑️ 엔터티 삭제 (선택)", expanded=False):
                    _del_ent_names = multiselect_with_all("삭제할 엔터티 이름", [e.get("name","") for e in _ents_show],
                        key="dm_ent5_del_sel")
                    if _del_ent_names and st.button("🗑️ 삭제 실행", key="dm_ent5_del_run", type="primary"):
                        st.session_state["entities"] = [
                            e for e in _entities_all if e.get("name") not in _del_ent_names
                        ]
                        save_persisted_data(); _flash(f"{len(_del_ent_names)}개 삭제 완료!"); st.rerun()

        with _ent5b:
            _relations_all = st.session_state.get("relations", [])
            if not _relations_all:
                st.info("저장된 관계가 없어요. 관계 관리 탭에서 체크박스로 연결하면 여기에 기록돼요.")
            else:
                import pandas as _pd5b
                _rel_rows = [{
                    "ID": r.get("id",""),
                    "출발 타입": r.get("source_type",""),
                    "출발": r.get("source_name",""),
                    "관계": r.get("relation_type",""),
                    "도착 타입": r.get("target_type",""),
                    "도착": r.get("target_name",""),
                    "생성일": r.get("created_at",""),
                } for r in _relations_all]
                st.dataframe(_pd5b.DataFrame(_rel_rows), use_container_width=True, height=320)
                st.caption(f"총 {len(_relations_all)}개 관계")

                with st.expander("🗑️ 관계 삭제", expanded=False):
                    _rel_labels = [f"{r.get('source_name','')} →[{r.get('relation_type','')}]→ {r.get('target_name','')}"
                                   for r in _relations_all]
                    _del_rels = multiselect_with_all("삭제할 관계", _rel_labels, key="dm_rel5_del_sel")
                    if _del_rels and st.button("🗑️ 관계 삭제 실행", key="dm_rel5_del_run", type="primary"):
                        _del_idxs = {_rel_labels.index(l) for l in _del_rels if l in _rel_labels}
                        st.session_state["relations"] = [
                            r for i, r in enumerate(_relations_all) if i not in _del_idxs
                        ]
                        save_persisted_data(); _flash(f"{len(_del_rels)}개 관계 삭제 완료!"); st.rerun()

                # 관계 타입별 통계
                st.divider()
                st.markdown("**관계 타입별 분포**")
                from collections import Counter as _RCnt
                _rtc = _RCnt(r.get("relation_type","기타") for r in _relations_all)
                _rc1, _rc2, _rc3 = st.columns(3)
                for _i5, (_rtype5, _rcnt5) in enumerate(_rtc.most_common()):
                    with [_rc1, _rc2, _rc3][_i5 % 3]:
                        st.metric(_rtype5, f"{_rcnt5}개")

    # ═══════════════════════════════════════════
    # TAB 6 — 개념 병합
    # ═══════════════════════════════════════════
    with _dm_tab6:
        st.markdown("#### 🧠 개념 병합")
        st.caption("비슷한 개념을 하나로 합쳐서 지식맵·브레인스토밍·패턴 분석의 품질을 높여요.")

        import difflib as _dfl

        # ── 모든 개념 이름 수집 ──
        _all_con_names = []
        for _cc6 in st.session_state.get("pkm_custom_concepts", []):
            _n6 = _cc6.get("name") if isinstance(_cc6, dict) else str(_cc6)
            if _n6:
                _all_con_names.append(_n6)
        # AI 추출 개념도 포함
        _ai_con_set = set()
        for _rn6 in st.session_state.get("archive_notes", []):
            for _lk6 in st.session_state.get("note_concept_links", []):
                if _lk6.get("note_id") == _rn6.get("id", ""):
                    _ai_con_set.add(_lk6.get("concept", ""))
        _all_con_names_full = list(dict.fromkeys(_all_con_names + [c for c in _ai_con_set if c and c not in _all_con_names]))

        if len(_all_con_names_full) < 2:
            st.info("병합할 개념이 2개 이상 필요해요. 메모를 저장하거나 개념을 추가하면 자동으로 나타나요.")
        else:
            _mg6_mode = st.radio("병합 방식", ["🤖 자동 유사도 탐지", "✋ 수동 선택"], horizontal=True, key="mg6_mode")

            st.divider()

            # ────────────────────────────────────────
            # 병합 실행 헬퍼 (공통) — 호출보다 먼저 정의
            # ────────────────────────────────────────
            def _do_merge6(rep: str, cands: list, alias_only: bool = False):
                """rep로 cands를 병합. alias_only=True면 비파괴(별칭만 기록).
                연결 데이터(메모 concepts, 작업 linked_concepts, 관계, 엔터티,
                폴더맵, note_concept_links)를 함께 업데이트하고 영향 수를 반환."""
                cands = [c for c in cands if c and c != rep]
                _cset = set(cands)
                _impact = {"notes": 0, "tasks": 0, "rels": 0, "ents": 0}

                # 1. pkm_custom_concepts: rep에 aliases 추가 (+ 완전병합 시 cands 제거)
                _new_cons = []
                for _c6x in st.session_state.get("pkm_custom_concepts", []):
                    _cn6x = _c6x.get("name") if isinstance(_c6x, dict) else str(_c6x)
                    if _cn6x == rep:
                        if isinstance(_c6x, dict):
                            _c6x["aliases"] = list(dict.fromkeys((_c6x.get("aliases", []) or []) + cands))
                        _new_cons.append(_c6x)
                    elif _cn6x in _cset and not alias_only:
                        continue  # 완전병합: cand 개념 제거
                    else:
                        _new_cons.append(_c6x)
                if not any((c.get("name") if isinstance(c, dict) else str(c)) == rep for c in _new_cons):
                    _new_cons.append({
                        "name": rep, "folder": "", "description": "",
                        "aliases": cands,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                st.session_state["pkm_custom_concepts"] = _new_cons

                if alias_only:
                    # 비파괴: 중앙 별칭 저장소에도 등록 → canonical_concept가 합산에 반영
                    add_concept_aliases(rep, cands)
                    st.session_state.pop("_alias_rev_cache", None)

                if not alias_only:
                    # 2. note_concept_links: cands → rep (중복 제거)
                    _seen_links = set()
                    _new_links = []
                    for _lk6x in st.session_state.get("note_concept_links", []):
                        if _lk6x.get("concept") in _cset:
                            _lk6x["concept"] = rep
                        _sig = (_lk6x.get("note_id"), _lk6x.get("concept"))
                        if _sig not in _seen_links:
                            _seen_links.add(_sig)
                            _new_links.append(_lk6x)
                    st.session_state["note_concept_links"] = _new_links

                    # 3. 메모 concepts 필드 업데이트
                    for _n6x in st.session_state.get("archive_notes", []):
                        _ncs = _n6x.get("concepts", []) or []
                        if any(c in _cset for c in _ncs):
                            _n6x["concepts"] = list(dict.fromkeys(
                                [rep if c in _cset else c for c in _ncs]))
                            _impact["notes"] += 1

                    # 4. 작업 linked_concepts 업데이트
                    for _t6x in st.session_state.get("tasks", []):
                        _lcs = _t6x.get("linked_concepts", []) or []
                        if any(c in _cset for c in _lcs):
                            _t6x["linked_concepts"] = list(dict.fromkeys(
                                [rep if c in _cset else c for c in _lcs]))
                            _impact["tasks"] += 1

                    # 5. relations: source/target cands → rep
                    for _r6x in st.session_state.get("relations", []):
                        _changed = False
                        if _r6x.get("source_name") in _cset:
                            _r6x["source_name"] = rep; _changed = True
                        if _r6x.get("target_name") in _cset:
                            _r6x["target_name"] = rep; _changed = True
                        if _changed:
                            _impact["rels"] += 1

                    # 6. entities: cands → rep (중복 제거)
                    _seen_ents = set()
                    _new_ents = []
                    for _e6x in st.session_state.get("entities", []):
                        if _e6x.get("name", "") in _cset:
                            _e6x["name"] = rep
                            _impact["ents"] += 1
                        if _e6x.get("name") not in _seen_ents:
                            _seen_ents.add(_e6x.get("name"))
                            _new_ents.append(_e6x)
                    st.session_state["entities"] = _new_ents

                    # 7. pkm_concept_folders: cand 키 제거 (rep 폴더 유지)
                    _folders = st.session_state.get("pkm_concept_folders", {})
                    if isinstance(_folders, dict):
                        for _ck in cands:
                            _folders.pop(_ck, None)

                    # 8. hidden_concepts: cands 숨기기
                    st.session_state["hidden_concepts"] = list(
                        set(st.session_state.get("hidden_concepts", [])) | _cset)

                    # 메모 연결 수 재계산 (concepts 외 note_concept_links 기준 포함)
                    _impact["notes"] = max(_impact["notes"],
                        len({l.get("note_id") for l in _new_links if l.get("concept") == rep}))

                save_persisted_data()
                if alias_only:
                    _flash(f"🔗 {rep}에 별칭 {len(cands)}개를 등록했어요 (개념은 그대로 유지).")
                else:
                    _flash(f"✅ {len(cands)}개 개념을 **{rep}**으로 병합했어요. "
                           f"메모 {_impact['notes']}개·작업 {_impact['tasks']}개가 업데이트됐어요.")
                st.rerun()

            # ────────────────────────────────────────
            # 자동 유사도 탐지
            # ────────────────────────────────────────
            if _mg6_mode == "🤖 자동 유사도 탐지":
                st.info("자동으로 병합하지 않아요. 후보를 등급(🟢🟡🔴)·이유와 함께 제안하면, "
                        "**직접 체크한 후보만** 병합돼요. 데이터 손실이 없도록 보수적으로 동작합니다.")
                _gc1, _gc2 = st.columns([3, 2])
                with _gc1:
                    _mg6_thresh = st.slider("최소 문자열 유사도 (%)", 40, 95, 60, 5, key="mg6_thresh") / 100.0
                with _gc2:
                    _show_red = st.checkbox("🔴 비추천 후보도 표시", value=False, key="mg6_show_red")

                # 제외(다시 추천 안 함) 저장소
                _dismissed = set(st.session_state.setdefault("merge_dismissed", []))

                # 후보 쌍 탐지 (조사 정규화 + 유사도)
                _sorted_cons = sorted(_all_con_names_full)
                _pairs6 = []  # (a, b, ratio, grade, reason)
                for _i6, _ca in enumerate(_sorted_cons):
                    for _cb in _sorted_cons[_i6 + 1:]:
                        _pair_key = "||".join(sorted([_ca, _cb]))
                        if _pair_key in _dismissed:
                            continue
                        _na, _nb = normalize_concept_token(_ca), normalize_concept_token(_cb)
                        _score = _dfl.SequenceMatcher(None, _ca.lower(), _cb.lower()).ratio()
                        _norm_score = _dfl.SequenceMatcher(None, _na.lower(), _nb.lower()).ratio()
                        _best = max(_score, _norm_score)
                        # 조사 정규화 일치는 임계값과 무관하게 후보로
                        if _best < _mg6_thresh and _na != _nb:
                            continue
                        _grade, _reason = grade_merge_pair(_ca, _cb, _best)
                        _pairs6.append((_ca, _cb, round(_best * 100), _grade, _reason))

                _grade_rank = {"green": 0, "yellow": 1, "red": 2}
                _pairs6.sort(key=lambda p: (_grade_rank[p[3]], -p[2]))
                if not _show_red:
                    _pairs6 = [p for p in _pairs6 if p[3] != "red"]

                _grade_meta = {
                    "green": ("🟢", "추천", "#16a34a"),
                    "yellow": ("🟡", "검토 필요", "#d97706"),
                    "red": ("🔴", "비추천", "#dc2626"),
                }

                if not _pairs6:
                    st.success("✅ 지금 기준으로 추천할 병합 후보가 없어요. 개념 구조가 깔끔해요.")
                else:
                    _gn = sum(1 for p in _pairs6 if p[3] == "green")
                    _yn = sum(1 for p in _pairs6 if p[3] == "yellow")
                    _rn = sum(1 for p in _pairs6 if p[3] == "red")
                    st.caption(f"후보 {len(_pairs6)}개 · 🟢 {_gn} · 🟡 {_yn}" + (f" · 🔴 {_rn}" if _show_red else ""))

                    for _pi, (_ca, _cb, _ss6, _grade, _reason) in enumerate(_pairs6):
                        _gi, _gl, _gcol = _grade_meta[_grade]
                        _pkey = "||".join(sorted([_ca, _cb]))
                        with st.container(border=True):
                            st.markdown(
                                f"<span style='color:{_gcol};font-weight:700'>{_gi} {_gl}</span> "
                                f"&nbsp; <b>{_ca}</b> &nbsp;↔&nbsp; <b>{_cb}</b> "
                                f"&nbsp;<span style='color:#94a3b8;font-size:0.85em'>유사도 {_ss6}%</span><br>"
                                f"<span style='color:#64748b;font-size:0.85em'>💡 {_reason}</span>",
                                unsafe_allow_html=True)
                            # 대표 개념 + 방식 선택
                            _rc1, _rc2 = st.columns(2)
                            with _rc1:
                                _rep6 = st.radio("남길 대표 개념", [_ca, _cb],
                                                 key=f"mg6_rep_{_pi}_{_pkey[:20]}", horizontal=True)
                            with _rc2:
                                _method6 = st.radio("방식", ["완전 병합", "별칭 등록", "제외"],
                                                    key=f"mg6_method_{_pi}_{_pkey[:20]}", horizontal=True,
                                                    help="완전 병합: 하나로 합침 · 별칭 등록: 원본 유지하고 alias 연결 · 제외: 다시 추천 안 함")
                            _cand6 = _cb if _rep6 == _ca else _ca
                            # 미리보기 (영향 범위)
                            if _method6 in ("완전 병합", "별칭 등록"):
                                _imp = concept_impact_counts([_cand6])
                                if _method6 == "완전 병합":
                                    st.caption(
                                        f"🔎 **{_cand6}** → **{_rep6}** 로 병합 · "
                                        f"연결된 메모 {_imp['notes']}개·작업 {_imp['tasks']}개·관계 {_imp['rels']}개가 이동돼요.")
                                else:
                                    st.caption(f"🔎 **{_rep6}** 에 별칭 **{_cand6}** 등록 (원본 개념·연결은 그대로 유지)")
                            else:
                                st.caption(f"🚫 이 후보를 다시 추천하지 않아요.")
                            _do6 = st.checkbox("이 후보 적용", value=False, key=f"mg6_apply_{_pi}_{_pkey[:20]}")
                            if st.button("실행", key=f"mg6_run_{_pi}_{_pkey[:20]}",
                                         type="primary", use_container_width=True, disabled=not _do6):
                                if _method6 == "제외":
                                    st.session_state["merge_dismissed"] = list(_dismissed | {_pkey})
                                    save_persisted_data()
                                    _flash(f"🚫 '{_ca} ↔ {_cb}' 후보를 제외했어요.")
                                    st.rerun()
                                else:
                                    _do_merge6(_rep6, [_cand6], alias_only=(_method6 == "별칭 등록"))

                    if _dismissed:
                        st.divider()
                        with st.expander(f"🚫 제외한 후보 {len(_dismissed)}개 (되돌리기)", expanded=False):
                            for _dk in sorted(_dismissed):
                                _da, _, _db = _dk.partition("||")
                                _drc1, _drc2 = st.columns([3, 1])
                                _drc1.markdown(f"{_da} ↔ {_db}")
                                if _drc2.button("복원", key=f"mg6_undismiss_{_dk[:24]}"):
                                    st.session_state["merge_dismissed"] = [x for x in _dismissed if x != _dk]
                                    save_persisted_data(); st.rerun()

            # ────────────────────────────────────────
            # 수동 선택
            # ────────────────────────────────────────
            else:
                st.markdown("**직접 병합할 개념 선택**")
                _mn1, _mn2 = st.columns(2)
                with _mn1:
                    _manual_rep = st.selectbox("대표 개념 (남길 것)", _all_con_names_full, key="mg6_manual_rep")
                with _mn2:
                    _manual_cands = st.multiselect(
                        "병합할 개념 (사라질 것)",
                        [n for n in _all_con_names_full if n != _manual_rep],
                        key="mg6_manual_cands"
                    )

                if _manual_cands:
                    st.markdown("**병합 미리보기:**")
                    st.markdown(
                        f'<div style="background:#fef3c7;border-radius:10px;padding:14px 18px;">'
                        f'<b>{"  +  ".join(_manual_cands)}</b>'
                        f'<span style="color:#92400e;margin:0 12px;font-size:1.2em">→</span>'
                        f'<b style="color:#1e3a8a">{_manual_rep}</b>'
                        f'<br><span style="font-size:0.85em;color:#78716c;">별칭으로 보존: {", ".join(_manual_cands)}</span>'
                        f'</div>',
                        unsafe_allow_html=True
                    )

                    # 영향 범위 표시
                    _aff_ncl = sum(1 for l in st.session_state.get("note_concept_links", []) if l.get("concept") in _manual_cands)
                    _aff_rel = sum(1 for r in st.session_state.get("relations", []) if r.get("source_name") in _manual_cands or r.get("target_name") in _manual_cands)
                    _aff_ent = sum(1 for e in st.session_state.get("entities", []) if e.get("name") in _manual_cands)
                    st.markdown(
                        f'<div style="background:#f1f5f9;border-radius:8px;padding:10px 14px;margin-top:10px;">'
                        f'영향 범위: 메모 연결 <b>{_aff_ncl}개</b> · 관계 <b>{_aff_rel}개</b> · 엔터티 <b>{_aff_ent}개</b> 업데이트 예정'
                        f'</div>',
                        unsafe_allow_html=True
                    )

                if st.button("🔗 병합 실행", key="mg6_manual_run", type="primary", use_container_width=True,
                             disabled=not _manual_cands):
                    _do_merge6(_manual_rep, _manual_cands)

            # ── 별칭 현황 ──
            st.divider()
            st.markdown("**📋 현재 별칭 현황**")
            _alias_data = [
                {"개념": c.get("name"), "별칭": ", ".join(c.get("aliases", []))}
                for c in st.session_state.get("pkm_custom_concepts", [])
                if isinstance(c, dict) and c.get("aliases")
            ]
            if _alias_data:
                import pandas as _pd6
                st.dataframe(_pd6.DataFrame(_alias_data), use_container_width=True, height=180)
            else:
                st.caption("아직 별칭이 없어요. 병합을 실행하면 여기에 기록돼요.")

    st.stop()



if menu == "엔터티 상세":
    # ══════════════════════════════════════════════════════════
    # 🔎 엔터티 상세페이지 — 위키 스타일
    # ══════════════════════════════════════════════════════════

    # ── 데이터 수집 ──
    _ep_notes    = st.session_state.get("archive_notes", [])
    _ep_projects = st.session_state.get("projects", [])
    _ep_tasks    = st.session_state.get("tasks", [])
    _ep_concepts = st.session_state.get("pkm_custom_concepts", [])
    _ep_relations= st.session_state.get("relations", [])
    _ep_nclinks  = st.session_state.get("note_concept_links", [])

    # ── 모든 엔터티 이름 목록 ──
    _ep_all_entities = []
    for _p in _ep_projects:
        _ep_all_entities.append(("📁 프로젝트", _p.get("name", "")))
    for _c in _ep_concepts:
        _cn = _c.get("name") if isinstance(_c, dict) else str(_c)
        if _cn:
            _ep_all_entities.append(("🧠 개념", _cn))
    # AI 추출 개념 (note_concept_links에만 있는 것)
    _my_con_names = {(c.get("name") if isinstance(c,dict) else str(c)) for c in _ep_concepts}
    _ai_con_names = {l.get("concept","") for l in _ep_nclinks} - _my_con_names
    for _ac in sorted(_ai_con_names):
        if _ac:
            _ep_all_entities.append(("🔖 AI개념", _ac))
    # 태그
    _tag_set = set()
    for _n in _ep_notes:
        for _t in _n.get("tags", []):
            _tag_set.add(str(_t).replace("#","").strip())
    for _tg in sorted(_tag_set):
        _ep_all_entities.append(("🏷️ 태그", _tg))

    # ── 헤더 ──
    st.markdown("""
<div style="background:linear-gradient(135deg,#1e3a8a,#7c3aed);border-radius:16px;
     padding:26px 32px 20px;margin-bottom:24px;color:white;">
  <div style="font-size:1.9rem;font-weight:900;letter-spacing:-1px;margin-bottom:4px;">
    🔎 엔터티 상세페이지
  </div>
  <div style="font-size:0.95rem;opacity:0.85;">
    개념·프로젝트·태그를 선택하면 연결된 메모·작업·관계·별칭이 한 화면에 나타나요.
  </div>
</div>""", unsafe_allow_html=True)

    if not _ep_all_entities:
        st.info("아직 엔터티가 없어요. 메모를 저장하거나 프로젝트·개념을 추가하면 여기에 표시돼요.")
        st.stop()

    # ── 검색 + 선택 ──
    _ep_search = st.text_input("🔍 엔터티 검색", placeholder="개념명·프로젝트명·태그 검색...", key="ep_search")
    _ep_filtered = [
        (_type, _name) for _type, _name in _ep_all_entities
        if _ep_search.lower() in _name.lower()
    ] if _ep_search else _ep_all_entities

    _ep_labels = [f"{_t} {_n}" for _t, _n in _ep_filtered]
    _ep_sel_label = st.selectbox(
        "엔터티 선택",
        ["(선택)"] + _ep_labels,
        key="ep_sel_entity"
    )

    # 사이드바 / 지식맵 / 데이터관리에서 넘어올 때를 위한 session_state 처리
    if st.session_state.get("ep_jump_entity"):
        _jump = st.session_state["ep_jump_entity"]
        _match = [l for l in _ep_labels if _jump in l]
        if _match:
            _ep_sel_label = _match[0]
        st.session_state["ep_jump_entity"] = None

    if _ep_sel_label == "(선택)":
        # 빠른 탐색: 최근 사용 개념 top 10
        st.divider()
        st.markdown("**⚡ 자주 쓰는 엔터티 (메모 연결 수 기준)**")
        from collections import Counter as _EPCnt
        _ep_top = _EPCnt(l.get("concept","") for l in _ep_nclinks).most_common(12)
        if _ep_top:
            _ep_cols = st.columns(4)
            for _ei, (_ename, _ecnt) in enumerate(_ep_top):
                with _ep_cols[_ei % 4]:
                    if st.button(f"🧠 {_ename}\n({_ecnt})", key=f"ep_quick_{_ei}", use_container_width=True):
                        st.session_state["ep_jump_entity"] = _ename
                        st.rerun()
        else:
            st.caption("메모를 저장하면 자동으로 개념 연결이 생겨요.")
        st.stop()

    # ── 선택된 엔터티 파싱 ──
    _ep_sel_idx = _ep_labels.index(_ep_sel_label) if _ep_sel_label in _ep_labels else 0
    _ep_type, _ep_name = _ep_filtered[_ep_sel_idx]

    # ── 위키 헤더 카드 ──
    _type_color = {
        "📁 프로젝트": "#3b82f6", "🧠 개념": "#8b5cf6",
        "🔖 AI개념": "#6366f1",  "🏷️ 태그": "#f59e0b",
    }.get(_ep_type, "#64748b")

    # 기본 정보 수집
    _ep_desc = ""
    _ep_aliases = []
    _ep_folder = ""
    _ep_status = ""
    if _ep_type == "📁 프로젝트":
        _pdata = next((p for p in _ep_projects if p.get("name") == _ep_name), {})
        _ep_desc   = _pdata.get("description", "")
        _ep_status = _pdata.get("status", "")
    elif _ep_type in ("🧠 개념", "🔖 AI개념"):
        _cdata = next((c for c in _ep_concepts if (c.get("name") if isinstance(c,dict) else str(c)) == _ep_name), {})
        if isinstance(_cdata, dict):
            _ep_desc    = _cdata.get("description", "")
            _ep_aliases = _cdata.get("aliases", [])
            _ep_folder  = _cdata.get("folder", "")

    # 칩/설명을 미리 문자열로 만들어 둠 (markdown이 4칸 들여쓰기를 코드블록으로 오인하는 문제 방지)
    _folder_chip = (f"<span style='background:#f1f5f9;border-radius:6px;padding:3px 10px;"
                    f"font-size:0.8rem;color:#64748b;'>{_ep_folder}</span>") if _ep_folder else ""
    _status_chip = (f"<span style='background:#dcfce7;border-radius:6px;padding:3px 10px;"
                    f"font-size:0.8rem;color:#166534;'>{_ep_status}</span>") if _ep_status else ""
    _desc_html = (f"<div style='color:#64748b;margin-top:6px;font-size:0.92rem;'>{_ep_desc}</div>"
                  if _ep_desc else "")
    _alias_html = ""
    if _ep_aliases:
        _alias_html = ("<div style='margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;'>"
                       + "".join(f"<span style='background:#ede9fe;color:#6d28d9;border-radius:20px;"
                                 f"padding:2px 10px;font-size:0.8rem;'>≈ {a}</span>" for a in _ep_aliases)
                       + "</div>")
    _ep_card_html = (
        f"<div style=\"background:white;border:2px solid {_type_color};border-radius:14px;"
        f"padding:20px 24px 16px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,0.07);\">"
        f"<div style=\"display:flex;align-items:center;gap:12px;margin-bottom:8px;\">"
        f"<span style=\"background:{_type_color};color:white;border-radius:8px;"
        f"padding:4px 12px;font-size:0.82rem;font-weight:700;\">{_ep_type}</span>"
        f"{_folder_chip}{_status_chip}</div>"
        f"<div style=\"font-size:1.7rem;font-weight:900;color:#0f172a;letter-spacing:-0.5px;\">{_ep_name}</div>"
        f"{_desc_html}{_alias_html}</div>"
    )
    st.markdown(_ep_card_html, unsafe_allow_html=True)

    # ── 연결 데이터 수집 ──
    # 연결된 메모
    if _ep_type in ("🧠 개념", "🔖 AI개념"):
        _ep_linked_note_ids = {l.get("note_id","") for l in _ep_nclinks if l.get("concept") == _ep_name}
        _ep_linked_notes = [n for n in _ep_notes if n.get("id","") in _ep_linked_note_ids or
                            _ep_name in [str(t).replace("#","").strip() for t in n.get("tags",[])]]
    elif _ep_type == "📁 프로젝트":
        _ep_linked_notes = [n for n in _ep_notes if n.get("project","") == _ep_name]
    elif _ep_type == "🏷️ 태그":
        _ep_linked_notes = [n for n in _ep_notes if _ep_name in [str(t).replace("#","").strip() for t in n.get("tags",[])]]
    else:
        _ep_linked_notes = []

    # 연결된 작업
    if _ep_type == "📁 프로젝트":
        _ep_linked_tasks = [t for t in _ep_tasks if t.get("project","") == _ep_name]
    else:
        _ep_linked_tasks = [t for t in _ep_tasks if _ep_name in t.get("title","") or _ep_name in t.get("description","")]

    # 연결된 관계
    _ep_linked_rels = [r for r in _ep_relations if r.get("source_name") == _ep_name or r.get("target_name") == _ep_name]

    # 연결된 개념 (메모들의 note_concept_links에서 공통 개념)
    if _ep_linked_notes:
        _ep_note_ids = {n.get("id","") for n in _ep_linked_notes}
        _ep_co_concepts = {}
        for _l in _ep_nclinks:
            if _l.get("note_id","") in _ep_note_ids and _l.get("concept","") != _ep_name:
                _ep_co_concepts[_l.get("concept","")] = _ep_co_concepts.get(_l.get("concept",""), 0) + 1
        _ep_co_concepts = sorted(_ep_co_concepts.items(), key=lambda x: x[1], reverse=True)
    else:
        _ep_co_concepts = []

    # ── 지식맵 바로가기 버튼 ──
    _nav_c1, _nav_c2, _nav_c3, _nav_c4 = st.columns(4)
    with _nav_c1:
        if st.button("🔗 관계형 지식맵", key="ep_goto_relmap", use_container_width=True,
                     help="관계형 지식맵(탭8)에서 이 엔터티를 중심으로 보기"):
            st.session_state["rg_sel_node"] = _ep_name
            st.query_params["page"] = "map"
            st.rerun()
    with _nav_c2:
        if _ep_type == "📁 프로젝트" and st.button("🗺️ 프로젝트 지식맵", key="ep_goto_projmap", use_container_width=True,
                                                    help="프로젝트 지식맵(탭7)에서 보기"):
            st.session_state["pm_sel_project"] = _ep_name
            st.query_params["page"] = "map"
            st.rerun()
        elif _ep_type != "📁 프로젝트":
            st.button("🗺️ 프로젝트 지식맵", key="ep_goto_projmap_dis", use_container_width=True, disabled=True)
    with _nav_c3:
        if st.button("🔎 개념 파인더", key="ep_goto_finder", use_container_width=True,
                     help="지식맵 개념 파인더 탭에서 보기"):
            st.session_state["pkm_concept_search"] = _ep_name
            st.query_params["page"] = "map"
            st.rerun()
    with _nav_c4:
        if _ep_type == "📁 프로젝트" and st.button("📁 프로젝트 상세", key="ep_goto_proj", use_container_width=True,
                                                    help="프로젝트 페이지로 이동"):
            st.session_state["proj_detail_open"] = _ep_name
            st.query_params["page"] = "projects"
            st.rerun()
        elif _ep_type != "📁 프로젝트":
            st.button("📁 프로젝트 상세", key="ep_goto_proj_dis", use_container_width=True, disabled=True)

    # ── 프로젝트 Dashboard 카드 (프로젝트 엔터티 선택 시) ──
    if _ep_type == "📁 프로젝트":
        _pdata_dash = next((p for p in _ep_projects if p.get("name") == _ep_name), {})
        _dash_tasks  = [t for t in _ep_tasks if t.get("project","") == _ep_name]
        _dash_notes  = [n for n in _ep_notes if n.get("project","") == _ep_name]
        _dash_done   = sum(1 for t in _dash_tasks if t.get("status") == "완료")
        _dash_wip    = sum(1 for t in _dash_tasks if t.get("status") == "진행중")
        _dash_hold   = sum(1 for t in _dash_tasks if t.get("status") == "보류")
        _dash_todo   = sum(1 for t in _dash_tasks if t.get("status") == "시작전")
        _dash_prog   = _pdata_dash.get("progress", 0)
        _prog_color  = "#22c55e" if _dash_prog >= 70 else "#f59e0b" if _dash_prog >= 30 else "#3b82f6"

        # 연결 개념 수
        _dash_note_ids = {n.get("id","") for n in _dash_notes}
        _dash_con_set  = {l.get("concept","") for l in _ep_nclinks if l.get("note_id","") in _dash_note_ids}

        st.markdown(f"""
<div style="background:linear-gradient(135deg,#f0f9ff,#e0f2fe);border:1px solid #bae6fd;
     border-radius:14px;padding:18px 22px 14px;margin:14px 0;">
  <div style="font-size:0.85rem;color:#0284c7;font-weight:700;margin-bottom:10px;">
    📊 프로젝트 대시보드 — {_ep_name}
  </div>
  <div style="margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
      <span style="font-size:0.85rem;color:#334155;">진행률</span>
      <span style="font-weight:700;color:{_prog_color};">{_dash_prog}%</span>
    </div>
    <div style="background:#e2e8f0;border-radius:6px;height:10px;">
      <div style="width:{_dash_prog}%;background:{_prog_color};height:10px;border-radius:6px;
           transition:width 0.4s;"></div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;">
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.3rem;font-weight:800;color:#3b82f6">{len(_dash_tasks)}</div>
      <div style="font-size:0.75rem;color:#64748b">전체 작업</div>
    </div>
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.3rem;font-weight:800;color:#22c55e">{_dash_done}</div>
      <div style="font-size:0.75rem;color:#64748b">완료</div>
    </div>
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.3rem;font-weight:800;color:#3b82f6">{_dash_wip}</div>
      <div style="font-size:0.75rem;color:#64748b">진행중</div>
    </div>
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.3rem;font-weight:800;color:#f59e0b">{_dash_hold}</div>
      <div style="font-size:0.75rem;color:#64748b">보류</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.1rem;font-weight:800;color:#8b5cf6">{len(_dash_notes)}</div>
      <div style="font-size:0.75rem;color:#64748b">메모</div>
    </div>
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.1rem;font-weight:800;color:#6366f1">{len(_dash_con_set)}</div>
      <div style="font-size:0.75rem;color:#64748b">연결 개념</div>
    </div>
    <div style="background:white;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:1.1rem;font-weight:800;color:#0284c7">{len(_ep_linked_rels)}</div>
      <div style="font-size:0.75rem;color:#64748b">관계</div>
    </div>
  </div>
  {"<div style='margin-top:8px;font-size:0.82rem;color:#94a3b8;'>마감일: " + _pdata_dash.get("due_date","") + "</div>" if _pdata_dash.get("due_date") else ""}
</div>""", unsafe_allow_html=True)

    # ── 통계 배너 ──
    _epm1, _epm2, _epm3, _epm4 = st.columns(4)
    with _epm1:
        st.metric("📝 연결 메모", f"{len(_ep_linked_notes)}개")
    with _epm2:
        st.metric("✅ 연결 작업", f"{len(_ep_linked_tasks)}개")
    with _epm3:
        st.metric("🔗 관계", f"{len(_ep_linked_rels)}개")
    with _epm4:
        st.metric("🧠 공통 개념", f"{len(_ep_co_concepts)}개")

    st.divider()

    # ── 상세 탭 ──
    _ep_t1, _ep_t2, _ep_t3, _ep_t4, _ep_t5 = st.tabs([
        f"📝 메모 ({len(_ep_linked_notes)})",
        f"✅ 작업 ({len(_ep_linked_tasks)})",
        f"🔗 관계 ({len(_ep_linked_rels)})",
        f"🧠 공통 개념 ({len(_ep_co_concepts)})",
        "✏️ 편집",
    ])

    with _ep_t1:
        if not _ep_linked_notes:
            st.info("연결된 메모가 없어요.")
        else:
            for _en in _ep_linked_notes:
                _score = _en.get("score", 0)
                _score_color = "#22c55e" if _score >= 70 else "#f59e0b" if _score >= 40 else "#ef4444"
                with st.expander(f"📝 {_en.get('title','제목 없음')}  —  {_en.get('project','')} · {_en.get('saved_at','')}"):
                    _em1, _em2, _em3 = st.columns(3)
                    with _em1:
                        st.markdown(f"**신뢰도:** <span style='color:{_score_color};font-weight:700'>{_score}점</span>", unsafe_allow_html=True)
                    with _em2:
                        st.markdown(f"**섹션:** {_en.get('section','')}")
                    with _em3:
                        st.markdown(f"**태그:** {' '.join(['#'+str(t).replace('#','').strip() for t in _en.get('tags',[])])}")
                    st.markdown(_en.get("note","")[:800])

                    # 연결된 작업 표시
                    _note_tasks = [t for t in _ep_tasks if t.get("source_note_id","") == _en.get("id","")]
                    if _note_tasks:
                        st.markdown("**이 메모에서 생성된 작업:**")
                        _nt_sc = {"완료":"#22c55e","진행중":"#3b82f6","시작전":"#94a3b8","보류":"#f59e0b"}
                        for _nt in _note_tasks:
                            _ntc = _nt_sc.get(_nt.get("status",""), "#94a3b8")
                            st.markdown(
                                f'<div style="background:#f8fafc;border-left:3px solid {_ntc};'
                                f'border-radius:6px;padding:6px 10px;margin-bottom:4px;font-size:0.88rem;">'
                                f'<span style="background:{_ntc};color:white;border-radius:8px;'
                                f'padding:1px 7px;font-size:0.75rem;margin-right:6px;">{_nt.get("status","")}</span>'
                                f'<b>{_nt.get("title","")}</b>'
                                f'</div>', unsafe_allow_html=True
                            )

                    # 빠른 작업 생성 (상위가 expander이므로 중첩 금지 → 체크박스 토글)
                    if st.checkbox("➕ 이 메모에서 작업 만들기", key=f"ep_qt_show_{_en.get('id','')[:8]}"):
                        _qt_title = st.text_input("작업 제목", key=f"ep_qt_title_{_en.get('id','')[:8]}", placeholder="예: 숙소 예약하기")
                        _qt_status = st.selectbox("상태", ["시작전","진행중","완료","보류"], key=f"ep_qt_status_{_en.get('id','')[:8]}")
                        _qt_priority = st.selectbox("우선순위", ["중간","높음","낮음"], key=f"ep_qt_pri_{_en.get('id','')[:8]}")
                        _qt_due = st.text_input("마감일 (YYYY-MM-DD, 선택)", key=f"ep_qt_due_{_en.get('id','')[:8]}")
                        if st.button("✅ 작업 생성", key=f"ep_qt_run_{_en.get('id','')[:8]}", type="primary"):
                            if _qt_title:
                                import uuid as _epuuid
                                _new_task = {
                                    "id": str(_epuuid.uuid4())[:8],
                                    "title": _qt_title,
                                    "project": _en.get("project","기본 프로젝트"),
                                    "status": _qt_status,
                                    "priority": _qt_priority,
                                    "due_date": _qt_due,
                                    "source_note_id": _en.get("id",""),
                                    "source_note_title": _en.get("title",""),
                                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                }
                                st.session_state.setdefault("tasks",[]).append(_new_task)
                                save_persisted_data()
                                _flash(f"작업 '{_qt_title}' 생성 완료!")
                                st.rerun()
                            else:
                                st.warning("작업 제목을 입력해주세요.")

                    # 프로젝트 이동 버튼
                    if st.button("📁 프로젝트 상세", key=f"ep_note_proj_{_en.get('id','')}", use_container_width=False):
                        _proj_name = _en.get("project","")
                        if _proj_name:
                            st.session_state["ep_jump_entity"] = _proj_name
                            st.rerun()

    with _ep_t2:
        # 빠른 작업 추가 (작업 탭 상단)
        with st.expander("➕ 새 작업 빠르게 추가", expanded=False):
            _t2_qc1, _t2_qc2 = st.columns([3,1])
            with _t2_qc1:
                _t2_qtitle = st.text_input("작업 제목", key="ep_t2_qtitle", placeholder="할 일을 입력하세요...")
            with _t2_qc2:
                _t2_qstatus = st.selectbox("상태", ["시작전","진행중","완료","보류"], key="ep_t2_qstatus")
            _t2_proj_default = _ep_name if _ep_type == "📁 프로젝트" else ""
            _t2_proj_opts = ["(선택 안 함)"] + [p.get("name","") for p in _ep_projects]
            _t2_proj = st.selectbox("프로젝트", _t2_proj_opts,
                                    index=_t2_proj_opts.index(_t2_proj_default) if _t2_proj_default in _t2_proj_opts else 0,
                                    key="ep_t2_qproj")
            if st.button("✅ 작업 추가", key="ep_t2_qadd", type="primary", use_container_width=True):
                if _t2_qtitle:
                    import uuid as _ep2uuid
                    _t2_new = {
                        "id": str(_ep2uuid.uuid4())[:8],
                        "title": _t2_qtitle,
                        "project": _t2_proj if _t2_proj != "(선택 안 함)" else "",
                        "status": _t2_qstatus,
                        "priority": "중간",
                        "due_date": "",
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                    st.session_state.setdefault("tasks",[]).append(_t2_new)
                    save_persisted_data()
                    _flash(f"작업 '{_t2_qtitle}' 추가!")
                    st.rerun()

        if not _ep_linked_tasks:
            st.info("연결된 작업이 없어요.")
        else:
            _status_colors = {"완료":"#22c55e","진행중":"#3b82f6","시작전":"#94a3b8","보류":"#f59e0b"}
            for _et in _ep_linked_tasks:
                _sc = _status_colors.get(_et.get("status",""), "#94a3b8")
                _source_note_title = _et.get("source_note_title","")
                st.markdown(
                    f'<div style="background:#f8fafc;border-left:4px solid {_sc};'
                    f'border-radius:8px;padding:10px 14px;margin-bottom:8px;">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                    f'<div><b>{_et.get("title","")}</b> <span style="color:#64748b;font-size:0.85em">· {_et.get("project","")}</span></div>'
                    f'<div style="display:flex;gap:8px;align-items:center;">'
                    f'<span style="background:{_sc};color:white;border-radius:12px;padding:2px 10px;font-size:0.8rem;">{_et.get("status","")}</span>'
                    f'<span style="color:#94a3b8;font-size:0.8rem;">{_et.get("due_date","")}</span>'
                    f'</div></div>'
                    + (f'<div style="font-size:0.78rem;color:#94a3b8;margin-top:4px;">📝 출처: {_source_note_title}</div>' if _source_note_title else "")
                    + f'</div>',
                    unsafe_allow_html=True
                )

    with _ep_t3:
        if not _ep_linked_rels:
            st.info("연결된 관계가 없어요. 데이터 관리 → 관계 관리에서 추가하세요.")
        else:
            _rtype_colors2 = {
                "포함":"#3b82f6","참조":"#8b5cf6","반박":"#ef4444",
                "지지":"#22c55e","확장":"#f59e0b","연결":"#64748b",
                "유사":"#06b6d4","선행":"#ec4899",
            }
            for _er in _ep_linked_rels:
                _is_src = _er.get("source_name") == _ep_name
                _other  = _er.get("target_name") if _is_src else _er.get("source_name")
                _arrow  = "→" if _is_src else "←"
                _rc     = _rtype_colors2.get(_er.get("relation_type",""), "#94a3b8")
                col_r1, col_r2 = st.columns([5, 1])
                with col_r1:
                    st.markdown(
                        f'<div style="background:#f8fafc;border-radius:8px;padding:10px 14px;margin-bottom:6px;">'
                        f'<b style="color:#1e293b">{_ep_name}</b>'
                        f' <span style="background:{_rc};color:white;border-radius:10px;'
                        f'padding:2px 10px;font-size:0.8rem;margin:0 8px">{_arrow} {_er.get("relation_type","")}</span>'
                        f'<b style="color:#1e293b">{_other}</b>'
                        f'<span style="color:#94a3b8;font-size:0.8rem;margin-left:12px">{_er.get("created_at","")}</span>'
                        f'</div>',
                        unsafe_allow_html=True
                    )
                with col_r2:
                    if st.button("🔎", key=f"ep_rel_jump_{_er.get('id',_other)}", help=f"{_other} 상세 보기"):
                        st.session_state["ep_jump_entity"] = _other
                        st.rerun()

    with _ep_t4:
        if not _ep_co_concepts:
            st.info("공통으로 연결된 개념이 없어요.")
        else:
            _cc_cols = st.columns(3)
            for _cci, (_ccname, _cccnt) in enumerate(_ep_co_concepts[:18]):
                with _cc_cols[_cci % 3]:
                    if st.button(
                        f"🧠 {_ccname}  ·  {_cccnt}회",
                        key=f"ep_co_{_cci}_{_ccname[:10]}",
                        use_container_width=True
                    ):
                        st.session_state["ep_jump_entity"] = _ccname
                        st.rerun()

    with _ep_t5:
        st.markdown("**엔터티 정보 편집**")
        if _ep_type in ("🧠 개념", "🔖 AI개념"):
            _cdata_edit = next(
                (c for c in _ep_concepts if isinstance(c,dict) and c.get("name") == _ep_name), None
            )
            if _cdata_edit is None:
                # AI 개념 → 내 개념으로 등록
                st.info("이 개념은 AI 추출 개념이에요. 내 개념으로 등록하면 편집 가능해요.")
                if st.button("📥 내 개념으로 등록", key="ep_reg_ai_concept", type="primary"):
                    st.session_state.setdefault("pkm_custom_concepts", []).append({
                        "name": _ep_name,
                        "folder": "",
                        "description": "",
                        "aliases": [],
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                    save_persisted_data()
                    _flash(f"'{_ep_name}' 등록 완료!")
                    st.rerun()
            else:
                _ed1, _ed2 = st.columns(2)
                with _ed1:
                    _new_desc = st.text_area("설명", _cdata_edit.get("description",""), key="ep_edit_desc")
                    _new_folder = st.text_input("폴더", _cdata_edit.get("folder",""), key="ep_edit_folder")
                with _ed2:
                    _alias_str = ", ".join(_cdata_edit.get("aliases",[]))
                    _new_alias_str = st.text_area("별칭 (쉼표 구분)", _alias_str, key="ep_edit_aliases")
                    st.caption("예: 인공지능, GenAI, 생성형AI")
                if st.button("💾 저장", key="ep_save_edit", type="primary"):
                    _cdata_edit["description"] = _new_desc
                    _cdata_edit["folder"] = _new_folder
                    _cdata_edit["aliases"] = [a.strip() for a in _new_alias_str.split(",") if a.strip()]
                    save_persisted_data()
                    _flash("저장 완료!")
                    st.rerun()

        elif _ep_type == "📁 프로젝트":
            _pdata_edit = next((p for p in _ep_projects if p.get("name") == _ep_name), None)
            if _pdata_edit:
                _ed1p, _ed2p = st.columns(2)
                with _ed1p:
                    _new_pdesc = st.text_area("프로젝트 설명", _pdata_edit.get("description",""), key="ep_edit_pdesc")
                    _new_pstatus = st.selectbox("상태", ["계획중","진행중","완료","보류"], index=["계획중","진행중","완료","보류"].index(_pdata_edit.get("status","계획중")) if _pdata_edit.get("status","계획중") in ["계획중","진행중","완료","보류"] else 0, key="ep_edit_pstatus")
                with _ed2p:
                    _new_ppriority = st.selectbox("우선순위", ["높음","중간","낮음"], key="ep_edit_ppriority")
                    _new_pdue = st.text_input("마감일 (YYYY-MM-DD)", _pdata_edit.get("due_date",""), key="ep_edit_pdue")
                if st.button("💾 프로젝트 저장", key="ep_save_proj", type="primary"):
                    _pdata_edit["description"] = _new_pdesc
                    _pdata_edit["status"] = _new_pstatus
                    _pdata_edit["priority"] = _new_ppriority
                    _pdata_edit["due_date"] = _new_pdue
                    save_persisted_data()
                    _flash("저장 완료!")
                    st.rerun()
        else:
            st.info("태그와 AI 개념은 별도 편집 기능이 없어요. 개념 병합 탭에서 대표 개념으로 통합하거나, 내 개념으로 등록해보세요.")

    st.stop()


if menu == "통합 검색":
    # ══════════════════════════════════════════════════════════
    # 🔍 통합 검색 v1 — 메모·분석·프로젝트·작업·개념·관계·연구노트
    # (검색 로직은 _us_match 한 곳에 모음 → v2에서 임베딩으로 교체 가능)
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="background:linear-gradient(135deg,#334155,#0ea5e9);border-radius:16px;
     padding:26px 30px 20px;margin-bottom:20px;">
    <div style="font-size:1.8rem;font-weight:900;margin-bottom:4px;color:#ffffff;text-shadow:0 1px 4px rgba(0,0,0,0.45);">🔍 통합 검색</div>
    <div style="line-height:1.5;color:#f1f5f9;text-shadow:0 1px 3px rgba(0,0,0,0.4);">
        메모·분석·프로젝트·작업·개념·연구노트를 한 번에 찾아요.
    </div>
</div>
""", unsafe_allow_html=True)

    def _us_match(query, *fields):
        """검색 매칭 + 점수. v2에서 이 함수만 임베딩 기반으로 교체."""
        import re as _re
        text = " ".join([str(f) for f in fields if f]).lower()
        q_tokens = [w for w in _re.split(r"[\s,./]+", query.lower()) if w]
        if not q_tokens:
            return 0
        score = 0
        for w in q_tokens:
            if w in text:
                score += text.count(w) * (2 if len(w) >= 3 else 1)
        return score

    _us_cats_all = ["메모", "분석", "프로젝트", "작업", "개념", "연구노트"]

    # ── 저장된 검색(필터) 빠른 실행 ──
    _saved_searches = st.session_state.get("saved_searches", [])
    if _saved_searches:
        st.caption("⭐ 저장된 검색")
        _ss_cols = st.columns(min(len(_saved_searches), 4) or 1)
        for _ssi, _ss in enumerate(_saved_searches):
            with _ss_cols[_ssi % len(_ss_cols)]:
                if st.button(f"🔍 {_ss.get('q','')}", key=f"us_run_saved_{_ssi}", use_container_width=True):
                    st.session_state["us_query"] = _ss.get("q", "")
                    st.session_state["us_cats"] = _ss.get("cats", _us_cats_all)
                    st.rerun()
                if st.button("🗑️", key=f"us_del_saved_{_ssi}", help="저장된 검색 삭제"):
                    st.session_state["saved_searches"].pop(_ssi)
                    save_persisted_data(); _flash("저장된 검색을 지웠어요."); st.rerun()

    # 추천어 클릭 등 위젯 생성 후 us_query를 바꿀 수 없으므로 pending으로 전달
    if "_us_pending_q" in st.session_state:
        st.session_state["us_query"] = st.session_state.pop("_us_pending_q")

    _us_q = st.text_input("검색어", key="us_query",
                          placeholder="예: 스크린골프, 마케팅, 마감 임박")
    _us_cats = st.multiselect("검색 대상", _us_cats_all, default=_us_cats_all, key="us_cats")

    # ── 🔖 추천 검색어 (카테고리 selectbox + 8개 + 페이지네이션) ──
    _cur_q_lower = (_us_q or "").strip().lower()
    _SUGG_PER_PAGE = 8

    def _collect(seq):
        """후보 리스트를 빈도순 정렬 + 현재 입력값으로 필터링 (전체 반환)."""
        from collections import Counter as _Cnt
        _cnt = _Cnt()
        for _v in seq:
            _v = str(_v).strip()
            if _v:
                _cnt[_v] += 1
        return [
            _w for _w, _ in _cnt.most_common(60)
            if not _cur_q_lower or (_cur_q_lower in _w.lower() and _w.lower() != _cur_q_lower)
        ]

    _cat_recent = _collect([
        _h.get("title", "") for _h in st.session_state.get("search_history", [])[:30]
        if str(_h.get("title", "")).strip() and _h.get("title") != "제목 없음"
    ])
    _cat_tags = _collect([
        str(_t).replace("#", "").strip()
        for _n in st.session_state.get("archive_notes", []) for _t in _n.get("tags", [])
    ])
    _cat_concepts = _collect([
        (_c.get("name") if isinstance(_c, dict) else str(_c)) or ""
        for _c in st.session_state.get("pkm_custom_concepts", [])
    ])
    _cat_projects = _collect([_p.get("name", "") for _p in st.session_state.get("projects", [])])
    _cat_tasks = _collect([_t.get("title", "") for _t in st.session_state.get("tasks", [])])

    # 전체 = 모든 카테고리 합치되 중복 제거(순서 유지)
    _cat_all = list(dict.fromkeys(_cat_recent + _cat_tags + _cat_concepts + _cat_projects + _cat_tasks))
    _SUGG_MAP = {
        "전체": _cat_all, "🕘 최근 검색": _cat_recent, "🏷️ 태그": _cat_tags,
        "🧠 개념": _cat_concepts, "📁 프로젝트": _cat_projects, "✅ 작업": _cat_tasks,
    }
    _has_sugg = any(_SUGG_MAP.values())

    if _has_sugg:
        _scat_col, _ = st.columns([1.4, 4])
        with _scat_col:
            _sel_cat = st.selectbox(
                "추천 카테고리", list(_SUGG_MAP.keys()),
                key="us_suggestion_category", label_visibility="collapsed",
            )
        _items = _SUGG_MAP.get(_sel_cat, [])
        # 카테고리/검색어 바뀌면 페이지 초기화
        _pg_token = f"{_sel_cat}::{_cur_q_lower}"
        if st.session_state.get("_us_sugg_token") != _pg_token:
            st.session_state["_us_sugg_token"] = _pg_token
            st.session_state["us_suggestion_page"] = 0
        _page = st.session_state.get("us_suggestion_page", 0)
        _total_pg = max(1, (len(_items) + _SUGG_PER_PAGE - 1) // _SUGG_PER_PAGE)
        _page = max(0, min(_page, _total_pg - 1))
        _show = _items[_page * _SUGG_PER_PAGE:(_page + 1) * _SUGG_PER_PAGE]

        if _show:
            _cols = st.columns(4)
            for _gi, _gv in enumerate(_show):
                with _cols[_gi % 4]:
                    if st.button(_gv, key=f"us_sugg_{_sel_cat[:2]}_{_page}_{_gi}_{_gv[:8]}", use_container_width=True):
                        st.session_state["_us_pending_q"] = _gv
                        st.rerun()
            if _total_pg > 1:
                _p1, _p2, _p3 = st.columns([1, 2, 1])
                with _p1:
                    if st.button("◀ 이전", key="us_sugg_prev", use_container_width=True, disabled=_page <= 0):
                        st.session_state["us_suggestion_page"] = _page - 1
                        st.rerun()
                with _p2:
                    st.markdown(f"<div style='text-align:center;color:#94a3b8;font-size:12px;padding-top:6px;'>{_page+1} / {_total_pg}</div>", unsafe_allow_html=True)
                with _p3:
                    if st.button("다음 ▶", key="us_sugg_next", use_container_width=True, disabled=_page >= _total_pg - 1):
                        st.session_state["us_suggestion_page"] = _page + 1
                        st.rerun()
        else:
            st.caption("해당 카테고리에 추천 검색어가 없어요.")

    if _us_q.strip():
        _q = _us_q.strip()
        # 결과 항목: (score, title, meta, snip, raw_item)
        _results = {c: [] for c in _us_cats_all}

        if "메모" in _us_cats:
            for n in st.session_state.get("archive_notes", []):
                s = _us_match(_q, n.get("title"), n.get("note"), n.get("original_text"),
                              " ".join([str(t) for t in n.get("tags", [])]), n.get("project"))
                if s > 0:
                    _results["메모"].append((s, n.get("title", "제목 없음"),
                        f"{n.get('project','')} · {n.get('saved_at','')}", n.get("note", "")[:120], n))
        if "분석" in _us_cats:
            for a in st.session_state.get("saved_analyses", []):
                s = _us_match(_q, a.get("title"), a.get("summary"), a.get("note"), a.get("url"),
                              " ".join([str(t) for t in a.get("tags", [])]))
                if s > 0:
                    _results["분석"].append((s, a.get("title", "분석 결과"),
                        f"점수 {a.get('trust_score','?')} · {a.get('saved_at','')}", str(a.get("summary", ""))[:120], a))
        if "프로젝트" in _us_cats:
            for p in st.session_state.get("projects", []):
                s = _us_match(_q, p.get("name"), p.get("description"), p.get("category"), p.get("status"))
                if s > 0:
                    _results["프로젝트"].append((s, p.get("name", "프로젝트"),
                        f"{p.get('category','')} · {p.get('status','')}", str(p.get("description", ""))[:120], p))
        if "작업" in _us_cats:
            for t in st.session_state.get("tasks", []):
                s = _us_match(_q, t.get("title"), t.get("description"), t.get("status"),
                              t.get("project"), t.get("due_date"))
                if s > 0:
                    _results["작업"].append((s, t.get("title", "작업"),
                        f"{t.get('status','')} · 마감 {t.get('due_date','없음')}", str(t.get("description", ""))[:120], t))
        if "개념" in _us_cats:
            for c in st.session_state.get("pkm_custom_concepts", []):
                if isinstance(c, dict):
                    s = _us_match(_q, c.get("name"), c.get("description"), c.get("folder"),
                                  " ".join(c.get("aliases", []) or []))
                    if s > 0:
                        _results["개념"].append((s, c.get("name", "개념"),
                            str(c.get("folder", "")), str(c.get("description", ""))[:120], c))
                else:
                    s = _us_match(_q, c)
                    if s > 0:
                        _results["개념"].append((s, str(c), "", "", {"name": str(c)}))
        if "연구노트" in _us_cats:
            for n in st.session_state.get("archive_notes", []):
                _is_rn = "연구노트" in str(n.get("title", "")) or \
                         any("연구노트" in str(t) for t in n.get("tags", []))
                if not _is_rn:
                    continue
                s = _us_match(_q, n.get("title"), n.get("note"))
                if s > 0:
                    _results["연구노트"].append((s, n.get("title", "연구노트"),
                        str(n.get("saved_at", "")), n.get("note", "")[:120], n))

        _total = sum(len(v) for v in _results.values())

        # ── 결과 헤더 + 이 검색 저장 ──
        _hc1, _hc2 = st.columns([3, 1])
        with _hc1:
            st.markdown(f"#### 🔎 '{_q}' 검색 결과 — 총 **{_total}건**")
        with _hc2:
            _already = any(s.get("q") == _q and s.get("cats") == _us_cats for s in _saved_searches)
            if st.button("⭐ 이 검색 저장", key="us_save_search", use_container_width=True, disabled=_already):
                st.session_state.setdefault("saved_searches", []).append({"q": _q, "cats": list(_us_cats)})
                save_persisted_data(); _flash(f"'{_q}' 검색을 저장했어요."); st.rerun()

        if _total == 0:
            st.info(f"'{_q}'에 대한 결과가 없어요. 다른 키워드로 검색해보세요.")
        else:
            _cat_color = {"메모": "#0ea5e9", "분석": "#6366f1", "프로젝트": "#ec4899",
                          "작업": "#f59e0b", "개념": "#0f766e", "연구노트": "#8b5cf6"}
            for cat in _us_cats_all:
                rows = sorted(_results[cat], key=lambda x: x[0], reverse=True)
                if not rows:
                    continue
                _clr = _cat_color.get(cat, "#64748b")
                st.markdown(f'<div style="margin:16px 0 4px;font-weight:800;color:{_clr};">'
                            f'{cat} ({len(rows)})</div>', unsafe_allow_html=True)
                for _ri, (_s, _title, _meta, _snip, _raw) in enumerate(rows[:10]):
                    with st.container(border=True):
                        st.markdown(
                            f'<div style="font-weight:700;color:#172033;border-left:3px solid {_clr};padding-left:8px;">{_title}</div>'
                            f'<div style="color:#64748b;font-size:12px;margin:2px 0 2px 8px;">{_meta}</div>'
                            f'<div style="color:#475569;font-size:13px;margin-left:8px;">{_snip}</div>',
                            unsafe_allow_html=True)
                        _bcols = st.columns([1, 1, 1])
                        _kbase = f"us_{cat}_{_ri}"
                        # [열기]
                        with _bcols[0]:
                            if st.button("📂 열기", key=f"{_kbase}_open", use_container_width=True):
                                if cat in ("메모", "연구노트"):
                                    _idx = next((i for i, x in enumerate(st.session_state.get("archive_notes", [])) if x is _raw), None)
                                    if _idx is not None:
                                        st.session_state["_archive_note_open_idx"] = _idx
                                    st.query_params["page"] = "archive"
                                elif cat == "분석":
                                    _idx = next((i for i, x in enumerate(st.session_state.get("saved_analyses", [])) if x is _raw), None)
                                    if _idx is not None:
                                        st.session_state["_saved_analysis_open_idx"] = _idx
                                    st.query_params["page"] = "saved"
                                elif cat == "프로젝트":
                                    st.query_params["page"] = "projects"
                                elif cat == "작업":
                                    st.query_params["page"] = "tasks"
                                elif cat == "개념":
                                    st.query_params["page"] = "map"
                                st.rerun()
                        # [작업 만들기] — 메모/개념/연구노트
                        with _bcols[1]:
                            if cat in ("메모", "연구노트", "개념"):
                                if st.button("➕ 작업", key=f"{_kbase}_task", use_container_width=True):
                                    create_task(
                                        f"{_title} 후속 작업",
                                        project=_raw.get("project", "") if cat != "개념" else "",
                                        summary=f"통합 검색 '{_q}'에서 생성",
                                        source_note_id=_raw.get("id", ""),
                                        source_note_title=_title,
                                    )
                                    _flash(f"'{_title}' 작업을 만들었어요.")
                                    st.rerun()
                        # [지식맵] — 개념/프로젝트
                        with _bcols[2]:
                            if cat in ("개념", "프로젝트"):
                                if st.button("🕸️ 지식맵", key=f"{_kbase}_map", use_container_width=True):
                                    st.query_params["page"] = "map"
                                    st.rerun()
                if len(rows) > 10:
                    st.caption(f"… 외 {len(rows) - 10}건 (검색어를 더 구체적으로)")
    else:
        st.caption("검색어를 입력하면 모든 지식에서 한 번에 찾아드려요.")

if menu == "지식 AI":
    # ══════════════════════════════════════════════════════════
    # 🧠 지식 AI (Personal Knowledge AI) — 내 지식 전체에 묻기 (RAG)
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="background:linear-gradient(135deg,#0f766e,#0ea5e9);border-radius:16px;
     padding:28px 32px 22px;margin-bottom:24px;color:white;">
    <div style="font-size:1.9rem;font-weight:900;margin-bottom:6px;">🧠 지식 AI</div>
    <div style="opacity:0.9;line-height:1.6;">
        지금까지 저장한 <b>메모·분석·개념·작업·연구노트</b> 전체를 근거로 AI가 답해요.<br>
        예: "지금까지 조사한 스크린골프 정리해줘", "내 프로젝트 중 마감 임박한 거 뭐야?"
    </div>
</div>
""", unsafe_allow_html=True)

    # ─── 지식 코퍼스 수집 ───────────────────────────────────
    def _pka_build_corpus():
        # text: 검색 매칭용(짧은 메타 포함) / body: 답변 근거용(원문 전체)
        # dconcepts: 문서의 대표 개념 집합 (canonical) — 근거 재정렬용
        # note_id별 연결 개념 prebuild
        _links_by_note = {}
        for _lk in st.session_state.get("note_concept_links", []):
            if _lk.get("note_id") and _lk.get("concept"):
                _links_by_note.setdefault(_lk["note_id"], []).append(_lk["concept"])
        docs = []
        for n in st.session_state.get("archive_notes", []):
            _meta = " ".join([
                str(n.get("title", "")),
                " ".join([str(t) for t in n.get("tags", [])]),
                str(n.get("project", "")),
            ])
            _orig = str(n.get("original_text", "")).strip()
            _note = str(n.get("note", "")).strip()
            # original_text가 의미있게 있으면 우선 사용, 없으면 note 사용
            _body = _orig if len(_orig) >= 100 else _note
            if _note and _note not in _body:  # 내 메모는 항상 앞에 덧붙임
                _body = (_note + "\n\n" + _body).strip()
            _dcon = set(canonical_concepts(
                list(n.get("concepts", []) or []) + _links_by_note.get(n.get("id"), [])))
            docs.append({
                "kind": "메모", "title": n.get("title", "제목 없음"),
                "text": _meta + " " + _body, "body": _body, "date": str(n.get("saved_at", "")),
                "dconcepts": _dcon,
            })
        for a in st.session_state.get("saved_analyses", []):
            _summary = a.get("summary", "")
            if isinstance(_summary, list):
                _summary = "\n".join([str(s) for s in _summary])
            _body = " ".join([str(_summary), str(a.get("note", ""))]).strip()
            _meta = " ".join([str(a.get("title", "")), str(a.get("url", "")),
                              " ".join([str(t) for t in a.get("tags", [])])])
            _dcon = set(canonical_concepts(
                list(a.get("concepts", []) or []) + list(a.get("tags", []) or [])))
            docs.append({
                "kind": "분석", "title": a.get("title", "분석 결과"),
                "text": _meta + " " + _body, "body": _body, "date": str(a.get("saved_at", "")),
                "dconcepts": _dcon,
            })
        for c in st.session_state.get("pkm_custom_concepts", []):
            if isinstance(c, dict):
                _body = " ".join([str(c.get("name", "")), str(c.get("description", "")),
                                  " ".join(c.get("aliases", []) or [])])
                _txt = _body + " " + str(c.get("folder", ""))
                _ti = c.get("name", "개념")
                _dcon = set(canonical_concepts([c.get("name", "")] + (c.get("aliases", []) or [])))
            else:
                _txt = _body = str(c); _ti = str(c)
                _dcon = set(canonical_concepts([str(c)]))
            docs.append({"kind": "개념", "title": _ti, "text": _txt, "body": _body,
                         "date": "", "dconcepts": _dcon})
        for t in st.session_state.get("tasks", []):
            _txt = " ".join([str(t.get("title", "")), str(t.get("description", "")),
                             str(t.get("status", "")), str(t.get("project", "")),
                             str(t.get("due_date", ""))])
            _dcon = set(canonical_concepts(t.get("linked_concepts", []) or []))
            docs.append({"kind": "작업", "title": t.get("title", "작업"),
                         "text": _txt, "body": _txt, "date": str(t.get("due_date", "")),
                         "dconcepts": _dcon})
        for p in st.session_state.get("projects", []):
            _txt = " ".join([str(p.get("name", "")), str(p.get("description", "")),
                             str(p.get("category", "")), str(p.get("status", ""))])
            docs.append({"kind": "프로젝트", "title": p.get("name", "프로젝트"),
                         "text": _txt, "body": _txt, "date": str(p.get("created_at", "")),
                         "dconcepts": set()})
        return docs

    def _q_tokens(query):
        """질문 토큰 + 조사 제거 정규화 토큰 (len>=2). '역전파가' → '역전파' 포함."""
        import re as _re
        _toks = set()
        for _w in _re.split(r"[\s,./?!()\[\]]+", query.lower()):
            if len(_w) >= 2:
                _toks.add(_w)
            _nw = normalize_concept_token(_w).lower()
            if len(_nw) >= 2:
                _toks.add(_nw)
        return _toks

    def _pka_score(query, text):
        # 조사 정규화 토큰으로 부분 일치 카운트 ('역전파'가 '역전파 알고리즘'에 잡힘)
        _toks = _q_tokens(query)
        if not _toks:
            return 0
        t_low = text.lower()
        score = 0
        for w in _toks:
            score += t_low.count(w) * (2 if len(w) >= 3 else 1)
        return score

    # 질문에서 핵심 개념 추출 (알려진 개념 기준 + 별칭 + 토큰 정규화 + 부분 일치)
    _known_concepts = {c for c, _ in concept_frequency()}

    def _extract_q_concepts(q):
        _ql = q.lower()
        out = set()
        # 1) 알려진 대표 개념이 질문에 등장
        for _c in _known_concepts:
            if _c and _c.lower() in _ql:
                out.add(_c)
        # 2) 별칭이 질문에 등장 → 대표 개념으로
        for _canon, _als in (st.session_state.get("concept_aliases", {}) or {}).items():
            for _a in (_als or []):
                if str(_a).strip() and str(_a).lower() in _ql:
                    out.add(_canon)
        # 3) 토큰 정규화 (알려진 개념일 때 채택)
        _qtoks = _q_tokens(q)
        for _w in _qtoks:
            _cc = canonical_concept(_w)
            if _cc and _cc in _known_concepts:
                out.add(_cc)
        # 4) 부분 일치: 질문 토큰이 개념에 포함되거나 그 반대 ('역전파' ↔ '역전파 알고리즘')
        for _c in _known_concepts:
            _cl = _c.lower()
            for _qt in _qtoks:
                if len(_qt) >= 2 and (_qt in _cl or _cl in _qt):
                    out.add(_c)
                    break
        return out

    _pka_docs = _pka_build_corpus()
    _pka_kinds = {}
    for d in _pka_docs:
        _pka_kinds[d["kind"]] = _pka_kinds.get(d["kind"], 0) + 1
    st.caption("📚 검색 가능한 지식: " + " · ".join([f"{k} {v}개" for k, v in _pka_kinds.items()]) if _pka_kinds else "📚 아직 저장된 지식이 없어요.")

    _pka_q = st.text_input("무엇이든 물어보세요", key="pka_query",
                           placeholder="예: 지금까지 조사한 스크린골프 핵심만 정리해줘")
    _pka_go = st.button("🧠 내 지식에서 답 찾기", type="primary", use_container_width=True, key="pka_go")

    if _pka_go and _pka_q.strip():
        if not _pka_docs:
            st.warning("저장된 지식이 없어요. 먼저 메모나 분석을 저장해주세요.")
        else:
            # ── 질문 유형 감지: '깊게 설명' 모드 ──
            _deep_kw = ["쉽게 설명", "쉽게설명", "자세히 설명", "자세히설명", "내가 준",
                        "내가준", "이 글 기반", "이글 기반", "이 글로", "초등학생",
                        "정리해줘", "정리 해줘", "공부용", "공부 용", "풀어서", "이해하게",
                        "원문 기반", "원문기반"]
            _q_low = _pka_q.lower()
            _deep_mode = any(k in _q_low for k in _deep_kw)

            # ── 근거 재정렬: 텍스트 점수 + 개념 일치 점수 ──
            _q_concepts = _extract_q_concepts(_pka_q)
            _CMATCH_BOOST = 6          # 개념 1개 일치당 가중
            _cand = []
            for d in _pka_docs:
                _ts = _pka_score(_pka_q, d["text"])
                _cm = len(d.get("dconcepts", set()) & _q_concepts)
                if _ts <= 0 and _cm == 0:
                    continue
                d["cmatch"] = _cm
                _cand.append((d, _ts + _cm * _CMATCH_BOOST, _ts, _cm))

            # 질문에 핵심 개념이 있으면, 개념 일치 0점 후보 컷 (단 일치 후보가 3개 이상일 때만)
            _insufficient = False
            if _q_concepts:
                _with_c = [x for x in _cand if x[3] > 0]
                if len(_with_c) >= 3:
                    _cand = _with_c
                elif not _with_c:
                    _insufficient = True   # 개념 일치 근거가 전혀 없음

            _cand.sort(key=lambda x: x[1], reverse=True)
            # 하위 30% 컷 (최소 3개 보장)
            if len(_cand) > 4:
                _keep = max(3, int(len(_cand) * 0.7))
                _cand = _cand[:_keep]

            _scored = [(d, sc) for d, sc, ts, cm in _cand]

            if not _scored:
                st.info("관련된 지식을 찾지 못했어요. 다른 키워드로 물어보세요.")
            else:
                if _insufficient:
                    st.warning("질문의 핵심 개념과 일치하는 저장 지식이 적어요. "
                               "저장된 지식만으로는 충분하지 않을 수 있어요 — 관련 메모를 더 저장하면 답이 정확해져요.")
                # ── 모드별 문서/길이 결정 ──
                if _deep_mode:
                    # 가장 강한 문서를 길게 + 보조 1~2개
                    _top_scored = _scored[:3]
                    _strong_len = 5000   # 최강 매칭 문서
                    _aux_len = 1200      # 보조 문서
                else:
                    _top_scored = _scored[:8]
                    _strong_len = 1500
                    _aux_len = 800

                _top = [d for d, s in _top_scored]
                _ctx_parts = []
                _ctx_lengths = []
                for i, (d, s) in enumerate(_top_scored, 1):
                    _limit = _strong_len if i == 1 else _aux_len
                    _snip = (d.get("body") or d.get("text") or "")[:_limit]
                    _ctx_lengths.append((d["title"], len(_snip), s))
                    _ctx_parts.append(f"[{i}] ({d['kind']}) {d['title']}\n{_snip}")
                _context = "\n\n".join(_ctx_parts)

                if _deep_mode:
                    _sys = (
                        "너는 사용자의 개인 지식 비서이자 친절한 설명 선생님이야. "
                        "아래 제공된 '내 지식'(사용자가 저장한 원문)을 깊게 읽고, 사용자가 이해하기 쉽게 한국어로 다시 설명해. "
                        "원문에 충분한 내용이 있으면 절대 '추가 정보가 필요하다'고 말하지 마. 원문을 끝까지 활용해서 최대한 풍부하게 설명해. "
                        "정말 원문에 전혀 없는 내용일 때만 '저장된 지식에는 없어요'라고 말해.\n\n"
                        "다음 형식(마크다운)으로 답해:\n"
                        "**한 줄 핵심**\n(핵심을 한 문장으로)\n\n"
                        "**쉬운 비유**\n(일상적 비유로)\n\n"
                        "**단계별 설명**\n1. ...\n2. ...\n(원문 흐름대로 단계별로)\n\n"
                        "**원문에서 나온 핵심 개념**\n- 개념: 짧은 설명\n\n"
                        "**주의점 / 한계**\n- ...\n\n"
                        "각 핵심 주장 끝에는 근거 번호 [1],[2]를 자연스럽게 표기해."
                    )
                else:
                    _sys = (
                        "너는 사용자의 개인 지식 비서야. 아래 제공된 '내 지식'만을 근거로 한국어로 답해. "
                        "원문에 정보가 있으면 충분히 활용하고, 함부로 '추가 정보가 필요하다'고 하지 마. "
                        "정말 지식에 없는 내용만 '저장된 지식에는 없어요'라고 말해. "
                        "답변은 핵심 요약 → 근거 정리 순서로, 마크다운 불릿으로 깔끔하게. "
                        "각 핵심 주장 끝에는 근거 번호 [1],[2]를 표기해."
                    )
                _usr = f"질문: {_pka_q}\n\n=== 내 지식 ===\n{_context}"
                with st.spinner("내 지식을 깊게 읽고 정리하는 중..." if _deep_mode else "내 지식을 읽고 정리하는 중..."):
                    try:
                        _ans = call_groq_simple(_sys, _usr)
                    except Exception as e:
                        _ans = f"AI 호출 중 오류가 났어요: {e}"
                st.markdown("### 💬 답변")
                if _deep_mode:
                    st.caption("🔍 깊게 설명 모드 — 가장 관련 높은 원문을 길게 읽어 설명했어요.")
                st.markdown(_ans)
                st.markdown("### 🔖 참고한 지식")
                if _q_concepts:
                    st.caption("개념 일치 점수순으로 재정렬했어요. 핵심 개념: "
                               + " ".join(f"`{c}`" for c in list(_q_concepts)[:8]))
                for i, d in enumerate(_top, 1):
                    _badge = {"메모": "#0ea5e9", "분석": "#6366f1", "개념": "#0f766e",
                              "작업": "#f59e0b", "프로젝트": "#ec4899"}.get(d["kind"], "#64748b")
                    _cm = d.get("cmatch", 0)
                    _cm_html = (f' <span style="background:#dcfce7;color:#15803d;font-size:11px;'
                                f'padding:1px 6px;border-radius:8px;">🎯 개념 {_cm}</span>') if _cm else ""
                    st.markdown(
                        f'<div style="border-left:3px solid {_badge};padding:6px 12px;margin:4px 0;'
                        f'background:#f8fafc;border-radius:6px;">'
                        f'<b>[{i}]</b> <span style="color:{_badge};font-weight:700;">{d["kind"]}</span> '
                        f'· {d["title"]}{_cm_html} <span style="color:#94a3b8;font-size:12px;">{d["date"]}</span></div>',
                        unsafe_allow_html=True)
                with st.expander("🛠️ 디버그 — AI가 실제로 읽은 내용", expanded=False):
                    st.write(f"**모드:** {'깊게 설명' if _deep_mode else '일반 검색'}")
                    st.write(f"**선택된 문서 수:** {len(_top)}개")
                    st.write("**문서별 context 길이 / 매칭 점수:**")
                    for _t, _ln, _sc in _ctx_lengths:
                        st.write(f"- {_t} — {_ln}자 (점수 {_sc})")
                    st.write(f"**전체 context 길이:** {len(_context)}자")
                    st.text_area("실제 prompt context (앞 1500자)", _context[:1500],
                                 height=200, key="pka_debug_ctx")

if menu == "AI 브레인스토밍":
    # ══════════════════════════════════════════════════════════
    # 🤖 AI 브레인스토밍 — 독립 페이지
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="background:linear-gradient(135deg,#1e3a8a,#6366f1);border-radius:16px;
     padding:28px 32px 22px;margin-bottom:24px;color:white;">
    <div style="font-size:1.9rem;font-weight:900;margin-bottom:6px;">🤖 AI 브레인스토밍</div>
    <div style="opacity:0.85;line-height:1.6;">
        저장된 메모나 프로젝트를 기반으로 AI가 새로운 관점·아이디어·다음 행동을 제안해요.
    </div>
</div>
""", unsafe_allow_html=True)

    _br_notes   = st.session_state.get("archive_notes", [])
    _br_projs   = st.session_state.get("projects", [])
    _br_tasks   = st.session_state.get("tasks", [])

    _br_tab1, _br_tab2, _br_tab3, _br_tab4 = st.tabs(["📝 메모 기반", "📁 프로젝트 기반", "🔀 크로스 분석", "🔬 AI 연구노트"])

    # ─── 탭 1: 메모 기반 ───────────────────────────────────
    with _br_tab1:
        if not _br_notes:
            st.info("저장된 메모가 없어요. 먼저 분석 결과를 저장해보세요.")
        else:
            _brc1, _brc2 = st.columns([2, 1])
            with _brc1:
                _note_titles_br = [n.get("title", "제목 없음") for n in _br_notes]
                _br_note_idx = st.selectbox("📝 메모 선택", range(len(_note_titles_br)),
                    format_func=lambda i: _note_titles_br[i], key="br_note_sel")
                _sel_note_br = _br_notes[_br_note_idx]
                with st.expander("선택한 메모 미리보기", expanded=False):
                    st.markdown(f"**프로젝트:** {_sel_note_br.get('project','')}")
                    st.markdown(f"**태그:** {', '.join([str(t) for t in _sel_note_br.get('tags',[])])}")
                    st.markdown("---")
                    render_readable_markdown(_sel_note_br.get("note", ""), max_chars=2000)
            with _brc2:
                _br_types_memo = st.multiselect(
                    "분석 유형 선택",
                    ["확장 주제 제안", "추가 조사 질문", "반대 관점", "발표 문장 초안", "연결 개념 찾기", "다음 할 일", "약점 분석", "연관 프로젝트 제안"],
                    default=["확장 주제 제안", "다음 할 일"],
                    key="br_types_memo"
                )
                _br_depth = st.radio("분석 깊이", ["간단히 (3개씩)", "상세히 (5개씩)"], key="br_depth", horizontal=True)
                _depth_n = 3 if "간단히" in _br_depth else 5

            if st.button("🤖 AI 브레인스토밍 시작", key="br_run_note", type="primary", use_container_width=True):
                if not _br_types_memo:
                    st.warning("분석 유형을 하나 이상 선택해주세요.")
                else:
                    _note_content = _sel_note_br.get("note", "")[:3000]
                    _sys = f"""당신은 지식 관리 전문가이자 비판적 사고 코치입니다.
사용자의 메모를 읽고 요청한 분석 유형별로 구체적이고 실용적인 제안을 해주세요.
분석 유형: {', '.join(_br_types_memo)}
각 유형별로 {_depth_n}개의 구체적인 항목을 bullet point (- )로 제안하세요.
각 항목은 한 문장으로 명확하게 써주세요. 한국어로 답변하세요."""
                    _usr = f"메모 제목: {_sel_note_br.get('title','')}\n프로젝트: {_sel_note_br.get('project','')}\n태그: {', '.join([str(t) for t in _sel_note_br.get('tags',[])])}\n\n메모 내용:\n{_note_content}"
                    with st.spinner("AI가 브레인스토밍 중... (10~20초)"):
                        try:
                            _br_result = call_groq_simple(_sys, _usr)
                            st.session_state["br_note_result"] = _br_result
                            st.session_state["br_note_result_title"] = _sel_note_br.get("title","")
                        except Exception as _e:
                            st.error(f"AI 오류: {_e}")

            if st.session_state.get("br_note_result"):
                st.divider()
                _br_r1, _br_r2 = st.columns([4, 1])
                with _br_r1:
                    st.markdown(f"#### 💡 AI 브레인스토밍 결과 — {st.session_state.get('br_note_result_title','')}")
                with _br_r2:
                    if st.button("🗑️ 지우기", key="br_note_clear"):
                        st.session_state.pop("br_note_result", None)
                        st.rerun()
                st.markdown(st.session_state["br_note_result"])
                st.divider()
                # 결과를 작업으로 추가
                st.markdown("##### ➕ 결과에서 작업 만들기")
                _br_task_title = st.text_input("작업 제목", key="br_add_task_title",
                    placeholder="AI 제안 중 실행할 항목 입력")
                _br_task_proj = st.selectbox("연결할 프로젝트",
                    ["없음"] + [p.get("name","") for p in _br_projs], key="br_add_task_proj")
                if _br_task_title and st.button("✅ 작업으로 추가", key="br_add_task_btn", type="primary"):
                    import uuid as _buid
                    st.session_state.setdefault("tasks", []).append({
                        "id": f"task_{_buid.uuid4().hex[:8]}",
                        "title": _br_task_title.strip(),
                        "project": "" if _br_task_proj == "없음" else _br_task_proj,
                        "status": "시작전",
                        "priority": "중간",
                        "due_date": "",
                        "note": f"[AI 브레인스토밍] {st.session_state.get('br_note_result_title','')}",
                        "user_id": "local_user",
                        "deleted_at": None,
                    })
                    save_persisted_data()
                    _flash(f"✅ '{_br_task_title.strip()}' 작업이 추가됐어요!")
                    st.rerun()

    # ─── 탭 2: 프로젝트 기반 ─────────────────────────────────
    with _br_tab2:
        if not _br_projs:
            st.info("저장된 프로젝트가 없어요. 먼저 프로젝트를 만들어보세요.")
        else:
            _br_proj_names = [p.get("name","이름 없음") for p in _br_projs]
            _brc3, _brc4 = st.columns([2, 1])
            with _brc3:
                _br_proj_idx = st.selectbox("📁 프로젝트 선택", range(len(_br_proj_names)),
                    format_func=lambda i: _br_proj_names[i], key="br_proj_sel")
                _sel_proj_br = _br_projs[_br_proj_idx]
                _proj_notes_br = [n for n in _br_notes if n.get("project") == _sel_proj_br.get("name")]
                _proj_tasks_br = [t for t in _br_tasks if t.get("project") == _sel_proj_br.get("name")]
                _bc1, _bc2, _bc3 = st.columns(3)
                with _bc1: st.metric("연결 메모", f"{len(_proj_notes_br)}개")
                with _bc2: st.metric("연결 작업", f"{len(_proj_tasks_br)}개")
                with _bc3: st.metric("진행률", f"{_sel_proj_br.get('progress',0)}%")
                if _proj_notes_br:
                    with st.expander("연결된 메모 목록", expanded=False):
                        for _pn in _proj_notes_br[:5]:
                            st.markdown(f"- **{_pn.get('title','')}** ({_pn.get('saved_at','')[:10]})")
            with _brc4:
                _br_types_proj = st.multiselect(
                    "분석 유형 선택",
                    ["부족한 자료 파악", "추가 조사 방향", "발표 목차 제안", "예상 질문", "추가 작업 아이디어", "리스크 분석", "완료 기준 제안"],
                    default=["부족한 자료 파악", "추가 작업 아이디어"],
                    key="br_types_proj"
                )
                _br_depth2 = st.radio("분석 깊이", ["간단히 (3개씩)", "상세히 (5개씩)"], key="br_depth2", horizontal=True)
                _depth_n2 = 3 if "간단히" in _br_depth2 else 5

            if st.button("🤖 프로젝트 AI 분석 시작", key="br_run_proj", type="primary", use_container_width=True):
                if not _br_types_proj:
                    st.warning("분석 유형을 하나 이상 선택해주세요.")
                else:
                    _proj_summary = f"프로젝트명: {_sel_proj_br.get('name')}\n설명: {_sel_proj_br.get('description','')}\n상태: {_sel_proj_br.get('status','')}\n진행률: {_sel_proj_br.get('progress',0)}%"
                    _notes_summary = "\n".join([f"- {n.get('title','')}: {n.get('note','')[:150]}" for n in _proj_notes_br[:6]])
                    _tasks_summary = "\n".join([f"- [{t.get('status','')}] {t.get('title','')}" for t in _proj_tasks_br[:6]])
                    _sys2 = f"""당신은 프로젝트 관리 전문가입니다.
프로젝트 정보와 연결된 메모·작업을 보고 요청한 유형별 분석을 해주세요.
분석 유형: {', '.join(_br_types_proj)}
각 유형별로 {_depth_n2}개의 구체적인 항목을 bullet point (- )로 제안하세요. 한국어로 답변하세요."""
                    _usr2 = f"{_proj_summary}\n\n연결된 메모:\n{_notes_summary if _notes_summary else '(없음)'}\n\n작업 현황:\n{_tasks_summary if _tasks_summary else '(없음)'}"
                    with st.spinner("AI가 프로젝트를 분석 중... (10~20초)"):
                        try:
                            _br_proj_result = call_groq_simple(_sys2, _usr2)
                            st.session_state["br_proj_result"] = _br_proj_result
                            st.session_state["br_proj_result_name"] = _sel_proj_br.get("name","")
                        except Exception as _e:
                            st.error(f"AI 오류: {_e}")

            if st.session_state.get("br_proj_result"):
                st.divider()
                _br_pr1, _br_pr2 = st.columns([4, 1])
                with _br_pr1:
                    st.markdown(f"#### 💡 프로젝트 AI 분석 결과 — {st.session_state.get('br_proj_result_name','')}")
                with _br_pr2:
                    if st.button("🗑️ 지우기", key="br_proj_clear"):
                        st.session_state.pop("br_proj_result", None)
                        st.rerun()
                st.markdown(st.session_state["br_proj_result"])
                st.divider()
                # 결과를 작업으로 추가
                st.markdown("##### ➕ 결과에서 작업 만들기")
                _br_task_title2 = st.text_input("작업 제목", key="br_add_proj_task_title",
                    placeholder="AI 제안 중 실행할 항목 입력")
                if _br_task_title2 and st.button("✅ 작업으로 추가", key="br_add_proj_task_btn", type="primary"):
                    import uuid as _buid2
                    st.session_state.setdefault("tasks", []).append({
                        "id": f"task_{_buid2.uuid4().hex[:8]}",
                        "title": _br_task_title2.strip(),
                        "project": st.session_state.get("br_proj_result_name",""),
                        "status": "시작전",
                        "priority": "중간",
                        "due_date": "",
                        "note": f"[AI 브레인스토밍] {st.session_state.get('br_proj_result_name','')}",
                        "user_id": "local_user",
                        "deleted_at": None,
                    })
                    save_persisted_data()
                    _flash(f"✅ '{_br_task_title2.strip()}' 작업이 추가됐어요!")
                    st.rerun()

    # ─── 탭 3: 크로스 분석 ────────────────────────────────────
    with _br_tab3:
        st.markdown("#### 🔀 크로스 분석 — 여러 메모를 비교해서 공통 패턴·차이점·연결고리 찾기")
        if len(_br_notes) < 2:
            st.info("메모가 2개 이상 있어야 해요. 더 많은 분석 결과를 저장해보세요.")
        else:
            _cross_titles = [n.get("title","제목 없음") for n in _br_notes]
            _cross_sel = st.multiselect("비교할 메모 선택 (2~4개 권장)", _cross_titles,
                default=_cross_titles[:min(2, len(_cross_titles))], key="br_cross_sel")
            _cross_types = st.multiselect(
                "분석 유형",
                ["공통 핵심 개념", "주장 충돌 지점", "보완 관계", "시간순 흐름", "종합 인사이트"],
                default=["공통 핵심 개념", "종합 인사이트"],
                key="br_cross_types"
            )
            if len(_cross_sel) >= 2 and st.button("🔀 크로스 분석 시작", key="br_cross_run", type="primary", use_container_width=True):
                _cross_notes_content = []
                for _ct in _cross_sel:
                    _cn = next((n for n in _br_notes if n.get("title","제목 없음")==_ct), None)
                    if _cn:
                        _cross_notes_content.append(f"[{_ct}]\n{_cn.get('note','')[:600]}")
                _sys3 = f"""당신은 비교 분석 전문가입니다.
여러 메모를 함께 분석하고 요청한 유형별 인사이트를 도출해주세요.
분석 유형: {', '.join(_cross_types)}
각 유형별로 3~5개의 구체적인 발견을 bullet point (- )로 써주세요. 한국어로 답변하세요."""
                _usr3 = "분석할 메모들:\n\n" + "\n\n---\n\n".join(_cross_notes_content)
                with st.spinner("AI가 비교 분석 중... (10~20초)"):
                    try:
                        _cross_result = call_groq_simple(_sys3, _usr3)
                        st.session_state["br_cross_result"] = _cross_result
                    except Exception as _e:
                        st.error(f"AI 오류: {_e}")
            if st.session_state.get("br_cross_result"):
                st.divider()
                _brcr1, _brcr2 = st.columns([4,1])
                with _brcr1:
                    st.markdown("#### 💡 크로스 분석 결과")
                with _brcr2:
                    if st.button("🗑️ 지우기", key="br_cross_clear"):
                        st.session_state.pop("br_cross_result", None)
                        st.rerun()
                st.markdown(st.session_state["br_cross_result"])

    # ─── 탭 4: AI 연구노트 ────────────────────────────────────
    with _br_tab4:
        st.markdown("#### 🔬 AI 연구노트 — 메모 여러 개를 종합해 연구노트 만들기")
        st.caption("선택한 메모들의 공통점·충돌점·새 아이디어·다음 작업을 AI가 구조화해서 정리해요. 결과는 연구노트로 저장하고 작업까지 만들 수 있어요.")

        if len(_br_notes) < 2:
            st.info("메모가 2개 이상 있어야 해요. 더 많은 분석 결과를 저장해보세요.")
        else:
            # 프로젝트/태그로 메모 후보 좁히기
            _rn_f1, _rn_f2 = st.columns(2)
            with _rn_f1:
                _rn_proj_opts = ["전체"] + sorted({n.get("project","") for n in _br_notes if n.get("project")})
                _rn_proj_filter = st.selectbox("프로젝트 필터", _rn_proj_opts, key="rn_proj_filter")
            with _rn_f2:
                _rn_all_tags = sorted({str(t).replace("#","").strip() for n in _br_notes for t in n.get("tags",[])})
                _rn_tag_filter = st.selectbox("태그 필터", ["전체"] + _rn_all_tags, key="rn_tag_filter")

            _rn_cands = _br_notes
            if _rn_proj_filter != "전체":
                _rn_cands = [n for n in _rn_cands if n.get("project","") == _rn_proj_filter]
            if _rn_tag_filter != "전체":
                _rn_cands = [n for n in _rn_cands if _rn_tag_filter in [str(t).replace("#","").strip() for t in n.get("tags",[])]]

            _rn_titles = [n.get("title","제목 없음") for n in _rn_cands]
            _rn_sel = st.multiselect("📝 종합할 메모 선택 (2~6개 권장)", _rn_titles,
                default=_rn_titles[:min(3, len(_rn_titles))], key="rn_sel")

            _rn_topic = st.text_input("연구 주제 (선택) — 비우면 AI가 자동 도출",
                key="rn_topic", placeholder="예: 결식아동 지원 정책의 사각지대")

            if len(_rn_sel) >= 2 and st.button("🔬 AI 연구노트 생성", key="rn_run", type="primary", use_container_width=True):
                _rn_content = []
                for _rt in _rn_sel:
                    _rnn = next((n for n in _rn_cands if n.get("title","제목 없음")==_rt), None)
                    if _rnn:
                        _rn_content.append(
                            f"[{_rt}] (프로젝트: {_rnn.get('project','')}, 신뢰도: {_rnn.get('score',0)}점)\n{_rnn.get('note','')[:700]}"
                        )
                _rn_sys = """당신은 연구 분석 전문가입니다. 여러 메모를 종합해서 하나의 연구노트를 작성하세요.
반드시 아래 4개 섹션 구조를 그대로 사용하세요 (마크다운 ## 헤더):

## 🔗 공통점
여러 메모에서 반복되는 핵심 주제·개념·주장 (3~5개 bullet)

## ⚡ 충돌점
서로 다르거나 모순되는 관점·데이터·주장 (있으면 2~4개, 없으면 '뚜렷한 충돌 없음')

## 💡 새 아이디어
메모들을 결합해서 도출되는 새로운 통찰·가설·연결고리 (3~5개 bullet)

## ✅ 다음 작업
구체적이고 실행 가능한 다음 행동. 각 줄은 반드시 '- ' 로 시작하는 한 문장 (3~5개)

한국어로, 구체적이고 실용적으로 작성하세요."""
                _rn_usr = (f"연구 주제: {_rn_topic}\n\n" if _rn_topic else "") + "종합할 메모들:\n\n" + "\n\n---\n\n".join(_rn_content)
                with st.spinner("AI가 연구노트를 작성 중... (15~30초)"):
                    try:
                        _rn_result = call_groq_simple(_rn_sys, _rn_usr)
                        st.session_state["rn_result"] = _rn_result
                        st.session_state["rn_result_topic"] = _rn_topic or "AI 연구노트"
                        st.session_state["rn_result_sources"] = _rn_sel
                    except Exception as _e:
                        st.error(f"AI 오류: {_e}")

            if st.session_state.get("rn_result"):
                st.divider()
                _rnr1, _rnr2 = st.columns([4,1])
                with _rnr1:
                    st.markdown(f"### 📔 {st.session_state.get('rn_result_topic','AI 연구노트')}")
                with _rnr2:
                    if st.button("🗑️ 지우기", key="rn_clear"):
                        for _k in ["rn_result","rn_result_topic","rn_result_sources"]:
                            st.session_state.pop(_k, None)
                        st.rerun()
                st.caption(f"종합 메모: {', '.join(st.session_state.get('rn_result_sources',[]))}")
                st.markdown(st.session_state["rn_result"])

                st.divider()
                # ── 연구노트 저장 ──
                _save_c1, _save_c2 = st.columns(2)
                with _save_c1:
                    st.markdown("##### 💾 연구노트로 저장")
                    _rn_save_proj = st.selectbox("저장할 프로젝트",
                        ["기본 프로젝트"] + [p.get("name","") for p in _br_projs], key="rn_save_proj")
                    if st.button("📔 지식 아카이브에 저장", key="rn_save_btn", type="primary", use_container_width=True):
                        import uuid as _rnuuid
                        _rn_note_id = str(_rnuuid.uuid4())[:8]
                        st.session_state.setdefault("archive_notes", []).append({
                            "id": _rn_note_id,
                            "url": "",
                            "title": f"[연구노트] {st.session_state.get('rn_result_topic','AI 연구노트')}",
                            "project": _rn_save_proj,
                            "section": "AI 연구노트",
                            "content_type": "research_note",
                            "score": 0,
                            "favorite": False,
                            "tags": ["연구노트", "AI종합"],
                            "note": st.session_state["rn_result"],
                            "original_text": "출처 메모: " + ", ".join(st.session_state.get("rn_result_sources",[])),
                            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        })
                        save_persisted_data()
                        st.success("📔 연구노트가 지식 아카이브에 저장됐어요!")
                with _save_c2:
                    st.markdown("##### ✅ '다음 작업'에서 작업 만들기")
                    # 결과에서 ✅ 다음 작업 섹션의 bullet 추출
                    _rn_text = st.session_state["rn_result"]
                    _rn_tasks_lines = []
                    _in_task_sec = False
                    for _ln in _rn_text.split("\n"):
                        if "다음 작업" in _ln and _ln.strip().startswith("#"):
                            _in_task_sec = True
                            continue
                        if _in_task_sec:
                            if _ln.strip().startswith("#"):
                                break
                            if _ln.strip().startswith("-"):
                                _rn_tasks_lines.append(_ln.strip().lstrip("-").strip())
                    if _rn_tasks_lines:
                        _rn_pick_tasks = st.multiselect("추가할 작업 선택", _rn_tasks_lines,
                            default=_rn_tasks_lines, key="rn_pick_tasks")
                        _rn_task_proj = st.selectbox("작업 프로젝트",
                            ["기본 프로젝트"] + [p.get("name","") for p in _br_projs], key="rn_task_proj")
                        if _rn_pick_tasks and st.button("✅ 선택 작업 일괄 추가", key="rn_add_tasks", use_container_width=True):
                            import uuid as _rntuuid
                            for _tl in _rn_pick_tasks:
                                st.session_state.setdefault("tasks", []).append({
                                    "id": f"task_{_rntuuid.uuid4().hex[:8]}",
                                    "title": _tl[:120],
                                    "project": _rn_task_proj,
                                    "status": "시작 전",
                                    "priority": "보통",
                                    "due_date": "",
                                    "note": f"[AI 연구노트] {st.session_state.get('rn_result_topic','')}",
                                    "user_id": "local_user",
                                    "deleted_at": None,
                                })
                            save_persisted_data()
                            _flash(f"✅ {len(_rn_pick_tasks)}개 작업 추가 완료!")
                            st.rerun()
                    else:
                        st.caption("결과에서 '다음 작업' 항목을 찾지 못했어요. 다시 생성해보세요.")

    st.stop()


# ══════════════════════════════════════════════════════════
# 📈 패턴 분석
# ══════════════════════════════════════════════════════════
if menu == "패턴 분석":
    st.markdown("""
<div style="background:linear-gradient(135deg,#0f766e,#0d9488);border-radius:16px;
     padding:28px 32px 22px;margin-bottom:24px;color:white;">
    <div style="font-size:1.9rem;font-weight:900;margin-bottom:6px;">📈 패턴 분석</div>
    <div style="opacity:0.85;line-height:1.6;">
        내 지식 활동의 패턴을 분석해요. 어떤 주제를 많이 저장했는지, 신뢰도 분포, 시간별 활동량을 확인해요.
    </div>
</div>
""", unsafe_allow_html=True)

    _pt_notes  = st.session_state.get("archive_notes", [])
    _pt_projs  = st.session_state.get("projects", [])
    _pt_tasks  = st.session_state.get("tasks", [])
    _pt_items  = get_all_knowledge_items()

    if not _pt_notes and not _pt_items:
        st.info("아직 저장된 데이터가 없어요. 분석 결과를 저장하면 여기서 패턴을 볼 수 있어요.")
        st.stop()

    # ═══════════ 🧠 내 사고 리포트 (통계가 아니라 인사이트) ═══════════
    from collections import Counter as _RCounter, defaultdict as _Rdd
    _r_now = datetime.now()

    def _r_days(ds):
        try:
            return (_r_now - datetime.strptime(str(ds)[:10], "%Y-%m-%d")).days
        except Exception:
            return 9999

    _links_by_note_r = _Rdd(list)
    for _l in st.session_state.get("note_concept_links", []):
        if _l.get("note_id") and _l.get("concept"):
            _links_by_note_r[_l["note_id"]].append(_l["concept"])

    def _note_cons_r(n):
        out = set()
        for c in (n.get("concepts", []) or []) + _links_by_note_r.get(n.get("id"), []):
            cc = canonical_concept(c)
            if cc:
                out.add(cc)
        return out

    def _top_topics(days, k=5):
        c = _RCounter()
        for n in _pt_notes:
            if _r_days(n.get("saved_at") or n.get("created_at")) <= days:
                for cc in _note_cons_r(n):
                    c[cc] += 1
        return c.most_common(k)

    _topics7 = _top_topics(7)
    _topics = _topics7 if _topics7 else _top_topics(30)
    _win = 7 if _topics7 else 30
    _pair = _RCounter()
    for _n in _pt_notes:
        _cs = sorted(_note_cons_r(_n))
        for _i in range(len(_cs)):
            for _j in range(_i + 1, len(_cs)):
                _pair[(_cs[_i], _cs[_j])] += 1
    _top_pairs = _pair.most_common(3)
    _recent_cc, _old_cc = set(), set()
    for _n in _pt_notes:
        _d = _r_days(_n.get("saved_at") or _n.get("created_at"))
        for _cc in _note_cons_r(_n):
            (_recent_cc if _d <= 7 else _old_cc).add(_cc)
    _emerging = sorted(_recent_cc - _old_cc)[:5]
    _low = [n for n in _pt_notes if isinstance(n.get("score"), (int, float)) and 0 < n.get("score") < 50]

    st.markdown("### 🧠 내 사고 리포트")
    st.caption("숫자가 아니라 '내가 요즘 무엇을 생각하고 있는지'를 보여줘요.")
    _rc1, _rc2 = st.columns(2)
    with _rc1:
        with st.container(border=True):
            st.markdown(f"**🔥 최근 {_win}일 핵심 주제**")
            if _topics:
                for _ti, (_t, _c) in enumerate(_topics, 1):
                    st.markdown(f"{_ti}. **{_t}** · {_c}회")
            else:
                st.caption("최근 기록이 적어요. 메모를 더 쌓아보세요.")
    with _rc2:
        with st.container(border=True):
            st.markdown("**🔗 자주 함께 등장한 개념**")
            if _top_pairs:
                for (_a, _b), _c in _top_pairs:
                    st.markdown(f"- {_a} ↔ {_b} · {_c}회")
            else:
                st.caption("아직 함께 등장한 개념이 적어요.")
    _rc3, _rc4 = st.columns(2)
    with _rc3:
        with st.container(border=True):
            st.markdown("**✨ 새롭게 떠오르는 개념** (최근 7일 신규)")
            st.markdown(" ".join(f"`{c}`" for c in _emerging) if _emerging else "_최근 신규 개념이 없어요._")
    with _rc4:
        with st.container(border=True):
            st.markdown("**⚠️ 검증이 필요한 메모**")
            if _low:
                st.markdown(f"신뢰도 50점 미만 **{len(_low)}개**")
                for _n in _low[:3]:
                    st.caption(f"· {_n.get('title', '제목 없음')} ({_n.get('score')}점)")
            else:
                st.caption("낮은 신뢰도 메모가 없어요. 👍")
    with st.container(border=True):
        st.markdown("**🚀 추천 다음 행동**")
        _recs = []
        if _top_pairs:
            (_pa, _pb), _ = _top_pairs[0]
            _recs.append(f"자주 함께 나오는 **{_pa} · {_pb}** 를 한 프로젝트나 개념 그룹으로 묶어보세요.")
        if _topics:
            _recs.append(f"핵심 주제 **{_topics[0][0]}** 관련 메모를 하나의 학습 노트로 정리해보세요.")
        if _low:
            _recs.append(f"신뢰도 낮은 메모 **{len(_low)}개**를 다시 검토해보세요.")
        if not _recs:
            _recs.append("메모를 더 쌓으면 맞춤 추천이 나와요.")
        for _r in _recs:
            st.markdown(f"- {_r}")
        render_action_buttons("report", key_prefix="rpt", title=None)
    st.divider()
    st.caption("아래는 상세 통계예요.")

    _pt_tab1, _pt_tab2, _pt_tab3, _pt_tab4 = st.tabs(["📊 전체 통계", "🏷️ 태그·개념 분포", "⏰ 시간 분석", "🤖 AI 인사이트"])

    # ─── 탭 1: 전체 통계 ─────────────────────────────────────
    with _pt_tab1:
        _pm1, _pm2, _pm3, _pm4, _pm5 = st.columns(5)
        with _pm1: st.metric("📝 저장된 메모", f"{len(_pt_notes)}개")
        with _pm2: st.metric("📁 프로젝트", f"{len(_pt_projs)}개")
        with _pm3: st.metric("✅ 작업", f"{len(_pt_tasks)}개")
        _avg_score = sum(n.get("score",0) for n in _pt_notes) / max(len(_pt_notes),1)
        with _pm4: st.metric("⭐ 평균 신뢰도", f"{_avg_score:.0f}점")
        _fav_cnt = sum(1 for n in _pt_notes if n.get("favorite"))
        with _pm5: st.metric("❤️ 즐겨찾기", f"{_fav_cnt}개")

        st.divider()

        # 콘텐츠 유형 분포
        import plotly.graph_objects as _ptgo
        from collections import Counter as _PtCnt
        _type_cnt = _PtCnt(n.get("content_type","unknown") for n in _pt_notes)
        _type_labels_map = {"news":"뉴스/기사","policy":"정책/지원","review":"후기/리뷰",
                            "research":"논문/연구","manual":"직접 작성","unknown":"기타","other":"기타"}
        _type_display = {_type_labels_map.get(k,k): v for k,v in _type_cnt.items()}

        _pt_col1, _pt_col2 = st.columns(2)
        with _pt_col1:
            st.markdown("**📂 콘텐츠 유형 분포**")
            if _type_display:
                _pie = _ptgo.Figure(_ptgo.Pie(
                    labels=list(_type_display.keys()),
                    values=list(_type_display.values()),
                    hole=0.4,
                    marker_colors=["#3b82f6","#10b981","#f59e0b","#8b5cf6","#ef4444","#64748b"],
                ))
                _pie.update_layout(height=280, margin=dict(l=10,r=10,t=10,b=10),
                    showlegend=True, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(_pie, use_container_width=True)

        with _pt_col2:
            st.markdown("**📁 프로젝트별 메모 수**")
            _proj_note_cnt = _PtCnt(n.get("project","미분류") for n in _pt_notes)
            if _proj_note_cnt:
                _bar = _ptgo.Figure(_ptgo.Bar(
                    x=list(_proj_note_cnt.keys()),
                    y=list(_proj_note_cnt.values()),
                    marker_color="#3b82f6",
                    text=list(_proj_note_cnt.values()),
                    textposition="outside",
                ))
                _bar.update_layout(height=280, margin=dict(l=10,r=10,t=10,b=30),
                    xaxis_tickangle=-20, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
                    yaxis=dict(showgrid=True, gridcolor="#e2e8f0"))
                st.plotly_chart(_bar, use_container_width=True)

        # 신뢰도 분포 히스토그램
        st.markdown("**🎯 신뢰도 점수 분포**")
        _scores = [n.get("score",0) for n in _pt_notes if n.get("score",0) > 0]
        if _scores:
            _hist = _ptgo.Figure(_ptgo.Histogram(
                x=_scores, nbinsx=10,
                marker_color="#3b82f6", opacity=0.8,
                xbins=dict(start=0, end=100, size=10),
            ))
            _hist.update_layout(height=220, margin=dict(l=10,r=10,t=10,b=10),
                xaxis_title="신뢰도 점수", yaxis_title="메모 수",
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
                bargap=0.1, xaxis=dict(range=[0,100]))
            st.plotly_chart(_hist, use_container_width=True)
            _score_low  = sum(1 for s in _scores if s < 50)
            _score_mid  = sum(1 for s in _scores if 50 <= s < 75)
            _score_high = sum(1 for s in _scores if s >= 75)
            _sc1, _sc2, _sc3 = st.columns(3)
            with _sc1: st.metric("🔴 낮음 (<50)", f"{_score_low}개")
            with _sc2: st.metric("🟡 보통 (50~74)", f"{_score_mid}개")
            with _sc3: st.metric("🟢 높음 (≥75)", f"{_score_high}개")

    # ─── 탭 2: 태그·개념 분포 ────────────────────────────────
    with _pt_tab2:
        _tag_cnt = _PtCnt()
        for _n in _pt_notes:
            for _t in _n.get("tags",[]):
                _tag_cnt[str(_t).replace("#","").strip()] += 1

        st.markdown("**🏷️ 상위 태그 20개**")
        if _tag_cnt:
            _top_tags = _tag_cnt.most_common(20)
            _tag_bar = _ptgo.Figure(_ptgo.Bar(
                x=[t[0] for t in _top_tags],
                y=[t[1] for t in _top_tags],
                marker_color=["#3b82f6" if i < 3 else "#93c5fd" for i in range(len(_top_tags))],
                text=[t[1] for t in _top_tags],
                textposition="outside",
            ))
            _tag_bar.update_layout(height=300, margin=dict(l=10,r=10,t=10,b=60),
                xaxis_tickangle=-35, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
                yaxis=dict(showgrid=True, gridcolor="#e2e8f0"))
            st.plotly_chart(_tag_bar, use_container_width=True)
            # 표로도 보기
            with st.expander("태그 전체 목록 (표)"):
                import pandas as _pd_pt
                st.dataframe(_pd_pt.DataFrame(
                    [{"태그": k, "사용 횟수": v} for k, v in _tag_cnt.most_common()]),
                    use_container_width=True, height=300)
        else:
            st.info("저장된 태그가 없어요.")

        st.divider()
        # 개념 사용 빈도
        from collections import Counter as _CCnt2
        _con_cnt = _CCnt2()
        _hidden_pt = set(st.session_state.get("hidden_concepts",[]))
        for _ki in _pt_items:
            for _tg in _ki.get("tags",[]):
                _tgc = str(_tg).replace("#","").strip()
                if _tgc and _tgc not in _hidden_pt:
                    _con_cnt[_tgc] += 1
        _custom_map_pt = {}
        for _c0 in st.session_state.get("pkm_custom_concepts",[]):
            _c0d = _c0 if isinstance(_c0,dict) else {"name":str(_c0),"folder":""}
            _n0 = _c0d.get("name","").strip()
            if _n0: _custom_map_pt[_n0] = _c0d

        st.markdown("**🧠 자주 등장하는 개념 TOP 15**")
        _top_cons = [(k,v) for k,v in _con_cnt.most_common(15) if k]
        if _top_cons:
            _con_bar = _ptgo.Figure(_ptgo.Bar(
                x=[c[0] for c in _top_cons],
                y=[c[1] for c in _top_cons],
                marker_color=["#10b981" if c[0] in _custom_map_pt else "#6ee7b7" for c in _top_cons],
                text=[c[1] for c in _top_cons],
                textposition="outside",
            ))
            _con_bar.update_layout(height=280, margin=dict(l=10,r=10,t=10,b=60),
                xaxis_tickangle=-35, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
                yaxis=dict(showgrid=True, gridcolor="#e2e8f0"))
            st.plotly_chart(_con_bar, use_container_width=True)
            st.caption("🟢 짙은 초록: 직접 등록한 개념 / 🟢 연한 초록: AI 추출 개념")
        else:
            st.info("개념이 없어요.")

    # ─── 탭 3: 시간 분석 ─────────────────────────────────────
    with _pt_tab3:
        st.markdown("**📅 월별 메모 저장량**")
        _monthly = _PtCnt()
        for _n in _pt_notes:
            _sa = _n.get("saved_at","")
            if _sa and len(_sa) >= 7:
                _monthly[_sa[:7]] += 1  # "YYYY-MM"
        if _monthly:
            _months_sorted = sorted(_monthly.keys())
            _month_bar = _ptgo.Figure(_ptgo.Bar(
                x=_months_sorted,
                y=[_monthly[m] for m in _months_sorted],
                marker_color="#3b82f6",
                text=[_monthly[m] for m in _months_sorted],
                textposition="outside",
            ))
            _month_bar.update_layout(height=280, margin=dict(l=10,r=10,t=10,b=40),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
                yaxis=dict(showgrid=True, gridcolor="#e2e8f0"))
            st.plotly_chart(_month_bar, use_container_width=True)
        else:
            st.info("날짜 정보가 있는 메모가 없어요.")

        st.divider()
        st.markdown("**📆 최근 30일 일별 활동**")
        from datetime import timedelta as _td
        _today_pt = datetime.now().date()
        _daily = {}
        for _d in range(30):
            _day = (_today_pt - _td(days=_d)).strftime("%m/%d")
            _daily[_day] = 0
        for _n in _pt_notes:
            _sa2 = _n.get("saved_at","")
            if _sa2:
                try:
                    _d2 = datetime.strptime(_sa2[:10], "%Y-%m-%d").date()
                    _diff = (_today_pt - _d2).days
                    if 0 <= _diff < 30:
                        _key2 = _d2.strftime("%m/%d")
                        if _key2 in _daily:
                            _daily[_key2] += 1
                except: pass
        _days_sorted = sorted(_daily.keys(), key=lambda x: datetime.strptime(f"2026/{x}", "%Y/%m/%d"))
        _day_bar = _ptgo.Figure(_ptgo.Bar(
            x=_days_sorted,
            y=[_daily[d] for d in _days_sorted],
            marker_color="#6366f1",
        ))
        _day_bar.update_layout(height=200, margin=dict(l=10,r=10,t=10,b=40),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#f8fafc",
            yaxis=dict(showgrid=True, gridcolor="#e2e8f0", dtick=1))
        st.plotly_chart(_day_bar, use_container_width=True)

        # 작업 상태 분포
        if _pt_tasks:
            st.divider()
            st.markdown("**✅ 작업 상태 분포**")
            _task_status_cnt = _PtCnt(t.get("status","시작전") for t in _pt_tasks)
            _status_colors = {"시작전":"#94a3b8","진행중":"#3b82f6","완료":"#10b981","보류":"#f59e0b"}
            _task_pie = _ptgo.Figure(_ptgo.Pie(
                labels=list(_task_status_cnt.keys()),
                values=list(_task_status_cnt.values()),
                hole=0.5,
                marker_colors=[_status_colors.get(k,"#64748b") for k in _task_status_cnt.keys()],
            ))
            _task_pie.update_layout(height=240, margin=dict(l=10,r=10,t=10,b=10),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(_task_pie, use_container_width=True)

    # ─── 탭 4: AI 인사이트 ───────────────────────────────────
    with _pt_tab4:
        st.markdown("#### 🤖 AI가 내 지식 활동을 분석해서 인사이트를 알려줘요.")
        st.caption("저장된 메모와 태그 패턴을 종합해서 AI가 개인화된 분석을 제공해요.")

        # 요약 데이터 준비
        _top_tags_ai = [t[0] for t in _tag_cnt.most_common(10)]
        _proj_note_cnt2 = _PtCnt(n.get("project","미분류") for n in _pt_notes)
        _top_proj_ai = [p[0] for p in _proj_note_cnt2.most_common(3)]
        _recent_titles = [n.get("title","") for n in sorted(_pt_notes, key=lambda x: x.get("saved_at",""), reverse=True)[:5]]
        _ct_dist = dict(_type_cnt.most_common())
        _score_dist_ai = f"낮음(0~49): {_score_low if _scores else 0}개, 보통(50~74): {_score_mid if _scores else 0}개, 높음(75~100): {_score_high if _scores else 0}개"

        _ai_insight_types = st.multiselect(
            "분석 항목 선택",
            ["관심 분야 요약", "지식 활동 강점", "개선이 필요한 점", "다음에 탐구할 주제", "지식 활동 전략 제안"],
            default=["관심 분야 요약", "지식 활동 강점", "다음에 탐구할 주제"],
            key="pt_ai_types"
        )

        if st.button("🤖 AI 인사이트 생성", key="pt_ai_run", type="primary", use_container_width=True):
            _sys_pt = f"""당신은 개인 지식 관리 컨설턴트입니다.
사용자의 지식 활동 데이터를 분석하고 요청한 항목별로 구체적인 인사이트를 제공해주세요.
분석 항목: {', '.join(_ai_insight_types)}
각 항목별로 3~5개의 구체적인 내용을 bullet point (- )로 써주세요. 한국어로 답변하세요."""
            _usr_pt = f"""저장된 메모: {len(_pt_notes)}개
프로젝트: {len(_pt_projs)}개 (상위: {', '.join(_top_proj_ai)})
평균 신뢰도: {_avg_score:.0f}점
신뢰도 분포: {_score_dist_ai}
자주 쓴 태그: {', '.join(_top_tags_ai[:8])}
콘텐츠 유형: {', '.join([f"{_type_labels_map.get(k,k)}({v}개)" for k,v in _ct_dist.items()])}
최근 저장 메모: {', '.join(_recent_titles)}
작업 수: {len(_pt_tasks)}개"""
            with st.spinner("AI가 분석 중... (10~20초)"):
                try:
                    _pt_ai_result = call_groq_simple(_sys_pt, _usr_pt)
                    st.session_state["pt_ai_result"] = _pt_ai_result
                except Exception as _e:
                    st.error(f"AI 오류: {_e}")

        if st.session_state.get("pt_ai_result"):
            st.divider()
            _ptair1, _ptair2 = st.columns([4,1])
            with _ptair1:
                st.markdown("#### 💡 AI 인사이트 결과")
            with _ptair2:
                if st.button("🗑️ 지우기", key="pt_ai_clear"):
                    st.session_state.pop("pt_ai_result", None)
                    st.rerun()
            st.markdown(st.session_state["pt_ai_result"])

    st.stop()


if menu == "데일리 노트":
    # ══════════════════════════════════════════════════════════
    # 📅 데일리 노트 — 날짜 기반 빠른 입력 허브 (Obsidian Daily Note 스타일)
    # ══════════════════════════════════════════════════════════
    from datetime import date as _dn_date
    st.markdown("""
<div style="background:linear-gradient(135deg,#0f766e,#0ea5e9);border-radius:16px;
     padding:24px 28px 20px;margin-bottom:18px;color:white;">
    <div style="font-size:1.8rem;font-weight:900;margin-bottom:4px;">📅 데일리 노트</div>
    <div style="opacity:0.92;line-height:1.5;">날짜를 고르고 오늘 생각난 걸 바로 적어요. 개념은 자동으로 뽑혀서 지식망에 연결돼요.</div>
</div>
""", unsafe_allow_html=True)

    # 미니 캘린더/월 이동에서 고른 날짜를 위젯 생성 '전에' 반영 (위젯 키 직접 수정 시 예외 방지)
    if "_dn_pending" in st.session_state:
        st.session_state["dn_date"] = st.session_state.pop("_dn_pending")
    # value=와 session_state를 동시에 주면 경고가 나므로, 기본값은 session_state로만 초기화
    if "dn_date" not in st.session_state:
        st.session_state["dn_date"] = _dn_date.today()
    _dn_sel = st.date_input("날짜 선택", key="dn_date")
    _dn_str = _dn_sel.strftime("%Y-%m-%d")
    _dn_projects = [p.get("name", "") for p in st.session_state.get("projects", []) if p.get("name")]

    # ── 📅 기록 캘린더 (컴팩트) — 큰 월간 달력 대신 '오늘 요약 + 최근 기록한 날' ──
    _dn_by_date = {}
    for _n in st.session_state.get("archive_notes", []):
        _nd = str(_n.get("saved_at", ""))[:10]
        if _nd:
            _dn_by_date.setdefault(_nd, []).append(_n)
    _sel_day_notes = _dn_by_date.get(_dn_str, [])
    _sel_day_concepts = sorted({c for n in _sel_day_notes for c in (n.get("concepts", []) or []) if c})
    _sel_day_tags = sorted({str(t).replace("#", "").strip() for n in _sel_day_notes
                            for t in (n.get("tags", []) or []) if str(t).strip()})
    st.markdown(
        f"**📅 {_dn_str}의 기록** — 📝 메모 {len(_sel_day_notes)} · "
        f"🧠 개념 {len(_sel_day_concepts)} · 🏷️ 태그 {len(_sel_day_tags)}")
    _recent_days = sorted(_dn_by_date.keys(), reverse=True)[:7]
    if _recent_days:
        st.caption("최근 기록한 날 — 눌러서 그날로 이동")
        _rd_cols = st.columns(len(_recent_days))
        for _i, _d in enumerate(_recent_days):
            with _rd_cols[_i]:
                _md = _d[5:].replace("-", "/")  # MM/DD
                if st.button(f"{_md} · {len(_dn_by_date[_d])}", key=f"dn_recent_{_d}",
                             use_container_width=True,
                             type=("primary" if _d == _dn_str else "secondary")):
                    _y, _m, _dd = _d.split("-")
                    st.session_state["_dn_pending"] = _dn_date(int(_y), int(_m), int(_dd))
                    st.rerun()
    else:
        st.caption("아직 기록한 날이 없어요. 아래에서 오늘 메모를 적어보세요.")

    # ── 📅 월간 캘린더 보기 (토글) — 점이 있는 날에 기록 ──
    if st.toggle("📅 월간 캘린더로 보기", key="dn_show_cal"):
        import calendar as _dn_calmod
        from datetime import timedelta
        _cy, _cm = _dn_sel.year, _dn_sel.month
        _mv1, _mv2, _mv3 = st.columns([1, 2, 1])
        with _mv1:
            if st.button("◀ 이전달", key="dn_cal_prev", use_container_width=True):
                _pm = _dn_date(_cy, _cm, 1) - timedelta(days=1)
                st.session_state["_dn_pending"] = _pm.replace(day=1)
                st.rerun()
        with _mv2:
            st.markdown(f"<div style='text-align:center;font-weight:800;'>{_cy}년 {_cm}월</div>",
                        unsafe_allow_html=True)
        with _mv3:
            _nm_first = (_dn_date(_cy, _cm, 28) + timedelta(days=7)).replace(day=1)
            if st.button("다음달 ▶", key="dn_cal_next", use_container_width=True):
                st.session_state["_dn_pending"] = _nm_first
                st.rerun()
        _hdr = st.columns(7)
        for _i, _wd in enumerate(["월", "화", "수", "목", "금", "토", "일"]):
            _hdr[_i].markdown(
                f"<div style='text-align:center;color:#94a3b8;font-size:0.8em'>{_wd}</div>",
                unsafe_allow_html=True)
        for _wk in _dn_calmod.monthcalendar(_cy, _cm):
            _wcols = st.columns(7)
            for _i, _day in enumerate(_wk):
                with _wcols[_i]:
                    if _day == 0:
                        st.markdown("&nbsp;", unsafe_allow_html=True)
                    else:
                        _ds = f"{_cy:04d}-{_cm:02d}-{_day:02d}"
                        _cnt = len(_dn_by_date.get(_ds, []))
                        _is_today = (_ds == _dn_str)
                        # 날짜 숫자(버튼) — 옵시디언처럼 숫자만
                        if st.button(str(_day), key=f"dn_cal_{_ds}", use_container_width=True,
                                     type=("primary" if _is_today else "secondary"),
                                     help=(f"메모 {_cnt}개" if _cnt else "메모 없음")):
                            st.session_state["_dn_pending"] = _dn_date(_cy, _cm, _day)
                            st.rerun()
                        # 숫자 아래 점(메모 수) — 메모 있는 날만 초록 점
                        if _cnt:
                            _dots = "●" * min(_cnt, 4) + ("⁺" if _cnt > 4 else "")
                            st.markdown(
                                f"<div style='text-align:center;color:#22c55e;"
                                f"font-size:0.55em;line-height:1;margin-top:-6px;'>{_dots}</div>",
                                unsafe_allow_html=True)
                        else:
                            st.markdown(
                                "<div style='height:8px;margin-top:-6px;'></div>",
                                unsafe_allow_html=True)
        st.caption("● = 그날 메모 수. 날짜를 누르면 그날 기록으로 이동해요.")

    _dn_ctx, _dn_left, _dn_right = st.columns([1, 1.6, 1.1])

    # ── 좌측: 최근 기억을 떠올리게 하는 컨텍스트 레일 (회상용 — 통계/관리 아님) ──
    with _dn_ctx:
        st.markdown("##### 🧭 최근 컨텍스트")
        st.caption("뭘 적을지 막힐 때, 최근 기록을 떠올려요.")

        _ctx_notes = sorted(st.session_state.get("archive_notes", []),
                            key=lambda n: str(n.get("saved_at", "")), reverse=True)[:5]
        st.markdown("**📝 최근 메모**")
        if _ctx_notes:
            for _cn in _ctx_notes:
                if st.button(f"· {(_cn.get('title') or '제목 없음')[:18]}",
                             key=f"dn_ctx_note_{_cn.get('id')}", use_container_width=True):
                    st.session_state["archive_open_note_id"] = _cn.get("id")
                    st.query_params["page"] = "archive"
                    st.rerun()
        else:
            st.caption("아직 없어요.")

        _ctx_concepts, _seen_cc = [], set()
        for _lk in sorted(st.session_state.get("note_concept_links", []),
                          key=lambda l: str(l.get("linked_at", "")), reverse=True):
            _cc = canonical_concept(_lk.get("concept"))
            if _cc and _cc not in _seen_cc:
                _seen_cc.add(_cc); _ctx_concepts.append(_cc)
            if len(_ctx_concepts) >= 8:
                break
        st.markdown("**🧠 최근 개념**")
        st.markdown(" ".join(f"`{c}`" for c in _ctx_concepts) if _ctx_concepts else "_아직 없어요._")

        _ctx_projs = sorted(st.session_state.get("projects", []),
                            key=lambda p: str(p.get("updated_at") or p.get("created_at") or ""),
                            reverse=True)[:5]
        st.markdown("**📁 최근 프로젝트**")
        st.markdown("\n".join(f"- {p.get('name', '')}" for p in _ctx_projs) if _ctx_projs else "_아직 없어요._")

        _ctx_tasks = sorted(st.session_state.get("tasks", []),
                            key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""),
                            reverse=True)[:5]
        st.markdown("**✅ 최근 작업**")
        st.markdown("\n".join(f"- {t.get('title', '')}" for t in _ctx_tasks) if _ctx_tasks else "_아직 없어요._")

    # ── 가운데: 입력 ──
    with _dn_left:
        st.markdown(f"#### ✍️ {_dn_str} 메모 쓰기")
        _dn_title = st.text_input("제목", value=f"{_dn_str} 데일리 노트", key="dn_title")
        _dn_did = st.text_area("📌 오늘 한 일", key="dn_did", height=80,
                               placeholder="오늘 한 일/공부한 것")
        _dn_learned = st.text_area("💡 배운 것", key="dn_learned", height=80,
                                   placeholder="새로 알게 된 것")
        _dn_think = st.text_area("🧠 생각 / 아이디어", key="dn_think", height=80,
                                 placeholder="떠오른 생각·아이디어")
        _dnc1, _dnc2 = st.columns(2)
        with _dnc1:
            _dn_proj = st.selectbox("관련 프로젝트", ["(없음)"] + _dn_projects, key="dn_proj")
        with _dnc2:
            _dn_extra_tags = st.text_input("태그 추가 (쉼표)", key="dn_tags", placeholder="예: 회고, TIL")

        if st.button("💾 데일리 노트 저장", type="primary", use_container_width=True, key="dn_save"):
            _parts = []
            if _dn_did.strip():
                _parts.append(f"## 📌 오늘 한 일\n{_dn_did.strip()}")
            if _dn_learned.strip():
                _parts.append(f"## 💡 배운 것\n{_dn_learned.strip()}")
            if _dn_think.strip():
                _parts.append(f"## 🧠 생각 / 아이디어\n{_dn_think.strip()}")
            _dn_body = "\n\n".join(_parts)
            if not _dn_body.strip():
                st.warning("내용을 한 가지 이상 입력해주세요.")
            else:
                _dn_tags = ["데일리노트", _dn_str] + [t.strip() for t in _dn_extra_tags.split(",") if t.strip()]
                _dn_concepts = extract_local_concepts(_dn_body, _dn_tags, limit=8)
                _dn_proj_val = _dn_proj if _dn_proj != "(없음)" else "기본 프로젝트"
                _m = create_memo(_dn_title.strip() or f"{_dn_str} 데일리 노트",
                                 note=_dn_body, project=_dn_proj_val, section="데일리노트",
                                 tags=_dn_tags, concepts=_dn_concepts)
                # 선택 날짜로 saved_at 고정 + 데일리 노트 메타
                _m["saved_at"] = f"{_dn_str} {datetime.now().strftime('%H:%M')}"
                _m["note_type"] = "daily_note"
                _first = next((l.strip() for l in _dn_body.splitlines()
                               if l.strip() and not l.strip().startswith("#")), "")
                _m["one_line_summary"] = _first[:120]
                save_persisted_data()
                for _k in ("dn_did", "dn_learned", "dn_think", "dn_tags"):
                    st.session_state.pop(_k, None)
                _flash(f"{_dn_str} 데일리 노트를 저장했어요! 개념 {len(_m.get('concepts', []))}개 자동 연결.")
                st.rerun()

    # ── 우측: 이 날짜 메모 + 최근 개념 ──
    with _dn_right:
        st.markdown(f"#### 📌 {_dn_str}의 메모")
        _dn_notes = [n for n in st.session_state.get("archive_notes", [])
                     if str(n.get("saved_at", ""))[:10] == _dn_str]
        if not _dn_notes:
            st.caption("아직 이 날짜의 메모가 없어요. 왼쪽에서 첫 메모를 적어보세요.")
        else:
            for _n in _dn_notes:
                with st.container(border=True):
                    _icon = "📅" if _n.get("note_type") == "daily_note" else "📝"
                    st.markdown(f"{_icon} **{_n.get('title', '제목 없음')}**")
                    _ol = (_n.get("one_line_summary") or "").strip()
                    if _ol:
                        st.caption(_ol)
                    _ncs = [c for c in (_n.get("concepts", []) or []) if c]
                    if _ncs:
                        st.markdown(" ".join(f"`{c}`" for c in _ncs[:5]))
                    if st.button("📖 열기", key=f"dn_open_{_n.get('id')}", use_container_width=True):
                        st.session_state["archive_open_note_id"] = _n.get("id")
                        st.query_params["page"] = "archive"
                        st.rerun()
    st.stop()


if menu == "설정":
    # ══════════════════════════════════════════════════════════
    # ⚙️ TrustLens Control Center (설정 통합 허브)
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="background:linear-gradient(135deg,#0f172a,#1e3a8a 60%,#3b82f6);
     border-radius:16px;padding:26px 30px 22px;margin-bottom:20px;color:white;">
    <div style="font-size:1.9rem;font-weight:900;margin-bottom:4px;">⚙️ Control Center</div>
    <div style="opacity:0.9;line-height:1.6;">
        TrustLens의 모든 설정을 한 곳에서. 화면·세계관·루미·Second Brain 기능·알림·실험실을 켜고 끌 수 있어요.
    </div>
</div>
""", unsafe_allow_html=True)

    _settings = st.session_state.setdefault("app_settings", dict(APP_SETTINGS_DEFAULTS))

    def _set(key, val):
        _settings[key] = val

    def _seg(label, key, options, help=None):
        _cur = get_setting(key)
        _idx = options.index(_cur) if _cur in options else 0
        _v = st.radio(label, options, index=_idx, horizontal=True, key=f"set_{key}", help=help)
        _set(key, _v)

    def _tog(label, key, help=None):
        _v = st.toggle(label, value=bool(get_setting(key)), key=f"set_{key}", help=help)
        _set(key, _v)

    _scr, _world, _lumi, _sb, _notif, _lab = st.tabs(
        ["🎨 화면", "🌌 세계관", "🤖 루미", "🧠 Second Brain", "🔔 알림", "📊 실험실"])

    with _scr:
        st.markdown("#### 🧪 고급 모드")
        st.caption("끄면 사이드바가 **내 세계 · 기록하기 · 루미 · 설정** 4가지로 단순해져요. "
                   "켜면 개념·태그·관계·분석 같은 전문가 메뉴가 다시 나타나요. (기능은 사라지지 않아요)")
        _prev_adv = bool(get_setting("show_advanced"))
        _tog("🧪 고급 모드 켜기 (전문가 메뉴 표시)", "show_advanced",
             help="개념 라이브러리·태그 관리·엔터티 상세·신뢰도 근거·분석 아카이브·최근 검색 기록")
        if bool(_settings.get("show_advanced")) != _prev_adv:
            st.session_state["app_settings"] = _settings
            save_persisted_data()
            st.rerun()
        st.divider()
        st.markdown("#### 🎨 화면")
        st.caption("🔜 카드 밀도·글자 크기·애니메이션은 지금은 **저장만** 돼요(곧 화면에 반영).")
        _seg("카드 밀도 🔜", "ui_density", ["여유", "보통", "촘촘"])
        _seg("글자 크기 🔜", "ui_font_scale", ["작게", "보통", "크게"])
        _tog("✨ 애니메이션 🔜", "ui_animations", help="성장 연출·전환 애니메이션 (곧 적용)")
        st.caption("라이트/다크 등 색 테마는 우측 상단 ⋮ → Settings(Streamlit) 또는 .streamlit/config.toml에서 바꿔요.")

    with _world:
        st.markdown("#### 🌌 세계관")
        st.caption("✅ 선택 즉시 홈 대시보드 용어에 반영돼요.")
        _theme_keys = list(_BRAIN_THEMES.keys())
        _theme_names = [_BRAIN_THEMES[k]["name"] for k in _theme_keys]
        _cur_theme = st.session_state.get("brain_theme", "default")
        _ti = _theme_keys.index(_cur_theme) if _cur_theme in _theme_keys else 0
        _sel_theme_name = st.radio("세계관 선택", _theme_names, index=_ti, key="set_world_theme")
        _sel_theme = _theme_keys[_theme_names.index(_sel_theme_name)]
        if _sel_theme != _cur_theme:
            st.session_state["brain_theme"] = _sel_theme
            save_persisted_data()
            st.rerun()
        _cfg = get_brain_theme_config(_sel_theme)
        st.caption("미리보기 — 같은 데이터, 다른 세계관 용어:")
        st.markdown(" · ".join(f"{_e} {_n}" for _e, _n in _cfg["elements"]))

    with _lumi:
        st.markdown("#### 🤖 루미")
        st.caption("🔜 페르소나·안내 수준은 **저장만** 돼요(루미 말투 반영은 곧).")
        _seg("말투(페르소나) 🔜", "lumi_persona", ["친구형", "분석가형", "코치형", "철학자형"],
             help="철학자형은 향후 철학 프로파일 리포트(v4.x)와 연결돼요.")
        _seg("안내 수준 🔜", "lumi_guide_level", ["적게", "보통", "자세히"])
        st.caption("루미 아바타는 홈 대시보드 우측 패널에서도 바꿀 수 있어요.")

    with _sb:
        st.markdown("#### 🧠 Second Brain 기능")
        st.caption("✅ 표시는 즉시 반영, 🔜 표시는 곧 적용돼요. (저장된 데이터는 항상 그대로)")
        _tog("자동 개념 추출 🔜", "feat_auto_concepts")
        _tog("개념 품질 게이트 🔜", "feat_quality_gate")
        _tog("⭐ TF-IDF 중요 개념 강조 ✅", "feat_tfidf",
             help="끄면 개념 허브 TF-IDF 섹션·프로젝트 맵 중요도 토글이 숨겨져요.")
        _tog("🪢 관련 메모 추천 ✅", "feat_related_notes",
             help="끄면 노트 상세의 관련 메모 추천이 숨겨져요.")

    with _notif:
        st.markdown("#### 🔔 알림")
        st.caption("🔜 알림 항목은 지금은 **저장만** 돼요(곧 적용).")
        _tog("마감일 알림 🔜", "notif_due")
        _tog("작업 완료 토스트 🔜", "notif_task_done")
        _tog("프로젝트 요약 표시 🔜", "notif_project_summary")
        _tog("✨ 토스트 메시지 🔜", "ui_toasts")

    with _lab:
        st.markdown("#### 📊 실험실 (베타)")
        st.caption("실험 중인 기능이에요. 켜면 사용할 수 있고, 안정화되면 정식 기능으로 승격돼요.")
        _tog("🗺️ 프로젝트 맵", "beta_project_map")
        _tog("⭐ TF-IDF 강조", "beta_tfidf")
        _tog("🔗 별칭 시스템", "beta_alias")
        _tog("🧬 의미 병합 (예정)", "beta_semantic_merge")
        _tog("🧠 철학 프로파일 (예정)", "beta_philosophy")

    st.divider()
    _sc1, _sc2 = st.columns([1, 1])
    with _sc1:
        if st.button("💾 설정 저장", type="primary", use_container_width=True):
            st.session_state["app_settings"] = _settings
            save_persisted_data()
            _flash("설정을 저장했어요.")
            st.rerun()
    with _sc2:
        if st.button("↩️ 기본값으로 복원", use_container_width=True):
            st.session_state["app_settings"] = dict(APP_SETTINGS_DEFAULTS)
            save_persisted_data()
            _flash("설정을 기본값으로 되돌렸어요.")
            st.rerun()
    st.caption("✅ = 지금 바로 반영 · 🔜 = 저장만 되고 곧 적용 예정. 변경 후 **설정 저장**을 눌러야 다음 실행에도 유지돼요. (세계관은 즉시 적용)")

    # ══════════════════════════════════════════════════════════
    # 🛠 개발자 진단 (상설) — 데이터 저장/로드 상태를 한눈에. 캡처해서 공유용.
    # ══════════════════════════════════════════════════════════
    st.divider()
    with st.expander("🛠 개발자 진단 (저장·로드 상태)"):
        st.caption("데이터가 안 보이거나 사라질 때, 이 내용을 캡처해서 개발자에게 주세요.")
        from collections import Counter as _DevCnt
        # 1) Supabase 연결
        _dev_connected = bool(_sb_client())
        # 2) Supabase 실데이터 (직접 조회)
        _dev_sb = _sb_load() if _dev_connected else None
        _dev_sb_ok = isinstance(_dev_sb, dict)
        _dev_sb_notes = (_dev_sb or {}).get("archive_notes", []) if _dev_sb_ok else []
        _dev_sb_dates = _DevCnt(str(n.get("saved_at", ""))[:10] for n in _dev_sb_notes)
        # 3) 현재 세션(메모리)
        _dev_ss_notes = st.session_state.get("archive_notes", [])
        _dev_ss_dates = _DevCnt(str(n.get("saved_at", ""))[:10] for n in _dev_ss_notes)
        # 4) note_type 분포 (수동/데일리/분석 구분 확인용)
        _dev_types = _DevCnt(str(n.get("note_type") or n.get("content_type") or "unknown")
                             for n in _dev_ss_notes)
        # 5) 패키지/시크릿
        try:
            import supabase as _devpkg
            _dev_pkg = getattr(_devpkg, "__version__", "unknown")
        except Exception as _e:
            _dev_pkg = f"import 실패: {_e}"
        try:
            _dev_secret_keys = list(st.secrets.keys())
        except Exception:
            _dev_secret_keys = []

        # 6) 엔티티별 개수 (세션 vs 클라우드)
        _dev_entity_keys = ("archive_notes", "tasks", "projects", "pkm_custom_concepts",
                            "note_concept_links", "relations", "saved_analyses")
        _dev_counts = {}
        for _ek in _dev_entity_keys:
            _ss_n = len(st.session_state.get(_ek, []) or [])
            _sb_n = len((_dev_sb or {}).get(_ek, []) or []) if _dev_sb_ok else "-"
            _dev_counts[_ek] = {"session": _ss_n, "supabase": _sb_n}
        # 7) 환경/버전
        import sys as _devsys
        _dev_env = {
            "app_build": APP_BUILD,
            "python": _devsys.version.split()[0],
            "streamlit": st.__version__,
            "supabase_pkg": _dev_pkg,
            "data_file_exists": DATA_FILE.exists(),
        }

        st.markdown("##### 1) 핵심 상태")
        st.write({
            "supabase_connected": _dev_connected,
            "supabase_load_ok": _dev_sb_ok,
            "persist_source": st.session_state.get("_persist_source"),
            "persist_blocked": st.session_state.get("_persist_blocked"),
            "secrets_keys": _dev_secret_keys,
            "last_sb_stage": _SB_DEBUG.get("stage"),
            "last_sb_error": _SB_DEBUG.get("error"),
        })
        st.markdown("##### 2) 메모 날짜별 개수 (저장 vs 화면)")
        st.write({
            "supabase_dates": dict(sorted(_dev_sb_dates.items())),
            "session_dates": dict(sorted(_dev_ss_dates.items())),
            "session_note_types": dict(_dev_types),
        })
        st.markdown("##### 3) 엔티티별 개수 (session ↔ supabase)")
        st.write(_dev_counts)
        st.markdown("##### 4) 환경·버전")
        st.write(_dev_env)

        st.markdown("##### 📋 전체 로그 (복사용)")
        st.caption("아래 박스 오른쪽 위 복사 아이콘을 눌러 통째로 복사해서 주세요.")
        _dev_report = {
            "app_build": APP_BUILD,
            "supabase_connected": _dev_connected,
            "supabase_load_ok": _dev_sb_ok,
            "persist_source": st.session_state.get("_persist_source"),
            "persist_blocked": st.session_state.get("_persist_blocked"),
            "secrets_keys": _dev_secret_keys,
            "last_sb_stage": _SB_DEBUG.get("stage"),
            "last_sb_error": _SB_DEBUG.get("error"),
            "supabase_dates": dict(sorted(_dev_sb_dates.items())),
            "session_dates": dict(sorted(_dev_ss_dates.items())),
            "session_note_types": dict(_dev_types),
            "entity_counts": _dev_counts,
            "env": _dev_env,
        }
        st.code(json.dumps(_dev_report, ensure_ascii=False, indent=2), language="json")

        st.markdown("##### 5) 빠른 링크")
        st.markdown(
            "- 🗄 [Supabase 테이블 에디터]"
            "(https://supabase.com/dashboard/project/fxjmipuajllwejypmvmk/editor)\n"
            "- 🔑 [Supabase API 키 설정]"
            "(https://supabase.com/dashboard/project/fxjmipuajllwejypmvmk/settings/api-keys)\n"
            "- 🐙 [GitHub 저장소](https://github.com/Elysia0215/trustlens)\n"
            "- ☁️ [Streamlit Cloud 앱 관리](https://share.streamlit.io/)"
        )

        st.markdown("##### 6) 점검·복구 액션")
        _dc1, _dc2 = st.columns(2)
        with _dc1:
            if st.button("🔄 Supabase 연결 캐시 비우기", key="dev_sb_clear", use_container_width=True):
                _sb_client.clear()
                st.rerun()
            if st.button("🔁 Supabase 읽기/쓰기 왕복 테스트", key="dev_roundtrip", use_container_width=True):
                _c = _sb_client()
                if not _c:
                    st.error("연결 안 됨 — 캐시 비우기 먼저.")
                else:
                    try:
                        import time as _t
                        _stamp = datetime.now().isoformat()
                        _c.table("jium_store").upsert(
                            {"id": "_healthcheck", "data": {"ping": _stamp}}).execute()
                        _r = _c.table("jium_store").select("data").eq(
                            "id", "_healthcheck").limit(1).execute()
                        _got = (_r.data or [{}])[0].get("data", {}).get("ping")
                        if _got == _stamp:
                            st.success(f"왕복 OK ✅ (write+read 정상) — {_stamp}")
                        else:
                            st.warning(f"읽은 값 불일치: {_got}")
                    except Exception as _e:
                        st.error(f"왕복 실패: {type(_e).__name__}: {_e}")
        with _dc2:
            if st.button("💾 지금 세션을 Supabase에 강제 저장", key="dev_force_save",
                         use_container_width=True, type="primary"):
                _ok = _sb_save(collect_persisted_data())
                if _ok:
                    st.success("강제 저장 완료. 위 supabase_dates를 다시 확인하세요.")
                else:
                    st.error(f"저장 실패: {_SB_DEBUG.get('error')}")
            if st.button("⬇️ 클라우드(Supabase)를 화면으로 다시 불러오기", key="dev_reload",
                         use_container_width=True):
                _fresh = _sb_load()
                if isinstance(_fresh, dict):
                    for _k, _v in normalize_persisted_data(_fresh).items():
                        st.session_state[_k] = _v
                    _flash("클라우드 데이터로 화면을 새로 채웠어요.")
                    st.rerun()
                else:
                    st.error("클라우드에서 데이터를 못 읽었어요.")
        st.caption(
            "해석: **session_dates엔 있는데 supabase_dates엔 없으면** → 저장이 클라우드까지 "
            "안 간 것(저장 버그). **둘 다 있는데 화면 목록에서 안 보이면** → 표시(필터) 문제. "
            "왕복 테스트가 실패하면 → 연결/키/RLS 문제."
        )
    st.stop()


if menu == "가이드북":
    # ══════════════════════════════════════════════════════════
    # 📘 TrustLens 가이드북
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="
    background: linear-gradient(135deg, #1e3a8a 0%, #1d4ed8 50%, #3b82f6 100%);
    border-radius: 16px;
    padding: 28px 32px 24px;
    margin-bottom: 24px;
    color: white;
">
    <div style="font-size:2rem; font-weight:900; letter-spacing:-1px; margin-bottom:6px;">
        📘 TrustLens 가이드북
    </div>
    <div style="font-size:1rem; opacity:0.9; line-height:1.6;">
        매일 떠오른 생각을 기록하면, JIUM이 그것들을 <strong>연결하고 확장</strong>해<br>
        하나의 <strong>내 지식 세계</strong>로 만들어가요.
    </div>
</div>
""", unsafe_allow_html=True)

    # ── 1) 왜 쓰는가 — 기능보다 먼저 '왜'를 (감성 카피) ──
    st.markdown(
        "<div style='background:linear-gradient(135deg,#f0f9ff,#faf5ff);border:1px solid #bae6fd;"
        "border-radius:14px;padding:22px 26px;margin-bottom:20px;line-height:1.8;color:#0c4a6e;'>"
        "<div style='font-size:1.15rem;font-weight:900;margin-bottom:8px;'>🌍 내 지식 세계</div>"
        "우리는 매일 많은 생각을 하지만, 대부분은 흩어지고 사라져요.<br>"
        "<b>JIUM</b>은 그 생각을 <b>기록하고 · 연결하고 · 확장해서</b><br>"
        "나만의 <b>지식 세계</b>를 만들어가는 공간이에요.<br>"
        "<span style='color:#475569;'>오늘 ‘한 줄’부터 시작해보세요. 쌓일수록 세계가 넓어져요.</span>"
        "</div>",
        unsafe_allow_html=True)

    # ── 2) 3분 시작하기 — 용어보다 먼저 '경험' ──
    st.markdown("#### 🚀 3분만에 시작하기")
    _quickstart = [
        ("1️⃣", "오늘 떠오른 생각을 적는다", "✍️ 홈 맨 위 ‘오늘 한 줄’이나 📅 데일리 노트에 그냥 적으면 끝이에요."),
        ("2️⃣", "JIUM이 개념·태그를 연결해준다", "적은 내용에서 핵심 개념·태그가 자동으로 뽑혀 비슷한 기록끼리 이어져요."),
        ("3️⃣", "쌓인 기록이 프로젝트·관계로 이어진다", "관련 기록을 하나의 주제(프로젝트)로 묶고, 기록 사이를 관계로 연결해요."),
        ("4️⃣", "어느 순간 내 지식 세계가 만들어진다", "🪐 지식 우주에서 내 세계가 커지고 연결되는 걸 눈으로 보게 돼요."),
    ]
    for _qi, _qt, _qd in _quickstart:
        st.markdown(
            f"<div style='display:flex;align-items:flex-start;gap:12px;margin-bottom:8px;'>"
            f"<div style='font-size:1.3rem;min-width:30px;'>{_qi}</div>"
            f"<div><b style='color:#1e293b'>{_qt}</b>"
            f"<div style='color:#64748b;font-size:0.9em;margin-top:1px'>{_qd}</div></div></div>",
            unsafe_allow_html=True)
    st.divider()

    # ── 3) 핵심 용어 — 경험을 설명한 뒤에 용어 ──
    st.markdown("#### 📖 핵심 용어 5가지")
    _terms = [
        ("📝", "메모", "가장 작은 생각 기록 한 조각."),
        ("📚", "연구노트", "여러 메모를 묶어 정리한 노트."),
        ("🧠", "개념", "여러 기록에서 반복되는 핵심 아이디어."),
        ("🔗", "관계", "기록과 기록 사이를 잇는 연결선."),
        ("🪐", "프로젝트", "하나의 주제를 담은 ‘행성’."),
    ]
    _tc1, _tc2 = st.columns(2)
    for _ti, (_tem, _tnm, _tds) in enumerate(_terms):
        with (_tc1 if _ti % 2 == 0 else _tc2):
            st.markdown(
                f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;"
                f"padding:10px 14px;margin-bottom:8px;'>"
                f"<b>{_tem} {_tnm}</b><br><span style='color:#64748b;font-size:0.9em'>{_tds}</span></div>",
                unsafe_allow_html=True)
    st.caption(
        "🪐 한 줄 정리: JIUM은 ‘프로젝트 > 연구노트 > 메모’ 구조 위에 "
        "‘개념·태그·관계’ 연결망을 덧씌운 지식 세계예요."
    )
    st.divider()

    # ── 4) 기능 설명 (아래 탭) ──
    st.markdown("#### 🧭 기능 자세히 보기")

    _g0, _g1, _g6, _g2, _g3, _g4, _g5 = st.tabs([
        "🗺 사용 흐름·활용 레벨",
        "🚀 TrustLens란",
        "🏠 홈·검색·AI",
        "🔍 분석·저장하기",
        "📁 프로젝트·작업",
        "🧠 지식맵·개념",
        "🔗 데이터 관리",
    ])

    # ─── 탭 0: 사용 흐름 · 활용 레벨 (처음 온 사람을 위한 길잡이) ───
    with _g0:
        st.markdown("### 🗺 추천 사용 흐름")
        st.markdown(
            "<div style='background:#eff6ff;border-radius:12px;padding:14px 18px;font-size:1.02em;line-height:2;'>"
            "✍️ <b>입력</b> &nbsp;→&nbsp; 📅 <b>Daily Note</b> &nbsp;→&nbsp; 📚 <b>지식 아카이브</b> &nbsp;→&nbsp; "
            "🧠 <b>개념</b> &nbsp;→&nbsp; 🕸 <b>관계</b> &nbsp;→&nbsp; 📁 <b>프로젝트</b> &nbsp;→&nbsp; "
            "🤖 <b>AI</b> &nbsp;→&nbsp; 📈 <b>성장</b>"
            "</div>", unsafe_allow_html=True)
        st.caption("생각을 적고 → 다시 떠올리고 → 연결하고 → 탐색하고 → AI로 확장하는 흐름이에요.")
        st.divider()

        st.markdown("### 🌱 초급 — 일단 하루 한 줄부터")
        _lv1, _lv2, _lv3 = st.columns(3)
        with _lv1:
            st.markdown("**🌱 처음 시작하기 (5분)**")
            st.markdown("1. 홈에서 **오늘 한 줄** 작성\n2. 📅 Daily Note에서 생각 확장\n3. 자동 생성된 개념 확인\n4. 저장")
            st.caption("🎯 목표: 오늘 생각 하나 남기기")
        with _lv2:
            st.markdown("**🌿 매일 쓰기**")
            st.markdown("홈 한 줄 → Daily Note → 회상 레일 참고 → 미니 캘린더에서 다시 보기")
            st.caption("🎯 목표: 생각을 꾸준히 쌓기")
        with _lv3:
            st.markdown("**🌳 Second Brain 활용**")
            st.markdown("Daily Note → 지식 아카이브 → 관련 메모 추천 → 개념 연결 → 프로젝트 연결")
            st.caption("🎯 목표: 생각을 지식으로 연결하기")
        st.divider()

        st.markdown("### 🚀 중급 — 사용 유형별 활용법")
        _md1, _md2, _md3 = st.columns(3)
        with _md1:
            st.markdown("**📁 프로젝트형**")
            st.markdown("메모 → 개념 → 작업 → 프로젝트 → 프로젝트 맵")
            st.caption("추천 메뉴: 📁 프로젝트 · 🗺 프로젝트 맵 · 📅 캘린더")
        with _md2:
            st.markdown("**📖 공부형**")
            st.markdown("Daily Note → 개념 → TF-IDF → 관련 메모 → 지식 AI")
            st.caption("추천 메뉴: 🧠 개념 · 📈 성장 · 🤖 지식 AI")
        with _md3:
            st.markdown("**🔬 연구형**")
            st.markdown("노트 → 브레인스토밍 → 연구노트 → 프로젝트")
            st.caption("추천 메뉴: 🤖 브레인스토밍 · 🧠 지식 페이지 · 🗺 프로젝트 맵")
        st.divider()

        st.markdown("### 🧙 고급 — 지식을 다듬고 분석하기")
        st.caption("📍위치 · 🎯언제 · 🔗관련 · 🚶사용 흐름 으로 '언제 어떻게 쓰는지'까지 안내해요.")
        _hi1, _hi2, _hi3 = st.columns(3)
        with _hi1:
            with st.container(border=True):
                st.markdown("**🧹 개념 품질 관리**")
                st.markdown("📍 **위치**: 지식 맵 → 🧠 개념 (⚙️ 개념 관리 도구) · 관리 → 데이터 관리 → 🧠 개념 병합")
                st.markdown("🎯 **언제**: 개념이 중복되거나 추천 품질을 높이고 싶을 때")
                st.markdown("🔗 **관련**: 품질 게이트 · 별칭 · 병합 · TF-IDF")
                st.markdown("🚶 **흐름**: 개념 확인 → 별칭 묶기 → 병합 → TF-IDF로 핵심 개념 부각")
        with _hi2:
            with st.container(border=True):
                st.markdown("**🕸 지식 구조 분석**")
                st.markdown("📍 **위치**: 지식 맵 → 🕸 관계 / 🗺 프로젝트맵 / 🕰 타임라인 (고급 보기)")
                st.markdown("🎯 **언제**: 지식이 어떻게 연결돼 있는지 보고 싶을 때")
                st.markdown("🔗 **관련**: 관계형 지식맵 · 프로젝트맵 · 타임라인")
                st.markdown("🚶 **흐름**: 관계맵에서 연결 보기 → 프로젝트맵으로 좁히기 → 타임라인으로 변화 보기")
        with _hi3:
            with st.container(border=True):
                st.markdown("**🤖 AI 활용**")
                st.markdown("📍 **위치**: AI → 🧠 지식 AI / 💡 브레인스토밍 / 📈 패턴 분석")
                st.markdown("🎯 **언제**: 쌓인 지식을 근거로 답을 얻거나 아이디어를 확장할 때")
                st.markdown("🔗 **관련**: 지식 AI · 브레인스토밍 · 패턴 분석")
                st.markdown("🚶 **흐름**: 지식 AI에 질문 → 브레인스토밍으로 확장 → 패턴 분석으로 회고")
        st.caption("🚶 **전체 흐름 예시**: ✍️ 오늘 한 줄 → 📅 데일리 노트 → 🧠 개념 생성 → 🕸 관계맵 확인 → 🤖 AI 확장")
        st.divider()

        st.markdown("### 🙋 나는 어떤 사용자? — 유형별 시나리오")
        st.caption("정답 사용법은 없어요. 자기 방식에 가까운 흐름을 골라 시작해보세요.")
        _personas = [
            ("🎓 학생", "강의·공부 정리", "📅 Daily Note → 🧠 개념 → 🤖 지식 AI → 시험 정리"),
            ("💼 취준생", "채용·면접 준비", "채용공고 메모 → 🧠 개념 → 📁 프로젝트 → 면접 준비"),
            ("🚀 창업가", "아이디어 발전", "아이디어 → 🤖 브레인스토밍 → 📁 프로젝트 → 🕸 관계맵"),
            ("🔬 연구자", "논문·자료 정리", "노트 → 🧠 개념 → ⭐ TF-IDF → 🔬 연구노트"),
            ("🧘 철학형", "생각·사고 기록", "📅 Daily Note → 생각 → 🧠 개념 → 🕸 관계 → 사고 패턴 → 🧠 철학 프로파일(예정)"),
        ]
        for _pi in range(0, len(_personas), 2):
            _pcols = st.columns(2)
            for _pj, (_pname, _pdesc, _pflow) in enumerate(_personas[_pi:_pi + 2]):
                with _pcols[_pj]:
                    with st.container(border=True):
                        st.markdown(f"**{_pname}** · <span style='color:#94a3b8'>{_pdesc}</span>",
                                    unsafe_allow_html=True)
                        st.markdown(f"<span style='color:#475569;font-size:0.9em'>{_pflow}</span>",
                                    unsafe_allow_html=True)
        st.info("👉 처음이라면 유형과 상관없이 **‘홈에서 오늘 한 줄’**부터. 쌓이면 자연스럽게 자기 흐름이 생겨요.")

    # ─── 탭 1: TrustLens란 ───────────────────────────────────
    with _g1:
        st.markdown("### 💡 TrustLens는 무엇인가요?")
        st.markdown("""
TrustLens는 **AI 기반 개인 지식·프로젝트 운영체제(AI Knowledge OS)** 예요.

단순한 신뢰도 분석기가 아니라, 정보를 **메모로 만들고 → 프로젝트에 연결하고 → 개념·지식맵으로 정리하고 → 검색·AI로 다시 꺼내 쓰는** 전체 지식 사이클을 제공해요.

> *"AI가 대신 생각하지 않는다. 더 나은 판단을 돕는다."*

**🧭 메뉴는 이렇게 구성돼 있어요 (사이드바):**
| 그룹 | 메뉴 | 역할 |
|------|------|------|
| 🏠 홈 | 홈 대시보드(✍️ 오늘 한 줄) / 📅 데일리 노트 / 통합 검색 / 새 메모·엔터티 | 시작점·입력·찾기 |
| 📝 지식 | 지식 아카이브 / 지식 맵 / 태그 관리 | **본체** — 메모·개념·시각화 |
| 📁 프로젝트 | 프로젝트 / 작업 관리 | 연구·업무 단위 |
| 🤖 AI | 지식 AI / AI 브레인스토밍 / 패턴 분석 | 내 지식 기반 AI |
| 📊 분석 | 분석 결과 / 신뢰도 근거 / 분석결과 아카이브 | **입구** — 신뢰도 분석 |
| ⚙️ 관리 | 엔터티 상세 / 데이터 관리 / 최근 검색 기록 | 데이터·관계 정비 |
| ⚙️ (하단 고정) | 설정 | Control Center — 화면·세계관·루미·기능·알림·실험실 |
| 📘 (하단 고정) | 가이드북 | 지금 이 페이지 |
| 🆕 (하단 고정) | 패치 노트 | 버전별 업데이트·수정 내역 + 포트폴리오 |

> 💡 **분석은 입구, 지식관리가 본체예요.** 정보를 한 번 검사하고 끝내는 게 아니라, 메모로 쌓고 연결해서 계속 다시 꺼내 쓰는 게 핵심이에요.
""")
        st.divider()
        st.markdown("### 🗺️ 기본 사용 흐름")
        _flow_steps = [
            ("1️⃣", "정보 수집 (입구)", "📊 분석에서 URL/텍스트 입력 → AI 신뢰도·편향·광고·작성자 분석", "#3b82f6"),
            ("2️⃣", "지식 메모 저장", "AI 요약 + 내 생각 합쳐 메모로 저장. 태그·개념 자동 추출", "#6366f1"),
            ("3️⃣", "프로젝트·작업 연결", "메모를 프로젝트/섹션/작업에 연결해 연구 흐름 만들기", "#8b5cf6"),
            ("4️⃣", "지식맵·개념 정리", "개념 간 연결, 마인드맵, ERD로 전체 지식 구조 한눈에", "#10b981"),
            ("5️⃣", "통합 검색으로 다시 찾기", "🔍 통합 검색에서 메모·작업·개념·연구노트 한 번에 탐색", "#f59e0b"),
            ("6️⃣", "AI로 확장하기", "🧠 지식 AI가 내 지식 근거로 답하고, 🤖 브레인스토밍이 다음 행동 제안", "#ef4444"),
        ]
        for _fi, _fc in enumerate(_flow_steps):
            _ficon, _ftitle, _fdesc, _fcolor = _fc
            st.markdown(f"""<div style="display:flex; align-items:flex-start; gap:14px; margin-bottom:12px;
     background:#f8fafc; border-left:4px solid {_fcolor}; border-radius:8px; padding:12px 16px;">
    <div style="font-size:1.4rem; min-width:32px;">{_ficon}</div>
    <div>
        <div style="font-weight:700; color:#1e293b; font-size:1rem;">{_ftitle}</div>
        <div style="color:#64748b; font-size:0.88rem; margin-top:2px;">{_fdesc}</div>
    </div>
</div>""", unsafe_allow_html=True)
        st.divider()
        st.markdown("### ❓ 자주 묻는 질문")
        with st.expander("🔑 Groq API 키는 어디서 구하나요?"):
            st.markdown("""
1. [console.groq.com](https://console.groq.com) 접속
2. 회원가입 → API Keys → Create API Key
3. TrustLens 사이드바 하단 설정 또는 `.env`에 붙여넣기
- 무료 플랜으로도 하루 수십 번 분석 가능해요.
""")
        with st.expander("💾 데이터는 어디에 저장되나요?"):
            st.markdown("""
현재는 **로컬 JSON 파일** (`trustlens_data.json`)에 저장돼요.
- 앱을 재시작해도 데이터 유지
- 클라우드 동기화는 추후 Supabase 연동 예정
- 지금은 파일을 백업해두면 데이터 보존 가능
""")
        with st.expander("🤖 분석이 안 되거나 느린 경우?"):
            st.markdown("""
- API 키 미입력: 사이드바 ⚙️ 설정 확인
- Groq 서버 일시 과부하: 30초 후 재시도
- Mock 모드(API 키 없음): 더미 결과로 UI 테스트 가능
""")
        with st.expander("📚 지식 라이브러리는 어떻게 쓰나요?"):
            st.markdown("""
**📚 지식 라이브러리**는 내가 지금까지 쓴 **모든 기록이 모이는 곳**이에요. (사이드바 📝 지식 → 지식 라이브러리)

- 맨 위 **대시보드**(📚 메모·🪐 프로젝트·🧠 개념·🏷 태그)로 내 기록 규모를 한눈에 봐요.
- **탐색 5축**으로 둘러봐요:
  - **▶ 전체** — 최신순 카드
  - **📁 프로젝트별** — 프로젝트(행성)마다 묶어서
  - **📅 날짜별** — 월별로 묶어서
  - **🏷 태그별 / 🧠 개념별** — 골라서 그 메모만
- 카드를 **열면** 한 줄 핵심·핵심 개념·연결된 지식·관련 메모를 한 화면에서 봐요.

> 💡 역할 분리 — 📚 **라이브러리** = "내가 *무엇을* 기록했나"(둘러보기) · 🪐 **지식 우주** = "그게 *어떻게 연결*됐나"(조망).
""")
        with st.expander("🌍 세계 성장 리포트 / 🛸 항로는 뭔가요?"):
            st.markdown("""
홈에서 **내 지식 세계가 커지고 연결되는 걸** 보여줘요.

- 🌍 **세계 성장 리포트**: 이번 달 새로 쌓은 메모·개념·관계 + **세계 확장도(%)** + 가장 많이 성장한 영역 / 가장 많이 연결된 개념.
- 🛸 **항로**: 직접 잇지 않아도 **같은 개념·태그를 쓰는 프로젝트**를 JIUM이 자동으로 연결해 선으로 보여줘요.
  - 🟢 실선 = 직접 만든 관계 · 🟣 보라 점선 = 공유 개념 · 🟠 주황 점선 = 공유 태그
  - 항로가 0개여도 걱정 마세요 — 같은 개념을 쓰는 메모가 다른 프로젝트에 생기면 자동으로 그려져요.

> 💡 '아는 만큼 보인다'는 정보가 많아지는 게 아니라, **보이지 않던 연결이 보이기 시작하는 것**이에요.
""")
        with st.expander("🪐 지식 우주 / 행성 간 지식 이동은 어떻게 쓰나요?"):
            st.markdown("""
홈의 **🪐 내 지식 우주**에서 프로젝트는 행성으로 보여요 (메모·개념·작업이 쌓일수록 커져요).

**정리하는 두 가지 방법:**
- **🌎🚀 지구 발사대** (🌌 전체 버튼) — 아직 어디에도 안 속한 지식을 행성으로 보내요. `🚀 전체 발사`로 한 번에도 가능.
- **🚀 다른 행성으로 보내기** (행성 선택 시 상세 상단) — 한 프로젝트의 메모·작업·개념을 골라 다른 프로젝트로 재배치.

**같이 따라가는 것:**
- 🏷️ **태그**는 메모 안에 들어 있어 메모를 옮기면 **자동으로 따라가요.**
- 🧠 **연결된 개념**도 함께 이동해요.
- 🔗 **관계(연구노트↔메모 등)**는 이동해도 **끊기지 않고 그대로 유지**돼요.

> 💡 데이터는 3겹 구조예요 — ① 콘텐츠(프로젝트·노트·작업) ② 의미망(개념·태그) ③ 관계(엣지). 이동은 ①을 옮기고 ②③은 자동으로 따라오거나 보존돼요.
""")

    # ─── 탭(신규): 홈·검색·AI ───────────────────────────────
    with _g6:
        st.markdown("### 🏠 홈 대시보드 · 🔍 통합 검색 · 🧠 지식 AI")
        _g6a, _g6b, _g6c = st.tabs(["🏠 홈 대시보드", "🔍 통합 검색", "🧠 지식 AI"])

        with _g6a:
            st.success("🆕 **이렇게 입력하세요** — ✍️ **홈 한 줄**(홈 대시보드 맨 위, 바로 저장) · 📅 **데일리 노트**(날짜별 기록 + 미니 캘린더 + 🧭 회상 레일). 둘 다 개념이 자동 추출돼 지식망에 연결돼요.")
            st.markdown("#### 🏠 오늘의 대시보드")
            st.markdown("""
사이드바 **🏠 홈 → 홈 대시보드**는 앱을 켜면 가장 먼저 보이는 시작 화면이에요.
맨 위 **✍️ 오늘 한 줄** 칸에 적고 저장하면 오늘 날짜 데일리 노트로 바로 쌓여요.

**한눈에 보여주는 것:**
- 📊 핵심 지표 4개: 진행 중 프로젝트 / 오늘 마감 작업 / 미완료 작업 / 전체 메모 수
- 📂 진행 중인 프로젝트 · ✅ 최근 작업 · 📝 최근 메모 요약
- 🔬 최근 연구노트 · 🧠 최근 추가한 개념

**그 아래 "➕ 새 메모 — 정보 수집하기"** 가 바로 이어져요.
→ 대시보드로 현황 확인 → 그 자리에서 새 정보 수집까지 한 흐름으로 끝나요.

> 💡 분석을 "한 번 하고 끝"이 아니라 **매일 들어와 쌓고 정리하는 습관**이 되도록 홈이 설계돼 있어요.
""")

        with _g6b:
            st.markdown("#### 🔍 통합 검색 (v1)")
            st.markdown("""
사이드바 **🏠 홈 → 통합 검색**. 흩어진 지식을 **한 검색어로 전부** 찾아요.

**한 번에 검색되는 6종:**
| 대상 | 검색 범위 |
|------|-----------|
| 📝 메모 | 제목·본문·원문·태그·프로젝트 |
| 📊 분석 | 제목·요약·메모·URL·태그 |
| 📁 프로젝트 | 이름·설명·분류·상태 |
| ✅ 작업 | 제목·설명·상태·프로젝트·마감일 |
| 🧠 개념 | 이름·설명·폴더·별칭 |
| 🔬 연구노트 | 연구노트 메모 제목·본문 |

**사용법:**
1. 검색어 입력
2. "검색 대상" 멀티셀렉트로 범위 좁히기 (기본은 전체)
3. 종류별 색상 섹션 + 관련도순으로 결과 카드 표시

> 💡 지금은 키워드 검색이에요. 추후 DB 연동 시 **의미 기반(Semantic) 검색 v2**로 업그레이드돼요.
""")

        with _g6c:
            st.markdown("#### 🧠 지식 AI (내 지식에게 묻기)")
            st.markdown("""
사이드바 **🤖 AI → 지식 AI**. 검색이 "찾기"라면, 지식 AI는 **"물어보면 정리해서 답하기"** 예요.

**무엇이 다른가요?**
- 통합 검색: 관련 항목을 *나열*
- 지식 AI: 메모·분석·개념·작업·프로젝트를 *근거로 읽고 요약해서 답변*

**사용 예:**
- "지금까지 조사한 스크린골프 핵심만 정리해줘"
- "내 프로젝트 중 마감 임박한 거 뭐야?"
- "마케팅 관련해서 내가 모아둔 개념 묶어줘"

**동작 방식:**
1. 질문 입력 → 내 지식 전체에서 관련 높은 항목 상위 8개 자동 선별
2. AI가 그 내용만 근거로 한국어 요약 답변 (없는 내용은 지어내지 않음)
3. 답변 아래 **참고한 지식** 출처를 종류별 배지로 표시 → 어디서 나온 답인지 추적 가능

> 💡 메모를 많이 쌓을수록 답이 정확해져요. 빈 데이터여도 안전하게 안내만 떠요.
""")

    # ─── 탭 2: 분석·저장하기 ─────────────────────────────────
    with _g2:
        st.markdown("### 🔍 정보 분석하고 메모로 저장하기")
        _g2a, _g2b, _g2c = st.tabs(["분석 시작하기", "지식 메모 저장", "아카이브 관리"])
        with _g2a:
            st.markdown("#### 📌 분석 시작하기 (4단계 수집 흐름)")
            st.markdown("""
새 메모 화면 상단에 **4단계 진행 표시줄(Stepper)** 이 고정돼 있어요. 단계가 진행될수록 색이 바뀌어요.

| 단계 | 내용 |
|------|------|
| 1️⃣ 정보 가져오기 | 🔗 URL 또는 📋 텍스트 붙여넣기로 입력 |
| 2️⃣ 원문 확인 | 추출된 **원문 전체**를 그대로 확인 (최대 2만 자 보관) |
| 3️⃣ AI 정리 | AI가 4탭으로 정리 (아래 참고) |
| 4️⃣ 지식 메모 저장 | 저장 방식 A/B/C 선택 |

**콘텐츠 유형**을 고를 수 있어요 — 특히 **📚 공부자료(study)** 는 신뢰도가 아니라 **이해 중심 학습 노트**로 정리돼요.

**STEP3 — AI 정리 결과 4탭:**
- 📌 **핵심 요약**: AI가 뽑은 핵심만 빠르게
- 🔍 **신뢰도 판단**: 점수 근거·판단 근거·차트·피드백 (자세한 건 접혀 있음)
- 🏷️ **개념·태그 후보**: AI 태그 + 핵심 개념 후보
- ▶️ **다음 행동**: 메모/프로젝트/작업으로 이어가기

> 💡 같은 URL·유형은 캐시에서 재사용 → API 호출 없음 (원문 추출 한도 변경 시 자동 무효화)
""")
        with _g2b:
            st.markdown("#### 💾 지식 메모 저장하기 (STEP4)")
            st.markdown("""
STEP3 아래에서 **AI 초안 → 내 메모 정리 → 저장**으로 이어져요.

1. **AI 초안 템플릿 선택** (보고서/일기/블로그/체크리스트/자유 — 공부자료는 **공부용 설명** 자동 선택)
2. "🔄 지식 메모 초안 다시 만들기"로 원문 전체 기반 초안 생성
3. 초안을 직접 편집 + **태그 / 프로젝트 / 섹션 / 단계** 선택
4. **STEP4 저장 방식 3가지** 중 선택:

| 버튼 | 동작 |
|------|------|
| 🗂️ 지식 메모로 저장 | (가장 많이 씀) 정리한 메모를 아카이브에 저장 |
| 📌 분석결과만 저장 | 신뢰도 분석결과(점수/근거)만 분석 아카이브에 저장 |
| 🧩 둘 다 저장 | 메모 + 분석결과 모두 저장 |

**자동으로 일어나는 일:**
- 핵심 개념 자동 추출 → 지식맵 업데이트
- `note_concept_links` 자동 생성
- 메모에 `project_id`, `task_id`, `user_id` 자동 연결
- 붙여넣기 원문은 메모 맨 아래에 자동 보관

**빠른 메모 (데이터 관리 → ⚡ 빠른 작업):**
- URL 없이 텍스트만으로 메모 저장
- AI 분석 탭: AI가 제목·태그·개념 자동 생성
""")
        with _g2c:
            st.markdown("#### 🗂️ 지식 아카이브 관리")
            st.markdown("""
저장된 모든 메모를 **검색·필터·편집**하는 공간이에요.

**검색:** 제목 + 본문 + 태그 + 프로젝트 + 섹션 통합 검색

**필터:** 프로젝트 / 태그 / 콘텐츠 유형 / 날짜 / 신뢰도 점수

**카드 기능:**
- ⭐ 즐겨찾기  ✏️ 편집  🏷️ 태그 수정  🗑️ 삭제
- 📋 원문 텍스트 펼쳐보기

**보기 방식:** 카드 뷰 / 리스트 뷰 전환 가능
""")

    # ─── 탭 3: 프로젝트·작업 ────────────────────────────────
    with _g3:
        st.markdown("### 📁 프로젝트와 작업 관리하기")
        _g3a, _g3b, _g3c = st.tabs(["프로젝트 만들기", "작업 관리", "보드 뷰"])
        with _g3a:
            st.markdown("#### 📂 프로젝트")
            st.markdown("""
TrustLens에서 **프로젝트**는 연구/공부/업무 단위예요.

**프로젝트 구조:**
```
📁 프로젝트
  ├── 📂 섹션 (예: 1차 조사, 발표 준비)
  │     └── 📋 단계 (예: 자료 수집, 분석, 작성)
  └── ✅ 작업들
```

**만드는 방법 2가지:**
1. **➕ 새 메모·엔터티** (사이드바 🏠 홈 그룹) → 프로젝트/작업/개념/메모/폴더를 한 곳에서 생성
2. 프로젝트 메뉴 → ➕ 새 프로젝트 버튼
- 이름 / 설명 / 대분류 / 상태 / 우선순위 / 마감일 입력
- 진행률은 슬라이더로 직접 설정

> 💡 **새 엔터티 위저드**: 생성 기능이 여러 페이지에 흩어져 있던 걸 하나로 통합했어요.
> 만든 엔터티는 즉시 지식맵·관계·엔터티 상세에 반영돼요.

**프로젝트 상세 탭:**
- 📄 연결된 자료 / 📂 섹션 관리 / 📅 캘린더 / 📊 타임라인 / **🗺️ 프로젝트 맵**

> 🗺️ **프로젝트 맵 (신규)**: 프로젝트를 중심으로 **작업 → 메모·개념**, **자료 → 개념** 관계를 한 화면에서 봐요.
> - 상단 요약: 작업 / 연결 메모 / 연결 개념 / 연결 관계 수
> - 핵심 개념: 칩 **크기 = 등장 빈도**, **색 = 최근성**(🔴최근 🟡보통 ⚪오래됨), 마우스를 올리면 중요도(importance)
> - 작업 카드: 상태·마감일·연결 메모/개념 수 + 연결 강도 막대
""")
        with _g3b:
            st.markdown("#### ✅ 작업 관리")
            st.markdown("""
**작업 필드:**
| 필드 | 설명 |
|------|------|
| 제목 | 할 일 이름 |
| 프로젝트 | 어느 프로젝트 소속인지 |
| 상태 | 시작전 / 진행중 / 완료 / 보류 |
| 우선순위 | 높음 / 중간 / 낮음 |
| 마감일 | 날짜 선택 |

**보기 방식 4가지:**
- 📋 목록 뷰: 전체 작업 리스트, 빠른 상태 변경
- 🗂️ 보드 뷰: **칸반 5단계** (시작 전 / 진행 중 / 검토 중 / 완료 / 보류)
  - **← → 버튼**으로 열(상태) 이동 가능, 단계별 색상 구분
- 📅 캘린더 뷰: 마감일 기준 달력
- 📊 타임라인 뷰: 프로젝트별 간트 차트

**메모에서 작업 만들기:**
- 🔎 엔터티 상세 → 📝 메모 탭 → ➕ 작업 만들기
- 생성된 작업엔 `source_note_id`가 붙어 출처 메모 추적 가능
""")
        with _g3c:
            st.markdown("#### 🧩 보드 뷰 사용법")
            st.markdown("""
**노션식 보드 (`🕸️ 지식 맵` → 🧩 보드 탭):**

지식 메모를 **대분류별 칸반**으로 볼 수 있어요.

**필터:** 프로젝트 / 대분류 / 태그 / 기간 / 최소 신뢰도 점수

**← → 버튼으로 열 이동:**
- 이동 내역은 `pkm_category_overrides`에 저장 → 재시작해도 유지

**보드 종류 3가지:**
| 보드 | 위치 | 열 구성 |
|------|------|---------|
| 노션식 보드 | 지식 맵 → 보드 | 대분류별 (뉴스/정책/...) |
| 프로젝트 보드 | 프로젝트 → 보드 | 상태별 (계획중/진행중/완료/보류) |
| 작업 보드 | 작업 관리 → 보드 | 상태별 (시작전/진행중/완료/보류) |
""")

    # ─── 탭 4: 지식맵·개념 ──────────────────────────────────
    with _g4:
        st.markdown("### 🧠 지식맵과 개념 관리")
        _g4a, _g4b, _g4c = st.tabs(["지식맵 탭 구조", "개념 관리", "AI 브레인스토밍"])
        with _g4a:
            st.markdown("#### 🕸️ 지식맵 8가지 뷰")
            st.markdown("""
| 탭 | 설명 |
|----|------|
| 📚 원노트 목차 | 프로젝트별 계층형 목차 (섹션 > 단계 > 메모) |
| 🧩 노션 보드 | 대분류별 칸반 보드. 필터: 프로젝트/태그/기간/최소점수 |
| 🕸️ 태그 마인드맵 | 태그 중심 또는 프로젝트별 행성 모드 Plotly 시각화 |
| 🧠 지식 페이지 | 메모를 페이지처럼 읽기 |
| 🗂️ 개념 파인더 | 폴더 칩 필터 + 카드 그리드로 개념 탐색 |
| 🤖 AI 브레인스토밍 | 메모/프로젝트 기반 AI 아이디어 제안 (크로스 분석 포함) |
| 🗺️ 프로젝트 지식맵 | 프로젝트 중심 마인드맵 — 메모·작업·개념·태그가 위성으로 연결 |
| 🔗 관계형 지식맵 | 옵시디언 스타일 네트워크 그래프 — `relations` DB 기반 실제 연결 시각화 |

**마인드맵 모드:**
- **태그 중심**: 태그가 중심 노드, 메모가 연결 노드
- **프로젝트별 행성**: 프로젝트=행성, 메모·개념=위성

**🗺️ 프로젝트 지식맵 사용법:**
1. 프로젝트 선택 드롭다운에서 보고 싶은 프로젝트 선택
2. 메모(신뢰도 색상), 작업(상태 색상), 개념, 태그가 노드로 표시
3. 하단 상세 패널에서 메모/작업/개념 목록 확인

**🔗 관계형 지식맵 사용법:**
1. 노드 유형(프로젝트/메모/개념/태그) 표시 여부 선택
2. 최소 연결 수 필터로 중요한 노드만 표시
3. 노드 선택 → 하단 상세 패널에서 연결된 엔터티·방향 확인
4. `🔗 데이터 관리 → 관계 관리` 에서 relations를 추가할수록 그래프가 풍부해져요
""")
        with _g4b:
            st.markdown("#### 🧠 개념 관리")
            st.markdown("""
**개념의 종류:**
- **AI 추출 개념**: 분석·저장 시 자동 추출
- **직접 추가 개념**: 내가 직접 등록한 개념

**개념 구조:**
```
개념명: "CREST 프레임워크"
폴더: "마케팅/프레임워크"
설명: "신뢰도 평가 5요소..."
```

**관리하는 곳:** `🔗 데이터 관리` → 테이블 편집 → 🧠 개념
- 내 개념 수정/추가/삭제
- AI 개념 선택해서 등록
- 개념 폴더 이동 (⚡ 빠른 작업)
- 중복 개념 병합 (⚡ 빠른 작업)

**개념 파인더 활용:**
1. 지식 맵 → 🗂️ 개념 파인더 탭
2. 상단 폴더 칩 클릭 → 해당 폴더 개념만 필터
3. 카드로 개념·연결 메모 수 확인

---

#### ✅ 개념 품질 게이트 (신규)
개념이 **저장되기 전에** 자동으로 정제돼요. 전부 경량 규칙 기반이라 API 비용이 들지 않아요.

| 단계 | 동작 | 예시 |
|------|------|------|
| 1. 정규화 | 조사·어미·기호 제거 | `경찰은→경찰`, `알고리즘에→알고리즘`, `#딥러닝→딥러닝` |
| 2. 불용어 | 일반어 정확 일치 제거 | `정보·사회·여기·최근` 제거 (단, `정보보안·사회복지`는 보존) |
| 3. 길이/기호 | 1글자·숫자·기호만 제외 | `ㄱ`, `2024`, `!!!` |

> 🛡️ **고유명사 보호**: `진영이네는 → 진영이네`처럼 가게명·고유명사는 과하게 자르지 않아요.
> 🚫 **품질 리포트**: 걸러진 개념은 버리지 않고 기록돼, 핵심 개념 허브의 **‘개념 품질 리포트’**에서 자주 제외된 단어·사유를 볼 수 있어요 (불용어 사전 개선에 활용).

#### 🔗 개념 별칭 (alias) — v3.8
같은 개념의 다른 표기를 **대표 개념으로 비파괴적으로 묶어요**.
- 예: `BackPropagation`·`역전파 알고리즘`·`backpropagation algorithm` → **역전파**
- 핵심 개념 허브 → **🔗 개념 별칭 관리**에서 대표 개념 + 별칭(쉼표)을 등록/삭제
- 원본 메모는 그대로 두고, **검색·관련 메모 추천·프로젝트 맵·빈도/TF-IDF 랭킹에서만** 대표 개념으로 합산돼요
- 완전 병합(데이터 변경)보다 안전하고, 잘못 등록하면 별칭만 삭제하면 다시 분리돼요

#### 🔝 핵심 개념 Top N · ⭐ TF-IDF 중요 개념
핵심 개념 허브에는 두 가지 랭킹이 있어요.
- **🔝 자주 등장하는 개념 Top 10**: 단순 빈도. 프로젝트 맵의 노드 크기·색으로도 쓰여요.
- **⭐ TF-IDF 중요 개념 Top 10**: 전체 메모에서 흔한 개념은 낮게, 특정 메모·프로젝트에 **특화된 개념**은 높게 평가해요 (API 비용 없는 통계 기반).

> 프로젝트 맵의 핵심 개념 영역에서 **빈도순 / 중요도순(TF-IDF)** 토글로 두 관점을 전환할 수 있어요.

---

#### 🗂️ 지식 아카이브 — 두 번째 뇌 (v3.7)
저장한 메모를 **카드**로 훑고, 카드를 열면 **노트 상세 페이지**가 나와요.
- 📌 **한 줄 핵심** · 🧠 **핵심 개념** 칩
- 🔗 **연결된 지식**: 프로젝트 / 작업 (이 메모에서 만든 작업, 연결된 작업)
- 🪢 **관련 메모 추천**: 개념을 공유하는 다른 메모로 바로 이동 (노트→노트 탐험)
- 📚 **원문 접힘** · 🤖 **이 메모로 지식 AI에 질문하기**
- ✏️ 상세 안에서 편집/삭제도 가능해요.
""")
        with _g4c:
            st.markdown("#### 🤖 AI 브레인스토밍")
            st.markdown("""
**메모 기반 분석 유형:**
| 유형 | 결과 |
|------|------|
| 확장 주제 제안 | 더 탐구할 주제 3~5개 |
| 추가 조사 질문 | 아직 답 못한 질문들 |
| 반대 관점 | 이 정보의 반론/한계 |
| 발표 문장 초안 | 발표/글에 쓸 문장 |
| 연결 개념 찾기 | 관련 있는 개념들 |
| 다음 할 일 | 구체적인 행동 제안 |

**프로젝트 기반:** 부족한 자료 / 조사 방향 / 발표 목차 / 예상 질문 / 추가 작업

**🔀 크로스 분석:** 메모 2~4개 비교 → 공통 개념 / 충돌 지점 / 보완 관계

**🔬 AI 연구노트 (신기능):**
- 메모 2~6개 선택 (프로젝트·태그로 후보 좁히기)
- AI가 **공통점 / 충돌점 / 새 아이디어 / 다음 작업** 4섹션으로 구조화
- 결과를 📔 연구노트로 지식 아카이브에 저장
- '다음 작업' 항목을 골라서 ✅ 작업으로 일괄 생성

> 💡 메모를 많이 쌓은 후 사용할수록 더 풍부한 결과가 나와요!
""")

    # ─── 추가 탭: 엔터티 상세 ─────────────────────────────────
    with st.expander("🔎 엔터티 상세페이지 사용법", expanded=False):
        st.markdown("""
**사이드바 ⚙️ 관리 → 🔎 엔터티 상세**

개념·프로젝트·태그 중 하나를 선택하면 위키 스타일 상세 페이지가 열려요.

**상단 바로가기 버튼:**
| 버튼 | 이동 |
|------|------|
| 🔗 관계형 지식맵 | 탭8에서 이 엔터티 중심으로 보기 |
| 🗺️ 프로젝트 지식맵 | 탭7 (프로젝트만 활성화) |
| 🔎 개념 파인더 | 지식맵 탭5로 이동 |
| 📁 프로젝트 상세 | 프로젝트 페이지로 이동 |

**5개 서브탭:**
- 📝 메모: 연결 메모 목록 + **➕ 작업 만들기** (메모에서 바로 task 생성)
- ✅ 작업: 연결 작업 + 출처 메모 표시 + **빠른 작업 추가**
- 🔗 관계: 방향 있는 관계 목록 + 🔎 버튼으로 연결 엔터티 이동
- 🧠 공통 개념: 이 엔터티 메모들에서 공통으로 나온 개념 클릭 이동
- ✏️ 편집: 설명·폴더·별칭·상태 직접 수정

**프로젝트 선택 시 대시보드 자동 표시:**
- 진행률 바, 전체/완료/진행중/보류 작업 수, 메모·개념·관계 카운트
""")

    # ─── 탭 5: 데이터 관리 ──────────────────────────────────
    with _g5:
        st.markdown("### 🔗 데이터 관리 완전 정복")
        _g5a, _g5b, _g5c = st.tabs(["테이블 편집", "관계·ERD", "빠른 작업·엔터티 DB"])
        with _g5a:
            st.markdown("#### 📋 테이블 편집 탭")
            st.markdown("""
**5가지 엔터티를 직접 표 형식으로 편집**해요.

| 엔터티 | 편집 가능 항목 |
|--------|----------------|
| 📁 프로젝트 | 이름/상태/우선순위/설명 + 행 추가/삭제 |
| 📝 지식 메모 | 제목/프로젝트/섹션/단계 수정 |
| ✅ 작업 | 작업명/프로젝트/상태/우선순위/마감일 |
| 🧠 개념 | 내 개념 추가/수정, AI 개념 선택 등록, 전체 병합 뷰 |
| 🏷️ 태그 | 이름 변경, 삭제, 태그 병합 |

**개념 서브탭:**
- 내 개념 / AI 추출 개념 선택 등록 / 병합 뷰

**🎨 노션식 옵션 관리 (신규):**
- 상태·우선순위·프로젝트·폴더 같은 **선택형 컬럼은 셀을 클릭하면 드롭다운**이 바로 펼쳐져요.
- 표 위 **"🎨 ○○ 옵션 관리"** 를 펼치면 현재 옵션이 색상 칩으로 보이고, 하단에서 **새 옵션을 바로 추가**할 수 있어요.
- 프로젝트·폴더는 추가 즉시 실제 엔터티로 생성되고, 추가하면 토스트로 알려줘요.
""")
        with _g5b:
            st.markdown("#### 🔗 관계 관리 + ERD 뷰")
            st.markdown("""
**관계 관리 탭:**

| 관계 종류 | 설명 |
|-----------|------|
| 📁 프로젝트 → 🧠 개념 | 프로젝트가 다루는 핵심 개념 |
| 📁 프로젝트 → ✅ 작업 | 프로젝트에 포함된 작업 |
| 📝 메모 → 🧠 개념 | 메모에서 다루는 개념 수동 연결 |
| 🧠 개념 → 🧠 개념 | 개념 간 관계 정의 |

**관계 유형:** 포함 / 참조 / 반박 / 지지 / 확장 / 연결 / 유사 / 선행
→ 체크박스로 연결 후 관계 유형 선택 → `relations` 테이블에 저장

**🤖 AI 관계 추천 (신기능):**
- 관계 관리 탭 상단 → "AI 관계 추천" 펼치기
- 저장된 개념·메모를 AI가 분석 → 관련 있을 법한 개념 쌍 + 관계 유형 자동 제안
- ➕ 버튼으로 개별 추가 / "추천 전체 추가"로 일괄 등록
- AI가 만든 관계는 `created_by: ai_suggestion`으로 기록

**🔗 관계형 지식맵과 연동:**
- 저장된 relations가 많을수록 지식 맵 탭8이 더 풍부하게 표시돼요
- `note_concept_links`(메모 저장 시 자동 생성)도 그래프에 자동 반영

**ERD 뷰:**
- 🔵 프로젝트  🟢 개념  🟡 작업  🟣 메모
- 중심 프로젝트 선택 또는 전체 보기
- 관계를 많이 연결할수록 ERD가 풍부해져요
""")
        with _g5c:
            st.markdown("#### ⚡ 빠른 작업 + 🗄️ 엔터티 DB")
            st.markdown("""
**⚡ 빠른 작업:**

| 기능 | 설명 |
|------|------|
| 📝 빠른 메모 추가 | 텍스트 붙여넣기로 메모 저장 (단순/AI 분석) |
| 🔗 개념 병합 | 중복 개념 여러 개 → 하나로 합치기 |
| 📦 개념 폴더 이동 | 개념들의 소속 폴더 일괄 변경 |
| 📁 메모 프로젝트 이동 | 메모 여러 개를 다른 프로젝트로 이동 |
| 🗑️ 일괄 삭제 | 프로젝트/개념/작업/태그 일괄 삭제 |

**🗄️ 엔터티 DB (v2 신기능):**
- 엔터티 목록: 타입별 필터 + 테이블 뷰 + 삭제
- 관계 목록: 저장된 모든 관계 + 삭제 + 타입별 통계

> 이 구조는 향후 **Supabase/PostgreSQL** 연동 시 그대로 DB 테이블로 전환돼요.
> `user_id`, `created_at`, `deleted_at` 필드가 이미 포함되어 있어요.
""")

        st.divider()
        st.markdown("### 🧭 추천 사용 시나리오")
        _scenarios = [
            ("📚 논문·기사 리서치",
             "URL 분석 → 메모 저장 (프로젝트 연결) → 개념 파인더로 연결 확인 → AI 브레인스토밍으로 추가 조사 방향 생성"),
            ("🎯 발표 준비",
             "관련 자료 여러 개 분석 저장 → 프로젝트 생성 → 메모 연결 → AI '발표 목차 제안' → 작업 목록으로 체크리스트"),
            ("🏢 업무 지식 관리",
             "회의록/문서 붙여넣기 → AI 분석 후 저장 → 프로젝트/섹션 구조화 → ERD로 전체 지식 구조 파악"),
            ("🔍 팩트체크·미디어 리터러시",
             "의심 기사 URL 분석 → 신뢰도 점수·편향 확인 → AI '반대 관점' 브레인스토밍 → 비교 메모 저장"),
        ]
        for _stitle, _sdesc in _scenarios:
            with st.expander(_stitle):
                st.markdown(_sdesc)

    st.stop()


if menu == "패치 노트":
    # ══════════════════════════════════════════════════════════
    # 🆕 버전 패치 노트 + 💼 포트폴리오
    # ══════════════════════════════════════════════════════════
    st.markdown("""
<div style="
    background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 55%, #2563eb 100%);
    border-radius: 16px; padding: 28px 32px 24px; margin-bottom: 24px; color: white;">
    <div style="font-size:2rem; font-weight:900; letter-spacing:-1px; margin-bottom:6px;">
        🆕 패치 노트 & 포트폴리오
    </div>
    <div style="font-size:1rem; opacity:0.88; line-height:1.6;">
        버전별 업데이트·기능·수정 내역을 한 곳에서 추적해요.<br>
        포트폴리오 탭은 이 프로젝트를 <strong>PM·기획 관점</strong>으로 정리한 요약본이에요.
    </div>
</div>
""", unsafe_allow_html=True)

    _CHANGELOG = [
        {
            "version": "v4.3",
            "codename": "Knowledge Library",
            "date": "2026-06-02",
            "title": "지식 라이브러리 — 내 기록이 모이는 도서관",
            "badge": "v4.3 완료",
            "problem": [
                "기능은 많은데 '내가 쓴 메모 전체를 둘러보는 허브'가 약함",
                "지식 아카이브가 '검색창 + 빈 공간'처럼 보여 중심 같지 않음",
                "프로젝트별·날짜별로 기록을 묶어 보는 길이 없음",
            ],
            "improvement": [
                "📚 지식 아카이브 → 📚 지식 라이브러리로 승격(읽기 허브)",
                "상단 대시보드: 📚 메모 · 🪐 프로젝트 · 🧠 개념 · 🏷 태그 카운트",
                "탐색 5축: ▶전체 / 📁프로젝트별 / 📅날짜별 / 🏷태그별 / 🧠개념별",
            ],
            "result": [
                "'내 모든 기록이 모이는 도서관' 체감 — 둘러보기 동선 생김",
                "역할 분리: 라이브러리=무엇을 기록했나 / 우주=어떻게 연결됐나",
            ],
            "features": [
                "검색·즐겨찾기·카드·상세(Second Brain)는 그대로 유지",
                "태그별·개념별은 선택 → 해당 메모만 필터",
                "태그 관리는 유지(태그 탐색 vs 유지보수 — 역할 분리)",
            ],
            "fixes": [
                "메모 작성 시 '새 개념 추가' 인라인 입력(개념 없어도 안 막힘)",
                "데일리노트 dn_date Session State 경고 제거",
                "엔터티 상세 카드 HTML이 텍스트로 보이던 버그(마크다운 4칸 들여쓰기) 수정",
            ],
            "infra": [
                "카드 렌더를 헬퍼로 추출해 탐색 축마다 재사용",
                "메뉴 IA 개편(기록→분석→연결→세계)은 방향 확정, 사용 검증 후 진행 예정",
            ],
        },
        {
            "version": "v4.2",
            "codename": "Connected Worlds",
            "date": "2026-06-02",
            "title": "Connected Worlds — 보이지 않던 연결이 보이기 시작하다",
            "badge": "v4.2 완료",
            "problem": [
                "메모·개념·관계가 쌓여도 '내 세계가 얼마나 커졌나'를 체감 못 함",
                "프로젝트끼리 사실 이어져 있어도 그 연결이 안 보임",
                "행성이 단조롭고 선택 상태가 색으로만 표현돼 약함",
            ],
            "improvement": [
                "🌍 세계 성장 리포트(홈): 이번 달 신규·세계 확장도%·가장 성장한 영역·가장 연결된 개념",
                "🛸 자동 항로: 공유 개념(🟣)·공유 태그(🟠)·직접 관계(🟢)로 행성 연결 자동 발견",
                "🪐 행성마다 고유 색 + 대기광(글로우), 🚀 탐사선으로 선택 표현",
            ],
            "result": [
                "'아는 만큼 보인다' = 정보량↑이 아니라 보이지 않던 연결이 보이는 것",
                "내 세계가 커지고 연결되는 걸 눈으로 보는 경험",
            ],
            "features": [
                "항로 강도 = 공유 개념×2 + 태그 + 직접관계 보너스, 발견된 항로 목록",
                "🚀 탐사선은 행성 바깥 궤도의 작은 보조 표시(주인공=행성)",
                "우주맵 설명은 'ℹ️ 읽는 법' 접기로 통합",
            ],
            "fixes": [
                "선택 행성과 중심 '내 지식'이 같은 색이던 문제 → 행성=팔레트색·중심=금빛 분리",
                "항로 0개 문구 긍정형으로(🌱 자동으로 연결을 발견해요)",
            ],
            "infra": [
                "데이터 3계층 모델(콘텐츠·의미망·관계)을 시각화 기준으로 적용",
            ],
        },
        {
            "version": "v4.1",
            "codename": "Knowledge Universe",
            "date": "2026-06-02",
            "title": "지식 우주 정리 — 행성 간 이동 + 배포 안정화",
            "badge": "v4.1 완료",
            "problem": [
                "발사대는 '미배정→행성'만 가능, 행성↔행성 재배치가 안 됨",
                "메모를 옮겨도 연결 개념이 소스 프로젝트에 남아 따로 놀음",
                "발사/이동 버튼 색이 흐려 처음에 잘 안 보임",
                "배포가 'Oh no'로 죽거나 healthz 503으로 안 뜨는 장애 반복",
            ],
            "improvement": [
                "🪐 행성 상세에 '🚀 다른 행성으로 보내기' 패널 — 메모·작업·개념 골라 재배치",
                "🚀 발사대에 '전체 발사' 버튼 + 발사/이동 버튼 primary 색으로 강조",
                "우주맵 캡션에 🌎🚀 지구 발사대 안내 한 줄 추가",
                "발사대 빈 상태 카피: '🌍 모든 지식이 제자리를 찾았어요'(정리 완료 느낌)",
            ],
            "result": [
                "미배정→행성뿐 아니라 행성↔행성도 자유롭게 정리 가능",
                "메모 이동 시 태그·연결 개념이 함께 따라가 지식이 흩어지지 않음",
                "배포가 안정적으로 기동 — 'Oh no'/503 장애 해소",
            ],
            "features": [
                "메모 이동 시 note_concept_links로 연결 개념 정의(project)까지 동반 이동",
                "태그는 메모 내장이라 자동 동반, 관계(relations)는 이름 엣지라 이동해도 보존",
                "사이드바 그룹별 포인트색(홈=하늘/지식=보라/프로젝트=초록/AI=주황/분석=빨강/관리=회색) + 활성 표시",
                "메뉴 용어 정리: 홈 대시보드→대시보드, 새 메모·엔터티→빠른 작성",
            ],
            "fixes": [
                "배포 'Oh no' 실제 원인 = 분석결과 페이지의 중첩 expander(render_feedback_section) → checkbox로 대체",
                "config.toml server.headless=true — false면 '이메일 온보딩' 입력 대기로 8501 안 떠 healthz refused",
            ],
            "infra": [
                "중첩 expander 전체 스캔(함수호출 경로 포함) 0건 유지 규칙화",
                "데이터 3계층 모델 정립: 1)콘텐츠(프로젝트·노트·작업) 2)의미망(개념·태그) 3)관계(relations 엣지)",
            ],
        },
        {
            "version": "v4.0",
            "codename": "Daily Note",
            "date": "2026-06-02",
            "title": "데일리 노트 — 분석하는 앱에서 매일 기록하는 앱으로",
            "badge": "v4.0 완료",
            "problem": [
                "저장·분석은 강한데(9/8점) 정작 매일 쓰는 '입력'이 약함(5점)",
                "'오늘 생각난 거'를 적으려면 메모 생성→프로젝트 연결→개념→저장까지 동선이 무거움",
                "Second Brain인데 날짜 기반 입력 허브가 없었음",
            ],
            "improvement": [
                "🏠 홈에 📅 데일리 노트 신설 — 날짜 선택 후 바로 입력",
                "입력이 통계보다 먼저 보이게 배치 (좌측 입력 / 우측 그날 메모·최근 개념)",
                "저장 시 개념 자동 추출·연결, note_type=daily_note로 분리",
            ],
            "result": [
                "'캘린더→열기→생성→연결→저장'이 '날짜→적기→저장'으로 단축",
                "입력량이 늘면 개념·관계·프로파일(상위 기능)이 더 살아나는 선순환 기반",
            ],
            "features": [
                "오늘 한 일 / 배운 것 / 생각·아이디어 + 프로젝트·태그",
                "그 날짜의 기존 메모(한 줄 핵심·개념 칩·열기) + 최근 자주 등장한 개념",
                "tags=[데일리노트,날짜], saved_at=선택 날짜, 같은 날짜 다중 노트 허용",
            ],
            "fixes": [
                "concepts 없어도 무오류, 저장 후 입력칸 초기화·즉시 갱신",
            ],
            "infra": [
                "create_memo 재사용 + note_type/one_line_summary 보강, page key 'daily'",
            ],
        },
        {
            "version": "v3.9",
            "codename": "Control Center",
            "date": "2026-06-02",
            "title": "설정 통합 — 흩어진 기능을 발견할 수 있는 ⚙️ Control Center",
            "badge": "v3.9 완료",
            "problem": [
                "기능이 폭증해 세계관·루미·애니메이션·토글 설정이 여기저기 흩어짐",
                "‘그거 어디서 켜?’ ‘세계관 어디서 바꿔?’ — 기능이 있어도 발견하기 어려움",
                "사이드바가 메뉴 추가마다 계속 길어짐",
            ],
            "improvement": [
                "⚙️ Control Center 한 페이지로 통합 (6탭: 화면·세계관·루미·Second Brain·알림·실험실)",
                "사이드바 하단에 설정/가이드/패치노트를 고정 분리 — 본 메뉴는 짧게",
                "📊 실험실(베타 플래그)로 실험 기능을 사용자가 직접 켜고 끄며 발견",
            ],
            "result": [
                "‘기능은 많은데 어디서 바꾸지’ 문제 해소 — 발견 가능성↑",
                "별칭·TF-IDF·철학 프로파일이 추가돼도 ‘어디서 보는지’ 안 헤맴",
            ],
            "features": [
                "🎨 화면(카드 밀도·글자 크기·애니메이션) · 🌌 세계관(기본/숲/우주/연구소+미리보기)",
                "🤖 루미(친구/분석가/코치/철학자 페르소나·안내 수준)",
                "🧠 Second Brain 토글(자동 개념·품질 게이트·TF-IDF·관련 메모) · 🔔 알림 · 📊 실험실",
                "app_settings 영속화 + get_setting() 단일 진입점, 저장/기본값 복원",
            ],
            "fixes": [
                "토글 실연동: TF-IDF off → 개념 허브·맵에서 숨김 / 관련 메모 off → 노트 상세에서 숨김",
            ],
            "infra": [
                "APP_SETTINGS_DEFAULTS로 기본값 보강·누락키 폴백, 사이드바 하단 고정 그룹에 settings 추가",
            ],
        },
        {
            "version": "v3.8",
            "codename": "Alias Network",
            "date": "2026-06-02",
            "title": "개념 별칭 시스템 — 같은 개념의 다른 표기를 비파괴적으로 묶기",
            "badge": "v3.8 완료",
            "problem": [
                "역전파·BackPropagation·역전파 알고리즘이 서로 다른 개념으로 취급돼 흩어짐",
                "관련 메모 추천·프로젝트 맵·랭킹이 ‘개념 문자열 일치’에 의존해 같은 주제를 나눠 읽음",
                "완전 병합은 데이터 손실 위험이 있어 망설여짐",
            ],
            "improvement": [
                "대표 개념 ← 별칭 매핑(concept_aliases)을 비파괴적으로 저장 (원본 메모 불변)",
                "canonical_concept()로 집계 시점에만 대표 개념으로 합산",
                "개념 병합 워크플로의 ‘별칭 등록’ 옵션을 실제 별칭 저장소에 연결",
            ],
            "result": [
                "관련 메모 추천·프로젝트 맵·빈도/TF-IDF 랭킹이 같은 개념군으로 묶여 품질↑",
                "완전 병합(손실 위험)과 의미 병합(무거움) 사이의 안전한 중간 단계 확보",
            ],
            "features": [
                "🔗 개념 별칭 관리 UI (대표 개념 + 쉼표 별칭 등록·목록·alias 수·삭제)",
                "canonical_concept / canonical_concepts / add_concept_aliases / remove_concept_alias",
                "적용: 관련 메모 추천 · 프로젝트 맵 · concept_frequency · concept_tfidf",
            ],
            "fixes": [
                "자기 자신·중복·빈 별칭 제외, 별칭 삭제 시 다시 분리",
            ],
            "infra": [
                "concept_aliases 영속화(save/init) + 역방향 매핑 캐시(_alias_rev_cache)",
            ],
        },
        {
            "version": "v3.7",
            "codename": "Second Brain",
            "date": "2026-06-02",
            "title": "지식 아카이브 UX 개편 — 저장소에서 ‘다시 읽는’ 두 번째 뇌로",
            "badge": "v3.7 완료",
            "problem": [
                "아카이브가 ‘제목+편집칸’ 평면 리스트라 파일 탐색기처럼 느껴지고 다시 읽고 싶지 않음",
                "저장한 메모의 핵심·연결 관계가 한눈에 안 보여 ‘쌓아만 두는’ 상태",
                "관련 메모로 이어지는 탐험 동선이 없음",
            ],
            "improvement": [
                "아카이브를 카드 그리드(유형 아이콘·한 줄 핵심·개념 칩·열기)로 재구성",
                "노트 상세 페이지 신설: 한 줄 핵심 / 핵심 개념 / 연결된 지식 / 관련 메모 추천 / 원문 접힘 / 지식 AI 질문",
                "공유 개념 기반 관련 메모 추천으로 노트→노트 탐험 동선 추가",
            ],
            "result": [
                "‘저장소’에서 ‘다시 읽고 연결을 탐험하는 두 번째 뇌’ 경험으로 전환",
                "메모·개념·프로젝트·작업이 상세 한 화면에서 이어져 보임",
            ],
            "features": [
                "카드 목록(2열·최신순·개념 검색 포함)",
                "노트 상세: 📌 한 줄 핵심 · 🧠 핵심 개념 · 🔗 연결 프로젝트/작업 · 🪢 관련 메모 추천 · 📚 원문 접힘",
                "🤖 ‘이 메모로 지식 AI에 질문하기’ (질문 프리필 후 지식 AI로 이동)",
                "상세 안 ✏️ 편집/🗑️ 삭제 (기존 편집 기능 유지)",
            ],
            "fixes": [
                "one_line_summary/concepts 없는 구버전 노트는 summary·note 첫 문장으로 폴백",
            ],
            "infra": [
                "archive_open_note_id 상태로 목록↔상세 전환, query_params 기반 페이지 점프 재사용",
            ],
        },
        {
            "version": "v3.6",
            "codename": "TF-IDF Highlight",
            "date": "2026-06-02",
            "title": "TF-IDF 중요 개념 강조 — 흔한 개념 말고 ‘특화 개념’ 찾기",
            "badge": "v3.6 완료",
            "problem": [
                "단순 빈도 랭킹은 모든 메모에 흔한 개념(정보·테스트 등)이 항상 상위라 변별력이 약함",
                "프로젝트를 특징짓는 개념이 흔한 개념에 묻힘",
            ],
            "improvement": [
                "메모 단위 문서 기준 TF-IDF 도입 (IDF는 전체 코퍼스 → 흔한 개념 강등)",
                "프로젝트 맵 핵심 개념에 ‘빈도순 / 중요도순(TF-IDF)’ 토글",
                "개념 허브에 ⭐ TF-IDF 중요 개념 Top 10 섹션 추가",
            ],
            "result": [
                "전체에서 흔치 않지만 특정 프로젝트에 특화된 개념을 상위로 끌어올림 (예: CREST·역전파)",
                "LLM/API 비용 없이 ‘더 똑똑한’ 개념 랭킹 확보",
            ],
            "features": [
                "concept_tfidf(note_filter, top_n) — idf=log((1+N)/(1+df))+1, tfidf=tf×idf",
                "프로젝트 맵 빈도/중요도 토글 (칩 크기=TF-IDF, 색=최근성, hover=빈도·df)",
                "개념 허브 ⭐ TF-IDF Top 10",
            ],
            "fixes": [
                "concepts 없는 메모 제외, 코퍼스 0이면 빈 결과 — 오류 없음",
            ],
            "infra": [
                "math.log + collections.Counter만 사용 (konlpy/sklearn/wordcloud 미사용)",
            ],
        },
        {
            "version": "v3.5",
            "codename": "Project Map",
            "date": "2026-06-02",
            "title": "프로젝트 중심 관계맵 — 작업·자료·개념·관계를 한 화면에",
            "badge": "E-3 완료",
            "problem": [
                "프로젝트·작업·메모·개념이 각각 따로 보여 ‘무엇이 무엇과 연결됐는지’ 한눈에 안 보임",
                "TrustLens가 ‘저장 앱’처럼 느껴지고 ‘지식 운영체제’라는 차별점이 드러나지 않음",
                "개념 빈도·최근성 데이터를 쌓아만 두고 실제로 보여주는 화면이 없었음",
            ],
            "improvement": [
                "프로젝트 상세에 ‘🗺️ 프로젝트 맵’ 탭 추가 (트리/카드형 MVP, 외부 그래프 의존성 없음)",
                "프로젝트를 중심 노드로 작업→메모·개념, 자료→개념 관계를 카드로 전개",
                "핵심 개념을 빈도·최근성·중요도(importance)로 시각화",
            ],
            "result": [
                "프로젝트 상세가 자료·작업·개념·일정·관계를 모두 가진 공간으로 완성",
                "지금까지 쌓은 연결 구조(작업↔메모↔개념)·품질 게이트·빈도/최근성이 한 화면에서 의미를 가짐",
            ],
            "features": [
                "상단 관계 요약: 작업 / 연결 메모 / 연결 개념 / 연결 관계 수",
                "핵심 개념 Top N — 칩 크기 = 빈도, 색/배지 = 최근성, hover = 중요도(importance)",
                "작업 카드 — 상태·마감일·연결 메모/개념 수 + 연결 강도 막대(선 굵기 대용)",
                "자료 카드 — 메모별 연결 개념 chip + 연결된 작업 수",
                "Empty State — 연결이 없을 때 ‘작업 수정에서 메모·개념을 연결해보세요’ 안내",
            ],
            "fixes": [
                "linked_note_ids / linked_concepts / concepts 미보유 구버전 데이터 .get(…, []) 안전 처리",
            ],
            "infra": [
                "프로젝트 범위 빈도·최근성 계산이 concept_importance와 동일한 0.5**(경과일/30) 가중치 규칙 사용",
            ],
        },
        {
            "version": "v3.4",
            "codename": "Concept Quality Gate",
            "date": "2026-06-02",
            "title": "개념 품질 게이트 — 쓰레기 개념을 ‘저장 전에’ 차단",
            "badge": "P1 완료",
            "problem": [
                "‘여기/최근/제목/회원/내용’ 같은 잡개념이 저장돼 개념 그래프를 오염시킴",
                "‘경찰은/알고리즘에/#딥러닝’처럼 조사·기호가 붙은 표기가 그대로 저장됨",
                "걸러낸 개념을 그냥 버려서 불용어 사전을 개선할 근거가 없었음",
            ],
            "improvement": [
                "저장 단계 입력 게이트 도입: 정규화(조사/어미 제거) → 불용어 → 길이/기호 검사 순",
                "제외된 개념을 폐기하지 않고 excluded_concepts_log에 사유와 함께 기록",
                "개념 허브에 빈도 Top N + 품질 리포트(제외 TOP·사유별 집계) 추가",
                "전부 경량 규칙 기반(API 비용 $0) — konlpy/wordcloud/torch 등 무거운 의존성 미사용",
            ],
            "result": [
                "입력 → 필터 → 저장 안 함 구조로, 병합 탭에서 사후 청소하던 부담 제거",
                "데이터가 쌓일수록 ‘제외 로그 → 불용어 개선’으로 시스템이 좋아지는 구조 확보",
                "정확 일치 불용어라 ‘정보’는 제거하되 ‘정보보안/사회복지’ 합성어는 보존",
            ],
            "features": [
                "clean_concept / filter_concepts — 정규화·불용어·길이 게이트",
                "개념 빈도 랭킹(concept_frequency) + 핵심 개념 Top N 막대",
                "🚫 개념 품질 리포트 — 자주 제외된 개념 TOP + 제외 사유별 건수 + 로그 비우기",
                "concept_importance — 빈도 × 최근성 가중치(프로젝트 맵 엔진)",
            ],
            "fixes": [
                "조사 목록에서 이네/이네는/네 제거 — 가게명·고유명사(진영이네) 과잉 절단 방지",
                "‘진영이네는 → 진영이네’로 보존, ‘경찰은 → 경찰’ 등 회귀 정상 확인",
            ],
            "infra": [
                "excluded_concepts_log 영속화(save/init) — 최대 2000건 순환 보관",
            ],
        },
        {
            "version": "v3.3",
            "codename": "Connected Workspace",
            "date": "2026-06-01",
            "title": "작업↔메모↔개념 연결 + 캘린더 UX + 개념 병합 안전화",
            "badge": "E-5 / 병합",
            "problem": [
                "작업이 메모·개념과 끊겨 있어 ‘이 작업이 무슨 자료·개념과 연결됐는지’ 알 수 없음",
                "월 캘린더가 마크다운 깨짐(**3**)·셀 정렬 흐트러짐으로 가독성이 낮음",
                "개념 자동 병합이 사용자 승인 없이 실행돼 잘못 합쳐질 위험",
            ],
            "improvement": [
                "작업 생성/수정에 🔗 연결 영역(메모·개념 체크박스 선택, 현재 프로젝트 우선) 추가",
                "캘린더를 카드형 셀(오늘 강조·타입별 배지)로 재설계 + HTML로 마크다운 누수 제거",
                "개념 병합을 ‘자동 실행’ → ‘후보 제안 + 등급(🟢🟡🔴) + 사용자 승인’ 워크플로로 전환",
            ],
            "result": [
                "작업 카드·상세에서 연결된 메모/개념이 보여 프로젝트 맵의 기반 데이터 확보",
                "캘린더가 한 달 KPI·타입 배지로 읽기 쉬워짐",
                "병합이 기본 비파괴(완전병합/별칭등록/제외 선택 + 되돌리기)로 안전해짐",
            ],
            "features": [
                "작업 ↔ 메모/개념 연결 (linked_note_ids / linked_concepts)",
                "월 캘린더 카드형 셀 + 월 KPI 요약 + 타입별 클릭 상세",
                "개념 병합 검토 워크플로 — 등급·사유·미리보기·되돌리기(merge_dismissed)",
            ],
            "fixes": [
                "캘린더 **3** 마크다운 누수·셀 높이 정렬·잘못된 날짜 무시 처리",
                "병합 시 메모·작업·관계·엔터티·폴더의 개념 참조를 일괄 갱신",
            ],
            "infra": [
                "create_task에 linked_concepts 필드 추가, merge_dismissed 영속화",
            ],
        },
        {
            "version": "v3.2",
            "codename": "Study Mode",
            "date": "2026-06-01",
            "title": "학습자료 분석 경험 개선 + 지식 OS 전환",
            "badge": "P1 완료",
            "problem": [
                "공부자료도 일반 정보글처럼 ‘신뢰도 중심’으로 처리돼 학습용 메모 품질이 낮음",
                "결과화면이 점수·차트 위주라 ‘정리된 지식’을 얻는 경험이 약함",
                "저장 방식이 모호해 ‘무엇이 저장됐는지’ 사용자가 헷갈림",
            ],
            "improvement": [
                "study 전용 분석 플로우 + 학습노트 템플릿 도입 (이해 중심)",
                "결과화면을 4탭(핵심 요약/신뢰도 판단/개념·태그 후보/다음 행동)으로 재배치, 차트·피드백은 접힘",
                "STEP4 저장 옵션 3분기: 지식 메모로 / 분석결과만 / 둘 다",
                "🛠️ Study Debug로 유형 분기·초안 사용 여부를 항상 노출",
            ],
            "result": [
                "원문 기반 설명 품질 향상 (제목 수준 요약 → 단계별 학습노트)",
                "‘정리 중심’ 화면으로 지식 아카이브 활용도 증가 설계",
                "품질 문제를 ‘추측’이 아니라 ‘확인’으로 디버깅 가능",
            ],
            "features": [
                "STEP3 결과화면 4탭 재구성",
                "STEP4 저장 옵션 3분기",
                "공부자료(study) 전용 AI 학습노트 초안 (한 줄 핵심·왜 필요한가·단계별 동작·핵심 개념·한계·기억법·시험 대비)",
                "🛠️ Study Debug expander",
            ],
            "fixes": [
                "공부자료를 골라도 AI 분류기가 info/unknown으로 덮어써 study 분기가 죽던 버그 수정 (selected_type==study 강제 유지)",
                "CONTENT_TYPE_LABELS·SCORE_KEYS_BY_TYPE에 study 추가",
                "분석 캐시 버전 v3-study로 올려 오염 캐시 무효화",
            ],
            "infra": [
                "메모 저장 구조에 P5 대비 필드 선반영: one_line_summary / concepts / related_note_ids / note_type / last_reviewed_at",
            ],
        },
        {
            "version": "v3.1",
            "codename": "Source Memory",
            "date": "2026-05-31",
            "title": "원문 기억력 강화 + 수집 단계 UX",
            "badge": "P0 안정화",
            "problem": [
                "지식 AI가 원문을 6,000자까지만 읽어 답변이 얕고 맥락이 끊김",
                "수집 과정이 어디까지 진행됐는지 한눈에 안 보임",
            ],
            "improvement": [
                "원문 추출 한도 6,000자 → 20,000자로 확장 (상수화 + 캐시 버전 무효화)",
                "4단계 수집 Stepper로 진행 상황 시각화 (상단 고정·색/진행률 변화)",
            ],
            "result": [
                "원문 활용도 3.3배 → 지식 AI 답변 품질 직접 개선",
                "수집 흐름이 명확해져 ‘저장까지’ 이탈 감소 설계",
            ],
            "features": [
                "4단계 수집 Stepper",
                "STEP2 원문 확인 패널 (추출 상태·길이·출처)",
            ],
            "fixes": [
                "extract_text 6,000자 병목 제거",
                "STEP2 원문 textarea가 비어 보이던 버그 수정 (value/key 충돌 → key 제거)",
            ],
            "infra": [
                "추출/원문 보관 한도 상수화, 캐시 키에 EXTRACTION_VERSION 포함",
            ],
        },
        {
            "version": "v3.0",
            "codename": "Knowledge Graph",
            "date": "2026-05-30",
            "title": "지식 AI(RAG) + 개념 파인더 + 지식맵",
            "badge": "Knowledge OS",
            "problem": [
                "저장한 지식이 흩어져 있어 ‘모은 걸 다시 꺼내 쓰기’가 어려움",
                "개념이 평면 목록이라 구조·연결을 파악하기 힘듦",
            ],
            "improvement": [
                "내 지식을 근거로 답하는 지식 AI(RAG) 도입",
                "개념 파인더(폴더 계층) + 지식맵(5개 서브탭) 시각화",
            ],
            "result": [
                "‘검색’을 넘어 ‘물어보면 정리해 답하는’ 경험 확보",
                "지식 구조를 한눈에 탐색 가능",
            ],
            "features": [
                "지식 AI — 관련 항목 상위 N개 근거 + 출처 배지",
                "개념 파인더 — 연결 문서·메모 수 표시",
                "지식 맵 — 목차/보드/마인드맵/지식페이지/개념 파인더",
            ],
            "fixes": [
                "concept 마이그레이션 시 string.get() AttributeError 수정",
                "모바일 흰 글자 가독성 버그 수정",
            ],
            "infra": [
                "note_concept_links 테이블 도입 (메모-개념 연결)",
            ],
        },
        {
            "version": "v2.x",
            "codename": "Workspace",
            "date": "2026-05-29",
            "title": "프로젝트·작업·태그 워크스페이스",
            "badge": "Workspace",
            "problem": [
                "수집한 지식을 묶고 업무·연구 단위로 운영할 구조가 없음",
            ],
            "improvement": [
                "프로젝트/섹션/단계 구조 + 작업(Task) 관리(보드/캘린더)",
                "태그 관리 + 통합 검색(제목·태그·본문·프로젝트·섹션)",
            ],
            "result": [
                "지식이 ‘낱개 메모’에서 ‘연구/업무 흐름’으로 조직화",
            ],
            "features": [
                "프로젝트/섹션/단계 + 작업(Task)",
                "통합 검색 + 태그 관리",
                "지식 아카이브 — 즐겨찾기·편집·필터",
            ],
            "fixes": [
                "최근 7일 필터를 누적 범위로 수정",
                "CSS double-dot 버그 수정",
            ],
            "infra": [
                "projects / project_sections / project_steps / tasks DB 추가",
            ],
        },
        {
            "version": "v1.x",
            "codename": "Trust Lens",
            "date": "2026-05-29",
            "title": "신뢰도 분석 MVP",
            "badge": "최초 MVP",
            "problem": [
                "정보 과잉 시대, ‘이 정보를 믿어도 될지’ 판단을 도와줄 도구가 필요",
            ],
            "improvement": [
                "URL/텍스트 신뢰도 분석 + 사용자 피드백 루프",
            ],
            "result": [
                "AI 점수와 사람 판단을 비교하는 신뢰 레이어의 출발점 확보",
            ],
            "features": [
                "신뢰도 분석 (점수·광고 위험도·작성자 유형·콘텐츠 유형)",
                "사용자 피드백 + AI vs 사람 비교",
                "분석결과 아카이브 저장",
            ],
            "fixes": [],
            "infra": [
                "Groq API(llama-3.3-70b) 연동 + 로컬 JSON 영속화",
            ],
        },
    ]

    _tab_changelog, _tab_portfolio = st.tabs(["🆕 버전 패치 노트", "💼 포트폴리오"])

    # ─── 패치 노트 탭 ───────────────────────────────────────
    with _tab_changelog:
        # 🎉 사용자 버전 — 무엇이 좋아졌는지 쉬운 말로 먼저
        st.markdown("### 🎉 최근 추가된 기능")
        _user_news = [
            ("📚 지식 라이브러리", "내 모든 기록이 모이는 도서관. 전체·프로젝트별·날짜별·태그별·개념별로 둘러봐요."),
            ("🌍 세계 성장 리포트", "이번 달 내 지식 세계가 얼마나 커지고 연결됐는지 홈에서 한눈에 봐요."),
            ("🛸 자동 항로 발견", "직접 잇지 않아도 같은 개념·태그를 쓰는 프로젝트를 JIUM이 자동으로 연결해줘요."),
            ("🚀 행성 간 지식 이동", "한 프로젝트의 메모·작업·개념을 다른 프로젝트로 보내며 정리해요. 태그·연결 개념도 함께 따라가요."),
            ("🪐 지구 발사대 정리", "아직 어디에도 안 속한 지식을 행성으로 보내요. '전체 발사'로 한 번에도 가능해요."),
            ("🎨 사이드바 그룹 색", "홈·지식·프로젝트·AI·분석·관리를 색으로 구분해 지금 어디 있는지 한눈에 보여요."),
            ("✍️ 홈 한 줄 입력", "앱을 열자마자 오늘 생각을 바로 적을 수 있어요."),
            ("📅 데일리 노트 + 미니 캘린더", "날짜별로 기록하고, 언제 무엇을 적었는지 한눈에 봐요."),
            ("🧭 회상 레일", "뭘 적을지 막힐 때 최근 메모·개념·프로젝트를 떠올려줘요."),
            ("🗺 프로젝트 맵", "프로젝트와 연결된 메모·작업·개념을 한눈에 봐요."),
            ("🔗 개념 별칭 / ⭐ TF-IDF", "같은 개념을 묶고, 이 프로젝트에 특화된 중요 개념을 골라줘요."),
            ("🧠 지식 아카이브 개편", "메모를 카드로 훑고, 관련 메모를 따라가며 탐험해요."),
            ("⚙️ 설정 Control Center", "테마·세계관·기능을 한 곳에서 켜고 꺼요."),
            ("🔎 지식 AI 근거 정리", "질문과 무관한 자료를 빼고, 관련 지식만 근거로 답해요."),
        ]
        _un1, _un2 = st.columns(2)
        for _ui, (_t, _d) in enumerate(_user_news):
            with (_un1 if _ui % 2 == 0 else _un2):
                st.markdown(f"**{_t}**<br><span style='color:#475569;font-size:0.9em'>{_d}</span>",
                            unsafe_allow_html=True)
        st.divider()
        st.markdown("### 🛠 개발 상세 (버전별 패치 노트)")
        st.caption(f"현재 버전: **{_CHANGELOG[0]['version']} {_CHANGELOG[0]['codename']}** · 총 {len(_CHANGELOG)}개 메이저 버전 · 각 버전의 ‘개발 상세’를 펼치면 기능·버그·인프라까지 보여요.")
        for _i, _v in enumerate(_CHANGELOG):
            _is_latest = _i == 0
            _border = "#2563eb" if _is_latest else "#cbd5e1"
            st.markdown(
                f"""<div style="border-left:4px solid {_border}; background:#f8fafc;
     border-radius:8px; padding:14px 18px; margin:10px 0 4px;">
    <span style="font-size:1.25rem; font-weight:800; color:#0f172a;">{_v['version']}</span>
    <span style="font-size:1rem; font-weight:700; color:#2563eb; margin-left:8px;">“{_v['codename']}”</span>
    <span style="background:#1e3a8a; color:#fff; font-size:0.72rem; font-weight:700;
        padding:2px 8px; border-radius:10px; margin-left:8px;">{_v['badge']}</span>
    {"<span style='background:#16a34a;color:#fff;font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:10px;margin-left:6px;'>최신</span>" if _is_latest else ""}
    <span style="color:#64748b; font-size:0.85rem; margin-left:8px;">{_v['date']}</span>
    <div style="font-weight:700; color:#1e293b; margin-top:6px;">{_v['title']}</div>
</div>""",
                unsafe_allow_html=True,
            )
            # 문제 → 개선 → 결과 (PM 관점)
            _pm_col1, _pm_col2, _pm_col3 = st.columns(3)
            with _pm_col1:
                st.markdown("**🔴 문제**")
                for _p in _v.get("problem", []):
                    st.markdown(f"<div style='font-size:0.84rem;color:#475569;'>· {_p}</div>", unsafe_allow_html=True)
            with _pm_col2:
                st.markdown("**🟡 개선**")
                for _p in _v.get("improvement", []):
                    st.markdown(f"<div style='font-size:0.84rem;color:#475569;'>· {_p}</div>", unsafe_allow_html=True)
            with _pm_col3:
                st.markdown("**🟢 결과**")
                for _p in _v.get("result", []):
                    st.markdown(f"<div style='font-size:0.84rem;color:#475569;'>· {_p}</div>", unsafe_allow_html=True)
            with st.expander("🛠️ 개발 상세 (기능 / 버그 수정 / 인프라)", expanded=False):
                if _v["features"]:
                    st.markdown("**✨ 추가/개선 기능**")
                    for _f in _v["features"]:
                        st.markdown(f"- {_f}")
                if _v["fixes"]:
                    st.markdown("**🐞 버그 수정**")
                    for _f in _v["fixes"]:
                        st.markdown(f"- {_f}")
                if _v["infra"]:
                    st.markdown("**🧱 데이터/인프라**")
                    for _f in _v["infra"]:
                        st.markdown(f"- {_f}")
            st.divider()

        st.divider()
        st.markdown("### 🗺️ 다음 로드맵 (예정)")
        _roadmap = [
            ("v4.1", "Side Panel · 좌측 컨텍스트 패널", "최근 메모·최근 개념·최근 프로젝트를 항상 보이게 — 입력·탐색 진입장벽 ↓ (Obsidian 사이드바 느낌)", "진행 예정"),
            ("v4.2", "Knowledge Map IA · 지식맵 단순화", "10개 탭 → 기본 모드(📚 문서·🧠 개념·📁 프로젝트·📈 성장) + 고급 모드(관계형·마인드맵·파인더·AI·타임라인) 토글", "대기"),
            ("v4.3", "Semantic Merge · AI 의미 병합", "신경망/신경망 구조/뉴럴 네트워크를 의미 유사도로 병합 (후보 제안→승인, 별칭 우선)", "대기"),
            ("v4.4", "Philosophy Profile · 사고 인바디", "메모·개념·작업·관계 데이터를 철학 축(안정↔도전·개인↔공동체·감정↔논리·탐색↔실행·이상↔현실)으로 환산해 ‘채연의 사고 구조’를 인바디처럼 리포트. 규칙 기반 MVP, API 불필요", "장기 차별화"),
        ]
        for _p, _t, _d, _s in _roadmap:
            st.markdown(
                f"""<div style="display:flex; gap:12px; align-items:flex-start; background:#fff;
     border:1px solid #e2e8f0; border-radius:8px; padding:10px 14px; margin-bottom:8px;">
    <span style="background:#eff6ff; color:#1d4ed8; font-weight:800; padding:3px 10px;
        border-radius:8px; min-width:52px; text-align:center; white-space:nowrap;">{_p}</span>
    <div><div style="font-weight:700; color:#1e293b;">{_t}
        <span style="color:#94a3b8; font-size:0.78rem; font-weight:600;"> · {_s}</span></div>
        <div style="color:#64748b; font-size:0.86rem; margin-top:2px;">{_d}</div></div>
</div>""",
                unsafe_allow_html=True,
            )

    # ─── 포트폴리오 탭 ──────────────────────────────────────
    with _tab_portfolio:
        st.markdown("""
> 💼 **읽는 분께** — 아래는 채용 담당자·PM 관점에서 이 프로젝트의 문제정의·의사결정·성과를 빠르게 파악할 수 있도록 정리한 요약본이에요.
""")
        st.markdown("### 🛡️ TrustLens — AI 개인 지식 운영체제 (Knowledge OS)")
        st.markdown("""
> *"AI가 대신 생각하지 않는다. 더 나은 판단을 돕는다."*

정보를 **수집 → 신뢰도 분석 → 지식 메모로 정리 → 개념·프로젝트로 연결 → 검색·AI로 재활용**하는 개인 지식 사이클을 한 앱에서 제공하는 서비스. 노션·옵시디언·ChatGPT·퍼플렉시티의 핵심 경험을 ‘판단을 돕는 도구’ 관점으로 재구성했습니다.
""")
        st.divider()
        st.markdown("### 🧭 제품 진화 — 사용자의 ‘다음 불편’을 따라 피벗")
        st.caption("기능을 늘린 게 아니라, 사용자가 다음에 느낄 불편을 예측하며 제품의 정의 자체를 바꿔왔습니다.")
        _evolution = [
            ("v1", "신뢰도 분석기", "“이 정보 믿어도 돼?”", "정보의 신뢰도를 판단", "#94a3b8"),
            ("v2", "지식 저장소", "“모은 정보를 잊어버려”", "분석을 메모로 저장·구조화", "#60a5fa"),
            ("v3", "지식 연결", "“저장한 지식을 다시 활용하고 싶어”", "개념·지식맵·지식 AI로 연결", "#2563eb"),
            ("v4", "Knowledge OS", "“AI와 함께 내 지식을 성장시키고 싶어”", "두 번째 뇌 — 탐험·성장 경험", "#7c3aed"),
        ]
        for _ev, _ename, _epain, _esol, _ecolor in _evolution:
            st.markdown(
                f"""<div style="display:flex; gap:14px; align-items:center; background:#fff;
     border:1px solid #e2e8f0; border-left:4px solid {_ecolor}; border-radius:8px;
     padding:10px 16px; margin-bottom:8px;">
    <span style="font-weight:900; color:{_ecolor}; min-width:34px;">{_ev}</span>
    <div style="flex:1;">
        <div style="font-weight:700; color:#1e293b;">{_ename}
            <span style="color:#475569; font-weight:500; font-size:0.88rem;">— {_esol}</span></div>
        <div style="color:{_ecolor}; font-size:0.9rem; font-style:italic; margin-top:2px;">사용자: {_epain}</div>
    </div>
</div>""",
                unsafe_allow_html=True,
            )
        st.divider()
        st.markdown("### 📊 제품 성숙도 (자체 진단)")
        st.caption("‘AI 분석’은 충분히 성숙했고, 다음 힘은 ‘저장한 지식을 다시 보는 경험’에 실어야 한다고 판단.")
        _maturity = [
            ("수집 (Capture)", 85, "#16a34a"),
            ("저장 (Store)", 80, "#16a34a"),
            ("연결 (Connect)", 60, "#f59e0b"),
            ("탐험·두 번째 뇌 (Explore)", 38, "#ef4444"),
        ]
        for _mname, _mscore, _mcolor in _maturity:
            st.markdown(
                f"""<div style="margin-bottom:8px;">
    <div style="display:flex; justify-content:space-between; font-size:0.88rem; color:#334155;">
        <span style="font-weight:600;">{_mname}</span><span style="font-weight:700; color:{_mcolor};">{_mscore}</span></div>
    <div style="background:#e2e8f0; border-radius:6px; height:9px; margin-top:3px;">
        <div style="width:{_mscore}%; background:{_mcolor}; height:9px; border-radius:6px;"></div></div>
</div>""",
                unsafe_allow_html=True,
            )
        st.info("👉 다음 전략: 입력·탐색 진입장벽부터 — **좌측 패널(v4.1) → 지식맵 단순화(v4.2) → 의미 병합(v4.3) → 사고 인바디(v4.4)**. 기능은 충분하니 ‘진입점’을 다듬어 강점이 묻히지 않게.")
        st.divider()
        st.markdown("### 📈 프로젝트 성장 타임라인")
        st.caption("기능 개수보다 ‘어떻게 진화했는가’ — 신뢰도 분석기에서 지식 OS로 피벗한 과정.")
        _timeline = [
            ("2026-05", "v1.x · Trust Lens", "신뢰도 분석기 MVP", "URL/텍스트 신뢰도 분석 + 사용자 피드백", "#94a3b8", False),
            ("2026-05", "v2.x · Workspace", "지식 저장소로 확장", "메모 저장 + 프로젝트/작업/태그 구조화", "#60a5fa", False),
            ("2026-05", "v3.0 · Knowledge Graph", "지식이 연결되기 시작", "지식 AI(RAG) + 개념 파인더 + 지식맵", "#3b82f6", False),
            ("2026-05", "v3.1 · Source Memory", "원문 기억력 강화", "원문 추출 6K→20K + 수집 Stepper", "#2563eb", False),
            ("2026-06", "v3.2 · Study Mode", "지식 OS로 전환", "학습노트 트랙 + 결과화면 4탭 재설계", "#1d4ed8", False),
            ("2026-06", "v3.3 · Connected Workspace", "지식이 연결되다", "작업↔메모↔개념 연결 + 캘린더 UX + 병합 안전화", "#1d4ed8", False),
            ("2026-06", "v3.4 · Concept Quality Gate", "데이터 품질 확보", "개념 품질 게이트 + 제외 로그/리포트 + 최근성 중요도", "#1e40af", False),
            ("2026-06", "v3.5 · Project Map", "관계가 보이다", "프로젝트 중심 작업·자료·개념·관계맵", "#7c3aed", False),
            ("2026-06", "v3.6 · TF-IDF Highlight", "특화 개념 강조", "흔한 개념 강등·프로젝트 특화 개념 부각", "#6d28d9", False),
            ("2026-06", "v3.7 · Second Brain", "다시 읽는 뇌", "아카이브 카드화 + 노트 상세 + 관련 메모 추천", "#7c3aed", False),
            ("2026-06", "v3.8 · Alias Network", "개념이 묶이다", "역전파↔BackPropagation 비파괴 별칭 → 추천·맵·랭킹 합산", "#6d28d9", False),
            ("2026-06", "v3.9 · Control Center", "발견 가능한 제품", "설정 통합(화면·세계관·루미·기능·실험실) + 사이드바 정리", "#6d28d9", False),
            ("2026-06", "v4.0 · Daily Note", "매일 기록하는 앱", "날짜 기반 빠른 입력 허브 — 입력 진입장벽 ↓", "#0ea5e9", True),
            ("예정", "v4.1 → v4.4", "Knowledge OS 완성", "좌측 패널 → 지식맵 단순화 → 의미 병합 → 사고 인바디", "#7c3aed", False),
        ]
        for _tdate, _tver, _ttitle, _tdesc, _tcolor, _tnow in _timeline:
            _now_badge = "<span style='background:#16a34a;color:#fff;font-size:0.68rem;font-weight:700;padding:1px 7px;border-radius:9px;margin-left:6px;'>현재</span>" if _tnow else ""
            st.markdown(
                f"""<div style="display:flex; gap:14px; align-items:stretch; margin-bottom:2px;">
    <div style="min-width:64px; text-align:right; color:#94a3b8; font-size:0.78rem; padding-top:2px;">{_tdate}</div>
    <div style="display:flex; flex-direction:column; align-items:center;">
        <div style="width:13px; height:13px; border-radius:50%; background:{_tcolor}; border:2px solid #fff; box-shadow:0 0 0 2px {_tcolor};"></div>
        <div style="flex:1; width:2px; background:#e2e8f0; margin:2px 0;"></div>
    </div>
    <div style="padding-bottom:14px;">
        <div style="font-weight:800; color:{_tcolor};">{_tver}{_now_badge}</div>
        <div style="font-weight:700; color:#1e293b; font-size:0.92rem;">{_ttitle}</div>
        <div style="color:#64748b; font-size:0.84rem; margin-top:1px;">{_tdesc}</div>
    </div>
</div>""",
                unsafe_allow_html=True,
            )
        st.divider()
        _pf1, _pf2 = st.columns(2)
        with _pf1:
            st.markdown("""
**🎯 문제 정의**
- 정보 과잉 시대, 사람들은 ‘무엇을 믿을지’와 ‘모은 걸 어떻게 다시 쓸지’ 둘 다 어려움
- 기존 도구는 *저장*은 잘하지만 *판단*과 *재활용*이 약함

**👤 역할 (1인 기획·개발)**
- 제품 기획(PM): 비전 정의·MVP 로드맵·우선순위 의사결정
- 개발: Streamlit 단일 앱 구현 (약 11,600줄)
- AI 엔지니어링: 프롬프트 설계·RAG·유형별 분기
""")
        with _pf2:
            st.markdown("""
**🧰 기술 스택**
- Python · Streamlit
- Groq API (llama-3.3-70b-versatile)
- 로컬 JSON 영속화 (→ 향후 DB)
- GitHub + Streamlit Cloud 자동 배포

**📈 핵심 지표(설계 관점)**
- 원문 활용도 6K→20K자 (3.3배) → 답변 품질 직접 개선
- 결과화면 정보구조 4탭화로 ‘정리 중심’ 전환
""")
        st.divider()
        st.markdown("### 💡 의사결정 하이라이트 (PM 사고 과정)")
        _decisions = [
            ("‘분석기’가 아니라 ‘지식 OS’로 피벗",
             "단발성 신뢰도 검사로는 재방문 동기가 약하다고 판단. ‘수집→정리→재활용’ 사이클을 핵심 가치로 재정의하고, 신뢰도 분석은 *입구*로 강등."),
            ("결과화면을 신뢰도 중심 → AI 정리 중심으로 재배치",
             "사용자가 실제로 원하는 건 점수가 아니라 ‘정리된 지식’. 차트·피드백은 접고, 핵심 요약/다음 행동을 전면 배치 (4탭 구조)."),
            ("공부자료 유형을 별도 트랙으로 분리",
             "신뢰도 프롬프트로 학습 자료를 처리하니 ‘제목 수준 요약’만 나옴. 이해 중심 학습노트 프롬프트를 분리해 사용 맥락별 품질을 확보."),
            ("개념 품질을 다음 핵심 과제로 선정",
             "그래프·별칭·병합을 아무리 잘 만들어도 입력 개념이 오염되면 무의미. ‘개념 품질 게이트’를 P2로 끌어올림 (쓰레기 입력 차단이 우선)."),
            ("미래 기능을 위한 데이터 구조 선투자",
             "UI는 미루되 메모 저장 스키마에 one_line_summary·concepts 등 필드를 미리 심어, 후속 UX 개편 시 마이그레이션 비용을 제거."),
        ]
        for _dt, _dd in _decisions:
            with st.expander(f"🔹 {_dt}"):
                st.markdown(_dd)

        st.divider()
        st.markdown("### 🐞 트러블슈팅 사례 (문제 해결력)")
        st.markdown("""
**사례: 공부자료를 선택해도 학습노트가 안 나오는 문제**

1. **현상** — 역전파 글을 ‘공부자료’로 분석해도 초안이 "신경망과 역전파 알고리즘에 대한 설명입니다" 수준
2. **가설** — study 전용 프롬프트는 추가됐는데 실제로 그 분기를 안 타는 것으로 추정
3. **원인 규명** — `analyze_with_groq`에서 AI 분류기 결과가 사용자 선택을 덮어씀. 그런데 AI 프롬프트의 유형 선택지에 **study 자체가 없어** AI는 절대 study를 못 돌려줌 → 항상 info/unknown으로 강등
4. **해결** — `selected_type=="study"`면 content_type을 study로 강제 유지 (사용자 명시 선택 > AI 추론). 라벨/점수 키 추가 + 캐시 버전 올려 오염 캐시 무효화
5. **재발 방지** — 🛠️ Study Debug expander로 selected/ai/final 유형과 분기 사용 여부를 항상 노출 → ‘추측’이 아니라 ‘확인’ 가능한 구조로 전환
""")
        st.divider()
        st.markdown("### 🌱 배운 점 & 다음")
        st.markdown("""
- **사용자 의도 > 모델 추론**: 명시적 선택은 AI가 덮어쓰지 않게 설계해야 함
- **관측 가능성(Observability)**: 디버그 패널 하나가 반복 추측 비용을 크게 줄임
- **데이터 선투자**: 스키마를 먼저 준비하면 기능 확장이 리팩터링이 아니라 ‘채우기’가 됨
- **다음**: 개념 품질 게이트(P2)로 입력 품질을 끌어올리고, 지식 아카이브를 ‘두 번째 뇌’ 경험으로 발전
""")

    st.stop()


if menu == "최근 검색 기록":
    st.markdown("## 🕘 최근 검색 기록")
    st.caption("같은 URL은 저장된 캐시를 불러와서 API 호출 없이 다시 볼 수 있어요.")

    if st.session_state.get("history_restored"):
        st.success("저장된 분석 결과를 다시 불러왔어요. 왼쪽 메뉴의 📊 분석 결과에서 확인할 수 있어요.")
        st.session_state["history_restored"] = False

    if st.session_state.get("history_restore_failed"):
        st.warning("이 기록의 캐시가 만료됐어요(분석 로직 버전 변경 등). 같은 URL을 다시 분석하면 최신 결과로 볼 수 있어요.")
        st.session_state["history_restore_failed"] = False

    if st.session_state.get("feedback_deleted"):
        st.success("피드백 기록을 삭제했어요.")
        st.session_state["feedback_deleted"] = False

    if st.session_state.get("all_data_cleared"):
        st.success("테스트 저장 데이터를 모두 초기화했어요.")
        st.session_state["all_data_cleared"] = False

    if st.session_state.search_history:
        for idx, item in enumerate(st.session_state.search_history[:20], start=1):
            cache_key = item.get("cache_key") or f'{item.get("url", "")}::{item.get("content_type", "unknown")}'
            st.markdown(
                f'''
                <div class="history-item">
                    <div class="history-title">{idx}. {item.get("title", "제목 없음")}</div>
                    <div class="history-meta">{item.get("time", "")} · {item.get("input_mode", "링크로 조회하기")} · {item.get("content_type", "unknown")} · {item.get("score", 0)}점</div>
                    <div class="history-meta">{display_source_label(item.get("url", ""))}</div>
                </div>
                ''',
                unsafe_allow_html=True,
            )
            st.button(
                "🔁 이 분석 결과 다시 보기",
                key=f"restore_history_{idx}_{cache_key}",
                use_container_width=True,
                on_click=restore_analysis_from_history,
                args=(cache_key,),
            )
    else:
        st.info("아직 검색 기록이 없어요.")

    st.divider()
    st.markdown("## 📩 사용자 피드백 기록")
    if st.session_state.feedback_history:
        for idx, item in enumerate(st.session_state.feedback_history[:20], start=1):
            original_index = idx - 1
            st.markdown(
                f'''
                <div class="history-item">
                    <div class="history-title">{idx}. {item.get("title", "피드백")}</div>
                    <div class="history-meta">{item.get("saved_at", "")} · 만족도 {item.get("rating", "-")} / 5 · {item.get("content_type", "unknown")}</div>
                    <div class="history-meta">도움 된 부분: {", ".join(item.get("useful_points", [])) or "없음"}</div>
                    <div class="history-meta">사용자 판단: {item.get("trust_vote", "판단 보류")} · 이유: {", ".join(item.get("feedback_reasons", [])) or "없음"}</div>
                    <div class="history-meta">보완 요청: {item.get("missing_points", "없음") or "없음"}</div>
                    <div class="history-meta">틀렸거나 어색한 부분: {item.get("wrong_points", "없음") or "없음"}</div>
                </div>
                ''',
                unsafe_allow_html=True,
            )
            st.button(
                "🗑️ 이 피드백 삭제",
                key=f"delete_feedback_{original_index}",
                use_container_width=True,
                on_click=delete_feedback_item,
                args=(original_index,),
            )
    else:
        st.info("아직 저장된 사용자 피드백이 없어요.")

    st.divider()
    st.caption("💾 백업·복원은 **사이드바 ⚙️ 관리 → 데이터 백업·관리**에서 할 수 있어요.")
    st.stop()

# -----------------------------
# 🏠 오늘의 대시보드 (Home) — 홈 전용. 다른 페이지로 새어나가지 않도록 가드.
# 통합 검색·지식 AI 등은 st.stop()을 부르지 않으므로, 여기서 홈이 아니면 종료한다.
# -----------------------------
if menu != "분석 시작하기":
    st.stop()

if st.session_state.get("history_restore_failed"):
    st.warning(
        "이 기록의 분석 캐시가 만료됐어요. 같은 URL을 다시 분석하면 최신 결과로 열려요."
    )
    st.session_state["history_restore_failed"] = False

_dash_today = datetime.now().strftime("%Y-%m-%d")
_dash_projects = st.session_state.get("projects", [])
_dash_tasks = st.session_state.get("tasks", [])
_dash_notes = st.session_state.get("archive_notes", [])
_dash_concepts = st.session_state.get("pkm_custom_concepts", [])

_dash_active_proj = [p for p in _dash_projects if "진행" in str(p.get("status", ""))]
_dash_today_tasks = [t for t in _dash_tasks if str(t.get("due_date", ""))[:10] == _dash_today]
_dash_open_tasks = [t for t in _dash_tasks if str(t.get("status", "")) not in ("완료", "보관됨")]

# ── 🌍 오늘의 세계 (대시보드 히어로) ──
from collections import Counter as _DashCnt
_dash_links = st.session_state.get("note_concept_links", [])
_dash_cc = _DashCnt()
for _l in _dash_links:
    _c = canonical_concept(_l.get("concept")) if _l.get("concept") else None
    if _c:
        _dash_cc[_c] += 1
_dash_ai_conn = sum(1 for _c, _n in _dash_cc.items() if _n >= 2)  # 2개 이상 메모에 연결된 개념 = 발견된 연결

def _wstat(num, label):
    return (
        f"<div style='text-align:center;padding:0 14px;'>"
        f"<div style='font-size:1.7rem;font-weight:900;"
        f"text-shadow:0 1px 3px rgba(0,0,0,0.35);line-height:1.1;'>{num}</div>"
        f"<div class='wlabel' style='font-size:0.78rem;'>{label}</div></div>"
    )

st.markdown(
    f"""
    <style>
    /* Streamlit Cloud 테마가 inline color를 덮어써서, 클래스 특이도로 흰색 강제 */
    .jium-hero, .jium-hero * {{ color:#ffffff !important; }}
    .jium-hero .wlabel {{ color:#dbeafe !important; opacity:0.95; }}
    </style>
    <div class="jium-hero" style="background:linear-gradient(135deg,#1e3a8a,#3b82f6);border-radius:18px;
         padding:22px 28px 20px;margin-bottom:14px;">
      <div style="font-size:1.55rem;font-weight:900;text-shadow:0 1px 3px rgba(0,0,0,0.3);">🌍 오늘의 세계</div>
      <div style="margin:2px 0 16px;font-size:0.9rem;" class="wlabel">{_dash_today} · 메모를 남기면 JIUM이 연결해 드려요</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-start;
           border-top:1px solid rgba(255,255,255,0.18);padding-top:14px;">
        {_wstat(len(_dash_notes), "📝 메모")}
        {_wstat(len(_dash_projects), "📁 프로젝트")}
        {_wstat(len(_dash_open_tasks), "✅ 할 일")}
        {_wstat(len(_dash_concepts), "🧠 개념")}
        {_wstat(_dash_ai_conn, "🔗 발견된 연결")}
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# 대시보드 핵심 행동 버튼 (가장 크게: 새 메모)
_cta1, _cta2, _cta3 = st.columns([2, 1, 1])
with _cta1:
    if st.button("➕ 새 메모 쓰기", type="primary", use_container_width=True, key="dash_new_memo"):
        st.query_params["page"] = "new"
        st.rerun()
with _cta2:
    if st.button("🌍 내 세계 보기", use_container_width=True, key="dash_view_map"):
        st.query_params["page"] = "map"
        st.rerun()
with _cta3:
    if st.button("📚 지식 라이브러리", use_container_width=True, key="dash_view_lib"):
        st.query_params["page"] = "archive"
        st.rerun()
# 새 메모 바로 밑 — 링크·글 가져오기 토글 (성장 리포트처럼 그 자리 열고/닫기, 새로고침 X)
st.markdown(
    "<div style='margin-top:8px;font-weight:700;color:#c2410c;'>🔗 링크·글 가져와서 메모 만들기</div>"
    "<div style='font-size:0.82rem;color:#9a3412;margin-bottom:2px;'>URL이나 글을 AI가 정리해 메모 초안으로 만들어줘요.</div>",
    unsafe_allow_html=True)
st.toggle("열기 / 닫기", key="home_show_import")
st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

# 🧠 루미가 발견한 연결 (대시보드 설명 카드)
if _dash_ai_conn:
    _lumi_top_c, _lumi_top_n = _dash_cc.most_common(1)[0]
    _lumi_note_ids = {_l.get("note_id") for _l in _dash_links
                      if canonical_concept(_l.get("concept")) == _lumi_top_c}
    _lumi_titles = [str(_n.get("title", "")).strip() for _n in _dash_notes
                    if _n.get("id") in _lumi_note_ids and str(_n.get("title", "")).strip()][:3]
    _lumi_titles_html = " · ".join(f"「{_t[:18]}」" for _t in _lumi_titles) or "여러 메모"
    st.markdown(
        f"""
        <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-left:4px solid #8b5cf6;
             border-radius:12px;padding:14px 18px;margin-bottom:12px;">
          <div style="font-weight:800;color:#6d28d9;margin-bottom:4px;">🧠 루미가 발견한 연결</div>
          <div style="color:#334155;font-size:0.92rem;line-height:1.6;">
            <b>‘{_lumi_top_c}’</b> 개념이 <b>{_lumi_top_n}개</b>의 메모에서 반복해서 나타났어요.<br>
            <span style="color:#64748b;font-size:0.85rem;">{_lumi_titles_html}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button(f"🕸️ '{_lumi_top_c}' 연결을 지식맵에서 보기", key="dash_lumi_map", use_container_width=True):
        st.query_params["page"] = "map"
        st.rerun()
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)


# 루미 캐릭터 선택지 (사용자가 고를 수 있는 아바타)
_LUMI_AVATARS = {
    "sparkle": "✨", "compass": "🧭", "star": "⭐",
    "chick": "🐣", "rabbit": "🐰", "fox": "🦊", "owl": "🦉",
}
_LUMI_AVATAR_LABELS = {
    "sparkle": "✨ 빛", "compass": "🧭 나침반", "star": "⭐ 별",
    "chick": "🐣 병아리", "rabbit": "🐰 토끼", "fox": "🦊 여우", "owl": "🦉 부엉이",
}


def get_lumi_theme_tone(theme_key):
    """테마별 루미 어휘/색감 톤. Brain Theme v2와 표현을 맞춘다."""
    tones = {
        "default": {"memo":"메모","concept":"개념","project":"프로젝트","task":"작업",
                    "tint":"#eff6ff","accent":"#3b82f6","emoji":"🧠"},
        "forest":  {"memo":"잎사귀","concept":"가지","project":"줄기","task":"열매",
                    "tint":"#ecfdf5","accent":"#10b981","emoji":"🍃"},
        "space":   {"memo":"행성","concept":"위성","project":"항성","task":"탐사",
                    "tint":"#eef2ff","accent":"#6366f1","emoji":"🛰️"},
        "lab":     {"memo":"자료","concept":"실험","project":"연구실","task":"성과",
                    "tint":"#f0f9ff","accent":"#0ea5e9","emoji":"🧪"},
    }
    return tones.get(theme_key, tones["default"])


def get_lumi_message_candidates(theme_key, counts, recent_title, today_cnt, open_cnt):
    """상태 기반 루미 대사 후보 목록 (테마 어휘로 변환)."""
    _t = get_lumi_theme_tone(theme_key)
    _memos, _concepts, _projects = counts
    _msgs = []
    _total = _memos + _concepts + _projects
    # 빈 상태 / 성장 요약
    if _total == 0:
        _msgs.append(f"아직 세계가 비어 있어요. 첫 {_t['memo']} 하나로 시작해볼까요? {_t['emoji']}")
    else:
        _msgs.append(f"{_t['memo']}이(가) 조금씩 늘고 있어요. 오늘도 하나 더 더해볼까요? {_t['emoji']}")
    # 오늘 할 일 / 진행 중
    if today_cnt:
        _msgs.append(f"오늘 마무리하면 좋은 {_t['task']}이(가) {today_cnt}개 있어요 ✅")
    elif open_cnt:
        _msgs.append(f"진행 중인 {_t['task']}이(가) {open_cnt}개 있어요. 가장 작은 것부터 시작해봐요 💪")
    # 최근 메모 기반 조언
    if recent_title:
        _msgs.append(f"최근 「{recent_title}」을(를) 남기셨네요. {_t['concept']}(으)로 연결하면 더 단단해져요 🔗")
    # 메모 많고 프로젝트 부족
    if _memos >= 5 and _projects == 0:
        _msgs.append(f"{_t['memo']}이(가) 제법 쌓였어요. 비슷한 주제를 하나의 {_t['project']}(으)로 묶어볼까요?")
    # 개념 연결 독려
    if _concepts >= 3:
        _msgs.append(f"{_t['concept']}이(가) 모이고 있어요. 지식맵에서 연결해 세계를 넓혀봐요 🕸️")
    if not _msgs:
        _msgs.append("오늘도 지식 세계에 오신 걸 환영해요 ✨")
    return _msgs


def render_lumi_assistant():
    """✨ 루미(Lumi) — 지식 세계의 안내자. 테마 연동 + 캐릭터 선택."""
    import random as _rnd
    _theme_key = st.session_state.get("brain_theme", "default")
    _tone = get_lumi_theme_tone(_theme_key)
    _notes = st.session_state.get("archive_notes", [])
    _concepts = st.session_state.get("pkm_custom_concepts", [])
    _projects = st.session_state.get("projects", [])
    _tasks = st.session_state.get("tasks", [])
    _today = datetime.now().strftime("%Y-%m-%d")
    _today_cnt = sum(1 for t in _tasks if str(t.get("due_date", ""))[:10] == _today
                     and str(t.get("status", "")) not in ("완료", "보관됨"))
    _open_cnt = sum(1 for t in _tasks if str(t.get("status", "")) not in ("완료", "보관됨"))
    _recent_title = ""
    if _notes:
        _recent_title = str(_notes[-1].get("title", "")).strip()[:20]

    _msgs = get_lumi_message_candidates(
        _theme_key, (len(_notes), len(_concepts), len(_projects)),
        _recent_title, _today_cnt, _open_cnt,
    )
    _pick = _rnd.choice(_msgs)

    _avatar_key = st.session_state.get("lumi_avatar", "sparkle")
    if _avatar_key not in _LUMI_AVATARS:
        _avatar_key = "sparkle"
    _avatar = _LUMI_AVATARS[_avatar_key]

    _c_card, _c_sel = st.columns([7, 1.3])
    with _c_card:
        st.markdown(
            f"""
            <div style="display:flex;align-items:center;gap:14px;background:{_tone['tint']};
                 border:1px solid #e2e8f0;border-left:5px solid {_tone['accent']};border-radius:14px;
                 padding:12px 18px;margin-bottom:6px;box-shadow:0 2px 8px rgba(0,0,0,0.04);">
              <div style="font-size:2rem;line-height:1;">{_avatar}</div>
              <div>
                <div style="font-weight:800;color:{_tone['accent']};font-size:0.85rem;letter-spacing:0.3px;">✨ 루미의 한마디</div>
                <div style="color:#334155;font-size:0.95rem;margin-top:1px;">{_pick}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with _c_sel:
        _keys = list(_LUMI_AVATARS.keys())
        _sel = st.selectbox(
            "루미 모습", _keys,
            index=_keys.index(_avatar_key),
            format_func=lambda k: _LUMI_AVATAR_LABELS[k],
            key="lumi_avatar_select", label_visibility="collapsed",
        )
        if _sel != _avatar_key:
            st.session_state["lumi_avatar"] = _sel
            save_persisted_data(); st.rerun()


render_lumi_assistant()

# ─────────────────────────────────────────────
# 🌳 내 지식 세계 (Brain Level + 지식 나무) — 게임화 성장 카드
# 기존 데이터(메모·개념·프로젝트·관계·작업)에서 계산. 추가 저장 불필요.
# ─────────────────────────────────────────────
_brain_memos    = len(st.session_state.get("archive_notes", []))
_brain_concepts = len(st.session_state.get("pkm_custom_concepts", []))
_brain_projects = len(st.session_state.get("projects", []))
_brain_relations = len(st.session_state.get("relations", [])) + len(st.session_state.get("note_concept_links", []))
_brain_tasks    = len(st.session_state.get("tasks", []))

# 가중 점수 (Brain Point)
_brain_points = (_brain_memos * 3 + _brain_concepts * 5 + _brain_projects * 8
                 + _brain_relations * 2 + _brain_tasks * 1)

# ── 테마 정의(_BRAIN_THEMES)와 get_brain_theme_config는 설정 페이지보다 먼저 쓰여서
#    파일 상단(APP_SETTINGS_DEFAULTS 부근)으로 이동했습니다. ──

# 5요소 성장 기준 (메모·개념·프로젝트·관계·작업 순서 — elements 순서와 동일)
_ELEMENT_THRESHOLDS = [
    [10, 50, 100, 300],   # 메모
    [10, 30, 50, 100],    # 개념
    [3, 10, 30],          # 프로젝트
    [10, 50, 100],        # 관계
    [10, 50, 100],        # 작업(완료)
]


# get_brain_theme_config는 상단으로 이동 (설정 페이지에서 먼저 사용)


# 5요소의 '기준 이름'(메모·개념·프로젝트·관계·작업) — default 테마 elements 순서와 동일.
# 테마로 이름이 바뀌어도(잎사귀/행성 등) 신규 사용자가 의미를 알 수 있게 괄호로 병기한다.
_BASE_ELEMENT_NAMES = [_n for _e, _n in _BRAIN_THEMES["default"]["elements"]]


def element_label_with_base(theme_cfg, idx, themed_name):
    """테마 이름 옆에 기준 이름을 괄호로 붙인다. 기본 테마면 그대로 둔다.
    예) forest → '잎사귀 (메모)', default → '메모'."""
    _base = _BASE_ELEMENT_NAMES[idx] if idx < len(_BASE_ELEMENT_NAMES) else ""
    if _base and themed_name != _base:
        return f"{themed_name} ({_base})"
    return themed_name


def get_home_theme_labels(theme_key):
    """홈 대시보드 메트릭/리스트 제목·아이콘을 테마별로 반환. 없으면 기본."""
    _maps = {
        "default": {
            "active_projects":"📁 진행중 프로젝트","today_tasks":"📅 오늘 작업",
            "open_tasks":"✅ 미완료 작업","total_memos":"📄 전체 메모",
            "recent_projects":"📁 진행중 프로젝트","recent_tasks":"✅ 최근 작업",
            "recent_memos":"📄 최근 메모","recent_research":"🔬 최근 연구노트",
            "recent_concepts":"🧠 최근 개념",
            "proj_icon":"📁","task_icon":"☐","memo_icon":"📄","research_icon":"🔬","concept_icon":"🧠",
        },
        "forest": {
            "active_projects":"🪵 성장 중인 줄기","today_tasks":"🍎 오늘 맺을 열매",
            "open_tasks":"🍏 아직 익지 않은 열매","total_memos":"🍃 전체 잎사귀",
            "recent_projects":"🪵 성장 중인 줄기","recent_tasks":"🍎 최근 열매",
            "recent_memos":"🍃 최근 잎사귀","recent_research":"🌿 최근 연구 가지",
            "recent_concepts":"🌿 최근 가지",
            "proj_icon":"🪵","task_icon":"🍎","memo_icon":"🍃","research_icon":"🌿","concept_icon":"🌿",
        },
        "space": {
            "active_projects":"☀️ 활성 항성","today_tasks":"🚀 오늘의 탐사",
            "open_tasks":"🛰️ 미완료 탐사","total_memos":"🌍 발견한 행성",
            "recent_projects":"☀️ 활성 항성","recent_tasks":"🚀 최근 탐사",
            "recent_memos":"🌍 최근 행성","recent_research":"🛰️ 최근 연구 위성",
            "recent_concepts":"🛰️ 최근 위성",
            "proj_icon":"☀️","task_icon":"🚀","memo_icon":"🌍","research_icon":"🛰️","concept_icon":"🛰️",
        },
        "lab": {
            "active_projects":"🔬 운영 중인 연구실","today_tasks":"🏆 오늘 성과",
            "open_tasks":"📋 미완료 실험","total_memos":"📄 전체 자료",
            "recent_projects":"🔬 운영 중인 연구실","recent_tasks":"🏆 최근 성과",
            "recent_memos":"📄 최근 자료","recent_research":"🧪 최근 연구 실험",
            "recent_concepts":"🧪 최근 실험",
            "proj_icon":"🔬","task_icon":"🏆","memo_icon":"📄","research_icon":"🧪","concept_icon":"🧪",
        },
    }
    _result = _maps.get(theme_key, _maps["default"])
    # 기본 테마가 아니면 최근 활동/요약 제목에도 기준명을 괄호로 병기한다.
    # (메인 대시보드 메트릭과 동일하게 '행성 (메모)' 형태로 통일 → 신규 사용자가 세계관 용어를 잊지 않게)
    if theme_key != "default":
        _base_terms = {
            "active_projects": "프로젝트", "today_tasks": "작업", "open_tasks": "작업",
            "total_memos": "메모", "recent_projects": "프로젝트", "recent_tasks": "작업",
            "recent_memos": "메모", "recent_research": "연구노트", "recent_concepts": "개념",
        }
        _result = dict(_result)
        for _k, _base in _base_terms.items():
            _val = _result.get(_k, "")
            if _val and f"({_base})" not in _val:
                _result[_k] = f"{_val} ({_base})"
    return _result


def get_brain_element_stats(counts):
    """각 요소의 현재 수량 → (현재, 다음기준, 진행률%, 상태문구) 리스트 반환."""
    stats = []
    for _idx, _cnt in enumerate(counts):
        _thrs = _ELEMENT_THRESHOLDS[_idx] if _idx < len(_ELEMENT_THRESHOLDS) else [10]
        _next = None
        for _t in _thrs:
            if _cnt < _t:
                _next = _t
                break
        if _next is None:
            # 모든 기준 통과 → 만렙
            stats.append({"cur": _cnt, "next": _thrs[-1], "pct": 100, "status": "풍성", "maxed": True})
            continue
        _pct = int(round((_cnt / _next) * 100)) if _next else 0
        _pct = max(0, min(100, _pct))
        if _cnt == 0:
            _status = "아직 시작 전"
        elif _pct < 30:
            _status = "부족"
        elif _pct < 70:
            _status = "성장 중"
        else:
            _status = "활발"
        stats.append({"cur": _cnt, "next": _next, "pct": _pct, "status": _status, "maxed": False})
    return stats


def render_brain_element_gauges(theme_cfg, counts):
    """🌱 요소 성장도 — 요소별 progress bar."""
    _stats = get_brain_element_stats(counts)
    st.markdown("<div style='margin:10px 0 4px 0;font-weight:800;'>🌱 요소 성장도</div>", unsafe_allow_html=True)
    _g_cols = st.columns(len(theme_cfg["elements"]))
    for _i, (_emo, _lbl) in enumerate(theme_cfg["elements"]):
        _s = _stats[_i]
        with _g_cols[_i]:
            _lbl_b = element_label_with_base(theme_cfg, _i, _lbl)
            _label = f"{_emo} {_lbl_b} {_s['cur']}/{_s['next']}" + ("✨" if _s["maxed"] else "")
            st.progress(_s["pct"] / 100, text=_label)


def render_brain_world_status(theme_cfg, counts):
    """현재 세계 상태 — 요소별 상태 문구 칩."""
    _stats = get_brain_element_stats(counts)
    _status_color = {
        "아직 시작 전": ("#f1f5f9", "#94a3b8"),
        "부족": ("#fee2e2", "#dc2626"),
        "성장 중": ("#fef3c7", "#d97706"),
        "활발": ("#dbeafe", "#2563eb"),
        "풍성": ("#dcfce7", "#16a34a"),
    }
    st.markdown(
        f"<div style='margin:12px 0 4px 0;font-weight:800;'>{theme_cfg['name'].split(' ')[0]} 현재 세계 상태</div>",
        unsafe_allow_html=True,
    )
    _chips = []
    for _i, (_emo, _lbl) in enumerate(theme_cfg["elements"]):
        _bg, _fg = _status_color.get(_stats[_i]["status"], ("#f1f5f9", "#64748b"))
        _lbl_b = element_label_with_base(theme_cfg, _i, _lbl)
        _chips.append(
            f'<span style="display:inline-block;margin:3px 8px 3px 0;padding:5px 13px;'
            f'border-radius:14px;font-size:13px;font-weight:700;color:{_fg};background:{_bg};">'
            f'{_emo} {_lbl_b} · {_stats[_i]["status"]}</span>'
        )
    st.markdown(f"<div>{''.join(_chips)}</div>", unsafe_allow_html=True)


_brain_theme_key = st.session_state.get("brain_theme", "default")
if _brain_theme_key not in _BRAIN_THEMES:
    _brain_theme_key = "default"
_THM = get_brain_theme_config(_brain_theme_key)
_BRAIN_LEVELS = _THM["levels"]

_cur_lv_idx = 0
for _i, (_thr, _nm, _em) in enumerate(_BRAIN_LEVELS):
    if _brain_points >= _thr:
        _cur_lv_idx = _i
_cur_thr, _cur_name, _cur_emoji = _BRAIN_LEVELS[_cur_lv_idx]
_brain_level = _cur_lv_idx + 1
if _cur_lv_idx < len(_BRAIN_LEVELS) - 1:
    _next_thr = _BRAIN_LEVELS[_cur_lv_idx + 1][0]
    _next_name = _BRAIN_LEVELS[_cur_lv_idx + 1][1]
    _span = _next_thr - _cur_thr
    _grow_pct = int(min(100, max(0, (_brain_points - _cur_thr) / _span * 100))) if _span else 100
    _to_next = max(0, _next_thr - _brain_points)
else:
    _next_name = "최고 레벨"
    _grow_pct = 100
    _to_next = 0

# ═══════════════════════════════════════════════
# 🎉 Brain Growth Reward v1 — 레벨업 감지 + 마일스톤 해금
# rerun 루프 방지: 상태만 저장, st.rerun() 호출하지 않음
# ═══════════════════════════════════════════════
_done_tasks = sum(1 for t in st.session_state.get("tasks", []) if str(t.get("status", "")) == "완료")

# 마일스톤 정의: id -> (지표값, 임계, 해금명, 이모지)
_MILESTONES = [
    ("memo_10",     _brain_memos,     10,  "첫 책장",        "📚"),
    ("memo_50",     _brain_memos,     50,  "지식 나무",      "🌳"),
    ("memo_100",    _brain_memos,    100,  "벚꽃 정원",      "🌸"),
    ("memo_300",    _brain_memos,    300,  "지식의 숲",      "🌲"),
    ("concept_10",  _brain_concepts,  10,  "아이디어 노트",  "💡"),
    ("concept_30",  _brain_concepts,  30,  "아이디어 칠판",  "🧠"),
    ("concept_50",  _brain_concepts,  50,  "개념 도서관",    "📖"),
    ("concept_100", _brain_concepts, 100,  "지혜의 탑",      "🗼"),
    ("relation_10", _brain_relations, 10,  "첫 연결고리",    "🔗"),
    ("relation_50", _brain_relations, 50,  "뿌리 네트워크",  "🕸️"),
    ("relation_100",_brain_relations,100,  "지식 성운",      "🌌"),
    ("project_3",   _brain_projects,   3,  "작업실",        "🛠️"),
    ("project_10",  _brain_projects,  10,  "연구동",        "🏢"),
    ("project_30",  _brain_projects,  30,  "연구 캠퍼스",    "🏛️"),
    ("task_done_10",_done_tasks,      10,  "성실 배지",      "✅"),
    ("task_done_50",_done_tasks,      50,  "생산성 배지",    "⚡"),
    ("task_done_100",_done_tasks,    100,  "마스터 배지",    "🏅"),
]
_REWARD_NAME = {m[0]: (m[3], m[4]) for m in _MILESTONES}  # id -> (name, emoji)

_growth = st.session_state.setdefault("brain_growth_state",
    {"last_level": 1, "unlocked_rewards": [], "last_checked_at": ""})
_growth.setdefault("last_level", 1)
_growth.setdefault("unlocked_rewards", [])
_unlocked = _growth["unlocked_rewards"]
_dirty = False

# 1) 레벨업 감지
_prev_level = _growth.get("last_level", 1)
if _brain_level > _prev_level:
    _growth["last_level"] = _brain_level
    _dirty = True
    st.balloons()
    _flash(f"Brain Level Up! Lv.{_brain_level} «{_cur_name}» 달성 {_cur_emoji}", "🎉")
elif _brain_level < _prev_level:
    _growth["last_level"] = _brain_level  # 동기화만
    _dirty = True

# 2) 마일스톤 해금 감지
_newly = []
for _mid, _val, _thr2, _rname, _remoji in _MILESTONES:
    if _val >= _thr2 and _mid not in _unlocked:
        _unlocked.append(_mid)
        _newly.append((_rname, _remoji))
        _dirty = True
for _rname, _remoji in _newly:
    _flash(f"{_remoji} '{_rname}' 해금!", "🏆")

if _dirty:
    from datetime import datetime as _dtg
    _growth["last_checked_at"] = _dtg.now().strftime("%Y-%m-%d %H:%M")
    st.session_state["brain_last_level"] = _brain_level
    save_persisted_data()  # rerun 없이 상태만 저장

# 성장 단계 이모지 (메모 수)
_tree = _THM["stages"][-1][1]
for _lim, _emo in _THM["stages"]:
    if _brain_memos < _lim:
        _tree = _emo
        break

# 테마 선택 + 카드
_th_c1, _th_c2 = st.columns([3, 1.2])
with _th_c2:
    _theme_labels = {k: v["name"] for k, v in _BRAIN_THEMES.items()}
    _theme_keys = list(_BRAIN_THEMES.keys())
    _sel_theme = st.selectbox(
        "🎨 성장 테마", _theme_keys,
        index=_theme_keys.index(_brain_theme_key),
        format_func=lambda k: _theme_labels[k],
        key="brain_theme_select", label_visibility="collapsed",
    )
    if _sel_theme != _brain_theme_key:
        st.session_state["brain_theme"] = _sel_theme
        save_persisted_data(); _flash(f"테마를 «{_theme_labels[_sel_theme]}»로 바꿨어요.", "🎨"); st.rerun()

# ── ✍️ 오늘 한 줄 — 홈 최상단 빠른 입력 (열자마자 기록, Daily Note 자동 생성) ──
from datetime import date as _qc_date_cls
_qc_today = _qc_date_cls.today().strftime("%Y-%m-%d")
with st.container(border=True):
    _qc_c1, _qc_c2 = st.columns([5, 1])
    with _qc_c1:
        _qc_text = st.text_input(
            "오늘 한 줄", key="home_quick_capture", label_visibility="collapsed",
            placeholder="✍️ 오늘 뭐 했어? — 예: 역전파 공부했다 / 팀플 회의 정리 (Enter 후 저장)")
    with _qc_c2:
        _qc_save = st.button("저장", key="home_quick_save", type="primary", use_container_width=True)
    if _qc_save and _qc_text.strip():
        _qc_concepts = extract_local_concepts(_qc_text, ["데일리노트", _qc_today], limit=8)
        _qcm = create_memo(f"{_qc_today} 데일리 노트", note=_qc_text.strip(),
                           project="기본 프로젝트", section="데일리노트",
                           tags=["데일리노트", _qc_today], concepts=_qc_concepts)
        _qcm["saved_at"] = f"{_qc_today} {datetime.now().strftime('%H:%M')}"
        _qcm["note_type"] = "daily_note"
        _qcm["one_line_summary"] = _qc_text.strip()[:120]
        save_persisted_data()
        st.session_state.pop("home_quick_capture", None)
        _flash(f"오늘 노트에 저장했어요! 개념 {len(_qcm.get('concepts', []))}개 자동 연결. 📅 데일리 노트에서 이어 쓸 수 있어요.")
        st.rerun()
    elif _qc_save:
        st.warning("한 줄 적어주세요.")

st.markdown(
    f"""
    <div style="background:linear-gradient(135deg,#eef2ff,#f5f3ff);
         border:1px solid #c7d2fe;border-left:6px solid #6366f1;
         border-radius:18px;padding:20px 26px;margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
        <div>
          <div style="font-size:0.85rem;letter-spacing:1px;color:#6366f1;">🧠 내 지식 세계 · {_THM['name']}</div>
          <div style="font-size:1.7rem;font-weight:900;margin-top:2px;color:#1e293b;">
            Lv.{_brain_level} {_cur_name} {_cur_emoji}
          </div>
          <div style="font-size:0.9rem;margin-top:2px;color:#4f46e5;">
            Brain Point {_brain_points:,} · 다음 «{_next_name}»까지 {_to_next:,}P
          </div>
        </div>
        <div style="font-size:2.6rem;line-height:1;">{_tree}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)
_bg1, _bg2 = st.columns([4, 1])
with _bg1:
    st.progress(_grow_pct / 100, text=f"성장도 {_grow_pct}%")
with _bg2:
    st.markdown(f"<div style='text-align:right;font-weight:800;color:{_THM['accent']};font-size:1.1rem;'>{_grow_pct}%</div>", unsafe_allow_html=True)
_brain_counts = [_brain_memos, _brain_concepts, _brain_projects, _brain_relations, _brain_tasks]
_bcols = st.columns(5)
for _bi, (_emo, _lbl) in enumerate(_THM["elements"]):
    _bcols[_bi].markdown(
        f"<div style='text-align:center;'>{_emo}<br><b>{_brain_counts[_bi]}</b><br>"
        f"<span style='color:#64748b;font-size:12px;'>{element_label_with_base(_THM, _bi, _lbl)}</span></div>",
        unsafe_allow_html=True)

# ── 🌱 요소별 성장 게이지 + 현재 세계 상태 (작업은 완료 기준) ──
_element_counts = [_brain_memos, _brain_concepts, _brain_projects, _brain_relations, _done_tasks]
render_brain_element_gauges(_THM, _element_counts)
render_brain_world_status(_THM, _element_counts)

# ── 🏆 업적/해금 배지 ──
_total_ms = len(_MILESTONES)
_got_ms = len(_unlocked)
_rc1, _rc2 = st.columns([3, 1])
with _rc1:
    st.markdown(f"##### 🏆 업적 {_got_ms} / {_total_ms}")
    if _unlocked:
        # 최근 해금 3개 (목록 뒤쪽이 최신)
        _recent = _unlocked[-3:][::-1]
        _badge_html = "".join(
            f'<span style="display:inline-block;margin:2px 6px 2px 0;padding:4px 12px;'
            f'border-radius:14px;font-size:13px;font-weight:700;color:#92400e;'
            f'background:#fef3c7;border:1px solid #fcd34d;">'
            f'{_REWARD_NAME.get(_rid,("?","🏆"))[1]} {_REWARD_NAME.get(_rid,("?","🏆"))[0]}</span>'
            for _rid in _recent
        )
        st.markdown(f"<div>{_badge_html}</div>", unsafe_allow_html=True)
    else:
        st.caption("아직 해금한 업적이 없어요. 메모를 쌓으면 첫 업적이 열려요!")
with _rc2:
    # 다음 해금까지 남은 조건 1개 (가장 가까운 미해금)
    _next_ms = None
    for _mid, _val, _thr2, _rname, _remoji in _MILESTONES:
        if _mid not in _unlocked:
            _next_ms = (_rname, _remoji, max(0, _thr2 - _val))
            break
    if _next_ms:
        st.markdown(
            f"<div style='text-align:right;color:#64748b;font-size:13px;'>다음 해금</div>"
            f"<div style='text-align:right;font-weight:800;'>{_next_ms[1]} {_next_ms[0]}</div>"
            f"<div style='text-align:right;color:#10b981;font-size:12px;'>{_next_ms[2]} 남음</div>",
            unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align:right;font-weight:800;color:#f59e0b;'>🎉 전부 해금!</div>", unsafe_allow_html=True)

with st.expander("📖 성장 기준 보기 (레벨 · 업적)"):
    st.markdown(f"""
**🧠 Brain Point 계산:** 메모×3 + 개념×5 + 프로젝트×8 + 관계×2 + 작업×1
*(관계 = 직접 만든 관계 + 메모-개념 자동 연결)*

**📈 레벨 ({_THM['name']} 기준):**
""")
    _lv_rows = " · ".join(f"Lv.{_i+1} {_nm} {_em} ({_thr:,}P)" for _i,(_thr,_nm,_em) in enumerate(_BRAIN_LEVELS))
    st.markdown(_lv_rows)
    st.markdown("**🏆 업적 목록:**")
    import pandas as _pd_ms
    _ms_df = _pd_ms.DataFrame([
        {"업적": f"{_remoji} {_rname}", "조건": f"{_mid.rsplit('_',1)[0]} {_thr2}개",
         "상태": "✅ 해금" if _mid in _unlocked else f"🔒 {max(0,_thr2-_val)} 남음"}
        for _mid, _val, _thr2, _rname, _remoji in _MILESTONES
    ])
    st.dataframe(_ms_df, use_container_width=True, hide_index=True, height=300)

# ── 🏠 내 지식 공간 (업적 진열) — 홈에선 토글 한 줄, 펼치면 전체 ──
_space_open = st.toggle(f"🏠 내 지식 공간 · 업적 {_got_ms}/{_total_ms} 진열 (펼치기)",
                        value=False, key="home_show_space")
if _space_open:
    st.caption(f"업적을 해금하면 공간에 아이템이 하나씩 채워져요. ({_got_ms}/{_total_ms} 진열됨)")
    _item_html = []
    for _mid, _val, _thr2, _rname, _remoji in _MILESTONES:
        _is_open = _mid in _unlocked
        if _is_open:
            _item_html.append(
                f'<div style="flex:0 0 auto;text-align:center;width:92px;padding:12px 6px;'
                f'border-radius:14px;background:{_THM["gradient"]};color:#fff;'
                f'box-shadow:0 4px 12px {_THM["shadow"]};">'
                f'<div style="font-size:1.9rem;line-height:1.1;">{_remoji}</div>'
                f'<div style="font-size:12px;font-weight:700;margin-top:4px;">{_rname}</div>'
                f'</div>'
            )
        else:
            _rem = max(0, _thr2 - _val)
            _item_html.append(
                f'<div style="flex:0 0 auto;text-align:center;width:92px;padding:12px 6px;'
                f'border-radius:14px;background:#f1f5f9;border:1px dashed #cbd5e1;'
                f'color:#94a3b8;opacity:0.65;">'
                f'<div style="font-size:1.9rem;line-height:1.1;filter:grayscale(1);">🔒</div>'
                f'<div style="font-size:12px;font-weight:700;margin-top:4px;">{_rname}</div>'
                f'<div style="font-size:10px;margin-top:2px;">{_rem} 남음</div>'
                f'</div>'
            )
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:6px 0 4px 0;">'
        f'{"".join(_item_html)}</div>',
        unsafe_allow_html=True,
    )

st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)


def build_home_mini_graph_data(max_nodes=30, max_edges=40):
    """홈 미니 뇌지도용 경량 데이터. 진행중 프로젝트·최근 메모·상위 개념·상위 태그."""
    _notes = st.session_state.get("archive_notes", [])
    _projects = st.session_state.get("projects", [])
    _concepts = st.session_state.get("pkm_custom_concepts", [])

    _active = [p for p in _projects if "진행" in str(p.get("status", ""))][:2]
    if not _active:
        _active = _projects[:2]
    _recent_notes = sorted(_notes, key=lambda n: str(n.get("saved_at", "")), reverse=True)[:5]

    # 상위 태그
    _tag_cnt = {}
    for _n in _notes:
        for _t in _n.get("tags", []):
            _c = str(_t).replace("#", "").strip()
            if _c:
                _tag_cnt[_c] = _tag_cnt.get(_c, 0) + 1
    _top_tags = sorted(_tag_cnt.items(), key=lambda x: x[1], reverse=True)[:8]

    # 상위 개념 (이름 기준)
    _con_names = []
    for _c in _concepts:
        _cn = (_c.get("name") if isinstance(_c, dict) else str(_c)) or ""
        _cn = _cn.strip()
        if _cn:
            _con_names.append(_cn)
    _top_concepts = _con_names[:8]

    return {
        "projects": [p.get("name", "") for p in _active if p.get("name")],
        "notes": [n.get("title", "제목 없음") for n in _recent_notes],
        "concepts": _top_concepts,
        "tags": [t for t, _ in _top_tags],
    }


def render_home_mini_knowledge_graph(theme_key):
    """🧠 내 지식 뇌지도 — 홈용 미니 마인드맵 카드."""
    import math as _math
    _cfg = get_brain_theme_config(theme_key)
    _data = build_home_mini_graph_data()
    _elem = _cfg["elements"]  # [(emoji,label)] memo,concept,project,relation,task
    _memo_emo, _concept_emo, _proj_emo = _elem[0][0], _elem[1][0], _elem[2][0]

    _c_title, _c_btn = st.columns([3, 1.2])
    with _c_title:
        st.markdown(
            f"<div style='font-weight:800;font-size:1.05rem;'>🧠 내 지식 뇌지도</div>"
            f"<div style='color:#64748b;font-size:13px;'>최근 연결된 프로젝트·메모·개념·태그를 미니맵으로 보여줘요.</div>",
            unsafe_allow_html=True,
        )
    with _c_btn:
        if st.button("전체 지식맵으로 보기", key="home_goto_map", use_container_width=True):
            st.query_params["page"] = "map"
            st.rerun()

    _total = len(_data["projects"]) + len(_data["notes"]) + len(_data["concepts"]) + len(_data["tags"])
    if _total < 2:
        st.info("아직 연결된 지식이 적어요. 메모를 저장하고 개념을 연결하면 이곳에 뇌지도가 자라나요 🌱")
        return

    try:
        import plotly.graph_objects as _go
    except Exception:
        st.caption("그래프 라이브러리를 불러올 수 없어요.")
        return

    node_x, node_y, node_text, node_size, node_color, node_hover = [], [], [], [], [], []
    edge_x, edge_y = [], []
    _edges = 0
    _MAX_EDGES = 40

    # 중심 노드: 현재 세계
    node_x.append(0); node_y.append(0)
    node_text.append(_cfg["name"]); node_size.append(34)
    node_color.append(_cfg["accent"]); node_hover.append("내 지식 세계")

    # 1차 링: 프로젝트(큰) + 개념(중)
    _ring1 = [("proj", p) for p in _data["projects"]] + [("con", c) for c in _data["concepts"]]
    _n1 = max(len(_ring1), 1)
    _proj_pos = {}
    for _i, (_kind, _name) in enumerate(_ring1):
        _ang = 2 * _math.pi * _i / _n1
        _x = _math.cos(_ang) * 2.6
        _y = _math.sin(_ang) * 2.6
        if _edges < _MAX_EDGES:
            edge_x += [0, _x, None]; edge_y += [0, _y, None]; _edges += 1
        node_x.append(_x); node_y.append(_y)
        if _kind == "proj":
            node_text.append(f"{_proj_emo} {_name}"); node_size.append(22); node_color.append("#2563eb")
            node_hover.append(f"프로젝트: {_name}")
            _proj_pos[_name] = (_x, _y)
        else:
            node_text.append(f"{_concept_emo} {_name}"); node_size.append(15); node_color.append("#8b5cf6")
            node_hover.append(f"개념: {_name}")

    # 2차 링: 태그(작은) + 최근 메모(잎/행성/자료)
    _ring2 = [("tag", t) for t in _data["tags"]] + [("memo", m) for m in _data["notes"]]
    _n2 = max(len(_ring2), 1)
    for _i, (_kind, _name) in enumerate(_ring2):
        _ang = 2 * _math.pi * _i / _n2 + 0.3
        _x = _math.cos(_ang) * 4.4
        _y = _math.sin(_ang) * 4.4
        if _edges < _MAX_EDGES:
            edge_x += [0, _x, None]; edge_y += [0, _y, None]; _edges += 1
        node_x.append(_x); node_y.append(_y)
        if _kind == "tag":
            node_text.append(f"#{_name}"); node_size.append(11); node_color.append("rgba(37,99,235,0.5)")
            node_hover.append(f"태그: #{_name}")
        else:
            _short = _name[:14] + ("…" if len(_name) > 14 else "")
            node_text.append(f"{_memo_emo} {_short}"); node_size.append(12); node_color.append("#10b981")
            node_hover.append(f"메모: {_name}")

    _edge_trace = _go.Scatter(x=edge_x, y=edge_y, mode="lines",
                              line=dict(width=0.8, color="rgba(148,163,184,0.5)"), hoverinfo="none")
    _node_trace = _go.Scatter(
        x=node_x, y=node_y, mode="markers+text", text=node_text, textposition="top center",
        textfont=dict(size=10), hovertext=node_hover, hoverinfo="text",
        marker=dict(size=node_size, color=node_color, line=dict(width=1, color="white")),
    )
    _fig = _go.Figure(data=[_edge_trace, _node_trace])
    _fig.update_layout(
        showlegend=False, margin=dict(l=0, r=0, t=0, b=0), height=360,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(_fig, use_container_width=True, config={"displayModeBar": False})


# ── 스크롤 복원 (JS 앵커) — 버튼 클릭 후 맨 위로 튀지 않게. fragment 미사용, rerun 유지 ──
def scroll_anchor(_name):
    """이 위치에 보이지 않는 앵커를 심어둠. request_scroll(_name) 후 여기로 되돌아옴."""
    st.markdown(f"<span id='tl-anchor-{_name}'></span>", unsafe_allow_html=True)

def request_scroll(_name):
    """다음 rerun 후 tl-anchor-{_name} 위치로 스크롤 복원을 예약."""
    st.session_state["_scroll_to"] = _name

def apply_scroll_restore():
    """예약된 스크롤이 있으면 JS로 해당 앵커로 이동(부모 문서).
    Streamlit이 rerun 후 늦게 맨 위로 스크롤하므로 재시도 루프로 덮어씀."""
    _t = st.session_state.pop("_scroll_to", None)
    if not _t:
        return
    try:
        import streamlit.components.v1 as _stc
        _stc.html(
            "<script>(function(){var n=0;var id='tl-anchor-" + str(_t) + "';"
            "function go(){n++;try{var d=window.parent.document;"
            "var el=d.getElementById(id);"
            "if(el){el.scrollIntoView({block:'start',behavior:'auto'});}}catch(e){}"
            "if(n<25){setTimeout(go,80);}}go();})();</script>",
            height=0)
    except Exception:
        pass


def render_home_universe():
    """🪐 내 지식 우주 — 프로젝트=행성(메모·개념·작업 수에 비례한 크기). 홈 축약판."""
    _projs = [
        p for p in st.session_state.get("projects", [])
        if isinstance(p, dict) and _clean_text_value(p.get("name")).strip()
    ]
    if not _projs:
        st.markdown(
            "<div style='font-weight:800;font-size:1.05rem;'>🪐 내 지식 우주</div>",
            unsafe_allow_html=True)
        st.info("아직 행성이 없어요. 🪐 프로젝트를 만들면 첫 행성이 생겨요. (사이드바 📁 프로젝트)")
        return
    _notes = st.session_state.get("archive_notes", [])
    _tasks = st.session_state.get("tasks", [])
    _links = st.session_state.get("note_concept_links", [])
    _concepts = st.session_state.get("pkm_custom_concepts", [])
    _planets = []
    for _p in _projs:
        _nm = _clean_text_value(_p.get("name")).strip()
        _pn = [n for n in _notes if _clean_text_value(n.get("project")).strip() == _nm]
        _pn_ids = {n.get("id") for n in _pn}
        # canonical(별칭) 적용 — '일본 여행/일본여행' 표기차로 항로가 끊기지 않게
        _cset = {
            canonical_concept(l.get("concept")) for l in _links
            if (
                l.get("note_id") in _pn_ids
                or _clean_text_value(l.get("project")).strip() == _nm
            ) and canonical_concept(l.get("concept"))
        } | {
            canonical_concept(c.get("name")) for c in _concepts
            if isinstance(c, dict)
            and _clean_text_value(c.get("project")).strip() == _nm
            and canonical_concept(c.get("name"))
        }
        _tset = {str(t).replace("#", "").strip() for n in _pn
                 for t in (n.get("tags", []) or []) if str(t).strip()}
        _cc = len(_cset)
        _tg = len(_tset)
        _tk = sum(1 for t in _tasks if _clean_text_value(t.get("project")).strip() == _nm)
        _size = len(_pn) + _cc + _tk
        _planets.append({"name": _nm, "memos": len(_pn), "concepts": _cc,
                         "tags": _tg, "tasks": _tk, "size": _size,
                         "cset": _cset, "tset": _tset})
    _planets.sort(key=lambda x: x["size"], reverse=True)
    _planets = _planets[:8]
    _univ_names = [p["name"] for p in _planets]
    _sel_planet = _clean_text_value(st.session_state.get("home_univ_pick")).strip()
    if _sel_planet not in _univ_names:
        _sel_planet = None
        st.session_state["home_univ_pick"] = None

    # 🛸 항로 발견 — 공유 개념·태그·직접 관계로 "원래 이어져 있던" 행성쌍을 찾음
    _relations = st.session_state.get("relations", [])
    def _is_proj_rel(_r, _a, _b):
        _s = _clean_text_value(_r.get("source_name")).strip()
        _t = _clean_text_value(_r.get("target_name")).strip()
        return {_s, _t} == {_a, _b}
    _routes = []
    for _ri in range(len(_planets)):
        for _rj in range(_ri + 1, len(_planets)):
            _pa, _pb = _planets[_ri], _planets[_rj]
            _sc = sorted(_pa["cset"] & _pb["cset"])
            _stg = sorted(_pa["tset"] & _pb["tset"])
            _direct = any(_is_proj_rel(_r, _pa["name"], _pb["name"]) for _r in _relations)
            if not (_sc or _stg or _direct):
                continue
            _strength = len(_sc) * 2 + len(_stg) + (3 if _direct else 0)
            _routes.append({"i": _ri, "j": _rj, "a": _pa["name"], "b": _pb["name"],
                            "concepts": _sc, "tags": _stg, "direct": _direct,
                            "strength": _strength})
    _routes.sort(key=lambda r: -r["strength"])

    st.markdown(
        "<div style='font-weight:800;font-size:1.05rem;'>🪐 내 지식 우주</div>"
        "<div style='color:#64748b;font-size:13px;'>프로젝트가 행성이에요. 메모·개념·작업이 쌓일수록 행성이 커져요.</div>",
        unsafe_allow_html=True)
    with st.expander("ℹ️ 우주맵 읽는 법", expanded=False):
        st.markdown(
            "- 🌎 **중심** = 내 지식 전체 (항성)\n"
            "- 🪐 **행성** = 프로젝트 · **크기 = 메모+개념+작업 수** (쌓일수록 커져요)\n"
            "- 🚀 **탐사중** = 지금 선택한 프로젝트\n"
            "- 🟢 **실선** = 직접 만든 관계 · 🟣 **보라 점선** = 공유 개념 연결 · 🟠 **주황 점선** = 공유 태그 연결\n"
            "- 🛰️ **아래 행성 버튼**을 누르면 → 🌙 위성(메모·개념·태그·작업)이 펼쳐져요\n"
            "- 🌌 **전체**를 누르면 → 전체 우주 요약과 🌎🚀 지구 발사대가 보여요\n"
            "- 🚀 **지구 발사대** = 아직 프로젝트에 속하지 않은 지식을 행성으로 보내는 정리 공간이에요\n"
            "- 📝 메모는 클릭하면 상세로, 📁 버튼으로 프로젝트로 이동해요\n"
            "- 마우스를 지도 행성에 올리면 📝/🧠/🏷️/✅ 개수가 보여요")
    try:
        import plotly.graph_objects as _ugo
        import math as _umath
        _fig = _ugo.Figure()
        _n = len(_planets)
        _sel_now = _sel_planet  # 선택된 행성 — 지도에 강조

        # 행성별 고유 색 팔레트 (진짜 우주처럼 다채롭게)
        _PL_PALETTE = ["#60a5fa", "#a78bfa", "#34d399", "#f472b6",
                       "#22d3ee", "#fb923c", "#818cf8", "#2dd4bf"]
        def _hex_rgba(_hx, _a):
            _hx = _hx.lstrip("#")
            return f"rgba({int(_hx[0:2],16)},{int(_hx[2:4],16)},{int(_hx[4:6],16)},{_a})"

        _xs, _ys, _sizes, _texts, _hov, _colors, _lines, _lcolors, _custom = \
            [], [], [], [], [], [], [], [], []
        _halo_sizes, _halo_colors = [], []
        _sel_xy = None  # 선택 행성 좌표 — 🚀 탐사선 띄울 위치
        for _i, _pl in enumerate(_planets):
            _ang = 2 * _umath.pi * _i / max(1, _n)
            _xs.append(_umath.cos(_ang)); _ys.append(_umath.sin(_ang))
            _is_sel = (_pl["name"] == _sel_now)
            _base = max(26, min(80, 26 + _pl["size"] * 3))  # clamp 26~80
            _pcolor = _PL_PALETTE[_i % len(_PL_PALETTE)]
            _sz = _base + 10 if _is_sel else _base
            if _is_sel:
                _sel_xy = (_xs[-1], _ys[-1], _sz)  # 🚀 탐사선 위치·행성 크기
            _sizes.append(_sz)
            _colors.append(_pcolor)
            # 선택 = 밝은 흰 테두리 두껍게 / 평소 = 같은 색 옅은 테두리(대기 가장자리 느낌)
            _lines.append(3.5 if _is_sel else 1.5)
            _lcolors.append("#ffffff" if _is_sel else _hex_rgba(_pcolor, 0.9))
            # 대기광(글로우) — 행성 뒤 반투명 헤일로
            _halo_sizes.append(_sz * (2.2 if _is_sel else 1.7))
            _halo_colors.append(_hex_rgba(_pcolor, 0.34 if _is_sel else 0.18))
            _texts.append(f"🪐 {_pl['name']}")  # 이름만 깔끔하게 (선택 표시는 궤도의 🚀 탐사중으로)
            _hov.append(f"{_pl['name']}<br>📝 {_pl['memos']} · 🧠 {_pl['concepts']} · 🏷 {_pl['tags']} · ✅ {_pl['tasks']}")
            _custom.append(_pl["name"])

        # 0) 항로 — 행성 뒤에 깔리도록 가장 먼저 추가
        for _rt in _routes:
            _xi, _yi = _xs[_rt["i"]], _ys[_rt["i"]]
            _xj, _yj = _xs[_rt["j"]], _ys[_rt["j"]]
            if _rt["direct"]:
                _dash, _lcol = "solid", _hex_rgba("#22c55e", 0.95)   # 🟢 직접 관계
            elif _rt["concepts"]:
                _dash, _lcol = "dash", _hex_rgba("#c084fc", 0.9)      # 🟣 공유 개념
            else:
                _dash, _lcol = "dot", _hex_rgba("#fb923c", 0.9)       # 🟠 공유 태그
            _rw = min(7, 2.5 + _rt["strength"] * 0.6)
            # hover에 '왜 연결됐는지'(실제 공유 개념·태그명)를 보여줌
            _rtxt = f"{_rt['a']} ↔ {_rt['b']} · 강도 {_rt['strength']}"
            if _rt["concepts"]:
                _rtxt += "<br>🟣 공유 개념: " + ", ".join(_rt["concepts"][:5])
            if _rt["tags"]:
                _rtxt += "<br>🟠 공유 태그: " + ", ".join(_rt["tags"][:5])
            if _rt["direct"]:
                _rtxt += "<br>🟢 직접 관계 있음"
            _fig.add_trace(_ugo.Scatter(
                x=[_xi, _xj], y=[_yi, _yj], mode="lines",
                line=dict(color=_lcol, width=_rw, dash=_dash),
                hoverinfo="text", hovertext=[_rtxt, _rtxt], showlegend=False))

        _center_selected = _sel_planet is None
        # 1) 중심 항성 글로우(금빛 대기광)
        _fig.add_trace(_ugo.Scatter(
            x=[0], y=[0], mode="markers",
            marker=dict(size=(40 if _center_selected else 30) * 2.1,
                        color=_hex_rgba("#fbbf24", 0.30 if _center_selected else 0.16)),
            hoverinfo="skip", showlegend=False))
        # 2) 행성 글로우 레이어 (뒤)
        if _xs:
            _fig.add_trace(_ugo.Scatter(
                x=_xs, y=_ys, mode="markers",
                marker=dict(size=_halo_sizes, color=_halo_colors),
                hoverinfo="skip", showlegend=False))
        # 3) 중심 항성 본체 (금빛 — 행성과 색 구분)
        _fig.add_trace(_ugo.Scatter(
            x=[0], y=[0], mode="markers+text",
            text=["🌎 내 지식" + (" ✨" if _center_selected else "")],
            textposition="bottom center",
            marker=dict(size=40 if _center_selected else 30,
                        color="#fcd34d" if _center_selected else "#fbbf24",
                        line=dict(width=4 if _center_selected else 2,
                                  color="#fff7ed" if _center_selected else _hex_rgba("#fbbf24", 0.8))),
            customdata=["__all__"], hovertext=["전체 지식 우주"], hoverinfo="text",
            showlegend=False))
        # 4) 행성 본체 (앞)
        if _xs:
            _fig.add_trace(_ugo.Scatter(
                x=_xs, y=_ys, mode="markers+text", text=_texts, textposition="top center",
                marker=dict(size=_sizes, color=_colors, opacity=0.96,
                            line=dict(width=_lines, color=_lcolors)),
                customdata=_custom, hovertext=_hov, hoverinfo="text", showlegend=False))
        # 5) 🚀 탐사중 — 선택 행성 '바깥 궤도'의 작은 보조 표시 (주인공은 행성)
        if _sel_xy is not None:
            _sx, _sy, _ssz = _sel_xy
            # 행성 반지름보다 바깥(우하단 궤도)에 배치 — 행성 중심/이름을 가리지 않게
            _orb = 0.18 + (_ssz / 80.0) * 0.22
            _fig.add_trace(_ugo.Scatter(
                x=[_sx + _orb * 0.78], y=[_sy - _orb * 0.78], mode="text",
                text=["🚀 탐사중"], textposition="middle right",
                textfont=dict(size=11, color="#cbd5e1"),   # 작고 차분하게 (보조 역할)
                hovertext=["탐사선이 이 행성을 탐험 중"], hoverinfo="text",
                showlegend=False))
        # 클릭 선택 ON + 줌 비활성(더블클릭/드래그 줌 OFF)
        _fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                           dragmode=False,
                           xaxis=dict(visible=False, fixedrange=True, range=[-1.6, 1.6]),
                           yaxis=dict(visible=False, fixedrange=True, range=[-1.6, 1.6]),
                           plot_bgcolor="#0f172a", paper_bgcolor="#0f172a",
                           font=dict(color="#e2e8f0"))
        st.plotly_chart(
            _fig, use_container_width=True,
            config={"displayModeBar": False, "scrollZoom": False, "doubleClick": False}
        )
        # 🛸 항로 디버그 카운트 (0이어도 표시 — 데이터라 항상 노출)
        _r_direct = sum(1 for r in _routes if r["direct"])
        _r_concept = sum(1 for r in _routes if r["concepts"] and not r["direct"])
        _r_tag = sum(1 for r in _routes if r["tags"] and not r["concepts"] and not r["direct"])
        st.caption(
            f"🛸 발견된 항로 {len(_routes)}개 · 🟢 직접 관계 {_r_direct} · "
            f"🟣 공유 개념 {_r_concept} · 🟠 공유 태그 {_r_tag}"
        )
        if not _routes:
            st.markdown(
                "<div style='background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;"
                "padding:12px 16px;color:#166534;font-size:0.9em;line-height:1.6;'>"
                "🌱 아직 발견된 항로가 없어요.<br>"
                "같은 개념이나 태그를 쓰는 프로젝트가 생기면 <b>JIUM이 자동으로 연결을 발견</b>해요."
                "</div>", unsafe_allow_html=True)
        # 🛸 발견된 연결 목록 — '왜 연결됐는지' 실제 개념명/태그명을 보여줌
        if _routes:
            with st.expander(f"🛸 발견된 항로 {len(_routes)}개 — 내 세계는 이렇게 연결돼 있어요", expanded=False):
                for _rt in _routes[:12]:
                    _reason = []
                    if _rt["concepts"]:
                        _reason.append(
                            "<div style='margin-top:3px;'>🟣 <b>공유 개념</b>: "
                            + ", ".join(_univ_esc(_c) for _c in _rt["concepts"][:6])
                            + (f" 외 {len(_rt['concepts'])-6}" if len(_rt['concepts']) > 6 else "")
                            + "</div>")
                    if _rt["tags"]:
                        _reason.append(
                            "<div style='margin-top:3px;'>🟠 <b>공유 태그</b>: "
                            + ", ".join(f"#{_univ_esc(_t)}" for _t in _rt["tags"][:6])
                            + (f" 외 {len(_rt['tags'])-6}" if len(_rt['tags']) > 6 else "")
                            + "</div>")
                    if _rt["direct"]:
                        _reason.append("<div style='margin-top:3px;'>🟢 <b>직접 관계</b> 있음</div>")
                    st.markdown(
                        f"<div style='padding:8px 0;border-bottom:1px solid #f1f5f9;'>"
                        f"<b>🪐 {_univ_esc(_rt['a'])}</b> <span style='color:#94a3b8'>↔</span> "
                        f"<b>🪐 {_univ_esc(_rt['b'])}</b> "
                        f"<span style='color:#64748b;font-size:0.85em'>· 강도 {_rt['strength']}</span>"
                        f"<div style='color:#475569;font-size:0.86em'>{''.join(_reason)}</div>"
                        "</div>", unsafe_allow_html=True)
                if len(_routes) > 12:
                    st.caption(f"외 {len(_routes) - 12}개 항로가 더 있어요.")
        # 🔧 항로 후보 디버그 — 로컬/배포에서 다르게 보일 때 추적용 (각 행성의 개념·태그 집합)
        with st.expander("🔧 항로 후보 데이터 (디버그)", expanded=False):
            st.caption("각 행성의 개념(canonical)·태그 집합이에요. 두 행성에 같은 항목이 있으면 항로가 그려져요. "
                       "로컬/배포에서 항로가 다르면 여기서 집합이 다른지 확인하세요.")
            for _pl in _planets:
                _cs = ", ".join(sorted(_pl.get("cset", set()))) or "(없음)"
                _ts = ", ".join(f"#{_t}" for _t in sorted(_pl.get("tset", set()))) or "(없음)"
                st.markdown(
                    f"**🪐 {_univ_esc(_pl['name'])}**  \n"
                    f"<span style='color:#6d28d9;font-size:0.85em'>🧠 개념: {_univ_esc(_cs)}</span>  \n"
                    f"<span style='color:#15803d;font-size:0.85em'>🏷 태그: {_univ_esc(_ts)}</span>",
                    unsafe_allow_html=True)
    except Exception:
        for _pl in _planets:
            st.markdown(f"🪐 **{_pl['name']}** · 📝 {_pl['memos']} 🧠 {_pl['concepts']} ✅ {_pl['tasks']}")
    # 🪐 행성 선택 — 지도 클릭과 버튼 모두 home_univ_pick 하나로 동기화
    scroll_anchor("univ")  # 행성/위성/태그 클릭 후 이 위치(버튼 영역)로 복원
    st.markdown("**🪐 행성을 골라 위성(메모·개념·태그·작업)을 펼쳐봐요**")
    _pick_cols = st.columns(min(5, len(_planets)) + 1)
    for _i, _pl in enumerate(_planets[:5]):
        with _pick_cols[_i]:
            _picked = _pl["name"] == _sel_planet
            _label = f"{'✨ ' if _picked else ''}🪐 {_pl['name'][:8]}"
            if st.button(
                _label,
                key=f"univ_pick_{_i}",
                use_container_width=True,
                type=("primary" if _picked else "secondary"),  # 선택 시 색 유지(활성 상태)
                help="이 프로젝트 행성을 선택해서 연결된 메모·개념·태그·작업을 아래에 펼쳐요.",
            ):
                st.session_state["home_univ_pick"] = _pl["name"]
                st.session_state["univ_sat_view"] = "memo"  # 새 행성 선택 시 기본 탭
                request_scroll("univ")
                st.rerun()
    with _pick_cols[-1]:
        _all_label = f"{'✨ ' if _sel_planet is None else ''}🌌 전체"
        if st.button(
            _all_label,
            key="univ_pick_all",
            use_container_width=True,
            type=("primary" if _sel_planet is None else "secondary"),  # 선택 시 색 유지
            help="전체 우주 요약을 보고, 프로젝트에 아직 배정되지 않은 지식을 지구 발사대에서 정리해요.",
        ):
            st.session_state["home_univ_pick"] = None
            request_scroll("univ")
            st.rerun()
    _sel_planet = _clean_text_value(st.session_state.get("home_univ_pick")).strip()

    # 공통 헬퍼 — 행성 상세(if)·전체/발사대(else) 양쪽에서 모두 사용 (NameError 방지)
    import html as _univ_html

    def _univ_esc(_v):
        return _univ_html.escape(_clean_text_value(_v).strip() or "미지정")

    def _univ_chip(_text, _bg="#eef2ff", _fg="#3730a3", _prefix=""):
        _label = _univ_esc(_text)
        return (
            f"<span style='display:inline-block;margin:3px 5px 3px 0;padding:4px 9px;"
            f"border-radius:999px;background:{_bg};color:{_fg};font-size:12px;"
            f"font-weight:800;border:1px solid rgba(99,102,241,0.18);'>{_prefix}{_label}</span>"
        )

    def _task_badge(_status):
        _s = _clean_text_value(_status).strip()
        if "완료" in _s:
            return "✅", "#dcfce7", "#166534"
        if "진행" in _s:
            return "🔵", "#dbeafe", "#1d4ed8"
        if "검토" in _s:
            return "🟣", "#f3e8ff", "#7e22ce"
        if "보류" in _s:
            return "⏸️", "#fef3c7", "#92400e"
        return "⬜", "#f1f5f9", "#475569"

    _sel_obj = next((p for p in _planets if p["name"] == _sel_planet), None)
    if _sel_obj:
        _sel_planet = _sel_obj["name"]
        _pn = [n for n in _notes if _clean_text_value(n.get("project")).strip() == _sel_planet]
        _pn_ids = {n.get("id") for n in _pn}
        _pcs = sorted({
            _clean_text_value(l.get("concept")).strip() for l in _links
            if (
                l.get("note_id") in _pn_ids
                or _clean_text_value(l.get("project")).strip() == _sel_planet
            ) and _clean_text_value(l.get("concept")).strip()
        } | {
            _clean_text_value(c.get("name")).strip() for c in _concepts
            if isinstance(c, dict)
            and _clean_text_value(c.get("project")).strip() == _sel_planet
            and _clean_text_value(c.get("name")).strip()
        })
        _ptags = sorted({str(t).replace("#", "").strip() for n in _pn
                         for t in (n.get("tags", []) or []) if str(t).strip()})
        _ptk = [t for t in _tasks if _clean_text_value(t.get("project")).strip() == _sel_planet]
        st.markdown(f"**🪐 {_sel_planet} 위성**")
        st.caption("선택한 행성의 위성을 펼쳐봤어요.")
        # 위성 4카드 — 누르면 아래 '행성 상세'에서 해당 목록이 펼쳐져요 (클릭 가능)
        _sat = [("🌙", "메모", len(_pn), "memo"), ("🧠", "개념", len(_pcs), "concept"),
                ("🏷️", "태그", len(_ptags), "tag"), ("✅", "작업", len(_ptk), "task")]
        _sat_view = st.session_state.get("univ_sat_view") or "memo"
        _sat_cols = st.columns(4)
        for _si, (_sem, _snm, _scnt, _skey) in enumerate(_sat):
            with _sat_cols[_si]:
                if st.button(
                    f"{_sem} {_snm} {_scnt}",
                    key=f"univ_sat_{_skey}_{_sel_planet}",
                    use_container_width=True,
                    type=("primary" if _sat_view == _skey else "secondary"),
                    help=f"이 행성의 {_snm} 목록을 아래 행성 상세에서 펼쳐 봐요.",
                ):
                    st.session_state["univ_sat_view"] = _skey
                    request_scroll("univ")
                    st.rerun()
        st.caption("👆 카드를 누르면 아래 **행성 상세**에 그 목록이 펼쳐져요.")
        # 위 카드에서 고른 종류의 목록 — 카드 바로 아래에 펼쳐 보여줌(클릭 시 멀리 안 가게)
        def _render_sat_list():
            _view_label = {"memo": "📝 메모", "concept": "🧠 개념",
                           "tag": "🏷️ 태그", "task": "✅ 작업"}.get(_sat_view, "📝 메모")
            st.markdown(f"**{_view_label} 목록 — {_univ_esc(_sel_planet)}**")

            # 메모 카드 — 2열 컴팩트(세로로 너무 길지 않게)
            def _render_memo_cards(_notes, _kp):
                for _row in range(0, len(_notes), 2):
                    _mc = st.columns(2)
                    for _col, _wn in zip(_mc, _notes[_row:_row + 2]):
                        with _col:
                            with st.container(border=True):
                                _title = _clean_text_value(_wn.get("title")).strip() or "제목 없음"
                                _tags = [_clean_text_value(_t).replace("#", "").strip()
                                         for _t in (_wn.get("tags", []) or []) if _clean_text_value(_t).strip()]
                                _tag_line = " ".join(f"#{_univ_esc(_t)}" for _t in _tags[:2])
                                st.markdown(
                                    f"<div style='font-weight:800;font-size:14px;'>📝 {_univ_esc(_title)}</div>"
                                    f"<div style='color:#94a3b8;font-size:11px;margin-top:2px;'>{_tag_line or '태그 없음'}</div>",
                                    unsafe_allow_html=True)
                                if st.button("열기", key=f"univ_memo_{_kp}_{_wn.get('id')}_{_row}",
                                             use_container_width=True):
                                    st.session_state["archive_open_note_id"] = _wn.get("id")
                                    st.query_params["page"] = "archive"
                                    st.rerun()

            if _sat_view == "memo":
                if _pn:
                    _render_memo_cards(_pn, "all")
                else:
                    st.caption("아직 이 행성엔 메모가 없어요. ✍️ 메모를 만들어 위성을 띄워보세요.")
            elif _sat_view == "concept":
                if _pcs:
                    st.caption("개념을 누르면 🧠 개념 라이브러리에서 어디에 연결됐는지 봐요.")
                    _ccols = st.columns(4)
                    for _ci, _c in enumerate(_pcs):
                        with _ccols[_ci % 4]:
                            if st.button(f"🧠 {_c}", key=f"univ_con_{_sel_planet}_{_ci}",
                                         use_container_width=True):
                                st.session_state["concept_lib_open"] = canonical_concept(_c)
                                st.query_params["page"] = "concept_lib"
                                st.rerun()
                else:
                    st.caption("아직 연결된 개념이 없어요. 메모에서 개념이 추출되면 여기 모여요.")
            elif _sat_view == "tag":
                if _ptags:
                    st.caption("태그를 누르면 그 태그가 달린 메모만 아래에 보여요.")
                    _tag_pick = st.session_state.get(f"univ_tag_pick_{_sel_planet}")
                    _tcols = st.columns(4)
                    for _ti, _t in enumerate(_ptags):
                        with _tcols[_ti % 4]:
                            if st.button(f"#{_t}", key=f"univ_tag_{_sel_planet}_{_ti}",
                                         use_container_width=True,
                                         type=("primary" if _tag_pick == _t else "secondary")):
                                st.session_state[f"univ_tag_pick_{_sel_planet}"] = (
                                    None if _tag_pick == _t else _t)
                                request_scroll("univ")
                                st.rerun()
                    if _tag_pick:
                        _tagged = [
                            n for n in _pn
                            if _tag_pick in [_clean_text_value(x).replace("#", "").strip()
                                             for x in (n.get("tags", []) or [])]
                        ]
                        st.markdown(f"**#{_univ_esc(_tag_pick)} 메모 {len(_tagged)}개**")
                        _render_memo_cards(_tagged, f"tag_{_tag_pick}")
                else:
                    st.caption("아직 태그가 없어요. 메모에 태그를 달면 여기 모여요.")
            elif _sat_view == "task":
                if _ptk:
                    for _ti, _wt in enumerate(_ptk):
                        _title = _clean_text_value(_wt.get("title")).strip() or "제목 없음"
                        _status = _clean_text_value(_wt.get("status")).strip() or "상태 없음"
                        _priority = _clean_text_value(_wt.get("priority")).strip()
                        _due = _clean_text_value(_wt.get("due_date")).strip()
                        _emo, _bg, _fg = _task_badge(_status)
                        _meta = " · ".join([_v for _v in [_priority, _due[:10] if _due else ""] if _v])
                        with st.container(border=True):
                            st.markdown(
                                f"<div style='display:flex;align-items:center;gap:8px;'>"
                                f"<span style='background:{_bg};color:{_fg};border-radius:999px;"
                                f"padding:4px 9px;font-weight:900;font-size:12px;'>{_emo} {_univ_esc(_status)}</span>"
                                f"<span style='color:#64748b;font-size:12px;'>퀘스트 #{_ti + 1}</span></div>"
                                f"<div style='font-weight:900;font-size:15px;margin-top:8px;'>{_univ_esc(_title)}</div>"
                                f"<div style='color:#64748b;font-size:12px;margin-top:4px;'>{_univ_esc(_meta) if _meta else '추가 정보 없음'}</div>",
                                unsafe_allow_html=True)
                    if st.button("✅ 작업 보드로 이동", key=f"univ_tasks_open_{_sel_planet}", use_container_width=True):
                        st.query_params["page"] = "tasks"
                        st.rerun()
                else:
                    st.caption("아직 작업이 없어요. 작업을 만들면 여기서 퀘스트로 보여요.")
        _render_sat_list()

        st.markdown(
            "<div style='margin-top:18px;padding:14px 16px;border-radius:14px;"
            "background:linear-gradient(135deg,#eef2ff,#f5f3ff);"
            "border:1px solid #c7d2fe;'>"
            "<div style='font-size:18px;font-weight:900;color:#3730a3;'>🛰️ 행성 상세</div>"
            "<div style='font-size:13px;color:#4f46e5;margin-top:4px;'>"
            "메모·개념·태그·작업을 한눈에 보고 바로 이어가요.</div></div>",
            unsafe_allow_html=True)
        with st.expander("ℹ️ 이 화면 설명", expanded=False):
            st.markdown(
                "- 위의 **🌙 메모 / 🧠 개념 / 🏷️ 태그 / ✅ 작업 카드를 누르면** 그 목록이 여기에 펼쳐져요. (지금 선택된 카드는 파란색)\n"
                "- **📝 메모**: ‘메모 열기’로 상세를 봐요.\n"
                "- **🧠 개념 / 🏷️ 태그**: 이 행성에 모인 의미 라벨이에요.\n"
                "- **✅ 작업**: ‘작업 보드로 이동’으로 관리 화면으로 가요.\n"
                "- 맨 위 **🛸 항로**는 이 행성이 다른 행성과 어떻게 이어져 있는지 보여줘요(공유 개념·태그·직접 관계)."
            )
        # 🚀 이 행성의 위성을 다른 행성으로 이동 (행성↔행성 재배치)
        _other_planets = [n for n in _univ_names if n and n != _sel_planet]
        if _other_planets and (_pn or _ptk or _pcs):
            if st.checkbox(
                "🚀 이 행성의 지식을 다른 행성으로 보내기",
                key=f"univ_move_toggle_{_sel_planet}",
                help="선택한 메모·작업·개념의 프로젝트를 다른 행성으로 옮겨요.",
            ):
                st.caption(f"🛸 '{_sel_planet}' 행성의 위성을 다른 프로젝트 행성으로 이동해요.")
                _mv_dest = st.selectbox(
                    "목적 행성",
                    _other_planets,
                    key=f"univ_move_dest_{_sel_planet}",
                    help="선택한 지식들이 이동할 프로젝트 행성이에요.",
                )
                _mt_memo, _mt_task, _mt_concept = st.tabs(["🌙 메모", "✅ 작업", "🧠 개념"])
                with _mt_memo:
                    _mv_note_ids = [n.get("id") for n in _pn if n.get("id")]
                    _mv_note_lookup = {n.get("id"): n for n in _pn if n.get("id")}
                    _mv_sel_notes = st.multiselect(
                        "보낼 메모",
                        _mv_note_ids,
                        key=f"univ_move_notes_{_sel_planet}",
                        format_func=lambda _nid: _clean_text_value(
                            _mv_note_lookup.get(_nid, {}).get("title")
                        ).strip() or "제목 없음",
                    )
                with _mt_task:
                    _mv_task_ids = [t.get("id") for t in _ptk if t.get("id")]
                    _mv_task_lookup = {t.get("id"): t for t in _ptk if t.get("id")}
                    _mv_sel_tasks = st.multiselect(
                        "보낼 작업",
                        _mv_task_ids,
                        key=f"univ_move_tasks_{_sel_planet}",
                        format_func=lambda _tid: _clean_text_value(
                            _mv_task_lookup.get(_tid, {}).get("title")
                        ).strip() or "제목 없음",
                    )
                with _mt_concept:
                    _mv_sel_concepts = st.multiselect(
                        "보낼 개념",
                        list(_pcs),
                        key=f"univ_move_concepts_{_sel_planet}",
                    )
                _mv_total = len(_mv_sel_notes) + len(_mv_sel_tasks) + len(_mv_sel_concepts)
                if st.button(
                    f"🚀 '{_mv_dest}'(으)로 보내기 ({_mv_total}개)",
                    key=f"univ_move_go_{_sel_planet}",
                    use_container_width=True,
                    type="primary",
                    disabled=_mv_total == 0,
                    help="선택한 항목의 프로젝트 값을 목적 행성으로 바꿔요.",
                ):
                    _mv_now = datetime.now().strftime("%Y-%m-%d %H:%M")
                    for _n in st.session_state.get("archive_notes", []):
                        if _n.get("id") in _mv_sel_notes:
                            _n["project"] = _mv_dest
                            _n["updated_at"] = _mv_now
                    for _t in st.session_state.get("tasks", []):
                        if _t.get("id") in _mv_sel_tasks:
                            _t["project"] = _mv_dest
                            _t["updated_at"] = _mv_now
                    # 메모에 연결된 개념도 함께 이동 (개념은 메모에 딸려감)
                    _carried = set(_mv_sel_concepts)
                    for _lk in st.session_state.get("note_concept_links", []):
                        _cn = _clean_text_value(_lk.get("concept")).strip()
                        if _cn and _lk.get("note_id") in _mv_sel_notes:
                            _carried.add(_cn)
                    for _lk in st.session_state.get("note_concept_links", []):
                        _cn = _clean_text_value(_lk.get("concept")).strip()
                        if _cn in _carried or _lk.get("note_id") in _mv_sel_notes:
                            _lk["project"] = _mv_dest
                            _lk["updated_at"] = _mv_now
                    # 공유(shared/global) 개념은 여러 프로젝트의 연결 근거라 강제 이동하지 않음(비파괴)
                    for _c in st.session_state.get("pkm_custom_concepts", []):
                        _cnm = _clean_text_value(_c.get("name")).strip() if isinstance(_c, dict) else ""
                        if _cnm in _carried and concept_scope(_cnm) == "owned":
                            _c["project"] = _mv_dest
                            _c["updated_at"] = _mv_now
                    save_persisted_data()
                    _carried_extra = len(_carried) - len(_mv_sel_concepts)
                    _carry_msg = f" (개념 {_carried_extra}개 동반)" if _carried_extra > 0 else ""
                    _flash(f"🚀 {_mv_total}개를 '{_sel_planet}' → '{_mv_dest}'(으)로 이동했어요!{_carry_msg}")
                    for _k in (f"univ_move_notes_{_sel_planet}",
                               f"univ_move_tasks_{_sel_planet}",
                               f"univ_move_concepts_{_sel_planet}"):
                        st.session_state.pop(_k, None)
                    st.rerun()
        render_action_buttons("project", target_name=_sel_planet, project=_sel_planet,
                              key_prefix=f"univ_act_{_sel_planet}")
    else:
        _all_note_ids = {n.get("id") for n in _notes}
        _all_concepts = {
            _clean_text_value(l.get("concept")).strip() for l in _links
            if l.get("note_id") in _all_note_ids and _clean_text_value(l.get("concept")).strip()
        } | {
            _clean_text_value(c.get("name")).strip() for c in _concepts
            if isinstance(c, dict) and _clean_text_value(c.get("name")).strip()
        }
        _all_tags = {str(t).replace("#", "").strip() for n in _notes
                     for t in (n.get("tags", []) or []) if str(t).strip()}
        st.markdown("**🌌 전체 지식 우주**")
        st.caption("전체 지식 우주의 규모를 한눈에 봐요.")
        _overview = [("🪐", "프로젝트", len(_projs)), ("🌙", "메모", len(_notes)),
                     ("🧠", "개념", len(_all_concepts)), ("🏷️", "태그", len(_all_tags)),
                     ("✅", "작업", len(_tasks))]
        _overview_cols = st.columns(5)
        for _oi, (_oem, _onm, _ocnt) in enumerate(_overview):
            with _overview_cols[_oi]:
                with st.container(border=True):
                    st.markdown(
                        f"<div style='text-align:center'><div style='font-size:1.3em'>{_oem}</div>"
                        f"<b>{_onm}</b><br><span style='color:#6366f1;font-weight:700'>{_ocnt}개</span></div>",
                        unsafe_allow_html=True)
        st.markdown("**최근 커진 행성**")
        for _pl in _planets[:3]:
            st.markdown(
                f"- 🪐 {_pl['name']} · 메모 {_pl['memos']} · 개념 {_pl['concepts']} · 작업 {_pl['tasks']}"
            )
        import html as _launch_html
        _valid_projects = set(_univ_names)
        _note_project_by_id = {
            n.get("id"): _clean_text_value(n.get("project")).strip()
            for n in _notes
        }

        def _needs_launch(_project):
            _pname = _clean_text_value(_project).strip()
            return not _pname or _pname not in _valid_projects

        def _launch_esc(_v):
            return _launch_html.escape(_clean_text_value(_v).strip() or "미지정")

        _loose_notes = [
            n for n in _notes if isinstance(n, dict) and _needs_launch(n.get("project"))
        ]
        _loose_tasks = [
            t for t in _tasks if isinstance(t, dict) and _needs_launch(t.get("project"))
        ]
        _loose_link_concepts = {
            _clean_text_value(l.get("concept")).strip() for l in _links
            if _clean_text_value(l.get("concept")).strip()
            and _needs_launch(l.get("project"))
            and _needs_launch(_note_project_by_id.get(l.get("note_id")))
        }
        _loose_custom_concepts = {
            _clean_text_value(c.get("name")).strip() for c in _concepts
            if isinstance(c, dict)
            and _clean_text_value(c.get("name")).strip()
            and _needs_launch(c.get("project"))
        }
        _loose_concepts = sorted(_loose_link_concepts | _loose_custom_concepts)
        _loose_tags = sorted({
            _clean_text_value(t).replace("#", "").strip()
            for n in _loose_notes
            for t in (n.get("tags", []) or [])
            if _clean_text_value(t).strip()
        })
        _sections = st.session_state.get("project_sections", [])
        _steps = st.session_state.get("project_steps", [])
        _loose_sections = [
            (_idx, s) for _idx, s in enumerate(_sections)
            if isinstance(s, dict) and _needs_launch(s.get("project"))
        ]
        _loose_steps = [
            (_idx, s) for _idx, s in enumerate(_steps)
            if isinstance(s, dict) and _needs_launch(s.get("project"))
        ]
        _launch_total = (
            len(_loose_notes) + len(_loose_tasks) + len(_loose_concepts)
            + len(_loose_sections) + len(_loose_steps)
        )
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='padding:16px 18px;border-radius:16px;"
            "background:linear-gradient(135deg,#eef2ff,#ede9fe);"
            "border:1px solid #c4b5fd;'>"
            "<div style='font-size:20px;font-weight:950;color:#5b21b6;'>🌎🚀 지구 발사대</div>"
            "<div style='font-size:13px;color:#6d28d9;margin-top:5px;'>"
            "아직 행성에 배정되지 않은 지식을 모아 목적 행성으로 발사해요.</div></div>",
            unsafe_allow_html=True)
        with st.expander("ℹ️ 지구 발사대 사용법", expanded=False):
            st.markdown(
                "- 프로젝트가 비어 있거나 잘못 연결된 메모·작업·개념이 여기에 모여요.\n"
                "- **목적 행성**을 고른 뒤, 탭에서 보낼 항목을 선택하세요.\n"
                "- `🚀 선택 항목 발사`를 누르면 선택한 항목의 프로젝트가 목적 행성으로 바뀌어요.\n"
                "- 메모를 발사하면 메모 안의 태그와 연결 개념도 같이 따라가요."
            )
        _lc1, _lc2, _lc3, _lc4, _lc5 = st.columns(5)
        _launch_counts = [
            (_lc1, "🌙", "대기 메모", len(_loose_notes)),
            (_lc2, "🧠", "대기 개념", len(_loose_concepts)),
            (_lc3, "🏷️", "동승 태그", len(_loose_tags)),
            (_lc4, "✅", "대기 작업", len(_loose_tasks)),
            (_lc5, "🧩", "섹션/단계", len(_loose_sections) + len(_loose_steps)),
        ]
        for _col, _em, _name, _count in _launch_counts:
            with _col:
                with st.container(border=True):
                    st.markdown(
                        f"<div style='text-align:center'><div style='font-size:1.25em'>{_em}</div>"
                        f"<b>{_name}</b><br><span style='color:#7c3aed;font-weight:900'>{_count}개</span></div>",
                        unsafe_allow_html=True)
        # 🔍 대기 항목이 '무엇인지' 실제 목록으로 펼쳐 보기 (숫자만 보이던 문제 해결)
        if _launch_total > 0:
            with st.expander("🔍 대기 항목 목록 보기", expanded=False):
                _lc_note_titles = [
                    _clean_text_value(n.get("title")).strip() or "제목 없음" for n in _loose_notes
                ]
                _lc_task_titles = [
                    _clean_text_value(t.get("title")).strip() or "제목 없음" for t in _loose_tasks
                ]
                _lc_lists = [
                    ("🌙", "대기 메모", _lc_note_titles, ""),
                    ("🧠", "대기 개념", list(_loose_concepts), ""),
                    ("🏷️", "동승 태그", list(_loose_tags), "#"),
                    ("✅", "대기 작업", _lc_task_titles, ""),
                ]
                for _em, _nm, _items, _pre in _lc_lists:
                    if _items:
                        st.markdown(f"**{_em} {_nm} {len(_items)}개**")
                        st.markdown(
                            "\n".join(f"- {_pre}{_univ_esc(_it)}" for _it in _items[:15])
                            + (f"\n- … 외 {len(_items)-15}개" if len(_items) > 15 else ""))
                st.caption("아래 탭에서 보낼 항목을 선택해 발사하거나, 그대로 두면 발사 대기로 남아요.")
        if _launch_total == 0:
            st.success("🌍 모든 지식이 제자리를 찾았어요.")
            st.caption("발사 대기 중인 메모나 개념이 없습니다.")
        else:
            _dest = st.selectbox(
                "목적 행성",
                _univ_names,
                key="univ_launch_dest",
                help="선택한 지식들이 이동할 프로젝트 행성이에요.",
            )
            _tab_memo, _tab_task, _tab_concept, _tab_section = st.tabs(
                ["🌙 메모", "✅ 작업", "🧠 개념", "🧩 섹션"]
            )
            with _tab_memo:
                _note_ids = [n.get("id") for n in _loose_notes if n.get("id")]
                _note_lookup = {n.get("id"): n for n in _loose_notes if n.get("id")}
                _sel_note_ids = st.multiselect(
                    "발사할 메모",
                    _note_ids,
                    key="univ_launch_notes",
                    help="프로젝트가 비어 있거나 현재 행성과 연결되지 않은 메모예요.",
                    format_func=lambda _nid: _clean_text_value(
                        _note_lookup.get(_nid, {}).get("title")
                    ).strip() or "제목 없음",
                )
                if _loose_tags:
                    st.caption("태그는 메모 로켓에 함께 실려 이동해요: " + ", ".join(f"#{t}" for t in _loose_tags[:10]))
            with _tab_task:
                _task_ids = [t.get("id") for t in _loose_tasks if t.get("id")]
                _task_lookup = {t.get("id"): t for t in _loose_tasks if t.get("id")}
                _sel_task_ids = st.multiselect(
                    "발사할 작업",
                    _task_ids,
                    key="univ_launch_tasks",
                    help="프로젝트가 비어 있거나 현재 행성과 연결되지 않은 작업이에요.",
                    format_func=lambda _tid: _clean_text_value(
                        _task_lookup.get(_tid, {}).get("title")
                    ).strip() or "제목 없음",
                )
            with _tab_concept:
                _sel_concepts = st.multiselect(
                    "발사할 개념",
                    _loose_concepts,
                    key="univ_launch_concepts",
                    help="메모나 프로젝트에 아직 안정적으로 배정되지 않은 개념이에요.",
                )
            with _tab_section:
                _section_options = [f"section:{_idx}" for _idx, _ in _loose_sections] + [
                    f"step:{_idx}" for _idx, _ in _loose_steps
                ]
                _section_lookup = {
                    f"section:{_idx}": s for _idx, s in _loose_sections
                } | {
                    f"step:{_idx}": s for _idx, s in _loose_steps
                }
                _sel_section_keys = st.multiselect(
                    "발사할 프로젝트 섹션/단계",
                    _section_options,
                    key="univ_launch_sections",
                    help="프로젝트 섹션이나 단계 데이터가 행성과 연결되지 않았을 때 여기에서 배정해요.",
                    format_func=lambda _key: (
                        ("섹션 · " if _key.startswith("section:") else "단계 · ")
                        + (_clean_text_value(_section_lookup.get(_key, {}).get("name")).strip()
                           or _clean_text_value(_section_lookup.get(_key, {}).get("title")).strip()
                           or "이름 없음")
                    ),
                )
            _selected_total = (
                len(_sel_note_ids) + len(_sel_task_ids) + len(_sel_concepts) + len(_sel_section_keys)
            )
            # 🚀 전체 발사 — 대기 중인 모든 지식을 목적 행성으로 한 번에
            if st.button(
                f"🚀 전체 발사 ({_launch_total}개)",
                key="univ_launch_all",
                use_container_width=True,
                help=f"발사 대기 중인 모든 항목을 '{_dest}'(으)로 한 번에 보내요.",
            ):
                _sel_note_ids = [n.get("id") for n in _loose_notes if n.get("id")]
                _sel_task_ids = [t.get("id") for t in _loose_tasks if t.get("id")]
                _sel_concepts = list(_loose_concepts)
                _sel_section_keys = (
                    [f"section:{_idx}" for _idx, _ in _loose_sections]
                    + [f"step:{_idx}" for _idx, _ in _loose_steps]
                )
                st.session_state["_univ_launch_force"] = True
            if st.button(
                f"🚀 선택 항목 발사 ({_selected_total}개)",
                key="univ_launch_selected",
                use_container_width=True,
                type="primary",
                disabled=_selected_total == 0,
                help="선택한 항목의 프로젝트 값을 목적 행성으로 바꿔서 우주맵에 배정해요.",
            ) or st.session_state.pop("_univ_launch_force", False):
                _now = datetime.now().strftime("%Y-%m-%d %H:%M")
                for _n in st.session_state.get("archive_notes", []):
                    if _n.get("id") in _sel_note_ids:
                        _n["project"] = _dest
                        _n["updated_at"] = _now
                for _t in st.session_state.get("tasks", []):
                    if _t.get("id") in _sel_task_ids:
                        _t["project"] = _dest
                        _t["updated_at"] = _now
                # 메모에 연결된 개념도 함께 발사 (개념은 메모에 딸려감)
                _launch_carried = set(_sel_concepts)
                for _lk in st.session_state.get("note_concept_links", []):
                    _cn = _clean_text_value(_lk.get("concept")).strip()
                    if _cn and _lk.get("note_id") in _sel_note_ids:
                        _launch_carried.add(_cn)
                for _lk in st.session_state.get("note_concept_links", []):
                    _concept_name = _clean_text_value(_lk.get("concept")).strip()
                    if _concept_name in _launch_carried or _lk.get("note_id") in _sel_note_ids:
                        _lk["project"] = _dest
                        _lk["updated_at"] = _now
                # 공유(shared/global) 개념은 연결 근거라 강제 이동하지 않음(비파괴)
                for _c in st.session_state.get("pkm_custom_concepts", []):
                    _cnm = _clean_text_value(_c.get("name")).strip() if isinstance(_c, dict) else ""
                    if _cnm in _launch_carried and concept_scope(_cnm) == "owned":
                        _c["project"] = _dest
                        _c["updated_at"] = _now
                for _key in _sel_section_keys:
                    _kind, _idx_text = _key.split(":", 1)
                    _idx = int(_idx_text)
                    if _kind == "section" and _idx < len(st.session_state.get("project_sections", [])):
                        st.session_state["project_sections"][_idx]["project"] = _dest
                        st.session_state["project_sections"][_idx]["updated_at"] = _now
                    if _kind == "step" and _idx < len(st.session_state.get("project_steps", [])):
                        st.session_state["project_steps"][_idx]["project"] = _dest
                        st.session_state["project_steps"][_idx]["updated_at"] = _now
                save_persisted_data()
                _flash(f"🚀 {_selected_total}개를 '{_dest}'(으)로 발사! 남은 지식을 계속 배분하세요.")
                # 발사 후에도 발사대(전체 보기)에 머물러 남은 항목을 계속 배분
                st.session_state["home_univ_pick"] = None
                for _k in ("univ_launch_notes", "univ_launch_tasks",
                           "univ_launch_concepts", "univ_launch_sections"):
                    st.session_state.pop(_k, None)
                st.rerun()


# ── 🌍 세계 성장 — 이번 달 내 지식 세계가 얼마나 커졌나 (체감용 요약) ──
def _wg_render():
    _now_ym = datetime.now().strftime("%Y-%m")
    def _ym_of(*vals):
        for _v in vals:
            _s = _clean_text_value(_v).strip()
            if len(_s) >= 7:
                return _s[:7]
        return ""
    _notes = st.session_state.get("archive_notes", [])
    _concepts = [c for c in st.session_state.get("pkm_custom_concepts", []) if isinstance(c, dict)]
    _rels = st.session_state.get("relations", [])
    _tasks = st.session_state.get("tasks", [])
    _projs = st.session_state.get("projects", [])
    _links = st.session_state.get("note_concept_links", [])

    def _cnt_this_before(_items, _fields):
        _this = _before = 0
        for _it in _items:
            if not isinstance(_it, dict):
                continue
            _m = _ym_of(*[_it.get(_f) for _f in _fields])
            if not _m:
                continue
            if _m == _now_ym:
                _this += 1
            elif _m < _now_ym:
                _before += 1
        return _this, _before

    # 동사형 라벨로 — '발견/탄생/연결/추가'
    _new_proj, _bef_proj = _cnt_this_before(_projs, ("created_at",))
    _new_note, _bef_note = _cnt_this_before(_notes, ("saved_at", "created_at"))
    _new_concept, _bef_concept = _cnt_this_before(_concepts, ("created_at",))
    _new_rel, _bef_rel = _cnt_this_before(_rels, ("created_at",))
    _tot_this = _new_proj + _new_note + _new_concept + _new_rel
    _tot_before = _bef_proj + _bef_note + _bef_concept + _bef_rel
    _expand_pct = round(_tot_this / _tot_before * 100) if _tot_before else None

    st.markdown(
        "<div style='font-weight:800;font-size:1.05rem;margin:2px 0 8px;'>🌍 세계 성장 리포트</div>",
        unsafe_allow_html=True)
    if _tot_this == 0:
        st.caption("이번 달 기록을 시작하면 여기서 내 세계가 커지는 게 보여요. ✍️ 위 오늘 한 줄부터!")
        return

    # 이번 달 성장 라인 (동사형)
    _lines_data = [
        ("🪐", f"새로운 행성 {_new_proj}개 발견", _new_proj),
        ("🌙", f"메모 {_new_note}개 기록", _new_note),
        ("🧠", f"개념 {_new_concept}개 탄생", _new_concept),
        ("🔗", f"관계 {_new_rel}개 연결", _new_rel),
    ]
    _grow_lines = "".join(
        f"<div style='font-size:0.95em;color:#1e293b;margin:2px 0;'>{_em} {_txt}</div>"
        for _em, _txt, _c in _lines_data if _c > 0
    )
    _pct_html = (
        f"<div style='font-size:1.7rem;font-weight:900;color:#059669;'>+{_expand_pct}%</div>"
        f"<div style='font-size:0.8rem;color:#64748b;'>세계 확장도</div>"
        if _expand_pct is not None else
        f"<div style='font-size:1.2rem;font-weight:900;color:#059669;'>첫 달 🌱</div>"
        f"<div style='font-size:0.8rem;color:#64748b;'>세계의 시작</div>"
    )
    st.markdown(
        "<div style='display:flex;align-items:center;gap:20px;background:linear-gradient(135deg,#f0f9ff,#faf5ff);"
        "border:1px solid #e2e8f0;border-radius:14px;padding:16px 22px;'>"
        f"<div style='text-align:center;min-width:100px;'>{_pct_html}</div>"
        f"<div style='flex:1;'><div style='font-size:0.85rem;color:#475569;margin-bottom:6px;font-weight:700;'>"
        f"이번 달({_now_ym}) 내 세계의 변화</div>{_grow_lines}</div>"
        "</div>",
        unsafe_allow_html=True)

    # ── 인사이트: 가장 많이 성장한 영역 / 가장 많이 연결된 개념 ──
    _proj_growth = {}
    for _n in _notes:
        if _ym_of(_n.get("saved_at"), _n.get("created_at")) == _now_ym:
            _p = _clean_text_value(_n.get("project")).strip()
            if _p:
                _proj_growth[_p] = _proj_growth.get(_p, 0) + 1
    for _t in _tasks:
        if _ym_of(_t.get("created_at")) == _now_ym:
            _p = _clean_text_value(_t.get("project")).strip()
            if _p:
                _proj_growth[_p] = _proj_growth.get(_p, 0) + 1
    for _c in _concepts:
        if _ym_of(_c.get("created_at")) == _now_ym:
            _p = _clean_text_value(_c.get("project")).strip()
            if _p:
                _proj_growth[_p] = _proj_growth.get(_p, 0) + 1
    _top_proj = max(_proj_growth.items(), key=lambda x: x[1]) if _proj_growth else None

    # 이번 달 가장 많이 연결된 개념 (note_concept_links 기준)
    _note_month = {
        _n.get("id"): _ym_of(_n.get("saved_at"), _n.get("created_at")) for _n in _notes
    }
    _concept_links = {}
    for _l in _links:
        _cn = _clean_text_value(_l.get("concept")).strip()
        _lm = _ym_of(_l.get("linked_at"), _l.get("created_at")) or _note_month.get(_l.get("note_id"), "")
        if _cn and _lm == _now_ym:
            _concept_links[_cn] = _concept_links.get(_cn, 0) + 1
    _top_concept = max(_concept_links.items(), key=lambda x: x[1]) if _concept_links else None

    if _top_proj or _top_concept:
        _ins1, _ins2 = st.columns(2)
        with _ins1:
            if _top_proj:
                st.markdown(
                    f"<div style='font-size:0.8rem;color:#64748b;'>📈 가장 많이 성장한 영역</div>"
                    f"<div style='font-weight:800;color:#7c3aed;'>🪐 {_top_proj[0]} "
                    f"<span style='color:#94a3b8;font-weight:600;'>(+{_top_proj[1]})</span></div>",
                    unsafe_allow_html=True)
        with _ins2:
            if _top_concept:
                st.markdown(
                    f"<div style='font-size:0.8rem;color:#64748b;'>🧠 가장 많이 연결된 개념</div>"
                    f"<div style='font-weight:800;color:#0ea5e9;'>{_top_concept[0]} "
                    f"<span style='color:#94a3b8;font-weight:600;'>({_top_concept[1]}개 연결)</span></div>",
                    unsafe_allow_html=True)
        if _top_concept and _top_concept[1] >= 2:
            st.caption(f"💡 ‘{_top_concept[0]}’ 개념이 이번 달 {_top_concept[1]}개의 새로운 연결을 만들었어요. 아는 만큼 보여요.")
        elif _expand_pct is not None:
            st.caption(f"💡 이번 달에만 지식 세계가 {_expand_pct}% 넓어졌어요. 아는 만큼 보여요.")

# 홈을 짧게: 성장 리포트/우주맵은 토글로 접어둠 (expander로 감싸면 내부 expander와 중첩 오류 → 토글 사용)
st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
_home_t1, _home_t2, _home_t3 = st.columns(3)
with _home_t1:
    _home_show_growth = st.toggle("🌍 성장 리포트", value=False, key="home_show_growth")
with _home_t2:
    _home_show_univ = st.toggle("🪐 내 지식 우주", value=False, key="home_show_univ")
with _home_t3:
    _home_show_brain = st.toggle("🧠 뇌지도", value=False, key="home_show_brain")

if _home_show_growth:
    try:
        _wg_render()
    except Exception:
        pass
if _home_show_univ:
    try:
        render_home_universe()
    except Exception as _univ_err:
        import traceback as _univ_tb
        st.error("🪐 내 지식 우주/지구 발사대 렌더 중 오류가 났어요.")
        st.code(_univ_tb.format_exc())
    apply_scroll_restore()
if _home_show_brain:
    render_home_mini_knowledge_graph(_brain_theme_key)
st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

_home_lbl = get_home_theme_labels(_brain_theme_key)

_dm1, _dm2, _dm3, _dm4 = st.columns(4)
_dm1.metric(_home_lbl["active_projects"], len(_dash_active_proj))
_dm2.metric(_home_lbl["today_tasks"], len(_dash_today_tasks))
_dm3.metric(_home_lbl["open_tasks"], len(_dash_open_tasks))
_dm4.metric(_home_lbl["total_memos"], len(_dash_notes))

# ── 최근 활동: 텍스트가 흩어지지 않게 테두리 카드 2열 그리드로 묶음 ──
st.markdown("#### 🗂️ 최근 활동")

# 각 영역 데이터 준비
_recent_proj = _dash_active_proj[:5]
_recent_tasks = sorted(_dash_open_tasks, key=lambda t: str(t.get("created_at", "")), reverse=True)
_recent_notes = sorted(_dash_notes, key=lambda n: str(n.get("saved_at", "")), reverse=True)
_research = [n for n in _dash_notes if "연구노트" in str(n.get("title", "")) or "연구노트" in [str(t) for t in n.get("tags", [])]]
_research = sorted(_research, key=lambda n: str(n.get("saved_at", "")), reverse=True)
_recent_con = [c for c in _dash_concepts if isinstance(c, dict) and c.get("created_at")]
_recent_con = sorted(_recent_con, key=lambda c: str(c.get("created_at", "")), reverse=True)

def _proj_lines():
    out = []
    for _p in _recent_proj:
        _pri = _p.get("priority", "")
        _badge = f" · {_pri}" if _pri else ""
        out.append(f"- {_home_lbl['proj_icon']} **{_p.get('name','')}**{_badge}")
    return out

def _task_lines():
    out = []
    for _t in _recent_tasks[:6]:
        _due = str(_t.get("due_date", ""))[:10]
        _due_str = f" · ~{_due}" if _due else ""
        out.append(f"- {_home_lbl['task_icon']} {_t.get('title','')}{_due_str}")
    return out

# 카드 스펙: 홈 간소화 — 최근 메모 + 실행 작업 2블록만 (개념·연구·프로젝트는 우주맵/각 메뉴에서)
_recent_cards = [
    (_home_lbl["recent_memos"], len(_dash_notes),
     [f"- {_home_lbl['memo_icon']} {_n.get('title','제목 없음')}" for _n in _recent_notes[:5]],
     "저장된 메모가 없어요."),
    (_home_lbl["recent_tasks"], len(_dash_open_tasks), _task_lines(),
     "미완료 작업이 없어요."),
]

def _render_recent_card(_col, _title, _total, _lines, _empty):
    with _col:
        with st.container(border=True):
            st.markdown(f"**{_title}** &nbsp;·&nbsp; <span style='color:#64748b;'>{_total}개</span>", unsafe_allow_html=True)
            if _lines:
                st.markdown("\n".join(_lines))
                if _total > len(_lines):
                    st.caption(f"+ 외 {_total - len(_lines)}개 더 있어요")
            else:
                st.caption(_empty)

# 2열 그리드로 렌더 (홀수면 마지막 카드는 왼쪽 한 칸)
for _ci in range(0, len(_recent_cards), 2):
    _gc1, _gc2 = st.columns(2)
    _t1, _n1, _l1, _e1 = _recent_cards[_ci]
    _render_recent_card(_gc1, _t1, _n1, _l1, _e1)
    if _ci + 1 < len(_recent_cards):
        _t2, _n2, _l2, _e2 = _recent_cards[_ci + 1]
        _render_recent_card(_gc2, _t2, _n2, _l2, _e2)

# 🔗 링크/글 가져오기 — 홈에선 상단 주황 버튼으로 펼침(기본 접힘). result/고급은 항상 노출
_input_page = st.query_params.get("page", "home")
if _input_page not in ("result",) and not get_setting("show_advanced"):
    if not st.session_state.get("home_show_import"):
        st.stop()  # 접힘: 상단 '🔗 링크·글 가져와서 메모 만들기' 토글로 열어요

st.divider()

# -----------------------------
# Main Input Page (➕ 새 메모 — 정보 수집/분석)
# -----------------------------
st.markdown("## ➕ 새 메모·정보 수집")
st.caption("URL이나 글을 가져와 원문을 보관하고, AI가 요약·신뢰도·개념 후보를 만든 뒤 지식 메모로 연결해요.")
_collect_step, _collect_done = compute_collection_step()
render_collection_stepper(_collect_step, _collect_done)
left_col, right_col = st.columns([1.35, 1])

with left_col:
    st.markdown('<div class="input-shell">', unsafe_allow_html=True)
    st.markdown('<div class="question-title">분석할 정보 유형과 입력 방식을 선택해주세요</div>', unsafe_allow_html=True)
    st.markdown('<div class="question-subtitle">맛집 후기와 정책 정보는 신뢰도 기준이 다르게 적용돼요.</div>', unsafe_allow_html=True)

    selected_type_label = st.radio(
        "콘텐츠 유형",
        ["자동 판단", "맛집/제품/장소 후기", "정책/지원사업/공공정보", "일반 정보글", "공부자료"],
        horizontal=False,
    )
    selected_type_map = {
        "자동 판단": "unknown",
        "맛집/제품/장소 후기": "review",
        "정책/지원사업/공공정보": "policy",
        "일반 정보글": "info",
        "공부자료": "study",
    }
    selected_type = selected_type_map[selected_type_label]

    input_mode = st.radio(
        "분석 방식",
        ["링크로 조회하기", "글 붙여넣기로 조회하기"],
        horizontal=True,
    )

    st.markdown(
        f'''
        <div class="choice-box">
            <div class="choice-box-title">현재 선택: {selected_type_label} · {input_mode}</div>
            <div class="choice-box-desc">선택한 유형에 맞춰 점수 기준과 분석 근거가 다르게 적용돼요.</div>
        </div>
        ''',
        unsafe_allow_html=True,
    )

    pasted_text = ""
    if input_mode == "링크로 조회하기":
        url_input = st.text_input("🔗 분석할 URL", placeholder="https://example.com/article")
    else:
        url_input = ""
        pasted_text = st.text_area(
            "📝 분석할 글 붙여넣기",
            placeholder="블로그 글, 정책 안내문, 상품 후기, 기사 일부 등을 여기에 붙여넣어주세요.",
            height=260,
        )

    show_debug = st.checkbox("추출/입력 본문 디버그 보기", value=False)
    analyze_btn = st.button("🔍 신뢰도 분석 시작", type="primary", use_container_width=True)

    st.markdown("""
    <div class="info-note">
    💡 <b>정확한 답변이 도움이 됩니다</b><br>
    리뷰 글은 본인 경험, 가격, 메뉴, 사진, 재방문 의사를 중심으로 보고<br>
    정책 글은 공식 기관, 날짜, 신청 조건, 출처를 중심으로 분석해요.
    </div>
    """, unsafe_allow_html=True)
    analysis_status_slot = st.empty()
    st.markdown("</div>", unsafe_allow_html=True)

with right_col:
    st.markdown('<div class="side-help-card">', unsafe_allow_html=True)
    st.markdown("### ✨ 예상 결과 미리보기")
    st.caption("분석 후 제공되는 결과와 현재 적용 중인 신뢰도 기준을 미리 확인해요.")
    card_col1, card_col2 = st.columns(2)
    with card_col1:
        st.markdown('<div class="help-grid-card"><div class="help-grid-title">🛡️ 신뢰도 분석</div><div class="help-grid-desc">콘텐츠 유형에 맞는 기준으로 점수를 계산해요.</div></div>', unsafe_allow_html=True)
    with card_col2:
        st.markdown('<div class="help-grid-card"><div class="help-grid-title">🟢 광고 위험도</div><div class="help-grid-desc">협찬/체험단/파트너스 문구를 확인해요.</div></div>', unsafe_allow_html=True)
    card_col3, card_col4 = st.columns(2)
    with card_col3:
        st.markdown('<div class="help-grid-card"><div class="help-grid-title">👤 작성자 성향</div><div class="help-grid-desc">기록형, 객관형, 비판형, 홍보형으로 분류해요.</div></div>', unsafe_allow_html=True)
    with card_col4:
        st.markdown('<div class="help-grid-card"><div class="help-grid-title">📈 시각화</div><div class="help-grid-desc">점수 근거를 그래프와 표로 확인할 수 있어요.</div></div>', unsafe_allow_html=True)
    st.markdown("""
    <div class="info-note" style="margin-top:18px;">
    🧠 <b>지식 아카이브 흐름</b><br>
    URL 분석 또는 글 붙여넣기 → 태그 추천 → AI 초안 생성 → 사용자 수정 → 저장 → 태그별 조회<br><br>
    ⚡ 같은 URL은 캐시를 사용해서 API 호출을 줄여요.<br>
    💾 메모/기록은 trustlens_data.json에 저장돼요.
    </div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown("### 🛠️ 이번 분석에 적용할 신뢰도 기준")
    st.caption("저장된 기준 중 이번 분석에 사용할 기준을 선택하거나, 아래에서 새 기준을 바로 추가할 수 있어요.")

    criterion_titles = [c.get("title") for c in st.session_state.custom_trust_criteria if c.get("title")]
    active_defaults = st.session_state.get("active_custom_criteria_titles", [])
    active_defaults = [title for title in active_defaults if title in criterion_titles]

    if criterion_titles:
        selected_active_titles = st.multiselect(
            "저장된 커스텀 기준 불러오기",
            options=criterion_titles,
            default=active_defaults or criterion_titles,
            help="선택한 기준만 다음 분석 프롬프트에 반영돼요.",
        )
        st.session_state.active_custom_criteria_titles = selected_active_titles
        save_persisted_data()

        if selected_active_titles:
            st.success(f"이번 분석에 {len(selected_active_titles)}개의 커스텀 기준이 반영돼요.")
            for idx, title in enumerate(selected_active_titles[:3], start=1):
                matched = next((c for c in st.session_state.custom_trust_criteria if c.get("title") == title), {})
                st.markdown(
                    f"""
                    <div class="history-item">
                        <div class="history-title">{idx}. {matched.get('title', title)} · 중요도 {matched.get('weight', '보통')}</div>
                        <div class="history-meta">{matched.get('description', '설명 없음')}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.warning("이번 분석에는 커스텀 기준이 적용되지 않아요.")
    else:
        st.info("아직 저장된 커스텀 기준이 없어요. 아래에서 바로 하나 추가할 수 있어요.")

    quick_custom_title_key = "quick_custom_criterion_title"
    quick_custom_desc_key = "quick_custom_criterion_desc"
    quick_custom_weight_key = "quick_custom_criterion_weight"

    with st.expander("➕ 새 신뢰도 기준 빠르게 추가하기", expanded=not bool(criterion_titles)):
        st.text_input("빠른 기준 이름", placeholder="예: 실제 사진 근거", key=quick_custom_title_key)
        st.text_area(
            "빠른 기준 설명",
            placeholder="예: 사진이 많고 상황 설명이 구체적인 글을 더 신뢰한다.",
            height=86,
            key=quick_custom_desc_key,
        )
        st.selectbox(
            "빠른 기준 중요도",
            ["낮음", "보통", "높음"],
            index=1,
            key=quick_custom_weight_key,
        )
        st.button(
            "➕ 이 기준 추가하고 이번 분석에 반영하기",
            key="quick_add_custom_trust_criterion",
            use_container_width=True,
            on_click=save_custom_trust_criterion,
            args=(quick_custom_title_key, quick_custom_desc_key, quick_custom_weight_key),
        )

    if st.session_state.get("custom_criterion_saved"):
        st.success("커스텀 기준을 저장했어요. 이번 분석부터 반영돼요.")
        st.session_state["custom_criterion_saved"] = False
    if st.session_state.get("custom_criterion_error"):
        st.warning(st.session_state["custom_criterion_error"])
        st.session_state["custom_criterion_error"] = ""

    st.markdown("</div>", unsafe_allow_html=True)


if analyze_btn:
    st.session_state["analysis_status_message"] = None
    if input_mode == "링크로 조회하기" and not url_input.strip():
        st.warning("URL을 입력해주세요.")
    elif input_mode == "링크로 조회하기" and not url_input.startswith("http"):
        st.warning("http:// 또는 https://로 시작하는 URL을 입력해주세요.")
    elif input_mode == "글 붙여넣기로 조회하기" and len(pasted_text.strip()) < 100:
        st.warning("분석할 글을 100자 이상 붙여넣어주세요.")
    else:
        if input_mode == "링크로 조회하기":
            with st.spinner("본문 추출 중..."):
                text, err, final_url = extract_text(url_input.strip())
            analysis_source = url_input.strip()
            # 우선순위 3: 본문 추출 품질 경고 (블로그 사이드바/댓글이 섞였을 때)
            if text:
                _junk_ratio, _junk_hits = assess_extract_quality(text)
                st.session_state["_extract_quality"] = {"junk_ratio": _junk_ratio, "junk_hits": _junk_hits, "len": len(text)}
                if _junk_ratio >= 0.35 or (_junk_hits >= 5 and len(text) < 1500):
                    st.warning(
                        "⚠️ 본문 추출 품질이 낮습니다. 블로그 사이드바·댓글·최근글이 포함된 것 같아요.\n\n"
                        "👉 더 정확한 분석을 원하면 **본문만 복사해서 '글 붙여넣기로 조회하기'**로 다시 시도해보세요."
                    )
        else:
            text = clean_text(pasted_text.strip())[:MAX_EXTRACT_TEXT_CHARS]
            err = ""
            final_url = f"pasted://{datetime.now().strftime('%Y%m%d%H%M%S')}"
            analysis_source = "사용자 붙여넣기 글"

        if err:
            st.error(err)
        elif not text or len(text) < 100:
            st.error("본문을 충분히 확보할 수 없어요.")
            if text:
                st.text(text[:1000])
        else:
            cache_key = f"{final_url}::{selected_type}::{input_mode}::{EXTRACTION_VERSION}"
            st.session_state["_last_cache_key"] = cache_key
            if cache_key in st.session_state.analysis_cache:
                result = st.session_state.analysis_cache[cache_key]
                st.session_state.last_result = result
                st.session_state.last_final_url = final_url
                st.session_state.last_text = text
                st.session_state.show_result = True
                st.session_state.result_closed = False
                st.session_state["analysis_status_message"] = "같은 조건(URL+유형+버전)의 분석 결과가 있어 캐시에서 불러왔어요."
            else:
                with st.spinner("AI가 분석 중..."):
                    try:
                        result = analyze_with_groq(text[:MAX_ANALYZE_CHARS], analysis_source, selected_type)
                        st.session_state.analysis_cache[cache_key] = result
                        st.session_state.last_result = result
                        st.session_state.last_final_url = final_url
                        st.session_state.last_text = text
                        st.session_state.show_result = True
                        st.session_state.result_closed = False
                        st.session_state["analysis_status_message"] = "분석 완료했어요. 아래에서 신뢰도 결과와 지식 메모 초안을 확인할 수 있어요."
                    except json.JSONDecodeError as e:
                        st.error(f"분석 결과 JSON 파싱 오류: {e}")
                    except Exception as e:
                        st.error(f"분석 중 오류 발생: {e}")
                        if "429" in str(e) or "사용량 제한" in str(e) or "Too Many Requests" in str(e):
                            st.info("Groq 제한에 걸렸어요. 잠시 후 다시 시도해주세요.")

            if st.session_state.get("last_result"):
                st.session_state.search_history.insert(
                    0,
                    {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "url": final_url,
                        "cache_key": cache_key,
                        "title": st.session_state.last_result.get("archive_title", "제목 없음"),
                        "content_type": st.session_state.last_result.get("content_type", "unknown"),
                        "score": st.session_state.last_result.get("trust_score", 0),
                        "source": "cache" if cache_key in st.session_state.analysis_cache else "new_analysis",
                        "input_mode": input_mode,
                    },
                )
                st.session_state.search_history = st.session_state.search_history[:30]
                save_persisted_data()

if st.session_state.get("analysis_status_message"):
    analysis_status_slot.info(st.session_state.get("analysis_status_message"))

if st.session_state.show_result and st.session_state.last_result:
    # ── STEP 2. 원문 확인 ──────────────────────────────
    _result_obj = st.session_state.last_result or {}
    # 원문 표시 변수 통일: 길이 metric과 textarea가 반드시 같은 _source_text 사용
    _src_candidates = [
        ("last_text", st.session_state.get("last_text")),
        ("session.original_text", st.session_state.get("original_text")),
        ("result.original_text", _result_obj.get("original_text")),
        ("result.text", _result_obj.get("text")),
        ("result.raw_text", _result_obj.get("raw_text")),
    ]
    _source_key = "(없음)"
    _source_text = ""
    for _k, _v in _src_candidates:
        if _v and str(_v).strip():
            _source_text = str(_v)
            _source_key = _k
            break
    _step2_url = st.session_state.get("last_final_url", "") or ""
    _step2_title = _result_obj.get("archive_title", "제목 없음")
    _step2_pasted = str(_step2_url).startswith("pasted://")
    _step2_len = len(_source_text)
    _step2_ok = _step2_len >= 100
    st.markdown("### 2️⃣ 원문 확인")
    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("추출 상태", "✅ 성공" if _step2_ok else "⚠️ 부족")
    _m2.metric("원문 길이", f"{_step2_len:,}자")
    _m3.metric("출처", "붙여넣기" if _step2_pasted else "URL")
    st.caption(f"📄 제목: {_step2_title}" + ("" if _step2_pasted else f" · 🔗 {_step2_url}"))
    st.success("💾 원문 전체가 메모 저장 시 `original_text`에 보관돼, 지식 AI가 깊게 읽을 수 있어요.")
    if _step2_len > 0 and not _source_text.strip():
        st.warning("원문 길이는 감지됐지만 표시용 원문을 찾지 못했어요. 변수 매핑을 확인해주세요.")
    with st.expander("원문 전체 보기 / 복사", expanded=False):
        # 확인/복사용(read-only) — key 미사용으로 value 바인딩 보장
        st.text_area("원문", value=_source_text, height=400, disabled=True,
                     label_visibility="collapsed")
    with st.expander("🛠️ 디버그 — 원문 변수 매핑", expanded=False):
        st.write(f"len(last_text) = {len(st.session_state.get('last_text','') or '')}")
        st.write(f"len(result.original_text) = {len(_result_obj.get('original_text','') or '')}")
        st.write(f"len(result.text) = {len(_result_obj.get('text','') or '')}")
        st.write(f"현재 사용 중인 source key = **{_source_key}** ({_step2_len:,}자)")
    st.markdown("### 3️⃣ AI 정리 · 신뢰도 판단")
    render_result(
        st.session_state.last_result,
        extracted_text=st.session_state.last_text if show_debug else None,
        final_url=st.session_state.last_final_url,
    )

st.markdown("---")
st.markdown(
    '<div style="text-align:center;color:#aaa;font-size:12px">'
    'TrustLens MVP · AI가 대신 생각하지 않는다. 더 나은 판단을 돕는다.'
    '</div>',
    unsafe_allow_html=True,
)
