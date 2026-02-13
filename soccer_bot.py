import logging
import os
import sys
import time
import openai
import requests
import secrets
import string
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.error import Conflict, NetworkError, TimedOut
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Enum, desc, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum
import asyncio
from functools import wraps

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
    telegram_id = Column(String, unique=True, nullable=False)
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
    timestamp = Column(DateTime, default=datetime.utcnow)

class ReferralCode(Base):
    __tablename__ = 'referral_codes'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False, index=True)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
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

engine = create_engine(get_database_url())
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """Initialize database with schema migration support"""
    db = get_db()
    try:
        inspector = inspect(engine)
        
        if 'users' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('users')]
            if 'is_authorized' not in columns:
                logger.info("Adding is_authorized column to users...")
                from sqlalchemy import text
                try:
                    db.execute(text("ALTER TABLE users ADD COLUMN is_authorized BOOLEAN DEFAULT FALSE"))
                    db.commit()
                except:
                    db.rollback()
        
        Base.metadata.create_all(engine)
        logger.info("Database ready!")
        
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

# LLM Configuration
USE_OPENAI = bool(OPENAI_API_KEY)
USE_OLLAMA = bool(OLLAMA_URL and not USE_OPENAI)

if USE_OPENAI:
    openai.api_key = OPENAI_API_KEY
    logger.info("Using OpenAI for LLM")
elif USE_OLLAMA:
    logger.info(f"Using Ollama at {OLLAMA_URL}")
else:
    logger.warning("No LLM configured - running in memory-only mode")

def check_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_TELEGRAM_ID

def is_user_authorized(telegram_id: str):
    """Check if user is authorized"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            return user.is_authorized
        return False
    finally:
        db.close()

def log_unauthorized_attempt(telegram_id: str, username: str, first_name: str, message: str):
    """Log attempts by unauthorized users"""
    db = get_db()
    try:
        attempt = UnauthorizedAttempt(
            telegram_id=str(telegram_id),
            username=username or "",
            first_name=first_name or "",
            message=message[:500]  # Limit message length
        )
        db.add(attempt)
        db.commit()
        logger.warning(f"Unauthorized attempt by {first_name} (@{username}): {message[:100]}")
    except Exception as e:
        logger.error(f"Error logging unauthorized attempt: {e}")
        db.rollback()
    finally:
        db.close()

def require_auth(func):
    """Decorator to require authorization"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        telegram_id = str(user.id)
        
        # Always allow admin
        if check_admin(user.id):
            return await func(update, context, *args, **kwargs)
        
        # Check if authorized
        if not is_user_authorized(telegram_id):
            # Log the attempt
            log_unauthorized_attempt(
                telegram_id, 
                user.username, 
                user.first_name, 
                update.message.text if update.message else "N/A"
            )
            
            # Send rejection message
            await update.message.reply_text(
                "⛔ **ACCESS DENIED**\n\n"
                "This is a private bot. You need a valid referral code to use it.\n\n"
                "🔑 To enter a code, type:\n`/code YOURCODE`\n\n"
                "🎟️ Codes are given out by the admin only.\n"
                "⏰ Codes expire after the set time period.\n\n"
                "Contact @LearnWithLucky for access.",
                parse_mode='Markdown'
            )
            return
        
        return await func(update, context, *args, **kwargs)
    return wrapper

def generate_referral_code(length=8):
    """Generate a random referral code"""
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '1m', '3m', '6m', '12m', '1y', '30d' into timedelta"""
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
    """Format timedelta into readable string"""
    days = td.days
    if days >= 365:
        years = days // 365
        remaining_days = days % 365
        if remaining_days > 30:
            months = remaining_days // 30
            return f"{years} year{'s' if years != 1 else ''}, {months} month{'s' if months != 1 else ''}"
        return f"{years} year{'s' if years != 1 else ''}"
    elif days >= 30:
        months = days // 30
        remaining_days = days % 30
        if remaining_days > 0:
            return f"{months} month{'s' if months != 1 else ''}, {remaining_days} days"
        return f"{months} month{'s' if months != 1 else ''}"
    else:
        return f"{days} day{'s' if days != 1 else ''}"

def create_referral_code(admin_id: str, duration: timedelta, max_uses: int = 1):
    """Create a new time-based referral code"""
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
        logger.error(f"Error creating referral code: {e}")
        db.rollback()
        return None
    finally:
        db.close()

def validate_referral_code(code: str, user_id: str):
    """Check if code is valid and not expired"""
    db = get_db()
    try:
        ref = db.query(ReferralCode).filter_by(code=code.upper()).first()
        
        if not ref:
            return False, "Invalid code."
        
        if not ref.is_active:
            return False, "This code has been deactivated."
        
        if datetime.utcnow() > ref.expires_at:
            ref.is_active = False
            db.commit()
            return False, "This code has expired."
        
        if ref.used_count >= ref.max_uses:
            return False, "This code has reached its maximum uses."
        
        used_by_list = ref.used_by.split(",") if ref.used_by else []
        if user_id in used_by_list:
            return False, "You have already used this code."
        
        return True, "Code is valid!"
        
    except Exception as e:
        logger.error(f"Error validating code: {e}")
        return False, "Error validating code."
    finally:
        db.close()

def use_referral_code(code: str, user_id: str):
    """Mark code as used by a specific user"""
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
        logger.error(f"Error using referral code: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def authorize_user(telegram_id: str):
    """Mark user as authorized"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.is_authorized = True
            db.commit()
            return True
        return False
    except Exception as e:
        logger.error(f"Error authorizing user: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def get_recent_memory(telegram_id: str, max_messages: int = 6):
    """Fetch only recent conversations for context"""
    db = get_db()
    try:
        history = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).order_by(desc(Conversation.timestamp)).limit(max_messages).all()
        return list(reversed(history))
    finally:
        db.close()

def get_memory_summary(telegram_id: str):
    """Get minimal summary"""
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
    """Check if message is a greeting"""
    greetings = [
        "hi", "hello", "hey", "greetings", "good morning", 
        "good afternoon", "good evening", "yo", "sup", "what's up",
        "howdy", "hi there", "hello there", "hey there"
    ]
    msg_lower = message.lower().strip()
    for greeting in greetings:
        if msg_lower == greeting or msg_lower.startswith(greeting + " "):
            return True
    return False

def get_llm_response(user_message: str, conversation_history: list, user_name: str, is_new_user: bool = False) -> str:
    """Get natural response from LLM"""
    
    messages = []
    
    system_prompt = """You are a knowledgeable soccer assistant having a natural conversation. 
You remember past discussions but speak casually like a friend. 
Don't summarize conversation history unless asked. 
Just respond to the current question while maintaining context from previous messages.
Be concise, friendly, and soccer-focused."""
    
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
                temperature=0.7
            )
            return response.choices[0].message.content
            
        elif USE_OLLAMA:
            prompt = f"{system_prompt}\n\n"
            if conversation_history and not is_new_user:
                prompt += "Recent conversation:\n"
                for conv in conversation_history[-3:]:
                    prompt += f"User: {conv.user_message}\n"
                    prompt += f"Assistant: {conv.bot_response}\n"
            prompt += f"User: {user_message}\nAssistant:"
            
            response = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama2",
                    "prompt": prompt,
                    "stream": False,
                    "max_tokens": 300
                },
                timeout=30
            )
            return response.json().get("response", "I couldn't generate a response right now.")
        
        else:
            return None
            
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    
    # Check authorization
    authorized = is_user_authorized(telegram_id)
    
    if not authorized and not check_admin(user.id):
        # Log this attempt
        log_unauthorized_attempt(
            telegram_id,
            user.username,
            user.first_name,
            "Started bot without code"
        )
        
        await update.message.reply_text(
            "👋 **Welcome to Learn With Lucky Soccer Bot**\n\n"
            "🔒 This is a **private bot**. Access is by invitation only.\n\n"
            "🎟️ **To join:**\n"
            "1. Get a referral code from @LearnWithLucky\n"
            "2. Type: `/code YOURCODE`\n\n"
            "⏰ Codes expire after set time\n"
            "👥 Limited uses per code\n\n"
            "No code? Contact @LearnWithLucky for access.",
            parse_mode='Markdown'
        )
        return
    
    memory = get_memory_summary(telegram_id)
    
    if memory["is_new_user"]:
        welcome = "Hey! I'm your soccer buddy. Ask me anything about the beautiful game! ⚽"
    else:
        if memory["time_since_last"] and memory["time_since_last"].days > 7:
            welcome = f"Hey {memory['user_name']}! Long time no see. What's on your mind about soccer?"
        else:
            welcome = f"Hey {memory['user_name']}! What's up?"
    
    await update.message.reply_text(welcome)

async def enter_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User enters referral code - NO auth required"""
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not context.args:
        await update.message.reply_text(
            "🔑 **Enter Referral Code**\n\n"
            "Usage: `/code YOURCODE`\n\n"
            "Example: `/code X7K9M2P4`",
            parse_mode='Markdown'
        )
        return
    
    code = context.args[0].upper()
    user_id_str = str(telegram_id)
    
    # Check if already authorized
    if is_user_authorized(telegram_id):
        await update.message.reply_text("✅ You're already authorized! Enjoy the bot! ⚽")
        return
    
    # Validate code
    is_valid, message = validate_referral_code(code, user_id_str)
    
    if not is_valid:
        # Log failed attempt
        log_unauthorized_attempt(
            telegram_id,
            user.username,
            user.first_name,
            f"Failed code attempt: {code}"
        )
        
        await update.message.reply_text(f"❌ **{message}**\n\nTry again or contact @LearnWithLucky for a valid code.", parse_mode='Markdown')
        return
    
    # Use the code
    if use_referral_code(code, user_id_str):
        # Create or update user
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
                "✅ **Access Granted!**\n\n"
                "Welcome to the Soccer Bot! 🎉⚽\n\n"
                "I remember every conversation we have. Ask me anything about:\n"
                "• Formations & tactics\n"
                "• Players & teams\n"
                "• Training & drills\n"
                "• Match analysis\n\n"
                "What would you like to talk about?"
            )
            
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            db.rollback()
            await update.message.reply_text("❌ Error processing code. Please try again.")
        finally:
            db.close()
    else:
        await update.message.reply_text("❌ Error processing code. Please try again.")

@require_auth
async def generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to generate referral code - REQUIRES AUTH"""
    user = update.effective_user
    
    if not check_admin(user.id):
        await update.message.reply_text("⛔ This command is only for admins.")
        return
    
    # Parse arguments: /gencode [duration] [uses]
    duration_str = "24h"
    max_uses = 1
    
    if context.args:
        first_arg = context.args[0]
        
        if any(c.isalpha() for c in first_arg):
            duration_str = first_arg
            if len(context.args) > 1:
                try:
                    max_uses = int(context.args[1])
                except ValueError:
                    pass
        else:
            try:
                hours = int(first_arg)
                duration_str = f"{hours}h"
                if len(context.args) > 1:
                    max_uses = int(context.args[1])
            except ValueError:
                await update.message.reply_text(
                    "📋 **Usage:**\n"
                    "`/gencode [duration] [uses]`\n\n"
                    "**Examples:**\n"
                    "`/gencode 1m` (1 month, 1 use)\n"
                    "`/gencode 3m 5` (3 months, 5 uses)\n"
                    "`/gencode 6m 10` (6 months, 10 uses)\n"
                    "`/gencode 12m` (12 months)\n"
                    "`/gencode 1y` (1 year)",
                    parse_mode='Markdown'
                )
                return
    
    duration = parse_duration(duration_str)
    result = create_referral_code(str(user.id), duration, max_uses)
    
    if result:
        expires_str = result['expires_at'].strftime("%B %d, %Y")
        duration_readable = format_duration(result['duration'])
        
        await update.message.reply_text(
            f"🎟️ **Referral Code Generated**\n\n"
            f"Code: `{result['code']}`\n"
            f"Duration: {duration_readable}\n"
            f"Expires: {expires_str}\n"
            f"Max uses: {result['max_uses']}\n\n"
            f"Share this code with friends!",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Error generating code. Please try again.")

@require_auth
async def list_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to list active codes - REQUIRES AUTH"""
    user = update.effective_user
    
    if not check_admin(user.id):
        await update.message.reply_text("⛔ This command is only for admins.")
        return
    
    db = get_db()
    try:
        codes = db.query(ReferralCode).filter_by(is_active=True).all()
        
        if not codes:
            await update.message.reply_text("📭 No active referral codes.")
            return
        
        message = "🎟️ **Active Referral Codes:**\n\n"
        for code in codes:
            expires_in = code.expires_at - datetime.utcnow()
            hours_left = int(expires_in.total_seconds() / 3600)
            days_left = hours_left // 24
            
            if days_left > 30:
                months_left = days_left // 30
                time_left = f"{months_left}mo"
            elif days_left > 0:
                time_left = f"{days_left}d"
            else:
                time_left = f"{hours_left}h"
            
            status = "⏰" if hours_left < 24 else "✅"
            
            message += (
                f"{status} `{code.code}` | "
                f"Uses: {code.used_count}/{code.max_uses} | "
                f"Expires: {time_left}\n"
            )
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error listing codes: {e}")
        await update.message.reply_text("❌ Error retrieving codes.")
    finally:
        db.close()

@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages - REQUIRES AUTH"""
    user = update.effective_user
    telegram_id = str(user.id)
    current_message = update.message.text
    
    history = get_recent_memory(telegram_id, max_messages=6)
    memory = get_memory_summary(telegram_id)
    
    current_lower = current_message.lower()
    
    if is_greeting(current_message):
        response = "Hi, how may I assist you?"
    
    elif any(x in current_lower for x in ["stats", "history", "how many messages", "memory"]):
        response = f"We've chatted {memory['total_messages']} times. What would you like to know?"
    
    elif any(x in current_lower for x in ["remember", "recall", "what did we talk about"]):
        if history:
            topics = set()
            for conv in history:
                msg = conv.user_message.lower()
                if "formation" in msg:
                    topics.add("formations")
                elif "player" in msg or any(name in msg for name in ["messi", "ronaldo", "neymar"]):
                    topics.add("players")
                elif "training" in msg or "drill" in msg:
                    topics.add("training")
                elif "tactic" in msg or "strategy" in msg:
                    topics.add("tactics")
            
            if topics:
                response = f"Recently we've talked about {', '.join(topics)}. What would you like to dive into?"
            else:
                response = "We've been chatting about soccer. What would you like to discuss?"
        else:
            response = "We just started talking! What soccer topics interest you?"
    
    else:
        llm_response = get_llm_response(
            current_message, 
            history, 
            memory['user_name'],
            memory['is_new_user']
        )
        
        if llm_response:
            response = llm_response
        else:
            if memory["is_new_user"]:
                response = "I'm here to talk soccer! What would you like to know?"
            else:
                response = "Got it. Tell me more about what you're thinking."

    await update.message.reply_text(response)
    
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
        
    except Exception as e:
        logger.error(f"Error saving to database: {e}")
        db.rollback()
    finally:
        db.close()

@require_auth
async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to delete all user data - REQUIRES AUTH"""
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not check_admin(user.id):
        await update.message.reply_text("⛔ This command is only for admins.")
        return
    
    db = get_db()
    try:
        db.query(Conversation).filter_by(telegram_id=telegram_id).delete()
        db.query(User).filter_by(telegram_id=telegram_id).delete()
        db.commit()
        await update.message.reply_text("🗑️ All your data has been deleted. Start fresh!")
    except Exception as e:
        logger.error(f"Error deleting data: {e}")
        db.rollback()
        await update.message.reply_text("❌ Error deleting data.")
    finally:
        db.close()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors gracefully"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    if isinstance(context.error, Conflict):
        logger.error("Conflict error detected. This means another instance is running.")
        return
    
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Network error. Will retry automatically...")
        return

def main():
    init_db()
    
    if not TELEGRAM_TOKEN:
        logger.error("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(False)
        .build()
    )
    
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("code", enter_code))
    application.add_handler(CommandHandler("gencode", generate_code))
    application.add_handler(CommandHandler("codes", list_codes))
    application.add_handler(CommandHandler("delete_my_data", delete_my_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🔒 Bot running with STRICT authorization!")
    logger.info("Only users with valid codes can access.")
    
    if RAILWAY_STATIC_URL:
        webhook_url = f"{RAILWAY_STATIC_URL}/webhook"
        logger.info(f"Using webhook at {webhook_url}")
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )
    else:
        logger.info("Using polling (development mode)")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

if __name__ == "__main__":
    main()
