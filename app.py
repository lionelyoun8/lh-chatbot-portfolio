import os
import uuid
from datetime import datetime
from google import genai
from google.genai import types
from google.genai import errors
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# 1. 웹 브라우저 탭 아이콘 및 제목 설정
st.set_page_config(
    page_title="LH 전세사기 피해주택 매입 Q&A 챗봇",
    page_icon="🏠",
    layout="wide"
)

# 2. Gemini 클라이언트 연결
client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])

# 3. 고유 세션 ID 및 대화 상태 초기화
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    st.session_state.messages = []

if "chat_started" not in st.session_state:
    st.session_state.chat_started = False

if "open_feedback_form" not in st.session_state:
    st.session_state.open_feedback_form = False

if "feedback_submitted" not in st.session_state:
    st.session_state.feedback_submitted = False

# ---------------------------------------------------------
# 📊 구글 시트(Google Sheets) 연동 및 워크시트 설정
# ---------------------------------------------------------
@st.cache_resource
def init_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)
    
    # "LH_Chatbot_Data" 스프레드시트 오픈
    sheet = gc.open("LH_Chatbot_Data")
    return sheet

def get_or_create_worksheet(sheet, title, headers):
    try:
        ws = sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=1000, cols=20)
        ws.append_row(headers)
    return ws

try:
    gc_sheet = init_google_sheets()
    
    # 탭별 워크시트 자동 생성 및 헤더 초기화
    chat_ws = get_or_create_worksheet(gc_sheet, "chat_logs", ["timestamp", "session_id", "user_question", "ai_response"])
    unanswered_ws = get_or_create_worksheet(gc_sheet, "unanswered_logs", ["timestamp", "session_id", "unanswered_question"])
    feedback_ws = get_or_create_worksheet(gc_sheet, "feedback_logs", [
        "timestamp", "session_id", "problem_solving", "accuracy", "reliability", 
        "speed", "attitude", "readability", "efficiency", 
        "alt_action", "time_saved", "avg_score", "good_feedback", "improve_feedback"
    ])
    visitor_ws = get_or_create_worksheet(gc_sheet, "visitors", ["timestamp", "session_id"])

except Exception as e:
    st.error(f"⚠️ 구글 시트 연동 중 오류가 발생했습니다. Secrets 설정을 확인해주세요. 상세 오류: {e}")
    st.stop()

# 방문자 수 카운팅 (세션당 1회 구글 시트에 기록)
if "counted_as_visitor" not in st.session_state:
    st.session_state.counted_as_visitor = True
    try:
        visitor_ws.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), st.session_state.session_id])
    except Exception as e:
        pass

# ---------------------------------------------------------
# 🌟 공고문 및 특별법 PDF 자동 내장
# ---------------------------------------------------------
DEFAULT_FILES = {
    "notice.pdf": "LH 전세사기 피해주택 매입 통합공고문",
    "law.pdf": "전세사기피해자 지원 및 주거안정에 관한 특별법"
}

if "uploaded_docs" not in st.session_state:
    st.session_state["uploaded_docs"] = []
    st.session_state["uploaded_filenames"] = []
    
    for file_path, display_name in DEFAULT_FILES.items():
        if os.path.exists(file_path):
            try:
                doc = client.files.upload(
                    file=file_path,
                    config={"display_name": display_name}
                )

                # PDF가 Gemini 서버에서 처리되는 동안 바로 질문을 보내지 않도록
                # ACTIVE 상태가 될 때까지 최대 30초간 확인합니다.
                import time
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
                    raise RuntimeError(
                        f"Gemini 파일 처리 대기시간 초과: {display_name} (상태: {state_name})"
                    )

                st.session_state["uploaded_docs"].append(doc)
                st.session_state["uploaded_filenames"].append(display_name)

            except Exception as e:
                st.warning(
                    f"문서 업로드 실패: {display_name} / "
                    f"{type(e).__name__}: {e}"
                )

# ---------------------------------------------------------
# 왼쪽 사이드바 (설정 및 데이터 대시보드)
# ---------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 챗봇 설정")
    
    st.subheader("📄 학습된 문서 목록")
    if "uploaded_filenames" in st.session_state and st.session_state["uploaded_filenames"]:
        for name in st.session_state["uploaded_filenames"]:
            st.info(f"✅ {name}")
    else:
        st.warning("학습된 기본 문서가 없습니다.")
    
    st.divider()

    persona = st.selectbox(
        "상담관 모드 선택",
        ["LH 전세피해지원 전문 상담관 (기본)", "따뜻하고 위로가 되는 상담관", "핵심만 짚어주는 요약 봇"]
    )
    
    if st.button("🔄 대화 내용 초기화"):
        st.session_state.messages = []
        st.session_state.chat_started = False
        st.session_state.open_feedback_form = False
        st.session_state.feedback_submitted = False
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

    st.divider()

    # 🔒 관리자 전용 대시보드 (구글 시트 데이터 연동)
    with st.expander("🔒 관리자 전용 (데이터 분석)"):
        admin_pw = st.text_input("관리자 비밀번호 입력", type="password", key="admin_pw_input")
        correct_pw = st.secrets.get("ADMIN_PASSWORD", "1128")
        
        if admin_pw == correct_pw:
            st.success("인증 성공! 대시보드 활성화")
            
            try:
                # 구글 시트에서 실시간 데이터 로드
                visitors_data = visitor_ws.get_all_records()
                chat_data = chat_ws.get_all_records()
                unanswered_data = unanswered_ws.get_all_records()
                feedback_data = feedback_ws.get_all_records()
                
                total_visitors = len(visitors_data)
                total_questions = len(chat_data)
                total_unanswered = len(unanswered_data)
                
                st.markdown("#### 📈 이용자 현황 (구글 시트 연동)")
                col1, col2 = st.columns(2)
                col1.metric("총 방문자 수", f"{total_visitors}명")
                col2.metric("총 질문 수", f"{total_questions}건")
                st.metric("🚨 미답변 발생 건수", f"{total_unanswered}건")
                
                st.markdown("#### 💾 데이터 다운로드")
                if total_questions > 0:
                    df_chat = pd.DataFrame(chat_data)
                    st.download_button("📥 전체 질문 로그 (.csv)", df_chat.to_csv(index=False).encode('utf-8-sig'), "chat_log.csv", "text/csv")
                        
                if total_unanswered > 0:
                    df_unanswered = pd.DataFrame(unanswered_data)
                    st.download_button("📥 미답변 질문 리스트 (.csv)", df_unanswered.to_csv(index=False).encode('utf-8-sig'), "unanswered_log.csv", "text/csv")
                
                if len(feedback_data) > 0:
                    df_feedback = pd.DataFrame(feedback_data)
                    overall_avg = round(df_feedback['avg_score'].mean(), 2)
                    st.metric(label="전체 평균 만족도", value=f"{overall_avg} / 5.0점")
                    st.download_button("📥 만족도 평가 결과 (.csv)", df_feedback.to_csv(index=False).encode('utf-8-sig'), "service_feedback.csv", "text/csv")
            except Exception as e:
                st.error(f"데이터를 불러오는 중 오류 발생: {e}")
                
        elif admin_pw:
            st.error("비밀번호가 올바르지 않습니다.")

# ---------------------------------------------------------
# 메인 화면
# ---------------------------------------------------------
st.header("🏠 LH 전세사기 피해주택 매입 Q&A 챗봇")
st.caption("이 챗봇은 '전세사기피해자 지원 및 주거안정에 관한 특별법'과 'LH 전세사기 피해주택 매입 통합공고' 문서를 기반으로 답변합니다.")

st.info(
    "⚠️ **안내 유의사항**\n\n"
    "본 챗봇은 LH 전세사기 피해주택 매입과 관련한 일반적인 이해를 돕기 위한 보조 도구일 뿐, "
    "**법률적 또는 행정적 공식 자문이나 효력을 갖지 않습니다.**\n\n"
    "개인의 구체적인 상황과 조건에 따라 적용 결과가 다를 수 있으므로, "
    "중요한 의사결정 및 신청 사항은 **반드시 LH 지역본부 또는 관련 기관에 직접 문의**하여 정확한 사실을 확인하시기 바랍니다."
)
st.divider()

if not st.session_state.chat_started:
    st.subheader("✅ LH 전세사기 피해주택 매입 신청 자격 요건 자가 진단")
    st.markdown("정확하고 원활한 상담을 위해 **신청 자격 요건 3가지**를 먼저 확인해 주세요.")
    
    check1 = st.checkbox("국토부로부터 '전세사기피해자 지원 및 주거안정에 관한 특별법'에 따른 전세사기피해자 또는 신탁사기피해자로 결정된 임차인")
    check2 = st.checkbox("경매 혹은 공매가 개시된 주택")
    check3 = st.checkbox("내국인")
    
    st.write("")
    
    if check1 and check2 and check3:
        st.success("🎉 1차 필수 요건을 모두 충족하셨습니다! 아래 버튼을 눌러 상담 챗봇을 시작하세요.")
        if st.button("🚀 챗봇 시작하기", type="primary"):
            st.session_state.chat_started = True
            st.rerun()
    elif check1 or check2 or check3:
        st.warning("⚠️ 매입 신청을 위해서는 위 3가지 요건을 모두 충족해야 합니다.")
        st.button("🚀 챗봇 시작하기", disabled=True)
else:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    hallucination_guardrail = """
    \n\n[초우선 필수 지침]
    1. 당신은 오직 제공된 문서(PDF)들의 내용만을 바탕으로 답변해야 합니다.
    2. 문서에 없는 내용이나 유추해야 하는 내용은 절대 지어내지 마십시오.
    3. 문서에서 정답을 찾을 수 없는 경우, 반드시 "해당 내용은 제공된 공고문 및 특별법에서 확인할 수 없습니다. 관할 지사에 문의하시기 바랍니다."라고만 답변하십시오.
    4. [근거 출처 필수 명시] 답변을 작성할 때, 내용의 근거가 되는 문서명(통합공고문 또는 특별법)과 함께 해당 조항, 장/절 등을 명시해 주십시오.
    """

    if persona == "LH 전세피해지원 전문 상담관 (기본)":
        system_prompt = "당신은 전세피해지원 담당 전문 상담관입니다. 지적이면서도 세심한 태도로, 정확한 근거 조항을 바탕으로 답변해 주세요." + hallucination_guardrail
    elif persona == "따뜻하고 위로가 되는 상담관":
        system_prompt = "당신은 전세피해자들의 마음을 어루만져주는 따뜻한 상담관입니다. 공감하고 위로하는 다정한 말투를 우선적으로 사용해 주세요." + hallucination_guardrail
    else:
        system_prompt = "당신은 핵심 요약 봇입니다. 인사말 없이 개조식으로 간결하게 요약해서 답변해 주세요." + hallucination_guardrail

    if prompt := st.chat_input("궁금한 점을 입력해주세요 (예: 특별법상 피해자 요건이 어떻게 되나요?)"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            contents_to_send = []
            if "uploaded_docs" in st.session_state:
                contents_to_send.extend(st.session_state["uploaded_docs"])
            contents_to_send.append(prompt)

            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents_to_send,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                    )
                )

                ai_text = response.text

                if not ai_text:
                    raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")

            except errors.APIError as e:
                status_code = getattr(e, "code", "unknown")

                st.error(
                    f"⚠️ Gemini API 요청에 실패했습니다. "
                    f"(HTTP {status_code})\n\n{e}"
                )

                if status_code == 401:
                    st.info(
                        "API 키가 잘못되었거나 만료되었을 가능성이 있습니다. "
                        "Streamlit Cloud → Settings → Secrets의 "
                        "GEMINI_API_KEY를 확인하세요."
                    )
                elif status_code == 403:
                    st.info(
                        "Gemini API 사용 권한 또는 Google Cloud 프로젝트 권한을 확인하세요."
                    )
                elif status_code == 429:
                    st.info(
                        "Gemini API 사용량 또는 요청 한도를 초과했을 가능성이 있습니다."
                    )
                elif status_code == 400:
                    st.info(
                        "요청 형식 또는 Gemini 파일 상태 문제일 가능성이 있습니다. "
                        "위 문서 업로드 상태 메시지도 확인하세요."
                    )

                st.stop()

            except Exception as e:
                st.error(
                    f"⚠️ Gemini 호출 중 예기치 않은 오류가 발생했습니다.\n\n"
                    f"{type(e).__name__}: {e}"
                )
                st.stop()

            st.markdown(ai_text)
            st.session_state.messages.append({"role": "assistant", "content": ai_text})
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 구글 시트에 질문 로그 기록
            try:
                chat_ws.append_row([timestamp, st.session_state.session_id, prompt, ai_text])
            except Exception as e:
                pass
                
            # 미답변 질문 감지 시 구글 시트에 기록
            fallback_phrase = "해당 내용은 제공된 공고문 및 특별법에서 확인할 수 없습니다"
            if fallback_phrase in ai_text:
                try:
                    unanswered_ws.append_row([timestamp, st.session_state.session_id, prompt])
                except Exception as e:
                    pass

            st.rerun()

    # ---------------------------------------------------------
    # 📝 채팅 하단 반응형 만족도 평가 및 피드백
    # ---------------------------------------------------------
    if len(st.session_state.messages) > 0:
        st.divider()
        st.markdown("### 💌 서비스 만족도 평가")
        st.info("💡 **여러분의 소중한 의견은 더 튼튼하고 정확한 피해자 지원 서비스를 만드는 데 밑거름이 됩니다.**")

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
                            "기타"
                        ]
                    )
                    alt_action_other = st.text_input("위 8번 문항에서 '기타'를 선택하신 경우, 직접 적어주세요.")
                    
                    st.write("")
                    
                    time_saved = st.radio(
                        "9. 이 챗봇 덕분에 정보를 찾는 시간을 대략 얼마나 단축했다고 생각하시나요?",
                        options=["10분 이내", "30분 정도", "1시간 정도", "1시간 이상", "기타"]
                    )
                    time_saved_other = st.text_input("위 9번 문항에서 '기타'를 선택하신 경우, 단축된 시간을 직접 적어주세요.")

                    st.divider()
                    st.markdown("#### [3단계] 상세 피드백")
                    st.caption("긍정적인 점과 아쉬운 점을 모두 남겨주시면 큰 도움이 됩니다!")
                    
                    good_text = st.text_area(
                        "10. 이 챗봇의 어떤 점이 가장 좋았거나 도움이 되셨나요? (장점)", 
                        placeholder="예: 복잡한 공고문을 쉽게 요약해줘서 좋았어요."
                    )
                    
                    improve_text = st.text_area(
                        "11. 더 나은 서비스를 위해 개선해야 할 점이 있다면 적어주세요. (개선점)", 
                        placeholder="예: 000에 대한 정보가 더 추가되면 좋겠어요."
                    )
                    
                    submitted = st.form_submit_button("피드백 제출하기")
                    if submitted:
                        final_alt_action = alt_action_other if alt_action == "기타" and alt_action_other else alt_action
                        final_time_saved = time_saved_other if time_saved == "기타" and time_saved_other else time_saved
                        
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        avg_score = round((f_solving + f_accuracy + f_reliability + f_speed + f_attitude + f_readability + f_efficiency) / 7, 2)
                        
                        try:
                            feedback_ws.append_row([
                                timestamp, st.session_state.session_id, f_solving, f_accuracy, f_reliability, f_speed, 
                                f_attitude, f_readability, f_efficiency, 
                                final_alt_action, final_time_saved, avg_score, good_text, improve_text
                            ])
                        except Exception as e:
                            pass
                        
                        st.session_state.feedback_submitted = True
                        st.rerun()
        else:
            st.success("🎉 따뜻한 의견 감사합니다. 더 나은 서비스로 보답하겠습니다.")