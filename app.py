import os
import re
import time
import uuid
from datetime import datetime

import gspread
import pandas as pd
import streamlit as st
from google import genai
from google.genai import errors, types
from google.oauth2.service_account import Credentials


# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(
    page_title="LH 전세사기 피해주택 매입 Q&A 챗봇",
    page_icon="🏠",
    layout="wide",
)

GEMINI_MODEL = st.secrets.get("GEMINI_MODEL", "gemini-3.6-flash")
VERIFY_ANSWER_WITH_SECOND_PASS = True
FALLBACK_PHRASE = "제공된 공고문 및 특별법에서 확인하기 어려운 내용입니다. 정확한 안내는 관련 담당 기관에 직접 확인해 주세요."

DOCUMENT_FRESHNESS_CAUTION = (
    "※ 답변은 「전세사기피해자 지원 및 주거안정에 관한 특별법」과 "
    "「LH 전세사기 피해주택 매입 통합 공고」를 기준으로 하며, "
    "이후 법령 개정이나 공고 변경 사항은 즉시 반영되지 않을 수 있습니다."
)

client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])


# =========================================================
# 개인정보 마스킹
# - 모델 답변에는 원문 질문을 사용하되,
# - Google Sheets에 저장할 때만 마스킹한다.
# =========================================================
def mask_personal_info(text: str) -> str:
    if not text:
        return ""

    masked = str(text)

    # 주민등록번호
    masked = re.sub(
        r"(?<!\d)\d{6}\s*-?\s*[1-4]\d{6}(?!\d)",
        "[주민등록번호]",
        masked,
    )

    # 휴대전화 / 대표적인 국내 전화번호
    masked = re.sub(
        r"(?<!\d)(?:01[016789]|02|0[3-6][1-5])[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)",
        "[전화번호]",
        masked,
    )

    # 이메일
    masked = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[이메일]",
        masked,
    )

    # 계좌번호: '계좌/계좌번호' 뒤에 이어지는 숫자열 중심으로 보수적으로 마스킹
    masked = re.sub(
        r"((?:계좌번호|계좌)\s*[:：]?\s*)(?:\d[\d\-\s]{7,}\d)",
        r"\1[계좌번호]",
        masked,
        flags=re.IGNORECASE,
    )

    # 상세 주소의 번지/건물번호 부분만 마스킹하여 시·군·구 수준은 남김
    # 예: '전주시 완산구 홍산로 158' -> '전주시 완산구 홍산로 [상세주소]'
    masked = re.sub(
        r"([가-힣A-Za-z0-9]+(?:로|길|동|읍|면)\s*)\d+(?:-\d+)?",
        r"\1[상세주소]",
        masked,
    )

    # 아파트/건물 동·호수
    masked = re.sub(
        r"(?<!\d)\d{1,4}\s*동\s*\d{1,5}\s*호(?!\d)",
        "[동·호수]",
        masked,
    )

    return masked


# =========================================================
# 공고문/특별법에 실제 기재된 연락처만 사용
# - 웹 검색 없음
# - notice.pdf 2쪽, law.pdf 3쪽 기반
# =========================================================
LH_CONTACTS = {
    "서울": {
        "office": "서울지역본부",
        "areas": "서울특별시",
        "purchase": ["02-2015-1030"],
        "supply": ["02-3416-3780", "02-3416-3688"],
    },
    "인천": {
        "office": "인천지역본부",
        "areas": "인천광역시, 경기도 부천시",
        "purchase": ["032-890-5315", "032-890-5317"],
        "supply": ["032-890-5319"],
    },
    "경기남부": {
        "office": "경기남부지역본부",
        "areas": "수원·성남·안양·평택·안산·과천·오산·군포·의왕·용인·안성·화성·광주·이천·여주·광명·시흥",
        "purchase": [
            "031-250-8119", "031-250-8118", "031-250-8157",
            "031-250-8170", "031-250-8317", "031-250-8137",
        ],
        "supply": ["031-250-6125", "031-250-6174"],
    },
    "경기북부": {
        "office": "경기북부지역본부",
        "areas": "의정부·포천·남양주·가평·구리·양주·동두천·연천·고양·파주·김포·하남·양평",
        "purchase": ["02-6040-1470", "02-6040-1471"],
        "supply": ["02-6363-0453"],
    },
    "부산울산": {
        "office": "부산울산지역본부",
        "areas": "부산광역시, 울산광역시",
        "purchase": ["051-796-6038", "051-796-6047", "051-796-6027", "051-460-5980"],
        "supply": ["051-796-6075", "051-796-6035"],
    },
    "대구경북": {
        "office": "대구경북지역본부",
        "areas": "대구광역시, 경상북도",
        "purchase": ["053-603-2958", "053-603-2744"],
        "supply": ["053-603-2732", "053-603-2745"],
    },
    "광주전남": {
        "office": "광주전남지역본부",
        "areas": "광주광역시, 전라남도",
        "purchase": ["062-360-3238", "062-360-3249"],
        "supply": ["062-360-3253", "062-360-3275"],
    },
    "대전충남": {
        "office": "대전충남지역본부",
        "areas": "대전광역시, 충청남도, 세종특별자치시",
        "purchase": ["042-470-0690"],
        "supply": ["042-470-0959", "042-470-0284"],
        "supply_note": "대전·충남",
        "sejong_supply": ["044-902-2326"],
    },
    "경남": {
        "office": "경남지역본부",
        "areas": "경상남도",
        "purchase": ["055-210-8576", "055-210-8658"],
        "supply": ["055-210-8668", "055-210-8463"],
    },
    "강원": {
        "office": "강원지역본부",
        "areas": "강원특별자치도",
        "purchase": ["033-258-4156"],
        "supply": [
            "춘천 033-258-4121",
            "원주 033-812-6775",
            "강릉 033-610-5165",
        ],
    },
    "충북": {
        "office": "충북지역본부",
        "areas": "충청북도",
        "purchase": ["043-901-4524"],
        "supply": ["043-901-4521", "043-901-4528"],
    },
    "전북": {
        "office": "전북지역본부",
        "areas": "전북특별자치도",
        "purchase": ["063-230-6257", "063-230-6238"],
        "supply": [
            "전주·완주 063-230-6224",
            "군산·익산 063-840-0919",
            "정읍·김제·남원 063-570-2314",
        ],
    },
    "제주": {
        "office": "제주지역본부",
        "areas": "제주특별자치도",
        "purchase": ["064-720-1036"],
        "supply": ["064-720-1035"],
    },
}

MOLIT_CONTACT = {
    "office": "국토교통부 피해지원총괄과",
    "phones": ["044-201-5233", "044-201-5234"],
}

GYEONGGI_SOUTH = [
    "수원", "성남", "안양", "평택", "안산", "과천", "오산", "군포", "의왕",
    "용인", "안성", "화성", "경기광주", "경기도 광주", "이천", "여주", "광명", "시흥",
]
GYEONGGI_NORTH = [
    "의정부", "포천", "남양주", "가평", "구리", "양주", "동두천", "연천", "고양",
    "파주", "김포", "하남", "양평",
]


def detect_region_key(text: str):
    t = (text or "").replace(" ", "")

    # 공고문상 부천은 인천지역본부 관할
    if "부천" in t:
        return "인천"

    if any(k.replace(" ", "") in t for k in GYEONGGI_SOUTH):
        return "경기남부"
    if any(k.replace(" ", "") in t for k in GYEONGGI_NORTH):
        return "경기북부"

    # '광주'는 광주광역시와 경기도 광주시가 충돌하므로 경기광주는 위에서 먼저 처리
    direct_rules = [
        (["서울"], "서울"),
        (["인천"], "인천"),
        (["부산", "울산"], "부산울산"),
        (["대구", "경북", "경상북도"], "대구경북"),
        (["광주광역시", "전남", "전라남도"], "광주전남"),
        (["대전", "충남", "충청남도", "세종"], "대전충남"),
        (["경남", "경상남도"], "경남"),
        (["강원"], "강원"),
        (["충북", "충청북도", "청주", "충주", "제천"], "충북"),
        (["전북", "전북특별자치도", "전주", "완주", "군산", "익산", "정읍", "김제", "남원"], "전북"),
        (["제주"], "제주"),
        (["광주"], "광주전남"),
    ]

    for keywords, key in direct_rules:
        if any(k.replace(" ", "") in t for k in keywords):
            return key

    # '경기/경기도'만 있는 경우 남·북부를 결정할 수 없음
    if "경기" in t or "경기도" in t:
        return "경기_모호"

    return None


def detect_contact_topic(text: str):
    t = (text or "").lower()

    supply_keywords = [
        "긴급주거", "주택공급", "우선공급", "전세임대", "임시거주", "공공임대",
        "대체 공공임대", "대체공공임대", "거주지원", "주거지원",
    ]
    purchase_keywords = [
        "매입", "사전협의", "실태조사", "감정평가", "감평", "매입요청", "매입 신청",
        "매입신청", "주택매입", "매입가능", "매입불가",
    ]
    law_keywords = [
        "특별법", "피해자 결정", "피해자등 결정", "이의신청", "위원회", "법 제",
        "전세사기피해자 요건", "피해자 요건",
    ]

    if any(k in t for k in supply_keywords):
        return "supply"
    if any(k in t for k in purchase_keywords):
        return "purchase"
    if any(k in t for k in law_keywords):
        return "law"
    return None


def make_contact_guidance(question: str, context_text: str = "") -> str:
    combined = f"{context_text}\n{question}".strip()
    topic = detect_contact_topic(combined)
    region_key = detect_region_key(combined)

    if topic == "law":
        phones = ", ".join(MOLIT_CONTACT["phones"])
        return (
            "\n\n---\n"
            "### 📞 문의처\n"
            f"- **{MOLIT_CONTACT['office']}**: {phones}"
        )

    if topic not in {"purchase", "supply"}:
        return (
            "\n\n---\n"
            "이 질문은 제공된 문서만으로 담당 기관을 안전하게 특정하기 어렵습니다. "
            "잘못된 기관 안내를 피하기 위해 임의의 전화번호를 제시하지 않습니다. "
            "`매입/사전협의`, `우선공급·긴급주거지원`, `특별법·피해자 결정` 중 어느 문의인지 알려주세요."
        )

    if region_key == "경기_모호":
        return (
            "\n\n---\n"
            "공고문에서는 경기도를 **경기남부지역본부와 경기북부지역본부로 나누고 있습니다.** "
            "피해주택이 있는 시·군을 알려주시면 공고문에 적힌 담당 전화번호를 정확히 안내할 수 있습니다."
        )

    if region_key is None:
        return (
            "\n\n---\n"
            "공고문상의 담당 지역본부를 특정하려면 **피해주택 소재지(시·군·구)**가 필요합니다. "
            "소재지를 알려주시면 「LH 전세사기 피해주택 매입 통합 공고」에 기재된 연락처만 사용해 안내하겠습니다."
        )

    info = LH_CONTACTS[region_key]

    if topic == "purchase":
        phone_text = ", ".join(info["purchase"])
        topic_name = "주택매입 문의"
    else:
        if region_key == "대전충남" and "세종" in combined:
            phone_text = ", ".join(info["sejong_supply"])
            topic_name = "주택공급(긴급주거지원) 문의 - 세종"
        else:
            phone_text = ", ".join(info["supply"])
            topic_name = "주택공급(긴급주거지원) 문의"

    return (
        "\n\n---\n"
        "### 📞 문의처\n"
        f"- **{info['office']} / {topic_name}**: {phone_text}\n"
        f"- 관할: {info['areas']}\n"
        "- 주택매입 문의와 우선공급·긴급주거지원 문의는 담당 연락처가 구분됩니다."
    )


# =========================================================
# Google Sheets
# =========================================================
@st.cache_resource
def init_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)
    return gc.open("LH_Chatbot_Data")


def get_or_create_worksheet(sheet, title, headers):
    try:
        ws = sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=1000, cols=max(20, len(headers) + 2))
        ws.append_row(headers)
        return ws

    # 기존 시트가 있어도 새 컬럼을 안전하게 추가
    try:
        current_headers = ws.row_values(1)
        changed = False
        for h in headers:
            if h not in current_headers:
                current_headers.append(h)
                changed = True
        if changed:
            ws.update("1:1", [current_headers])
    except Exception:
        pass

    return ws


try:
    gc_sheet = init_google_sheets()

    chat_headers = [
        "timestamp",
        "session_id",
        "user_question",       # 마스킹된 질문
        "ai_response",         # 마스킹된 답변
        "verification_status",
        "contact_route",
    ]
    unanswered_headers = [
        "timestamp",
        "session_id",
        "unanswered_question",  # 마스킹된 질문
        "contact_route",
    ]
    feedback_headers = [
        "timestamp", "session_id", "problem_solving", "accuracy", "reliability",
        "speed", "attitude", "readability", "efficiency",
        "alt_action", "time_saved", "avg_score", "good_feedback", "improve_feedback",
    ]

    chat_ws = get_or_create_worksheet(gc_sheet, "chat_logs", chat_headers)
    unanswered_ws = get_or_create_worksheet(gc_sheet, "unanswered_logs", unanswered_headers)
    feedback_ws = get_or_create_worksheet(gc_sheet, "feedback_logs", feedback_headers)
    visitor_ws = get_or_create_worksheet(gc_sheet, "visitors", ["timestamp", "session_id"])

except Exception as e:
    st.error(f"⚠️ 구글 시트 연동 중 오류가 발생했습니다. Secrets 설정을 확인해주세요. 상세 오류: {e}")
    st.stop()


# =========================================================
# 세션 상태
# =========================================================
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "open_feedback_form" not in st.session_state:
    st.session_state.open_feedback_form = False
if "feedback_submitted" not in st.session_state:
    st.session_state.feedback_submitted = False

if "counted_as_visitor" not in st.session_state:
    st.session_state.counted_as_visitor = True
    try:
        visitor_ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            st.session_state.session_id,
        ])
    except Exception:
        pass


# =========================================================
# 기본 PDF 업로드
# =========================================================
DEFAULT_FILES = {
    "notice.pdf": "전세사기 피해주택 매입 통합 공고",
    "law.pdf": "전세사기피해자 지원 및 주거안정에 관한 특별법",
}


@st.cache_resource(show_spinner=False)
def upload_default_documents():
    docs = []
    filenames = []

    for file_path, display_name in DEFAULT_FILES.items():
        if not os.path.exists(file_path):
            continue

        doc = client.files.upload(
            file=file_path,
            config={"display_name": display_name},
        )

        for _ in range(30):
            state = getattr(doc, "state", None)
            state_name = getattr(state, "name", str(state)) if state is not None else "ACTIVE"

            if state_name == "ACTIVE":
                break
            if state_name == "FAILED":
                raise RuntimeError(f"Gemini 파일 처리 실패: {display_name}")

            time.sleep(1)
            doc = client.files.get(name=doc.name)

        state = getattr(doc, "state", None)
        state_name = getattr(state, "name", str(state)) if state is not None else "ACTIVE"
        if state_name != "ACTIVE":
            raise RuntimeError(f"Gemini 파일 처리 대기시간 초과: {display_name} (상태: {state_name})")

        docs.append(doc)
        filenames.append(display_name)

    return docs, filenames


try:
    uploaded_docs, uploaded_filenames = upload_default_documents()
except Exception as e:
    uploaded_docs, uploaded_filenames = [], []
    st.warning(f"기본 문서 업로드 중 오류가 발생했습니다: {type(e).__name__}: {e}")


# =========================================================
# 답변 생성 / 2차 근거 검증
# =========================================================
def build_history_text(messages, max_messages=6):
    recent = messages[-max_messages:]
    lines = []
    for m in recent:
        role = "사용자" if m["role"] == "user" else "챗봇"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def make_system_prompt(persona: str) -> str:
    guardrail = f"""

[초우선 필수 지침]
1. 사실 판단과 제도 설명은 반드시 「전세사기피해자 지원 및 주거안정에 관한 특별법」과 「LH 전세사기 피해주택 매입 통합 공고」의 내용에서만 확인하십시오.
2. 인터넷 검색, 일반 상식 보충, 기억에 의한 법률 지식 추가를 하지 마십시오.
3. 두 문서에 없는 내용 또는 두 문서만으로 확정할 수 없는 내용은 추측하지 마십시오.
4. 두 문서만으로 답변할 수 없으면 반드시 다음 문장으로 시작하십시오.
   "{FALLBACK_PHRASE}"
5. 내부적으로는 조문 번호, 절 제목, Q&A 번호가 실제 문서와 일치하는지 확인하십시오. 존재하지 않는 조문, 절 제목 또는 Q&A 번호를 만들어내지 마십시오.
6. 사용자에게는 별도의 [근거] 섹션, "근거:", PDF 파일명, 페이지 번호를 표시하지 마십시오. 답변 내용만 자연스럽게 설명하십시오.
7. 전화번호는 직접 생성하거나 추측하지 마십시오. 전화번호 안내는 애플리케이션 코드가 문서에 기재된 번호만 별도로 제공합니다.
8. 사용자가 말하지 않은 개인 상황을 임의로 가정하지 마십시오.
"""

    if persona == "LH 전세피해지원 전문 상담관 (기본)":
        base = "당신은 전세피해지원 담당 전문 상담관입니다. 정확하고 이해하기 쉽게 답변하십시오."
    elif persona == "따뜻하고 위로가 되는 상담관":
        base = "당신은 전세피해자를 배려하는 상담관입니다. 공감하되 사실 판단은 반드시 제공 문서에만 근거하십시오."
    else:
        base = "당신은 핵심 요약 봇입니다. 인사말을 줄이고 핵심을 개조식으로 간결하게 답변하십시오."

    return base + guardrail


def generate_answer(question: str, persona: str, history_text: str):
    system_prompt = make_system_prompt(persona)

    user_payload = question
    if history_text:
        user_payload = (
            "[이전 대화 맥락]\n"
            f"{history_text}\n\n"
            "[현재 질문]\n"
            f"{question}\n\n"
            "이전 대화는 현재 질문의 생략된 대상을 이해하는 용도로만 사용하고, "
            "사실 판단은 반드시 「전세사기피해자 지원 및 주거안정에 관한 특별법」과 "
            "「LH 전세사기 피해주택 매입 통합 공고」에서 확인하십시오."
        )

    contents_to_send = list(uploaded_docs)
    contents_to_send.append(user_payload)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents_to_send,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.1,
        ),
    )

    text = response.text
    if not text:
        raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")
    return text


def verify_and_repair_answer(question: str, draft: str):
    if not VERIFY_ANSWER_WITH_SECOND_PASS:
        return draft, "SKIPPED"

    verification_prompt = f"""
당신은 답변 검증기입니다. 제공된 두 문서만 이용해 아래 초안을 검증하십시오.
인터넷이나 외부 지식은 사용하지 마십시오.

[질문]
{question}

[초안]
{draft}

검증 항목:
- 사실 주장마다 제공 문서에서 직접 확인 가능한가?
- 특별법 조문 번호가 실제 문서 내용과 일치하는가?
- 통합공고문 절 제목 또는 Q&A 번호가 실제 문서와 일치하는가?
- 문서에 없는 내용을 추론하거나 만들어내지 않았는가?

출력 규칙:
1) 완전히 문제없으면 정확히 한 줄만 출력: VERIFICATION_OK
2) 수정으로 해결 가능하면 첫 줄에 REVISED_ANSWER 라고 쓰고, 둘째 줄부터 수정된 최종 답변만 출력
   - 수정된 답변에도 [근거] 섹션, "근거:", PDF 파일명, 페이지 번호는 넣지 마십시오.
3) 제공 문서로 답할 수 없으면 정확히 한 줄만 출력: UNSUPPORTED
"""

    contents = list(uploaded_docs)
    contents.append(verification_prompt)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.0,
        ),
    )

    result = (response.text or "").strip()

    if result == "VERIFICATION_OK":
        return draft, "VALID"
    if result == "UNSUPPORTED":
        return FALLBACK_PHRASE, "UNSUPPORTED"
    if result.startswith("REVISED_ANSWER"):
        revised = result[len("REVISED_ANSWER"):].strip()
        if revised:
            return revised, "REVISED"

    # 검증 결과 형식이 깨진 경우에도 검증되지 않은 초안은 사용자에게 보여주지 않음
    return FALLBACK_PHRASE, "VERIFY_PARSE_FAILED"


# =========================================================
# 사이드바
# =========================================================
with st.sidebar:
    st.header("⚙️ 챗봇 설정")

    persona = st.selectbox(
        "상담관 모드 선택",
        [
            "LH 전세피해지원 전문 상담관 (기본)",
            "따뜻하고 위로가 되는 상담관",
            "핵심만 짚어주는 요약 봇",
        ],
    )

    if st.button("🔄 대화 내용 초기화"):
        st.session_state.messages = []
        st.session_state.open_feedback_form = False
        st.session_state.feedback_submitted = False
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

    st.divider()

    with st.expander("🔒 관리자 전용 (데이터 분석)"):
        correct_pw = st.secrets.get("ADMIN_PASSWORD")

        if not correct_pw:
            st.info("ADMIN_PASSWORD가 Secrets에 설정되지 않아 관리자 기능이 비활성화되어 있습니다.")
        else:
            admin_pw = st.text_input("관리자 비밀번호 입력", type="password", key="admin_pw_input")

            if admin_pw == correct_pw:
                st.success("인증 성공! 대시보드 활성화")

                try:
                    visitors_data = visitor_ws.get_all_records()
                    chat_data = chat_ws.get_all_records()
                    unanswered_data = unanswered_ws.get_all_records()
                    feedback_data = feedback_ws.get_all_records()

                    total_visitors = len(visitors_data)
                    total_questions = len(chat_data)
                    total_unanswered = len(unanswered_data)

                    st.markdown("#### 📈 이용자 현황")
                    col1, col2 = st.columns(2)
                    col1.metric("총 방문자 수", f"{total_visitors}명")
                    col2.metric("총 질문 수", f"{total_questions}건")
                    st.metric("🚨 미답변 발생 건수", f"{total_unanswered}건")

                    st.markdown("#### 💾 데이터 다운로드")
                    if total_questions > 0:
                        df_chat = pd.DataFrame(chat_data)
                        st.download_button(
                            "📥 전체 질문 로그 (.csv)",
                            df_chat.to_csv(index=False).encode("utf-8-sig"),
                            "chat_log.csv",
                            "text/csv",
                        )

                    if total_unanswered > 0:
                        df_unanswered = pd.DataFrame(unanswered_data)
                        st.download_button(
                            "📥 미답변 질문 리스트 (.csv)",
                            df_unanswered.to_csv(index=False).encode("utf-8-sig"),
                            "unanswered_log.csv",
                            "text/csv",
                        )

                    if feedback_data:
                        df_feedback = pd.DataFrame(feedback_data)
                        if "avg_score" in df_feedback.columns:
                            numeric_scores = pd.to_numeric(df_feedback["avg_score"], errors="coerce")
                            if numeric_scores.notna().any():
                                st.metric("전체 평균 만족도", f"{numeric_scores.mean():.2f} / 5.0점")
                        st.download_button(
                            "📥 만족도 평가 결과 (.csv)",
                            df_feedback.to_csv(index=False).encode("utf-8-sig"),
                            "service_feedback.csv",
                            "text/csv",
                        )
                except Exception as e:
                    st.error(f"데이터를 불러오는 중 오류 발생: {e}")

            elif admin_pw:
                st.error("비밀번호가 올바르지 않습니다.")


# =========================================================
# 메인 화면
# =========================================================
st.header("🏠 LH 전세사기 피해주택 매입 Q&A 챗봇")
st.caption(
    "이 챗봇은 「전세사기피해자 지원 및 주거안정에 관한 특별법」과 "
    "「LH 전세사기 피해주택 매입 통합 공고」를 바탕으로 답변합니다."
)

st.warning(
    "⚠️ **이용 전 꼭 확인해 주세요**\n\n"
    "이 챗봇의 답변은 제공된 공고문과 특별법을 바탕으로 생성한 **참고용 정보**입니다. "
    "AI의 특성상 일부 내용이나 근거가 부정확할 수 있으며, 개인의 구체적인 상황에 따라 적용 결과가 달라질 수 있습니다.\n\n"
    "**신청, 경·공매, 계약, 금전 지급 등 중요한 의사결정 전에는 반드시 LH 지역본부 또는 관련 담당기관에 직접 확인해 주세요.**\n\n"
    "서비스 개선을 위해 질문과 답변이 저장될 수 있으며, 저장 전 전화번호·주민등록번호·이메일·상세주소 등 주요 개인정보를 마스킹합니다."
)
st.caption(DOCUMENT_FRESHNESS_CAUTION)
st.divider()


# =========================================================
# 자가진단 / 챗봇 상담 분리
# =========================================================
chat_tab, diagnosis_tab = st.tabs(["💬 챗봇 상담", "✅ 자가진단"])

with diagnosis_tab:
    st.subheader("✅ LH 전세사기 피해주택 매입 신청 자격 간이 확인")
    st.markdown(
        "아래 항목은 통합공고문에 제시된 핵심 조건을 빠르게 확인하기 위한 **간이 체크**입니다. "
        "최종 신청 가능 여부는 공고문과 담당기관 확인이 필요합니다."
    )

    check1 = st.checkbox(
        "국토부로부터 특별법에 따른 전세사기피해자 또는 신탁사기피해자로 결정된 임차인",
        key="diagnosis_check1",
    )
    check2 = st.checkbox(
        "경매 또는 공매가 개시된 주택",
        key="diagnosis_check2",
    )
    check3 = st.checkbox(
        "내국인(주민등록표등본 등재)",
        key="diagnosis_check3",
    )

    st.write("")

    if check1 and check2 and check3:
        st.success("핵심 조건 3가지를 모두 체크했습니다. 세부 요건은 공고문과 담당기관에서 최종 확인해 주세요.")
    elif check1 or check2 or check3:
        st.info("일부 항목만 체크되었습니다. 자가진단 결과와 관계없이 챗봇 상담은 이용할 수 있습니다.")
    else:
        st.caption("자가진단은 선택 사항입니다. 체크하지 않아도 챗봇 상담을 이용할 수 있습니다.")

with chat_tab:
    st.subheader("💬 챗봇 상담")
    st.caption("자가진단을 하지 않았거나 조건을 모두 충족하지 않아도 질문할 수 있습니다.")

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("궁금한 점을 입력해주세요 (예: 매입 사전협의는 언제 신청할 수 있나요?)"):
        # 현재 질문을 화면에 먼저 표시
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # 현재 질문을 제외한 직전 대화 맥락
        previous_messages = st.session_state.messages[:-1]
        history_text = build_history_text(previous_messages, max_messages=6)
        contact_context = "\n".join(
            m["content"] for m in previous_messages[-6:] if m["role"] == "user"
        )

        with st.chat_message("assistant"):
            try:
                draft = generate_answer(prompt, persona, history_text)

                verification_status = "NOT_RUN"
                try:
                    ai_text, verification_status = verify_and_repair_answer(prompt, draft)
                except Exception:
                    # 검증에 실패한 경우 검증되지 않은 초안을 노출하지 않고 동일한 안내 문구 사용
                    ai_text = FALLBACK_PHRASE
                    verification_status = "VERIFY_API_FAILED"

                # 미답변 또는 사용자가 문의처를 직접 요청한 경우에만 문서 기반 연락처 보강
                wants_contact = any(
                    k in prompt for k in ["전화", "연락처", "어디 문의", "어디에 문의", "문의처"]
                )
                is_unsupported = FALLBACK_PHRASE in ai_text

                contact_route = ""
                if is_unsupported or wants_contact:
                    topic = detect_contact_topic(f"{contact_context}\n{prompt}")
                    region = detect_region_key(f"{contact_context}\n{prompt}")
                    contact_route = f"topic={topic or 'unknown'};region={region or 'unknown'}"
                    ai_text += make_contact_guidance(prompt, contact_context)

                st.markdown(ai_text)
                st.session_state.messages.append({"role": "assistant", "content": ai_text})

            except errors.APIError as e:
                status_code = getattr(e, "code", "unknown")
                st.error(f"⚠️ Gemini API 요청에 실패했습니다. (HTTP {status_code})\n\n{e}")

                if status_code == 401:
                    st.info("API 키가 잘못되었거나 만료되었을 수 있습니다. Streamlit Secrets의 GEMINI_API_KEY를 확인하세요.")
                elif status_code == 403:
                    st.info("Gemini API 또는 Google Cloud 프로젝트 권한을 확인하세요.")
                elif status_code == 429:
                    st.info("Gemini API 무료/유료 사용량 한도를 초과했을 가능성이 있습니다.")
                elif status_code == 400:
                    st.info("요청 형식 또는 업로드된 Gemini 파일 상태를 확인하세요.")
                st.stop()

            except Exception as e:
                st.error(f"⚠️ Gemini 호출 중 오류가 발생했습니다.\n\n{type(e).__name__}: {e}")
                st.stop()

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # -------------------------------------------------
            # 저장할 때만 개인정보 마스킹
            # -------------------------------------------------
            masked_prompt = mask_personal_info(prompt)
            masked_ai_text = mask_personal_info(ai_text)

            try:
                chat_ws.append_row([
                    timestamp,
                    st.session_state.session_id,
                    masked_prompt,
                    masked_ai_text,
                    verification_status,
                    contact_route,
                ])
            except Exception:
                pass

            if FALLBACK_PHRASE in ai_text:
                try:
                    unanswered_ws.append_row([
                        timestamp,
                        st.session_state.session_id,
                        masked_prompt,
                        contact_route,
                    ])
                except Exception:
                    pass

            st.rerun()

    # =====================================================
    # 만족도 평가
    # =====================================================
    if st.session_state.messages:
        st.divider()
        st.markdown("### 💌 서비스 만족도 평가")
        st.info("💡 여러분의 의견은 더 정확한 피해자 지원 서비스를 만드는 데 활용됩니다.")

        if not st.session_state.feedback_submitted:
            col1, col2 = st.columns(2)
            with col1:
                if st.button("👍 답변이 도움이 되었어요!", use_container_width=True):
                    st.session_state.open_feedback_form = True
            with col2:
                if st.button("💡 개선 사항을 제안하고 싶어요", use_container_width=True):
                    st.session_state.open_feedback_form = True

            if st.session_state.open_feedback_form:
                st.write("")
                with st.form("main_feedback_form"):
                    st.markdown("#### [1단계] 항목별 만족도 (1점 ~ 5점)")
                    f_solving = st.slider("1. 문제 해결 기여도", 1, 5, 5)
                    f_accuracy = st.slider("2. 정보의 정확성", 1, 5, 5)
                    f_reliability = st.slider("3. 정보의 신뢰성", 1, 5, 5)
                    f_speed = st.slider("4. 답변 속도", 1, 5, 5)
                    f_attitude = st.slider("5. 챗봇 상담 태도", 1, 5, 5)
                    f_readability = st.slider("6. 가독성", 1, 5, 5)
                    f_efficiency = st.slider("7. 정보 탐색 수고 절감 (효율성)", 1, 5, 5)

                    st.divider()
                    st.markdown("#### [2단계] 서비스 효율성 측정")

                    alt_action = st.radio(
                        "8. 만약 이 챗봇이 없었다면 궁금한 점을 어떻게 해결하셨을 것 같나요?",
                        options=[
                            "LH 콜센터 상담이나 지사에 직접 전화 혹은 방문한다",
                            "공고문과 특별법을 직접 찾아본다",
                            "오픈채팅방을 이용한다",
                            "인터넷에 검색한다",
                            "다른 AI에게 물어본다",
                            "포기한다",
                            "기타",
                        ],
                    )
                    alt_action_other = st.text_input("위 8번 문항에서 '기타'를 선택하신 경우, 직접 적어주세요.")

                    st.write("")

                    time_saved = st.radio(
                        "9. 이 챗봇 덕분에 정보를 찾는 시간을 대략 얼마나 단축했다고 생각하시나요?",
                        options=["10분 이내", "30분 정도", "1시간 정도", "1시간 이상", "기타"],
                    )
                    time_saved_other = st.text_input("위 9번 문항에서 '기타'를 선택하신 경우, 단축된 시간을 직접 적어주세요.")

                    st.divider()
                    st.markdown("#### [3단계] 상세 피드백")
                    st.caption("긍정적인 점과 아쉬운 점을 모두 남겨주시면 큰 도움이 됩니다!")

                    good_text = st.text_area(
                        "10. 이 챗봇의 어떤 점이 가장 좋았거나 도움이 되셨나요? (장점)",
                        placeholder="예: 복잡한 공고문을 쉽게 요약해줘서 좋았어요.",
                    )
                    improve_text = st.text_area(
                        "11. 더 나은 서비스를 위해 개선해야 할 점이 있다면 적어주세요. (개선점)",
                        placeholder="예: 000에 대한 정보가 더 추가되면 좋겠어요.",
                    )

                    submitted = st.form_submit_button("피드백 제출하기")
                    if submitted:
                        final_alt_action = (
                            alt_action_other
                            if alt_action == "기타" and alt_action_other
                            else alt_action
                        )
                        final_time_saved = (
                            time_saved_other
                            if time_saved == "기타" and time_saved_other
                            else time_saved
                        )

                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        avg_score = round(
                            (
                                f_solving
                                + f_accuracy
                                + f_reliability
                                + f_speed
                                + f_attitude
                                + f_readability
                                + f_efficiency
                            )
                            / 7,
                            2,
                        )

                        # 자유서술 및 기타 입력값도 저장 전에 마스킹
                        final_alt_action = mask_personal_info(final_alt_action)
                        final_time_saved = mask_personal_info(final_time_saved)
                        good_text = mask_personal_info(good_text)
                        improve_text = mask_personal_info(improve_text)

                        try:
                            feedback_ws.append_row([
                                timestamp,
                                st.session_state.session_id,
                                f_solving,
                                f_accuracy,
                                f_reliability,
                                f_speed,
                                f_attitude,
                                f_readability,
                                f_efficiency,
                                final_alt_action,
                                final_time_saved,
                                avg_score,
                                good_text,
                                improve_text,
                            ])
                        except Exception:
                            pass

                        st.session_state.feedback_submitted = True
                        st.rerun()
        else:
            st.success("🎉 따뜻한 의견 감사합니다. 더 나은 서비스로 보답하겠습니다.")
