# Binance Futures Auto Trading Bot v1.1

바이낸스 선물(USDT-M) 자동 매매 봇입니다. 복합 시그널 분석, AI 기반 자동 튜닝, 다층 리스크 관리 시스템을 탑재하고 있습니다.

---

## 주요 기능

- **복합 시그널 분석**: 모멘텀(50%) + 거래량(30%) + 멀티타임프레임 EMA(20%) 기반 진입 판단
- **자동 튜닝**: 시장 레짐(추세/횡보) 자동 감지 및 파라미터 최적화
- **다층 청산 시스템**: 부분 익절, ATR 트레일링 스탑, 손익분기 스탑, 시간 스탑, 시그널 감쇠 청산
- **리스크 관리**: Kelly Criterion 포지션 사이징, ATR 기반 손절, 세션 손실 한도
- **레짐 방향 바이어스**: 추세 방향과 일치하는 진입에 자동 보너스
- **스파이크 가드**: 급변동 시 즉시 청산 및 심볼별 쿨다운

---

## 빠른 시작 (exe 실행)

1. `BinanceAutoBot.exe`를 다운로드합니다
2. 바이낸스 계정에서 **선물 API Key**를 발급받습니다
3. **Windows 환경변수**에 API 키를 등록합니다:
   - `BINANCE_API_KEY` = 발급받은 API Key
   - `BINANCE_API_SECRET` = 발급받은 API Secret
4. `BinanceAutoBot.exe`를 실행합니다
5. 설정에서 테스트넷/실거래를 선택합니다

### 환경변수 설정 방법 (Windows)

```
[시작] → "환경 변수" 검색 → "시스템 환경 변수 편집"
→ "환경 변수" 클릭 → 사용자 변수에 "새로 만들기"

변수 이름: BINANCE_API_KEY
변수 값: (발급받은 API Key)

변수 이름: BINANCE_API_SECRET
변수 값: (발급받은 API Secret)
```

테스트넷을 사용하려면 `TESTNET_API_KEY`, `TESTNET_API_SECRET`도 동일하게 등록합니다.

---

## 소스에서 실행 (개발자용)

### 요구사항

- Python 3.9 이상
- Windows 10/11

### 설치

```bash
git clone <repo-url>
cd binance-auto-bot
pip install -r requirements.txt
```

### 실행

```bash
python bot_gui.py
```

### exe 빌드

```bash
pip install pyinstaller
pyinstaller bot.spec
```

또는 `build.bat`를 실행합니다.

---

## 프로젝트 구조

```
├── bot_gui.py                    # GUI 메인 (Tkinter)
├── gui_config.json               # 런타임 설정
├── gui_config.template.json      # 배포용 설정 템플릿
├── assets/                       # UI 아이콘/이미지
├── binance_futures_bot1_1/
│   ├── main.py                   # 엔진 진입점
│   └── binance_futures_bot/
│       ├── tick_engine.py        # 핵심 매매 엔진
│       ├── config.py             # 엔진 설정 (EngineConfig)
│       ├── auto_tuner.py         # 자동 파라미터 튜닝
│       ├── neural_scorer.py      # 신경망 스코어러 (프리미엄)
│       ├── ai_advisor.py         # AI 어드바이저
│       ├── license_gate.py       # 라이선스 검증
│       ├── risk_limits.py        # 리스크 한도
│       └── exchange_utils.py     # 거래소 유틸
├── requirements.txt
├── bot.spec                      # PyInstaller 빌드 설정
├── build.bat                     # 원클릭 빌드 스크립트
└── README.md
```

---

## 주요 파라미터

| 파라미터 | 기본값 | 설명 |
|---------|-------|------|
| position_pct | 12% | 잔고 대비 포지션 크기 |
| composite_min_score | 0.72 | 최소 진입 복합 스코어 |
| max_single_trade_loss_pct | 1.8% | 단일 거래 최대 손실 |
| max_loss_per_position | 1.8% | 포지션 최대 손실 한도 |
| partial_tp_levels | 0.5R/1.0R/1.8R | 부분 익절 단계 |
| trail_activate_pnl_pct | 0.8% | 트레일링 스탑 활성화 기준 |
| symbol_reentry_cooldown_sec | 120초 | 동일 심볼 재진입 대기 |

전체 파라미터는 `gui_config.json`에서 확인 및 수정할 수 있습니다.

---

## 주의사항

- **투자 원금 손실 가능성이 있습니다.** 반드시 테스트넷에서 충분히 검증한 후 실거래를 시작하세요.
- API Key에는 **선물 거래 권한**만 부여하고, **출금 권한은 반드시 비활성화**하세요.
- IP 제한을 설정하면 보안이 강화됩니다.

---

## 라이선스

이 소프트웨어는 개인 사용 목적으로 무료 배포됩니다. 상업적 재배포는 금지됩니다.
