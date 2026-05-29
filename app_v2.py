import streamlit as st
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
import json
import re
from urllib.parse import urlparse
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
        "feedback_history": st.session_state.get("feedback_history", []),
        "analysis_cache": st.session_state.get("analysis_cache", {}),
        "draft_cache": st.session_state.get("draft_cache", {}),
    }
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
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

.help-grid-card { background:#fbfdff; border:1px solid #e5edf8; border-radius:18px; padding:18px; min-height:118px; }
.help-grid-title { font-weight:850; color:#172033; margin-bottom:6px; }
.help-grid-desc { color:#8fa1b8; font-size:13px; line-height:1.45; }

.history-item { background:#f8fbff; border:1px solid #e5edf8; border-radius:14px; padding:14px 16px; margin:8px 0; }
.history-title { font-weight:800; color:#172033; }
.history-meta { color:#64748b; font-size:13px; margin-top:4px; }
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
            "🏷️ 태그 관리",
            "🗂️ 지식 아카이브",
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
        "search_history": persisted.get("search_history", []),
        "show_result": False,
        "note_saved": False,
        "feedback_history": persisted.get("feedback_history", []),
        "analysis_cache": persisted.get("analysis_cache", {}),
        "draft_cache": persisted.get("draft_cache", {}),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_state()

st.markdown('<div class="hero-title">안녕하세요 👋</div>', unsafe_allow_html=True)
st.markdown('<div class="hero-subtitle">URL을 입력하면 콘텐츠 유형에 맞춰 신뢰도, 광고 위험도, 작성자 성향을 분석해드릴게요.</div>', unsafe_allow_html=True)

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

# -----------------------------
# AI Functions
# -----------------------------
def analyze_with_groq(text, url, selected_type):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY가 없습니다.")

    prompt = f"""
너는 TrustLens라는 정보 신뢰도 분석 서비스의 AI 분석 엔진이다.
아래 웹페이지 본문을 분석하고 반드시 순수 JSON만 반환해라.
마크다운, 설명문, 코드블록을 절대 붙이지 마라.

URL:
{url}

사용자가 선택한 콘텐츠 유형:
{selected_type}

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
- URL: {final_url or ""}
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


def generate_note_draft_with_groq(original_text, result, final_url, template_type, user_prompt):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY가 없습니다.")

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
            "content_type": result.get("content_type", "unknown"),
            "score": result.get("trust_score", 0),
            "tags": selected_tags,
            "note": note_text,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    st.session_state.note_saved = True
    st.session_state.show_result = True
    save_persisted_data()

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

# -----------------------------
# User Feedback Save Function
# -----------------------------
def save_user_feedback(result, final_url, rating_key, useful_key, wrong_key, missing_key, memo_key):
    rating = st.session_state.get(rating_key, 3)
    useful_points = st.session_state.get(useful_key, [])
    wrong_points = st.session_state.get(wrong_key, "")
    missing_points = st.session_state.get(missing_key, "")
    feedback_memo = st.session_state.get(memo_key, "")

    feedback = {
        "url": final_url or "",
        "title": result.get("archive_title", "TrustLens 분석"),
        "content_type": result.get("content_type", "unknown"),
        "score": result.get("trust_score", 0),
        "rating": rating,
        "useful_points": useful_points,
        "wrong_points": wrong_points,
        "missing_points": missing_points,
        "feedback_memo": feedback_memo,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    st.session_state.feedback_history.insert(0, feedback)
    st.session_state["feedback_saved"] = True
    save_persisted_data()


def close_current_result():
    st.session_state.show_result = False
    st.session_state.last_result = None
    st.session_state.last_final_url = None
    st.session_state.last_text = ""

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
        f'<div class="chart-subtitle">막대그래프, 점수 비중, 세부 점수표를 한 번에 확인해요.</div>',
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

    c1, c2 = st.columns([1, 1.15])
    with c1:
        pie_fig = px.pie(df, values="점수", names="항목", title="점수 비중", hole=0.52)
        pie_fig.update_layout(
            template="plotly_white",
            paper_bgcolor="#ffffff",
            plot_bgcolor="#ffffff",
            font=dict(color="#172033", size=13),
            title=dict(font=dict(size=18, color="#172033"), x=0.03),
            height=340,
            margin=dict(l=10, r=135, t=70, b=20),
            showlegend=True,
            legend=dict(font=dict(color="#172033", size=12), orientation="v", x=1.02, y=0.5),
        )
        st.plotly_chart(pie_fig, use_container_width=True)
    with c2:
        table_df = df.copy()
        table_df["표시"] = table_df.apply(lambda row: f"{int(row['점수'])} / {int(row['최대점수'])}점 ({row['달성률']}%)", axis=1)
        st.markdown("#### 세부 점수표")
        st.dataframe(table_df[["항목", "표시"]], use_container_width=True, hide_index=True)

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

    st.markdown('<div class="result-shell">', unsafe_allow_html=True)
    st.markdown("## 📊 신뢰도 분석 결과")
    if final_url:
        st.caption(f"분석 URL: {final_url}")

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

    st.divider()
    render_score_dashboard(breakdown, content_type)

    st.divider()
    st.markdown('<div class="feedback-shell">', unsafe_allow_html=True)
    st.markdown("### ⭐ 사용자 피드백으로 TrustLens 개선하기")
    st.caption("분석 결과가 맞았는지 바로 평가해주세요. 이 피드백은 이후 점수 기준, 태그 추천, AI 초안 개선 데이터로 쌓입니다.")

    feedback_base = final_url or "current"
    rating_key = f"feedback_rating_{feedback_base}"
    useful_key = f"feedback_useful_{feedback_base}"
    wrong_key = f"feedback_wrong_{feedback_base}"
    missing_key = f"feedback_missing_{feedback_base}"
    memo_key = f"feedback_memo_{feedback_base}"

    quick_col1, quick_col2, quick_col3 = st.columns([0.8, 1.1, 1.1])
    with quick_col1:
        st.slider("만족도", min_value=1, max_value=5, value=4, key=rating_key)
    with quick_col2:
        st.multiselect(
            "도움 된 부분",
            ["신뢰도 점수", "광고 위험도", "작성자 유형", "핵심 요약", "AI 메모 초안", "태그 추천", "차트 시각화"],
            default=["핵심 요약", "차트 시각화"],
            key=useful_key,
        )
    with quick_col3:
        st.text_area("추가 필요/아쉬운 점", placeholder="예: 사진 개수 반영, 점수 기준 설명 강화 등", height=96, key=missing_key)

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
    st.markdown("### 📝 내 지식 아카이브용 메모")
    st.caption("원문 전체 기반 AI 초안을 만들고, 내가 수정한 뒤 태그와 함께 저장해요.")

    tag_options = []
    for tag in result.get("tags_positive", []) + result.get("tags_warning", []):
        clean_tag = str(tag).replace("#", "").strip()
        if clean_tag and clean_tag not in tag_options:
            tag_options.append(clean_tag)

    selected_tags = st.multiselect("저장할 태그 선택", options=tag_options, default=tag_options, key=f"selected_tags_{final_url or 'current'}")
    template_type = st.selectbox("AI 초안 템플릿 선택", ["보고서 형식", "일기 형식", "블로그 초안 형식", "체크리스트 형식", "자유 형식"], key=f"template_{final_url or 'current'}")
    user_draft_prompt = st.text_area("초안에 반영할 추가 요청", placeholder="예: 가격과 팁을 표로 정리해줘 / 블로그에 올릴 수 있게 정리해줘 / 내 말투처럼 자연스럽게 정리해줘", height=90, key=f"draft_prompt_{final_url or 'current'}")

    draft_key = f"note_draft_{final_url or 'current'}"
    note_key = f"edited_{draft_key}"

    if draft_key not in st.session_state:
        # 분석 직후 자동으로 Groq를 한 번 더 호출하면 무료 한도/분당 제한에 쉽게 걸릴 수 있음.
        # 그래서 기본 초안은 로컬에서 즉시 만들고, 사용자가 버튼을 누를 때만 AI 초안을 다시 생성한다.
        st.session_state[draft_key] = make_basic_note_draft(result, final_url, selected_tags)

    if note_key not in st.session_state:
        st.session_state[note_key] = st.session_state[draft_key]

    if st.button("✨ 원문 전체 기반 AI 초안 만들기 / 다시 만들기", key=f"refresh_{draft_key}", type="primary", use_container_width=True):
        original_text = st.session_state.get("last_text", "")
        if not original_text:
            st.warning("원문이 저장되어 있지 않아요. URL을 다시 분석한 뒤 초안을 만들어주세요.")
        else:
            with st.spinner("원문 전체를 보고 AI가 메모 초안을 만드는 중..."):
                try:
                    new_draft = generate_note_draft_with_groq(original_text, result, final_url, template_type, user_draft_prompt)
                    st.session_state[draft_key] = new_draft
                    st.session_state[note_key] = new_draft
                    st.success("AI 초안을 만들었어요. 아래 메모창에서 수정 후 저장할 수 있어요.")
                except Exception as e:
                    st.error(f"AI 초안 생성 중 오류 발생: {e}")

    st.text_area("AI 초안 기반으로 내 메모 정리하기", height=460, key=note_key)

    save_col, close_col = st.columns(2)
    with save_col:
        st.button("🗂️ 이 메모 저장하기", key=f"save_{draft_key}", use_container_width=True, on_click=save_note_to_archive, args=(note_key, result, final_url, selected_tags))
    with close_col:
        st.button("닫기 / 나가기", key=f"close_{draft_key}", use_container_width=True, on_click=close_current_result)

    if st.session_state.get("note_saved"):
        st.success("지식 아카이브에 저장했어요. 왼쪽 메뉴의 🗂️ 지식 아카이브에서 확인할 수 있어요.")
        st.session_state.note_saved = False

    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

# -----------------------------
# Menu Pages
# -----------------------------
if menu == "📊 분석 결과":
    st.markdown("## 📊 분석 결과")
    if st.session_state.last_result:
        render_result(st.session_state.last_result, extracted_text=None, final_url=st.session_state.last_final_url)
    else:
        st.info("아직 분석 결과가 없습니다. 먼저 URL을 분석해주세요.")
    st.stop()

if menu == "🔎 신뢰도 근거":
    st.markdown("## 🔎 신뢰도 근거")
    if st.session_state.last_result:
        evidence = st.session_state.last_result.get("evidence", {})
        st.markdown(f"**공식 출처 근거:** {evidence.get('official_source', '없음')}")
        st.markdown(f"**광고 판단 근거:** {evidence.get('ad_signal', '없음')}")
        st.markdown(f"**경험 신호 근거:** {evidence.get('experience_signal', '없음')}")
        st.markdown(f"**단점/비판 신호:** {evidence.get('negative_signal', '없음')}")
    else:
        st.info("먼저 URL을 분석해주세요.")
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
                st.markdown(f"**URL:** {item.get('url', '')}")
                st.markdown(f"**저장일:** {item.get('saved_at', '')}")
                st.markdown(f"**태그:** {', '.join(item.get('tags', []))}")
                original_index = st.session_state.archive_notes.index(item)
                edit_key = f"tag_note_{original_index}_{selected_tag}"
                st.text_area("메모 수정", value=item.get("note", ""), height=260, key=edit_key)
                st.button(
                    "💾 수정 내용 저장",
                    key=f"save_tag_note_{original_index}_{selected_tag}",
                    use_container_width=True,
                    on_click=update_archive_note,
                    args=(original_index, edit_key),
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
    if st.session_state.archive_notes:
        for idx, item in enumerate(st.session_state.archive_notes, start=1):
            tags_text = ", ".join([str(t) for t in item.get("tags", [])])
            with st.expander(f"{idx}. {item.get('title', '저장 메모')} · {item.get('content_type', '')} · {item.get('score', 0)}점 · {tags_text}", expanded=False):
                st.markdown(f"**URL:** {item.get('url', '')}")
                st.markdown(f"**저장일:** {item.get('saved_at', '')}")
                st.markdown("**메모**")
                original_index = idx - 1
                edit_key = f"archive_note_{original_index}"
                st.text_area("저장된 메모 수정", value=item.get("note", ""), height=320, key=edit_key)
                st.button(
                    "💾 수정 내용 저장",
                    key=f"save_archive_note_{original_index}",
                    use_container_width=True,
                    on_click=update_archive_note,
                    args=(original_index, edit_key),
                )
                if st.session_state.get(f"archive_updated_{original_index}"):
                    st.success("수정한 메모를 저장했어요.")
                    st.session_state[f"archive_updated_{original_index}"] = False
    else:
        st.info("아직 저장된 메모가 없어요. 분석 결과 하단에서 메모를 저장해보세요.")
    st.stop()

if menu == "🕘 최근 검색 기록":
    st.markdown("## 🕘 최근 검색 기록")
    if st.session_state.search_history:
        for idx, item in enumerate(st.session_state.search_history[:20], start=1):
            st.markdown(
                f'''
                <div class="history-item">
                    <div class="history-title">{idx}. {item.get("title", "제목 없음")}</div>
                    <div class="history-meta">{item.get("time", "")} · {item.get("content_type", "unknown")} · {item.get("score", 0)}점</div>
                    <div class="history-meta">{item.get("url", "")}</div>
                </div>
                ''',
                unsafe_allow_html=True,
            )
    else:
        st.info("아직 검색 기록이 없어요.")
    st.divider()
    st.markdown("## 📩 사용자 피드백 기록")
    if st.session_state.feedback_history:
        for idx, item in enumerate(st.session_state.feedback_history[:20], start=1):
            st.markdown(
                f'''
                <div class="history-item">
                    <div class="history-title">{idx}. {item.get("title", "피드백")}</div>
                    <div class="history-meta">{item.get("saved_at", "")} · 만족도 {item.get("rating", "-")} / 5 · {item.get("content_type", "unknown")}</div>
                    <div class="history-meta">도움 된 부분: {", ".join(item.get("useful_points", [])) or "없음"}</div>
                    <div class="history-meta">보완 요청: {item.get("missing_points", "없음") or "없음"}</div>
                    <div class="history-meta">틀렸거나 어색한 부분: {item.get("wrong_points", "없음") or "없음"}</div>
                </div>
                ''',
                unsafe_allow_html=True,
            )
    else:
        st.info("아직 저장된 사용자 피드백이 없어요.")
    st.stop()

# -----------------------------
# Main Input Page
# -----------------------------
left_col, right_col = st.columns([1.35, 1])

with left_col:
    st.markdown('<div class="input-shell">', unsafe_allow_html=True)
    st.markdown('<span class="progress-pill">1 / 1</span>', unsafe_allow_html=True)
    st.markdown('<div class="progress-line"><div class="progress-fill"></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="question-title">분석할 정보 유형과 URL을 입력해주세요</div>', unsafe_allow_html=True)
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

    st.markdown(
        f'''
        <div class="choice-box">
            <div class="choice-box-title">현재 선택: {selected_type_label}</div>
            <div class="choice-box-desc">선택한 유형에 맞춰 점수 기준과 분석 근거가 다르게 적용돼요.</div>
        </div>
        ''',
        unsafe_allow_html=True,
    )

    url_input = st.text_input("🔗 분석할 URL", placeholder="https://example.com/article")
    show_debug = st.checkbox("추출 본문 디버그 보기", value=False)
    analyze_btn = st.button("🔍 신뢰도 분석 시작", type="primary", use_container_width=True)

    st.markdown("""
    <div class="info-note">
    💡 <b>정확한 답변이 도움이 됩니다</b><br>
    리뷰 글은 본인 경험, 가격, 메뉴, 사진, 재방문 의사를 중심으로 보고<br>
    정책 글은 공식 기관, 날짜, 신청 조건, 출처를 중심으로 분석해요.
    </div>
    """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

with right_col:
    st.markdown('<div class="side-help-card">', unsafe_allow_html=True)
    st.markdown("### ✨ 예상 결과 미리보기")
    st.caption("URL 분석 후 아래 항목들이 카드 형태로 제공돼요.")
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
    URL 분석 → 태그 추천 → AI 초안 생성 → 사용자 수정 → 저장 → 태그별 조회
    </div>
    """, unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

if analyze_btn:
    if not url_input.strip():
        st.warning("URL을 입력해주세요.")
    elif not url_input.startswith("http"):
        st.warning("http:// 또는 https://로 시작하는 URL을 입력해주세요.")
    elif not os.getenv("GROQ_API_KEY"):
        st.error("API 키가 없어요. .env 파일에 GROQ_API_KEY를 입력해주세요.")
    else:
        with st.spinner("본문 추출 중..."):
            text, err, final_url = extract_text(url_input.strip())

        if err:
            st.error(err)
        elif not text or len(text) < 100:
            st.error("본문을 충분히 추출할 수 없어요. 다른 URL을 넣어보세요.")
            if text:
                st.text(text[:1000])
        else:
            with st.spinner("AI가 분석 중..."):
                try:
                    result = analyze_with_groq(text, url_input.strip(), selected_type)
                    st.session_state.last_result = result
                    st.session_state.last_final_url = final_url
                    st.session_state.last_text = text
                    st.session_state.search_history.insert(
                        0,
                        {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "url": final_url,
                            "title": result.get("archive_title", "제목 없음"),
                            "content_type": result.get("content_type", "unknown"),
                            "score": result.get("trust_score", 0),
                        },
                    )
                    st.session_state.show_result = True
                except json.JSONDecodeError as e:
                    st.error(f"분석 결과 JSON 파싱 오류: {e}")
                except Exception as e:
                    st.error(f"분석 중 오류 발생: {e}")
                    if "429" in str(e) or "사용량 제한" in str(e) or "Too Many Requests" in str(e):
                        st.info("지금은 Groq 무료/분당 호출 제한에 걸린 상태예요. 1~3분 뒤 다시 시도하거나, AI 초안 생성 버튼은 분석 후 따로 눌러주세요.")

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