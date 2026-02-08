import logging
import os
import sys
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# Database code inline
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    username = Column(String)
    first_name = Column(String)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

class Conversation(Base):
    __tablename__ = 'conversations'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String)
    user_message = Column(Text)
    bot_response = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Get database URL from environment
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')

# Handle Railway's postgres:// vs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """Create tables"""
    Base.metadata.create_all(engine)

def get_user_stats(telegram_id):
    """Get user statistics"""
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=str(telegram_id)).first()
    
    if user:
        count = user.message_count
        history = session.query(Conversation).filter_by(telegram_id=str(telegram_id)).order_by(Conversation.timestamp.desc()).limit(5).all()
        session.close()
        return count, history
    session.close()
    return 0, []

def save_conversation(telegram_id, username, first_name, user_msg, bot_msg):
    """Save conversation to database"""
    session = SessionLocal()
    
    # Update or create user
    user = session.query(User).filter_by(telegram_id=str(telegram_id)).first()
    if not user:
        user = User(telegram_id=str(telegram_id), username=username, first_name=first_name)
        session.add(user)
    
    user.message_count += 1
    
    # Save conversation
    conv = Conversation(
        telegram_id=str(telegram_id),
        user_message=user_msg,
        bot_response=bot_msg
    )
    session.add(conv)
    
    session.commit()
    session.close()

# Bot configuration - all from environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-3.5-turbo"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def get_openai_response(message):
    """Get response from OpenAI API"""
    if not OPENAI_API_KEY:
        return "OpenAI API key not configured. Please set OPENAI_API_KEY environment variable."
    
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": message}
        ],
        "max_tokens": 500
    }
    
    try:
        response = requests.post(OPENAI_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"Error getting AI response: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm your AI bot powered by OpenAI. Send me a message!")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    count, history = get_user_stats(telegram_id)
    
    if count == 0:
        await update.message.reply_text("No stats yet. Start chatting first!")
    else:
        msg = f"📊 Your Stats:\nMessages sent: {count}\n\nRecent conversations:\n"
        for conv in history:
            msg += f"- You: {conv.user_message[:30]}...\n"
        await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user = update.effective_user
    
    await update.message.chat.send_action(action="typing")
    
    # Get AI response from OpenAI
    ai_response = get_openai_response(user_message)
    await update.message.reply_text(ai_response)
    
    # Save to database
    save_conversation(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        user_msg=user_message,
        bot_msg=ai_response
    )

def main():
    # Initialize database tables on startup!
    print("Initializing database...")
    init_db()
    print("Database initialized!")
    
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is running with OpenAI! Message it on Telegram!")
    application.run_polling()

if __name__ == "__main__":
    main()
