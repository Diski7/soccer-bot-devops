import logging
import os
import sys
import time
import openai
import requests
import secrets
import string
import re
import json
import hashlib
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.error import Conflict, NetworkError, TimedOut, RetryAfter
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Enum, desc, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
import enum
import asyncio
from functools import wraps
from collections import defaultdict
import threading

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

Base = declarative_base()

class UserRole(enum.Enum):
    USER = "user"
    ADMIN = "admin"

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False, index=True)
    username = Column(String)
    first_name = Column(String)
    role = Column(Enum(UserRole), default=UserRole.USER)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    message_count = Column(Integer, default=0)
    is_authorized = Column(Boolean, default=False)

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, index=True)
    user_message = Column(Text)
    bot_response = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class ReferralCode(Base):
    __tablename__ = 'referral_codes'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False, index=True)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, index=True)
    used_by = Column(Text, default="")

class UnauthorizedAttempt(Base):
    __tablename__ = 'unauthorized_attempts'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, index=True)
    username = Column(String)
    first_name = Column(String)
    message = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

# OPTIMIZED: Connection pooling for high concurrency
engine = create_engine(
    get_database_url(),
    poolclass=QueuePool,
    pool_size=10,           # Keep 10 connections ready
    max_overflow=20,        # Allow 20 extra under load
    pool_timeout=30,        # Wait 30s for available connection
    pool_recycle=1800,      # Recycle connections after 30min
    pool_pre_ping=True      # Verify connections before use
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db():
    db = get_db()
    try:
        inspector = inspect(engine)
        if 'users' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('users')]
            if 'is_authorized' not in columns:
                try:
                    db.execute(text("ALTER TABLE users ADD COLUMN is_authorized BOOLEAN DEFAULT FALSE"))
                    db.commit()
                except:
                    db.rollback()
        Base.metadata.create_all(engine)
        logger.info("Database ready with optimized pooling!")
    except Exception as e:
        logger.error(f"Database error: {e}")
        try:
            Base.metadata.drop_all(engine)
        except:
            pass
        Base.metadata.create_all(engine)
        logger.info("Database recreated!")
    finally:
        db.close()

def get_db():
    return SessionLocal()

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
PORT = int(os.getenv("PORT", "8080"))
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL", "")

USE_OPENAI = bool(OPENAI_API_KEY)
USE_OLLAMA = bool(OLLAMA_URL and not USE_OPENAI)

if USE_OPENAI:
    openai.api_key = OPENAI_API_KEY
    logger.info("Using OpenAI for LLM")
elif USE_OLLAMA:
    logger.info(f"Using Ollama at {OLLAMA_URL}")

def check_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_TELEGRAM_ID

# SIMPLE CACHE: In-memory with TTL
class SimpleCache:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            if key in self.cache:
                value, expiry = self.cache[key]
                if datetime.utcnow() < expiry:
                    return value
                else:
                    del self.cache[key]
            return None
    
    def set(self, key, value, ttl=None):
        with self.lock:
            expiry = datetime.utcnow() + timedelta(seconds=(ttl or self.ttl))
            self.cache[key] = (value, expiry)
    
    def delete(self, key):
        with self.lock:
            if key in self.cache:
                del self.cache[key]

# Initialize caches
auth_cache = SimpleCache(ttl_seconds=60)      # Auth status (1 min)
memory_cache = SimpleCache(ttl_seconds=30)    # Recent memory (30 sec)
rate_limit_cache = SimpleCache(ttl_seconds=60) # Rate limiting (1 min)

def is_user_authorized(telegram_id: str):
    # Check cache first
    cached = auth_cache.get(f"auth_{telegram_id}")
    if cached is not None:
        return cached
    
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        result = user.is_authorized if user else False
        auth_cache.set(f"auth_{telegram_id}", result)
        return result
    finally:
        db.close()

def log_unauthorized_attempt(telegram_id: str, username: str, first_name: str, message: str):
    # Batch insert for performance (optional optimization)
    db = get_db()
    try:
        attempt = UnauthorizedAttempt(
            telegram_id=str(telegram_id),
            username=username or "",
            first_name=first_name or "",
            message=message[:500]
        )
        db.add(attempt)
        db.commit()
    except Exception as e:
        logger.error(f"Error logging: {e}")
        db.rollback()
    finally:
        db.close()

def check_rate_limit(telegram_id: str, max_requests=30):
    """Rate limit: 30 messages per minute per user"""
    key = f"rate_{telegram_id}"
    count = rate_limit_cache.get(key) or 0
    if count >= max_requests:
        return False
    rate_limit_cache.set(key, count + 1, ttl=60)
    return True

def require_auth(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        telegram_id = str(user.id)
        
        if check_admin(user.id):
            return await func(update, context, *args, **kwargs)
        
        if not is_user_authorized(telegram_id):
            log_unauthorized_attempt(telegram_id, user.username, user.first_name, 
                                   update.message.text if update.message else "N/A")
            await update.message.reply_text(
                "🔒 **Private Bot**\n\nInvitation only.\n\n🔑 `/code YOURCODE`",
                parse_mode='Markdown'
            )
            return
        
        # Rate limiting check
        if not check_rate_limit(telegram_id):
            await update.message.reply_text("⏱️ Too many messages. Slow down!")
            return
        
        return await func(update, context, *args, **kwargs)
    return wrapper

def generate_referral_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_duration(duration_str: str) -> timedelta:
    duration_str = duration_str.lower().strip()
    patterns = {
        r'^(\d+)m$': lambda x: int(x) * 30,
        r'^(\d+)mo$': lambda x: int(x) * 30,
        r'^(\d+)month$': lambda x: int(x) * 30,
        r'^(\d+)months$': lambda x: int(x) * 30,
        r'^(\d+)y$': lambda x: int(x) * 365,
        r'^(\d+)yr$': lambda x: int(x) * 365,
        r'^(\d+)year$': lambda x: int(x) * 365,
        r'^(\d+)years$': lambda x: int(x) * 365,
        r'^(\d+)d$': lambda x: int(x),
        r'^(\d+)day$': lambda x: int(x),
        r'^(\d+)days$': lambda x: int(x),
        r'^(\d+)h$': lambda x: int(x) / 24,
        r'^(\d+)hr$': lambda x: int(x) / 24,
        r'^(\d+)hour$': lambda x: int(x) / 24,
        r'^(\d+)hours$': lambda x: int(x) / 24,
    }
    for pattern, converter in patterns.items():
        match = re.match(pattern, duration_str)
        if match:
            days = converter(match.group(1))
            return timedelta(days=int(days))
    return timedelta(days=1)

def format_duration(td: timedelta) -> str:
    days = td.days
    if days >= 365:
        years = days // 365
        return f"{years}y"
    elif days >= 30:
        months = days // 30
        return f"{months}m"
    else:
        return f"{days}d"

def create_referral_code(admin_id: str, duration: timedelta, max_uses: int = 1):
    db = get_db()
    try:
        code = generate_referral_code()
        expires_at = datetime.utcnow() + duration
        ref_code = ReferralCode(
            code=code,
            created_by=admin_id,
            expires_at=expires_at,
            max_uses=max_uses,
            used_count=0,
            is_active=True
        )
        db.add(ref_code)
        db.commit()
        return {
            "code": code,
            "expires_at": expires_at,
            "max_uses": max_uses,
            "duration": duration
        }
    except Exception as e:
        logger.error(f"Error: {e}")
        db.rollback()
        return None
    finally:
        db.close()

def validate_referral_code(code: str, user_id: str):
    db = get_db()
    try:
        ref = db.query(ReferralCode).filter_by(code=code.upper()).first()
        if not ref:
            return False, "Invalid code."
        if not ref.is_active:
            return False, "Code deactivated."
        if datetime.utcnow() > ref.expires_at:
            ref.is_active = False
            db.commit()
            return False, "Code expired."
        if ref.used_count >= ref.max_uses:
            return False, "Max uses reached."
        used_by_list = ref.used_by.split(",") if ref.used_by else []
        if user_id in used_by_list:
            return False, "Already used."
        return True, "Valid!"
    except Exception as e:
        logger.error(f"Error: {e}")
        return False, "Error."
    finally:
        db.close()

def use_referral_code(code: str, user_id: str):
    db = get_db()
    try:
        ref = db.query(ReferralCode).filter_by(code=code.upper()).first()
        if ref:
            ref.used_count += 1
            used_by_list = ref.used_by.split(",") if ref.used_by else []
            used_by_list.append(user_id)
            ref.used_by = ",".join(used_by_list)
            if ref.used_count >= ref.max_uses:
                ref.is_active = False
            db.commit()
            return True
        return False
    except Exception as e:
        logger.error(f"Error: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def authorize_user(telegram_id: str):
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.is_authorized = True
            db.commit()
            # Invalidate cache
            auth_cache.delete(f"auth_{telegram_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def get_recent_memory(telegram_id: str, max_messages: int = 6):
    # Check cache
    cache_key = f"mem_{telegram_id}"
    cached = memory_cache.get(cache_key)
    if cached:
        return cached
    
    db = get_db()
    try:
        history = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).order_by(desc(Conversation.timestamp)).limit(max_messages).all()
        result = list(reversed(history))
        memory_cache.set(cache_key, result, ttl=30)
        return result
    finally:
        db.close()

def get_memory_summary(telegram_id: str):
    db = get_db()
    try:
        total_convos = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).count()
        first_convo = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).order_by(Conversation.timestamp).first()
        last_convo = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).order_by(desc(Conversation.timestamp)).first()
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        time_since_last = None
        if last_convo:
            time_since_last = datetime.utcnow() - last_convo.timestamp
        return {
            "total_messages": total_convos,
            "first_chat": first_convo.timestamp if first_convo else None,
            "last_chat": last_convo.timestamp if last_convo else None,
            "user_name": user.first_name if user else "Friend",
            "time_since_last": time_since_last,
            "is_new_user": total_convos == 0,
            "is_authorized": user.is_authorized if user else False
        }
    finally:
        db.close()

def is_greeting(message: str) -> bool:
    greetings = ["hi", "hello", "hey", "greetings", "good morning", 
                "good afternoon", "good evening", "yo", "sup", "what's up",
                "howdy", "hi there", "hello there", "hey there"]
    msg_lower = message.lower().strip()
    for greeting in greetings:
        if msg_lower == greeting or msg_lower.startswith(greeting + " "):
            return True
    return False

def get_llm_response(user_message: str, conversation_history: list, user_name: str, is_new_user: bool = False) -> str:
    messages = []
    
    system_prompt = f"""You are a helpful AI assistant. You can discuss any topic knowledgeably.
You remember past conversations with {user_name} and maintain continuity.
Be concise, helpful, and natural. If unsure, say so."""
    
    messages.append({"role": "system", "content": system_prompt})
    
    if conversation_history and not is_new_user:
        for conv in conversation_history[-3:]:
            messages.append({"role": "user", "content": conv.user_message})
            messages.append({"role": "assistant", "content": conv.bot_response})
    
    messages.append({"role": "user", "content": user_message})
    
    try:
        if USE_OPENAI:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                max_tokens=300,
                temperature=0.7,
                request_timeout=10  # Fail fast if slow
            )
            return response.choices[0].message.content
            
        elif USE_OLLAMA:
            prompt = f"{system_prompt}\n\n"
            if conversation_history and not is_new_user:
                for conv in conversation_history[-3:]:
                    prompt += f"User: {conv.user_message}\nAssistant: {conv.bot_response}\n"
            prompt += f"User: {user_message}\nAssistant:"
            
            response = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": "llama2", "prompt": prompt, "stream": False, "max_tokens": 300},
                timeout=10
            )
            return response.json().get("response", "Can't respond now.")
        else:
            return None
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not is_user_authorized(telegram_id) and not check_admin(user.id):
        log_unauthorized_attempt(telegram_id, user.username, user.first_name, "Started bot")
        await update.message.reply_text(
            "🔒 **Private AI Bot**\n\nInvitation only.\n\n🔑 Get code from @LearnWithLucky\n💬 Then type: `/code YOURCODE`",
            parse_mode='Markdown'
        )
        return
    
    memory = get_memory_summary(telegram_id)
    
    if memory["is_new_user"]:
        welcome = "Hey! I'm your AI assistant. Ask me anything - I remember our conversations. What's on your mind?"
    else:
        if memory["time_since_last"] and memory["time_since_last"].days > 7:
            welcome = f"Hey {memory['user_name']}! Been a while. What's up?"
        else:
            welcome = f"Hey {memory['user_name']}! What's up?"
    
    await update.message.reply_text(welcome)

async def enter_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not context.args:
        await update.message.reply_text("🔑 `/code YOURCODE`", parse_mode='Markdown')
        return
    
    code = context.args[0].upper()
    user_id_str = str(telegram_id)
    
    if is_user_authorized(telegram_id):
        await update.message.reply_text("✅ Already have access!")
        return
    
    is_valid, message = validate_referral_code(code, user_id_str)
    
    if not is_valid:
        log_unauthorized_attempt(telegram_id, user.username, user.first_name, f"Bad code: {code}")
        await update.message.reply_text(f"❌ {message}")
        return
    
    if use_referral_code(code, user_id_str):
        db = get_db()
        try:
            user_db = db.query(User).filter_by(telegram_id=telegram_id).first()
            if not user_db:
                user_db = User(
                    telegram_id=telegram_id,
                    username=user.username,
                    first_name=user.first_name,
                    role=UserRole.ADMIN if check_admin(user.id) else UserRole.USER,
                    is_authorized=True
                )
                db.add(user_db)
            else:
                user_db.is_authorized = True
            db.commit()
            
            await update.message.reply_text(
                "✅ **Welcome!**\n\n"
                "I'm your AI with memory. Ask me anything:\n"
                "• Tech, science, business, coding\n"
                "• Advice, writing, analysis\n"
                "• Sports, history, life questions\n\n"
                "What would you like to talk about?"
            )
        except Exception as e:
            logger.error(f"Error: {e}")
            db.rollback()
            await update.message.reply_text("❌ Error. Try again.")
        finally:
            db.close()
    else:
        await update.message.reply_text("❌ Error. Try again.")

@require_auth
async def generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_admin(user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    duration_str = "24h"
    max_uses = 1
    
    if context.args:
        first_arg = context.args[0]
        if any(c.isalpha() for c in first_arg):
            duration_str = first_arg
            if len(context.args) > 1:
                try:
                    max_uses = int(context.args[1])
                except:
                    pass
        else:
            try:
                hours = int(first_arg)
                duration_str = f"{hours}h"
                if len(context.args) > 1:
                    max_uses = int(context.args[1])
            except:
                await update.message.reply_text("Usage: `/gencode 3m 5`", parse_mode='Markdown')
                return
    
    duration = parse_duration(duration_str)
    result = create_referral_code(str(user.id), duration, max_uses)
    
    if result:
        expires_str = result['expires_at'].strftime("%b %d, %Y")
        duration_readable = format_duration(result['duration'])
        
        await update.message.reply_text(
            f"🎟️ **Code Generated**\n\n"
            f"`{result['code']}`\n"
            f"Duration: {duration_readable}\n"
            f"Expires: {expires_str}\n"
            f"Uses: {result['max_uses']}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Error.")

@require_auth
async def list_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_admin(user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    db = get_db()
    try:
        codes = db.query(ReferralCode).filter_by(is_active=True).all()
        if not codes:
            await update.message.reply_text("No active codes.")
            return
        
        msg = "🎟️ **Active Codes:**\n\n"
        for code in codes:
            expires_in = code.expires_at - datetime.utcnow()
            days_left = expires_in.days
            if days_left > 30:
                time_left = f"{days_left//30}m"
            elif days_left > 0:
                time_left = f"{days_left}d"
            else:
                time_left = f"{expires_in.seconds//3600}h"
            
            msg += f"`{code.code}` | {code.used_count}/{code.max_uses} | {time_left}\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Error.")
    finally:
        db.close()

@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    current_message = update.message.text
    
    history = get_recent_memory(telegram_id, max_messages=6)
    memory = get_memory_summary(telegram_id)
    current_lower = current_message.lower()
    
    if is_greeting(current_message):
        response = "Hi, how may I assist you?"
    
    elif any(x in current_lower for x in ["stats", "history", "memory"]):
        response = f"We've chatted {memory['total_messages']} times. What's up?"
    
    elif any(x in current_lower for x in ["remember", "recall"]):
        if history:
            response = "We've talked about various things. What specifically?"
        else:
            response = "Just getting started! What would you like to discuss?"
    
    else:
        llm_response = get_llm_response(current_message, history, memory['user_name'], memory['is_new_user'])
        if llm_response:
            response = llm_response
        else:
            response = "I'm here to help. What would you like to know?" if memory["is_new_user"] else "Tell me more."
    
    await update.message.reply_text(response)
    
    # Async save to not block response
    async def save_conversation():
        db = get_db()
        try:
            conv = Conversation(
                telegram_id=telegram_id,
                user_message=current_message,
                bot_response=response,
                timestamp=datetime.utcnow()
            )
            db.add(conv)
            
            user_db = db.query(User).filter_by(telegram_id=telegram_id).first()
            if not user_db:
                user_db = User(
                    telegram_id=telegram_id,
                    username=user.username,
                    first_name=user.first_name,
                    role=UserRole.ADMIN if check_admin(user.id) else UserRole.USER,
                    is_authorized=True
                )
                db.add(user_db)
            
            user_db.message_count = memory['total_messages'] + 1
            user_db.last_active = datetime.utcnow()
            db.commit()
            
            # Invalidate memory cache
            memory_cache.delete(f"mem_{telegram_id}")
        except Exception as e:
            logger.error(f"Error saving: {e}")
            db.rollback()
        finally:
            db.close()
    
    # Fire and forget save
    asyncio.create_task(save_conversation())

@require_auth
async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    if not check_admin(user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    
    db = get_db()
    try:
        db.query(Conversation).filter_by(telegram_id=telegram_id).delete()
        db.query(User).filter_by(telegram_id=telegram_id).delete()
        db.commit()
        auth_cache.delete(f"auth_{telegram_id}")
        memory_cache.delete(f"mem_{telegram_id}")
        await update.message.reply_text("🗑️ Data deleted.")
    except Exception as e:
        logger.error(f"Error: {e}")
        db.rollback()
        await update.message.reply_text("❌ Error.")
    finally:
        db.close()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}")
    
    if isinstance(context.error, RetryAfter):
        # Rate limited by Telegram, wait and retry
        retry_after = context.error.retry_after
        logger.warning(f"Rate limited. Retry after {retry_after}s")
        await asyncio.sleep(retry_after)
        return
    
    if isinstance(context.error, Conflict):
        logger.error("Conflict - multiple instances")
        return
    
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Network error")
        return

def main():
    init_db()
    if not TELEGRAM_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN!")
        return
    
    # Build with optimized settings for scale
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)  # Enable concurrent processing
        .connection_pool_size(20)   # More connections for high load
        .pool_timeout(30.0)
        .build()
    )
    
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("code", enter_code))
    application.add_handler(CommandHandler("gencode", generate_code))
    application.add_handler(CommandHandler("codes", list_codes))
    application.add_handler(CommandHandler("delete_my_data", delete_my_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🚀 SCALABLE BOT RUNNING!")
    logger.info("Ready for 1000+ users")
    
    if RAILWAY_STATIC_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{RAILWAY_STATIC_URL}/webhook",
            drop_pending_updates=True
        )
    else:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

if __name__ == "__main__":
    main()
