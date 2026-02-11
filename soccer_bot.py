import logging
import os
import sys
import time
import openai
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.error import Conflict, NetworkError, TimedOut
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Enum, desc, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum
import asyncio

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

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, index=True)
    user_message = Column(Text)
    bot_response = Column(Text)
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
        
        if 'conversations' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('conversations')]
            required_cols = ['user_message', 'bot_response', 'timestamp']
            if not all(col in columns for col in required_cols):
                logger.info("Schema outdated, recreating tables...")
                Base.metadata.drop_all(engine)
        
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

def get_all_memory(telegram_id: str, max_messages: int = 20):
    """Fetch recent conversations for context"""
    db = get_db()
    try:
        history = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).order_by(desc(Conversation.timestamp)).limit(max_messages).all()
        return list(reversed(history))
    finally:
        db.close()

def get_memory_summary(telegram_id: str):
    """Create summary of entire lifetime relationship"""
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
        
        if first_convo:
            time_since_first = datetime.utcnow() - first_convo.timestamp
            days_together = time_since_first.days
            years_together = days_together // 365
            months_together = (days_together % 365) // 30
        else:
            days_together = 0
            years_together = 0
            months_together = 0
            time_since_first = timedelta(0)
            
        return {
            "total_messages": total_convos,
            "first_chat": first_convo.timestamp.strftime("%B %d, %Y at %H:%M") if first_convo else "Today",
            "last_chat": last_convo.timestamp.strftime("%B %d, %Y at %H:%M") if last_convo else "Never",
            "user_name": user.first_name if user else "Friend",
            "days_together": days_together,
            "years_together": years_together,
            "months_together": months_together,
            "time_since_first": format_duration(time_since_first) if first_convo else "Just now"
        }
    finally:
        db.close()

def format_duration(td: timedelta) -> str:
    days = td.days
    years = days // 365
    months = (days % 365) // 30
    remaining_days = days % 30
    
    parts = []
    if years > 0:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months > 0:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if remaining_days > 0 and years == 0:
        parts.append(f"{remaining_days} day{'s' if remaining_days != 1 else ''}")
    
    return ", ".join(parts) if parts else "Just started"

def get_recent_activity(telegram_id: str) -> dict:
    db = get_db()
    try:
        now = datetime.utcnow()
        lifetime = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id
        ).count()
        
        last_hour = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id,
            Conversation.timestamp >= now - timedelta(hours=1)
        ).count()
        
        last_24h = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id,
            Conversation.timestamp >= now - timedelta(hours=24)
        ).count()
        
        last_7d = db.query(Conversation).filter(
            Conversation.telegram_id == telegram_id,
            Conversation.timestamp >= now - timedelta(days=7)
        ).count()
        
        return {
            "lifetime": lifetime,
            "last_hour": last_hour,
            "last_24h": last_24h,
            "last_7d": last_7d
        }
    finally:
        db.close()

def get_llm_response(user_message: str, conversation_history: list, memory_summary: dict) -> str:
    """Get response from LLM with memory context"""
    
    # Build conversation context from memory
    context = "You are a knowledgeable soccer assistant with LIFETIME memory. You remember all past conversations with this user.\n\n"
    
    if memory_summary["total_messages"] > 0:
        context += f"User: {memory_summary['user_name']}\n"
        context += f"Relationship: {memory_summary['time_since_first']}\n"
        context += f"Total messages: {memory_summary['total_messages']}\n\n"
    
    # Add recent conversation history
    if conversation_history:
        context += "Recent conversation history:\n"
        for conv in conversation_history[-10:]:  # Last 10 messages
            context += f"User: {conv.user_message}\n"
            context += f"Assistant: {conv.bot_response}\n\n"
    
    context += f"Current user message: {user_message}\n\n"
    context += "Respond as a helpful soccer expert. Be conversational and reference previous discussions if relevant."
    
    try:
        if USE_OPENAI:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful soccer assistant with perfect memory of all past conversations."},
                    {"role": "user", "content": context}
                ],
                max_tokens=500,
                temperature=0.7
            )
            return response.choices[0].message.content
            
        elif USE_OLLAMA:
            response = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama2",
                    "prompt": context,
                    "stream": False,
                    "max_tokens": 500
                },
                timeout=30
            )
            return response.json().get("response", "I couldn't generate a response right now.")
        
        else:
            # Fallback without LLM
            return None
            
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    
    memory = get_memory_summary(telegram_id)
    activity = get_recent_activity(telegram_id)
    
    if memory["total_messages"] > 0:
        welcome = (
            f"Welcome back {memory['user_name']}! 🎉\n\n"
            f"🧠 I have LIFETIME memory - I never forget!\n"
            f"📅 We've been chatting for: {memory['time_since_first']}\n"
            f"💬 Total messages: {memory['total_messages']}\n"
            f"🎂 First chat: {memory['first_chat']}\n\n"
        )
        
        if memory["years_together"] >= 1:
            welcome += f"🎉 Happy {memory['years_together']}-year anniversary! 🎂\n\n"
        
        welcome += "Ask me anything about soccer!"
    else:
        welcome = (
            "Hello! I'm your soccer assistant with LIFETIME memory! ⚽🧠\n\n"
            "I will remember every single conversation we have - forever. "
            "Ask me anything about soccer, formations, tactics, players, or matches!"
        )
    
    await update.message.reply_text(welcome)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    current_message = update.message.text
    
    # Get memory
    history = get_all_memory(telegram_id, max_messages=20)
    memory = get_memory_summary(telegram_id)
    activity = get_recent_activity(telegram_id)
    
    current_lower = current_message.lower()
    
    # Check for memory-related commands first
    if "remember" in current_lower or "recall" in current_lower:
        response = (
            f"Of course! I have LIFETIME memory 🧠\n\n"
            f"📅 We've known each other for {memory['time_since_first']}\n"
            f"💬 {memory['total_messages']} total messages\n"
            f"🎂 Since: {memory['first_chat']}\n\n"
            f"I remember every single conversation. What soccer topic would you like to discuss?"
        )
    
    elif "how long" in current_lower or "history" in current_lower:
        response = (
            f"📊 Our Lifetime History:\n\n"
            f"⏰ Together for: {memory['time_since_first']}\n"
            f"🎂 First chat: {memory['first_chat']}\n"
            f"💬 Total messages: {memory['total_messages']}\n"
            f"🧠 Memory type: LIFETIME (no expiration)"
        )
    
    elif "stats" in current_lower or "numbers" in current_lower:
        response = (
            f"⚽ Your Lifetime Stats:\n\n"
            f"🏆 Total messages: {activity['lifetime']}\n"
            f"🔥 Last hour: {activity['last_hour']}\n"
            f"📅 Today: {activity['last_24h']}\n"
            f"📊 This week: {activity['last_7d']}\n"
            f"⏰ Journey length: {memory['time_since_first']}"
        )
    
    else:
        # Get LLM response with memory context
        llm_response = get_llm_response(current_message, history, memory)
        
        if llm_response:
            response = llm_response
        else:
            # Fallback responses if LLM fails
            if memory["total_messages"] > 10:
                response = f"Welcome back {memory['user_name']}! {memory['total_messages']} messages in our lifetime archive. What soccer topics shall we explore today?"
            elif memory["total_messages"] > 0:
                response = f"Welcome back {memory['user_name']}! Message #{memory['total_messages'] + 1} in our lifetime memory. What would you like to chat about?"
            else:
                response = "Hi! Ask me anything about soccer - formations, tactics, players, or matches!"
    
    # Send response
    await update.message.reply_text(response)
    
    # SAVE TO LIFETIME MEMORY
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
                role=UserRole.ADMIN if check_admin(user.id) else UserRole.USER
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

async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to delete all user data"""
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not check_admin(user.id):
        await update.message.reply_text("This command is only available to admins.")
        return
    
    db = get_db()
    try:
        db.query(Conversation).filter_by(telegram_id=telegram_id).delete()
        db.query(User).filter_by(telegram_id=telegram_id).delete()
        db.commit()
        await update.message.reply_text("All your data has been deleted. Start fresh!")
    except Exception as e:
        logger.error(f"Error deleting data: {e}")
        db.rollback()
        await update.message.reply_text("Error deleting data.")
    finally:
        db.close()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors gracefully"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    if isinstance(context.error, Conflict):
        logger.error("Conflict error detected. This means another instance is running.")
        # Don't crash, just log it
        return
    
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Network error. Will retry automatically...")
        return

def main():
    init_db()
    
    if not TELEGRAM_TOKEN:
        logger.error("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    # Build application
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(False)
        .build()
    )
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("delete_my_data", delete_my_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot running with LIFETIME memory + LLM!")
    logger.info(f"OpenAI: {USE_OPENAI}, Ollama: {USE_OLLAMA}")
    
    # Check if we should use webhooks (production) or polling (development)
    if RAILWAY_STATIC_URL:
        # Production: Use webhooks
        webhook_url = f"{RAILWAY_STATIC_URL}/webhook"
        logger.info(f"Using webhook at {webhook_url}")
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )
    else:
        # Development: Use polling
        logger.info("Using polling (development mode)")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

if __name__ == "__main__":
    main()
