# 기여와 공개 원칙

TalkTurn 소스는 AGPL-3.0-only로 준비합니다. 복제·수정·상업적 이용을 허용하며 해당 라이선스의 소스 제공 의무가 적용됩니다. 경쟁 포크나 광고를 제거한 포크도 일률적으로 금지하지 않습니다. 저작권 고지·라이선스는 유지해야 합니다.

수정본을 배포할 때는 변경 사실을 표시하고 AGPL에 맞게 대응 소스를 제공합니다. 수정한 프로그램을 네트워크로 사용자와 상호작용하게 제공하면 제13조에 따라 해당 사용자에게 대응 소스를 받을 기회를 제공해야 합니다. 개인적으로 수정했다는 이유만으로 모든 변경을 인터넷에 공개해야 하는 것은 아닙니다. 구체적인 조건은 [LICENSE](LICENSE)와 [AGPL 원문](https://opensource.org/license/agpl-3.0)을 따릅니다.

기여물은 해당 라이선스로 배포할 권리가 있는 코드만 제출해 주세요. 이 프로젝트는 기여자의 저작권을 자동 양도받지 않습니다. 나중에 다른 라이선스나 비공개 기능으로 재사용할 때에는 기여 코드와 외부 의존성의 권리를 별도로 확인해야 합니다.

TalkTurn 이름이나 로고 사용으로 공식 배포·승인을 받은 것처럼 표시하지 마세요. 수정 포크에는 구별되는 이름·로고를 권장합니다. 사실에 맞는 출처 표시는 허용하며 자세한 기준은 [브랜드 정책](TRADEMARKS.md)을 따릅니다. 제삼자 코드와 브랜드의 권리는 기존 권리자에게 있습니다.

PR에는 변경 목적·영향·직접 관련 검증을 적습니다. 개인 채팅, API 키, OAuth 토큰, 계정 설정, 스크린샷, 사용량 원본을 포함하지 않습니다. Windows UI 연동 수정은 연결한 self-chat만 대상으로 검증하고 임의 대화방으로 전송하지 않습니다.

버그 제보에는 Windows·Python·카카오톡 PC·해당 CLI의 버전, 문제가 생긴 단계, 재현 순서, 기대 결과와 실제 결과를 적어 주세요. 개인 대화 대신 짧은 테스트 문구를 사용하고 계정 이름·세션 UUID·로컬 경로는 지워 주세요. `reply-log.json`을 참고한다면 필요한 `stage`·`gate`만 옮기고, 로그 전체·인증 파일·실패 캡처를 그대로 첨부하지 않습니다. 전송 성공 여부가 불분명하면 먼저 카톡과 원래 세션의 접수를 확인하고 같은 요청을 반복하지 마세요.

저장소는 [xodn-sell/talkturn](https://github.com/xodn-sell/talkturn)이며 현재 비공개 상태로 소스 업로드, Windows CI와 macOS 공통 기능 CI 검증을 마쳤습니다. 알파 공개 후 [Releases](https://github.com/xodn-sell/talkturn/releases)에서 wheel과 같은 버전의 소스 아카이브를 함께 제공합니다.

Windows의 Python 3.11·3.13에서 `python -m unittest discover -s tests -v`와 `python -m build`로 검증합니다. CI는 모의 provider와 임시 상태를 사용하며 로그인·카카오 UI 조작·메시지 전송을 실행하지 않습니다. 실제 수신 검증 결과는 단위 테스트 결과와 구분해 적습니다.

2026-11-17 [Windows CI 실행 44034353934](https://github.com/xodn-sell/talkturn/actions/runs/44034353934)는 커밋 `6a7a5e6`의 Python 3.11·3.13에서 각각 231건 중 230건 통과·POSIX 전용 1건 건너뜀으로 완료됐습니다. [macOS CI 실행 44034756881](https://github.com/xodn-sell/talkturn/actions/runs/44034756881)는 커밋 `00b549b`의 Intel 15.7.9/Python 3.11.9·Apple Silicon 26.7.2/Python 3.13.17에서 각각 공통 시험 79건 중 76건 통과·Windows 전용 3건 건너뜀으로 완료됐습니다. 각 작업에서 만든 wheel의 설치·CLI·한글 이미지도 확인했습니다. 두 커밋은 실행 코드·테스트가 같고 macOS 워크플로 설정만 다릅니다.

macOS 공통 기능 변경 전에는 AGY 통합 후 224개 테스트가 있었고, 공통 기능 검사 7개를 추가해 최신 전체 시험 집합은 231개입니다. Mac에서는 Windows UI·키보드·글꼴 의존 시험을 제외한 범위만 실행합니다. 전체 목록은 [macOS 워크플로](.github/workflows/macos.yml)를 따르며, 플랫폼 전용 건너뛰기를 숨기거나 Mac 전체 지원으로 기록하지 않습니다. 같은 시험을 여러 환경에서 실행한 횟수를 서로 다른 테스트 수로 합산하지 않습니다.

Mac 렌더 검사는 `TALKTURN_FONT=/System/Library/Fonts/AppleSDGothicNeo.ttc`를 직접 지정한 [설치본 smoke](tests/macos_smoke.py)로 수행합니다. 한글 글리프·이미지 크기를 확인한 범위이며 글꼴 자동 탐색·모든 이모지 표시 보장은 아닙니다. `setup`이 Windows 필요 안내로 거절되는지도 확인합니다. macOS의 카카오톡 자동화·OCR은 지원하지 않으며 실제 provider 로그인 연동은 미검증입니다. 실제 카톡 왕복과 다른 사용자 PC의 앱 연동 검증은 계속 별도 항목입니다. 비공개 저장소의 CI 링크에는 접근 권한이 필요합니다.

AGY 변경은 ACP 목록 메타데이터 조회와 동일 세션 ID·작업 경로 재개를 구분해 검증합니다. 저장 대화를 실행 중이라고 표시하거나 원래 터미널에 직접 연결한 것으로 기록하지 않습니다. 원격 permission 취소·추가 질문 미지원·접수 불명 시 재전송 금지·기존 사용자 훅 보존도 확인합니다. 완료 훅의 transcript 시험은 합성 기록으로 작성하고 실제 대화나 계정 정보를 fixture에 넣지 않습니다.

완료 훅은 문서상의 `model_stop`과 실제 CLI 1.5.9에서 확인한 `NO_TOOL_CALL`만 종료 사유로 허용하고, `fullyIdle: true`·오류 없음·선택된 세션 조건을 함께 검사합니다. 세션별 로컬 `transcript.jsonl`·`transcript_full.jsonl`의 제한된 끝부분에서 현재 사용자 입력 뒤의 완료된 `PLANNER_RESPONSE.content`만 추출합니다. `thinking` 필드 제외, 불완전 기록·다른 세션 경로·링크 경로 거절을 유지해 주세요. 실제 AGY 직접 시험의 문맥 유지·훅 추출 성공과 카카오톡 왕복 성공도 구분합니다.

배포 변경에는 현재 소스로 sdist와 wheel을 만들고, 별도 가상환경에 wheel을 설치한 뒤 실제 import 경로와 핵심 CLI를 확인합니다. 문서만 바꿨다면 해당 문서의 명령·링크·확인 범위를 대조하는 것으로 충분합니다. UI·수신 경로를 바꿨다면 모의 테스트만으로 실제 카톡 검증을 완료했다고 기록하지 않습니다.

`tests/fixtures/kakao-quote-original.png`와 `kakao-quote-thumbnail.png`는 TalkTurn 검증용으로 내부 생성한 그림과 그 카카오 인용 썸네일입니다. 테스트 문구만 포함하며 개인 대화 화면은 포함하지 않습니다. 두 테스트 이미지는 프로젝트와 같은 AGPL-3.0-only로 제공합니다.
