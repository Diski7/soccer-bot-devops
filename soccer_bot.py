import logging
import os
import sys
import requests
import asyncio
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# Database code inline
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta

print("="*50)
print("BOT STARTING UP...")
print("="*50)

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    username = Column(String)
    first_name = Column(String)
    message_count = Column(Integer, default=0)
    last_active = Column(DateTime, default=datetime.utcnow)
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
print(f"DATABASE_URL: {DATABASE_URL[:30]}...")

# Handle Railway's postgres:// vs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    print("Converted postgres:// to postgresql://")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """Create tables"""
    print("Creating database tables...")
    Base.metadata.create_all(engine)
    print("Database tables created!")

def get_user_stats(telegram_id):
    """Get user statistics"""
    print(f"Getting stats for user: {telegram_id}")
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(telegram_id=str(telegram_id)).first()
        
        if user:
            count = user.message_count or 0
            history = session.query(Conversation).filter_by(telegram_id=str(telegram_id)).order_by(Conversation.timestamp.desc()).limit(5).all()
            print(f"Found user with {count} messages")
            return count, history
        print("User not found in database")
        return 0, []
    finally:
        session.close()

def get_all_users():
    """Get all users for agent features"""
    session = SessionLocal()
    try:
        users = session.query(User).all()
        return users
    finally:
        session.close()

def get_inactive_users(days=7):
    """Get users who haven't been active in X days"""
    session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        users = session.query(User).filter(User.last_active < cutoff).all()
        return users
    finally:
        session.close()

def save_conversation(telegram_id, username, first_name, user_msg, bot_msg):
    """Save conversation to database"""
    print(f"SAVING CONVERSATION for user {telegram_id}")
    session = SessionLocal()
    
    try:
        # Update or create user
        user = session.query(User).filter_by(telegram_id=str(telegram_id)).first()
        if not user:
            print(f"Creating new user: {telegram_id}")
            user = User(telegram_id=str(telegram_id), username=username, first_name=first_name, message_count=0)
            session.add(user)
        
        # FIX: Handle None value for message_count
        if user.message_count is None:
            user.message_count = 0
        
        user.message_count += 1
        user.last_active = datetime.utcnow()  # Update last active time
        print(f"User message count: {user.message_count}")
        
        # Save conversation
        conv = Conversation(
            telegram_id=str(telegram_id),
            user_message=user_msg,
            bot_response=bot_msg
        )
        session.add(conv)
        print("Committing to database...")
        
        session.commit()
        print("SAVED SUCCESSFULLY!")
    except Exception as e:
        print(f"ERROR SAVING: {e}")
        session.rollback()
        import traceback
        traceback.print_exc()
    finally:
        session.close()

# Bot configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-3.5-turbo"

print(f"TELEGRAM_TOKEN exists: {bool(TELEGRAM_TOKEN)}")
print(f"OPENAI_API_KEY exists: {bool(OPENAI_API_KEY)}")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def get_openai_response(message):
    """Get response from OpenAI API"""
    if not OPENAI_API_KEY:
        return "OpenAI API key not configured."
    
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
        print(f"OpenAI error: {e}")
        return f"Error: {str(e)}"

# AGENT FEATURES - Simple version without job queue (to avoid errors)

async def welcome_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agent feature: Enhanced welcome for new users"""
    user = update.effective_user
    telegram_id = user.id
    
    # Check if user is new (has no conversations)
    count, _ = get_user_stats(telegram_id)
    
    if count == 0:
        # New user - send special welcome
        welcome_msg = f"""🎉 Welcome {user.first_name or 'there'}!

I'm your AI assistant powered by OpenAI. I can:
• Chat with you about anything
• Remember our conversations
• Show you stats with /stats

Try sending me a message!"""
        
        await update.message.reply_text(welcome_msg)
        print(f"AGENT: Sent welcome to new user {telegram_id}")

# Command handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"COMMAND: /start from user {update.effective_user.id}")
    await welcome_new_user(update, context)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"COMMAND: /stats from user {update.effective_user.id}")
    telegram_id = update.effective_user.id
    count, history = get_user_stats(telegram_id)
    
    if count == 0:
        await update.message.reply_text("No stats yet. Start chatting first!")
    else:
        msg = f"📊 Your Stats:\nMessages sent: {count}\n\nRecent conversations:\n"
        for conv in history:
            msg += f"- You: {conv.user_message[:30]}...\n"
        await update.message.reply_text(msg)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to send message to all users (agent feature)"""
    # Only allow for specific admin (you can change this)
    admin_id = os.getenv("ADMIN_TELEGRAM_ID", "")
    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text("⛔ Admin only command!")
        return
    
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    users = get_all_users()
    sent_count = 0
    
    for user in users:
        try:
            await context.bot.send_message(chat_id=int(user.telegram_id), text=f"📢 Announcement:\n\n{message}")
            sent_count += 1
        except Exception as e:
            print(f"Broadcast failed to {user.telegram_id}: {e}")
    
    await update.message.reply_text(f"Broadcast sent to {sent_count} users!")

async def agent_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show agent status"""
    users = get_all_users()
    inactive = get_inactive_users(days=3)
    
    msg = f"""🤖 Agent Status:
    
Total users: {len(users)}
Inactive (3+ days): {len(inactive)}

Agent features active:
✅ User tracking
✅ Stats monitoring
✅ Admin broadcast
"""
    await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"\n{'='*50}")
    print(f"MESSAGE RECEIVED from {update.effective_user.id}")
    print(f"Text: {update.message.text[:50]}...")
    print(f"{'='*50}\n")
    
    user_message = update.message.text
    user = update.effective_user
    
    await update.message.chat.send_action(action="typing")
    
    # Get AI response from OpenAI
    print("Getting AI response...")
    ai_response = get_openai_response(user_message)
    print(f"AI response: {ai_response[:50]}...")
    
    await update.message.reply_text(ai_response)
    
    # Save to database
    print("Saving to database...")
    save_conversation(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        user_msg=user_message,
        bot_msg=ai_response
    )
    print("Done!\n")

def main():
    print("\n" + "="*50)
    print("MAIN FUNCTION STARTING")
    print("="*50 + "\n")
    
    # Initialize database tables on startup!
    print("Initializing database...")
    init_db()
    print("Database initialized!\n")
    
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    # Create application (simpler version without job queue)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("agent", agent_status))
    
    # This should catch all text messages that are not commands
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Handlers registered:")
    print("- /start command")
    print("- /stats command")
    print("- /broadcast command (admin)")
    print("- /agent command")
    print("- text messages (not commands)")
    print("\nBot is running with AGENT features! Send a message on Telegram!\n")
    
    application.run_polling()

if __name__ == "__main__":
    main()
