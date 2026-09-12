# 선생앱 커뮤니티 개편 SSOT 설계문서

> **Version:** V1.1.1
> **Status:** ACTIVE (설계 확정, 구현 착수 전)
> **Last Updated:** 2026-03-19

---

## 1. 목적

선생앱 커뮤니티 영역은 다음 역할을 담당한다:
- 학생 대상 공지 전달
- 강의/차시 단위 소통
- 자료 배포
- 질문 답변
- 상담 요청 처리

### 1-1. 현재 문제 (확인된 사실)

| # | 문제 | 코드 확인 결과 | 심각도 |
|---|------|---------------|--------|
| 1 | 게시 대상(scope)과 조회(view)가 혼재 | `CommunityScopeContext`가 scope를 하나로 관리. 좌측 트리가 조회/게시 겸용 | 구조적 |
| 2 | 읽기 화면에서 에디터 UI 노출 | Board/Materials 상세에서 `RichTextEditor`가 항상 렌더됨 (readOnly 미사용) | UX 혼란 |
| 3 | raw HTML 사용자 노출 | `stripHtml()` 유틸 존재하나 일부 컨텍스트에서 미적용. `<p>` 태그 직접 노출 | 버그 |
| 4 | 공지/게시판/자료실이 동일 UI 틀 | Board/Materials가 거의 동일 코드. 도메인 특성 사라짐 | UX |
| 5 | 정보 위계 약함 | 대상 배지, 상태 배지, 카드 경계, 강조가 부족 | 디자인 |

### 1-2. 개편 목표

1. 게시 대상(scope)을 명확히 모델링, 조회(view)와 분리
2. 읽기/편집 책임 분리
3. 선생앱은 **업무형 SNS UX**, 학생앱은 **소비형 UX**로 분리
4. 데이터 1:1 매핑 유지
5. 운영 데이터 보호 (기존 데이터 의미 불변)

---

## 2. 핵심 개념 정렬

### 2-1. 게시 대상 (Scope)

커뮤니티 글은 반드시 하나의 게시 대상을 가진다.

| scope_type | 대상 | 의미 |
|-----------|------|------|
| `global` | 테넌트 소속 학생 전체 | 전체 대상 |
| `lecture` | 특정 강의 소속 학생 | 강의 대상 |
| `session` | 특정 강의의 특정 차시 소속 학생 | 차시 대상 |

### 2-2. 조회 필터 (View)

조회는 게시 대상과 다르다. 관리자가 어떤 범위의 글을 보는지를 뜻한다.

| 조회 필터 | 의미 |
|----------|------|
| 전체 보기 | 모든 글 모아보기 (scope 무관) |
| 전체 대상만 | global scope 글만 |
| 강의별 보기 | 특정 강의 관련 글만 |
| 차시별 보기 | 특정 차시 관련 글만 |

**절대 규칙:** scope와 view는 혼용되면 안 된다.

### 2-3. 용어 SSOT

"전체"라는 단어가 충돌하므로 분리한다.

| 금지 | 권장 |
|------|------|
| "전체" = 전체 학생 대상 | **전체 대상** |
| "전체" = 모든 글 모아보기 | **전체 보기** |
| 둘 다 "전체"로 부르는 것 | 명확히 분리 |

---

## 3. 엔티티 모델 SSOT

### 3-1. 현재 백엔드 모델 (변경 없음, 해석만 정리)

현재 모델은 이미 scope 개념을 지원한다. 변경이 아닌 **해석 정리**가 필요하다.

**PostEntity** (기존 유지):
```
id, tenant_id, post_type, title, content,
created_by, author_display_name, author_role,
is_urgent, created_at
```

**ScopeNode** (기존 유지):
```
id, tenant_id, level (COURSE|SESSION),
lecture_id, session_id, parent_id
```

**PostMapping** (기존 유지):
```
post_id, node_id (M:N)
```

### 3-2. Scope 해석 규칙 (SSOT)

| 상태 | scope_type 해석 |
|------|----------------|
| PostMapping 없음 (mappings 비어있음) | `global` — 전체 대상 |
| PostMapping → COURSE node | `lecture` — 강의 대상 |
| PostMapping → SESSION node | `session` — 차시 대상 |

### 3-3. Scope 무결성 규칙

| scope_type | lecture_id | session_id | 유효 |
|-----------|-----------|-----------|------|
| global | null | null | O |
| lecture | not null | null | O |
| session | not null | not null | O |
| session | null | not null | **X — 금지** |
| lecture | not null | not null | **X — 금지** |
| global | not null | any | **X — 금지** |

**현재 상태:** 백엔드의 `CommunityService.create_post()`가 `node_ids`를 받아 매핑 생성. 무결성 검증은 ScopeNode의 unique constraint `(tenant, lecture, session)`으로 간접 보장. 추가 validation 불필요 — 기존 구조로 충분.

---

## 4. Audience Resolution SSOT

학생이 어떤 글을 볼 수 있는지는 글의 scope로 계산한다.

### 4-1. 현재 백엔드 로직 (변경 없음)

```python
# 현재 views.py 구현 (정확히 이 로직)
# 1. global (mappings 없음) → 테넌트 소속 학생 전체 노출
# 2. lecture/session (mappings 있음) → 해당 강의 수강생만 노출
enrolled_lecture_ids = Enrollment.filter(student=student, status="ACTIVE")
visible_node_ids = ScopeNode.filter(lecture_id__in=enrolled_lecture_ids)
scoped_post_ids = PostMapping.filter(node_id__in=visible_node_ids)
qs.filter(Q(mappings__isnull=True) | Q(id__in=scoped_post_ids))
```

### 4-2. 타입별 가시성 규칙

| post_type | global (mappings 없음) | scoped (mappings 있음) |
|-----------|----------------------|----------------------|
| notice | 전체 학생 | 수강생만 |
| board | 전체 학생 | 수강생만 |
| materials | 전체 학생 | 수강생만 |
| qna | 전체 학생 | 작성자 본인만 |
| counsel | 작성자 본인만 | 작성자 본인만 |

### 4-3. 핵심 원칙

같은 scope면, 공지든 자료실이든 게시판이든 **같은 학생 노출 결과**가 나와야 한다.
이것이 선생앱↔학생앱 1:1 매핑의 핵심이다.

---

## 5. 커뮤니티 타입별 목적 정의

### 5-1. 공지사항 (notice)
- **목적:** 공적 전달, 운영 안내, 시험 일정, 휴강/보강, 필독 전달
- **특징:** 중요도 높음, 상단 고정 가능, 읽기 중심, 댓글 optional
- **핵심 정보:** 제목, 대상 배지, 중요 배지, 고정 여부, 작성일, 읽음 수

### 5-2. 게시판 (board)
- **목적:** 자유 소통, 수업 관련 커뮤니티, 가벼운 글
- **특징:** 댓글 중심, 카드형 피드, 최근 활동 중요
- **핵심 정보:** 작성자 아바타, 제목, excerpt, 대상 배지, 댓글 수, 최근 활동

### 5-3. 자료실 (materials)
- **목적:** 파일/자료 배포
- **특징:** 첨부파일이 중심, 본문은 보조
- **핵심 정보:** 제목, 파일 수, 파일명/확장자/크기, 대상 배지, 업로드 일시

### 5-4. QnA (qna)
- **목적:** 학생 질문 → 선생 답변
- **특징:** 상태 중요, 질문-답변 타임라인, 이전 질문 이력 필요, 처리 중심
- **핵심 정보:** 학생명, 상태 배지(답변대기/완료), 질문 시간, 강의/차시

### 5-5. 상담 신청 (counsel)
- **목적:** 학생/학부모 요청 접수, 선생/운영자 처리
- **특징:** 티켓형, 처리상태, 담당자, 메모

학생과 학부모는 같은 질문·상담 작성 계약을 사용한다. 학부모 쓰기는 반드시 현재
tenant에서 활성 연결된 자녀의 `X-Student-Id`를 포함하며 서버가 `created_by`를 그
자녀로 고정하고 `author_role=parent`를 기록한다. payload의 다른 `created_by` 값은
권한을 넓히지 않는다. 학부모는 선택 자녀의 `qna|counsel`만 작성하고 그 글의 첨부를
추가·삭제하거나 글을 수정·삭제할 수 있다. 헤더 누락, 미소유·삭제·다른 tenant 자녀,
선택한 자녀와 글 작성자 불일치는 object-store/DB 쓰기 전에 `403` 또는 객체 비공개
`404`로 실패 폐쇄한다.
게시판·공지·자료실, 댓글·좋아요·신고의 기존 학부모 제한은 유지한다.

---

## 6. 읽기/편집 모드 분리 SSOT

### 6-1. 읽기 모드 (Read Mode)

**표시 항목:**
- 제목
- 작성자 + 역할 배지
- 작성일 (상대 시간)
- 대상 배지 (전체 대상 / 강의명 / 차시명)
- 본문 렌더링 결과 (sanitized HTML render)
- 첨부파일 카드
- 댓글/답변/처리이력

**금지:**
- 에디터 툴바 노출
- 입력창처럼 보이는 본문 박스
- 수정 가능해 보이는 UI

### 6-2. 편집 모드 (Edit Mode)

**표시 항목:**
- 제목 입력
- 대상 선택
- RichTextEditor
- 첨부 추가/삭제
- 저장/취소

**진입 조건:**
- 새 글 작성 시
- "수정" 버튼 클릭 후에만

### 6-3. 현재 코드와의 갭

| 컴포넌트 | 현재 | 목표 |
|---------|------|------|
| `BoardAdminPage.tsx` 상세 | RichTextEditor 항상 렌더 | 읽기: HTML render / 편집: Editor |
| `MaterialsBoardPage.tsx` 상세 | RichTextEditor 항상 렌더 | 읽기: HTML render / 편집: Editor |
| `NoticeAdminPage.tsx` 상세 | RichTextEditor 항상 렌더 | 읽기: HTML render / 편집: Editor |
| `QnaReadPage.tsx` 질문 | `white-space: pre-wrap` div | 유지 (이미 읽기 전용) |

**구현 방법:** `RichTextEditor`에는 이미 `readOnly` prop이 있음. 하지만 readOnly에서도 툴바가 보일 수 있으므로, 읽기 모드에서는 Editor 대신 `dangerouslySetInnerHTML` + DOMPurify sanitize로 렌더링.

---

## 7. HTML 처리 SSOT

### 7-1. 저장
- `content` 필드에 HTML 저장 (기존 유지)

### 7-2. 목록 Preview
- HTML 제거 → plain text 변환 → 최대 80자 excerpt
- 기존 `stripHtml()` 함수 사용 (이미 존재)
- **적용 확인 필요:** 모든 리스트 카드에서 `stripHtml(post.content)` 호출 여부

### 7-3. 상세 읽기
- sanitize 후 render
- 허용 태그: p, br, strong, em, u, s, h2, h3, ul, ol, li, a, img, blockquote, code
- script, style, on* 이벤트, iframe 제거
- **구현:** DOMPurify 또는 자체 sanitizer

### 7-4. 수정
- editor 전용 value로 사용

### 7-5. 금지
- raw `<p>`, `<br>`, `<div>` 사용자 노출
- 목록에서 HTML literal 출력

---

## 8. 레이아웃 구조 SSOT

### 8-1. 좌측 패널: 조회 트리/필터

**역할:** 지금 어떤 범위의 글을 보고 있는지 선택

**구성:**
```
전체 보기          (모든 글)
대상별 보기
├─ 전체 대상       (global scope 글만)
├─ 강의별
│  ├─ [칩] 수학 기초반     (12)
│  │  ├─ · 1차시            (3)
│  │  └─ · 2차시            (5)
│  └─ [칩] 영어 고급반      (8)
│     └─ · 1차시            (2)
```

**중요:** 좌측은 **조회 트리**다. 게시 대상을 결정하는 UI와 혼동되면 안 된다.

### 8-2. 가운데 패널: 리스트

**역할:** 현재 필터에 맞는 글 목록, 타입별 다른 카드 UI

**포함:**
- 상단 컨텍스트 바 (현재 범위 배지)
- 검색
- 정렬/필터
- 새 글 작성 버튼

### 8-3. 우측 패널: 상세 읽기

**역할:** 선택한 항목의 read-only 상세. 수정 시에만 editor 모달/인라인으로 전환.

**디자인:** SNS 게시물/업무카드 형태 (에디터 아님)

---

## 9. 상단 컨텍스트 바 SSOT

각 화면 상단에 항상 현재 맥락을 표시한다.

**예시:**
```
게시판 · 전체 보기
공지사항 · 강의 대상 · 수학 기초반
자료실 · 차시 대상 · 수학 기초반 > 1차시
QnA · 답변 대기 · 3건
```

이 줄 하나가 있어야 사용자가 맥락을 잃지 않는다.

---

## 10. 글 작성 UX SSOT

글쓰기 버튼을 누르면, 가장 먼저 묻는 건 본문이 아니라 **대상**이다.

### 10-1. 작성 순서
1. 타입 확인 (공지/게시판/자료실)
2. **대상 선택** (전체 대상 / 강의 대상 / 차시 대상)
3. 제목
4. 본문
5. 첨부
6. 게시/임시저장

### 10-2. 대상 선택 UI
```
게시 대상:
○ 전체 대상
○ 강의 대상  → [강의 선택 dropdown]
○ 차시 대상  → [강의 선택] → [차시 선택]
```

### 10-3. 대상 확인 문구 (항상 표시)
```
"이 글은 [수학 기초반 1차시] 학생에게만 보입니다."
"이 공지는 전체 학생에게 보입니다."
```

---

## 11. 타입별 화면 설계

### 11-1. 공지사항

**리스트 카드:**
- [전체 대상] [중요] 3월 시험 일정 공지
- 수학 기초반 · 3월 19일 · 읽음 67

**상세 (읽기 모드):**
- 제목, 대상 배지, 중요도, 작성자, 작성시각
- 본문 (sanitized HTML render)
- 첨부, 읽음 통계
- 수정/삭제 버튼

### 11-2. 게시판

**리스트 카드:**
- 작성자 아바타 + 제목 + excerpt
- 대상 배지 + 댓글 수 + 최근 활동

**상세 (읽기 모드):**
- SNS형 게시물 카드: 작성자/시간/대상 → 본문 → 첨부 → 댓글 스레드
- "수정" 누르기 전엔 에디터 안 보임

### 11-3. 자료실

**리스트 카드:**
- 제목 + 설명 excerpt + 대상 배지
- 파일 수 + 작성자 + 업로드 일시

**상세 (읽기 모드):**
- 제목, 대상 배지, 설명 본문
- **첨부파일 카드 리스트** (파일명 + 확장자 + 크기 + 다운로드)
- 첨부파일이 본문보다 더 눈에 띄어야 한다

### 11-4. QnA

**좌측 필터:**
- 전체 질문 / 답변 대기 / 답변 완료 / 강의별 / 차시별

**리스트 카드:**
- 학생명 + 질문 제목 + excerpt + 상태 배지 + 시간

**상세 (읽기 모드):**
- 대화형 타임라인: 학생 질문 → 선생 답변
- 이전 질문 이력
- 답변 입력/수정

### 11-5. 상담 신청

**리스트:**
- 학생/학부모명 + 상담 유형 + 신청 시간 + 상태 + 담당자

**상세:**
- 신청 내용 + 연락 정보 + 희망 시간
- 내부 메모 + 처리 상태 변경 + 완료 처리

---

## 12. 배지 체계 SSOT

모든 배지는 통일된 디자인으로 일관성을 유지한다.

### 12-1. 대상 배지 (Scope Badge)
| 배지 | 의미 | 톤 |
|------|------|-----|
| `전체 대상` | global | primary/blue |
| `[강의명]` | lecture | neutral/gray |
| `[강의명] > [차시명]` | session | neutral/gray |

### 12-2. 상태 배지
| 배지 | 의미 | 톤 |
|------|------|-----|
| `중요` | 긴급 공지 | danger/red |
| `고정` | 핀 고정 | warning/amber |
| `답변 대기` | QnA 미처리 | warning/orange |
| `답변 완료` | QnA 처리됨 | success/green |

### 12-3. 역할 배지
| 배지 | 의미 | 톤 |
|------|------|-----|
| `관리자` | staff 작성 | primary |
| `학생` | student 작성 | neutral |

---

## 13. 학생앱 매핑 SSOT

선생앱과 학생앱은 같은 데이터를 서로 다르게 보여준다.

### 13-1. 공통 (불변)
- 같은 post id
- 같은 scope (PostMapping)
- 같은 audience resolution
- 같은 attachments
- 같은 댓글/답변 연결

### 13-2. 차이
| 관점 | 선생앱 | 학생앱 |
|------|--------|--------|
| 목적 | 작성/수정/삭제/처리 | 읽기/응답 |
| 정보 | 관리 정보 노출 (대상, 상태, 통계) | 내가 볼 수 있는 것만 |
| UI | 업무형 SNS | 소비형 카드 |

---

## 14. 상태 전이 SSOT

### 공지/게시판/자료실
```
draft → published → archived → deleted
```

### QnA
```
waiting → answered → closed
```

### 상담
```
requested → in_progress → completed → cancelled
```

**현재 상태:** 백엔드에 명시적 status 필드 없음 (QnA는 replies_count로 간접 판단). 향후 status 필드 추가 시 기존 데이터에 backfill 필요.

---

## 15. 권한 규칙 SSOT

| 역할 | 권한 |
|------|------|
| 선생 | 본인 작성 글 작성/수정, 권한 있으면 강의/차시 대상 지정 |
| 원장/관리자 | 전체 대상 포함 모든 글 작성, 중요공지/고정/삭제 |
| 학생 | 본인 audience 글만 조회, 허용 타입에만 댓글/질문 |
| 학부모 | 선택 자녀의 질문·상담 작성/수정/삭제와 첨부 관리; 그 외 쓰기는 제한 |

---

## 16. 구현 우선순위

### P0 — 즉시 수정 (정합성/안전)

| 항목 | 변경 범위 | 백엔드 변경 |
|------|----------|-----------|
| scope/view 개념 분리 (용어 + UI) | 프론트 | 없음 |
| raw HTML 노출 제거 | 프론트 | 없음 |
| read/edit 모드 분리 | 프론트 | 없음 |
| 대상 배지 노출 | 프론트 | 없음 |
| 상단 컨텍스트 바 추가 | 프론트 | 없음 |
| 좌측 트리 네이밍 변경 | 프론트 | 없음 |

### P1 — 도메인 분화

| 항목 | 변경 범위 | 백엔드 변경 |
|------|----------|-----------|
| 공지/게시판/자료실 리스트 카드 분화 | 프론트 | 없음 |
| 자료실 상세: 파일 카드 우선 | 프론트 | 없음 |
| QnA 타임라인 정리 | 프론트 | 없음 |
| 첨부파일 영역 카드화 | 프론트 | 없음 |
| 글 작성 시 대상 선택 강화 | 프론트 | 없음 |

### P2 — 경험 고도화

| 항목 | 변경 범위 | 백엔드 변경 |
|------|----------|-----------|
| 읽음 상태 | 프론트+백엔드 | 새 모델 필요 |
| 고정 공지 (is_pinned) | 프론트 | 이미 지원 가능 |
| 예약 게시 | 프론트+백엔드 | published_at 필드 |
| 임시저장 | 프론트+백엔드 | status 필드 추가 |
| 학생앱 소비형 최적화 | 프론트 | 없음 |

---

## 17. 운영 데이터 영향도

### P0 변경사항: 운영 데이터 영향 없음

| 항목 | 영향 |
|------|------|
| 기존 PostEntity | 변경 없음 |
| 기존 PostMapping | 변경 없음 |
| 기존 ScopeNode | 변경 없음 |
| 기존 PostReply | 변경 없음 |
| 기존 PostAttachment | 변경 없음 |
| 백엔드 API 응답 | 변경 없음 |
| 학생앱 데이터 흐름 | 변경 없음 |

**P0는 100% 프론트엔드 변경이다.** 백엔드 모델, API, 서비스 로직 변경 없음.
기존 운영 데이터의 의미, 연결, 통계가 깨지지 않는다.

### P1 변경사항: 운영 데이터 영향 없음
- 카드 디자인 변경은 표현 계층만 변경
- 기존 API 응답을 다르게 렌더링하는 것뿐

### P2 변경사항: 백엔드 변경 필요 (별도 설계)
- 읽음 상태: 새 모델 `PostReadLog` 필요
- 예약 게시: `published_at` 필드 추가 + 기존 데이터 backfill
- 임시저장: `status` 필드 추가 + 기존 데이터 default='published'

---

## 18. 반드시 막아야 할 오류

| 금지 사례 | 이유 |
|----------|------|
| 전체 보기에서 전체 대상 글과 개념 혼동 | scope/view 혼용 |
| raw HTML 노출 | 시스템 깨짐처럼 보임 |
| read mode인데 toolbar 노출 | UX 혼란 |
| session 대상인데 lecture 연결 누락 | 데이터 무결성 |
| 학생앱에서 잘못된 audience 노출 | 보안 |
| 공지/자료/게시판이 동일 스타일 | 타입 인지 불가 |
| 게시 대상이 상세 화면에 안 보임 | 맥락 상실 |

---

## 19. 디자인 원칙

### 19-1. 핵심 원칙
예쁜 것보다 먼저:
- **무엇인지** 바로 보일 것
- **누구 대상인지** 바로 보일 것
- **읽기 상태인지 수정 상태인지** 바로 보일 것

### 19-2. 시각 위계 (강조 순서)
1. 제목
2. 대상 배지
3. 상태/중요도
4. 작성자/시간
5. 본문 요약
6. 부가 메타

### 19-3. 카드성 강화
- 카드 경계 강화
- 선택 카드 명확화
- 섹션 간 간격 확대
- 제목과 본문 간 대비 강화

---

## 20. 컴포넌트 구조

### 공통
- `CommunityScopeBadge` — 대상 배지 (전체 대상/강의/차시)
- `CommunityContextHeader` — 상단 컨텍스트 바
- `CommunityTreeFilter` — 좌측 조회 트리 (CmsTreeNav 개선)
- `PostReadView` — 읽기 전용 게시물 렌더링
- `AttachmentCard` — 파일 카드
- `CommentThread` — 댓글 스레드

### 타입별 리스트 카드
- `NoticeListCard` — 공지 카드 (중요/고정/대상/읽음 수)
- `BoardListCard` — 게시판 카드 (아바타/댓글/활동)
- `MaterialListCard` — 자료 카드 (파일 수/크기)
- `QnaListCard` — QnA 카드 (상태/학생명)
- `CounselingListCard` — 상담 카드 (상태/담당자)

### 폼
- `ScopeSelector` — 대상 선택 (전체/강의/차시)
- `PostComposeForm` — 글 작성 폼
- `QnaAnswerForm` — QnA 답변 폼

---

## 21. 검증 체크리스트

### P0 구현 후 검증 항목
- [ ] 모든 리스트 카드에서 raw HTML 미노출
- [ ] 상세 뷰에서 에디터 툴바 미노출 (읽기 모드)
- [ ] "수정" 클릭 후에만 에디터 표시
- [ ] 대상 배지가 리스트 카드와 상세 뷰에 모두 표시
- [ ] 상단 컨텍스트 바에 현재 조회 범위 표시
- [ ] 좌측 트리: "전체 보기" / "강의별" 네이밍 변경
- [ ] 글 작성 시 대상 선택 UI 동작
- [ ] 학생앱에서 기존 공지/QnA/게시물 정상 노출
- [ ] 기존 운영 데이터 읽기 정상
- [ ] 새로 작성한 글이 학생앱에 정상 반영

## 22. 게시글·첨부 삭제의 저장소 내구성

일반 커뮤니티의 `DELETE /api/v1/community/posts/:id/`와
`DELETE /api/v1/community/posts/:id/attachments/:att_id/`는 권한 확인 후
메타데이터 삭제와 정확한 원본 정리 의도를 같은 DB transaction에 기록한다.
게시글 삭제는 해당 글의 첨부 전체를 열거하며, 첨부 단건 삭제는 글 내용과
다른 첨부를 보존한다. DB 삭제 또는 intent 저장 실패/outer rollback에서는
메타데이터도 원본도 삭제하지 않는다. 실제 R2 DELETE는 outer commit 후에만 실행한다.

- tenant와 글·첨부의 tenant 관계가 정확해야 한다. 학생은 본인 글,
  학부모는 명시적으로 선택한 자녀의 QnA/상담이라는 기존 쓰기 권한을 유지한다.
- 지원 문의는 [개발자 문의 운영함](../operations/dev-console-inbox.md)의 제출 후
  기록 보존 계약을 유지한다. 일반 게시글 queryset에서 제외되어 게시글 DELETE는
  404이며, 지원 첨부 DELETE는 403이다. 일반 조회 범위를 넓혀 지원 기록을 노출하지 않는다.
- 원본은 실제 `PostAttachment.r2_key`만 대상으로 삼는다. 현재
  `tenants/<tenant>/community/posts/<post>/...` 및 기존 global exact key를 지원한다.
  다른 tenant namespace나 잘못 연결된 첨부 graph는 `409 storage_cleanup_scope_mismatch`로
  DB/provider 변경 전에 중단한다. prefix 열거/추정 삭제를 하지 않는다.
- 다른 tenant를 포함한 surviving storage owner가 같은 키를 참조하면 원본을 보존한다.
  이 키는 물리 삭제 완료 수에 넣지 않는다. 기존 storage owner registry와 처리 직전
  재검사를 그대로 사용한다([저장소 소유 계약](inventory-storage.md)).
- 업로드와 삭제는 같은 post row → 정렬된 exact storage key 순서로 잠근다.
  기존 cleanup processor는 exact key lock → owner **조회**만 하며 post row lock을
  취하지 않는다. 동일 idempotency key 재업로드의 PUT/metadata commit과 정리를
  직렬화해, 정리가 새 원본을 지우거나 업로드가 삭제된 원본 metadata를 남기지 않게 한다.
  SQLite는 이 잠금의 동시성 증거가 아니며 PostgreSQL 회귀가 별도 필요하다.

### 응답과 재조회

정상 삭제는 기존 `204`를 유지한다. DB 삭제가 commit됐지만 원본 정리 일부가
실패하거나 대기 중이면 `502`를 반환한다. 이 응답은 DB rollback을 뜻하지 않는다.

```json
{
  "code": "community_storage_cleanup_pending",
  "detail": "목록에서 삭제되었습니다. 원본 파일 정리는 재시도 대기 중입니다.",
  "deleted": {"posts": 1, "attachments": 2, "r2_objects": 1},
  "storage_cleanup": {"pending": 0, "failed": 1, "cleaned": 1}
}
```

`deleted`는 boolean이 아니라 정확한 DB 삭제 수와 물리 정리 완료 수이다.
`pending`(PENDING/PROCESSING), `failed`, `cleaned`는 서로 배타적이고,
미완료 수는 `pending + failed`이다. raw object key/provider 예외는 응답하지 않는다.
502 뒤에도 GET은 삭제된 첨부를 제외하며 삭제된 글은 404이다. 프런트엔드는
authoritative 목록/detail/count 재조회와 이 partial detail을 표시하고, 삭제된
상세에서 정상적으로 이탈해야 한다. 전체 성공이나 원본 그대로라는 실패 안내를
하지 않고, 같은 DELETE를 자동 재시도하지 않는다. 이 BE 변경만으로 FE companion의
실제 화면·reload 검증이 완료됐다고 간주하지 않는다.

외부 transaction 안에서 호출돼 callback이 아직 실행되지 않은 경우도 pending을
숨기지 않는다. 응답 결정 시 미완료면 502이며, 바깥 transaction rollback은 모든
DB 전환과 callback을 함께 취소한다. 204 양성 증거는 실제 autocommit 요청 경계에서
processor 완료를 확인하는 TransactionTestCase로 검증한다.

### 기존 outbox 처리와 검증

기존 `SubmissionStorageCleanupIntent` 및 processor를 재사용하며 새 테이블/queue는 없다.
failed/pending과 15분 이상 stale PROCESSING은 기존 command
`process_submission_storage_cleanup --limit 1000`으로 재시도하고 cleaned 키는 다시
삭제하지 않는다. `purge_deleted_students`도 학생 삭제 대상 유무와 무관하게 이
processor를 먼저 호출한다. `infra/terraform/purge_schedule.tf`의 EventBridge rule
`${var.naming_prefix}-purge-soft-deleted`(현재 SSOT `academy-v1-purge-soft-deleted`),
`cron(15 18 * * ? *)` → target `SsmRunCommandPurgeSoftDeleted` → `tag:Name=academy-v1-api`
→ `docker exec academy-api python manage.py purge_deleted_students`가 기존 예약 실행이다.
즉시 성공/실제 운영 스케줄 정상 실행을 응답으로 보장하지 않는다. 한 번의 처리 한도는
1000이며 실패·잔여는 intent 상태와 command의 cleaned/failed/deferred 수로 관측한다.

검증 소유는 `tests/test_community_delete_durability.py`이다. provider 실패와 재시도
수렴, cascade/rollback, legacy/공유/타 tenant 보호, 기존 purge consumer, 정확 키
재업로드 양방향 PostgreSQL 경합을 포함한다. 기존 inventory, community upload,
parent-selected-child 및 support inbox 회귀도 함께 통과해야 한다. 실제 원본 부재와
paired UI 성공/partial/reload는 승인된 isolated QA에서 별도 확인해야 하며 로컬 mock이나
미실행 PostgreSQL 검사를 운영 성공으로 기록하지 않는다.
