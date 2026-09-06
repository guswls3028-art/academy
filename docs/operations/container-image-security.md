# 컨테이너 이미지 보안 게이트

운영 후보 이미지는 immutable ECR digest로 식별하며 persistent development보다
먼저 기본 ECR scan을 완료해야 한다. 실행 정본은
`.github/workflows/v1-build-and-push-latest.yml`과
`scripts/v1/ecr-critical-scan-gate.py`다.

## 빌드 입력과 런타임 패키지

- 공통 Python 이미지는 `docker/Dockerfile.base`의 두 stage 모두 같은 upstream
  OCI index digest로 고정한다. 태그가 이동해도 승인되지 않은 OS 변경이 빌드에
  섞이지 않는다.
- Docker Dependabot이 `/docker`를 매주 확인하며, base digest 변경은 일반 PR과
  ECR scan을 다시 통과해야 한다. Python 3.11 minor line을 유지하고 3.12+
  전환은 별도 호환성 검증 없이는 자동 제안하지 않는다.
- 변경 감지나 수동 전체 빌드가 공통 base 빌드를 선택하면 workflow run ID와
  attempt를 `APT_REFRESH_TOKEN`으로 전달한다. 이 값은 builder/runtime의 APT
  설치 레이어를 무효화하므로 오래된 `apt-get update` 결과를 BuildKit 캐시에서
  재사용하지 않고, 당시 Debian 저장소의 최신 보안 패키지를 설치한다. base가
  선택되지 않은 일반 앱 코드 빌드는 기존 digest를 재사용한다.
- 공통 runtime은 `openssl`과 `util-linux`를 명시적으로 설치하고 각각
  `3.5.7-1~deb13u2`, `2.41.5-0+deb13u1` 이상인지 빌드 중 검증한다. upstream
  slim digest에 더 낮은 essential package가 들어 있어도 단순 `apt-get update`에
  의존하지 않으며, Debian stable 보안 수정본이 후보에 실제 포함되지 않으면
  이미지 빌드가 실패한다.
- pip Dependabot은 같은 호환 버전이 필요한 `boto3`/`botocore`를 한 PR로
  갱신하고, 개발 의존성 및 GitHub Actions minor/patch는 각각 묶어 중복 CI를
  줄인다. 모든 묶음은 개별 업데이트와 같은 전체 품질·이미지 scan 게이트를
  통과해야 한다.
- 런타임에는 앱이 실제 사용하는 패키지만 둔다. DB migration과 점검은 Django와
  AWS/RDS readback을 사용하므로 `postgresql-client` CLI는 제거했고, Python
  PostgreSQL 연결에 필요한 `libpq5`는 유지한다.
- 최종 런타임 이미지는 upstream `python:3.11-slim`에서 상속한 미사용
  `perl-base`를 마지막 apt 경계 뒤에 제거한다. 공통 base가 상속만 하는
  Messaging을 봉인하고, API·Video·AI·Tools는 각 전용 apt 설치가 끝난 뒤 다시
  제거해 전이 의존성의 재유입도 막는다. 정리된 base 위에서 Debian 패키지의
  debconf/postinst 구성이 Perl을 요구하므로, 이 네 이미지의 최종 runtime apt
  `RUN`은 설치 목록 맨 앞에 `perl-base`를 구성 전용 prerequisite로 명시한다.
  `dpkg --configure -a` 뒤 즉시 purge·autoremove·clean하며 이 계층 밖으로 Perl을
  복사하거나 유지하지 않는다. Debian trixie `5.40.1-6`에는 stable 수정본이 없으며
  forky/sid 패키지를 stable 이미지에 섞지 않는다. 모든 제거 경계는
  `dpkg --audit`와 `apt-get check`가 정상이고, `dpkg-query`와 `command -v perl`이
  모두 실패해야 빌드를 계속한다. 후속 Tesseract,
  OpenCV/embedded FFmpeg, sentence-transformers와 각 entrypoint 검증이 비-Perl
  런타임을 봉인한다. 따라서 어떤 runtime repository도 Perl Critical/High 예외
  범위에 포함하지 않으며, Perl이 다시 들어오면 완료 ECR scan에서 신규
  finding으로 실패 폐쇄한다.
- Video의 비공개 `ffmpeg-builder` stage는 `build-essential`·`git`의 실제 빌드
  의존성인 Perl toolchain을 유지할 수 있다. 이 stage는 ECR에 publish하지 않고
  final stage로는 `/opt/academy-ffmpeg`만 정확히 복사한다. broad filesystem,
  dpkg database, interpreter 또는 Perl module copy는 계약 테스트가 차단하며,
  최종 이미지 검증은 복사된 트리와 FFmpeg 동적 링크에도 Perl payload가 없는지
  확인한다.
- builder의 Python 패키지는 최종 `appuser` 소유권을 지정한
  `COPY --chown`으로 runtime에 한 번만 기록한다. 앱 소스도 같은 방식으로
  복사하며 별도 `RUN chown -R`을 두지 않는다. 별도 chown 레이어는 같은 파일
  바이트를 이미지에 다시 저장해 ECR 크기와 cold pull을 늘리기 때문이다.
- 서비스 Dockerfile은 apt/Python requirements를 `academy/`, `apps/`, `libs/`
  소스보다 먼저 설치한다. 코드 변경은 entrypoint 검증과 소스 레이어만
  무효화하며, requirements가 그대로면 의존성 설치 캐시를 재사용한다.
- 시스템 FFmpeg는 실제 변환을 수행하는 격리된 AWS Batch Video 이미지에만 둔다.
  Debian stable FFmpeg에 수정 패키지가 없는 보안 결함은 High 상한을 올려
  넘기지 않는다. Video 이미지는 공식 FFmpeg GitHub mirror의 전체 commit SHA를
  고정하고 checkout SHA를 빌드 중 재검증한 뒤 source build를 사용한다. 현재
  `db05df9d135fb56a4babb836d5e9f5c1d984e087`은
  CVE-2026-70628과 CVE-2026-70632 수정을 모두 포함한다. 최종 이미지에는
  Debian `ffmpeg`/`libav*` 패키지를 넣지 않고, 고정 빌드의 `ffmpeg`와
  `ffprobe`, H.264 `libx264`, AAC, HLS 실제 변환 smoke를 이미지 빌드에서
  통과시킨다. checkout한 전체 SHA는 설치 디렉터리의
  `academy-source-commit` 마커에 기록하고 최종 stage에서 다시 대조한다.
  FFmpeg가 shallow checkout SHA를 자체 version 문자열에 노출하는지에는
  의존하지 않는다. Video source build만 공개 저장소용 GitHub
  `ubuntu-24.04-arm`에서 네이티브로 수행하며, 다른 runtime 이미지는 기존
  x64 runner와 QEMU 경계를 유지한다. commit을 바꿀 때는 두 수정의 ancestry,
  전체 SHA, 설치 마커, HLS smoke,
  ECR 완료 스캔과 기존 High 상한 비증가를 함께 확인한다. Debian FFmpeg와
  전이 패키지를 제거한 뒤 Video 이미지의 High 상한은 공통 base와 같은 8로
  즉시 낮추며, 이 수치를 넘는 후보는 다시 실패 폐쇄한다.
  API의 upload-complete probe는 실패 허용 보조 검사이고 Video worker가 최종 검증과
  변환을 소유한다. AI frame extraction은 OpenCV wheel에 포함된 FFmpeg 지원을 쓰며,
  wheel이 그 기능을 잃으면 AI 이미지 빌드가 즉시 실패한다. AI와 Video 런타임은
  GUI가 없는 `opencv-python-headless`를 사용하므로 system `libglib2.0-0`을 OpenCV
  호환용으로 직접 설치하지 않는다. Video 후보에는 GLib가 남지 않으며, API·AI·Tools
  OCR 런타임은 Debian `tesseract-ocr`의 필수 전이 의존성으로만 정확한 GLib
  패키지를 포함한다. OpenCV import/FFmpeg smoke와 완료된 ECR scan이 이 경계를
  봉인한다.
- Debian Tesseract 실행 파일과 `libtesseract5`는 `libcurl4t64`를 직접 요구한다.
  따라서 이 패키지는 OCR을 소유하는 API·AI·Tools에만 존재하고 Base·Video·Messaging은
  포함하지 않는다. Trixie stable의 `8.14.1-2+deb13u4`는
  CVE-2026-8924와 CVE-2026-8927 수정 전 버전이므로 세 OCR Dockerfile은 공식
  `trixie-backports`의 `8.21.0-2~bpo13+1`을 exact pin으로 설치한다. 빌드 중
  필요한 backports 의존성 폐쇄도 `libnghttp3-9=1.15.0-1~bpo13+1`,
  `libngtcp2-16=1.22.1-1~bpo13+1`,
  `libngtcp2-crypto-ossl0=1.22.1-1~bpo13+1`로 함께 고정한다. 그렇지 않으면 APT가
  stable의 이전 HTTP/3 라이브러리를 유지해 libcurl 설치를 거부한다. 각
  `dpkg-query` 결과가 exact pin과 다르면 즉시 실패하고, backports source와 APT
  index는 같은 패키지 경계가 끝날 때 제거한다. 이 예외는 Debian suite 전환이나
  전체 backports upgrade가 아니며 Tesseract의 필수 공유 라이브러리 폐쇄만 올린다.
  `tests/test_ecr_critical_scan_gate.py`가 세 affected image와 세 unaffected image의
  exact 경계를 검사한다. 세 Dockerfile의 `curl-fixed-system` target은 공통 base와
  실제 OCR APT 계층만 따로 빌드·실행·scan할 수 있게 하며 최종 runtime은 그 target을
  그대로 상속한다. 완료 ECR scan은 두 CVE가 어떤 affected digest에도 남아 있으면
  기존 Critical gate에서 계속 실패 폐쇄한다.

## Critical 및 High 판정

1. 후보 manifest에 `source=built`인 각 digest의 scan 결과가 없으면 CI가
   repository-scoped `ecr:StartImageScan` 권한으로 scan을 호출한다. 재사용
   digest라는 이유로 scan을 건너뛰지 않는다. ECR이 동일 digest scan quota가
   이미 소비됐다고 응답해도 기존 scan의 `COMPLETE` readback은 끝까지 요구한다.
2. scan이 `COMPLETE`가 아니거나 Critical/High finding identity(CVE, package,
   version)가 불완전하면 실패 폐쇄한다. ECR severity count와 중복 제거한 exact
   identity 수가 다를 때도 결과를 신뢰하지 않는다.
3. 승인되지 않은 Critical은 하나라도 있으면 development/preprod/production으로
   진행하지 않는다.
4. 예외는 `docs/ssot/ecr-critical-risk-acceptance.json`에 repository, CVE,
   package, version, 만료일, Debian tracker와 도달 가능성 근거를 모두 정확히
   적은 항목만 허용한다. wildcard는 없으며 package version이나 CVE가 달라지면
   즉시 실패한다.
5. 만료일 다음 날부터는 scan 전에 전체 게이트가 실패한다. 만료 연장은 새
   vendor 상태와 실제 사용 경로를 다시 검토한 PR로만 가능하다.
6. High는 `docs/ssot/ecr-high-risk-baseline.json` schema 3의 repository별 상한과
   `acceptedHighFindings` exact identity를 모두 비교한다. metadata 없는 별도 known
   목록은 허용하지 않는다. 모든 High 항목은 exact Debian tracker, 실제 런타임의
   도달 가능성 근거와 hard expiration을 가져야 하며, 만료 다음 날에는 scan 전에
   실패한다. 수가 같아도 CVE, package, version 중 하나가 바뀌거나 다른 High가 기존
   항목을 대체하면 실패한다. 반대로 패키지 제거 또는 vendor 수정으로 기존 항목이
   사라져도 기준선이 stale하다고 실패하므로, 운영 scan readback을 근거로 identity와
   상한을 같은 PR에서 내려야 한다. 알 수 없는 항목, 누락된 기존 항목,
   identity/count 불일치 중 어느 것도 development/preprod로 진행할 수 없다.

2026-08-20 후보 `sha-31d3845d9...-run-32316780655-1`의 완료된 ECR scan을
재검토했다. Base·Video·Messaging은 glibc 1건과 Perl 3건으로 Critical 4건,
API·AI·Tools는 여기에 GLib 1건이 더해져 Critical 5건이었다. Debian 공식
tracker는 이 exact Trixie 패키지를 계속 affected 또는 `no-dsa`/minor로 표시하고
stable 수정 패키지를 제공하지 않는다. Academy의 Python entrypoint, Perl 미사용,
GLib D-Bus introspection 미사용 경계를 다시 확인한 뒤 이 다섯 exact
CVE/package/version 항목만 2026-09-19까지 재승인했다. 이전 Mbed TLS Critical
3건은 같은 후보의 완료 scan에서 더 이상 관측되지 않아 acceptance에서 즉시
삭제했다. 다시 나타나거나 identity가 달라지면 새 Critical로 실패 폐쇄한다.

2026-08-09 ECR 데이터베이스 갱신으로 동일한 GLib `2.84.4-3~deb13u3`에
`CVE-2026-58010`부터 `CVE-2026-58015`까지 여섯 High가 새로 나타났다. 후보와 직전
운영 digest의 finding identity를 대조해 패키지 변경이 아니라 신규 공개분임을
확인했다. Debian은 여섯 건 모두 trixie `no-dsa`/minor로 분류하며, 각각 GVariant
비정상 역직렬화, 잘못 생성된 GDateTime, `G_REGEX_RAW` case escape, 다중 문자
GIOChannel terminator, 빈 locale key-file 값, 악성 D-Bus 서버가 전제인 제한된
over-read/DoS 경로다. Academy OCR 경로는 이 API와 D-Bus를 사용하지 않는다.
따라서 Tesseract를 포함하는 API·AI·Tools만 검증된 현재 수치 18로 올렸고,
Base·Video·Messaging 상한 8은 유지했다. vendor 수정 패키지가 나오면 세 상한을
실제 scan readback에 맞춰 즉시 다시 낮춘다.

2026-08-11 ECR 데이터베이스 갱신은 같은 API·AI·Tools 이미지의 Debian
`libssh2` `1.11.1-1+deb13u1`에 `CVE-2026-58050`과 `CVE-2026-58051`을
추가했다. 직전 운영 AI digest와 새 후보의 finding identity를 비교했을 때
추가분은 이 두 건뿐이었고, 새 후보 여섯 개의 완료된 scan은 Base·Video·Messaging
High 8, API·AI·Tools High 20을 각각 반환했다. Debian tracker에는 아직 stable
수정 버전이 없다. 58050은 악성 SSH publickey 응답을 처리하는 32-bit allocation
overflow이고 Academy 운영 이미지는 ARM64다. 58051도 악성 SSH 서버의 publickey
subsystem 응답과 오류 cleanup이 전제다. 저장소의 앱·워커 entrypoint에는 SSH,
SFTP, SCP, Paramiko 또는 libssh2 실행 경로가 없고 운영 원격 명령은 컨테이너 밖의
AWS SSM이 소유한다. 따라서 불안정 Debian 패키지를 혼합하지 않고 완료된 scan의
현재 수치 20으로 세 상한만 갱신한다. vendor 추적은
`https://security-tracker.debian.org/tracker/CVE-2026-58050`과
`https://security-tracker.debian.org/tracker/CVE-2026-58051`이며, 수정 패키지가
나오면 새 이미지를 빌드·스캔하고 상한을 즉시 낮춘다.

2026-08-15 현재 같은 `libssh2` 패키지에 `CVE-2026-66032`부터
`CVE-2026-66035`까지 네 High가 추가되어 API·AI·Tools의 exact 집합은 공통 8건,
GLib 6건, libssh2 6건인 총 20건이다. Debian tracker는 trixie
`1.11.1-1+deb13u1`을 네 건 모두 vulnerable로, forky/sid `1.11.1-5`를 fixed로
표시한다. Academy 이미지에 다른 suite 패키지를 섞지 않으며, trixie 수정 패키지가
나오면 새 digest의 완료 scan에서 제거를 확인한 뒤 identity와 상한을 함께 낮춘다.
현재 앱·워커 entrypoint에는 SSH/SFTP client 호출 경로가 없다는 도달 가능성 경계는
유지하지만, 그 사실이 다른 CVE나 버전으로의 조용한 교체를 허용하지는 않는다.

2026-08-22 후보 `sha-8f8014d5a...-run-32493438087-1`의 완료된 ECR scan에서
새 API digest `sha256:1c9210dc...`와 AI digest `sha256:b395f75c...`는 동일한
`libssh2` `1.11.1-1+deb13u1`을 유지하면서 `CVE-2026-66032`만 남기고
`CVE-2026-58050`, `CVE-2026-58051`, `CVE-2026-66033`, `CVE-2026-66034`,
`CVE-2026-66035`를 더 이상 반환하지 않았다. 두 digest의 exact High 집합은 공통
8건, GLib 6건, libssh2 1건인 총 15건이므로 API·AI 상한과 다섯 finding의
repository identity를 함께 낮췄다. 같은 후보의 Video·Messaging은 기존 8건을
유지했다. 후속 후보 `sha-d06e895c1...-run-32498688185-1`은 새 Tools digest
`sha256:35123a457b7903688bd7553f5fb84a6938be5f72aef4f60d2f105618ed6b7481`을
빌드했다. 그 digest의 완료 scan도 동일한 다섯 CVE를 더 이상 반환하지 않고 공통
8건, GLib 6건, libssh2 1건인 exact High 15건을 반환했다. 따라서 Tools 상한을
15로 낮추고 기준선에서 다섯 finding을 제거했다. 이는 패키지 업그레이드나 위험
승인 확대가 아니라 ECR의 digest별 완료 scan readback을 exact 기준선에 반영한
것이다.

후속 후보 `sha-f92c02728...-run-32532674189-1`의 완료된 ECR scan에서는 API
`sha256:dd4ee4be...`, AI `sha256:ee976e64...`, Tools `sha256:bb19687d...` 세
digest 모두 같은 패키지의 마지막 `CVE-2026-66032`도 더 이상 반환하지 않았다.
세 digest의 exact High 집합은 공통 8건과 GLib 6건인 14건이며 나머지 finding
identity와 package version은 변하지 않았다. 따라서 세 저장소 상한을 14로
낮추고 마지막 libssh2 identity를 제거한다. 해당 후보는 stale 기준선 때문에
development 진입 전에 실패했고 production을 변경하지 않았으며, 이 축소를 포함한
다음 후보가 전체 release gate를 다시 통과해야 한다.

2026-08-23 sender/runtime 후보 `sha-43e9946e...-run-32614790812-1`의 새 API
`sha256:ebf04e84...`와 AI `sha256:d5cf49af...` 완료 scan은 같은 Debian trixie
`libssh2` `1.11.1-1+deb13u1`에서 앞서 사라졌던 여섯 High를 모두 다시 반환했다.
두 digest 모두 공통 8건, GLib 6건, libssh2 6건인 exact 20건이며 package version은
변하지 않았다. Debian tracker에서 trixie는 여섯 건 모두 vulnerable이고 stable
수정 패키지가 없다. 저장소와 두 runtime entrypoint에
SSH, SFTP, SCP, Paramiko 또는 libssh2 client 경로가 없고, 이 패키지는 API·AI의
Tesseract/libcurl 전이 의존으로만 존재한다. 따라서 다른 Debian suite 패키지를
혼합하지 않고 여섯 exact identity만 `acceptedHighFindings`에서 2026-09-19까지
한시 수용한다. 운영 Tools digest의 11:05 KST 완료 scan은 여전히 14건이고 동일 digest
재scan은 ECR quota로 거부됐으므로 Tools를 추론으로 승인하지 않고 상한 14를 유지한다.
새 Tools digest가 같은 finding을 실제 반환하면 그 exact 후보에서 별도 검토한다.
Debian stable fix나 API·AI ECR identity 변화가 먼저 나오면 acceptance를 즉시 제거한다.
run `32614790812`는 이 판정 전에 실패하여 development/preprod/production runtime을
변경하지 않았고 shared lock을 반환했다.

같은 날 후속 후보 `sha-2cd2ed8e...-run-32626283905-1`의 새 Tools digest
`sha256:f06386b6...` 완료 scan도 동일한 Debian trixie `libssh2`
`1.11.1-1+deb13u1` 여섯 High를 정확히 반환했다. 공통 8건, GLib 6건,
libssh2 6건인 총 20건이며 그 밖의 CVE, package, version 변화는 없다. Debian
tracker JSON은 여섯 건 모두 trixie에서 open이고 fixed version이 없음을 다시
확인했다. Tools Dockerfile은 API와 같은 Tesseract 전이 의존성으로 libssh2를
포함하지만 저장소의 Tools entrypoint, 앱, requirements에는 SSH, SFTP, SCP,
Paramiko 또는 libssh2 호출이 없다. 따라서 기존 2026-09-19 만료와 rationale을
그대로 적용해 여섯 exact identity의 repository 범위에 Tools만 추가하고 상한을
20으로 맞춘다. run `32626283905`는 이 scan gate에서 실패해
development/preprod/production runtime을 변경하지 않았고 shared lock을 반환했다.
다른 Tools finding이나 package version은 이 검토로 허용되지 않는다.

2026-08-26 후보 `sha-ff7e45a0...-run-32908570937-1`의 여섯 완료 scan은
Debian trixie `openssl` `3.5.6-1~deb13u2`에 새 High 여섯 건을 동일하게
반환했다. Base·Video·Messaging은 14건, API·AI·Tools는 26건이며 기존 exact
identity에는 변화가 없었다. `CVE-2026-14457`의 RPK key-only 설정,
`CVE-2026-18798`·`CVE-2026-63075`의 QUIC, `CVE-2026-54874`의 DTLS,
`CVE-2026-63072`의 CMS decrypt, `CVE-2026-63076`의 CMP/PBM 경로는 Academy
런타임에서 사용하지 않는다. 외부 HTTPS는 ALB에서 일반 인증서 TLS로 종료되고
컨테이너는 Gunicorn HTTP를 제공하며 QUIC·DTLS·CMS·CMP·raw-public-key
entrypoint가 없다. 따라서 exact package/version/CVE와 repository 여섯 개만
`2026-09-19`까지 한시 수용하고 상한을 완료 scan 수치로 맞춘다. vendor 수정
Debian 패키지가 나오거나 identity가 달라지면 예외를 제거하고 여섯 이미지를 다시
빌드·스캔한다. 이 후보는 scan gate에서 development 진입 전에 실패했고 shared
lock을 반환했으므로, 다음 후보가 persistent development부터 production mechanical
playback까지 전체 release gate를 새로 통과해야 한다.

같은 날 change-risk 계약 merge `11bc01f15...`의 후보 run `32933546410`은
AI digest `sha256:0c2f7416...` scan에서 기존 기준선 외 `CVE-2026-14456`
(`openssl` `3.5.6-1~deb13u2`)과 `CVE-2026-16118` (`glib2.0`
`2.84.4-3~deb13u3`)을 각각 한 건 발견하고 development 진입 전에 실패 폐쇄했다.
Debian trixie security에는 OpenSSL 수정판 `3.5.7-1~deb13u2`가 있으므로 공통
base가 `openssl`을 명시적으로 갱신하고 그 최소 버전을 빌드 중 검사한다. 이에 따라
이전 OpenSSL High 여섯 건의 한시 승인도 제거하고 여섯 image 상한을 함께 낮춘다.
GLib 건은 Debian stable 수정판이 아직 없고, 공격자가 로컬 사용자 쓰기 가능한
`XDG_DATA_HOME/mime/magic`을 만든 뒤 GLib content-type guess를 호출해야 한다.
API·AI·Tools는 로컬 사용자 세션과 사용자 제공 XDG MIME database를 노출하지 않고
Python validator를 사용하므로 exact 세 repository/package/CVE만 `2026-09-19`까지
한시 수용한다. 다음 후보는 공통 base와 여섯 runtime image를 모두 새 digest로
빌드·scan해 OpenSSL 제거와 GLib exact identity를 실측해야 하며, 그 전에는 어떤
development/preprod/production runtime도 변경하지 않는다.

2026-08-29 문서·배포 스크립트 정리 merge `6989a7b0c...`의 후보 run
`33210363052`는 AI digest `sha256:2af9fcd0...`의 완료 scan에서 직전 성공 AI
digest의 High 21건 외에 `CVE-2026-53615` (`util-linux` `2.41-5`) 한 건을
정확히 추가로 반환해 development 진입 전에 실패 폐쇄했다. Debian trixie
security는 `2.41.5-0+deb13u1`을 수정 버전으로 제공하므로 High 상한이나 예외를
늘리지 않는다. 공통 base가 `util-linux`를 명시적으로 갱신하고 해당 최소 버전을
빌드 중 검사하며, 다음 후보는 여섯 runtime image의 완료 scan에서 이 finding이
제거됐음을 실측한 뒤에만 development/preprod/production으로 진행한다. 실패한
run은 shared lock을 반환했고 운영 runtime을 변경하지 않았다.

현재 Critical 한시 항목은 Debian stable에 수정본이 아직 없거나 Debian이
`no-dsa`/minor로 분류한 glibc·GLib finding이다. GLib의
`g_dbus_node_info_new_for_xml` malformed introspection-XML 경로는 OCR CLI와
Academy Python 워커가 호출하지 않으며, 워커는 D-Bus introspection XML을 입력으로
받지 않는다. glibc 취약 native `scanf` 경로도 Academy Python 앱의 실행
경로가 아니다. 모든 최종 runtime은 미사용 `perl-base`를 제거하므로 Perl 예외를
갖지 않는다. Debian trixie가 glibc·GLib 패키지를 계속 vulnerable 또는
`no-dsa`로 표시하는 동안 다른 suite 패키지를 혼합하지 않고 정확한 현재 버전에
대한 한시 승인만 2026-09-19까지 유지한다. 이 판단은 위험을 삭제하지 않으며 다음
연장은 다시 vendor 상태와 실제 실행 경로를 검토한 PR이 필요하다.

2026-09-01 후보 run `33499266157`은 새 Debian DB가 반환한
`CVE-2026-42496`·`CVE-2026-8376` (`perl` `5.40.1-6`)을 AI 완료 scan에서
승인되지 않은 Critical로 탐지해 development 전에 실패 폐쇄했다. 같은 후보의
API·Video 완료 scan에도 두 identity가 존재함을 확인했다. 저장소와 runtime
entrypoint에는 Perl 실행 경로가 없고 stable 수정판도 없으므로 위험 승인을 늘리지
않는다. 공통 base에서 제거한 직후 API의 Tesseract apt를 실행한 failure-first
빌드는 Perl 기반 debconf frontend 부재로 `fontconfig-config` postinst가 code
100을 반환했다. 따라서 후속 apt가 있는 이미지는 같은 계층에서만 `perl-base`를
잠시 복원해 패키지 구성을 마친 뒤 제거한다. 공통 base와 전용 apt 경계의 최종
상태에서 `perl-base`를 제거하고, 기존 Perl
Critical 3건과 High 5건도 SSOT에서 함께 삭제한다. 다음 후보는 Base·API·Video·
Messaging·AI·Tools 여섯 완료 scan에서 새 두 CVE와 기존 Perl identity가 모두
사라지고 High exact identity가 새 상한과 일치해야만 release를 진행한다. 실패한
run은 development/preprod/production을 변경하지 않았고 shared lock을 반환했다.

2026-09-06 성적 편집 인계 후보 `sha-0134ce8c...-run-34013277396-1`은
development 진입 전 ECR High 게이트에서 신규 공개된 native-library finding을
차단했다. 여섯 새 digest의 완료 scan을 직접 재조회한 결과 `CVE-2026-86145`
(`pcre2` `10.46-1~deb13u1`)와 `CVE-2026-85091` (`zlib`
`1.3.dfsg+really1.3.1-1`)은 여섯 repository 모두에 있었고,
`CVE-2026-86140` (`libxml2`
`2.12.7+dfsg+really2.9.14-2.1+deb13u3`)은 API·Video·AI·Tools 네
repository에만 있었다. 실패한 run은 development/preprod/production을 모두
건너뛰고 shared lock을 반환했다.

이 세 finding은 High 상한이나 acceptance를 늘리지 않고
`docker/native-security/build-fixed-libs.sh`가 공통 base build에서 수정한다.
모든 원본과 patch는 HTTPS URL과 SHA-256으로 고정한다. zlib은 공개된
`e3dc0a85...` 수정 커밋을 기존 `zlib1g` ABI로 패키징하고, pcre2는 수정 릴리스
10.48의 8-bit shared library만 기존 `libpcre2-8-0` ABI로 패키징한다. libxml2는
새 SONAME으로 직접 교체하지 않는다. trixie `deb13u3` 전체 patch series를 먼저
적용한 2.9.14 source에 공식 `d1686f91...` bounds-check patch만 backport하여
`libxml2.so.2`를 유지한다. 세 package는 base runtime의 같은 Debian package
이름을 원자적으로 upgrade하므로 이후 service `apt` layer가 취약 버전으로
downgrade하지 않는다.

Base build는 세 upstream test suite, exact package 최소 버전, Python zlib
round-trip, libxml2 dynamic load, PCRE2 match를 모두 확인한다. base/security 파일이
바뀐 PR은 `Native security arm64 image contract`가 production과 같은 arm64 이미지를
실제로 build하고 같은 ABI 확인을 컨테이너 안에서 반복한다. 어떤 source hash,
patch, build, ABI load 또는 version check가 달라도 이미지 생성 자체가 실패한다.
다음 후보는 기존 상한 Base 3, API 16, Video 3, Messaging 3, AI 16, Tools 16을
그대로 만족하면서 세 CVE가 여섯 완료 scan에 없음을 입증해야만 persistent
development, isolated preprod, production 순서로 진행한다.

집중 검증:

```powershell
python -m pytest tests/test_ecr_critical_scan_gate.py -q
python -m pytest tests/test_release_performance_contract.py -q
pwsh scripts/v1/test-workflow-governance-contract.ps1
```
