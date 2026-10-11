# duet — 두 AI 모델이 만들고, 규칙은 하네스가 검사한다

> 이 페이지는 영어 README에서 옮긴 짧은 입문서입니다. **기준은 [영어 README](../../README.md)** 이며, 모든 실행 기록은 [docs/QA.md](../QA.md)에 있습니다.

AI 에이전트 하나는 자기 숙제를 스스로 채점하고 "통과"라고 보고합니다. duet는 **두 번째 모델**을 참여시키고, **테스트를 하네스가 직접 실행**하며, 각 워크플로에 규칙을 겁니다. 그 규칙은 어느 모델의 말이 아니라 실제로 일어난 일에 비추어 검사됩니다.

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

또는 Claude Code 안에서 플러그인으로:

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

이미 가진 Claude와 ChatGPT 구독을 그대로 사용합니다 — **API 키가 필요 없습니다**. 구독이 하나뿐이어도 동작합니다 (예: `--pair claude:opus+claude:sonnet`).

## 아홉 개의 명령, 각각 검증되는 규칙

```bash
duet build "가계부 웹 앱"                  # 처음부터: 수용 기준 → 테스트 → 코드
duet fix "쉼표가 들어간 행이 내보내기에서 빠짐" # 테스트는 원래 코드에서 실패하고 수정 후 통과해야 함
duet add "report에 --json 옵션"            # 그 기능이 없으면 실패하는 테스트가 필요
duet refactor "parser.py를 둘로 분리"       # 기존 테스트는 한 바이트도 바꿀 수 없음
duet plan "저장소를 Postgres로 이전"        # 논의 끝에 PLAN.md만 바뀔 수 있음
duet review                                # 다른 모델이 diff를 읽기 전용으로 리뷰
duet make "…" --like URL                   # 참고 앱 조사, 구축, 기능 동등성 점수, 클린룸 검사
duet jury                                  # 가상의 사용자·고객·투자자·심사위원; 모든 지적은 제품을 인용
duet page check index.html                 # 웹사이트의 게이트: 데스크톱과 모바일 폭에서 잘못 보이는 것
```

`fix`는 완성된 테스트를 **원래 코드의 스냅샷 위에서 다시 실행**합니다. 버그가 있는 코드에서도 테스트가 통과한다면 그 테스트는 버그를 잡지 못한 것이므로, 양쪽의 승인이 취소됩니다.

## 결과

문장 하나와 빈 디렉터리에서 시작해, duet는 가계부 웹 앱(싱글 페이지 UI, JSON API, SQLite, CSV 내보내기)을 **35분** 만에 만들었습니다. 그 뒤 직접 공격했습니다: SQL 인젝션은 평범한 텍스트로 저장되었고, 저장형 XSS는 실제 브라우저에서 텍스트로 표시되었으며, CSV 수식 인젝션은 무력화되었고, `0.1 + 0.2`의 합계는 정확히 `0.30`이었습니다.

## 기본값으로 만들기

```bash
duet skill default    # 되돌리기: duet skill undefault
```

이후 Claude Code, Codex, Gemini CLI, Antigravity, OpenCode는 실제 코드 변경(버그 수정, 기능 추가, 리팩터링)을 duet에 맡기고, 질문이나 한 줄 수정은 그대로 직접 처리합니다.
