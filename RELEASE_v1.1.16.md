# 🤖 바이낸스 선물 자동매매 봇 v1.1.16 (Beta)

> ⚠️ **베타 버전입니다.** 테스트넷에서 충분한 검증 후 실거래에 사용하세요.
> 초기 릴리즈(v1.1.0) 이후 16차 패치를 통해 엔진 안정성과 GUI를 대폭 개선했습니다.
>
> ⚠️ **This is a Beta release.** Please test thoroughly on Testnet before live trading.
> Includes 16 patches for engine stability and GUI improvements since v1.1.0 initial release.

---

## 📥 다운로드 방법 (How to Download)

> **이 페이지 맨 아래 ⬇️ `Assets` 섹션**에서 파일을 다운로드합니다.
> Scroll down to the **Assets** section at the bottom of this page ⬇️

| 파일명 (File) | 설명 (Description) |
|---|---|
| `BinanceAutoBot.exe` | 한국어 기본 버전 (Korean) |
| `BinanceAutoBot_EN.exe` | 영어 기본 버전 (English) |

> 💡 **Assets가 안 보이면?** → `Assets` 옆의 ▶ 화살표를 클릭해서 펼쳐주세요.
> 💡 **Can't see Assets?** → Click the ▶ arrow next to `Assets` to expand.

---

## ⚠️ 다운로드 시 주의사항 (Download Notice)

### 🔴 Edge 브라우저 사용 금지

| | |
|---|---|
| ❌ **Microsoft Edge** | 다운로드 차단됨 — **사용하지 마세요** |
| ✅ **Google Chrome** | 정상 다운로드 — **Chrome을 사용하세요** |

코드 서명 인증서(Code Signing Certificate)가 아직 적용되지 않아 Edge에서 차단됩니다.
파일은 안전하며, 향후 업데이트에서 인증서를 적용할 예정입니다.

> ❌ **Do NOT use Microsoft Edge** — download will be blocked.
> ✅ **Use Google Chrome** to download.
>
> A Code Signing Certificate has not yet been applied. The file is safe.

---

## 🖥️ 설치 및 실행 (Installation)

### 1단계: 실행하기

다운로드한 `.exe` 파일을 더블클릭하여 실행합니다.

### 2단계: Windows SmartScreen 경고 허용

처음 실행 시 아래와 같은 경고가 나타납니다. **정상이며 한 번만 허용하면 됩니다.**

<img width="531" height="497" alt="윈도우 PC 보호 1" src="https://github.com/user-attachments/assets/9ad9d14f-19a5-491e-8898-9d9a5e552619" />
<img width="531" height="495" alt="윈도우 PC 보호 2" src="https://github.com/user-attachments/assets/f437d868-826a-4620-bf66-5028b9b5cd10" />

```
"Windows의 PC 보호" 또는 "Windows protected your PC"
```

**👉 해결 방법:**
1. **"추가 정보"** (More info) 클릭
2. **"실행"** (Run anyway) 버튼 클릭

### 3단계: API Key 환경변수 등록

> API Key를 아직 발급받지 않았다면, 아래 **🔑 바이낸스 API Key 발급 방법**을 먼저 참고하세요.

이 프로그램은 **보안을 위해 API Key를 프로그램 안에 직접 입력하지 않습니다.**
대신 Windows 환경변수에 등록합니다.

**설정 방법:**

1. 키보드에서 `Win + S` 키를 누릅니다
2. **"환경 변수"** 를 검색합니다
3. **"시스템 환경 변수 편집"** 을 클릭합니다

<img width="848" height="317" alt="시스템 환경변수편집 1" src="https://github.com/user-attachments/assets/640df7fc-afbd-4553-aca5-d62eb49dcf1d" />

4. 열린 창에서 **"환경 변수(N)"** 버튼을 클릭합니다
5. **사용자 변수** 영역에서 **"새로 만들기(N)"** 를 클릭합니다
6. 아래 4개 변수를 하나씩 추가합니다:

| 변수 이름 | 입력할 값 | 용도 |
|---|---|---|
| `TESTNET_API_KEY` | 테스트넷 API Key | 모의거래용 |
| `TESTNET_API_SECRET` | 테스트넷 API Secret | 모의거래용 |
| `BINANCE_API_KEY` | 실거래 API Key | 실거래용 |
| `BINANCE_API_SECRET` | 실거래 API Secret | 실거래용 |

7. **확인** 을 눌러 모두 저장합니다
8. ⚠️ 프로그램이 이미 실행 중이었다면 **프로그램을 껐다 다시 켜주세요**

### 4단계: 테스트넷으로 먼저 테스트

실행 후 **설정 탭**에서 환경변수 상태를 확인합니다 (설정됨 ✅ / 미설정 ❌ 표시).

<img width="1102" height="742" alt="시스템 환경변수편집 8" src="https://github.com/user-attachments/assets/bdb466e5-ad5a-4e2d-99bb-34ffafb684d3" />

**반드시 테스트넷(모의거래)으로 먼저 테스트한 후 실거래로 전환하세요.**

---

## 🔑 바이낸스 API Key 발급 방법

### 📌 테스트넷 (모의거래) — 먼저 이것부터 하세요!

1. https://testnet.binancefuture.com 에 접속합니다
2. **GitHub 계정**으로 로그인합니다 (GitHub 계정이 없으면 먼저 만드세요)
3. 페이지 상단의 프로필을 클릭한 뒤 **"Demo Trading API"** 메뉴를 클릭합니다

<img width="323" height="312" alt="Demo Trading API 키 버튼" src="https://github.com/user-attachments/assets/7f03a85f-c5a5-4861-954c-898e51106936" />

4. 우측 상단의 **"Create API"** 를 클릭합니다
5. 아래 이미지와 같이 **System generated**가 선택된 상태에서 **Next**를 눌러주세요

<img width="436" height="446" alt="Create API 2" src="https://github.com/user-attachments/assets/a301e97d-f17e-4ab0-8911-22a6c3efa184" />

6. API 이름을 입력합니다 (예: `AutoBot`)
7. 표시되는 API Key와 Secret Key를 복사합니다
8. 위 3단계에서 설명한 환경변수에 등록합니다

> ⚠️ 테스트넷 키는 주기적으로 만료됩니다. 작동이 안 되면 다시 발급해주세요.

### 📌 실거래 (메인넷) — 테스트넷 확인 후 진행하세요

> ⚠️ 계정에 잔고가 있어야 API를 바로 사용할 수 있습니다.

1. https://www.binance.com 에 로그인합니다
2. 우측 상단 프로필 아이콘 → Account 메뉴의 **"API 관리"** 를 클릭합니다

<img width="383" height="638" alt="API 키 발급 1" src="https://github.com/user-attachments/assets/50048aa5-3d80-4f4c-96b5-04dd37b7d24b" />

3. 우측 상단의 **"Create API"** 를 클릭합니다
4. **System generated**가 선택된 상태에서 **Next**를 눌러주세요

<img width="456" height="478" alt="API 키 발급 3" src="https://github.com/user-attachments/assets/950549a4-0395-4ff5-8108-847d2624396c" />

5. API Key 라벨을 입력합니다 (예: `AutoBot`)
6. 이메일/2FA 인증을 완료합니다
7. 키 발급 후 선물 거래를 위해 설정을 수정해야 합니다. **"Edit restrictions"** 를 클릭합니다
8. **IP access restrictions**에서 **"Restrict access to trusted IPs only"** 를 체크하고 본인 PC의 IP 주소를 입력합니다
9. 내 IP 확인: https://url.kr/web_tools/ip/ 에서 확인 후 복사·붙여넣기
10. **API restrictions**에서 **"Enable Futures"** 를 활성화한 후 저장합니다

> 🔴 **절대로 "Enable Withdrawals"는 활성화하지 마세요!** API를 통한 출금을 허용하는 옵션이므로 반드시 꺼두어야 합니다.

11. API Key와 Secret Key를 복사하여 환경변수에 등록합니다

**⚠️ API 보안 설정 (반드시 확인하세요!):**

| 설정 | 상태 | 이유 |
|---|---|---|
| ✅ 선물 거래 | **활성화** | 봇이 선물 거래를 실행하기 위해 필요 |
| ✅ IP 접근 제한 | **본인 IP만 허용** | 다른 사람이 API를 사용하지 못하도록 |
| ❌ 출금 | **반드시 비활성화** | 출금 권한은 절대 켜지 마세요! |

> ⚠️ **Secret Key는 생성 시 딱 한 번만 표시됩니다!** 반드시 그 자리에서 바로 복사하세요.

---

## ✨ 주요 기능 (Key Features)

- **복합 시그널 자동매매** — 모멘텀(50%) + 거래량(30%) + MTF EMA(20%) 복합 스코어링
- **다층 청산 시스템** — 손절 / 부분익절 / ATR 트레일링 / 브레이크이븐
- **ATR 포지션 사이징 + Kelly Criterion** — 변동성 기반 자동 포지션 크기 조절
- **레짐 감지** — 상승추세 / 하락추세 / 횡보장 자동 판별
- **Maker 우선 청산** — 수수료 절감을 위한 지정가 청산
- **심볼별 재진입 쿨다운** — 동일 심볼 연속 진입 방지 (120초)
- **스파이크 가드** — 급변동 시 즉시 시장가 청산으로 청산 방어
- **Feature Flags 시스템** — 모듈별 ON/OFF 제어, 안전한 기능 토글
- **KPI 대시보드** — 실시간 성과 추적 (메이커 비율, TCA, 24h PnL 등)
- **체결 품질 분석** — 슬리피지, 체결률, 수수료 효율 실시간 모니터링
- **다크 테마 GUI** — 한국어/영어 지원, 실시간 모니터링, KPI 카드
- **자동 업데이트 확인** — 새 버전 출시 시 알림

---

## 🔧 패치 노트 (Patch Notes, v1.1.0 이후)

- **PATCH-16**: 시스템 간소화 — 손실 유발 레이어 비활성화, 안정적 코어 로직만 유지
- **PATCH-15**: PnL 이중 수수료 차감 버그 수정, Auto-Tuner 파라미터 잔존값 정리
- **PATCH-14**: 전체 코드베이스 하드코딩 일괄 수정 (20+건), 설정 기반 구조로 전환
- **PATCH-13**: 트레이딩 엔진 손실 원인 전면 분석 및 수정 (Auto-Tuner 오버라이드 버그, 킬스위치 버그, 포지션 수 제한 완화 3→10개)
- **PATCH-12**: 횡보(chop) 레짐 진입 임계값 강화, 레퍼럴 보호 적용
- **UI 개선**: 정보탭 리디자인, 사이드바/스크롤바 다크 테마 적용, 환경설정 탭 UX 개선, 환경변수 가이드 시인성 강화

---

## 📋 요구사항 (Requirements)

- Windows 10 / 11
- 바이낸스 선물 계정 + API Key (선물 거래 권한 필요)

---

## ⚠️ 주의사항 (Disclaimer)

- **투자 손실의 책임은 전적으로 사용자에게 있습니다**
- 반드시 **테스트넷에서 충분히 테스트** 후 실거래로 전환하세요
- **소액으로 시작**하는 것을 강력히 권장합니다
- 이 소프트웨어는 투자 조언이 아닙니다

---

# 🤖 Binance Futures Auto Trading Bot v1.1.16 (Beta)

> ⚠️ **This is a Beta release.** Please test thoroughly on Testnet before live trading.
> Includes 16 patches for engine stability, bug fixes, and GUI improvements since v1.1.0.

---

## 📥 How to Download

> Scroll down to the **Assets** section at the very bottom of this page ⬇️

| File | Description |
|---|---|
| `BinanceAutoBot.exe` | Korean default version |
| `BinanceAutoBot_EN.exe` | English default version |

> 💡 **Can't see Assets?** → Click the ▶ arrow next to `Assets` to expand the list.

---

## ⚠️ Download Notice

### 🔴 Do NOT use Edge browser

| | |
|---|---|
| ❌ **Microsoft Edge** | Download will be BLOCKED |
| ✅ **Google Chrome** | Works normally — **use Chrome** |

A Code Signing Certificate has not yet been applied, causing Edge to block the download.
The file is safe, and a certificate will be added in a future update.

---

## 🖥️ Installation

### Step 1: Run the program

Double-click the downloaded `.exe` file.

### Step 2: Allow Windows SmartScreen

A warning will appear on first run. **This is normal and you only need to allow it once.**

<img width="531" height="497" alt="Windows PC Protection 1" src="https://github.com/user-attachments/assets/9ad9d14f-19a5-491e-8898-9d9a5e552619" />
<img width="531" height="495" alt="Windows PC Protection 2" src="https://github.com/user-attachments/assets/f437d868-826a-4620-bf66-5028b9b5cd10" />

```
"Windows protected your PC"
```

**👉 How to fix:**
1. Click **"More info"**
2. Click **"Run anyway"**

### Step 3: Register API Keys as Environment Variables

> If you haven't obtained your API keys yet, see the **🔑 How to Get Binance API Keys** section below.

For security, this program **never asks you to enter API keys directly inside the app.**
Instead, you register them as Windows environment variables.

**How to set up:**

1. Press `Win + S` on your keyboard
2. Search for **"environment variables"**
3. Click **"Edit the system environment variables"**

<img width="848" height="317" alt="Edit system environment variables" src="https://github.com/user-attachments/assets/640df7fc-afbd-4553-aca5-d62eb49dcf1d" />

4. Click the **"Environment Variables"** button
5. Under **User variables**, click **"New"**
6. Add these 4 variables one by one:

| Variable Name | Value | Purpose |
|---|---|---|
| `TESTNET_API_KEY` | Your Testnet API Key | Paper trading |
| `TESTNET_API_SECRET` | Your Testnet API Secret | Paper trading |
| `BINANCE_API_KEY` | Your Live API Key | Live trading |
| `BINANCE_API_SECRET` | Your Live API Secret | Live trading |

7. Click **OK** to save all
8. ⚠️ If the program is already running, **restart it**

### Step 4: Test on Testnet first

After launching, check the **Settings tab** for environment variable status (Set ✅ / Not set ❌).

<img width="1102" height="742" alt="Settings tab environment variable status" src="https://github.com/user-attachments/assets/bdb466e5-ad5a-4e2d-99bb-34ffafb684d3" />

**Always test on Testnet (paper trading) before switching to live trading.**

---

## 🔑 How to Get Binance API Keys

### 📌 Testnet (Paper Trading) — Do this first!

1. Go to https://testnet.binancefuture.com
2. Log in with your **GitHub account** (create one first if you don't have one)
3. Click your profile at the top, then click **"Demo Trading API"**

<img width="323" height="312" alt="Demo Trading API button" src="https://github.com/user-attachments/assets/7f03a85f-c5a5-4861-954c-898e51106936" />

4. Click **"Create API"** in the top right
5. Make sure **System generated** is selected, then click **Next**

<img width="436" height="446" alt="Create API dialog" src="https://github.com/user-attachments/assets/a301e97d-f17e-4ab0-8911-22a6c3efa184" />

6. Enter a name for the API (e.g., `AutoBot`)
7. Copy the displayed API Key and Secret Key
8. Register them as environment variables (see Step 3 above)

> ⚠️ Testnet keys expire periodically. If it stops working, generate new keys.

### 📌 Live Trading (Mainnet) — Only after confirming Testnet works

> ⚠️ Your account must have a balance before you can use the API.

1. Log in to https://www.binance.com
2. Click the profile icon (top right) → Under Account, click **"API Management"**

<img width="383" height="638" alt="API Management menu" src="https://github.com/user-attachments/assets/50048aa5-3d80-4f4c-96b5-04dd37b7d24b" />

3. Click **"Create API"** in the top right
4. Make sure **System generated** is selected, then click **Next**

<img width="456" height="478" alt="Create API dialog" src="https://github.com/user-attachments/assets/950549a4-0395-4ff5-8108-847d2624396c" />

5. Enter an API key label (e.g., `AutoBot`)
6. Complete email/2FA verification
7. After the key is created, you need to configure it for futures trading. Click **"Edit restrictions"**
8. Under **IP access restrictions**, check **"Restrict access to trusted IPs only"** and enter your PC's IP address
9. To find your IP: visit https://url.kr/web_tools/ip/, then copy and paste it
10. Under **API restrictions**, enable **"Enable Futures"** and save

> 🔴 **NEVER enable "Enable Withdrawals"!** This option allows withdrawals via API and must always remain OFF.

11. Copy the API Key and Secret Key, then register them as environment variables

**⚠️ API Security Settings (MUST configure!):**

| Setting | Status | Why |
|---|---|---|
| ✅ Futures Trading | **Enable** | Required for the bot to trade |
| ✅ IP Access Restriction | **Your IP only** | Prevents unauthorized access |
| ❌ Withdrawal | **MUST be disabled** | Never enable withdrawal! |

> ⚠️ **Secret Key is shown only ONCE at creation!** Copy it immediately.

---

## ✨ Key Features

- **Composite Signal Auto Trading** — Momentum(50%) + Volume(30%) + MTF EMA(20%) scoring
- **Multi-layer Exit System** — SL / Partial TP / ATR Trailing / Breakeven
- **ATR Position Sizing + Kelly Criterion** — Volatility-based automatic position sizing
- **Regime Detection** — Auto-classify trend up / trend down / chop
- **Maker-first Exit** — Limit order exit for fee reduction
- **Per-symbol Reentry Cooldown** — Prevents consecutive entries (120s)
- **Spike Guard** — Instant market-order exit on sudden adverse moves to prevent liquidation
- **Feature Flags System** — Per-module ON/OFF control for safe feature toggling
- **KPI Dashboard** — Real-time performance tracking (maker rate, TCA, 24h PnL, etc.)
- **Execution Quality Analytics** — Slippage, fill rate, fee efficiency monitoring
- **Dark Theme GUI** — Korean/English support, real-time monitoring, KPI cards
- **Auto Update Check** — Notification when new version is available

---

## 🔧 Patch Notes (since v1.1.0 initial release)

- **PATCH-16**: System simplification — disabled loss-inducing layers, stable core logic only
- **PATCH-15**: Fixed PnL double fee deduction bug, cleaned up residual Auto-Tuner parameters
- **PATCH-14**: Eliminated 20+ hardcoded values across entire codebase, moved to config-based architecture
- **PATCH-13**: Full trading engine loss analysis & fix (Auto-Tuner override bug, kill-switch bug, position limit relaxed 3→10)
- **PATCH-12**: Strengthened chop regime entry thresholds, referral protection applied
- **UI Improvements**: Info tab redesign, sidebar/scrollbar dark theme, settings tab UX improvements, environment variable guide visibility enhanced

---

## 📋 Requirements

- Windows 10 / 11
- Binance Futures account + API Key (Futures trading permission required)

---

## ⚠️ Disclaimer

- **Users are solely responsible for any trading losses**
- Always **test thoroughly on Testnet** before switching to live trading
- **Starting with a small amount** is strongly recommended
- This software is not financial advice
