# 선생님 전용 모바일 앱 설계 문서

**Version:** 1.0.0  
**Status:** DRAFT  
**Created:** 2026-04-15  

---

## 목차

1. [배경 및 동기](#1-배경-및-동기)
2. [현황 분석](#2-현황-분석)
3. [설계 원칙](#3-설계-원칙)
4. [기술 아키텍처](#4-기술-아키텍처)
5. [앱 구조](#5-앱-구조)
6. [핵심 기능 설계](#6-핵심-기능-설계)
7. [네비게이션 & UX 설계](#7-네비게이션--ux-설계)
8. [API 전략](#8-api-전략)
9. [오프라인 & 성능](#9-오프라인--성능)
10. [푸시 알림](#10-푸시-알림)
11. [테넌트 & 보안](#11-테넌트--보안)
12. [구현 로드맵](#12-구현-로드맵)
13. [기존 시스템과의 관계](#13-기존-시스템과의-관계)
14. [리스크 & 제약](#14-리스크--제약)

---

## 1. 배경 및 동기

### 1.1 왜 전용 앱인가

현재 선생님 화면은 `app_admin`의 반응형 레이아웃으로 제공된다. `useIsMobile()` (1024px 이하)로 모바일 레이아웃을 전환하지만, 이는 **데스크톱 앱을 작은 화면에 끼워 맞춘 것**이지 모바일 전용 경험이 아니다.

**현재 모바일 경험의 구조적 한계:**

| 문제 | 상세 |
|------|------|
| 테이블 기반 UI | 출석, 성적, 학생 목록이 모두 `overflow-x-auto` 테이블 — 모바일에서 가로 스크롤 필수 |
| 기능 과잉 | 28개 도메인, 30+ 페이지가 모바일에 전부 노출 — 선생님 일상 업무의 80%는 5개 기능 |
| 입력 최적화 부재 | 성적 입력, 메모 작성이 데스크톱 폼 그대로 — 터치/스와이프 최적화 없음 |
| 오프라인 불가 | PWA 미구현. 교실 와이파이 불안정 시 사용 불가 |
| 푸시 알림 없음 | FCM/APNs 미구축. 알림톡(솔라피)만 존재 — 앱 내 실시간 알림 불가 |
| 컨텍스트 전환 비용 | 수업 중 빠른 출석 체크 → 드로어 → 강의 → 세션 → 출석 탭. 4단계 네비게이션 |

**전용 앱의 목표:**
- 선생님의 **수업 중/수업 직후** 워크플로우에 최적화
- 3초 내 핵심 작업(출석, 성적 입력) 도달
- 오프라인 기본 동작 (교실 환경)
- 네이티브 푸시 알림
- 모바일 전용 인터랙션 (스와이프, 롱프레스, 풀다운 새로고침)

### 1.2 대상 사용자

| 역할 | 주요 시나리오 | 빈도 |
|------|-------------|------|
| 강사 (teacher) | 출석 체크, 성적 입력, 학생 소통 | 매 수업 |
| 원장 (owner) | 대시보드 확인, 미처리 알림, 빠른 승인 | 수시 |
| 관리자 (admin) | 학생 등록 승인, 메시지 발송 | 수시 |
| 직원 (staff) | 출석 확인, 공지 게시 | 간헐 |

---

## 2. 현황 분석

### 2.1 현재 아키텍처 (as-is)

```
┌─────────────────────────────────────────────────────┐
│                   app_admin (React SPA)              │
│  ┌─────────────┐  ┌────────────────────────────┐    │
│  │  Desktop    │  │   Mobile (≤1024px)          │    │
│  │  Sidebar    │  │   AppLayoutMobile           │    │
│  │  + Content  │  │   Header + Content + TabBar │    │
│  └─────────────┘  └────────────────────────────┘    │
│                                                      │
│  28 domains / 30+ pages — 동일 코드, 동일 라우터     │
│  Ant Design 6 — 데스크톱 우선 컴포넌트 라이브러리    │
└──────────────────────┬──────────────────────────────┘
                       │ REST API
                       ▼
              Django + DRF Backend
              (tenant-resolved, JWT)
```

### 2.2 학생 앱 아키텍처 (참조 모델)

`app_student`는 이미 모바일 전용으로 구축되어 있다:

```
app_student/
├── app/StudentRouter.tsx       ← 전용 라우터
├── layout/                     ← 전용 레이아웃 (TopBar, TabBar, Drawer)
├── domains/ (20개)             ← 학생 관점의 기능 단위
└── shared/                     ← 학생 전용 테마, API, 컨텍스트
```

**학생 앱에서 검증된 패턴:**
- 테넌트별 CSS 테마 (`data-student-theme`)
- 5탭 하단 네비게이션
- `lazyWithRetry` 청크 분할
- 1.5초 아이들 후 프리청킹
- 부모 계정 연동 (linkedStudents)

### 2.3 기존 모바일 지원 인프라 (재사용 가능)

| 인프라 | 위치 | 재사용 |
|--------|------|--------|
| `useIsMobile` hook | `shared/hooks/useIsMobile.ts` | ✅ 공유 |
| JWT 인증 | `shared/api/` | ✅ 공유 |
| 테넌트 해석 | 도메인 기반 미들웨어 | ✅ 공유 |
| React Query 인프라 | `shared/api/queryClient` | ✅ 공유 |
| 알림 카운트 API | `admin-notifications/api.ts` | ✅ 래핑 |
| 버전 체커 | `shared/ui/layout/VersionChecker.tsx` | ✅ 공유 |
| 에러 바운더리 | `shared/ui/error/` | ✅ 공유 |

---

## 3. 설계 원칙

### 3.0 학생 자가 가입 정책과 과거 요청

- 학생 자가 가입 가능 여부는 `students.services.registration_policy`가 테넌트별로
  판정한다. `godmin`, `tchul`은 명부 기반 직접 등록만 사용하며 공개 가입 신청을
  받지 않는다.
- 정책을 끈 뒤 남은 `StudentRegistrationRequest(status=pending)`는 삭제하거나
  승인·거절하지 않고 감사 이력으로 보존한다. 직원용 `status=pending` 목록과
  승인·복구 승인·거절·일괄 처리 API는 `self_registration_disabled` 403으로
  실패 폐쇄한다. 상태 없는 이력 조회는 테넌트 범위 안에서 기존 계약을 유지한다.
  일반 `PUT`·`PATCH`·`DELETE`는 자가 가입 사용 여부와 관계없이 제공하지 않으며,
  활성 테넌트도 승인·거절·복구 전용 액션으로만 상태를 바꾼다. 목록·상세 응답의
  `tenant`·`status`·`student`를 포함한 필드는 읽기 전용이고 초기 비밀번호는 응답하지
  않는다. OpenAPI는 일반 변형 작업을 노출하지 않고, 정책 차단 가능 작업마다
  `self_registration_disabled` 403 응답 스키마를 명시한다. OpenAPI 목록 계약은
  `status=approved|pending|rejected` 쿼리 enum을 공개하며, 이 403은 자가 가입 비활성
  학원의 `status=pending` 조회에만 조건부로 발생한다고 설명한다.
- 교사 Today·알림센터의 가입 신청 건수는 위 403을 의도적인 비활성 0건으로만
  해석한다. 일반 401·403·5xx·네트워크 오류는 0건으로 합성하지 않는다.
- 직접 가입신청 화면을 열면 과거 요청이 보존된다는 정책 안내만 표시하고 처리
  버튼을 만들지 않는다. 자가 가입 사용 테넌트는 기존 목록·상세·승인·거절 흐름과
  `/workspace/mobile/comms?tab=requests` 처리 경로를 그대로 사용한다.

회귀 기준은
`apps/domains/students/tests/test_registration_approval_identity.py`와 프런트엔드
`e2e/teacher/comms-reply-mobile.mock.spec.ts`다. 두 테스트는 비활성 이력 무변경,
활성 테넌트 호환, 390px/데스크톱 처리 경로, API mutation 0을 함께 검증한다.

### 3.1 핵심 원칙

1. **수업 중심 (Class-First)**
   - 모든 UX의 중심은 "오늘의 수업". 홈 = 오늘 수업 목록.
   - 수업 카드 → 1탭으로 출석/성적/학생 접근.

2. **3초 룰**
   - 앱 열기 → 핵심 작업 시작까지 3초 이내.
   - 출석 체크: 앱 열기 → 수업 탭 → 학생 스와이프. 2스텝.

3. **모바일 네이티브 인터랙션**
   - 스와이프로 출석 상태 변경 (좌: 결석, 우: 출석).
   - 롱프레스로 학생 상세 퀵뷰.
   - 풀다운 새로고침.
   - 바텀시트 (모달 대체).

4. **오프라인 퍼스트**
   - 출석, 성적 입력은 오프라인 큐잉 → 온라인 시 동기화.
   - 학생 목록, 수업 정보는 로컬 캐시.

5. **정보 밀도 최적화**
   - 데스크톱의 테이블 → 모바일의 카드/리스트.
   - 필수 정보만 1차 노출, 상세는 드릴다운.

### 3.2 제외 범위 (데스크톱 전용 유지)

선생님 앱에 포함하지 **않는** 기능 — 데스크톱 `app_admin`에서만 사용:

| 기능 | 제외 사유 |
|------|----------|
| 설정 (Settings) | 테넌트/조직 설정은 관리 업무. 모바일 부적합 |
| 수납/정산 (Fees) | 복잡한 테이블, 엑셀 다운로드 중심. 데스크톱 전용 |
| 직원 관리 (Staff) | 급여/인사 — 모바일 부적합 |
| 자료실 (Storage) | 대용량 파일 업로드/관리 — 데스크톱 전용 |
| 개발자 도구 (Developer) | 내부 도구 |
| 가이드 (Guide) | 정적 콘텐츠. 웹 링크로 대체 |
| 시험 편집기 (Exam Editor) | 복잡한 폼 — 생성은 데스크톱, 조회/채점만 모바일 |
| 영상 업로드 (Video Upload) | 대용량 — 데스크톱 전용. 영상 목록 조회만 모바일 |
| 랜딩 에디터 | 마케팅 도구. 데스크톱 전용 |

---

## 4. 기술 아키텍처

### 4.1 PWA (Progressive Web App) 선택

**네이티브 vs PWA vs 하이브리드 비교:**

| 기준 | 네이티브 (RN/Flutter) | PWA | 하이브리드 (Capacitor) |
|------|---------------------|-----|---------------------|
| 개발 비용 | 높음 (별도 코드베이스) | **낮음 (기존 React 확장)** | 중간 |
| 배포 | 앱스토어 심사 | **즉시 배포 (현행 CI/CD)** | 앱스토어 심사 |
| 코드 공유 | 제한적 | **shared/ 완전 공유** | 부분 공유 |
| 오프라인 | 완전 | **Service Worker** | Service Worker |
| 푸시 알림 | FCM/APNs 직접 | **Web Push API** | Capacitor Push |
| 카메라/파일 | 네이티브 API | **Web API (충분)** | 네이티브 브릿지 |
| 홈화면 설치 | 앱스토어 | **A2HS (Add to Home Screen)** | 앱스토어 |
| iOS 제약 | 없음 | **푸시 지원 (iOS 16.4+)** | 없음 |
| 팀 역량 | 새 스택 학습 | **React/TS 그대로** | 약간의 학습 |

**결론: PWA를 1차로 구축하고, 필요 시 Capacitor 래핑으로 앱스토어 배포.**

이유:
- 현재 팀이 React/TypeScript 전문. 네이티브 스택 도입은 리스크.
- 기존 `shared/` 코드 (인증, API, 쿼리, 유틸) 100% 재사용.
- 배포가 현행 CI/CD 파이프라인 그대로. 앱스토어 심사 불필요.
- 학원 선생님 대상 — 앱스토어 설치보다 URL 공유가 접근성 높음.
- iOS Web Push는 16.4부터 지원 (2023.03~). 한국 iOS 점유율 고려해도 커버 충분.

### 4.2 프로젝트 구조

```
frontend/src/
├── app_admin/          ← 기존 데스크톱 관리자 앱 (변경 없음)
├── app_student/        ← 기존 학생 앱 (변경 없음)
├── app_teacher/        ← ★ 신규: 선생님 전용 모바일 앱
│   ├── app/
│   │   └── TeacherRouter.tsx
│   ├── layout/
│   │   ├── TeacherLayout.tsx
│   │   ├── TeacherTopBar.tsx
│   │   ├── TeacherTabBar.tsx
│   │   ├── TeacherDrawer.tsx
│   │   └── TeacherThemeProvider.tsx
│   ├── domains/
│   │   ├── today/               ← 홈: 오늘의 수업
│   │   ├── attendance/          ← 출석 (스와이프 UI)
│   │   ├── scores/              ← 성적 입력 (모바일 최적화)
│   │   ├── students/            ← 학생 목록/상세
│   │   ├── lectures/            ← 강의 관리
│   │   ├── exams/               ← 시험 조회/채점
│   │   ├── messages/            ← 메시지 발송
│   │   ├── community/           ← 공지/Q&A
│   │   ├── clinic/              ← 클리닉 (section_mode 시)
│   │   ├── notifications/       ← 알림 센터
│   │   └── profile/             ← 내 프로필
│   ├── shared/
│   │   ├── hooks/               ← 모바일 전용 훅
│   │   ├── ui/                  ← 모바일 전용 컴포넌트
│   │   │   ├── SwipeCard.tsx
│   │   │   ├── BottomSheet.tsx
│   │   │   ├── PullToRefresh.tsx
│   │   │   ├── FloatingAction.tsx
│   │   │   └── QuickActionBar.tsx
│   │   └── offline/             ← 오프라인 큐 관리
│   └── pwa/
│       ├── manifest.json
│       ├── sw.ts                ← Service Worker
│       └── icons/               ← PWA 아이콘 세트
├── shared/             ← 공유 인프라 (인증, API, 유틸)
└── ...
```

### 4.3 진입점 & 라우팅 분리

현재 `AppRouter.tsx`에서 역할별 라우팅:

```typescript
// 현재: /admin → app_admin (데스크톱+모바일 반응형)
// 변경 후:

// 방법 1: URL 기반 분리 (권장)
"/teacher/*"  → app_teacher (모바일 전용)
"/admin/*"    → app_admin   (데스크톱 전용으로 전환)

// 방법 2: 자동 감지
// 모바일 디바이스 + teacher/staff 역할 → app_teacher 자동 리다이렉트
// 데스크톱 → app_admin 유지
// 수동 전환 토글 제공
```

**권장: 방법 1 + 자동 감지 조합**

```typescript
// AppRouter.tsx 수정
function TeacherRouteGuard() {
  const { isMobile } = useIsMobile();
  const { user } = useAuth();
  const isTeacherRole = ["owner", "admin", "teacher", "staff"].includes(user.role);

  // 모바일 + 선생님 역할 → /teacher로 리다이렉트
  if (isMobile && isTeacherRole && location.pathname.startsWith("/admin")) {
    return <Navigate to="/teacher" replace />;
  }
  
  // 데스크톱에서 /teacher 접근 → /admin으로 리다이렉트
  if (!isMobile && location.pathname.startsWith("/teacher")) {
    return <Navigate to="/admin" replace />;
  }

  return <Outlet />;
}
```

### 4.4 빌드 & 배포

```
기존 빌드:
  vite build → dist/ (app_admin + app_student 통합 번들)

변경 후:
  vite build → dist/
    ├── index.html          ← SPA 엔트리 (기존)
    ├── assets/
    │   ├── admin-*.js      ← app_admin 청크
    │   ├── student-*.js    ← app_student 청크
    │   ├── teacher-*.js    ← app_teacher 청크 ★
    │   └── shared-*.js     ← 공유 청크
    ├── manifest.json       ← PWA 매니페스트 ★
    └── sw.js               ← Service Worker ★
```

코드 스플리팅으로 `app_teacher`는 독립 청크. 학생/관리자 앱 로딩에 영향 없음.

---

## 5. 앱 구조

### 5.1 화면 계층 (Screen Hierarchy)

```
TeacherApp
├── 🏠 오늘 (Today)                    ← 기본 탭. 오늘의 수업 + 알림
│   ├── 수업 카드 (SessionCard)         ← 탭하면 세션 상세로
│   ├── 미처리 알림 배너               ← Q&A, 등록 요청, 채점 대기
│   └── 빠른 통계 (KPI 카드)
│
├── 📋 수업 (Classes)                   ← 강의/세션 관리
│   ├── 강의 목록 (LectureList)
│   │   └── 강의 상세 (LectureDetail)
│   │       ├── 세션 목록
│   │       ├── 수강생 목록
│   │       └── 반 관리 (section_mode)
│   └── 세션 상세 (SessionDetail)       ← 핵심 작업 허브
│       ├── 출석 (Attendance)           ← 스와이프 UI
│       ├── 성적 (Scores)              ← 모바일 입력
│       ├── 시험 (Exams)               ← 채점/결과
│       ├── 과제 (Homework)            ← 제출 확인
│       └── 영상 (Videos)              ← 목록 조회
│
├── 👨‍🎓 학생 (Students)                ← 학생 관리
│   ├── 학생 목록 (StudentList)         ← 검색, 필터
│   │   └── 학생 상세 (StudentDetail)
│   │       ├── 프로필
│   │       ├── 수강 이력
│   │       ├── 성적 추이
│   │       ├── 출석 기록
│   │       └── 메시지 발송
│   └── 등록 요청 (RegistrationRequests)
│
├── 💬 소통 (Communication)             ← 알림/게시판 통합
│   ├── 알림 센터 (NotificationCenter)
│   ├── 공지사항 (Notices)
│   ├── Q&A (질의응답)
│   ├── 상담 (Counseling)
│   └── 메시지 발송 (QuickMessage)
│
└── ☰ 더보기 (More)                    ← 부가 기능
    ├── 클리닉 (section_mode 시)
    ├── 시험 목록
    ├── 영상 목록
    ├── 출석부 (전체 매트릭스)
    ├── 내 프로필
    ├── 데스크톱 버전으로 전환
    └── 로그아웃
```

### 5.2 핵심 화면 와이어프레임

#### 오늘 (Today) — 홈 화면

```
┌──────────────────────────────┐
│ 📍 학원플러스 수학   🔔(3)  │  ← TopBar: 테넌트명 + 알림 뱃지
├──────────────────────────────┤
│                              │
│ ⚠️ 미처리 3건              │  ← 알림 배너 (탭하면 알림센터)
│  Q&A 2건 · 등록요청 1건     │
│                              │
│ ── 오늘의 수업 ──────────── │
│                              │
│ ┌──────────────────────────┐ │
│ │ 📘 중3 수학 A반          │ │  ← 수업 카드
│ │ 14:00-16:00 · 301호      │ │
│ │ 학생 24명                 │ │
│ │ ┌────┐ ┌────┐ ┌────┐    │ │
│ │ │출석 │ │성적│ │시험 │    │ │  ← 퀵 액션 버튼
│ │ └────┘ └────┘ └────┘    │ │
│ └──────────────────────────┘ │
│                              │
│ ┌──────────────────────────┐ │
│ │ 📗 고1 수학 기본          │ │
│ │ 16:30-18:30 · 302호      │ │
│ │ 학생 18명                 │ │
│ │ ┌────┐ ┌────┐ ┌────┐    │ │
│ │ │출석 │ │성적│ │시험 │    │ │
│ │ └────┘ └────┘ └────┘    │ │
│ └──────────────────────────┘ │
│                              │
│ ── 이번 주 요약 ─────────── │
│  출석률 94% · 미채점 5건    │
│                              │
├──────────────────────────────┤
│ 🏠오늘  📋수업  👨‍🎓학생  💬소통  ☰ │  ← BottomTabBar
└──────────────────────────────┘
```

#### 출석 체크 — 스와이프 UI

```
┌──────────────────────────────┐
│ ← 중3 수학 A반  출석        │  ← 뒤로가기 + 제목
│   2026-04-15 (화) 14:00     │
├──────────────────────────────┤
│ 전체출석  │  출석 20  결석 2  미처리 2 │ ← 상태 요약 + 전체출석 버튼
├──────────────────────────────┤
│                              │
│ ┌──────────────────────────┐ │
│ │ 😊 김민수                │ │  ← 학생 카드
│ │     ←  스와이프 →        │ │
│ │  [출석 ✅]               │ │  ← 현재 상태
│ └──────────────────────────┘ │
│                              │
│ ┌──────────────────────────┐ │
│ │ 😊 이서연                │ │
│ │     ←  스와이프 →        │ │
│ │  [미처리 ⬜]             │ │  ← 스와이프하면 상태 변경
│ └──────────────────────────┘ │
│                              │
│ ┌──────────────────────────┐ │
│ │ 😊 박지호                │ │
│ │     ←  스와이프 →        │ │
│ │  [결석 ❌]               │ │
│ └──────────────────────────┘ │
│                              │
│ ...                          │
│                              │
│ ┌──────────────────────────┐ │
│ │  💾 저장 완료 · 20/24    │ │  ← 플로팅 상태 바
│ └──────────────────────────┘ │
└──────────────────────────────┘

스와이프 동작:
  → 우측: 출석(PRESENT)        녹색 배경
  ← 좌측: 결석(ABSENT)        적색 배경
  탭:    상태 선택 바텀시트    (6개 전체 상태)
```

#### 성적 입력 — 모바일 최적화

```
┌──────────────────────────────┐
│ ← 중3 수학 A반  성적입력    │
│   제4회 모의고사             │
├──────────────────────────────┤
│ 만점: 100  │ 입력 18/24     │  ← 진행률
├──────────────────────────────┤
│                              │
│ ┌──────────────────────────┐ │
│ │ 김민수                    │ │
│ │ ┌────────────────┐       │ │
│ │ │     85         │  /100 │ │  ← 큰 숫자 입력 필드
│ │ └────────────────┘       │ │
│ │ 메모: _____________      │ │  ← 선택적 메모
│ └──────────────────────────┘ │
│                              │
│ ┌──────────────────────────┐ │
│ │ 이서연                    │ │
│ │ ┌────────────────┐       │ │
│ │ │     92         │  /100 │ │
│ │ └────────────────┘       │ │
│ └──────────────────────────┘ │
│                              │
│  ↕ 스크롤하며 연속 입력     │
│                              │
├──────────────────────────────┤
│ ┌──────────────────────────┐ │
│ │  [임시저장]    [제출]     │ │  ← 하단 고정 액션
│ └──────────────────────────┘ │
└──────────────────────────────┘

입력 UX:
  - 숫자 입력 시 num 키패드 자동 활성 (inputMode="numeric")
  - Enter/Next로 다음 학생 자동 포커스
  - 임시저장은 로컬 + 서버 동기화
  - 오프라인 시 로컬 큐잉, 연결 복구 시 동기화
```

#### 학생 상세 — 카드 기반 정보

```
┌──────────────────────────────┐
│ ← 학생 상세                 │
├──────────────────────────────┤
│                              │
│      😊                      │
│    김민수                    │
│    중3 · 남 · 한빛중         │
│    📱 010-1234-5678         │  ← 탭하면 전화
│    👨‍👩‍👧 010-9876-5432 (학부모) │  ← 탭하면 전화
│                              │
│ ┌─────┐ ┌─────┐ ┌─────┐    │
│ │ 전화 │ │문자 │ │알림톡│    │  ← 퀵 액션
│ └─────┘ └─────┘ └─────┘    │
│                              │
│ ── 수강 현황 ─────────────── │
│ 📘 중3 수학 A반 · 수강 중   │
│ 📗 중3 영어 · 수강 중       │
│                              │
│ ── 최근 출석 ─────────────── │
│ 04/15 출석 · 04/14 출석 ·   │
│ 04/12 결석 · 04/11 출석     │
│                              │
│ ── 성적 추이 ─────────────── │
│ [미니 차트: 최근 5회 점수]   │
│  78 → 82 → 85 → 81 → 92   │
│                              │
│ ── 메모 ──────────────────── │
│ "수학 기초 보강 필요"        │
│                              │
└──────────────────────────────┘
```

---

## 6. 핵심 기능 설계

### 6.1 출석 체크 (Attendance)

**현재 문제:**
- 테이블 기반 (`SessionAttendancePage.tsx`, 775줄)
- 상태 변경이 popover 클릭 → 모바일에서 오조작 빈번
- `getBoundingClientRect()` 기반 위치 계산 → 작은 화면에서 오버플로우 가능

**모바일 전용 설계:**

```typescript
// app_teacher/domains/attendance/SwipeAttendanceList.tsx

interface SwipeAttendanceProps {
  sessionId: number;
  students: AttendanceStudent[];
}

// 스와이프 상태 매핑
const SWIPE_ACTIONS = {
  right: { status: 'PRESENT', color: 'green',  icon: '✅', label: '출석' },
  left:  { status: 'ABSENT',  color: 'red',    icon: '❌', label: '결석' },
  tap:   'BOTTOM_SHEET',  // 전체 6개 상태 선택
} as const;

// 즉시 저장 (낙관적 업데이트 + 오프라인 큐)
const handleSwipe = (studentId: number, direction: 'left' | 'right') => {
  const status = SWIPE_ACTIONS[direction].status;
  
  // 1. 즉시 UI 업데이트 (낙관적)
  queryClient.setQueryData(['attendance', sessionId], (old) => 
    old.map(a => a.student_id === studentId ? { ...a, status } : a)
  );
  
  // 2. API 호출 (또는 오프라인 큐)
  offlineQueue.enqueue({
    type: 'ATTENDANCE_UPDATE',
    payload: { attendanceId, status },
    api: () => patchAttendance(attendanceId, { status }),
  });
};
```

**출석 전체 흐름:**

```
오늘 홈 → 수업 카드 "출석" 탭
  → 학생 리스트 (카드형, 프로필 사진 + 이름)
  → 스와이프 또는 탭으로 상태 변경
  → 실시간 요약 바 업데이트 (출석 20/결석 2/미처리 2)
  → "전체 출석" 버튼 (1탭으로 미처리 전원 출석 처리)
  → 완료 시 자동 저장 + 알림톡 발송 (설정에 따라)
```

**Section Mode 지원:**
```
section_mode = true 일 때:
  세션 → 반 선택 (A반/B반 탭) → 해당 반 학생만 표시
  SectionAssignment 기반 필터링
```

### 6.2 성적 입력 (Score Entry)

**현재 문제:**
- 데스크톱 테이블 그리드에 직접 입력
- 모바일에서 셀 크기가 작아 오입력 빈번
- 드래프트 시스템이 복잡한 폼 상태 관리

**모바일 전용 설계:**

```typescript
// app_teacher/domains/scores/MobileScoreEntry.tsx

// 연속 입력 모드: 학생별 카드를 세로 스크롤
// 각 카드에 큰 숫자 입력 필드 + 자동 다음 포커스

const ScoreEntryCard = ({ student, maxScore, onSubmit }) => {
  const inputRef = useRef<HTMLInputElement>(null);
  
  return (
    <div className="score-card">
      <StudentChip student={student} />
      <input
        ref={inputRef}
        type="text"
        inputMode="numeric"      // 숫자 키패드 강제
        pattern="[0-9]*"
        className="score-input"   // 48px 높이, 큰 폰트
        onKeyDown={(e) => {
          if (e.key === 'Enter') focusNextStudent();
        }}
      />
      <span className="max-score">/ {maxScore}</span>
    </div>
  );
};
```

**성적 입력 워크플로우:**
1. 세션 상세 → "성적" 탭
2. 시험/과제 선택 (바텀시트)
3. 학생 카드 리스트 → 숫자 입력
4. Enter로 다음 학생 자동 포커스
5. 임시저장 (1분 간격 자동 + 수동)
6. "제출" → 확인 → 서버 동기화

### 6.3 학생 관리 (Students)

**모바일 최적화:**

```
현재 (데스크톱):
  테이블: 이름 | 학년 | 학교 | 전화 | 학부모전화 | 태그 | 상태
  → 모바일에서 가로 스크롤 필수, 읽기 어려움

모바일 전용:
  카드 리스트:
  ┌─────────────────────────┐
  │ 😊 김민수  중3 · 한빛중  │
  │ 📘 중3수학A  📗 중3영어  │  ← 수강 중 강의 칩
  │ 📱 010-1234-5678        │
  └─────────────────────────┘
  
  - 검색: 이름/전화번호 통합 검색
  - 필터: 강의별, 학년별 (바텀시트)
  - 학생 탭 → 상세 페이지 (전화/문자 바로 연결)
  - 롱프레스 → 퀵 액션 (전화, 문자, 알림톡)
```

### 6.4 메시지 발송 (Quick Message)

**현재:** 복잡한 모달 (`MessagingModal`) with SMS/알림톡 분리, 변수 시스템.

**모바일 전용:**

```
1. 수신자 선택
   - 출석 화면에서 학생 선택 → "메시지" 버튼
   - 학생 상세에서 "문자" 버튼
   - 커뮤니티에서 전체 공지

2. 메시지 작성 (바텀시트)
   ┌──────────────────────────┐
   │ 수신: 김민수 외 3명      │
   │                          │
   │ 발송 유형:               │
   │ [SMS] [알림톡]           │
   │                          │
   │ 템플릿:                  │
   │ [수업 안내] [결석 알림]  │
   │ [시험 결과] [직접 입력]  │
   │                          │
   │ ┌──────────────────────┐ │
   │ │ 안녕하세요, {학생명}  │ │
   │ │ 학부모님.             │ │
   │ │ ...                   │ │
   │ └──────────────────────┘ │
   │                          │
   │     [미리보기]  [발송]   │
   └──────────────────────────┘

3. 발송 확인 → 크레딧 차감 안내 → 발송
```

### 6.5 대시보드 / 오늘 (Today)

**데스크톱 대시보드와의 차이:**

| 데스크톱 (`DashboardPage`) | 모바일 (Today) |
|---------------------------|----------------|
| 바로가기 위젯 (7개) | 오늘 수업 카드 (시간순) |
| 미처리 일감 위젯 | 알림 배너 (요약) |
| 메시징 위젯 | 제거 (소통 탭으로 이동) |
| KPI 카드 | 이번 주 요약 (출석률, 미채점) |

**데이터 로딩 전략:**

```typescript
// app_teacher/domains/today/api.ts

// 전용 API 엔드포인트 (BFF 패턴)
// 한 번의 호출로 오늘 화면에 필요한 모든 데이터 로드
export const fetchTodayOverview = () =>
  api.get<TodayOverview>('/api/v1/teacher-app/today/');

interface TodayOverview {
  today_sessions: TodaySession[];       // 오늘 수업 목록
  notification_counts: NotifCounts;     // 미처리 알림 수
  week_summary: WeekSummary;            // 이번 주 요약
  recent_activity: Activity[];          // 최근 활동
}
```

### 6.6 클리닉 (section_mode 전용)

section_mode 활성 테넌트에서만 노출:

```
더보기 → 클리닉
  ├── 오늘 클리닉 세션 목록
  ├── 예약 현황 (학생별)
  ├── 패스카드 스캔 (카메라)
  └── 클리닉 출석 (스와이프)
```

---

## 7. 네비게이션 & UX 설계

### 7.1 하단 탭 바 (Bottom Tab Bar)

```typescript
// app_teacher/layout/TeacherTabBar.tsx

const TABS = [
  { key: 'today',    label: '오늘',   icon: CalendarIcon,    path: '/teacher' },
  { key: 'classes',  label: '수업',   icon: BookIcon,        path: '/teacher/classes' },
  { key: 'students', label: '학생',   icon: UsersIcon,       path: '/teacher/students' },
  { key: 'comms',    label: '소통',   icon: MessageIcon,     path: '/teacher/comms',
    badge: notificationCount },
  { key: 'more',     label: '더보기', icon: MenuIcon,        action: 'drawer' },
] as const;
```

### 7.2 인터랙션 패턴

| 패턴 | 용도 | 구현 |
|------|------|------|
| **스와이프** | 출석 상태 변경, 리스트 아이템 삭제 | `react-swipeable` 또는 자체 터치 핸들러 |
| **롱프레스** | 학생 퀵 액션, 컨텍스트 메뉴 | 500ms 프레스 → 햅틱 피드백 + 바텀시트 |
| **풀다운 새로고침** | 모든 리스트 페이지 | `PullToRefresh` 컴포넌트 |
| **바텀시트** | 필터, 상태 선택, 메시지 작성 | Ant Design `Drawer` placement="bottom" 또는 자체 구현 |
| **플로팅 액션** | 학생 추가, 메시지 발송 | `FloatingActionButton` 우하단 고정 |
| **무한 스크롤** | 학생 목록, 게시판 | React Query `useInfiniteQuery` |
| **스켈레톤** | 모든 데이터 로딩 상태 | 카드형 스켈레톤 UI |

### 7.3 제스처 네비게이션

```
화면 좌측 가장자리 → 우측 스와이프 = 뒤로가기
  (iOS Safari/Chrome 네이티브 제스처와 충돌 방지: 가장자리 20px만 감지)

수업 카드 내 좌우 스와이프 = 날짜 변경 (어제/오늘/내일)

출석 카드 스와이프:
  - 임계값 40% 초과 시 액션 확정
  - 임계값 미만 시 스냅백
  - 스와이프 중 배경색 그라데이션 피드백
```

### 7.4 레이아웃 구조

```typescript
// app_teacher/layout/TeacherLayout.tsx

export function TeacherLayout() {
  return (
    <div className="teacher-app" style={{ height: '100dvh', display: 'flex', flexDirection: 'column' }}>
      {/* 상단 바 */}
      <TeacherTopBar />
      
      {/* 메인 콘텐츠 - 스크롤 영역 */}
      <main style={{ 
        flex: 1, 
        overflowY: 'auto',
        WebkitOverflowScrolling: 'touch',
        paddingBottom: 'calc(56px + env(safe-area-inset-bottom))',
      }}>
        <Suspense fallback={<TeacherRouteFallback />}>
          <Outlet />
        </Suspense>
      </main>
      
      {/* 하단 탭 바 */}
      <TeacherTabBar />
      
      {/* 드로어 (더보기) */}
      <TeacherDrawer />
      
      {/* 오프라인 상태 표시 */}
      <OfflineIndicator />
    </div>
  );
}
```

---

## 8. API 전략

### 8.1 BFF (Backend For Frontend) 엔드포인트

모바일 앱은 네트워크 효율이 중요하다. 여러 API를 조합하는 대신 **모바일 전용 집계 엔드포인트**를 백엔드에 추가한다.

```python
# backend/apps/domains/teacher_app/urls.py (신규)

urlpatterns = [
    path('today/',         TodayOverviewView.as_view()),
    path('sessions/<id>/summary/', SessionSummaryView.as_view()),
    path('students/quick-list/',   StudentQuickListView.as_view()),
    path('attendance/batch/',      BatchAttendanceView.as_view()),
    path('scores/batch/',          BatchScoreView.as_view()),
    path('notifications/summary/', NotificationSummaryView.as_view()),
]
```

**예시: TodayOverviewView**

```python
# backend/apps/domains/teacher_app/views.py

class TodayOverviewView(APIView):
    """모바일 홈 화면용 집계 API. 한 번의 호출로 필요한 데이터 전부 반환."""
    permission_classes = [TenantResolvedAndStaff]

    def get(self, request):
        tenant = request.tenant
        today = date.today()
        teacher = request.user.teacher_profile  # nullable for owner/admin

        # 오늘 세션 (해당 선생님 담당 또는 전체)
        sessions_qs = Session.objects.filter(
            lecture__tenant=tenant,
            date=today,
            lecture__is_active=True,
        ).select_related('lecture', 'section')
        
        if teacher:
            sessions_qs = sessions_qs.filter(lecture__teacher=teacher)

        # 알림 카운트
        notif_counts = get_notification_counts(tenant)

        # 이번 주 요약
        week_start = today - timedelta(days=today.weekday())
        week_attendance = Attendance.objects.filter(
            session__lecture__tenant=tenant,
            session__date__gte=week_start,
            session__date__lte=today,
        )
        
        return Response({
            'today_sessions': TodaySessionSerializer(sessions_qs, many=True).data,
            'notification_counts': notif_counts,
            'week_summary': {
                'attendance_rate': calculate_rate(week_attendance),
                'ungraded_count': get_ungraded_count(tenant, teacher),
            },
        })
```

### 8.2 기존 API 재사용

BFF 없이 기존 API를 그대로 쓰는 경우:

| 기능 | 기존 API | 그대로 사용 |
|------|---------|------------|
| 출석 CRUD | `PATCH /api/v1/lectures/attendance/{id}/` | ✅ |
| 전체 출석 | `POST /api/v1/lectures/attendance/bulk_set_present/` | ✅ |
| 학생 목록 | `GET /api/v1/students/` | ✅ (페이지네이션 조정) |
| 학생 상세 | `GET /api/v1/students/{id}/` | ✅ |
| 강의 목록 | `GET /api/v1/lectures/` | ✅ |
| 메시지 발송 | `POST /api/v1/messaging/send/` | ✅ |
| 공지/Q&A | `GET /api/v1/community/posts/` | ✅ |
| 시험 결과 | `GET /api/v1/results/` | ✅ |

### 8.3 API 응답 최적화

```python
# 모바일 전용 시리얼라이저: 필드 최소화

class TodaySessionSerializer(serializers.ModelSerializer):
    """모바일 홈용 세션 요약. 최소 필드만."""
    lecture_title = serializers.CharField(source='lecture.title')
    lecture_color = serializers.CharField(source='lecture.color')
    student_count = serializers.IntegerField()
    attendance_summary = serializers.SerializerMethodField()
    section_label = serializers.CharField(source='section.label', default=None)
    location = serializers.CharField(source='section.location', default=None)
    start_time = serializers.TimeField(source='section.start_time', default=None)
    end_time = serializers.TimeField(source='section.end_time', default=None)

    class Meta:
        model = Session
        fields = ['id', 'lecture_id', 'lecture_title', 'lecture_color',
                  'student_count', 'attendance_summary', 'section_label',
                  'location', 'start_time', 'end_time', 'date']
```

---

## 9. 오프라인 & 성능

### 9.1 Service Worker 전략

```typescript
// app_teacher/pwa/sw.ts

// 캐싱 전략
const CACHE_STRATEGIES = {
  // 1. 앱 쉘 (HTML, CSS, JS) — Cache First
  appShell: {
    strategy: 'CacheFirst',
    cacheName: 'teacher-app-shell-v1',
    match: /\.(html|css|js|woff2)$/,
    maxAge: 7 * 24 * 60 * 60, // 7일
  },
  
  // 2. API 데이터 — Network First (오프라인 폴백)
  apiData: {
    strategy: 'NetworkFirst',
    cacheName: 'teacher-api-cache',
    match: /\/api\/v1\//,
    networkTimeout: 3000, // 3초 타임아웃 후 캐시 사용
    maxAge: 24 * 60 * 60,  // 24시간
  },
  
  // 3. 프로필 이미지 — Stale While Revalidate
  images: {
    strategy: 'StaleWhileRevalidate',
    cacheName: 'teacher-images',
    match: /\.(png|jpg|jpeg|svg)$/,
    maxEntries: 200,
  },
};
```

### 9.2 오프라인 큐 (Offline Queue)

```typescript
// app_teacher/shared/offline/OfflineQueue.ts

interface QueueItem {
  id: string;
  type: 'ATTENDANCE_UPDATE' | 'SCORE_SAVE' | 'MESSAGE_DRAFT';
  payload: unknown;
  createdAt: number;
  retryCount: number;
  api: () => Promise<unknown>;
}

class OfflineQueue {
  private db: IDBDatabase;  // IndexedDB for persistence
  
  async enqueue(item: Omit<QueueItem, 'id' | 'createdAt' | 'retryCount'>) {
    const queueItem: QueueItem = {
      ...item,
      id: crypto.randomUUID(),
      createdAt: Date.now(),
      retryCount: 0,
    };
    
    // IndexedDB에 저장
    await this.db.put('offlineQueue', queueItem);
    
    // 온라인이면 즉시 실행
    if (navigator.onLine) {
      await this.processQueue();
    }
  }
  
  async processQueue() {
    const items = await this.db.getAll('offlineQueue');
    
    for (const item of items.sort((a, b) => a.createdAt - b.createdAt)) {
      try {
        await item.api();
        await this.db.delete('offlineQueue', item.id);
      } catch (error) {
        if (item.retryCount >= 3) {
          // 3회 실패 → 사용자에게 알림
          showOfflineError(item);
          await this.db.delete('offlineQueue', item.id);
        } else {
          item.retryCount++;
          await this.db.put('offlineQueue', item);
        }
      }
    }
  }
  
  // 온라인 복귀 시 자동 동기화
  constructor() {
    window.addEventListener('online', () => this.processQueue());
  }
}
```

### 9.3 성능 목표

| 지표 | 목표 | 전략 |
|------|------|------|
| FCP (First Contentful Paint) | < 1.5s | 앱 쉘 캐싱, 코드 스플리팅 |
| LCP (Largest Contentful Paint) | < 2.5s | 크리티컬 CSS 인라인, 이미지 lazy |
| TTI (Time to Interactive) | < 3.0s | 청크 분할, 디퍼드 로딩 |
| 번들 사이즈 (초기) | < 150KB gzip | tree shaking, Ant Design 제거 |
| API 응답 (Today) | < 500ms | BFF 집계, select_related |
| 오프라인 전환 | 즉시 | Service Worker + IndexedDB |

### 9.4 UI 라이브러리 선택

```
현재 app_admin: Ant Design 6 (antd)
  - 장점: 풍부한 컴포넌트
  - 단점: 번들 사이즈 대형, 데스크톱 우선, 터치 최적화 부족

선생님 앱: Ant Design 미사용
  - Tailwind CSS 4 (기존 인프라) + 자체 모바일 컴포넌트
  - 이유:
    1. antd 번들 제거 → 초기 로딩 50%+ 감소
    2. 터치 최적화 컴포넌트 자체 구현 (스와이프, 바텀시트)
    3. 학생 앱도 antd 미사용 → 검증된 접근
    4. Tailwind만으로 충분한 UI 구성 가능
```

---

## 10. 푸시 알림

### 10.1 Web Push 아키텍처

```
현재:
  선생님 → 솔라피 알림톡 → 학부모 전화

추가:
  시스템 이벤트 → Web Push API → 선생님 브라우저/PWA
  
  이벤트 예시:
  - 학생 등록 요청 접수
  - Q&A 새 질문 등록
  - 시험 제출 완료
  - 클리닉 예약 접수
  - 영상 인코딩 완료
```

### 10.2 백엔드 구현

```python
# backend/apps/domains/teacher_app/push/models.py (신규)

class PushSubscription(TenantModel):
    """Web Push 구독 정보"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=500)
    p256dh_key = models.CharField(max_length=200)
    auth_key = models.CharField(max_length=200)
    user_agent = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'endpoint')


class PushNotificationConfig(TenantModel):
    """선생님별 알림 설정"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    student_registration = models.BooleanField(default=True)
    qna_new_question = models.BooleanField(default=True)
    exam_submission = models.BooleanField(default=True)
    clinic_booking = models.BooleanField(default=False)
    video_encoding_complete = models.BooleanField(default=True)
```

```python
# backend/apps/domains/teacher_app/push/service.py

from pywebpush import webpush, WebPushException

def send_push_to_teacher(user_id: int, tenant_id: int, payload: dict):
    """선생님에게 Web Push 발송"""
    subscriptions = PushSubscription.objects.filter(
        user_id=user_id,
        tenant_id=tenant_id,
        is_active=True,
    )
    
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {
                        'p256dh': sub.p256dh_key,
                        'auth': sub.auth_key,
                    },
                },
                data=json.dumps(payload),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': f'mailto:{settings.VAPID_CONTACT_EMAIL}'},
            )
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                sub.is_active = False
                sub.save(update_fields=['is_active'])
```

### 10.3 프론트엔드 구현

```typescript
// app_teacher/pwa/pushManager.ts

export async function subscribeToPush(): Promise<PushSubscription | null> {
  if (!('PushManager' in window)) return null;
  
  const registration = await navigator.serviceWorker.ready;
  
  // VAPID 공개키로 구독
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
  });
  
  // 서버에 구독 정보 전송
  await api.post('/api/v1/teacher-app/push/subscribe/', {
    endpoint: subscription.endpoint,
    p256dh_key: btoa(String.fromCharCode(...new Uint8Array(subscription.getKey('p256dh')!))),
    auth_key: btoa(String.fromCharCode(...new Uint8Array(subscription.getKey('auth')!))),
    user_agent: navigator.userAgent,
  });
  
  return subscription;
}
```

---

## 11. 테넌트 & 보안

### 11.1 테넌트 격리 (변경 없음)

선생님 앱은 기존 테넌트 격리 메커니즘을 **100% 그대로 사용**한다.

```
요청 흐름:
  1. 도메인 → TenantMiddleware → request.tenant 설정
  2. JWT 토큰 → 사용자 인증
  3. TenantResolvedAndStaff 퍼미션 → 역할 검증
  4. TenantQuerySet → DB 쿼리 자동 필터링

변경 사항: 없음. 새 API도 동일 미들웨어/퍼미션 사용.
```

### 11.2 인증 흐름

```
1. PWA 열기 → JWT 확인
2. 유효 → 앱 진입
3. 만료 → refresh token으로 갱신
4. refresh도 만료 → 로그인 화면
5. 로그인 → 기존 AuthAPI 사용 (동일)
6. 오프라인 → 마지막 유효 토큰으로 캐시 데이터 접근
           → API 호출은 큐잉, 온라인 복귀 시 토큰 갱신 후 실행
```

### 11.3 역할별 기능 접근

```typescript
// app_teacher/shared/hooks/useTeacherPermissions.ts

interface TeacherPermissions {
  canMarkAttendance: boolean;      // teacher, admin, owner
  canEditScores: boolean;          // teacher, admin, owner
  canSendMessages: boolean;        // teacher, admin, owner (크레딧 필요)
  canApproveRegistration: boolean; // admin, owner
  canViewAllLectures: boolean;     // admin, owner (teacher는 본인 강의만)
  canManageClinic: boolean;        // admin, owner + section_mode
  canAccessFees: boolean;          // false (데스크톱 전용)
}
```

### 11.4 데이터 보호

```
- 학생 전화번호: 마스킹 표시 (010-****-5678), 탭 시 전화 연결은 가능
- 학부모 전화번호: 동일 마스킹 정책
- 성적 데이터: 로컬 캐시 시 IndexedDB 암호화 미적용 (PWA 한계)
             → 앱 잠금 화면 미구현 시 리스크 존재
             → Phase 2에서 앱 잠금 (PIN/생체인증) 검토
- 오프라인 큐: 민감 데이터 포함 가능 → 동기화 완료 후 즉시 삭제
```

---

## 12. 구현 로드맵

### Phase 1: 핵심 (MVP) — 4~5주

**목표: 선생님이 수업 중 출석/성적을 모바일로 처리할 수 있다.**

| 주차 | 작업 | 산출물 |
|------|------|--------|
| W1 | 프로젝트 구조 셋업 | `app_teacher/` 디렉토리, 라우터, 레이아웃, 테마 |
| W1 | PWA 기반 구축 | manifest.json, Service Worker (앱 쉘 캐싱) |
| W2 | 오늘 홈 화면 | BFF API (`/teacher-app/today/`), 수업 카드 UI |
| W2 | 하단 탭 바 + 드로어 | 네비게이션 완성 |
| W3 | 출석 체크 (스와이프) | `SwipeAttendanceList`, 오프라인 큐 |
| W3 | 출석 → 기존 API 연동 | PATCH attendance, bulk_set_present |
| W4 | 성적 입력 (모바일) | `MobileScoreEntry`, 임시저장, 제출 |
| W4 | 학생 목록/상세 | 카드 리스트, 검색, 전화 연결 |
| W5 | 통합 테스트 + E2E | Playwright 모바일 뷰포트, 실기기 테스트 |
| W5 | 데스크톱 ↔ 모바일 전환 | 자동 리다이렉트 + 수동 토글 |

**Phase 1 완료 기준:**
- 선생님이 모바일로 오늘 수업 확인 → 출석 체크 → 성적 입력 가능
- 오프라인에서 출석/성적 입력 → 온라인 복귀 시 동기화
- section_mode 테넌트에서 반별 출석 정상 동작
- PWA 홈화면 설치 가능

### Phase 2: 소통 & 알림 — 3주

| 주차 | 작업 | 산출물 |
|------|------|--------|
| W6 | 알림 센터 | 미처리 목록, 탭별 필터 |
| W6 | Web Push 구현 | 백엔드 구독 모델, VAPID 설정, 프론트 구독 |
| W7 | 메시지 발송 | SMS/알림톡 바텀시트, 템플릿 선택 |
| W7 | 공지/Q&A | 커뮤니티 게시판 (조회 + 답변) |
| W8 | 학생 등록 요청 처리 | 승인/거절 + 푸시 알림 |

**Phase 2 완료 기준:**
- 학생 Q&A 등록 → 선생님 푸시 알림 → 모바일에서 답변
- 출석 후 학부모 알림톡은 선생님의 대상 확인 → 미리보기 → 확정으로만 발송
  (일반 출결 저장에 따른 자동 발송 없음)
- 등록 요청 푸시 → 모바일 승인

### Phase 3: 고도화 — 3주

| 주차 | 작업 | 산출물 |
|------|------|--------|
| W9 | 시험/과제 조회 | 시험 목록, 제출 현황, 간이 채점 |
| W9 | 영상 목록 | 수업별 영상 목록 조회, 인코딩 상태 |
| W10 | 클리닉 (section_mode) | 클리닉 출석, 예약 현황 |
| W10 | 상담 메모 | 학생별 상담 기록 CRUD |
| W11 | 성능 최적화 | 번들 분석, 청크 최적화, 이미지 최적화 |
| W11 | 앱 잠금 (선택) | PIN/생체인증 잠금 화면 |

### Phase 4: 확장 (선택) — 2주

| 작업 | 설명 |
|------|------|
| Capacitor 래핑 | 앱스토어 배포 (필요 시) |
| 위젯 | iOS/Android 위젯 (오늘 수업 요약) |
| 딥링크 | 알림톡에서 앱 직접 열기 |
| 다크모드 | 기존 테마 시스템 확장 |
| 통계 대시보드 | 원장용 모바일 리포트 |

---

## 13. 기존 시스템과의 관계

### 13.1 공유 vs 전용

```
공유 (shared/):                          전용 (app_teacher/):
├── api/                                 ├── layout/ (전용 레이아웃)
│   ├── axiosInstance.ts                 ├── domains/ (전용 페이지)
│   ├── queryClient.ts                   ├── shared/
│   └── errorHandlers.ts                 │   ├── ui/ (스와이프, 바텀시트)
├── hooks/                               │   ├── hooks/ (모바일 전용)
│   ├── useIsMobile.ts                   │   └── offline/ (큐, SW)
│   ├── useAuth.ts                       └── pwa/ (매니페스트, SW)
│   └── useTenant.ts
├── ui/
│   ├── StudentChip.tsx
│   ├── LectureChip.tsx
│   └── StatusBadge.tsx
├── utils/
│   ├── formatPhone.ts
│   └── formatDate.ts
└── types/
    ├── attendance.ts
    ├── student.ts
    └── lecture.ts
```

### 13.2 app_admin과의 관계

```
app_admin (데스크톱 관리자):
  - 전체 28개 도메인, 모든 기능
  - 모바일 레이아웃 유지 (기존 사용자 호환)
  - 향후: 모바일 접근 시 app_teacher 리다이렉트 (Phase 1 이후)
  - 장기: app_admin의 모바일 레이아웃 제거 → 데스크톱 전용화

app_teacher (모바일 선생님):
  - 10개 도메인, 수업 중심 기능만
  - app_admin의 API를 공유하되, BFF 추가
  - 자체 레이아웃, 자체 컴포넌트
  - app_admin의 코드를 import하지 않음 (독립성)
```

### 13.3 백엔드 변경 범위

```
기존 변경 없음:
  - apps/domains/attendance/    (기존 API 재사용)
  - apps/domains/lectures/      (기존 API 재사용)
  - apps/domains/students/      (기존 API 재사용)
  - apps/domains/community/     (기존 API 재사용)
  - apps/domains/messaging/     (기존 API 재사용)
  
신규 추가:
  - apps/domains/teacher_app/   ★ 신규 Django 앱
    ├── views.py               (BFF 집계 뷰)
    ├── serializers.py         (모바일 전용 시리얼라이저)
    ├── push/                  (Web Push 구독/발송)
    │   ├── models.py
    │   ├── views.py
    │   └── service.py
    └── urls.py
```

---

## 14. 리스크 & 제약

### 14.1 기술 리스크

| 리스크 | 영향 | 완화 |
|--------|------|------|
| iOS PWA 제약 | iOS Safari에서 Web Push 지원이 16.4+. 구형 iOS 미지원 | 최소 iOS 16.4 요구. 미지원 시 인앱 알림만 제공 |
| 오프라인 동기화 충돌 | 동일 출석을 모바일/데스크톱에서 동시 수정 | Last-write-wins + 충돌 시 사용자 알림 |
| 스와이프 오조작 | 의도치 않은 출석 상태 변경 | Undo 스낵바 (3초), 임계값 40% 이상만 확정 |
| Service Worker 캐시 무효화 | 배포 후 구버전 캐시 사용 | 기존 VersionChecker 활용 + SW 업데이트 프롬프트 |
| IndexedDB 용량 | 대형 학원 (학생 500+) 오프라인 데이터 | 최근 7일 데이터만 캐싱, 이전 데이터는 API 호출 |

### 14.2 UX 리스크

| 리스크 | 완화 |
|--------|------|
| 데스크톱 습관 사용자의 전환 저항 | 자동 리다이렉트 + "데스크톱으로 보기" 토글 상시 제공 |
| 기능 부족 불만 (데스크톱에 있는데 모바일에 없음) | Phase별 점진 추가, "더보기 → 데스크톱 버전" 링크 |
| section_mode 복잡도 | section_mode 비활성 테넌트에서는 반 선택 UI 미노출 |

### 14.3 운영 리스크

| 리스크 | 완화 |
|--------|------|
| 유지보수 표면적 증가 (app_admin + app_teacher) | shared/ 최대 활용, 도메인 로직은 공유 API에 집중 |
| 백엔드 BFF 엔드포인트 추가 부담 | 최소 3개 BFF만 추가 (today, session-summary, notification-summary) |
| 테스트 커버리지 증가 | 모바일 뷰포트 E2E 추가 (기존 Playwright 인프라 재사용) |

### 14.4 비적용 제약

- **app_teacher는 app_admin의 코드를 import하지 않는다.** 공유는 `shared/`를 통해서만.
- **Ant Design(antd)을 app_teacher에서 사용하지 않는다.** Tailwind + 자체 컴포넌트만.
- **기존 API를 모바일을 위해 변경하지 않는다.** 필요 시 BFF 엔드포인트를 추가한다.
- **테넌트 격리 로직을 일체 변경하지 않는다.**

---

## 부록 A: 파일 목록 (Phase 1 예상)

```
frontend/src/app_teacher/
├── app/
│   └── TeacherRouter.tsx
├── layout/
│   ├── TeacherLayout.tsx
│   ├── TeacherTopBar.tsx
│   ├── TeacherTabBar.tsx
│   ├── TeacherDrawer.tsx
│   └── TeacherThemeProvider.tsx
├── domains/
│   ├── today/
│   │   ├── pages/TodayPage.tsx
│   │   ├── components/SessionCard.tsx
│   │   ├── components/AlertBanner.tsx
│   │   ├── components/WeekSummary.tsx
│   │   └── api.ts
│   ├── attendance/
│   │   ├── pages/SwipeAttendancePage.tsx
│   │   ├── components/SwipeAttendanceList.tsx
│   │   ├── components/AttendanceCard.tsx
│   │   ├── components/AttendanceSummaryBar.tsx
│   │   ├── components/StatusBottomSheet.tsx
│   │   ├── hooks/useSwipeGesture.ts
│   │   └── api.ts
│   ├── scores/
│   │   ├── pages/MobileScoreEntryPage.tsx
│   │   ├── components/ScoreEntryCard.tsx
│   │   ├── components/ScoreProgress.tsx
│   │   └── api.ts
│   ├── students/
│   │   ├── pages/StudentListPage.tsx
│   │   ├── pages/StudentDetailPage.tsx
│   │   ├── components/StudentCard.tsx
│   │   ├── components/StudentQuickActions.tsx
│   │   └── api.ts
│   ├── lectures/
│   │   ├── pages/LectureListPage.tsx
│   │   ├── pages/LectureDetailPage.tsx
│   │   ├── pages/SessionDetailPage.tsx
│   │   └── api.ts
│   └── profile/
│       ├── pages/ProfilePage.tsx
│       └── api.ts
├── shared/
│   ├── hooks/
│   │   ├── useOfflineQueue.ts
│   │   ├── usePullToRefresh.ts
│   │   └── useTeacherPermissions.ts
│   ├── ui/
│   │   ├── SwipeCard.tsx
│   │   ├── BottomSheet.tsx
│   │   ├── PullToRefresh.tsx
│   │   ├── FloatingActionButton.tsx
│   │   ├── CardSkeleton.tsx
│   │   └── UndoSnackbar.tsx
│   └── offline/
│       ├── OfflineQueue.ts
│       ├── OfflineIndicator.tsx
│       └── SyncManager.ts
└── pwa/
    ├── manifest.json
    ├── sw.ts
    ├── pushManager.ts
    └── icons/
        ├── icon-192.png
        ├── icon-512.png
        └── apple-touch-icon.png

backend/apps/domains/teacher_app/   (신규)
├── __init__.py
├── apps.py
├── urls.py
├── views.py
├── serializers.py
├── push/
│   ├── __init__.py
│   ├── models.py
│   ├── views.py
│   ├── serializers.py
│   └── service.py
└── migrations/
    └── 0001_initial.py
```

## 부록 B: 기존 코드 참조 맵

| 기존 파일 | 참조 목적 |
|-----------|----------|
| `app_student/layout/StudentLayout.tsx` | 모바일 전용 레이아웃 패턴 참조 |
| `app_student/layout/StudentTabBar.tsx` | 하단 탭 바 구현 참조 |
| `app_admin/domains/lectures/pages/attendance/SessionAttendancePage.tsx` | 출석 비즈니스 로직 참조 |
| `app_admin/domains/scores/` | 성적 입력 비즈니스 로직 참조 |
| `app_admin/domains/admin-notifications/` | 알림 카운트 API 참조 |
| `app_admin/layout/TeacherBottomBar.tsx` | 기존 모바일 탭 바 (대체 대상) |
| `shared/hooks/useIsMobile.ts` | 모바일 감지 공유 |
| `shared/api/` | API 인프라 공유 |

---

*이 문서는 구현 착수 전 리뷰를 위한 설계 초안이다. 실제 구현 시 코드 수준의 결정은 현재 코드베이스 상태에 따라 조정된다.*
