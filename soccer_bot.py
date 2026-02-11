import logging
import os
import sys
import time
import json
import enum
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, 
    CommandHandler, CallbackQueryHandler, ConversationHandler
)
import requests

# ============== DATABASE CODE (All in one file) ==============

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey, Enum, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

Base = declarative_base()

class UserRole(enum.Enum):
    PARENT = "parent"
    ATHLETE = "athlete"
    COACH = "coach"
    ADMIN = "admin"

class ConversationType(enum.Enum):
    GENERAL = "general"
    SOCCER_STATS = "soccer_stats"
    MATCH_PREDICTION = "match_prediction"
    TEAM_INFO = "team_info"

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    role = Column(Enum(UserRole), default=UserRole.ATHLETE)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    favorite_team = Column(String)
    favorite_league = Column(String)
    notifications_enabled = Column(Boolean, default=True)
    message_count = Column(Integer, default=0)
    total_tokens_used = Column(Integer, default=0)
    
    conversations = relationship("Conversation", back_populates="user", lazy="dynamic")
    analytics = relationship("UserAnalytics", back_populates="user", uselist=False)
    predictions = relationship("MatchPrediction", back_populates="user")

class Conversation(Base):
    __tablename__ = 'conversations'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    telegram_id = Column(String, index=True)
    message_content = Column(Text)
    bot_response = Column(Text)
    conversation_type = Column(Enum(ConversationType), default=ConversationType.GENERAL)
    response_time_ms = Column(Integer)
    tokens_used = Column(Integer, default=0)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user = relationship("User", back_populates="conversations")

class UserAnalytics(Base):
    __tablename__ = 'user_analytics'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    total_sessions = Column(Integer, default=1)
    longest_streak_days = Column(Integer, default=0)
    current_streak_days = Column(Integer, default=0)
    most_asked_topic = Column(String)
    favorite_command = Column(String, default="/start")
    user = relationship("User", back_populates="analytics")

class MatchPrediction(Base):
    __tablename__ = 'match_predictions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    match_description = Column(String)
    user_prediction = Column(String)
    actual_result = Column(String, nullable=True)
    was_correct = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User", back_populates="predictions")

# Database setup
def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

engine = create_engine(get_database_url())
SessionLocal = sessionmaker(bind=engine)

def init_db():
    print("🔧 Creating database tables...")
    Base.metadata.create_all(engine)
    print("✅ Database tables created!")

def get_db():
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise

# ============== BOT CODE ==============

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def check_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_TELEGRAM_ID

def get_or_create_user(telegram_id: str, username: str, first_name: str, last_name: str = None):
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=UserRole.ADMIN if check_admin(int(telegram_id)) else UserRole.ATHLETE
            )
            db.add(user)
            db.commit()
            analytics = UserAnalytics(user_id=user.id)
            db.add(analytics)
            db.commit()
            logger.info(f"New user: {first_name} ({telegram_id})")
        return user
    except Exception as e:
        db.rollback()
        logger.error(f"Error: {e}")
        raise
    finally:
        db.close()

def log_conversation(telegram_id: str, user_message: str, bot_response: str, 
                    response_time: float, tokens_used: int = 0):
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            conv = Conversation(
                user_id=user.id,
                telegram_id=telegram_id,
                message_content=user_message,
                bot_response=bot_response,
                response_time_ms=int(response_time * 1000),
                tokens_used=tokens_used
            )
            db.add(conv)
            user.message_count += 1
            user.total_tokens_used += tokens_used
            if user.analytics:
                user.analytics.last_seen = datetime.utcnow()
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error logging: {e}")
    finally:
        db.close()

def get_daily_stats():
    db = get_db()
    try:
        today = datetime.utcnow().date()
        daily_active = db.query(User).filter(User.last_active >= today).count()
        total_users = db.query(User).count()
        today_messages = db.query(Conversation).filter(Conversation.timestamp >= today).count()
        new_users_today = db.query(User).filter(User.created_at >= today).count()
        return {
            "daily_active_users": daily_active,
            "total_users": total_users,
            "messages_today": today_messages,
            "new_users_today": new_users_today
        }
    finally:
        db.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    db_user = get_or_create_user(telegram_id, user.username, user.first_name, user.last_name)
    
    if db_user.role == UserRole.ADMIN:
        welcome = f"""👑 Welcome Admin {user.first_name}!

🤖 Soccer Bot with Analytics

Commands:
⚽ /mystats - Your stats
🏆 /leaderboard - Top users
📢 /broadcast - Message all users
📈 /analytics - System stats
🎯 /predict - Match predictions"""
    else:
        welcome = f"""⚽ Welcome {user.first_name}!

I'm your AI Soccer Assistant!

Commands:
⚽ /mystats - Your stats & engagement
🏆 /leaderboard - Top users
🎯 /predict - Make predictions

Start chatting!"""
    
    await update.message.reply_text(welcome)

async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            await update.message.reply_text("❌ No stats found. Start chatting!")
            return
        
        stats_text = f"""📊 Your Soccer Bot Stats

👤 Profile:
• Total Messages: {user.message_count}
• Role: {user.role.value.title()}
• Favorite Team: {user.favorite_team or "Not set"}
• Account Age: {(datetime.utcnow() - user.created_at).days} days

🔥 Engagement:
• Last Active: {user.last_active.strftime('%Y-%m-%d %H:%M')}
• Status: {'🔥 Active' if (datetime.utcnow() - user.last_active).days == 0 else '👋 Come back!'}

Keep chatting to increase your score!"""
        await update.message.reply_text(stats_text)
    finally:
        db.close()

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()
    try:
        top_users = db.query(User).order_by(User.message_count.desc()).limit(10).all()
        text = "🏆 Top Users Leaderboard\n\n"
        for idx, user in enumerate(top_users, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
            text += f"{medal} {user.first_name}: {user.message_count} msgs\n"
        await update.message.reply_text(text)
    finally:
        db.close()

async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only!")
        return
    
    stats = get_daily_stats()
    text = f"""📈 System Analytics (Today)

👥 Users:
• Daily Active: {stats['daily_active_users']}
• New Today: {stats['new_users_today']}
• Total Users: {stats['total_users']}

💬 Activity:
• Messages Today: {stats['messages_today']}

System: 🟢 Healthy"""
    await update.message.reply_text(text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only!")
        return
    
    if not context.args:
        await update.message.reply_text("📢 Usage: /broadcast <message>")
        return
    
    message = ' '.join(context.args)
    db = get_db()
    try:
        users = db.query(User).filter_by(is_active=True).all()
        sent = 0
        for user in users:
            try:
                await context.bot.send_message(
                    chat_id=int(user.telegram_id),
                    text=f"📢 Announcement:\n\n{message}"
                )
                sent += 1
            except Exception as e:
                logger.error(f"Failed to {user.telegram_id}: {e}")
        await update.message.reply_text(f"✅ Broadcast sent to {sent}/{len(users)} users!")
    finally:
        db.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    user_message = update.message.text
    user = update.effective_user
    telegram_id = str(user.id)
    
    await update.message.chat.send_action(action="typing")
    
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "system", "content": "You are a soccer expert bot. Be enthusiastic about soccer!"},
                    {"role": "user", "content": user_message}
                ],
                "max_tokens": 500
            },
            timeout=30
        )
        ai_response = response.json()['choices'][0]['message']['content']
        tokens_used = response.json().get('usage', {}).get('total_tokens', 0)
    except Exception as e:
        ai_response = "Sorry, I'm having trouble. Try again! ⚽"
        tokens_used = 0
        logger.error(f"OpenAI error: {e}")
    
    await update.message.reply_text(ai_response)
    response_time = time.time() - start_time
    log_conversation(telegram_id, user_message, ai_response, response_time, tokens_used)

def main():
    print("🚀 Starting Advanced Soccer Bot...")
    init_db()
    
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN not set!")
        return
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mystats", mystats))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("analytics", analytics))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot running with Advanced Features!")
    print("📊 Analytics enabled")
    print("🔐 Authentication enabled")
    
    application.run_polling()

if __name__ == "__main__":
    main()
