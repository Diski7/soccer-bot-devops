from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum
import os

Base = declarative_base()

# User Roles Enum
class UserRole(enum.Enum):
    USER = "user"
    PREMIUM = "premium"
    ADMIN = "admin"

# Conversation Types
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
    
    # Authentication & Roles
    role = Column(Enum(UserRole), default=UserRole.USER)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    
    # Soccer Preferences
    favorite_team = Column(String)  # e.g., "Manchester United"
    favorite_league = Column(String)  # e.g., "Premier League"
    notifications_enabled = Column(Boolean, default=True)
    
    # Analytics
    message_count = Column(Integer, default=0)
    total_tokens_used = Column(Integer, default=0)  # Track API usage
    
    # Relationships
    conversations = relationship("Conversation", back_populates="user", lazy="dynamic")
    analytics = relationship("UserAnalytics", back_populates="user", uselist=False)
    predictions = relationship("MatchPrediction", back_populates="user")

class Conversation(Base):
    __tablename__ = 'conversations'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    telegram_id = Column(String, index=True)  # For quick lookups
    
    message_content = Column(Text)
    bot_response = Column(Text)
    conversation_type = Column(Enum(ConversationType), default=ConversationType.GENERAL)
    
    # Analytics
    response_time_ms = Column(Integer)  # How long the bot took to respond
    tokens_used = Column(Integer, default=0)  # OpenAI tokens used
    
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="conversations")

class UserAnalytics(Base):
    """Track detailed analytics per user"""
    __tablename__ = 'user_analytics'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True)
    
    # Engagement metrics
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    total_sessions = Column(Integer, default=1)
    longest_streak_days = Column(Integer, default=0)
    current_streak_days = Column(Integer, default=0)
    
    # Content preferences
    most_asked_topic = Column(String)
    favorite_command = Column(String, default="/start")
    
    # Relationship
    user = relationship("User", back_populates="analytics")

class MatchPrediction(Base):
    """Track soccer match predictions"""
    __tablename__ = 'match_predictions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    
    match_description = Column(String)  # "Man Utd vs Liverpool"
    user_prediction = Column(String)  # "Man Utd wins 2-1"
    actual_result = Column(String, nullable=True)  # Filled after match
    was_correct = Column(Boolean, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    user = relationship("User", back_populates="predictions")

class SystemAnalytics(Base):
    """Global system-wide analytics"""
    __tablename__ = 'system_analytics'
    
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, default=datetime.utcnow)
    
    daily_active_users = Column(Integer, default=0)
    total_messages = Column(Integer, default=0)
    new_users = Column(Integer, default=0)
    avg_response_time_ms = Column(Float, default=0.0)
    
    # Command usage
    stats_command_count = Column(Integer, default=0)
    help_command_count = Column(Integer, default=0)
    other_commands = Column(Text)  # JSON string of command counts

# Database connection setup
def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

engine = create_engine(get_database_url())
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """Initialize database with all tables"""
    print("🔧 Creating database tables...")
    Base.metadata.create_all(engine)
    print("✅ Database tables created successfully!")

def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise

# Helper functions for analytics
def update_user_activity(telegram_id: str):
    """Update user last_active timestamp"""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.last_active = datetime.utcnow()
            user.message_count += 1
            session.commit()
    finally:
        session.close()

def get_daily_stats():
    """Get system-wide daily statistics"""
    session = SessionLocal()
    try:
        today = datetime.utcnow().date()
        
        # Count today's active users
        daily_active = session.query(User).filter(
            User.last_active >= today
        ).count()
        
        # Count total users
        total_users = session.query(User).count()
        
        # Count today's messages
        today_messages = session.query(Conversation).filter(
            Conversation.timestamp >= today
        ).count()
        
        # Count new users today
        new_users_today = session.query(User).filter(
            User.created_at >= today
        ).count()
        
        return {
            "daily_active_users": daily_active,
            "total_users": total_users,
            "messages_today": today_messages,
            "new_users_today": new_users_today
        }
    finally:
        session.close()
