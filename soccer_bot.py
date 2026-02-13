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
            "is_new_user": total_convos == 0
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
    # Check if message is exactly a greeting or starts with one
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
    
    # Add recent conversation history
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
    
    memory = get_memory_summary(telegram_id)
    
    if memory["is_new_user"]:
        welcome = "Hey! I'm your soccer buddy. Ask me anything about the beautiful game! ⚽"
    else:
        if memory["time_since_last"] and memory["time_since_last"].days > 7:
            welcome = f"Hey {memory['user_name']}! Long time no see. What's on your mind about soccer?"
        else:
            welcome = f"Hey {memory['user_name']}! What's up?"
    
    await update.message.reply_text(welcome)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    current_message = update.message.text
    
    history = get_recent_memory(telegram_id, max_messages=6)
    memory = get_memory_summary(telegram_id)
    
    current_lower = current_message.lower()
    
    # Check if it's a greeting first
    if is_greeting(current_message):
        response = "Hi, how may I assist you?"
    
    # Stats/memory commands
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
        # Normal conversation - get LLM response
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

    # Send response
    await update.message.reply_text(response)
    
    # Save to memory
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
    application.add_handler(CommandHandler("delete_my_data", delete_my_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot running with greeting detection!")
    
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
