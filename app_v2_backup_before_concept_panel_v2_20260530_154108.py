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

st.set_page_config(page_title="TrustLens", page_icon="🔍", layout="wide")

DATA_FILE = Path("trustlens_data.json")

def load_persisted_data():
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_persisted_data():
    data = {
        "archive_notes": st.session_state.get("archive_notes", []),
        "search_history": st.session_state.get("search_history", []),
        "saved_analyses": st.session_state.get("saved_analyses", []),
        "feedback_history": st.session_state.get("feedback_history", []),
        "analysis_cache": st.session_state.get("analysis_cache", {}),
        "draft_cache": st.session_state.get("draft_cache", {}),
        "auto_feedback_stats": st.session_state.get(
            "auto_feedback_stats", {}
        ),
        "custom_trust_criteria": st.session_state.get("custom_trust_criteria", []),
        "active_custom_criteria_titles": st.session_state.get("active_custom_criteria_titles", []),
    }
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        backup_dir = Path("trustlens_backups")
        backup_dir.mkdir(exist_ok=True)
        backup_file = backup_dir / f"trustlens_backup_{datetime.now().strftime('%Y%m%d')}.json"
        with backup_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"저장 파일을 쓰는 중 문제가 생겼어요: {e}")

st.markdown("""
<style>
:root {
    --main-blue: #1f3f91;
    --soft-blue: #eef4ff;
    --point-blue: #2f73ff;
    --text-main: #172033;
    --text-sub: #64748b;
    --card-border: #e7edf7;
    --bg-main: #f5f7fb;
}

.stApp { background: var(--bg-main); }
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1320px; }

section[data-testid="stSidebar"] { background: linear-gradient(180deg, #203f92 0%, #18357d 100%); }
section[data-testid="stSidebar"] * { color: white !important; }
section[data-testid="stSidebar"] div[role="radiogroup"] label {
    background: transparent;
    border-radius: 12px;
    padding: 6px 8px;
    margin-bottom: 4px;
    transition: background 0.15s ease;
}
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background: rgba(255,255,255,0.10); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
    background: rgba(255,255,255,0.18);
    font-weight: 800;
}

.sidebar-title { font-size: 24px; font-weight: 800; margin-bottom: 4px; }
.sidebar-subtitle { font-size: 13px; opacity: 0.82; margin-bottom: 28px; }
.hero-title { font-size: 30px; font-weight: 850; color: var(--text-main); margin-bottom: 8px; }
.hero-subtitle { color: var(--text-sub); font-size: 15px; margin-bottom: 24px; }

.input-shell, .side-help-card, .result-shell, .page-card {
    background: white;
    border: 1px solid var(--card-border);
    border-radius: 24px;
    padding: 26px;
    box-shadow: 0 10px 25px rgba(15, 23, 42, 0.04);
    margin-bottom: 20px;
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
.note-action-card h2,
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
</style>
""", unsafe_allow_html=True)

with st.sidebar:
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
            "🕘 최근 검색 기록",
        ],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.caption("MVP v3 · URL 분석 + 지식 아카이브")

# -----------------------------
# Session State
# -----------------------------
def init_state():
    persisted = load_persisted_data()
    defaults = {
        "last_result": None,
        "last_final_url": None,
        "last_text": "",
        "archive_notes": persisted.get("archive_notes", []),
        "saved_analyses": persisted.get("saved_analyses", []),
        "search_history": persisted.get("search_history", []),
        "show_result": False,
        "note_saved": False,
        "feedback_history": persisted.get("feedback_history", []),
        "analysis_cache": persisted.get("analysis_cache", {}),
        "draft_cache": persisted.get("draft_cache", {}),
        "auto_feedback_stats": persisted.get("auto_feedback_stats", {}),
        "custom_trust_criteria": persisted.get("custom_trust_criteria", []),
        "active_custom_criteria_titles": persisted.get("active_custom_criteria_titles", []),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


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
hydrate_last_result_from_cache()

st.markdown(
    '''
    <div class="page-card" style="margin-bottom:24px;">
        <div class="hero-title">🛡️ TrustLens</div>
        <div class="hero-subtitle">
            기사 신뢰도 분석을 넘어, 내가 저장한 정보와 생각을 연결하는 개인 AI 지식 아카이브
        </div>
        <div style="margin-top:14px;">
            <span class="tag-badge">신뢰도 분석</span>
            <span class="tag-badge">지식 메모</span>
            <span class="tag-badge">태그 연결</span>
            <span class="tag-badge">개인 AI 두뇌</span>
        </div>
    </div>
    ''',
    unsafe_allow_html=True,
)

# -----------------------------
# Basic Helpers
# -----------------------------
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

        candidates = []
        for selector in [
            "div.se-main-container",
            "div#postViewArea",
            "div.post_ct",
            "div.post-view",
            "article",
            "main",
            "body",
        ]:
            selected = soup.select_one(selector)
            if selected:
                candidates.append(selected.get_text("\n", strip=True))

        if candidates:
            text = max(candidates, key=len)
        else:
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)

        text = clean_text(text)
        if title:
            text = f"[페이지 제목]\n{title}\n\n[본문]\n{text}"
        return text[:6000], "", target_url
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
def analyze_with_groq(text, url, selected_type):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        content_type = infer_content_type_from_text(text, selected_type)
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
            "tags_positive": ["맛집", "속초", "리뷰"],
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

    if content_type == "review":
        result["score_breakdown"] = normalize_review_breakdown(result.get("score_breakdown", {}), text)

    result["content_type"] = content_type
    result["trust_score"] = calculate_score_by_type(result.get("score_breakdown", {}), content_type)
    if not result.get("archive_title"):
        result["archive_title"] = "TrustLens 분석 메모"
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
    skip_words = ["NAVER", "본문 바로가기", "로그아웃", "서비스", "댓글", "함께 볼만한 뉴스", "랭킹", "Copyright"]
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
    if not api_key:
        return make_local_content_note_draft(original_text, result, final_url, template_type, user_prompt)

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
{original_text[:4500]}

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
    st.session_state.archive_notes.append(
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
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    st.session_state.note_saved = True
    st.session_state.show_result = True
    save_persisted_data()


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

    st.rerun()


# -----------------------------
# Restore/Delete/Clear Functions
# -----------------------------

def restore_analysis_from_history(cache_key):
    cached = st.session_state.analysis_cache.get(cache_key)
    if cached:
        st.session_state.last_result = cached
        parts = cache_key.split("::")
        st.session_state.last_final_url = parts[0] if parts else ""
        st.session_state.last_text = ""
        st.session_state.show_result = True
        st.session_state.result_closed = False
        st.session_state["history_restored"] = True


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

    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("### 📋 점수 근거")
        st.caption("각 항목은 실제 점수 / 최대 점수 비율로 표시돼요.")
        items = get_score_items_for_type(content_type)
        for key, label, max_val in items:
            val = get_int_score(breakdown, key)
            ratio = val / max_val if max_val else 0
            st.markdown(f"**{label}** · {val}/{max_val}점 · {round(ratio * 100)}%")
            st.progress(float(ratio))

    with right:
        st.markdown('<div class="summary-box">', unsafe_allow_html=True)
        st.markdown('<div class="summary-title">💡 AI 핵심 요약</div>', unsafe_allow_html=True)
        if isinstance(summary, list):
            for s in summary:
                st.markdown(f"- {s}")
        else:
            for s in [x.strip() for x in str(summary).split(".") if x.strip()]:
                st.markdown(f"- {s}.")
        st.markdown('</div>', unsafe_allow_html=True)

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
        
        st.markdown("### 🧠 AI 학습 신호")

        if ratings:
            st.success(
                f"사용자들이 이 분석을 "
                f"평균 {avg_rating}/5 로 평가했어요."
            )
        else:
            st.caption(
                "아직 사용자 평가 데이터가 없어요."
            )
    st.divider()
    render_score_dashboard(breakdown, content_type)

    st.divider()
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

    with st.expander("✍️ 자세한 피드백 남기기"):
        st.text_area("틀렸거나 어색한 부분", placeholder="예: 맛집 후기인데 공식 출처 기준이 보이면 어색함 / 점수가 너무 낮음", height=90, key=wrong_key)
        st.text_area("자유 피드백", placeholder="TrustLens가 다음 분석에서 더 잘 판단했으면 하는 기준을 적어주세요.", height=90, key=memo_key)

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

    st.divider()
    st.markdown("### 🔎 판단 근거")
    tab1, tab2, tab3 = st.tabs(["원문 근거", "작성자 분석", "광고 판단"])
    with tab1:
        st.markdown(f"**공식 출처 근거:** {evidence.get('official_source', '없음')}")
        st.markdown(f"**경험 신호 근거:** {evidence.get('experience_signal', '없음')}")
        st.markdown(f"**단점/비판 신호:** {evidence.get('negative_signal', '없음')}")
    with tab2:
        st.markdown(f"**유형:** {author_type}")
        st.markdown(f"**판단 이유:** {result.get('author_reason', '')}")
    with tab3:
        st.markdown(f"**위험도:** {ad_emoji} {ad_text}")
        st.markdown(f"**판단 이유:** {result.get('ad_risk_reason', '')}")
        st.markdown(f"**광고 신호:** {evidence.get('ad_signal', '없음')}")

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

    draft_key = f"note_draft_{final_url or 'current'}"
    note_key = f"edited_{draft_key}"

    if draft_key not in st.session_state:
        original_text_for_draft = st.session_state.get("last_text", "")
        if original_text_for_draft:
            st.session_state[draft_key] = make_local_content_note_draft(
                original_text_for_draft,
                result,
                final_url,
                "보고서 형식",
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

    note_panel, save_panel = st.columns(2, gap="large")

    with note_panel:
        st.markdown(
            '<div class="note-action-card"><h2>🤖 AI 메모 초안 생성</h2><p>AI 초안을 만들고 수정해서 긴 메모로 저장해요.</p></div>',
            unsafe_allow_html=True,
        )
        template_type = st.selectbox(
            "AI 초안 템플릿 선택",
            ["보고서 형식", "일기 형식", "블로그 초안 형식", "체크리스트 형식", "자유 형식"],
            key=f"template_{final_url or 'current'}",
        )
        user_draft_prompt = st.text_area(
            "초안에 반영할 추가 요청",
            placeholder="예: 가격과 팁을 표로 정리해줘 / 블로그에 올릴 수 있게 정리해줘 / 내 말투처럼 자연스럽게 정리해줘",
            height=110,
            key=f"draft_prompt_{final_url or 'current'}",
        )
        st.markdown('<div class="ai-draft-button-scope big-action-button blue-action"></div>', unsafe_allow_html=True)
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
                draft_cache_key = f"{final_url or 'current'}::{template_type}::{user_draft_prompt.strip()}"
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
        st.markdown(
            '<div class="archive-action-card"><h2>📌 분석결과 저장</h2><p>지금 분석한 결과를 아카이브에 저장해요.</p></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="big-action-button red-action"></div>', unsafe_allow_html=True)
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
        if st.button(
            "🔴 분석결과 아카이브에 저장",
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
        if st.session_state.get("analysis_archive_saved"):
            st.success("분석결과 아카이브에 저장했어요.")
            st.session_state["analysis_archive_saved"] = False

    proj_col, sec_col = st.columns(2)
    with proj_col:
        st.text_input(
            "프로젝트명",
            value="기본 프로젝트",
            key="note_project_name",
        )
    with sec_col:
        st.text_input(
            "섹션명",
            value="일반",
            key="note_section_name",
        )
    st.markdown("## ✍️ 메모 초안 편집")
    st.caption("AI 초안을 기반으로 내 메모를 정리한 뒤, 맨 아래에서 지식 메모로 저장해요.")
    st.text_area("AI 초안 기반으로 내 메모 정리하기", height=700, key=note_key)

    bottom_save_col, bottom_close_col = st.columns(2)
    with bottom_save_col:
        st.button(
            "🗂️ 지식 메모 저장",
            key=f"save_{draft_key}",
            use_container_width=True,
            on_click=save_note_to_archive,
            args=(
                note_key,
                result,
                final_url,
                st.session_state.get(f"selected_tags_{final_url or 'current'}", []),
            ),
        )
    with bottom_close_col:
        st.button(
            "닫기 / 나가기",
            key=f"close_{draft_key}",
            use_container_width=True,
            on_click=close_current_result,
        )

    if st.session_state.get("note_saved"):
        st.success("지식 아카이브에 저장했어요.")
        st.session_state.note_saved = False

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
            "full_text": item.get("note", ""),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": f"item_{idx}_{len(items)}",
        })

    for idx, item in enumerate(st.session_state.get("saved_analyses", [])):
        items.append({
            "kind": "분석 결과",
            "title": item.get("title", "저장 분석"),
            "project": item.get("project", "분석결과"),
            "section": item.get("section", item.get("content_type", "일반")),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "tags": item.get("tags", []),
            "date": item.get("saved_at", ""),
            "memo": item.get("memo", ""),
            "full_text": "\n".join(item.get("summary", [])) if isinstance(item.get("summary", []), list) else str(item.get("summary", "")),
            "favorite": item.get("favorite", False),
            "raw_item": item,
            "raw_index": f"item_{idx}_{len(items)}",
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
    date_text = str(date_text or "").strip()
    if not date_text:
        return "날짜 없음"

    try:
        from datetime import datetime
        saved = datetime.strptime(date_text[:10], "%Y-%m-%d").date()
        today = datetime.now().date()
        diff = (today - saved).days
    except Exception:
        return "날짜 없음"

    if diff < 0:
        return "오늘/어제"
    if diff <= 1:
        return "오늘/어제"
    if diff <= 7:
        return "최근 7일"
    if diff <= 30:
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
        st.markdown(str(item.get("memo") or item.get("full_text") or "메모 내용이 없어요.")[:3500])


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
                    use_container_width=True,
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

    from collections import Counter

    st.markdown("### 🧠 핵심 개념 허브")
    st.caption("내 문서에서 가장 자주 등장하는 태그와 개념을 모아 보여줘요.")

    concept_counter = Counter()

    for item in items:
        for tag in item.get("tags", []):
            clean = str(tag).replace("#", "").strip()
            if clean:
                concept_counter[clean] += 1

        combined_text = " ".join([
            str(item.get("title", "")),
            str(item.get("memo", "")),
            str(item.get("full_text", "")),
        ])

        for concept in extract_local_concepts(combined_text, item.get("tags", []), limit=10):
            clean = str(concept).replace("#", "").strip()
            if clean:
                concept_counter[clean] += 1

    top_concepts = concept_counter.most_common(20)

    if "selected_concept" not in st.session_state:
        st.session_state.selected_concept = None

    if top_concepts:
        cols = st.columns(4)
        for idx, (concept, count) in enumerate(top_concepts):
            with cols[idx % 4]:
                st.markdown(
                    f"""
                    <div class="pkm-sidebar-card">
                        <div class="pkm-sidebar-title">🧠 {concept}</div>
                        <div class="pkm-sidebar-meta">{count}개 문서 연결</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button(
                    "관련 문서 보기",
                    key=f"concept_open_{idx}_{abs(hash(concept))}",
                    use_container_width=True,
                ):
                    st.session_state.selected_concept = concept
    else:
        st.caption("아직 태그/개념 데이터가 없어요.")

    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(["📚 원노트 목차", "🧩 노션 보드", "🕸️ 태그 마인드맵", "🧠 지식 페이지"])

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

        for large_idx, (large, middle_groups) in enumerate(sorted(grouped.items())):
            with st.expander(f"📒 {large} · {sum(len(v) for v in middle_groups.values())}개", expanded=True):
                for middle_idx, (middle, group) in enumerate(sorted(middle_groups.items())):
                    st.markdown(f'<span class="pkm-section-pill">📑 {middle}</span>', unsafe_allow_html=True)
                    for item_i, item in enumerate(group):
                        tag_text = ", ".join(item.get("tags", [])) or "태그 없음"
                        item_unique_key = item.get("raw_index", f"{large_idx}_{middle_idx}_{item_i}")
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

                for board_item_idx, item in enumerate(group[:12]):
                    tags = ", ".join(item.get("tags", [])[:3]) or "태그 없음"
                    st.markdown(
                        f"""
                        <div class="map-mini-card">
                            <div class="map-mini-title">{item.get("title", "제목 없음")}</div>
                            <div class="map-mini-meta">{item.get("kind")} · {item.get("score", 0)}점</div>
                            <div class="map-mini-meta">{tags}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    board_unique_key = item.get("raw_index", f"{name}_{board_item_idx}")
                    st.button(
                        "열기",
                        key=f"knowledge_board_open_{name}_{board_item_idx}_{board_unique_key}",
                        use_container_width=True,
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

# -----------------------------
# Menu Pages
# -----------------------------
if menu == "📊 분석 결과":
    st.markdown("## 📊 분석 결과")
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

if menu == "🔎 신뢰도 근거":
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

if menu == "🏷️ 분석결과 아카이브":
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
            or q in " ".join([str(t) for t in item.get("tags", [])]).lower()
        ]

    if only_favorite_analysis:
        analyses_to_show = [(idx, item) for idx, item in analyses_to_show if item.get("favorite", False)]

    if selected_archive_tag != "전체":
        analyses_to_show = [
            (idx, item) for idx, item in analyses_to_show
            if selected_archive_tag in [str(t).replace("#", "").strip() for t in item.get("tags", [])]
        ]

    if analyses_to_show:
        for display_idx, (original_index, item) in enumerate(analyses_to_show, start=1):
            star = "⭐" if item.get("favorite", False) else "☆"
            tags_text = ", ".join([str(t) for t in item.get("tags", [])]) or "태그 없음"
            with st.expander(f"{display_idx}. {star} {item.get('title', '저장 분석')} · {item.get('score', 0)}점 · {tags_text}", expanded=False):
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
                        st.rerun()
                with b4:
                    st.button("🗑️ 삭제", key=f"delete_saved_analysis_{original_index}", use_container_width=True, on_click=delete_saved_analysis, args=(original_index,))

                if st.session_state.get(f"saved_analysis_updated_{original_index}"):
                    st.success("분석 메모와 태그를 저장했어요.")
                    st.session_state[f"saved_analysis_updated_{original_index}"] = False
    else:
        st.info("아직 저장된 분석결과가 없어요. 분석 결과 하단의 '현재 분석결과 저장' 버튼으로 저장해보세요.")
    st.stop()

if menu == "🏷️ 태그 관리":
    st.markdown("## 🏷️ 태그 관리")
    all_tags = []
    for item in st.session_state.archive_notes:
        for tag in item.get("tags", []):
            clean = str(tag).replace("#", "").strip()
            if clean and clean not in all_tags:
                all_tags.append(clean)

    if all_tags:
        selected_tag = st.selectbox("태그를 선택하면 해당 메모만 볼 수 있어요", ["전체"] + all_tags)
        filtered_notes = st.session_state.archive_notes
        if selected_tag != "전체":
            filtered_notes = [note for note in st.session_state.archive_notes if selected_tag in [str(t).replace("#", "").strip() for t in note.get("tags", [])]]
        for idx, item in enumerate(filtered_notes, start=1):
            with st.expander(f"{idx}. {item.get('title', '저장 메모')} · {item.get('score', 0)}점", expanded=False):
                st.markdown(f"**출처:** {display_source_label(item.get('url', ''))}")
                st.markdown(f"**저장일:** {item.get('saved_at', '')}")
                original_index = st.session_state.archive_notes.index(item)
                title_key = f"tag_note_title_{original_index}_{selected_tag}"
                edit_key = f"tag_note_{original_index}_{selected_tag}"
                tags_key = f"tag_note_tags_{original_index}_{selected_tag}"
                new_tags_key = f"tag_note_new_tags_{original_index}_{selected_tag}"
                tag_options_for_edit = get_tag_edit_options(item)

                st.text_input("제목 수정", value=item.get("title", ""), key=title_key)
                st.multiselect(
                    "기존 태그 선택/삭제",
                    options=tag_options_for_edit,
                    default=[tag for tag in item.get("tags", []) if tag in tag_options_for_edit],
                    key=tags_key,
                    help="기존 태그를 선택/해제할 수 있어요.",
                )
                st.text_input(
                    "새 태그 추가",
                    placeholder="예: 맛집후보, 재확인필요 처럼 쉼표/엔터로 여러 개 입력/태그 입력 후 아래 버튼(태그수정 저장)을 누른 뒤 위 기존 태그 선택칸을 누르세요.",
                    key=new_tags_key,
                    help="입력 후 아래의 제목/태그/메모 수정 저장 버튼을 눌러야 반영돼요.",
                )
                st.text_area("메모 수정", value=item.get("note", ""), height=260, key=edit_key)
                if st.button(
                    "💾 제목/태그/메모 수정 저장",
                    key=f"save_tag_note_{original_index}_{selected_tag}",
                    use_container_width=True,
                ):
                    update_archive_note_and_tags(original_index, edit_key, tags_key, new_tags_key, title_key)
                    st.rerun()
                st.button(
                    "🗑️ 이 메모 삭제",
                    key=f"delete_tag_note_{original_index}_{selected_tag}",
                    use_container_width=True,
                    on_click=delete_archive_note,
                    args=(original_index,),
                )
                if st.session_state.get(f"archive_updated_{original_index}"):
                    st.success("수정한 메모를 저장했어요.")
                    st.session_state[f"archive_updated_{original_index}"] = False
    else:
        st.info("아직 저장된 태그가 없어요. 분석 결과에서 메모를 저장하면 태그가 생겨요.")
    st.stop()

if menu == "🗂️ 지식 아카이브":
    st.markdown("## 🗂️ 지식 아카이브")
    st.caption("분석 결과에서 저장한 메모가 여기에 쌓여요. 테스트 단계에서는 trustlens_data.json 파일에 저장돼서 재실행해도 유지돼요.")
    if st.session_state.get("archive_deleted"):
        st.success("저장된 메모를 삭제했어요.")
        st.session_state["archive_deleted"] = False
    search_query = st.text_input("🔍 아카이브 검색", placeholder="제목, URL, 태그, 메모 내용으로 검색")
    only_fav = st.checkbox("⭐ 즐겨찾기만 보기", value=False)

    notes_to_show = st.session_state.archive_notes
    if search_query.strip():
        q = search_query.strip().lower()
        notes_to_show = [
            note for note in notes_to_show
            if q in str(note.get("title", "")).lower()
            or q in str(note.get("url", "")).lower()
            or q in str(note.get("note", "")).lower()
            or q in " ".join([str(t) for t in note.get("tags", [])]).lower()
        ]

    if only_fav:
        notes_to_show = [note for note in notes_to_show if note.get("favorite", False)]

    if notes_to_show:
        for idx, item in enumerate(notes_to_show, start=1):
            tags_text = ", ".join([str(t) for t in item.get("tags", [])])
            with st.expander(f"{idx}. {item.get('title', '저장 메모')} · {item.get('content_type', '')} · {item.get('score', 0)}점 · {tags_text}", expanded=False):
                st.markdown(f"**출처:** {display_source_label(item.get('url', ''))}")
                st.markdown(f"**저장일:** {item.get('saved_at', '')}")
                st.markdown("**제목, 태그와 메모 수정**")
                original_index = st.session_state.archive_notes.index(item)
                title_key = f"archive_note_title_{original_index}"
                edit_key = f"archive_note_{original_index}"
                tags_key = f"archive_note_tags_{original_index}"
                new_tags_key = f"archive_note_new_tags_{original_index}"
                tag_options_for_edit = get_tag_edit_options(item)

                st.text_input("제목 수정", value=item.get("title", ""), key=title_key)
                st.multiselect(
                    "기존 태그 선택/삭제",
                    options=tag_options_for_edit,
                    default=[tag for tag in item.get("tags", []) if tag in tag_options_for_edit],
                    key=tags_key,
                    help="기존 기록의 태그를 선택/해제할 수 있어요.",
                )
                st.text_input(
                    "새 태그 추가",
                    placeholder="예: 맛집후보, 재확인필요 처럼 쉼표/엔터로 여러 개 입력/태그 입력 후 아래 버튼(태그수정 저장)을 누른 뒤 위 기존 태그 선택칸을 누르세요.",
                    key=new_tags_key,
                    help="입력 후 아래의 제목/태그/메모 수정 저장 버튼을 눌러야 반영돼요.",
                )
                st.text_area("저장된 메모 수정", value=item.get("note", ""), height=320, key=edit_key)
                fav_label = "⭐ 즐겨찾기 해제" if item.get("favorite", False) else "☆ 즐겨찾기"
                st.button(
                    fav_label,
                    key=f"favorite_archive_note_{original_index}",
                    use_container_width=True,
                    on_click=toggle_archive_favorite,
                    args=(original_index,),
                )
                if st.button(
                    "💾 제목/태그/메모 수정 저장",
                    key=f"save_archive_note_{original_index}",
                    use_container_width=True,
                ):
                    update_archive_note_and_tags(original_index, edit_key, tags_key, new_tags_key, title_key)
                    st.rerun()
                st.button(
                    "🗑️ 이 메모 삭제",
                    key=f"delete_archive_note_{original_index}",
                    use_container_width=True,
                    on_click=delete_archive_note,
                    args=(original_index,),
                )
                if st.session_state.get(f"archive_updated_{original_index}"):
                    st.success("수정한 메모를 저장했어요.")
                    st.session_state[f"archive_updated_{original_index}"] = False
    else:
        st.info("아직 저장된 메모가 없어요. 분석 결과 하단에서 메모를 저장해보세요.")
    st.stop()

if menu == "🧠 지식 맵":
    render_knowledge_map_page()
    st.stop()

if menu == "🕘 최근 검색 기록":
    st.markdown("## 🕘 최근 검색 기록")
    st.caption("같은 URL은 저장된 캐시를 불러와서 API 호출 없이 다시 볼 수 있어요.")

    if st.session_state.get("history_restored"):
        st.success("저장된 분석 결과를 다시 불러왔어요. 왼쪽 메뉴의 📊 분석 결과에서 확인할 수 있어요.")
        st.session_state["history_restored"] = False

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
    st.divider()
    st.markdown("## 💾 로컬 백업")
    st.caption("저장 데이터는 trustlens_data.json에 저장되고, 날짜별 백업은 trustlens_backups 폴더에 생성돼요.")

    with st.expander("⚠️ 테스트 데이터 전체 초기화"):
        st.warning("지식 아카이브, 검색 기록, 피드백, 분석 캐시, 초안 캐시가 모두 삭제돼요.")
        st.button(
            "🧹 전체 저장 데이터 초기화",
            key="clear_all_saved_data",
            type="primary",
            use_container_width=True,
            on_click=clear_all_saved_data,
        )
    st.stop()

# -----------------------------
# Main Input Page
# -----------------------------
left_col, right_col = st.columns([1.35, 1])

with left_col:
    st.markdown('<div class="input-shell">', unsafe_allow_html=True)
    st.markdown('<span class="progress-pill">1 / 1</span>', unsafe_allow_html=True)
    st.markdown('<div class="progress-line"><div class="progress-fill"></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="question-title">분석할 정보 유형과 입력 방식을 선택해주세요</div>', unsafe_allow_html=True)
    st.markdown('<div class="question-subtitle">맛집 후기와 정책 정보는 신뢰도 기준이 다르게 적용돼요.</div>', unsafe_allow_html=True)

    selected_type_label = st.radio(
        "콘텐츠 유형",
        ["자동 판단", "맛집/제품/장소 후기", "정책/지원사업/공공정보", "일반 정보글"],
        horizontal=False,
    )
    selected_type_map = {
        "자동 판단": "unknown",
        "맛집/제품/장소 후기": "review",
        "정책/지원사업/공공정보": "policy",
        "일반 정보글": "info",
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
        else:
            text = clean_text(pasted_text.strip())[:6000]
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
            cache_key = f"{final_url}::{selected_type}::{input_mode}"
            if cache_key in st.session_state.analysis_cache:
                result = st.session_state.analysis_cache[cache_key]
                st.session_state.last_result = result
                st.session_state.last_final_url = final_url
                st.session_state.last_text = text
                st.session_state.show_result = True
                st.session_state.result_closed = False
                st.session_state["analysis_status_message"] = "같은 조건의 분석 결과가 있어서 저장된 결과를 다시 불러왔어요."
            else:
                with st.spinner("AI가 분석 중..."):
                    try:
                        result = analyze_with_groq(text, analysis_source, selected_type)
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