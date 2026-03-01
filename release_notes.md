# 🤖 Binance Auto Trading Bot v1.1.1

> ⚠️ **Windows SmartScreen 경고**: 처음 실행 시 "Windows가 PC를 보호했습니다" 메시지가 나올 수 있습니다.
> `추가 정보` → `실행` 을 클릭하면 정상 실행됩니다. (코드 서명 미적용으로 인한 정상 경고)

> 📄 **English instructions are included below.**

---

## 📦 다운로드

| 파일 | 설명 |
|------|------|
| `BinanceAutoBot_KR.exe` | 한국어 기본 실행 파일 |
| `BinanceAutoBot_EN.exe` | English default executable |

---

## 🔧 v1.1.1 변경사항

### 횡보장(Chop) 방어 강화
- 횡보 레짐 진입 임계값 상향 (0.72 → 0.85)
- 횡보 구간 포지션 사이즈 50% 자동 축소
- 횡보 시 최대 동시 오픈 심볼 2개 제한

### 레퍼럴 코드 보안
- 레퍼럴 코드 읽기 전용 고정 (변조 방지)
- Base64 난독화 + SHA-256 무결성 검증
- 변조 감지 시 비공식 버전 경고 다이얼로그

### 프리미엄 구독 결제
- 월간($9.99) / 연간($99, 17% 할인) 구독 버튼 추가
- Lemon Squeezy 결제 연동

### 환경설정 UX 개선
- 환경변수 설정 방법 단계별 가이드 추가 (최상단 배치)
- 변수명 테이블 (색상 구분, 시인성 강화)
- 필수 동의 완료 후 환경변수 미설정 시 자동 안내
- 하단 DEFAULT/SAVE 버튼 잘림 문제 수정 (스크롤 구조 개선)

---

## 🔑 환경변수 설정 (최초 1회)

API 키는 프로그램 안에서 입력하지 않습니다. Windows 환경 변수에 등록해 주세요.

### 설정 순서
1. Windows 검색 → `환경 변수` → **시스템 환경 변수 편집**
2. **환경 변수(N)...** 버튼 클릭
3. **사용자 변수** → **새로 만들기(N)...**
4. 아래 4개 변수를 하나씩 추가

| 환경 | 변수명 | 값 |
|------|--------|-----|
| 테스트넷 | `TESTNET_API_KEY` | 발급받은 테스트넷 API Key |
| 테스트넷 | `TESTNET_API_SECRET` | 발급받은 테스트넷 Secret Key |
| 실거래 | `BINANCE_API_KEY` | 발급받은 실거래 API Key |
| 실거래 | `BINANCE_API_SECRET` | 발급받은 실거래 Secret Key |

5. 모든 창에서 **확인** 클릭
6. 프로그램 **종료 후 다시 실행**

### 바이낸스 API 키 발급
- **테스트넷**: https://testnet.binancefuture.com → API Management → Create API
- **실거래**: https://www.binance.com → API Management → Create API
  - ✅ Enable Futures 체크 필수
  - ✅ IP 제한 권장 (보안)

---

---

# 🤖 Binance Auto Trading Bot v1.1.1 — English

> ⚠️ **Windows SmartScreen Warning**: On first launch, you may see "Windows protected your PC".
> Click `More info` → `Run anyway`. This is a normal warning due to unsigned code.

---

## 📦 Downloads

| File | Description |
|------|-------------|
| `BinanceAutoBot_KR.exe` | Korean default executable |
| `BinanceAutoBot_EN.exe` | English default executable |

---

## 🔧 v1.1.1 Changes

### Chop Regime Defense
- Entry threshold raised (0.72 → 0.85) during sideways markets
- Position size auto-reduced by 50% in chop regime
- Max 2 simultaneous open symbols during chop

### Referral Code Security
- Referral code set to read-only (tamper-proof)
- Base64 obfuscation + SHA-256 integrity check
- Warning dialog on tamper detection

### Premium Subscription
- Monthly ($9.99) / Yearly ($99, 17% off) subscription buttons
- Lemon Squeezy payment integration

### Settings UX Improvements
- Step-by-step environment variable guide (top of settings)
- Variable name table with color-coded columns
- Auto-redirect to settings after consent if env vars missing
- Fixed bottom buttons being cut off (scrollable layout)

---

## 🔑 Environment Variables (One-time Setup)

API keys are NOT entered inside the program. Register them as Windows environment variables.

### Steps
1. Windows Search → `Environment Variables` → **Edit system environment variables**
2. Click **Environment Variables...** button
3. Under **User variables** → click **New...**
4. Add these 4 variables one by one:

| Env | Variable Name | Value |
|-----|--------------|-------|
| Testnet | `TESTNET_API_KEY` | Your Testnet API Key |
| Testnet | `TESTNET_API_SECRET` | Your Testnet Secret Key |
| Live | `BINANCE_API_KEY` | Your Live API Key |
| Live | `BINANCE_API_SECRET` | Your Live Secret Key |

5. Click **OK** on all dialogs
6. **Close and restart** the program

### Binance API Key Guide
- **Testnet**: https://testnet.binancefuture.com → API Management → Create API
- **Live**: https://www.binance.com → API Management → Create API
  - ✅ Enable Futures (required)
  - ✅ IP restriction recommended (security)
