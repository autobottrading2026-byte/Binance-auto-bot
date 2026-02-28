## v1.1.0 - 첫 공식 배포

바이낸스 선물(USDT-M) 자동매매 봇의 첫 공식 릴리즈입니다.

### 주요 기능

- **복합 시그널 기반 자동 진입/청산** — 모멘텀(50%), 거래량(30%), MTF EMA(20%) 복합 스코어링
- **다층 청산 시스템** — 손절, 부분익절, ATR 트레일링, 브레이크이븐, 시그널 감쇠, 타임스탑
- **ATR 기반 포지션 사이징** + Kelly Criterion 적용
- **레짐 감지** — trend_up / trend_down / chop 자동 판별 및 방향 바이어스
- **Maker 우선 청산** — 수수료 절감을 위한 지정가 청산 (5bps 오프셋, 4회 시도)
- **심볼별 재진입 쿨다운** — 동일 심볼 연속 진입 방지 (120초)
- **Chop 레짐 포지션 제한** — 횡보장에서 최대 2개 심볼로 리스크 관리
- **Auto Tuner** — 레짐별 파라미터 자동 최적화
- **다크 테마 GUI** — 한국어/영어 지원, 실시간 모니터링
- **자동 업데이트 확인** — 새 버전 출시 시 알림

### 바이낸스 API Key 발급 방법

#### 📌 테스트넷 (모의거래)

1. https://testnet.binancefuture.com 접속
2. GitHub 계정으로 로그인
3. 하단 **"API Key"** 메뉴 클릭
4. **"Generate HMAC_SHA256 Key"** 클릭
5. API Key와 Secret Key 복사 후 환경변수에 등록
6. ⚠️ 테스트넷 키는 주기적으로 만료되므로 재발급 필요할 수 있음

#### 📌 실거래 (메인넷)

1. https://www.binance.com 로그인
2. 우측 상단 프로필 아이콘 → **"API 관리"** 클릭
3. API Key 라벨 입력 (예: `AutoBot`) → **"API 키 생성"** 클릭
4. 이메일/2FA 인증 완료
5. API Key와 Secret Key 복사 후 환경변수에 등록
6. **API 제한 설정 (필수):**
   - ✅ **선물 거래 활성화** — "선물" 체크
   - ✅ **IP 접근 제한** — 본인 IP만 허용 권장
   - ❌ **출금 비활성화** — 출금 권한은 반드시 OFF
7. ⚠️ Secret Key는 생성 시 한 번만 표시되므로 반드시 즉시 복사

### 설치 방법

1. `BinanceAutoBot.exe` 다운로드

2. Windows 환경변수에 API Key 등록 (프로그램 내에서 직접 키를 입력하지 않으므로 보안 안전)

   **환경변수 설정 방법:**
   1. `Win + S` 키를 눌러 검색창 열기
   2. **"환경 변수"** 검색 → **"시스템 환경 변수 편집"** 클릭
   3. **"환경 변수(N)"** 버튼 클릭
   4. **사용자 변수** 영역에서 **"새로 만들기(N)"** 클릭
   5. 아래 변수를 각각 추가:

   | 변수 이름 | 변수 값 | 용도 |
   |-----------|---------|------|
   | `TESTNET_API_KEY` | 테스트넷 API Key | 모의거래 |
   | `TESTNET_API_SECRET` | 테스트넷 API Secret | 모의거래 |
   | `BINANCE_API_KEY` | 실거래 API Key | 실거래 |
   | `BINANCE_API_SECRET` | 실거래 API Secret | 실거래 |

   6. **확인** 눌러 모두 저장
   7. ⚠️ 이미 프로그램이 실행 중이라면 **재시작** 필요

3. 실행 후 설정 탭에서 환경변수 상태 확인 (설정됨 / 미설정 표시)
4. 테스트넷으로 먼저 테스트 권장

### ⚠️ 최초 실행 시 Windows SmartScreen 안내

처음 실행할 때 **"Windows의 PC 보호"** 경고가 나타날 수 있습니다. 이는 코드 서명이 없는 새 프로그램에 대한 기본 보안 경고이며, 한 번만 허용하면 이후에는 나타나지 않습니다.

1. **"추가 정보"** 클릭
2. **"실행"** 버튼 클릭

### 요구사항

- Windows 10/11
- 바이낸스 선물 계정 + API Key (선물 거래 권한 필요)

### 주의사항

- 투자 손실의 책임은 사용자에게 있습니다
- 반드시 테스트넷에서 충분히 테스트 후 실거래 전환하세요
- 소액으로 시작하는 것을 권장합니다

---

## v1.1.0 - First Official Release

First official release of the Binance Futures (USDT-M) auto trading bot.

### Key Features

- **Composite Signal-based Auto Entry/Exit** — Momentum(50%), Volume(30%), MTF EMA(20%) scoring
- **Multi-layer Exit System** — SL, partial TP, ATR trailing, breakeven, signal decay, time stop
- **ATR-based Position Sizing** + Kelly Criterion
- **Regime Detection** — Auto-classify trend_up / trend_down / chop with directional bias
- **Maker-first Exit** — Limit order exit for fee reduction (5bps offset, 4 attempts)
- **Per-symbol Reentry Cooldown** — Prevents consecutive entries on the same symbol (120s)
- **Chop Regime Position Limit** — Max 2 symbols during sideways markets
- **Auto Tuner** — Automatic parameter optimization per regime
- **Dark Theme GUI** — Korean/English support, real-time monitoring
- **Auto Update Check** — Notification when a new version is available

### Binance API Key Guide

#### 📌 Testnet (Paper Trading)

1. Go to https://testnet.binancefuture.com
2. Log in with your GitHub account
3. Click **"API Key"** at the bottom
4. Click **"Generate HMAC_SHA256 Key"**
5. Copy API Key and Secret Key, then register as environment variables
6. ⚠️ Testnet keys may expire periodically and require regeneration

#### 📌 Live Trading (Mainnet)

1. Log in to https://www.binance.com
2. Click profile icon (top right) → **"API Management"**
3. Enter API Key label (e.g., `AutoBot`) → Click **"Create API"**
4. Complete email/2FA verification
5. Copy API Key and Secret Key, then register as environment variables
6. **API Restriction Settings (Required):**
   - ✅ **Enable Futures** — Check "Futures"
   - ✅ **Restrict IP Access** — Allow only your IP (recommended)
   - ❌ **Disable Withdrawal** — Withdrawal permission must be OFF
7. ⚠️ Secret Key is shown only once at creation — copy it immediately

### Installation

1. Download `BinanceAutoBot.exe`

2. Register your API Keys as Windows environment variables (keys are never entered directly in the program for security)

   **How to set environment variables:**
   1. Press `Win + S` to open search
   2. Search **"environment variables"** → Click **"Edit the system environment variables"**
   3. Click **"Environment Variables"** button
   4. Under **User variables**, click **"New"**
   5. Add the following variables:

   | Variable Name | Variable Value | Purpose |
   |---------------|---------------|---------|
   | `TESTNET_API_KEY` | Testnet API Key | Paper trading |
   | `TESTNET_API_SECRET` | Testnet API Secret | Paper trading |
   | `BINANCE_API_KEY` | Live API Key | Live trading |
   | `BINANCE_API_SECRET` | Live API Secret | Live trading |

   6. Click **OK** to save all
   7. ⚠️ If the program is already running, **restart** is required

3. Run and verify environment variable status in the Settings tab (Set / Not set indicator)
4. Testing on Testnet first is recommended

### ⚠️ Windows SmartScreen Notice

A **"Windows protected your PC"** warning may appear on first run. This is a standard security warning for new unsigned programs. You only need to allow it once.

1. Click **"More info"**
2. Click **"Run anyway"**

### Requirements

- Windows 10/11
- Binance Futures account + API Key (Futures trading permission required)

### Disclaimer

- Users are solely responsible for any trading losses
- Always test thoroughly on Testnet before switching to live trading
- Starting with a small amount is recommended
