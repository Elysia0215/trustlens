import streamlit as st
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
import json

load_dotenv()

st.set_page_config(page_title="TrustLens", page_icon="🔍", layout="centered")

st.markdown("""
<style>
    .score-box {
        background: #f0f4ff;
        border-left: 4px solid #2E75B6;
        padding: 16px 20px;
        border-radius: 0 8px 8px 0;
        margin: 8px 0;
    }
    .tag {
        display: inline-block;
        background: #e8f0fe;
        color: #1a56db;
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 13px;
        margin: 3px;
    }
    .tag-warn { background: #fff3e0; color: #b85c00; }
</style>
""", unsafe_allow_html=True)

st.markdown("# 🔍 TrustLens")
st.markdown("**AI 정보 신뢰도 분석기** — 이 URL, 믿어도 될까?")
st.markdown("---")


def extract_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines)[:4000], ""
    except requests.exceptions.Timeout:
        return "", "페이지 로딩 시간이 초과됐어요."
    except Exception as e:
        return "", f"본문 추출 실패: {e}"


def analyze_with_groq(text, url):
    api_key = os.getenv("GROQ_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    prompt = f"""아래는 웹페이지 본문이야. 분석하고 반드시 JSON만 반환해.
다른 텍스트, 설명, 마크다운 코드블록 없이 순수 JSON만 반환해.

URL: {url}
본문: {text}

[광고 판단 기준 - 매우 중요]
- 광고(high): "협찬", "체험단", "제공받았습니다", "쿠팡파트너스", "이 포스팅은 제품을 제공받아" 등 명시적 키워드가 있을 때만
- 주의(mid): 광고 키워드는 없지만 단점이 전혀 없고 과도하게 긍정적일 때
- 낮음(low): 개인 경험담 형식으로 작성되고 장단점이 균형잡혀 있을 때
- 플랫폼(네이버 블로그, 티스토리 등) 자체만으로 광고로 판단하지 말 것
- 정리된 포맷(총평, 재방문 의사 등)은 광고가 아니라 성실한 후기의 특징임
- 실제 경험 묘사("웨이팅 없었음", "남자친구도 리필함" 등)가 있으면 실사용 후기로 볼 것

반환할 JSON:
{{
  "trust_score": 0에서 100 사이 정수,
  "score_breakdown": {{
    "official_source": 0에서 20,
    "recency": 0에서 15,
    "source_diversity": 0에서 12,
    "ad_free": 0에서 10,
    "info_density": 0에서 8,
    "revisit_mention": 0에서 5
  }},
  "ad_risk": "low" 또는 "mid" 또는 "high",
  "ad_risk_reason": "광고 위험도 판단 이유 한 문장",
  "author_type": "기록형" 또는 "객관형" 또는 "비판형" 또는 "홍보형",
  "author_reason": "작성자 유형 판단 이유 한 문장",
  "is_official": true 또는 false,
  "official_org": "공식 기관명 또는 빈 문자열",
  "tags_positive": ["긍정 태그 최대 4개"],
  "tags_warning": ["주의 태그 최대 3개"],
  "summary": "핵심 내용 3문장 요약"
}}"""

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.3
    }
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=30)
    res.raise_for_status()
    raw = res.json()["choices"][0]["message"]["content"].strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def render_result(result):
    score = result.get("trust_score", 0)
    breakdown = result.get("score_breakdown", {})
    ad_risk = result.get("ad_risk", "mid")
    author_type = result.get("author_type", "-")
    is_official = result.get("is_official", False)
    official_org = result.get("official_org", "")
    tags_pos = result.get("tags_positive", [])
    tags_warn = result.get("tags_warning", [])
    summary = result.get("summary", "")

    st.markdown("### 📊 신뢰도 분석 결과")
    col1, col2, col3 = st.columns(3)

    with col1:
        color = "#1a6b3c" if score >= 70 else ("#b85c00" if score >= 40 else "#8b1a1a")
        st.markdown(f'<div style="text-align:center;font-size:52px;font-weight:700;color:{color}">{score}</div><div style="text-align:center;color:#666;font-size:13px">신뢰도 점수</div>', unsafe_allow_html=True)

    with col2:
        ad_emoji = {"low": "🟢", "mid": "🟡", "high": "🔴"}.get(ad_risk, "⚪")
        ad_text = {"low": "낮음", "mid": "주의", "high": "위험"}.get(ad_risk, "-")
        ad_color = {"low": "#1a6b3c", "mid": "#b85c00", "high": "#8b1a1a"}.get(ad_risk, "#666")
        st.markdown(f'<div style="text-align:center;font-size:32px">{ad_emoji}</div><div style="text-align:center;font-size:16px;font-weight:600;color:{ad_color}">{ad_text}</div><div style="text-align:center;color:#666;font-size:13px">광고 위험도</div>', unsafe_allow_html=True)

    with col3:
        type_emoji = {"기록형": "📝", "객관형": "📊", "비판형": "🔍", "홍보형": "📣"}.get(author_type, "❓")
        st.markdown(f'<div style="text-align:center;font-size:32px">{type_emoji}</div><div style="text-align:center;font-size:16px;font-weight:600">{author_type}</div><div style="text-align:center;color:#666;font-size:13px">작성자 유형</div>', unsafe_allow_html=True)

    st.markdown("---")

    if is_official and official_org:
        st.success(f"✅ 공식 출처 확인됨 — {official_org}")

    with st.expander("📋 점수 근거 보기"):
        items = [
            ("official_source", "공식 출처 포함", 20),
            ("recency", "정보 최신성", 15),
            ("source_diversity", "출처 다양성", 12),
            ("ad_free", "광고 문구 없음", 10),
            ("info_density", "정보 밀도", 8),
            ("revisit_mention", "재방문 경험 언급", 5),
        ]
        for key, label, max_val in items:
            val = breakdown.get(key, 0)
            st.markdown(f'<div class="score-box">+{val}점 / {max_val}점 최대 &nbsp;&nbsp; <b>{label}</b></div>', unsafe_allow_html=True)

    with st.expander("👤 작성자 성향 분석"):
        st.markdown(f"**유형:** {author_type}")
        st.markdown(f"**판단 이유:** {result.get('author_reason', '')}")

    with st.expander("⚠️ 광고 위험도 판단 근거"):
        st.markdown(f"**위험도:** {ad_emoji} {ad_text}")
        st.markdown(f"**판단 이유:** {result.get('ad_risk_reason', '')}")

    st.markdown("---")
    st.markdown("### 🏷️ AI 태그")
    tag_html = "".join([f'<span class="tag">#{t}</span>' for t in tags_pos])
    tag_html += "".join([f'<span class="tag tag-warn">⚠ #{t}</span>' for t in tags_warn])
    st.markdown(f'<div style="line-height:2">{tag_html}</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### 💡 AI 핵심 요약")
    for s in [s.strip() for s in summary.replace(". ", ".|").split("|") if s.strip()]:
        st.markdown(f"- {s}{'.' if not s.endswith('.') else ''}")


url_input = st.text_input("🔗 분석할 URL을 입력하세요", placeholder="https://example.com/article")
analyze_btn = st.button("🔍 신뢰도 분석 시작", type="primary", use_container_width=True)

if analyze_btn:
    if not url_input.strip():
        st.warning("URL을 입력해주세요.")
    elif not url_input.startswith("http"):
        st.warning("http:// 또는 https://로 시작하는 URL을 입력해주세요.")
    elif not os.getenv("GROQ_API_KEY"):
        st.error("API 키가 없어요. .env 파일에 GROQ_API_KEY를 입력해주세요.")
    else:
        with st.spinner("본문 추출 중..."):
            text, err = extract_text(url_input.strip())
        if err:
            st.error(err)
        elif not text:
            st.error("본문을 추출할 수 없어요.")
        else:
            with st.spinner("AI가 분석 중... (5~10초 소요)"):
                try:
                    result = analyze_with_groq(text, url_input.strip())
                    render_result(result)
                except json.JSONDecodeError:
                    st.error("분석 결과 파싱 오류. 다시 시도해보세요.")
                except Exception as e:
                    st.error(f"분석 중 오류 발생: {e}")

st.markdown("---")
st.markdown('<div style="text-align:center;color:#aaa;font-size:12px">TrustLens MVP · AI가 대신 생각하지 않는다. 더 나은 판단을 돕는다.</div>', unsafe_allow_html=True)
