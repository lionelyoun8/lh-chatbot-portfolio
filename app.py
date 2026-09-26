import os
import re
import json
import time
import uuid
from datetime import datetime

import gspread
import pandas as pd
import streamlit as st
from google import genai
from google.genai import errors, types
from google.oauth2.service_account import Credentials


st.set_page_config(
    page_title="LH 전세사기 피해주택 매입 Q&A 챗봇",
    page_icon="🏠",
    layout="wide",
)

GEMINI_MODEL = st.secrets.get("GEMINI_MODEL", "gemini-3.6-flash")
VERIFY_ANSWER_WITH_SECOND_PASS = True
FALLBACK_PHRASE = "현재 참고 문서에서는 해당 내용을 명확히 확인하기 어렵습니다. 정확한 안내는 아래 관련 기관에 문의해 주세요."

DOCUMENT_FRESHNESS_CAUTION = (
    "※ 답변은 「전세사기피해자 지원 및 주거안정에 관한 특별법」, "
    "「전세사기 피해주택 매입 통합 공고」, "
    "「전세피해지원 프로그램 및 전세피해 상담 사례집」을 기준으로 하며, "
    "이후 법령 개정이나 공고·지원제도 변경 사항은 즉시 반영되지 않을 수 있습니다."
)

client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])


def mask_personal_info(text: str) -> str:
    if not text:
        return ""

    masked = str(text)


    masked = re.sub(
        r"(?<!\d)\d{6}\s*-?\s*[1-4]\d{6}(?!\d)",
        "[주민등록번호]",
        masked,
    )


    masked = re.sub(
        r"(?<!\d)(?:01[016789]|02|0[3-6][1-5])[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)",
        "[전화번호]",
        masked,
    )


    masked = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[이메일]",
        masked,
    )


    masked = re.sub(
        r"((?:계좌번호|계좌)\s*[:：]?\s*)(?:\d[\d\-\s]{7,}\d)",
        r"\1[계좌번호]",
        masked,
        flags=re.IGNORECASE,
    )


    masked = re.sub(
        r"([가-힣A-Za-z0-9]+(?:로|길|동|읍|면)\s*)\d+(?:-\d+)?",
        r"\1[상세주소]",
        masked,
    )


    masked = re.sub(
        r"(?<!\d)\d{1,4}\s*동\s*\d{1,5}\s*호(?!\d)",
        "[동·호수]",
        masked,
    )

    return masked


CONTACT_REQUEST_KEYWORDS = [
    "전화", "전화번호", "연락처", "문의처", "어디 문의", "어디에 문의",
    "어디로 문의", "어디에 전화", "어디로 전화", "기관", "상담센터",
]


def wants_contact_info(text: str) -> bool:
    t = str(text or "")
    return any(keyword in t for keyword in CONTACT_REQUEST_KEYWORDS)


NATIONAL_CONTACTS = {
    "hug_general": {
        "name": "HUG 전세피해지원센터",
        "phones": ["02-6917-8119"],
        "purpose": "전세피해 일반상담, 무료 법률상담, 피해지원 프로그램 안내",
        "source": "「전세피해지원 프로그램 및 전세피해 상담 사례집」 5·7쪽",
    },
    "hug_auction": {
        "name": "HUG 경·공매지원센터",
        "phones": ["1533-8119"],
        "purpose": "경·공매 지원, 경·공매 절차 관련 지원, 상속재산관리인 선임지원 관련 문의",
        "source": "「전세피해지원 프로그램 및 전세피해 상담 사례집」 7쪽",
    },
    "psych": {
        "name": "전세피해자 심리상담",
        "phones": ["1670-5724"],
        "purpose": "전세피해 관련 심리상담(문서상 09:00~21:00, 연중무휴)",
        "source": "「전세피해지원 프로그램 및 전세피해 상담 사례집」 7쪽",
    },
    "welfare": {
        "name": "보건복지상담센터",
        "phones": ["129"],
        "purpose": "긴급복지지원 등 복지제도 추가 확인",
        "source": "「전세피해지원 프로그램 및 전세피해 상담 사례집」 13쪽",
    },
    "molit": {
        "name": "국토교통부 피해지원총괄과",
        "phones": ["044-201-5233", "044-201-5234"],
        "purpose": "전세사기피해자 지원 특별법 및 피해지원 제도 관련 문의",
        "source": "「전세사기피해자 지원 및 주거안정에 관한 특별법」 3쪽",
    },
    "lh_clean_call": {
        "name": "LH 매입 클린콜",
        "phones": ["055-922-5637"],
        "purpose": "주택매입 관련 청탁·부정행위 신고",
        "source": "「전세사기 피해주택 매입 통합 공고」 16쪽",
    },
}


LOCAL_VICTIM_CENTERS = {
    "서울": {"name": "서울 전월세 종합지원센터", "phones": ["02-2133-1200~8"]},
    "인천": {"name": "인천 전세피해지원센터", "phones": ["032-440-1803"]},
    "부산": {"name": "부산 전세피해지원센터", "phones": ["051-888-5101~2"]},
    "대전": {"name": "대전 전세피해지원센터", "phones": ["042-270-6521~6"]},
    "대구": {"name": "대구 전세피해지원센터", "phones": ["053-803-4664"]},
    "광주": {"name": "광주 전세피해지원센터", "phones": ["062-613-4875~7"]},
    "울산": {"name": "울산광역시청 건축정책과", "phones": ["052-229-4453"]},
    "세종": {"name": "세종특별자치시청 주택과", "phones": ["044-300-5934"]},
    "경기": {"name": "경기도 전세피해지원센터", "phones": ["031-242-2450"]},
    "강원": {"name": "강원특별자치도청 건축과", "phones": ["033-249-3464"]},
    "충남": {"name": "충청남도청 건축도시과", "phones": ["041-635-4662"]},
    "충북": {"name": "충청북도청 건축문화과", "phones": ["043-220-4474"]},
    "경남": {"name": "경상남도청 건축주택과", "phones": ["055-211-4344"]},
    "경북": {"name": "경상북도청 건축디자인과", "phones": ["054-880-4022"]},
    "전남": {"name": "전라남도청 건축개발과", "phones": ["061-286-7721"]},
    "전북": {"name": "전북특별자치도청 주택건축과", "phones": ["063-280-2365"]},
    "제주": {"name": "제주특별자치도청 주택토지과", "phones": ["064-710-2693", "064-710-2695"]},
}
LOCAL_VICTIM_CENTER_SOURCE = "「전세피해지원 프로그램 및 전세피해 상담 사례집」 4~5쪽"


LH_REGIONAL_CONTACTS = {
    "서울지역본부": {
        "purchase": ["02-2015-1030"],
        "supply": ["02-3416-3780", "02-3416-3688"],
    },
    "인천지역본부": {
        "purchase": ["032-890-5315", "032-890-5317"],
        "supply": ["032-890-5319"],
    },
    "경기남부지역본부": {
        "purchase": ["031-250-8119", "031-250-8118", "031-250-8157", "031-250-8170", "031-250-8317", "031-250-8137"],
        "supply": ["031-250-6125", "031-250-6174"],
    },
    "경기북부지역본부": {
        "purchase": ["02-6040-1470", "02-6040-1471"],
        "supply": ["02-6363-0453"],
    },
    "부산울산지역본부": {
        "purchase": ["051-796-6038", "051-796-6047", "051-796-6027", "051-460-5980"],
        "supply": ["051-796-6075", "051-796-6035"],
    },
    "대구경북지역본부": {
        "purchase": ["053-603-2958", "053-603-2744"],
        "supply": ["053-603-2732", "053-603-2745"],
    },
    "광주전남지역본부": {
        "purchase": ["062-360-3238", "062-360-3249"],
        "supply": ["062-360-3253", "062-360-3275"],
    },
    "대전충남지역본부": {
        "purchase": ["042-470-0690"],
        "supply": ["042-470-0959", "042-470-0284"],
        "supply_sejong": ["044-902-2326"],
    },
    "경남지역본부": {
        "purchase": ["055-210-8576", "055-210-8658"],
        "supply": ["055-210-8668", "055-210-8463"],
    },
    "강원지역본부": {
        "purchase": ["033-258-4156"],
        "supply_chuncheon": ["033-258-4121"],
        "supply_wonju": ["033-812-6775"],
        "supply_gangneung": ["033-610-5165"],
    },
    "충북지역본부": {
        "purchase": ["043-901-4524"],
        "supply": ["043-901-4521", "043-901-4528"],
    },
    "전북지역본부": {
        "purchase": ["063-230-6257", "063-230-6238"],
        "supply_jeonju_wanju": ["063-230-6224"],
        "supply_gunsan_iksan": ["063-840-0919"],
        "supply_jeongeup_gimje_namwon": ["063-570-2314"],
    },
    "제주지역본부": {
        "purchase": ["064-720-1036"],
        "supply": ["064-720-1035"],
    },
}
LH_CONTACT_SOURCE = "「전세사기 피해주택 매입 통합 공고」 2쪽"


SEOUL_DISTRICTS = [
    "종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구",
    "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구",
    "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구",
]

GYEONGGI_SOUTH_CITIES = [
    "수원", "성남", "안양", "평택", "안산", "과천", "오산", "군포", "의왕", "용인",
    "안성", "화성", "경기 광주", "광주시", "이천", "여주", "광명", "시흥",
]
GYEONGGI_NORTH_CITIES = [
    "의정부", "포천", "남양주", "가평", "구리", "양주", "동두천", "연천", "고양", "파주",
    "김포", "하남", "양평",
]

SUPPORT_REGION_ALIASES = {
    "서울": ["서울"] + SEOUL_DISTRICTS,
    "인천": ["인천"],
    "부산": ["부산"],
    "대전": ["대전"],
    "대구": ["대구"],
    "광주": ["광주광역시", "광주"],
    "울산": ["울산"],
    "세종": ["세종"],
    "경기": ["경기", "부천"] + GYEONGGI_SOUTH_CITIES + GYEONGGI_NORTH_CITIES,
    "강원": ["강원", "춘천", "원주", "강릉", "속초", "동해", "삼척", "태백"],
    "충남": ["충남", "충청남도", "천안", "아산", "공주", "논산", "서산", "당진", "보령", "계룡"],
    "충북": ["충북", "충청북도", "청주", "충주", "제천"],
    "경남": ["경남", "경상남도", "창원", "김해", "진주", "양산", "거제", "통영", "사천", "밀양"],
    "경북": ["경북", "경상북도", "포항", "경주", "구미", "안동", "김천", "영주", "영천", "상주", "문경"],
    "전남": ["전남", "전라남도", "목포", "여수", "순천", "나주", "광양"],
    "전북": ["전북", "전라북도", "전북특별자치도", "전주", "완주", "군산", "익산", "정읍", "김제", "남원"],
    "제주": ["제주"],
}


def _contains_any(text: str, words) -> bool:
    return any(w in text for w in words)


def detect_support_region(text: str):
    t = str(text or "")


    explicit_checks = [
        ("인천", ["인천"]), ("부산", ["부산"]), ("대전", ["대전"]),
        ("대구", ["대구"]), ("울산", ["울산"]), ("세종", ["세종"]),
        ("강원", ["강원", "강원특별자치도"]),
        ("충남", ["충남", "충청남도"]), ("충북", ["충북", "충청북도"]),
        ("경남", ["경남", "경상남도"]), ("경북", ["경북", "경상북도"]),
        ("전남", ["전남", "전라남도"]),
        ("전북", ["전북", "전라북도", "전북특별자치도"]),
        ("제주", ["제주", "제주특별자치도"]),
    ]
    for region, aliases in explicit_checks:
        if any(alias in t for alias in aliases):
            return region

    if "광주광역시" in t:
        return "광주"
    if "경기 광주" in t or "경기도 광주" in t or ("광주시" in t and "광주광역시" not in t):
        return "경기"
    if "경기" in t or "경기도" in t or "부천" in t or _contains_any(t, GYEONGGI_SOUTH_CITIES + GYEONGGI_NORTH_CITIES):
        return "경기"
    if "서울" in t:
        return "서울"

    if _contains_any(t, SEOUL_DISTRICTS):
        return "서울"
    if "광주" in t:
        return "광주"


    for region, aliases in SUPPORT_REGION_ALIASES.items():
        if any(alias in t for alias in aliases):
            return region
    return None


def detect_lh_region(text: str):
    t = str(text or "")


    if "부산" in t or "울산" in t:
        return "부산울산지역본부"
    if "인천" in t:
        return "인천지역본부"
    if "대구" in t or "경북" in t or "경상북도" in t or _contains_any(t, ["포항", "경주", "구미", "안동", "김천", "영주", "영천", "상주", "문경"]):
        return "대구경북지역본부"
    if "광주광역시" in t or "전남" in t or "전라남도" in t or _contains_any(t, ["목포", "여수", "순천", "나주", "광양"]):
        return "광주전남지역본부"
    if "대전" in t or "충남" in t or "충청남도" in t or "세종" in t or _contains_any(t, ["천안", "아산", "공주", "논산", "서산", "당진", "보령", "계룡"]):
        return "대전충남지역본부"
    if "경남" in t or "경상남도" in t or _contains_any(t, ["창원", "김해", "진주", "양산", "거제", "통영", "사천", "밀양"]):
        return "경남지역본부"
    if "강원" in t or _contains_any(t, ["춘천", "원주", "강릉", "속초", "동해", "삼척", "태백"]):
        return "강원지역본부"
    if "충북" in t or "충청북도" in t or _contains_any(t, ["청주", "충주", "제천"]):
        return "충북지역본부"
    if "전북" in t or "전라북도" in t or "전북특별자치도" in t or _contains_any(t, ["전주", "완주", "군산", "익산", "정읍", "김제", "남원"]):
        return "전북지역본부"
    if "제주" in t:
        return "제주지역본부"


    if "부천" in t:
        return "인천지역본부"
    if _contains_any(t, GYEONGGI_SOUTH_CITIES):
        return "경기남부지역본부"
    if _contains_any(t, GYEONGGI_NORTH_CITIES):
        return "경기북부지역본부"

    if "서울" in t or _contains_any(t, SEOUL_DISTRICTS):
        return "서울지역본부"
    if "광주" in t:
        return "광주전남지역본부"
    return None


def detect_contact_intents(text: str):
    t = str(text or "")
    intents = set()

    if _contains_any(t, ["매입", "사전협의", "매입요청", "실태조사", "감정평가", "LH 매입", "우선매수권 양도"]):
        intents.add("purchase")
    if _contains_any(t, ["긴급주거", "긴급 주거", "우선공급", "공공임대", "전세임대", "주거지원", "퇴거", "이사"]):
        intents.add("housing")
    if _contains_any(t, ["경매", "공매", "경·공매", "배당", "낙찰", "매각기일", "유예", "정지", "우선매수권", "집행권원"]):
        intents.add("auction")
    if _contains_any(t, ["피해자 결정", "결정신청", "피해 인정", "전세사기피해자 신청", "피해확인서", "나목", "다목"]):
        intents.add("victim_decision")
    if _contains_any(t, ["법률", "소송", "고소", "무고", "보증금반환소송", "상속재산관리인", "임대인 사망", "회생", "파산", "손해배상", "공인중개사"]):
        intents.add("legal")
    if _contains_any(t, ["심리", "우울", "불안", "트라우마", "정신건강", "심리치료"]):
        intents.add("psych")
    if _contains_any(t, ["긴급복지", "생계비", "의료비", "교육비", "복지지원", "복지 상담"]):
        intents.add("welfare")
    if _contains_any(t, ["대출", "대환", "무이자", "저리", "금융", "신용", "연체"]):
        intents.add("finance")
    if _contains_any(t, ["특별법", "법 조항", "법조항", "피해자 요건", "이의신청", "결정 취소", "국토부"]):
        intents.add("molit")
    if _contains_any(t, ["청탁", "부정행위", "부패", "클린콜", "비리 신고"]):
        intents.add("integrity")

    return intents


def _contact_item(name, phones, purpose, source):
    return {
        "name": name,
        "phones": list(phones),
        "purpose": purpose,
        "source": source,
    }


def _lh_contact_for(text: str, lh_region: str, kind: str):
    if not lh_region or lh_region not in LH_REGIONAL_CONTACTS:
        return None

    data = LH_REGIONAL_CONTACTS[lh_region]
    phones = []

    if kind == "purchase":
        phones = data.get("purchase", [])
        purpose = "피해주택 매입·사전협의 관련 문의"
    else:
        purpose = "공공임대 우선공급·긴급주거지원 관련 문의"
        if lh_region == "대전충남지역본부" and "세종" in text:
            phones = data.get("supply_sejong", [])
        elif lh_region == "강원지역본부":
            if "춘천" in text:
                phones = data.get("supply_chuncheon", [])
            elif "원주" in text:
                phones = data.get("supply_wonju", [])
            elif "강릉" in text:
                phones = data.get("supply_gangneung", [])
            else:
                phones = []
        elif lh_region == "전북지역본부":
            if _contains_any(text, ["전주", "완주"]):
                phones = data.get("supply_jeonju_wanju", [])
            elif _contains_any(text, ["군산", "익산"]):
                phones = data.get("supply_gunsan_iksan", [])
            elif _contains_any(text, ["정읍", "김제", "남원"]):
                phones = data.get("supply_jeongeup_gimje_namwon", [])
            else:
                phones = []
        else:
            phones = data.get("supply", [])

    if not phones:
        return None

    return _contact_item(lh_region, phones, purpose, LH_CONTACT_SOURCE)


def recommend_contacts(text: str, max_items=3, include_generic=True):
    t = str(text or "")
    intents = detect_contact_intents(t)
    support_region = detect_support_region(t)
    lh_region = detect_lh_region(t)

    result = []
    seen = set()

    def add(item):
        if not item:
            return
        key = (item["name"], tuple(item["phones"]))
        if key in seen:
            return
        seen.add(key)
        result.append(item)


    if "psych" in intents:
        add(NATIONAL_CONTACTS["psych"])
    if "welfare" in intents:
        add(NATIONAL_CONTACTS["welfare"])
    if "integrity" in intents:
        add(NATIONAL_CONTACTS["lh_clean_call"])
    if "auction" in intents:
        add(NATIONAL_CONTACTS["hug_auction"])


    if "purchase" in intents:
        add(_lh_contact_for(t, lh_region, "purchase"))
    if "housing" in intents:
        add(_lh_contact_for(t, lh_region, "supply"))


    if support_region and (
        not intents
        or intents.intersection({"victim_decision", "legal", "finance", "auction", "housing", "purchase"})
    ):
        c = LOCAL_VICTIM_CENTERS.get(support_region)
        if c:
            add(_contact_item(
                c["name"],
                c["phones"],
                "전세사기피해자 결정 신청·지역 피해지원 상담",
                LOCAL_VICTIM_CENTER_SOURCE,
            ))

    if "legal" in intents or "finance" in intents or "victim_decision" in intents:
        add(NATIONAL_CONTACTS["hug_general"])
    if "molit" in intents:
        add(NATIONAL_CONTACTS["molit"])


    if include_generic and not result:
        if support_region:
            c = LOCAL_VICTIM_CENTERS.get(support_region)
            if c:
                add(_contact_item(
                    c["name"],
                    c["phones"],
                    "지역 전세피해 지원·신청 안내",
                    LOCAL_VICTIM_CENTER_SOURCE,
                ))
        add(NATIONAL_CONTACTS["hug_general"])
        add(NATIONAL_CONTACTS["molit"])

    return result[:max_items]


def strip_model_phone_numbers(text: str) -> str:
    """전화번호는 하드코딩된 공식 디렉터리에서만 노출되도록 모델 생성 번호를 제거한다."""
    if not text:
        return ""

    cleaned = re.sub(
        r"(?<!\d)(?:0\d{1,2}-\d{3,4}-\d{4}|1\d{3}-\d{4})(?:\s*[~,]\s*\d{1,4})*",
        "[공식 문의처는 아래 안내 참고]",
        text,
    )
    cleaned = re.sub(r"(?<!\d)129(?!\d)", "[공식 문의처는 아래 안내 참고]", cleaned)
    return cleaned


def append_contact_recommendations(ai_text: str, contacts):
    if not contacts:
        return ai_text


    source_matches = re.findall(r"(?m)^출처:\s*(.+?)\s*$", ai_text or "")
    body = re.sub(r"(?m)^출처:\s*.+?\s*$", "", ai_text or "").rstrip()

    lines = [body, "", "☎️ **문의해볼 곳**"]
    for idx, c in enumerate(contacts, 1):
        phone_text = ", ".join(c["phones"])
        lines.append(f"{idx}. **{c['name']}** — {phone_text}")
        lines.append(f"   - {c['purpose']}")

    sources = []
    for src in source_matches + [c["source"] for c in contacts]:
        if src and src not in sources:
            sources.append(src)

    if sources:
        lines.extend(["", "출처: " + " / ".join(sources)])

    return "\n".join(lines)


BROAD_FALLBACK_CONTACT_NAMES = {
    "HUG 전세피해지원센터",
    "국토교통부 피해지원총괄과",
}


def _contacts_are_only_broad(contacts) -> bool:
    if not contacts:
        return True
    return all(c.get("name") in BROAD_FALLBACK_CONTACT_NAMES for c in contacts)


def _parse_json_array(raw_text: str):
    text = (raw_text or "").strip()
    if not text:
        return []


    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except Exception:

        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            return []

    return data if isinstance(data, list) else []


def _valid_phone_string(phone: str) -> bool:
    p = str(phone or "").strip()
    return bool(re.fullmatch(
        r"(?:0\d{1,2}-\d{3,4}-\d{4}(?:~\d{1,4})?|1\d{2,3}-\d{4}|129)",
        p,
    ))


def _sanitize_document_contacts(items, max_items=3):
    cleaned = []
    seen = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        purpose = str(item.get("purpose", "")).strip()
        source = str(item.get("source", "")).strip()
        phones_raw = item.get("phones", [])
        if isinstance(phones_raw, str):
            phones_raw = [phones_raw]
        if not isinstance(phones_raw, list):
            continue

        phones = []
        for phone in phones_raw:
            p = str(콜).strip()
            if _valid_phone_string(p) and p not in phones:
                phones.append(p)


        if not name or not phones or not source:
            continue

        key = (name, tuple(phones))
        if key in seen:
            continue
        seen.add(key)

        cleaned.append(_contact_item(
            name=name,
            phones=phones,
            purpose=purpose or "관련 문의",
            source=source,
        ))

        if len(cleaned) >= max_items:
            break

    return cleaned


def find_verified_document_contacts(question: str, max_items=3):
    """
    하드코딩 디렉터리에서 구체적인 번호를 찾지 못했을 때만 호출한다.
    1차: 세 참고 문서에서 기관명+전화번호 후보를 그대로 추출
    2차: 같은 문서로 후보가 실제로 명시되어 있는지 독립 검증
    검증기가 새 번호를 만들 수 없도록 1차 후보와 완전히 동일한 기관명/번호 조합만 허용한다.
    """
    if not uploaded_docs:
        return []

    extraction_prompt = f"""
당신은 공식 문서의 연락처 추출기입니다.
아래 사용자 질문과 직접 관련된 공식 문의기관과 전화번호를, 함께 제공된 참고 문서에서만 찾으십시오.
인터넷, 기억, 일반지식은 사용하지 마십시오.

[사용자 질문]
{question}

규칙:
1. 기관명과 전화번호가 문서에서 명확하게 연결되어 적혀 있는 경우만 후보로 내십시오.
2. 전화번호의 일부를 추정·보완하거나 다른 번호 형식으로 고치지 마십시오.
3. 질문과 관련성이 높은 기관만 최대 {max_items}개 제시하십시오.
4. 출처는 실제 문서명과 PDF 페이지를 정확히 적으십시오. 예: 「전세피해지원 프로그램 및 전세피해 상담 사례집」 13쪽
5. 문서에서 확실한 번호를 찾지 못하면 []만 출력하십시오.
6. 설명 문장이나 마크다운 없이 아래 JSON 배열 형식만 출력하십시오.

[
  {{"name":"기관명","phones":["전화번호"],"purpose":"이 기관에 문의할 이유","source":"문서명 + 페이지"}}
]
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=list(uploaded_docs) + [extraction_prompt],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        candidates = _sanitize_document_contacts(
            _parse_json_array(response.text or ""),
            max_items=max_items,
        )
    except Exception:
        return []

    if not candidates:
        return []

    candidate_json = json.dumps(candidates, ensure_ascii=False)
    verification_prompt = f"""
당신은 공식 연락처 검증기입니다.
함께 제공된 참고 문서만 사용해 아래 후보를 검증하십시오.
인터넷, 기억, 일반지식은 사용하지 마십시오.

[사용자 질문]
{question}

[검증할 후보]
{candidate_json}

규칙:
1. 각 후보의 기관명과 전화번호가 참고 문서에 실제로 함께 명시되어 있는지 확인하십시오.
2. 전화번호가 한 자리라도 불확실하거나, 기관명과 번호의 연결이 명확하지 않으면 그 후보를 제거하십시오.
3. 질문과 무관한 기관도 제거하십시오.
4. source의 문서명과 페이지도 실제 위치와 맞아야 합니다.
5. 후보의 기관명이나 전화번호를 수정하거나 새로운 번호를 추가하지 마십시오. 검증된 후보만 그대로 남기십시오.
6. 설명 문장이나 마크다운 없이 JSON 배열만 출력하십시오. 모두 탈락하면 []만 출력하십시오.
"""

    try:
        verified_response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=list(uploaded_docs) + [verification_prompt],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        verified = _sanitize_document_contacts(
            _parse_json_array(verified_response.text or ""),
            max_items=max_items,
        )
    except Exception:
        return []


    candidate_keys = {
        (c["name"], tuple(c["phones"]))
        for c in candidates
    }
    final = [
        c for c in verified
        if (c["name"], tuple(c["phones"])) in candidate_keys
    ]
    return final[:max_items]


def merge_contacts(primary, secondary, max_items=3):
    result = []
    seen = set()
    for item in list(primary or []) + list(secondary or []):
        key = (item.get("name"), tuple(item.get("phones", [])))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= max_items:
            break
    return result


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


SHEETS_AVAILABLE = False
gc_sheet = None
chat_ws = None
unanswered_ws = None
feedback_ws = None
visitor_ws = None
SHEETS_ERROR = ""

try:
    gc_sheet = init_google_sheets()

    chat_headers = [
        "timestamp",
        "session_id",
        "user_question",
        "ai_response",
        "verification_status",
        "contact_route",
    ]
    unanswered_headers = [
        "timestamp",
        "session_id",
        "unanswered_question",
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
    SHEETS_AVAILABLE = True

except Exception as e:

    SHEETS_ERROR = f"{type(e).__name__}: {e}"


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
    if SHEETS_AVAILABLE and visitor_ws is not None:
        try:
            visitor_ws.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                st.session_state.session_id,
            ])
        except Exception:
            pass


DEFAULT_FILES = {
    "notice.pdf": "전세사기 피해주택 매입 통합 공고",
    "law.pdf": "전세사기피해자 지원 및 주거안정에 관한 특별법",
    "hug_2025_casebook.pdf": "전세피해지원 프로그램 및 전세피해 상담 사례집",
}


DOCUMENT_FILE_ALIASES = {
    "hug_2025_casebook.pdf": [
        "hug_2025_casebook.pdf",
        "2025전세피해지원사례집.pdf",
        "2025전세피해지원사례집(1).pdf",
    ],
}


def resolve_document_path(canonical_path: str):
    candidates = DOCUMENT_FILE_ALIASES.get(canonical_path, [canonical_path])
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


@st.cache_resource(show_spinner=False, ttl=60 * 60 * 24)
def upload_default_documents():
    docs = []
    filenames = []

    for canonical_path, display_name in DEFAULT_FILES.items():
        file_path = resolve_document_path(canonical_path)
        if not file_path:
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


def build_history_text(messages, max_messages=6):
    recent = messages[-max_messages:]
    lines = []
    for m in recent:
        role = "사용자" if m["role"] == "user" else "챗봇"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def make_system_prompt() -> str:
    guardrail = f"""

[초우선 필수 지침]
1. 사실 판단과 제도 설명은 반드시 다음 세 자료에서 확인하십시오.
   - 「전세사기피해자 지원 및 주거안정에 관한 특별법」
   - 「전세사기 피해주택 매입 통합 공고」
   - 「전세피해지원 프로그램 및 전세피해 상담 사례집」
2. 인터넷 검색, 일반 상식 보충, 기억에 의한 법률 지식 추가를 하지 마십시오.
3. 자료 간 내용이 충돌하면 법령을 최우선으로 하고, 다음으로 최신 LH 통합공고, 마지막으로 2025 HUG 상담 사례집을 참고하십시오. 다만 각 자료의 소관 범위가 다른 경우에는 해당 소관 자료의 구체적 안내를 사용하십시오.
4. 세 자료에 없는 내용 또는 세 자료만으로 확정할 수 없는 내용은 추측하지 마십시오.
5. 세 자료만으로 답변할 수 없으면 반드시 다음 문장으로 시작하십시오.
   "{FALLBACK_PHRASE}"
6. 답변 맨 아래에 반드시 한 줄로 출처를 표시하십시오. 형식은 다음 예시와 같습니다.
   출처: 「전세사기피해자 지원 및 주거안정에 관한 특별법」 제25조 / 「전세사기 피해주택 매입 통합 공고」 7쪽
   - 실제 답변에 사용한 자료만 적으십시오.
   - 특별법은 가능한 경우 정확한 조문 번호를 적으십시오.
   - 통합공고와 상담 사례집은 가능한 경우 정확한 PDF 페이지 번호를 적으십시오.
   - 존재하지 않는 조문이나 페이지를 만들어내지 마십시오.
7. 사용자에게 내부 파일명(notice.pdf, law.pdf 등)은 절대 표시하지 마십시오. 문서의 정식 제목만 표시하십시오.
8. 전화번호는 답변 본문에 직접 작성하지 마십시오. 공식 전화번호는 별도 코드가 검증된 연락처 디렉터리에서 추가합니다.
   - 사용자가 문의처를 물으면 어떤 종류의 기관이 적합한지 설명할 수는 있지만 전화번호 숫자는 생성하지 마십시오.
9. 「전세피해지원 프로그램 및 전세피해 상담 사례집」의 주요 상담 사례는 사용자의 질문과 유사한 상황을 이해하고 관련 지원제도·문의기관을 찾는 참고자료로 적극 활용하십시오. 사례집의 사례를 사용자의 사실로 단정하지 마십시오.
10. 세 자료의 내용을 폭넓게 활용해 사용자의 상황에 맞춰 답하되, 자료에 없는 사실을 보충하거나 추측하지 마십시오.
11. 사용자가 말하지 않은 개인 상황을 임의로 가정하지 마십시오.
"""

    base = (
        "당신은 전세피해지원 전문 상담 챗봇입니다. "
        "정확성을 최우선으로 하되, 사용자가 이해하기 쉬운 말로 핵심부터 설명하십시오. "
        "필요한 경우 절차를 단계별로 정리하십시오."
    )
    return base + guardrail


def generate_answer(question: str, history_text: str):
    system_prompt = make_system_prompt()

    user_payload = question
    if history_text:
        user_payload = (
            "[이전 대화 맥락]\n"
            f"{history_text}\n\n"
            "[현재 질문]\n"
            f"{question}\n\n"
            "이전 대화는 현재 질문의 생략된 대상을 이해하는 용도로만 사용하고, "
            "사실 판단은 반드시 「전세사기피해자 지원 및 주거안정에 관한 특별법」, "
            "「전세사기 피해주택 매입 통합 공고」, "
            "「전세피해지원 프로그램 및 전세피해 상담 사례집」에서 확인하십시오."
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
당신은 답변 검증기입니다. 제공된 세 자료만 이용해 아래 초안을 검증하십시오.
인터넷이나 외부 지식은 사용하지 마십시오.
자료 간 충돌 시 법령 > 최신 LH 통합공고 > 2025 HUG 상담 사례집 순으로 우선하되, 각 자료의 소관 범위가 다른 경우 해당 자료의 구체적 안내를 사용하십시오.

[질문]
{question}

[초안]
{draft}

검증 항목:
- 사실 주장마다 제공 자료에서 직접 확인 가능한가?
- 특별법 조문 번호가 실제 법령 내용과 일치하는가?
- 통합공고 또는 HUG 상담 사례집의 페이지 번호가 실제 PDF 페이지와 일치하는가?
- 답변 마지막에 '출처:' 한 줄이 있고, 실제 답변에 사용한 자료만 정확히 적혀 있는가?
- 내부 파일명(notice.pdf, law.pdf 등)이 사용자에게 노출되지 않았는가?
- 자료에 없는 내용을 추론하거나 만들어내지 않았는가?
- 답변 본문에 전화번호 숫자가 들어가 있지 않은가? 전화번호는 검증된 코드 디렉터리가 별도로 추가하므로 모델 답변에서는 전화번호를 제거해야 한다.
- 사용자가 문의처를 요청했다면 적합한 기관 유형에 대한 설명은 가능하지만, 전화번호 숫자는 생성하지 않아야 한다.
- HUG 상담 사례집의 사례를 사용자 개인의 사실처럼 단정하지 않았는가?

출력 규칙:
1) 완전히 문제없으면 정확히 한 줄만 출력: VERIFICATION_OK
2) 수정으로 해결 가능하면 첫 줄에 REVISED_ANSWER 라고 쓰고, 둘째 줄부터 수정된 최종 답변만 출력
   - 수정된 답변 맨 아래에는 반드시 정확한 '출처:' 한 줄을 포함하십시오.
   - 내부 파일명(notice.pdf, law.pdf 등)은 표시하지 마십시오.
   - 전화번호 숫자는 모두 제거하십시오. 공식 연락처는 별도 코드에서 추가됩니다.
3) 질문 자체는 자료로 답할 수 없더라도 관련 공식 문의기관 유형을 확인할 수 있으면 UNSUPPORTED 대신 REVISED_ANSWER로 출력하고,
   '{FALLBACK_PHRASE}'로 시작해 어떤 기관에 확인해야 하는지 설명하십시오. 전화번호 숫자는 쓰지 마십시오.
4) 질문에도 답할 수 없고 관련 문의기관도 자료에서 확인할 수 없으면 정확히 한 줄만 출력: UNSUPPORTED
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


    return FALLBACK_PHRASE, "VERIFY_PARSE_FAILED"


def render_feedback_form(form_key: str = "sidebar_feedback_form"):
    if st.session_state.feedback_submitted:
        st.success("🎉 의견 감사합니다. 서비스 개선에 반영하겠습니다.")
        return

    has_answer = any(m.get("role") == "assistant" for m in st.session_state.messages)
    if not has_answer:
        st.caption("챗봇 답변을 한 번 이상 이용한 뒤 설문에 참여할 수 있습니다.")
        return

    with st.form(form_key):
        st.markdown("**항목별 만족도 (1~5점)**")
        f_solving = st.slider("문제 해결 기여도", 1, 5, 5, key=f"{form_key}_solving")
        f_accuracy = st.slider("정보의 정확성", 1, 5, 5, key=f"{form_key}_accuracy")
        f_reliability = st.slider("정보의 신뢰성", 1, 5, 5, key=f"{form_key}_reliability")
        f_speed = st.slider("답변 속도", 1, 5, 5, key=f"{form_key}_speed")
        f_attitude = st.slider("챗봇 상담 태도", 1, 5, 5, key=f"{form_key}_attitude")
        f_readability = st.slider("가독성", 1, 5, 5, key=f"{form_key}_readability")
        f_efficiency = st.slider("정보 탐색 수고 절감", 1, 5, 5, key=f"{form_key}_efficiency")

        alt_action = st.radio(
            "이 챗봇이 없었다면 어떻게 해결했을 것 같나요?",
            options=[
                "LH 콜센터 상담이나 지사에 직접 전화 혹은 방문한다",
                "공고문과 특별법을 직접 찾아본다",
                "오픈채팅방을 이용한다",
                "인터넷에 검색한다",
                "다른 AI에게 물어본다",
                "포기한다",
                "기타",
            ],
            key=f"{form_key}_alt_action",
        )
        alt_action_other = st.text_input(
            "'기타'인 경우 직접 적어주세요.",
            key=f"{form_key}_alt_other",
        )

        time_saved = st.radio(
            "정보를 찾는 시간을 얼마나 단축했다고 생각하시나요?",
            options=["10분 이내", "30분 정도", "1시간 정도", "1시간 이상", "기타"],
            key=f"{form_key}_time_saved",
        )
        time_saved_other = st.text_input(
            "'기타'인 경우 직접 적어주세요.",
            key=f"{form_key}_time_other",
        )

        good_text = st.text_area(
            "가장 좋았거나 도움이 된 점",
            key=f"{form_key}_good",
        )
        improve_text = st.text_area(
            "개선이 필요한 점",
            key=f"{form_key}_improve",
        )

        submitted = st.form_submit_button("설문 제출하기", use_container_width=True)
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
                ) / 7,
                2,
            )

            final_alt_action = mask_personal_info(final_alt_action)
            final_time_saved = mask_personal_info(final_time_saved)
            good_text = mask_personal_info(good_text)
            improve_text = mask_personal_info(improve_text)

            if SHEETS_AVAILABLE and feedback_ws is not None:
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


with st.sidebar:
    st.header("⚙️ 챗봇 설정")

    st.subheader("📚 참고 문서")
    st.caption("챗봇이 답변할 때 참고하는 원문입니다.")

    for canonical_path, display_name in DEFAULT_FILES.items():
        st.markdown(f"**{display_name}**")
        file_path = resolve_document_path(canonical_path)
        if file_path:
            with open(file_path, "rb") as pdf_file:
                st.download_button(
                    "⬇️ PDF 다운로드",
                    data=pdf_file.read(),
                    file_name=canonical_path,
                    mime="application/pdf",
                    key=f"download_{canonical_path}",
                    use_container_width=True,
                )
        else:
            st.warning(f"PDF 파일을 찾을 수 없습니다: {canonical_path}")

    st.divider()

    st.caption("현재 대화 맥락을 지우고 새 상담을 시작하려면 아래 버튼을 눌러주세요.")
    if st.button("🔄 새 상담 시작 (대화 초기화)", use_container_width=True):
        st.session_state.messages = []
        st.session_state.open_feedback_form = False
        st.session_state.feedback_submitted = False
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

    with st.expander("📝 서비스 설문 참여"):
        st.caption("챗봇 이용 후 만족도와 개선 의견을 남겨주세요.")
        render_feedback_form()

    st.divider()

    with st.expander("🔒 관리자 전용 (데이터 분석)"):
        correct_pw = st.secrets.get("ADMIN_PASSWORD")

        if not SHEETS_AVAILABLE:
            st.info("현재 Google Sheets 로그 저장/관리자 통계 기능을 사용할 수 없습니다. 챗봇 상담 기능은 정상 이용할 수 있습니다.")
            if SHEETS_ERROR:
                st.caption(f"연동 오류: {SHEETS_ERROR}")
        elif not correct_pw:
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


st.header("🏠 LH 전세사기 피해주택 매입 Q&A 챗봇")
st.warning(
    "⚠️ **이용 전 꼭 확인해 주세요**\n\n"
    "이 챗봇의 답변은 참고 자료를 바탕으로 생성한 **참고용 정보**입니다. "
    "AI의 특성상 일부 내용이나 근거가 부정확할 수 있으며, 개인의 구체적인 상황에 따라 적용 결과가 달라질 수 있습니다.\n\n"
    "**신청, 경·공매, 계약, 금전 지급 등 중요한 의사결정 전에는 반드시 LH 지역본부 또는 관련 담당기관에 직접 확인해 주세요.**\n\n"
    "서비스 개선을 위해 질문과 답변이 저장될 수 있으며, 저장 전 전화번호·주민등록번호·이메일·상세주소 등 주요 개인정보를 마스킹합니다."
)
st.caption(DOCUMENT_FRESHNESS_CAUTION)
st.divider()


st.subheader("✅ LH 전세사기 피해주택 매입 신청 자격 요건 자가 진단")
st.markdown("정확하고 원활한 상담을 위해 **신청 자격 요건 3가지**를 먼저 확인해 주세요.")

check1 = st.checkbox(
    "국토부로부터 '전세사기피해자 지원 및 주거안정에 관한 특별법'에 따른 전세사기피해자 또는 신탁사기피해자로 결정된 임차인",
    key="diagnosis_check1",
)
check2 = st.checkbox(
    "경매 혹은 공매가 개시된 주택",
    key="diagnosis_check2",
)
check3 = st.checkbox(
    "내국인",
    key="diagnosis_check3",
)

st.write("")

if check1 and check2 and check3:
    st.success("✅ 핵심 확인 항목 3가지를 모두 체크했습니다.")
elif check1 or check2 or check3:
    st.warning("⚠️ 매입 신청을 위해서는 위 3가지 요건을 모두 충족해야 합니다.")

st.divider()


st.subheader("💬 챗봇 상담")
st.caption("궁금한 내용을 자유롭게 질문해 주세요.")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("궁금한 점을 입력해주세요 (예: 매입 사전협의는 언제 신청할 수 있나요?)"):

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)


    previous_messages = st.session_state.messages[:-1]
    history_text = build_history_text(previous_messages, max_messages=6)
    contact_context = "\n".join(
        m["content"] for m in previous_messages[-6:] if m["role"] == "user"
    )

    with st.chat_message("assistant"):
        try:


            contact_query = f"{contact_context}\n{prompt}"
            direct_contact_request = wants_contact_info(prompt)

            draft = generate_answer(prompt, history_text)

            verification_status = "NOT_RUN"
            skip_second_pass = False


            if FALLBACK_PHRASE in draft:
                ai_text = draft
                verification_status = "SKIPPED_FALLBACK"
                skip_second_pass = True


            elif direct_contact_request:
                ai_text = draft
                verification_status = "SKIPPED_CONTACT"
                skip_second_pass = True

            if not skip_second_pass:
                try:
                    ai_text, verification_status = verify_and_repair_answer(prompt, draft)
                except Exception:

                    ai_text = FALLBACK_PHRASE
                    verification_status = "VERIFY_API_FAILED"


            wants_contact = wants_contact_info(contact_query)


            answer_is_fallback = (
                FALLBACK_PHRASE in ai_text
                or verification_status in {
                    "UNSUPPORTED", "VERIFY_API_FAILED", "VERIFY_PARSE_FAILED", "SKIPPED_FALLBACK"
                }
            )
            should_recommend_contacts = wants_contact or answer_is_fallback
            contacts = []

            if should_recommend_contacts:

                hardcoded_contacts = recommend_contacts(
                    contact_query,
                    max_items=3,
                    include_generic=False,
                )


                document_contacts = []
                if _contacts_are_only_broad(hardcoded_contacts):
                    document_contacts = find_verified_document_contacts(
                        contact_query,
                        max_items=3,
                    )

                if document_contacts:


                    contacts = merge_contacts(
                        document_contacts,
                        hardcoded_contacts,
                        max_items=3,
                    )
                else:
                    contacts = hardcoded_contacts


                if not contacts:
                    contacts = recommend_contacts(
                        contact_query,
                        max_items=3,
                        include_generic=True,
                    )

            contact_route = ",".join(c["name"] for c in contacts)

            ai_text = strip_model_phone_numbers(ai_text)
            if contacts:
                ai_text = append_contact_recommendations(ai_text, contacts)

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


        masked_prompt = mask_personal_info(prompt)
        masked_ai_text = mask_personal_info(ai_text)

        if SHEETS_AVAILABLE and chat_ws is not None:
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

        if FALLBACK_PHRASE in ai_text and SHEETS_AVAILABLE and unanswered_ws is not None:
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
