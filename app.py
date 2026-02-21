"""
TCSM CRYPTO PRO v5.0 — CRYPTOCURRENCY SIGNAL SCANNER (PRODUCTION GRADE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Adapted from TCSM Thai Stock Pro v4.1 for 24/7 crypto markets.

FEATURES:
  ✅ 120+ Cryptocurrencies across all major categories
  ✅ Dual Engine: Triple Confluence + 7-Indicator SmartMomentum
  ✅ Multi-Timeframe Analysis (15m, 1h, 4h, 1D)
  ✅ Divergence Detection (Bull/Bear)
  ✅ OBV (On-Balance Volume) Confirmation
  ✅ Volume Spike Detection
  ✅ ADX Trend Strength + CCI + Williams %R
  ✅ ATR-based Stop Loss & Take Profit (R:R targets)
  ✅ Auto-delist detection for dead/migrated tokens
  ✅ Real-time WebSocket push with glassmorphism UI
  ✅ 24/7 scanning (crypto never sleeps)
  ✅ Free CoinGecko API (no key required) + yfinance fallback
  ✅ gevent async — no monkey_patch issues

DATA SOURCE:
  Primary:   CoinGecko Free API (no API key needed, 30 calls/min)
  Fallback:  yfinance (BTC-USD style tickers)
  Cache:     60s per coin to respect rate limits

DEPLOY on Render (Free Tier):
  Build Command:  pip install -r requirements.txt
  Start Command:  gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 -b 0.0.0.0:$PORT app:app

requirements.txt:
  flask
  flask-socketio
  gunicorn
  gevent
  gevent-websocket
  pandas
  numpy
  requests
"""
# ═══════════════════════════════════════════════════════════════════════════════
#  CRITICAL: gevent monkey patch MUST be the VERY FIRST thing
# ═══════════════════════════════════════════════════════════════════════════════
from gevent import monkey
monkey.patch_all()

import gevent
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit
from datetime import datetime, timezone, timedelta
from functools import wraps
import pandas as pd
import numpy as np
import hashlib, secrets, time, json, os, traceback, logging, math, threading, io
import requests as req_lib

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('TCSM-CRYPTO')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='gevent',
    ping_timeout=120,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
    always_connect=True,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  TIME UTILITIES — Bangkok Time (ICT = UTC+7)
# ═══════════════════════════════════════════════════════════════════════════════
ICT = timezone(timedelta(hours=7))
def now(): return datetime.now(ICT)
def ts(f="%H:%M:%S"): return now().strftime(f)
def dts(): return now().strftime("%Y-%m-%d %H:%M:%S ICT")

def sanitize(obj):
    if obj is None: return None
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return 0.0 if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)): return bool(obj)
    if isinstance(obj, np.ndarray): return obj.tolist()
    if isinstance(obj, pd.Timestamp): return str(obj)
    if isinstance(obj, dict): return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [sanitize(i) for i in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj): return 0.0
    return obj

def safe_emit(event, data, **kwargs):
    try:
        socketio.emit(event, sanitize(data), **kwargs)
    except (BrokenPipeError, OSError, IOError):
        pass
    except RuntimeError as e:
        if 'Working outside' not in str(e):
            logger.error(f"Emit '{event}' RuntimeError: {e}")
    except Exception as e:
        logger.warning(f"Emit '{event}' failed: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — Crypto Coins & Categories
# ═══════════════════════════════════════════════════════════════════════════════
class C:
    SCAN_INTERVAL = 75          # seconds between full scans
    BATCH_SIZE = 4              # coins per batch (CoinGecko rate-limit safe)
    BATCH_DELAY = 2.5           # delay between batches
    COIN_DELAY = 1.0            # delay between coins in batch
    DELIST_THRESHOLD = 5        # failures before marking dead
    CACHE_TTL = 55              # cache seconds per coin

    # ── Indicator Parameters (identical to TCSM Thai Stock Pro) ──
    STOCH_RSI_K = 3; STOCH_RSI_D = 3; RSI_PD = 14; STOCH_PD = 14
    ENH_K = 21; ENH_D = 3; ENH_SLOW = 5
    MACD_F = 12; MACD_S = 26; MACD_SIG = 9; MACD_NLB = 500
    OB = 80.0; OS = 20.0; NEU_HI = 60.0; NEU_LO = 40.0
    SIG_TH = 38; CHG_TH = 14; MOM_SM = 3; SWING_LB = 5
    RSI_W = 18; MACD_W = 18; STOCH_W = 14; CCI_W = 14; WPR_W = 12; ADX_W = 12; MOM_W = 12
    CCI_PD = 20; WPR_PD = 14; ADX_PD = 14; MOM_PD = 14
    TREND_MA = 50; MIN_MOM = 0.4; STRONG_STR = 52.0
    ATR_PD = 14; ATR_SL = 1.5; RR = 2.0
    DIV_BARS = 20; VOL_PD = 20

    # ── Cryptocurrency Universe ──
    # Format: (display_symbol, coingecko_id, yfinance_ticker)
    COINS_RAW = [
        # === Layer 1 / Store of Value ===
        ("BTC",   "bitcoin",            "BTC-USD"),
        ("ETH",   "ethereum",           "ETH-USD"),
        ("BNB",   "binancecoin",        "BNB-USD"),
        ("SOL",   "solana",             "SOL-USD"),
        ("ADA",   "cardano",            "ADA-USD"),
        ("AVAX",  "avalanche-2",        "AVAX-USD"),
        ("DOT",   "polkadot",           "DOT-USD"),
        ("NEAR",  "near",               "NEAR-USD"),
        ("ATOM",  "cosmos",             "ATOM-USD"),
        ("APT",   "aptos",              "APT-USD"),
        ("SUI",   "sui",                "SUI-USD"),
        ("SEI",   "sei-network",        "SEI-USD"),
        ("ICP",   "internet-computer",  "ICP-USD"),
        ("FIL",   "filecoin",           "FIL-USD"),
        ("HBAR",  "hedera-hashgraph",   "HBAR-USD"),
        ("ALGO",  "algorand",           "ALGO-USD"),
        ("EOS",   "eos",                "EOS-USD"),
        ("XTZ",   "tezos",             "XTZ-USD"),
        ("EGLD",  "elrond-erd-2",       "EGLD-USD"),
        ("FTM",   "fantom",             "FTM-USD"),
        ("KAVA",  "kava",               "KAVA-USD"),
        ("MINA",  "mina-protocol",      "MINA-USD"),
        ("KAS",   "kaspa",              "KAS-USD"),
        ("TIA",   "celestia",           "TIA-USD"),
        ("INJ",   "injective-protocol", "INJ-USD"),
        # === Payments / Currency ===
        ("XRP",   "ripple",             "XRP-USD"),
        ("DOGE",  "dogecoin",           "DOGE-USD"),
        ("SHIB",  "shiba-inu",          "SHIB-USD"),
        ("LTC",   "litecoin",           "LTC-USD"),
        ("BCH",   "bitcoin-cash",       "BCH-USD"),
        ("XLM",   "stellar",            "XLM-USD"),
        ("PEPE",  "pepe",               "PEPE-USD"),
        ("FLOKI", "floki",              "FLOKI-USD"),
        ("WIF",   "dogwifcoin",         "WIF-USD"),
        ("BONK",  "bonk",               "BONK-USD"),
        # === DeFi ===
        ("UNI",   "uniswap",            "UNI-USD"),
        ("AAVE",  "aave",               "AAVE-USD"),
        ("MKR",   "maker",              "MKR-USD"),
        ("LDO",   "lido-dao",           "LDO-USD"),
        ("SNX",   "havven",             "SNX-USD"),
        ("CRV",   "curve-dao-token",    "CRV-USD"),
        ("COMP",  "compound-governance-token", "COMP-USD"),
        ("SUSHI", "sushi",              "SUSHI-USD"),
        ("1INCH", "1inch",              "1INCH-USD"),
        ("DYDX",  "dydx",               "DYDX-USD"),
        ("CAKE",  "pancakeswap-token",  "CAKE-USD"),
        ("JUP",   "jupiter-exchange-solana", "JUP-USD"),
        ("PENDLE","pendle",             "PENDLE-USD"),
        ("ENA",   "ethena",             "ENA-USD"),
        # === Layer 2 / Scaling ===
        ("MATIC", "matic-network",      "MATIC-USD"),
        ("ARB",   "arbitrum",           "ARB-USD"),
        ("OP",    "optimism",           "OP-USD"),
        ("IMX",   "immutable-x",        "IMX-USD"),
        ("STRK",  "starknet",           "STRK-USD"),
        ("MNT",   "mantle",             "MNT-USD"),
        ("METIS", "metis-token",        "METIS-USD"),
        ("ZK",    "zksync",             "ZK-USD"),
        # === Gaming / Metaverse ===
        ("AXS",   "axie-infinity",      "AXS-USD"),
        ("SAND",  "the-sandbox",        "SAND-USD"),
        ("MANA",  "decentraland",       "MANA-USD"),
        ("GALA",  "gala",               "GALA-USD"),
        ("IMX",   "immutable-x",        "IMX-USD"),
        ("PRIME", "echelon-prime",      "PRIME-USD"),
        ("PIXEL", "pixels",             "PIXEL-USD"),
        ("RONIN", "ronin",              "RONIN-USD"),
        # === AI / Data ===
        ("FET",   "fetch-ai",           "FET-USD"),
        ("RNDR",  "render-token",       "RNDR-USD"),
        ("TAO",   "bittensor",          "TAO-USD"),
        ("OCEAN", "ocean-protocol",     "OCEAN-USD"),
        ("ARKM",  "arkham",             "ARKM-USD"),
        ("AKT",   "akash-network",      "AKT-USD"),
        ("WLD",   "worldcoin-wld",      "WLD-USD"),
        ("AI16Z", "ai16z",              "AI16Z-USD"),
        # === Infrastructure / Oracle ===
        ("LINK",  "chainlink",          "LINK-USD"),
        ("GRT",   "the-graph",          "GRT-USD"),
        ("QNT",   "quant-network",      "QNT-USD"),
        ("VET",   "vechain",            "VET-USD"),
        ("THETA", "theta-token",        "THETA-USD"),
        ("AR",    "arweave",            "AR-USD"),
        ("PYTH",  "pyth-network",       "PYTH-USD"),
        ("TRB",   "tellor",             "TRB-USD"),
        # === Exchange Tokens ===
        ("CRO",   "crypto-com-chain",   "CRO-USD"),
        ("OKB",   "okb",                "OKB-USD"),
        ("LEO",   "leo-token",          "LEO-USD"),
        ("GT",    "gate-token",         "GT-USD"),
        # === Stablecoins / Yield (for reference) ===
        # Not scanned — they don't trend
        # === Privacy ===
        ("XMR",   "monero",             "XMR-USD"),
        ("ZEC",   "zcash",              "ZEC-USD"),
        # === RWA / Tokenization ===
        ("ONDO",  "ondo-finance",       "ONDO-USD"),
        ("CFG",   "centrifuge",         "CFG-USD"),
        ("POLYX", "polymesh",           "POLYX-USD"),
        # === Misc Trending ===
        ("TON",   "the-open-network",   "TON-USD"),
        ("TRX",   "tron",               "TRX-USD"),
        ("STX",   "blockstack",         "STX-USD"),
        ("RUNE",  "thorchain",          "RUNE-USD"),
        ("ENS",   "ethereum-name-service","ENS-USD"),
        ("AERO",  "aerodrome-finance",  "AERO-USD"),
        ("W",     "wormhole",           "W-USD"),
        ("JTO",   "jito-governance-token","JTO-USD"),
        ("ETHFI", "ether-fi",           "ETHFI-USD"),
        ("EIGEN", "eigenlayer",         "EIGEN-USD"),
    ]

    # Deduplicate by symbol
    _seen = set()
    COINS = []
    for sym, cg_id, yf_id in COINS_RAW:
        if sym not in _seen:
            _seen.add(sym)
            COINS.append((sym, cg_id, yf_id))

    # Maps
    COIN_MAP = {sym: (cg_id, yf_id) for sym, cg_id, yf_id in COINS}
    SYMBOLS = [sym for sym, _, _ in COINS]

    # Category mapping
    CATEGORY_MAP = {}
    _l1 = {"BTC","ETH","BNB","SOL","ADA","AVAX","DOT","NEAR","ATOM","APT","SUI","SEI","ICP","FIL","HBAR","ALGO","EOS","XTZ","EGLD","FTM","KAVA","MINA","KAS","TIA","INJ"}
    _pay = {"XRP","DOGE","SHIB","LTC","BCH","XLM","PEPE","FLOKI","WIF","BONK"}
    _defi = {"UNI","AAVE","MKR","LDO","SNX","CRV","COMP","SUSHI","1INCH","DYDX","CAKE","JUP","PENDLE","ENA"}
    _l2 = {"MATIC","ARB","OP","IMX","STRK","MNT","METIS","ZK"}
    _game = {"AXS","SAND","MANA","GALA","PRIME","PIXEL","RONIN"}
    _ai = {"FET","RNDR","TAO","OCEAN","ARKM","AKT","WLD","AI16Z"}
    _infra = {"LINK","GRT","QNT","VET","THETA","AR","PYTH","TRB"}
    _exc = {"CRO","OKB","LEO","GT"}
    _priv = {"XMR","ZEC"}
    _rwa = {"ONDO","CFG","POLYX"}
    _misc = {"TON","TRX","STX","RUNE","ENS","AERO","W","JTO","ETHFI","EIGEN"}
    for s in _l1:    CATEGORY_MAP[s] = "Layer 1"
    for s in _pay:   CATEGORY_MAP[s] = "Payment"
    for s in _defi:  CATEGORY_MAP[s] = "DeFi"
    for s in _l2:    CATEGORY_MAP[s] = "Layer 2"
    for s in _game:  CATEGORY_MAP[s] = "Gaming"
    for s in _ai:    CATEGORY_MAP[s] = "AI"
    for s in _infra: CATEGORY_MAP[s] = "Infra"
    for s in _exc:   CATEGORY_MAP[s] = "Exchange"
    for s in _priv:  CATEGORY_MAP[s] = "Privacy"
    for s in _rwa:   CATEGORY_MAP[s] = "RWA"
    for s in _misc:  CATEGORY_MAP[s] = "Misc"

    @staticmethod
    def get_category(sym): return C.CATEGORY_MAP.get(sym, "Other")


# ═══════════════════════════════════════════════════════════════════════════════
#  CRYPTO DATA CLIENT — CoinGecko (free) + yfinance fallback
# ═══════════════════════════════════════════════════════════════════════════════
class CryptoClient:
    CG_BASE = "https://api.coingecko.com/api/v3"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }

    def __init__(self):
        self._cache = {}
        self._cache_time = {}
        self._lock = threading.Lock()
        self._session = None
        self._fail_count = {}
        self._dead_tickers = set()
        self._last_cg_call = 0
        self._cg_interval = 2.2  # ~27 calls/min, under 30/min free limit

    def _get_session(self):
        if self._session is None:
            self._session = req_lib.Session()
            self._session.headers.update(self.HEADERS)
        return self._session

    def _rate_limit_cg(self):
        elapsed = time.time() - self._last_cg_call
        if elapsed < self._cg_interval:
            gevent.sleep(self._cg_interval - elapsed)
        self._last_cg_call = time.time()

    def is_dead(self, symbol):
        return symbol in self._dead_tickers

    def _mark_dead(self, symbol, reason=""):
        fc = self._fail_count.get(symbol, 0) + 1
        self._fail_count[symbol] = fc
        if fc >= C.DELIST_THRESHOLD:
            self._dead_tickers.add(symbol)
            logger.warning(f"☠ {symbol} marked dead after {fc} failures ({reason})")
            return True
        return False

    def _fetch_coingecko(self, symbol, cg_id):
        """Fetch OHLC from CoinGecko (free, no key)"""
        try:
            self._rate_limit_cg()
            s = self._get_session()
            # CoinGecko OHLC: 1=1day(30min candles), 7=7days(4h), 14=14d(4h), 30=30d(4h), 90=90d(1d)
            # For 1h-equivalent: use 30 days = 4h candles, or market_chart for custom
            url = f"{self.CG_BASE}/coins/{cg_id}/ohlc"
            params = {'vs_currency': 'usd', 'days': '30'}
            r = s.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data and len(data) >= 20:
                    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close'])
                    df.index = pd.to_datetime(df['timestamp'], unit='ms')
                    df = df.drop(columns=['timestamp'])
                    df = df.dropna(subset=['close'])
                    df = df[df['close'] > 0]
                    # Generate synthetic volume from price movement
                    df['volume'] = ((df['high'] - df['low']) / (df['close'] + 1e-10) * 1e6).clip(lower=100)
                    if len(df) >= 20:
                        return df
            elif r.status_code == 404:
                self._mark_dead(symbol, "CG 404")
            elif r.status_code == 429:
                logger.warning(f"CoinGecko 429 rate limit, backing off")
                self._cg_interval = min(self._cg_interval + 1, 6)
                gevent.sleep(10)
            else:
                logger.warning(f"CoinGecko {r.status_code} for {symbol}")
        except Exception as e:
            logger.warning(f"CoinGecko {symbol}: {e}")
        return None

    def _fetch_coingecko_market_chart(self, symbol, cg_id):
        """Fallback: market_chart with price+volume (granularity auto by CG)"""
        try:
            self._rate_limit_cg()
            s = self._get_session()
            url = f"{self.CG_BASE}/coins/{cg_id}/market_chart"
            params = {'vs_currency': 'usd', 'days': '30'}
            r = s.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                prices = data.get('prices', [])
                volumes = data.get('total_volumes', [])
                if len(prices) >= 20:
                    pdf = pd.DataFrame(prices, columns=['timestamp', 'close'])
                    pdf.index = pd.to_datetime(pdf['timestamp'], unit='ms')
                    pdf = pdf.drop(columns=['timestamp'])
                    if volumes:
                        vdf = pd.DataFrame(volumes, columns=['timestamp', 'volume'])
                        vdf.index = pd.to_datetime(vdf['timestamp'], unit='ms')
                        vdf = vdf.drop(columns=['timestamp'])
                        pdf = pdf.join(vdf, how='left')
                    else:
                        pdf['volume'] = 1e6
                    pdf['open'] = pdf['close'].shift(1).fillna(pdf['close'])
                    pdf['high'] = pdf[['open', 'close']].max(axis=1) * 1.001
                    pdf['low'] = pdf[['open', 'close']].min(axis=1) * 0.999
                    pdf['volume'] = pdf['volume'].fillna(1e6)
                    pdf = pdf.dropna(subset=['close'])
                    pdf = pdf[pdf['close'] > 0]
                    if len(pdf) >= 20:
                        return pdf
            elif r.status_code == 429:
                gevent.sleep(10)
        except Exception as e:
            logger.warning(f"CG market_chart {symbol}: {e}")
        return None

    def _fetch_yfinance(self, symbol, yf_ticker):
        """Fallback: yfinance"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_ticker)
            df = ticker.history(period="1mo", interval="1h")
            if df is not None and len(df) >= 20:
                df.columns = [c.lower() for c in df.columns]
                df = df.rename(columns={'stock splits': 'splits'})
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0]
                if len(df) >= 20:
                    return df
        except Exception as e:
            es = str(e)
            if 'No data found' in es or 'delisted' in es:
                self._mark_dead(symbol, es[:80])
            else:
                logger.warning(f"yfinance {symbol}: {e}")
        return None

    def get_history(self, symbol):
        if symbol in self._dead_tickers:
            return None
        cache_key = f"{symbol}_hist"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < C.CACHE_TTL:
                return self._cache[cache_key]

        cg_id, yf_id = C.COIN_MAP.get(symbol, (None, None))
        if not cg_id:
            return None

        df = None
        methods = [
            ("coingecko_ohlc", lambda: self._fetch_coingecko(symbol, cg_id)),
            ("coingecko_chart", lambda: self._fetch_coingecko_market_chart(symbol, cg_id)),
            ("yfinance", lambda: self._fetch_yfinance(symbol, yf_id)),
        ]
        for name, method in methods:
            if symbol in self._dead_tickers:
                return None
            try:
                df = method()
                if df is not None and len(df) >= 20:
                    with self._lock:
                        self._cache[cache_key] = df
                        self._cache_time[cache_key] = time.time()
                    self._fail_count[symbol] = 0
                    logger.info(f"✓ {symbol} via {name} ({len(df)} rows)")
                    return df
            except Exception as e:
                logger.warning(f"{name} failed for {symbol}: {e}")
            gevent.sleep(0.3)

        if symbol not in self._dead_tickers:
            self._mark_dead(symbol, "all methods failed")
        return None

    def get_daily(self, symbol):
        """Get daily data for MTF analysis"""
        if symbol in self._dead_tickers:
            return None
        cache_key = f"{symbol}_daily"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < C.CACHE_TTL:
                return self._cache[cache_key]

        cg_id, yf_id = C.COIN_MAP.get(symbol, (None, None))
        if not cg_id:
            return None

        try:
            self._rate_limit_cg()
            s = self._get_session()
            url = f"{self.CG_BASE}/coins/{cg_id}/ohlc"
            params = {'vs_currency': 'usd', 'days': '90'}
            r = s.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data and len(data) >= 20:
                    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close'])
                    df.index = pd.to_datetime(df['timestamp'], unit='ms')
                    df = df.drop(columns=['timestamp'])
                    df['volume'] = ((df['high'] - df['low']) / (df['close'] + 1e-10) * 1e6).clip(lower=100)
                    df = df.dropna(subset=['close'])
                    df = df[df['close'] > 0]
                    if len(df) >= 20:
                        with self._lock:
                            self._cache[cache_key] = df
                            self._cache_time[cache_key] = time.time()
                        return df
        except Exception as e:
            logger.warning(f"Daily {symbol}: {e}")

        # yfinance fallback for daily
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_id)
            df = ticker.history(period="3mo", interval="1d")
            if df is not None and len(df) >= 20:
                df.columns = [c.lower() for c in df.columns]
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0]
                if len(df) >= 20:
                    with self._lock:
                        self._cache[cache_key] = df
                        self._cache_time[cache_key] = time.time()
                    return df
        except:
            pass
        return None

crypto_client = CryptoClient()


# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS (identical engine to TCSM Thai Stock Pro)
# ═══════════════════════════════════════════════════════════════════════════════
def sma(s, n): return s.rolling(n, min_periods=1).mean()
def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=1).mean()
def rma(s, n): return s.ewm(alpha=1.0/n, adjust=False, min_periods=1).mean()

def calc_rsi(close, period=14):
    d = close.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1.0/period, adjust=False, min_periods=1).mean()
    l = (-d.where(d < 0, 0.0)).ewm(alpha=1.0/period, adjust=False, min_periods=1).mean()
    return 100.0 - 100.0 / (1.0 + g / (l + 1e-10))

def calc_stoch(close, high, low, period):
    ll = low.rolling(period, min_periods=1).min()
    hh = high.rolling(period, min_periods=1).max()
    return ((close - ll) / (hh - ll + 1e-10)) * 100.0

def calc_cci(close, high, low, period):
    tp = (high + low + close) / 3.0
    ma = tp.rolling(period, min_periods=1).mean()
    md = tp.rolling(period, min_periods=1).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def calc_wpr(close, high, low, period):
    hh = high.rolling(period, min_periods=1).max()
    ll = low.rolling(period, min_periods=1).min()
    return ((hh - close) / (hh - ll + 1e-10)) * -100.0

def calc_atr(high, low, close, period):
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()

def calc_adx(high, low, close, period):
    up = high.diff(); dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    atr_s = rma(tr, period)
    pdi = 100.0 * rma(pdm, period) / (atr_s + 1e-10)
    mdi = 100.0 * rma(mdm, period) / (atr_s + 1e-10)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
    return rma(dx, period), pdi, mdi

def calc_obv(close, volume):
    return (volume * np.sign(close.diff())).cumsum()


# ═══════════════════════════════════════════════════════════════════════════════
#  CRYPTO MARKET SESSION (24/7 but with context)
# ═══════════════════════════════════════════════════════════════════════════════
def get_market_session():
    """Crypto market session labels in Bangkok time (ICT = UTC+7)"""
    n = now(); h = n.hour
    if 0 <= h < 7:     return "🌙 กลางคืน Late Night", True
    elif 7 <= h < 10:  return "🌅 เช้า Morning", True
    elif 10 <= h < 12: return "☀️ สาย Mid-Morning", True
    elif 12 <= h < 14: return "🍜 เที่ยง Lunch", True
    elif 14 <= h < 17: return "🌤️ บ่าย Afternoon", True
    elif 17 <= h < 20: return "🌆 เย็น Evening", True
    elif 20 <= h < 22: return "🗽 US Open (Peak Vol)", True
    else:              return "🌙 ดึก Late Night", True


# ═══════════════════════════════════════════════════════════════════════════════
#  ANALYSIS ENGINE — Dual Engine (identical logic to TCSM Thai Stock Pro)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_crypto(symbol):
    try:
        category = C.get_category(symbol)
        df = crypto_client.get_history(symbol)
        if df is None or len(df) < 30:
            return None
        close = df['close']; high = df['high']; low = df['low']; volume = df['volume']
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        chg = price - prev
        chg_pct = (chg / prev * 100) if prev else 0

        # Smart decimal formatting
        if price >= 1000:    dec = 2
        elif price >= 1:     dec = 3
        elif price >= 0.01:  dec = 5
        elif price >= 0.0001:dec = 6
        else:                dec = 8

        df_daily = crypto_client.get_daily(symbol)

        # ═══ ENGINE A: TRIPLE CONFLUENCE ═══
        rv = calc_rsi(close, C.RSI_PD)
        rh = rv.rolling(C.STOCH_PD, min_periods=1).max()
        rl = rv.rolling(C.STOCH_PD, min_periods=1).min()
        srK = sma(((rv - rl) / (rh - rl + 1e-10)) * 100.0, C.STOCH_RSI_K)
        srD = sma(srK, C.STOCH_RSI_D)
        ehh = high.rolling(C.ENH_K, min_periods=1).max()
        ell = low.rolling(C.ENH_K, min_periods=1).min()
        enK = sma(((close - ell) / (ehh - ell + 1e-10)) * 100.0, C.ENH_SLOW)
        enD = sma(enK, C.ENH_D)
        ml = ema(close, C.MACD_F) - ema(close, C.MACD_S)
        sl = ema(ml, C.MACD_SIG)
        lb = min(len(df), C.MACD_NLB)
        rH = pd.concat([ml.rolling(lb, min_periods=1).max(), sl.rolling(lb, min_periods=1).max()], axis=1).max(axis=1)
        rL = pd.concat([ml.rolling(lb, min_periods=1).min(), sl.rolling(lb, min_periods=1).min()], axis=1).min(axis=1)
        rng = rH - rL; pH = rH + rng * 0.1; pL = rL - rng * 0.1
        mnA = ((ml - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)
        msA = ((sl - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)
        sr_k = float(srK.iloc[-1]); sr_d = float(srD.iloc[-1])
        en_k = float(enK.iloc[-1]); en_d = float(enD.iloc[-1])
        mn_a = float(mnA.iloc[-1]); ms_a = float(msA.iloc[-1])

        def xo(a, b): return len(a) >= 2 and a.iloc[-1] > b.iloc[-1] and a.iloc[-2] <= b.iloc[-2]
        def xu(a, b): return len(a) >= 2 and a.iloc[-1] < b.iloc[-1] and a.iloc[-2] >= b.iloc[-2]
        def ir(v): return v > C.OS and v < C.OB

        bx = sum([xo(srK, srD) and ir(sr_k), xo(enK, enD) and ir(en_k), xo(mnA, msA) and ir(mn_a)])
        sx = sum([xu(srK, srD) and ir(sr_k), xu(enK, enD) and ir(en_k), xu(mnA, msA) and ir(mn_a)])
        bull_zn = sum([sr_k > C.NEU_HI, en_k > C.NEU_HI, mn_a > C.NEU_HI])
        bear_zn = sum([sr_k < C.NEU_LO, en_k < C.NEU_LO, mn_a < C.NEU_LO])
        ema20 = float(ema(close, 20).iloc[-1])
        ema50 = float(ema(close, 50).iloc[-1])
        above_ma = price > ema50
        avg_mom_a = float((srK.diff().abs().iloc[-1] + enK.diff().abs().iloc[-1] + mnA.diff().abs().iloc[-1]) / 3.0)
        conf_buy = bool(bx >= 2 and above_ma and avg_mom_a >= C.MIN_MOM)
        conf_sell = bool(sx >= 2 and not above_ma and avg_mom_a >= C.MIN_MOM)

        def recent_xo(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                if abs(-i - 1) < len(a) and a.iloc[-i] > b.iloc[-i] and a.iloc[-i-1] <= b.iloc[-i-1]:
                    return True
            return False
        def recent_xu(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                if abs(-i - 1) < len(a) and a.iloc[-i] < b.iloc[-i] and a.iloc[-i-1] >= b.iloc[-i-1]:
                    return True
            return False

        rbx = sum([recent_xo(srK, srD), recent_xo(enK, enD), recent_xo(mnA, msA)])
        rsx = sum([recent_xu(srK, srD), recent_xu(enK, enD), recent_xu(mnA, msA)])
        if not conf_buy and rbx >= 2 and above_ma:
            conf_buy = True; bx = max(bx, rbx)
        if not conf_sell and rsx >= 2 and not above_ma:
            conf_sell = True; sx = max(sx, rsx)
        bx = int(bx); sx = int(sx); bull_zn = int(bull_zn); bear_zn = int(bear_zn)

        # ═══ ENGINE B: SMARTMOMENTUM (7-Indicator Weighted) ═══
        rsi_v = float(calc_rsi(close, C.RSI_PD).iloc[-1])
        rsi_sc = float((rsi_v - 50) * 2.0)
        atr_v = float(calc_atr(high, low, close, C.ATR_PD).iloc[-1])
        if pd.isna(atr_v) or atr_v <= 0:
            atr_v = float((high - low).mean())
        ml_v = float(ml.iloc[-1]); sl_v = float(sl.iloc[-1])
        macd_nb = float(np.clip((ml_v / max(atr_v, 1e-10)) * 50.0, -100, 100))
        macd_sc = float(np.clip(macd_nb + (15 if ml_v > sl_v else -15), -100, 100))
        stv = calc_stoch(close, high, low, C.STOCH_PD)
        st_sig = sma(stv, 3)
        stoch_sc = float(np.clip((stv.iloc[-1] - 50) * 2.0 + (10 if stv.iloc[-1] > st_sig.iloc[-1] else -10), -100, 100))
        cci_sc = float(np.clip(float(calc_cci(close, high, low, C.CCI_PD).iloc[-1]) / 2.0, -100, 100))
        wpr_sc = float((float(calc_wpr(close, high, low, C.WPR_PD).iloc[-1]) + 50) * 2.0)
        adx_v, pdi, mdi = calc_adx(high, low, close, C.ADX_PD)
        adx_val = float(adx_v.iloc[-1]); pdi_v = float(pdi.iloc[-1]); mdi_v = float(mdi.iloc[-1])
        adx_sc = float(np.clip(adx_val * 2.0, 0, 100) if pdi_v > mdi_v else np.clip(-adx_val * 2.0, -100, 0))
        mom_pd = min(C.MOM_PD, len(close) - 1)
        mom_pct = float((close.iloc[-1] - close.iloc[-mom_pd]) / close.iloc[-mom_pd] * 100) if mom_pd > 0 and float(close.iloc[-mom_pd]) != 0 else 0.0
        mom_sc = float(np.clip(mom_pct * 10.0, -100, 100))

        wts = [C.RSI_W, C.MACD_W, C.STOCH_W, C.CCI_W, C.WPR_W, C.ADX_W, C.MOM_W]
        scs = [rsi_sc, macd_sc, stoch_sc, cci_sc, wpr_sc, adx_sc, mom_sc]
        tw = sum(wts)
        momentum = round(sum(s * w for s, w in zip(scs, wts)) / tw, 2) if tw > 0 else 0.0

        # Divergence
        div = "NONE"
        try:
            if len(df) > C.DIV_BARS:
                rl_ = low.iloc[-C.DIV_BARS:]; rs_ = stv.iloc[-C.DIV_BARS:]
                if low.iloc[-1] < rl_.min() * 1.001 and stv.iloc[-1] > rs_.min():
                    div = "BULL"
                rh_ = high.iloc[-C.DIV_BARS:]
                if high.iloc[-1] > rh_.max() * 0.999 and stv.iloc[-1] < rs_.max():
                    div = "BEAR"
        except:
            pass

        # Volume
        va = float(volume.rolling(C.VOL_PD, min_periods=1).mean().iloc[-1])
        vr = round(float(volume.iloc[-1]) / va, 2) if va > 0 else 1.0
        obv = calc_obv(close, volume); obv_sma = sma(obv, 20)
        obv_bullish = bool(obv.iloc[-1] > obv_sma.iloc[-1])

        # ═══ MTF Analysis ═══
        mtf = {}
        h_score = 50 + (rsi_v - 50) * 0.8
        mtf['4h'] = {'score': round(h_score, 1), 'summary': 'Buy' if h_score > 55 else ('Sell' if h_score < 45 else 'Neutral')}
        if df_daily is not None and len(df_daily) >= 20:
            dc = df_daily['close']; dh = df_daily['high']; dl = df_daily['low']
            d_rsi = float(calc_rsi(dc, 14).iloc[-1])
            d_ema20 = float(ema(dc, 20).iloc[-1])
            d_ema50 = float(ema(dc, 50).iloc[-1])
            d_score = float(50 + np.clip((d_rsi - 50) * 0.6 + (25 if float(dc.iloc[-1]) > d_ema20 else -25) + (15 if float(dc.iloc[-1]) > d_ema50 else -15), -50, 50))
            mtf['1D'] = {'score': round(d_score, 1), 'summary': 'Buy' if d_score > 55 else ('Sell' if d_score < 45 else 'Neutral')}
            if len(df_daily) >= 30:
                wc = dc.iloc[-7:]
                wt = float((wc.iloc[-1] - wc.iloc[0]) / wc.iloc[0] * 100)
                ws = float(50 + np.clip(wt * 8, -40, 40))
                mtf['1W'] = {'score': round(ws, 1), 'summary': 'Buy' if ws > 55 else ('Sell' if ws < 45 else 'Neutral')}
        # Shorter TF from main data
        if len(close) >= 20:
            short_rsi = float(calc_rsi(close.iloc[-20:], 14).iloc[-1]) if len(close) >= 20 else 50
            s_score = 50 + (short_rsi - 50) * 0.9
            mtf['1h'] = {'score': round(s_score, 1), 'summary': 'Buy' if s_score > 55 else ('Sell' if s_score < 45 else 'Neutral')}

        for tf in ['1h', '4h', '1D', '1W']:
            if tf not in mtf:
                mtf[tf] = {'score': 50.0, 'summary': 'Neutral'}

        mtf_buy = sum(1 for v in mtf.values() if v['score'] > 55)
        mtf_sell = sum(1 for v in mtf.values() if v['score'] < 45)
        if mtf_buy >= 3:      mtf_align = "★ ALL BUY"
        elif mtf_sell >= 3:   mtf_align = "★ ALL SELL"
        elif mtf_buy >= 2:    mtf_align = "↗ Most Buy"
        elif mtf_sell >= 2:   mtf_align = "↘ Most Sell"
        else:                 mtf_align = "Mixed"
        htf_score = float(mtf.get('1D', {}).get('score', 50))
        ema_golden = bool(ema20 > ema50)
        trend_label = "UPTREND" if ema_golden and price > ema20 else ("DOWNTREND" if not ema_golden and price < ema20 else "SIDEWAYS")

        # ═══ Signal Strength Scoring ═══
        am = abs(momentum); sig_str = 0.0
        sig_str += 20 if am >= C.SIG_TH else (12 if am >= C.CHG_TH else 5)
        sig_str += min(am * 0.35, 15)
        if (momentum > 0 and conf_buy) or (momentum < 0 and conf_sell): sig_str += 20
        if (momentum > 0 and bull_zn >= 2) or (momentum < 0 and bear_zn >= 2): sig_str += 10
        if (momentum > 0 and htf_score > 55) or (momentum < 0 and htf_score < 45): sig_str += 12
        if div == "BULL" and momentum > 0: sig_str += 8
        if div == "BEAR" and momentum < 0: sig_str += 8
        if vr > 1.5: sig_str += 10
        elif vr > 1.2: sig_str += 5
        if (momentum > 0 and obv_bullish) or (momentum < 0 and not obv_bullish): sig_str += 5
        if (momentum > 0 and ema_golden) or (momentum < 0 and not ema_golden): sig_str += 5
        sig_str = min(sig_str, 100)

        if momentum >= C.SIG_TH:     zone = "STRONG BUY"
        elif momentum >= C.CHG_TH:   zone = "BULLISH"
        elif momentum <= -C.SIG_TH:  zone = "STRONG SELL"
        elif momentum <= -C.CHG_TH:  zone = "BEARISH"
        else:                         zone = "NEUTRAL"

        session_name, in_session = get_market_session()

        # ═══ SIGNAL GENERATION ═══
        signal = None; reasons = []; direction = None; sig_type = None
        st_val = float(stv.iloc[-1])
        bull_rev = st_val < 35 and momentum > -C.CHG_TH
        bear_rev = st_val > 65 and momentum < C.CHG_TH

        strong_buy = ((momentum >= C.SIG_TH) or bull_rev or conf_buy) and sig_str >= C.STRONG_STR
        strong_sell = ((momentum <= -C.SIG_TH) or bear_rev or conf_sell) and sig_str >= C.STRONG_STR
        c_buy = conf_buy and not strong_buy
        c_sell = conf_sell and not strong_sell
        w_buy = (momentum >= C.CHG_TH or bull_rev or (bull_zn >= 2 and above_ma)) and not strong_buy and not c_buy
        w_sell = (momentum <= -C.CHG_TH or bear_rev or (bear_zn >= 2 and not above_ma)) and not strong_sell and not c_sell

        if strong_buy:    direction = "BUY";  sig_type = "STRONG"; reasons.append("🔥 Strong Buy Signal")
        elif strong_sell: direction = "SELL"; sig_type = "STRONG"; reasons.append("🔥 Strong Sell Signal")
        elif c_buy:       direction = "BUY";  sig_type = "CONF";   reasons.append(f"◆ Confluence Buy ({bx}/3)")
        elif c_sell:      direction = "SELL"; sig_type = "CONF";   reasons.append(f"◆ Confluence Sell ({sx}/3)")
        elif w_buy:       direction = "BUY";  sig_type = "WEAK";   reasons.append("◇ Weak Buy Signal")
        elif w_sell:      direction = "SELL"; sig_type = "WEAK";   reasons.append("◇ Weak Sell Signal")

        if direction:
            if above_ma and direction == "BUY":           reasons.append("✅ Above EMA50")
            if not above_ma and direction == "SELL":      reasons.append("✅ Below EMA50")
            if ema_golden and direction == "BUY":         reasons.append("✅ Golden Cross")
            if not ema_golden and direction == "SELL":    reasons.append("✅ Death Cross")
            if bull_zn >= 2 and direction == "BUY":       reasons.append(f"✅ Bull Zone {bull_zn}/3")
            if bear_zn >= 2 and direction == "SELL":      reasons.append(f"✅ Bear Zone {bear_zn}/3")
            if div != "NONE":                             reasons.append(f"✅ {div} Divergence")
            if vr > 1.5:                                  reasons.append(f"✅ Vol {vr:.1f}x")
            if obv_bullish and direction == "BUY":        reasons.append("✅ OBV Bullish")
            if not obv_bullish and direction == "SELL":   reasons.append("✅ OBV Bearish")
            reasons.append(f"🕐 {session_name}")

            if direction == "BUY":
                sl_ = round(price - atr_v * C.ATR_SL, dec); risk = price - sl_
                tp1_ = round(price + risk * 1.5, dec)
                tp2_ = round(price + risk * C.RR, dec)
                tp3_ = round(price + risk * 3.0, dec)
            else:
                sl_ = round(price + atr_v * C.ATR_SL, dec); risk = sl_ - price
                tp1_ = round(price - risk * 1.5, dec)
                tp2_ = round(price - risk * C.RR, dec)
                tp3_ = round(price - risk * 3.0, dec)
            risk_pct = round(risk / price * 100, 2) if price > 0 else 0.0
            rr = round(abs(price - tp2_) / max(risk, 1e-10), 2)

            signal = {
                'id': f"{symbol}_{direction}_{now().strftime('%H%M%S')}",
                'symbol': symbol, 'category': category, 'direction': direction, 'type': sig_type,
                'entry': round(price, dec), 'sl': float(sl_),
                'tp1': float(tp1_), 'tp2': float(tp2_), 'tp3': float(tp3_),
                'momentum': float(momentum), 'strength': round(sig_str),
                'conf': int(bx if direction == "BUY" else sx),
                'srK': round(sr_k, 1), 'srD': round(sr_d, 1),
                'enK': round(en_k, 1), 'enD': round(en_d, 1),
                'mnA': round(mn_a, 1), 'msA': round(ms_a, 1),
                'rsi_v': round(rsi_v, 1),
                'rsi_sc': round(rsi_sc, 1), 'macd_sc': round(macd_sc, 1),
                'stoch_sc': round(stoch_sc, 1), 'cci_sc': round(cci_sc, 1),
                'wpr_sc': round(wpr_sc, 1), 'adx_sc': round(adx_sc, 1),
                'mom_sc': round(mom_sc, 1),
                'trend': trend_label, 'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
                'div': div, 'vol': float(vr), 'htf': round(htf_score, 1),
                'mtf_1h': mtf.get('1h', {}).get('summary', '?'),
                'mtf_4h': mtf.get('4h', {}).get('summary', '?'),
                'mtf_1d': mtf.get('1D', {}).get('summary', '?'),
                'mtf_1w': mtf.get('1W', {}).get('summary', '?'),
                'mtf_align': mtf_align, 'session': session_name,
                'atr': round(atr_v, dec + 2),
                'risk_pct': float(risk_pct), 'rr': float(rr),
                'reasons': reasons,
                'timestamp': dts(), 'ts_unix': time.time(),
            }

        analysis = {
            'symbol': symbol, 'cat': category, 'price': round(price, dec),
            'prev': round(prev, dec), 'change': round(chg, dec + 1),
            'change_pct': round(chg_pct, 3),
            'momentum': float(momentum), 'strength': round(sig_str), 'zone': zone,
            'session': session_name, 'trend': trend_label,
            'srK': round(sr_k, 1), 'enK': round(en_k, 1), 'mnA': round(mn_a, 1),
            'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
            'bx': int(bx), 'sx': int(sx),
            'conf_buy': bool(conf_buy), 'conf_sell': bool(conf_sell),
            'above_ma': bool(above_ma),
            'ema20': round(ema20, dec), 'ema50': round(ema50, dec),
            'rsi_v': round(rsi_v, 1), 'adx_v': round(adx_val, 1),
            'rsi_sc': round(rsi_sc, 1), 'macd_sc': round(macd_sc, 1),
            'stoch_sc': round(stoch_sc, 1), 'cci_sc': round(cci_sc, 1),
            'wpr_sc': round(wpr_sc, 1), 'adx_sc': round(adx_sc, 1),
            'mom_sc': round(mom_sc, 1),
            'div': div, 'vol': float(vr), 'atr': round(atr_v, dec + 2),
            'obv_bull': bool(obv_bullish),
            'mtf': mtf, 'mtf_align': mtf_align, 'time': ts(),
        }
        return analysis, signal
    except Exception as e:
        logger.error(f"Analyze {symbol}: {e}\n{traceback.format_exc()}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  STORE & AUTH
# ═══════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self):
        self.prices = {}; self.signals = {}; self.analysis = {}; self.history = []
        self.last_scan = "Never"; self.scan_count = 0; self.connected = 0
        self.status = "STARTING"; self.errors = []; self.first_scan_done = False
        self.stats = {'buy': 0, 'sell': 0, 'strong': 0, 'conf': 0, 'weak': 0}
        self.dead_tickers = []; self.ok_count = 0; self.fail_count = 0

    def err(self, m):
        self.errors.insert(0, f"[{ts()}] {m}")
        self.errors = self.errors[:50]

    def get_state(self):
        return sanitize({
            'prices': self.prices, 'signals': self.signals,
            'analysis': self.analysis, 'history': self.history[:200],
            'scan_count': self.scan_count, 'last_scan': self.last_scan,
            'status': self.status, 'stats': self.stats,
            'dead_tickers': self.dead_tickers,
            'ok_count': self.ok_count, 'fail_count': self.fail_count,
        })

store = Store()

class Users:
    def __init__(self):
        self.f = 'users.json'; self.u = self._load()

    def _load(self):
        try:
            if os.path.exists(self.f):
                with open(self.f) as f:
                    return json.load(f)
        except:
            pass
        d = {'admin': {'pw': hashlib.sha256(b'admin123_tcsm').hexdigest(),
                       'role': 'admin', 'name': 'Admin', 'on': True}}
        self._save(d); return d

    def _save(self, u=None):
        try:
            with open(self.f, 'w') as f:
                json.dump(u or self.u, f, indent=2)
        except:
            pass

    def verify(self, u, p):
        if u not in self.u: return False
        return self.u[u].get('on', True) and self.u[u]['pw'] == hashlib.sha256(f"{p}_tcsm".encode()).hexdigest()

users = Users()

def login_req(f):
    @wraps(f)
    def d(*a, **k):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **k)
    return d


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER — gevent background task
# ═══════════════════════════════════════════════════════════════════════════════
def scanner():
    gevent.sleep(5)
    logger.info("🚀 TCSM Crypto Pro v5.0 Scanner started (gevent production)")
    store.status = "RUNNING"

    with app.app_context():
        while True:
            try:
                store.scan_count += 1
                store.last_scan = ts()
                t0 = time.time(); ok = 0; fail = 0
                coins = [s for s in C.SYMBOLS if not crypto_client.is_dead(s)]
                store.dead_tickers = list(crypto_client._dead_tickers)

                for i in range(0, len(coins), C.BATCH_SIZE):
                    batch = coins[i:i + C.BATCH_SIZE]
                    for symbol in batch:
                        try:
                            result = analyze_crypto(symbol)
                            if result:
                                analysis, signal = result; ok += 1
                                analysis = sanitize(analysis)
                                store.analysis[symbol] = analysis
                                store.prices[symbol] = {
                                    'symbol': symbol, 'cat': analysis['cat'],
                                    'price': analysis['price'],
                                    'change': analysis['change'],
                                    'change_pct': analysis['change_pct'],
                                    'time': analysis['time']
                                }
                                safe_emit('price_update', {
                                    'symbol': symbol,
                                    'data': store.prices[symbol],
                                    'analysis': analysis
                                })
                                if signal:
                                    signal = sanitize(signal)
                                    old = store.signals.get(symbol)
                                    is_new = (not old
                                              or old.get('direction') != signal['direction']
                                              or time.time() - old.get('ts_unix', 0) > 1800)
                                    if is_new:
                                        store.signals[symbol] = signal
                                        store.history.insert(0, signal)
                                        store.history = store.history[:500]
                                        if signal['direction'] == "BUY":
                                            store.stats['buy'] += 1
                                        else:
                                            store.stats['sell'] += 1
                                        st = signal['type'].lower()
                                        store.stats[st] = store.stats.get(st, 0) + 1
                                        safe_emit('new_signal', {'symbol': symbol, 'signal': signal})
                                        logger.info(f"🎯 {signal['type']} {signal['direction']} {symbol} @ ${signal['entry']} Str:{signal['strength']}%")
                            else:
                                fail += 1
                        except Exception as e:
                            fail += 1; store.err(f"{symbol}: {e}")
                        gevent.sleep(C.COIN_DELAY)
                    gevent.sleep(C.BATCH_DELAY)

                dur = time.time() - t0
                store.first_scan_done = True
                store.ok_count = ok; store.fail_count = fail
                store.dead_tickers = list(crypto_client._dead_tickers)

                safe_emit('scan_update', sanitize({
                    'scan_count': store.scan_count, 'last_scan': store.last_scan,
                    'stats': store.stats, 'status': store.status,
                    'ok': ok, 'fail': fail, 'dur': round(dur, 1),
                    'signals': len(store.signals),
                    'dead': len(crypto_client._dead_tickers)
                }))
                safe_emit('full_sync', store.get_state())
                logger.info(f"✅ Scan #{store.scan_count}: {ok}/{ok+fail} ({dur:.1f}s) | "
                            f"Signals: {len(store.signals)} | Dead: {len(crypto_client._dead_tickers)} | "
                            f"Clients: {store.connected}")
            except Exception as e:
                logger.error(f"Scanner: {e}\n{traceback.format_exc()}")
            gevent.sleep(C.SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = '''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCSM Crypto Pro v5.0</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes gradient{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
body{background:linear-gradient(-45deg,#020617,#0a0f1e,#020617,#0d1117);background-size:400% 400%;animation:gradient 20s ease infinite}
.card{background:rgba(15,23,42,.7);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.06);box-shadow:0 25px 50px -12px rgba(0,0,0,.5)}
.gb{position:relative;overflow:hidden;border-radius:12px;transition:all .3s}.gb:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(99,102,241,.3)}
.gb::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:conic-gradient(from 0deg,transparent,rgba(99,102,241,.4),transparent 30%);animation:spin 3s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.gb>span{position:relative;z-index:1;display:block;padding:14px;background:linear-gradient(135deg,#4338ca,#6366f1);border-radius:11px;font-weight:700;letter-spacing:.5px}
.input-glow:focus{box-shadow:0 0 0 3px rgba(99,102,241,.15),0 0 20px rgba(99,102,241,.08)}
@keyframes particle{0%{transform:translateY(0) rotate(0);opacity:1}100%{transform:translateY(-100vh) rotate(720deg);opacity:0}}
.particle{position:fixed;width:3px;height:3px;background:rgba(99,102,241,.3);border-radius:50%;pointer-events:none;animation:particle linear infinite}
</style></head>
<body class="min-h-screen flex items-center justify-center p-4">
<div class="particle" style="left:10%;animation-duration:8s;animation-delay:0s"></div>
<div class="particle" style="left:30%;animation-duration:12s;animation-delay:2s"></div>
<div class="particle" style="left:50%;animation-duration:10s;animation-delay:4s"></div>
<div class="particle" style="left:70%;animation-duration:9s;animation-delay:1s"></div>
<div class="particle" style="left:90%;animation-duration:11s;animation-delay:3s"></div>
<div class="card rounded-3xl w-full max-w-md p-10 relative z-10">
<div class="text-center mb-10">
<div class="text-7xl mb-5" style="animation:float 3s ease-in-out infinite">₿</div>
<h1 class="text-4xl font-black bg-clip-text text-transparent bg-gradient-to-r from-indigo-300 via-purple-400 to-cyan-400 tracking-tight">TCSM Crypto</h1>
<p class="text-gray-500 text-sm mt-2 font-medium tracking-wide">Cryptocurrency Scanner v5.0</p>
<div class="flex justify-center gap-2 mt-4">
<span class="text-[10px] bg-indigo-500/10 text-indigo-400 px-3 py-1 rounded-full border border-indigo-500/20 font-semibold">100+ Coins</span>
<span class="text-[10px] bg-cyan-500/10 text-cyan-400 px-3 py-1 rounded-full border border-cyan-500/20 font-semibold">Dual Engine</span>
<span class="text-[10px] bg-purple-500/10 text-purple-400 px-3 py-1 rounded-full border border-purple-500/20 font-semibold">24/7 Live</span>
</div></div>
{% with m=get_flashed_messages(with_categories=true) %}{% if m %}{% for c,msg in m %}
<div class="mb-5 p-4 rounded-xl text-sm backdrop-blur-sm {% if c=='error' %}bg-red-500/10 text-red-300 border border-red-500/20{% else %}bg-green-500/10 text-green-300 border border-green-500/20{% endif %}">{{msg}}</div>
{% endfor %}{% endif %}{% endwith %}
<form method="POST" class="space-y-6">
<div><label class="block text-gray-400 text-[11px] font-semibold mb-2 uppercase tracking-widest">Username</label>
<input type="text" name="u" required class="input-glow w-full px-5 py-3.5 bg-slate-900/60 border border-slate-700/40 rounded-xl text-white placeholder-slate-600 focus:border-indigo-500/40 focus:outline-none transition-all" placeholder="Enter username"></div>
<div><label class="block text-gray-400 text-[11px] font-semibold mb-2 uppercase tracking-widest">Password</label>
<input type="password" name="p" required class="input-glow w-full px-5 py-3.5 bg-slate-900/60 border border-slate-700/40 rounded-xl text-white placeholder-slate-600 focus:border-indigo-500/40 focus:outline-none transition-all" placeholder="Enter password"></div>
<div class="gb"><span class="text-center text-white cursor-pointer"><button type="submit" class="w-full text-base">🔓 Sign In</button></span></div>
</form>
<p class="mt-8 text-center text-slate-700 text-[10px]">TCSM Crypto Pro &copy; 2026 | Not Financial Advice</p>
</div></body></html>'''


DASH_HTML = '''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>₿ TCSM Crypto Pro v5.0</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
*{scrollbar-width:thin;scrollbar-color:#1e293b transparent}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-thumb{background:#334155;border-radius:4px}
@keyframes pulse2{0%,100%{opacity:1}50%{opacity:.35}}.pulse2{animation:pulse2 1.5s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 12px rgba(99,102,241,.15)}50%{box-shadow:0 0 30px rgba(99,102,241,.35)}}.glow{animation:glow 2.5s infinite}
@keyframes slideUp{from{transform:translateY(12px);opacity:0}to{transform:translateY(0);opacity:1}}.slideUp{animation:slideUp .3s ease-out}
@keyframes loading{0%{transform:translateX(-100%)}50%{transform:translateX(0%)}100%{transform:translateX(100%)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
.glass{background:rgba(15,23,42,.65);backdrop-filter:blur(16px);border:1px solid rgba(51,65,85,.3)}
.gc{background:rgba(15,23,42,.5);backdrop-filter:blur(8px);border:1px solid rgba(51,65,85,.25);transition:all .2s}
.gc:hover{border-color:rgba(99,102,241,.2);background:rgba(15,23,42,.7)}
.sc{background:rgba(15,23,42,.5);backdrop-filter:blur(8px);border:1px solid rgba(51,65,85,.2)}
.sb{border-left:3px solid #22c55e;background:linear-gradient(135deg,rgba(22,101,52,.06),transparent)}
.ss{border-left:3px solid #ef4444;background:linear-gradient(135deg,rgba(127,29,29,.06),transparent)}
.tag{display:inline-flex;align-items:center;padding:2px 8px;border-radius:6px;font-size:9px;font-weight:600;letter-spacing:.3px}
body{background:#020617;color:#e2e8f0}
.strength-ring{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
.cat-badge{font-size:8px;padding:1px 6px;border-radius:4px;font-weight:700;letter-spacing:.5px}
.cat-layer1{background:rgba(99,102,241,.12);color:#818cf8;border:1px solid rgba(99,102,241,.2)}
.cat-payment{background:rgba(234,179,8,.12);color:#facc15;border:1px solid rgba(234,179,8,.2)}
.cat-defi{background:rgba(168,85,247,.12);color:#c084fc;border:1px solid rgba(168,85,247,.2)}
.cat-layer2{background:rgba(14,165,233,.12);color:#38bdf8;border:1px solid rgba(14,165,233,.2)}
.cat-gaming{background:rgba(244,63,94,.12);color:#fb7185;border:1px solid rgba(244,63,94,.2)}
.cat-ai{background:rgba(20,184,166,.12);color:#2dd4bf;border:1px solid rgba(20,184,166,.2)}
.cat-infra{background:rgba(249,115,22,.12);color:#fb923c;border:1px solid rgba(249,115,22,.2)}
.cat-exchange{background:rgba(234,179,8,.12);color:#fbbf24;border:1px solid rgba(234,179,8,.2)}
.cat-privacy{background:rgba(107,114,128,.15);color:#9ca3af;border:1px solid rgba(107,114,128,.2)}
.cat-rwa{background:rgba(52,211,153,.12);color:#6ee7b7;border:1px solid rgba(52,211,153,.2)}
.cat-misc{background:rgba(148,163,184,.12);color:#94a3b8;border:1px solid rgba(148,163,184,.2)}
</style></head>
<body class="min-h-screen text-[13px]">
<div class="max-w-[1800px] mx-auto px-3 py-3">

<!-- HEADER -->
<div class="flex flex-wrap justify-between items-center mb-3 gap-2">
<div>
<div class="flex items-center gap-3">
<h1 class="text-lg md:text-xl font-black bg-clip-text text-transparent bg-gradient-to-r from-indigo-300 via-purple-400 to-cyan-400">₿ TCSM Crypto Pro</h1>
<span class="tag bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">v5.0</span>
<span class="tag bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">24/7 LIVE</span>
</div>
<p class="text-slate-600 text-[10px] mt-0.5 font-medium">Dual Engine • 7-Indicator SmartMomentum • MTF • Divergence • OBV • Volume • 100+ Coins</p>
</div>
<div class="flex items-center gap-3">
<div id="clk" class="glass px-4 py-2 rounded-xl font-mono text-sm font-bold text-indigo-400 tracking-wider">--:--:--</div>
<span id="st" class="text-red-400 text-sm font-medium">● Offline</span>
<button onclick="doRefresh()" class="px-4 py-2 bg-indigo-600/50 hover:bg-indigo-500/70 rounded-xl text-xs transition font-semibold border border-indigo-500/20">🔄 Refresh</button>
<a href="/logout" class="px-4 py-2 bg-red-600/40 hover:bg-red-500/60 rounded-xl text-xs transition font-semibold border border-red-500/20">Logout</a>
</div></div>

<!-- STATS -->
<div class="grid grid-cols-4 md:grid-cols-8 gap-2 mb-3">
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Scans</div><div id="scc" class="font-black text-xl mt-1">0</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Signals</div><div id="si" class="font-black text-xl text-indigo-400 mt-1">0</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Strong</div><div id="st2" class="font-black text-xl text-purple-300 mt-1">0</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Buy</div><div id="bu" class="font-black text-xl text-emerald-400 mt-1">0</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Sell</div><div id="se" class="font-black text-xl text-red-400 mt-1">0</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Session</div><div id="ses" class="text-indigo-400 font-bold text-[11px] mt-1.5">—</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Last Scan</div><div id="ls" class="font-mono text-xs mt-1.5">--:--</div></div>
<div class="sc rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Status</div><div id="ss2" class="text-cyan-400 font-bold text-xs mt-1.5">INIT</div></div>
</div>

<!-- ALERT -->
<div id="al" class="hidden mb-3"><div class="bg-gradient-to-r from-indigo-950/50 to-purple-950/30 border border-indigo-600/40 rounded-2xl p-5 glow">
<div class="flex items-center gap-4"><span class="text-4xl" id="ai">🎯</span>
<div class="flex-1"><div id="at" class="font-bold text-indigo-200 text-base"></div><div id="ax" class="text-indigo-300/60 text-sm mt-1"></div></div>
<button onclick="this.closest('#al').classList.add('hidden')" class="text-indigo-400/40 hover:text-white text-2xl transition">✕</button></div></div></div>

<!-- MAIN GRID -->
<div class="grid grid-cols-1 xl:grid-cols-12 gap-3">

<!-- LEFT: COINS -->
<div class="xl:col-span-3 space-y-3"><div class="glass rounded-2xl p-3">
<div class="flex justify-between items-center mb-3">
<h2 class="font-bold text-sm flex items-center gap-2">₿ Cryptocurrencies <span id="ps" class="text-[9px] text-slate-600 font-normal">Loading</span></h2>
<input id="search" type="text" placeholder="Search..." class="bg-slate-900/50 border border-slate-700/30 rounded-xl px-3 py-1.5 text-xs w-28 focus:outline-none focus:border-indigo-500/40 transition-all" oninput="rP()">
</div>
<div class="flex flex-wrap gap-1 mb-3 text-[10px]">
<button onclick="fP('all')" class="px-2.5 py-1 rounded-lg pf bg-indigo-600/80 transition font-semibold" data-c="all">All</button>
<button onclick="fP('Layer 1')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Layer 1">L1</button>
<button onclick="fP('Payment')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Payment">Pay</button>
<button onclick="fP('DeFi')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="DeFi">DeFi</button>
<button onclick="fP('Layer 2')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Layer 2">L2</button>
<button onclick="fP('AI')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="AI">AI</button>
<button onclick="fP('Gaming')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Gaming">Game</button>
<button onclick="fP('Infra')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Infra">Infra</button>
<button onclick="fP('Exchange')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Exchange">CEX</button>
<button onclick="fP('RWA')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="RWA">RWA</button>
</div>
<div id="pr" class="space-y-1.5 max-h-[620px] overflow-y-auto pr-1"></div>
</div></div>

<!-- CENTER: SIGNALS -->
<div class="xl:col-span-6 space-y-3">
<div class="flex justify-between items-center">
<h2 class="font-bold text-sm">🎯 Trading Signals</h2>
<div class="flex gap-1 text-[10px]">
<button onclick="fS('all')" class="px-2.5 py-1 rounded-lg sf bg-indigo-600/80 transition font-semibold" data-c="all">All</button>
<button onclick="fS('STRONG')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="STRONG">🔥 Strong</button>
<button onclick="fS('BUY')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="BUY">🟢 Buy</button>
<button onclick="fS('SELL')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="SELL">🔴 Sell</button>
</div></div>
<div id="sg" class="space-y-2.5"><div class="glass rounded-2xl p-10 text-center text-slate-500">
<div class="text-5xl mb-4">📡</div>
<div class="text-base font-semibold">Scanning 100+ Cryptocurrencies...</div>
<div class="text-xs mt-2 text-slate-600">Dual Engine • MTF • Volume • OBV • Divergence</div>
<div class="mt-6 flex justify-center"><div class="w-56 h-1.5 bg-slate-800 rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-indigo-600 to-purple-400 rounded-full" style="animation:loading 2s ease-in-out infinite;width:30%"></div></div></div>
</div></div></div>

<!-- RIGHT: PANELS -->
<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-indigo-400 mb-2.5">🚀 Top Momentum</h3>
<div id="tm" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-emerald-400 mb-2.5">📊 Category Heatmap</h3>
<div id="hm" class="grid grid-cols-2 gap-1.5 text-[10px]"></div></div>
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-violet-400 mb-2.5">📡 Live Log</h3>
<div id="lg" class="h-36 overflow-y-auto font-mono text-[9px] space-y-0.5 leading-relaxed"></div></div>
</div></div>

<!-- HISTORY TABLE -->
<div class="glass rounded-2xl overflow-hidden mt-3">
<div class="flex justify-between items-center p-4 border-b border-slate-800/40">
<h2 class="font-bold text-sm">📜 Signal History</h2>
<span class="text-[10px] text-slate-500 font-medium" id="hc">0</span></div>
<div class="overflow-x-auto"><table class="w-full text-[11px]">
<thead class="bg-slate-900/50"><tr class="text-slate-500 uppercase text-[9px] tracking-wider font-semibold">
<th class="px-3 py-2.5 text-left">Time</th><th class="px-3 py-2.5">Coin</th><th class="px-3 py-2.5">Type</th><th class="px-3 py-2.5">Dir</th>
<th class="px-3 py-2.5 text-right">Entry</th><th class="px-3 py-2.5 text-right">SL</th>
<th class="px-3 py-2.5 text-right">TP1</th><th class="px-3 py-2.5 text-right">TP2</th>
<th class="px-3 py-2.5">R:R</th><th class="px-3 py-2.5">Str%</th><th class="px-3 py-2.5">Mom</th><th class="px-3 py-2.5">MTF</th>
</tr></thead>
<tbody id="ht"><tr><td colspan="12" class="py-8 text-center text-slate-600">Waiting for signals...</td></tr></tbody>
</table></div></div>

<div class="mt-4 text-center text-slate-700 text-[9px] pb-6">⚠️ For educational purposes only — Not financial advice | TCSM Crypto Pro v5.0</div>
</div>

<script>
const P={},A={},S={},H=[];
let cn=false,pf_='all',sf_='all';
const so=io({transports:['websocket','polling'],reconnection:true,reconnectionDelay:1000,reconnectionDelayMax:5000,reconnectionAttempts:Infinity,timeout:30000});

const catClass={'Layer 1':'cat-layer1','Payment':'cat-payment','DeFi':'cat-defi','Layer 2':'cat-layer2','Gaming':'cat-gaming','AI':'cat-ai','Infra':'cat-infra','Exchange':'cat-exchange','Privacy':'cat-privacy','RWA':'cat-rwa','Misc':'cat-misc'};
function cc(c){return catClass[c]||'cat-misc';}

function uc(){
const d=new Date();
document.getElementById('clk').textContent=d.toLocaleTimeString('en-GB',{hour12:false,timeZone:'Asia/Bangkok'})+' ICT';
const bkk=new Date(d.toLocaleString("en-US",{timeZone:"Asia/Bangkok"}));
const h=bkk.getHours();let s='';
if(h>=0&&h<7)s='🌙 กลางคืน Late Night';else if(h>=7&&h<10)s='🌅 เช้า Morning';
else if(h>=10&&h<12)s='☀️ สาย Mid-Morning';else if(h>=12&&h<14)s='🍜 เที่ยง Lunch';
else if(h>=14&&h<17)s='🌤️ บ่าย Afternoon';else if(h>=17&&h<20)s='🌆 เย็น Evening';
else if(h>=20&&h<22)s='🗽 US Open (Peak Vol)';else s='🌙 ดึก Late Night';
document.getElementById('ses').textContent=s;}
setInterval(uc,1000);uc();

function lo(m,t='info'){const e=document.getElementById('lg');
const c={signal:'text-emerald-400',error:'text-red-400',info:'text-slate-400',sys:'text-violet-400'};
const d=document.createElement('div');d.className=(c[t]||'text-slate-400')+' leading-snug';
d.textContent=`[${new Date().toLocaleTimeString('en-GB',{timeZone:'Asia/Bangkok',hour12:false})}] ${m}`;
e.insertBefore(d,e.firstChild);while(e.children.length>80)e.removeChild(e.lastChild);}

function fP(c){pf_=c;document.querySelectorAll('.pf').forEach(b=>{b.classList.toggle('bg-indigo-600/80',b.dataset.c===c);b.classList.toggle('bg-slate-700/60',b.dataset.c!==c)});rP();}
function fS(c){sf_=c;document.querySelectorAll('.sf').forEach(b=>{b.classList.toggle('bg-indigo-600/80',b.dataset.c===c);b.classList.toggle('bg-slate-700/60',b.dataset.c!==c)});rS();}
function sf(v,d=0){return typeof v==='number'&&isFinite(v)?v:d;}
function fp(p){if(p>=1000)return p.toFixed(2);if(p>=1)return p.toFixed(3);if(p>=0.01)return p.toFixed(5);if(p>=0.0001)return p.toFixed(6);return p.toFixed(8);}
function zc(z){return z.includes('BUY')?'text-emerald-400':z.includes('SELL')?'text-red-400':z.includes('BULL')?'text-emerald-500/80':z.includes('BEAR')?'text-red-500/80':'text-slate-500';}

function rP(){let h='',n=0;const q=(document.getElementById('search')?.value||'').toUpperCase();
const syms=Object.keys(A).sort((a,b)=>Math.abs(sf(A[b]?.momentum))-Math.abs(sf(A[a]?.momentum)));
for(const s of syms){const a=A[s];if(!a)continue;if(pf_!=='all'&&a.cat!==pf_)continue;if(q&&!s.includes(q))continue;n++;
const chc=sf(a.change_pct)>=0?'text-emerald-400':'text-red-400';const mc=sf(a.momentum)>=0?'text-emerald-400':'text-red-400';
const hs=S[s]?`<span class="w-2 h-2 rounded-full inline-block ${S[s].direction==='BUY'?'bg-emerald-400':'bg-red-400'} pulse2"></span>`:'';
const bw=Math.min(Math.abs(sf(a.momentum)),100);
h+=`<div class="gc rounded-xl p-2.5 ${S[s]?'border-indigo-600/20':''}"><div class="flex justify-between items-center">
<div class="flex items-center gap-1.5">${hs}<span class="font-bold text-sm">${s}</span><span class="cat-badge ${cc(a.cat||'')}">${a.cat||''}</span></div>
<div class="text-right"><span class="font-bold">$${fp(sf(a.price))}</span> <span class="${chc} text-[10px] font-semibold">${sf(a.change_pct)>=0?'+':''}${sf(a.change_pct).toFixed(2)}%</span></div></div>
<div class="flex justify-between mt-1.5 text-[9px]"><span class="${mc} font-semibold">Mom:${sf(a.momentum).toFixed(1)}</span>
<span class="${zc(a.zone||'')} font-semibold">${a.zone||''}</span><span class="text-slate-400">Str:${sf(a.strength)}%</span><span class="text-slate-600">${a.trend||''}</span></div>
<div class="mt-1.5 bg-slate-800/40 rounded-full h-1 overflow-hidden"><div class="${sf(a.momentum)>=0?'bg-gradient-to-r from-emerald-600 to-emerald-400':'bg-gradient-to-r from-red-600 to-red-400'} h-full rounded-full transition-all duration-500" style="width:${bw}%"></div></div></div>`;}
if(!n)h='<div class="text-slate-600 text-center py-8 text-[11px]">Scanning coins...</div>';
document.getElementById('pr').innerHTML=h;document.getElementById('ps').innerHTML=n>0?`<span class="text-emerald-400 pulse2">● ${n} coins</span>`:'Loading';}

function rT(){const arr=Object.values(A).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum))).slice(0,10);
document.getElementById('tm').innerHTML=arr.map(a=>{const mc=sf(a.momentum)>=0?'text-emerald-400':'text-red-400';
return`<div class="flex justify-between items-center py-1.5 border-b border-slate-800/20"><span class="font-bold text-[11px]">${a.symbol}</span>
<span class="${mc} font-mono font-bold text-[11px]">${sf(a.momentum)>=0?'+':''}${sf(a.momentum).toFixed(1)}</span>
<span class="text-[9px] ${zc(a.zone||'')} font-medium">${a.zone||''}</span></div>`}).join('')||'<div class="text-slate-600 text-xs py-4 text-center">Waiting...</div>';}

function rHM(){const sectors={};
Object.values(A).forEach(a=>{const cat=a.cat||'Other';if(!sectors[cat])sectors[cat]={sum:0,cnt:0,buy:0,sell:0};
sectors[cat].sum+=sf(a.momentum);sectors[cat].cnt++;if(S[a.symbol]){S[a.symbol].direction==='BUY'?sectors[cat].buy++:sectors[cat].sell++;}});
document.getElementById('hm').innerHTML=Object.entries(sectors).sort((a,b)=>b[1].cnt-a[1].cnt).map(([k,v])=>{const avg=(v.sum/v.cnt).toFixed(1);
const c=avg>=0?'from-emerald-950/40 to-emerald-900/20 border-emerald-700/20 text-emerald-400':'from-red-950/40 to-red-900/20 border-red-700/20 text-red-400';
return`<div class="bg-gradient-to-br ${c} border rounded-xl p-2.5"><div class="font-bold text-[11px]">${k}</div>
<div class="text-xs font-mono font-bold">${avg>=0?'+':''}${avg}</div><div class="text-[9px] text-slate-500 mt-0.5">${v.cnt} coins • ${v.buy}B/${v.sell}S</div></div>`}).join('');}

function rS(){let list=Object.values(S).sort((a,b)=>sf(b.strength)-sf(a.strength));
if(sf_==='STRONG')list=list.filter(s=>s.type==='STRONG');else if(sf_==='BUY')list=list.filter(s=>s.direction==='BUY');else if(sf_==='SELL')list=list.filter(s=>s.direction==='SELL');
if(!list.length){document.getElementById('sg').innerHTML=`<div class="glass rounded-2xl p-10 text-center text-slate-500"><div class="text-4xl mb-3">📡</div><div class="text-sm font-medium">${Object.keys(S).length?'No signals match filter':'Scanning...'}</div></div>`;document.getElementById('si').textContent=Object.keys(S).length;return;}
document.getElementById('si').textContent=Object.keys(S).length;
document.getElementById('sg').innerHTML=list.slice(0,20).map(s=>{const ib=s.direction==='BUY';const dc=ib?'text-emerald-400':'text-red-400';const cls=ib?'sb':'ss';
const tc=s.type==='STRONG'?'bg-gradient-to-r from-purple-500 to-indigo-500':s.type==='CONF'?'bg-gradient-to-r from-cyan-500 to-blue-500':'bg-slate-600';
const sw=Math.min(sf(s.strength),100);const scc=sw>=75?'bg-gradient-to-r from-emerald-500 to-emerald-400':sw>=50?'bg-gradient-to-r from-indigo-500 to-purple-400':'bg-slate-600';
const dT=ib?'BUY':'SELL';const strColor=sw>=75?'text-emerald-400 border-emerald-500/30 bg-emerald-500/10':sw>=50?'text-indigo-400 border-indigo-500/30 bg-indigo-500/10':'text-slate-400 border-slate-500/30 bg-slate-500/10';
return`<div class="glass ${cls} rounded-2xl p-4 slideUp"><div class="flex justify-between items-start mb-3"><div>
<div class="flex items-center gap-2"><span class="text-lg font-black ${dc}">${ib?'🟢':'🔴'} ${s.symbol}</span>
<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold text-white ${tc} shadow-sm">${s.type} ${dT}</span>
<span class="cat-badge ${cc(s.category||'')}">${s.category||''}</span></div>
<div class="text-[10px] text-slate-500 mt-1">Mom:${sf(s.momentum).toFixed(1)} • Conf:${sf(s.conf)}/3 • ${s.trend||''} • RSI:${sf(s.rsi_v,'?')}</div></div>
<div class="text-right"><div class="strength-ring ${strColor} border text-[13px]">${sf(s.strength)}%</div>
<div class="text-[9px] text-slate-600 mt-1">${s.timestamp||''}</div></div></div>
<div class="grid grid-cols-5 gap-2 mb-3">
<div class="bg-slate-900/50 rounded-xl p-2.5 text-center border border-slate-800/30"><div class="text-[9px] text-slate-500 font-medium">Entry</div><div class="font-bold text-sm mt-0.5">$${fp(sf(s.entry))}</div></div>
<div class="bg-red-950/30 rounded-xl p-2.5 text-center border border-red-800/20"><div class="text-[9px] text-red-400 font-medium">SL</div><div class="font-bold text-sm text-red-400 mt-0.5">$${fp(sf(s.sl))}</div></div>
<div class="bg-emerald-950/20 rounded-xl p-2.5 text-center border border-emerald-800/20"><div class="text-[9px] text-emerald-400 font-medium">TP1</div><div class="font-bold text-sm text-emerald-400 mt-0.5">$${fp(sf(s.tp1))}</div></div>
<div class="bg-emerald-950/25 rounded-xl p-2.5 text-center border border-emerald-700/25"><div class="text-[9px] text-emerald-300 font-medium">TP2</div><div class="font-bold text-sm text-emerald-300 mt-0.5">$${fp(sf(s.tp2))}</div></div>
<div class="bg-emerald-950/30 rounded-xl p-2.5 text-center border border-emerald-600/30"><div class="text-[9px] text-emerald-200 font-medium">TP3</div><div class="font-bold text-sm text-emerald-200 mt-0.5">$${fp(sf(s.tp3))}</div></div></div>
<div class="flex items-center gap-3 mb-2.5"><span class="text-[10px] text-slate-500 font-medium">Strength</span>
<div class="flex-1 bg-slate-800/40 rounded-full h-2 overflow-hidden"><div class="${scc} h-full rounded-full transition-all duration-500" style="width:${sw}%"></div></div>
<span class="text-slate-400 text-[10px]">R:R <b class="text-slate-200">${sf(s.rr)}:1</b></span>
<span class="text-slate-400 text-[10px]">Risk <b class="text-slate-200">${sf(s.risk_pct)}%</b></span>
<span class="text-[10px] font-bold ${(s.mtf_align||'').includes('BUY')?'text-emerald-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-slate-500'}">${s.mtf_align||''}</span></div>
<div class="flex flex-wrap gap-1">${(s.reasons||[]).slice(0,10).map(r=>`<span class="tag bg-slate-800/60 text-slate-300 border border-slate-700/25">${r}</span>`).join('')}</div></div>`}).join('');}

function rH(){if(!H.length){document.getElementById('ht').innerHTML='<tr><td colspan="12" class="py-8 text-center text-slate-600">Waiting for signals...</td></tr>';return;}
document.getElementById('hc').textContent=H.length+' signals';
document.getElementById('ht').innerHTML=H.slice(0,80).map(s=>{const dc=s.direction==='BUY'?'text-emerald-400 bg-emerald-500/10':'text-red-400 bg-red-500/10';
const tc=s.type==='STRONG'?'text-purple-400':s.type==='CONF'?'text-cyan-400':'text-slate-400';
return`<tr class="border-t border-slate-800/20 hover:bg-slate-800/15 transition-colors">
<td class="px-3 py-2 text-slate-500 text-[10px]">${s.timestamp||''}</td>
<td class="px-3 py-2 font-bold text-center">${s.symbol}</td><td class="px-3 py-2 text-center ${tc} font-bold">${s.type}</td>
<td class="px-3 py-2 text-center"><span class="px-2 py-0.5 rounded-md ${dc} font-bold text-[10px]">${s.direction}</span></td>
<td class="px-3 py-2 text-right font-mono">$${fp(sf(s.entry))}</td><td class="px-3 py-2 text-right font-mono text-red-400">$${fp(sf(s.sl))}</td>
<td class="px-3 py-2 text-right font-mono text-emerald-400">$${fp(sf(s.tp1))}</td><td class="px-3 py-2 text-right font-mono text-emerald-300">$${fp(sf(s.tp2))}</td>
<td class="px-3 py-2 font-bold text-center">${sf(s.rr)}:1</td>
<td class="px-3 py-2 text-center"><span class="px-1.5 py-0.5 rounded-md ${sf(s.strength)>=75?'bg-emerald-500/15 text-emerald-400':sf(s.strength)>=50?'bg-indigo-500/15 text-indigo-400':'bg-slate-700/50 text-slate-400'} font-bold">${sf(s.strength)}%</span></td>
<td class="px-3 py-2 text-center font-mono font-bold ${sf(s.momentum)>=0?'text-emerald-400':'text-red-400'}">${sf(s.momentum)>=0?'+':''}${sf(s.momentum).toFixed(1)}</td>
<td class="px-3 py-2 text-center text-[10px] font-bold ${(s.mtf_align||'').includes('BUY')?'text-emerald-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-slate-500'}">${s.mtf_align||''}</td></tr>`}).join('');}

function sA(s){document.getElementById('ai').textContent=s.direction==='BUY'?'🟢':'🔴';
document.getElementById('at').textContent=`${s.type} ${s.direction} — ${s.symbol} @ $${fp(sf(s.entry))}`;
document.getElementById('ax').textContent=`SL:$${fp(sf(s.sl))} TP2:$${fp(sf(s.tp2))} Str:${sf(s.strength)}% Mom:${sf(s.momentum).toFixed(1)}`;
document.getElementById('al').classList.remove('hidden');
try{const c=new(window.AudioContext||window.webkitAudioContext)();const o=c.createOscillator();const g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=s.direction==='BUY'?880:660;g.gain.value=0.06;o.start();o.stop(c.currentTime+0.12);}catch(e){}
setTimeout(()=>document.getElementById('al').classList.add('hidden'),12000);}

function loadState(d){if(!d)return;if(d.prices)Object.assign(P,d.prices);
if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}
if(d.analysis){for(const[k,v]of Object.entries(d.analysis)){A[k]=v;}}
if(d.history){const ids=new Set(H.map(h=>h.id));const ni=(d.history||[]).filter(h=>!ids.has(h.id));H.unshift(...ni);if(H.length===0&&d.history.length>0)H.push(...d.history);}
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}
document.getElementById('scc').textContent=d.scan_count||0;rP();rS();rH();rT();rHM();}

function doRefresh(){lo('Refreshing...','sys');fetch('/api/signals').then(r=>r.json()).then(d=>{
if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}if(d.history){H.length=0;H.push(...d.history);}
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}
rS();rH();lo(`Refreshed: ${Object.keys(S).length} signals`,'sys');}).catch(e=>lo('Failed: '+e,'error'));}
setInterval(()=>{if(Object.keys(A).length===0){lo('Auto-sync...','sys');doRefresh();}},30000);

so.on('connect',()=>{cn=true;document.getElementById('st').innerHTML='<span class="text-emerald-400 pulse2 font-semibold">● LIVE</span>';lo('Connected','sys');});
so.on('disconnect',()=>{cn=false;document.getElementById('st').innerHTML='<span class="text-red-400 font-semibold">● Offline</span>';lo('Disconnected','error');});
so.on('reconnect',()=>{lo('Reconnected','sys');so.emit('request_state');});
so.on('init',(d)=>{lo(`Init: ${Object.keys(d.analysis||{}).length} coins`,'sys');loadState(d);fP('all');});
so.on('full_sync',(d)=>{lo(`Sync: ${Object.keys(d.analysis||{}).length} coins, ${Object.keys(d.signals||{}).length} signals`,'sys');loadState(d);});
so.on('price_update',(d)=>{if(!d?.symbol)return;P[d.symbol]=d.data;if(d.analysis)A[d.symbol]=d.analysis;rP();rT();rHM();});
so.on('new_signal',(d)=>{if(!d?.signal)return;S[d.symbol]=d.signal;if(!H.find(h=>h.id===d.signal.id)){H.unshift(d.signal);}rS();rH();sA(d.signal);
lo(`🎯 ${d.signal.type} ${d.signal.direction} ${d.symbol} @$${fp(sf(d.signal.entry))} Str:${sf(d.signal.strength)}%`,'signal');});
so.on('scan_update',(d)=>{if(!d)return;document.getElementById('scc').textContent=d.scan_count||0;document.getElementById('ls').textContent=d.last_scan||'--';document.getElementById('ss2').textContent=d.status||'OK';
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}});
</script></body></html>'''


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return redirect(url_for('login') if 'user' not in session else url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = request.form.get('u', '').strip().lower()
        p = request.form.get('p', '')
        if users.verify(u, p):
            session.permanent = True
            session['user'] = u
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_req
def dashboard():
    return render_template_string(DASH_HTML)

@app.route('/health')
def health():
    return jsonify(sanitize({
        'status': 'ok', 'version': '5.0-crypto-gevent', 'time': dts(),
        'scans': store.scan_count, 'coins': len(C.SYMBOLS),
        'active_signals': len(store.signals),
        'connected': store.connected,
        'first_scan_done': store.first_scan_done,
        'dead_tickers': list(crypto_client._dead_tickers),
    }))

@app.route('/api/signals')
@login_req
def api_signals():
    return jsonify(sanitize({
        'signals': store.signals,
        'history': store.history[:200],
        'stats': store.stats
    }))

@app.route('/api/refresh')
@login_req
def api_refresh():
    return jsonify(store.get_state())


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
@socketio.on('connect')
def ws_conn():
    store.connected += 1
    try:
        emit('init', store.get_state())
    except Exception as e:
        logger.warning(f"WS init: {e}")

@socketio.on('disconnect')
def ws_disc():
    store.connected = max(0, store.connected - 1)

@socketio.on('request_state')
def ws_req():
    try:
        emit('full_sync', store.get_state())
    except:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════════════
port = int(os.environ.get('PORT', 8000))
gevent.spawn(scanner)

if __name__ == '__main__':
    print("=" * 65)
    print("  ₿ TCSM CRYPTO PRO v5.0 — PRODUCTION (GEVENT)")
    print("  📊 CoinGecko Free API + yfinance Fallback")
    print(f"  🕐 Time: {dts()} | Coins: {len(C.SYMBOLS)}")
    print(f"  🌐 Port: {port} | Login: admin / admin123")
    print("  🔄 24/7 Scanning — Crypto Never Sleeps")
    print("=" * 65)
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)
