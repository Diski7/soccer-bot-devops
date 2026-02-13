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
    language = Column(String, default="en")  # Language code
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

# MULTI-LANGUAGE TRANSLATIONS
TRANSLATIONS = {
    "en": {
        "welcome_new": "Hey! I'm your AI assistant. Ask me anything - I remember our conversations. What's on your mind?",
        "welcome_back": "Hey {name}! What's up?",
        "welcome_back_long": "Hey {name}! Been a while. What's up?",
        "access_denied": "🔒 **Private Bot**\n\nInvitation only.\n\n🔑 `/code YOURCODE`",
        "code_prompt": "🔑 `/code YOURCODE`",
        "code_accepted": "✅ **Welcome!**\n\nI'm your assistant. Ask me anything\n• Tech, science, business, coding\n• Advice, writing, analysis\n• Sports, history, life questions\n\nWhat would you like to talk about?",
        "already_authorized": "✅ Already have access!",
        "invalid_code": "❌ Invalid code.",
        "code_expired": "❌ Code expired.",
        "code_max_uses": "❌ Code max uses reached.",
        "code_used": "❌ You already used this code.",
        "greeting": "Hi, how may I assist you?",
        "stats": "We've chatted {count} times. What's up?",
        "remember": "We've talked about various things. What specifically?",
        "new_user_prompt": "I'm here to help. What would you like to know?",
        "returning_user_prompt": "Tell me more.",
        "admin_only": "⛔ Admin only.",
        "code_generated": "🎟️ **Code Generated**\n\n`{code}`\nDuration: {duration}\nExpires: {expires}\nUses: {uses}",
        "no_codes": "No active codes.",
        "active_codes": "🎟️ **Active Codes:**\n\n",
        "data_deleted": "🗑️ Data deleted.",
        "rate_limit": "⏱️ Too many messages. Slow down!",
        "error": "❌ Error. Try again.",
        "language_set": "✅ Language set to English",
        "language_prompt": "🌍 **Select Language:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "af": {
        "welcome_new": "Hallo! Ek is jou AI-assistent. Vra my enigiets - ek onthou ons gesprekke. Wat is aan die gang?",
        "welcome_back": "Hé {name}! Hoe gaan dit?",
        "welcome_back_long": "Hé {name}! Lanklaas. Hoe gaan dit?",
        "access_denied": "🔒 **Privaat Bot**\n\nSlegs op uitnodiging.\n\n🔑 `/code JOUKODE`",
        "code_prompt": "🔑 `/code JOUKODE`",
        "code_accepted": "✅ **Welkom!**\n\nEk is jou assistent. Vra my enigiets\n• Tegnologie, wetenskap, besigheid, kode\n• Advies, skryf, ontleding\n• Sport, geskiedenis, lewensvrae\n\nWaaroor wil jy praat?",
        "already_authorized": "✅ Jy het reeds toegang!",
        "invalid_code": "❌ Ongeldige kode.",
        "code_expired": "❌ Kode het verval.",
        "code_max_uses": "❌ Kode maksimum gebruik bereik.",
        "code_used": "❌ Jy het hierdie kode reeds gebruik.",
        "greeting": "Hallo, hoe kan ek jou help?",
        "stats": "Ons het {count} keer gesels. Hoe gaan dit?",
        "remember": "Ons het oor verskeie dinge gepraat. Wat spesifiek?",
        "new_user_prompt": "Ek is hier om te help. Wat wil jy weet?",
        "returning_user_prompt": "Vertel my meer.",
        "admin_only": "⛔ Slegs admin.",
        "code_generated": "🎟️ **Kode Geskep**\n\n`{code}`\nDuur: {duration}\nVerval: {expires}\nGebruik: {uses}",
        "no_codes": "Geen aktiewe kodes nie.",
        "active_codes": "🎟️ **Aktiewe Kodes:**\n\n",
        "data_deleted": "🗑️ Data uitgevee.",
        "rate_limit": "⏱️ Te veel boodskappe. Stadiger!",
        "error": "❌ Fout. Probeer weer.",
        "language_set": "✅ Taal gestel na Afrikaans",
        "language_prompt": "🌍 **Kies Taal:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "fr": {
        "welcome_new": "Salut! Je suis votre assistant IA. Demandez-moi n'importe quoi - je me souviens de nos conversations. Qu'avez-vous en tête?",
        "welcome_back": "Salut {name}! Quoi de neuf?",
        "welcome_back_long": "Salut {name}! Ça fait longtemps. Quoi de neuf?",
        "access_denied": "🔒 **Bot Privé**\n\nSur invitation uniquement.\n\n🔑 `/code VOTRECODE`",
        "code_prompt": "🔑 `/code VOTRECODE`",
        "code_accepted": "✅ **Bienvenue!**\n\nJe suis votre assistant. Demandez-moi n'importe quoi\n• Technologie, science, business, code\n• Conseils, écriture, analyse\n• Sport, histoire, questions de vie\n\nDe quoi voulez-vous parler?",
        "already_authorized": "✅ Vous avez déjà accès!",
        "invalid_code": "❌ Code invalide.",
        "code_expired": "❌ Code expiré.",
        "code_max_uses": "❌ Utilisations maximales atteintes.",
        "code_used": "❌ Vous avez déjà utilisé ce code.",
        "greeting": "Bonjour, comment puis-je vous aider?",
        "stats": "Nous avons discuté {count} fois. Quoi de neuf?",
        "remember": "Nous avons parlé de divers sujets. Quoi spécifiquement?",
        "new_user_prompt": "Je suis là pour aider. Que voulez-vous savoir?",
        "returning_user_prompt": "Dites-m'en plus.",
        "admin_only": "⛔ Admin uniquement.",
        "code_generated": "🎟️ **Code Généré**\n\n`{code}`\nDurée: {duration}\nExpire: {expires}\nUtilisations: {uses}",
        "no_codes": "Aucun code actif.",
        "active_codes": "🎟️ **Codes Actifs:**\n\n",
        "data_deleted": "🗑️ Données supprimées.",
        "rate_limit": "⏱️ Trop de messages. Ralentissez!",
        "error": "❌ Erreur. Réessayez.",
        "language_set": "✅ Langue définie sur Français",
        "language_prompt": "🌍 **Choisir la Langue:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "es": {
        "welcome_new": "¡Hola! Soy tu asistente de IA. Pregúntame lo que sea - recuerdo nuestras conversaciones. ¿Qué tienes en mente?",
        "welcome_back": "¡Hola {name}! ¿Qué tal?",
        "welcome_back_long": "¡Hola {name}! Hace tiempo. ¿Qué tal?",
        "access_denied": "🔒 **Bot Privado**\n\nSolo con invitación.\n\n🔑 `/code TUCODIGO`",
        "code_prompt": "🔑 `/code TUCODIGO`",
        "code_accepted": "✅ **¡Bienvenido!**\n\nSoy tu asistente. Pregúntame lo que sea\n• Tecnología, ciencia, negocios, código\n• Consejos, escritura, análisis\n• Deportes, historia, preguntas de la vida\n\n¿De qué te gustaría hablar?",
        "already_authorized": "✅ ¡Ya tienes acceso!",
        "invalid_code": "❌ Código inválido.",
        "code_expired": "❌ Código expirado.",
        "code_max_uses": "❌ Usos máximos alcanzados.",
        "code_used": "❌ Ya usaste este código.",
        "greeting": "Hola, ¿cómo puedo ayudarte?",
        "stats": "Hemos charlado {count} veces. ¿Qué tal?",
        "remember": "Hemos hablado de varias cosas. ¿Qué específicamente?",
        "new_user_prompt": "Estoy aquí para ayudar. ¿Qué te gustaría saber?",
        "returning_user_prompt": "Cuéntame más.",
        "admin_only": "⛔ Solo admin.",
        "code_generated": "🎟️ **Código Generado**\n\n`{code}`\nDuración: {duration}\nExpira: {expires}\nUsos: {uses}",
        "no_codes": "No hay códigos activos.",
        "active_codes": "🎟️ **Códigos Activos:**\n\n",
        "data_deleted": "🗑️ Datos eliminados.",
        "rate_limit": "⏱️ Demasiados mensajes. ¡Más lento!",
        "error": "❌ Error. Inténtalo de nuevo.",
        "language_set": "✅ Idioma cambiado a Español",
        "language_prompt": "🌍 **Seleccionar Idioma:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "de": {
        "welcome_new": "Hey! Ich bin dein KI-Assistent. Frag mich alles - ich erinnere mich an unsere Gespräche. Was beschäftigt dich?",
        "welcome_back": "Hey {name}! Was geht?",
        "welcome_back_long": "Hey {name}! Lange nichts gehört. Was geht?",
        "access_denied": "🔒 **Privater Bot**\n\nNur auf Einladung.\n\n🔑 `/code DEINCODE`",
        "code_prompt": "🔑 `/code DEINCODE`",
        "code_accepted": "✅ **Willkommen!**\n\nIch bin dein Assistent. Frag mich alles\n• Technologie, Wissenschaft, Business, Code\n• Ratschläge, Schreiben, Analyse\n• Sport, Geschichte, Lebensfragen\n\nWorüber möchtest du sprechen?",
        "already_authorized": "✅ Du hast bereits Zugriff!",
        "invalid_code": "❌ Ungültiger Code.",
        "code_expired": "❌ Code abgelaufen.",
        "code_max_uses": "❌ Maximale Nutzung erreicht.",
        "code_used": "❌ Du hast diesen Code bereits verwendet.",
        "greeting": "Hallo, wie kann ich dir helfen?",
        "stats": "Wir haben {count} Mal geplaudert. Was geht?",
        "remember": "Wir haben über verschiedene Dinge gesprochen. Was genau?",
        "new_user_prompt": "Ich bin hier um zu helfen. Was möchtest du wissen?",
        "returning_user_prompt": "Erzähl mir mehr.",
        "admin_only": "⛔ Nur Admin.",
        "code_generated": "🎟️ **Code Erstellt**\n\n`{code}`\nDauer: {duration}\nLäuft ab: {expires}\nNutzungen: {uses}",
        "no_codes": "Keine aktiven Codes.",
        "active_codes": "🎟️ **Aktive Codes:**\n\n",
        "data_deleted": "🗑️ Daten gelöscht.",
        "rate_limit": "⏱️ Zu viele Nachrichten. Langsamer!",
        "error": "❌ Fehler. Versuche erneut.",
        "language_set": "✅ Sprache auf Deutsch gesetzt",
        "language_prompt": "🌍 **Sprache Wählen:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "pt": {
        "welcome_new": "Olá! Sou seu assistente de IA. Pergunte-me qualquer coisa - lembro nossas conversas. O que você tem em mente?",
        "welcome_back": "Ei {name}! E aí?",
        "welcome_back_long": "Ei {name}! Tempo sem ver. E aí?",
        "access_denied": "🔒 **Bot Privado**\n\nApenas por convite.\n\n🔑 `/code SEUCODIGO`",
        "code_prompt": "🔑 `/code SEUCODIGO`",
        "code_accepted": "✅ **Bem-vindo!**\n\nSou seu assistente. Pergunte-me qualquer coisa\n• Tecnologia, ciência, negócios, código\n• Conselhos, escrita, análise\n• Esportes, história, questões da vida\n\nSobre o que você gostaria de falar?",
        "already_authorized": "✅ Você já tem acesso!",
        "invalid_code": "❌ Código inválido.",
        "code_expired": "❌ Código expirado.",
        "code_max_uses": "❌ Usos máximos atingidos.",
        "code_used": "❌ Você já usou este código.",
        "greeting": "Olá, como posso ajudar?",
        "stats": "Conversamos {count} vezes. E aí?",
        "remember": "Falamos sobre várias coisas. O especificamente?",
        "new_user_prompt": "Estou aqui para ajudar. O que você gostaria de saber?",
        "returning_user_prompt": "Conte-me mais.",
        "admin_only": "⛔ Apenas admin.",
        "code_generated": "🎟️ **Código Gerado**\n\n`{code}`\nDuração: {duration}\nExpira: {expires}\nUsos: {uses}",
        "no_codes": "Nenhum código ativo.",
        "active_codes": "🎟️ **Códigos Ativos:**\n\n",
        "data_deleted": "🗑️ Dados deletados.",
        "rate_limit": "⏱️ Muitas mensagens. Mais devagar!",
        "error": "❌ Erro. Tente novamente.",
        "language_set": "✅ Idioma definido para Português",
        "language_prompt": "🌍 **Selecionar Idioma:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "zh": {
        "welcome_new": "嘿！我是你的AI助手。问我任何事——我记得我们的对话。你在想什么？",
        "welcome_back": "嘿{name}！最近怎么样？",
        "welcome_back_long": "嘿{name}！好久不见。最近怎么样？",
        "access_denied": "🔒 **私人机器人**\n\n仅限邀请。\n\n🔑 `/code 你的代码`",
        "code_prompt": "🔑 `/code 你的代码`",
        "code_accepted": "✅ **欢迎！**\n\n我是你的助手。问我任何事\n• 技术、科学、商业、编程\n• 建议、写作、分析\n• 体育、历史、生活问题\n\n你想聊什么？",
        "already_authorized": "✅ 你已经有权限了！",
        "invalid_code": "❌ 无效代码。",
        "code_expired": "❌ 代码已过期。",
        "code_max_uses": "❌ 已达到最大使用次数。",
        "code_used": "❌ 你已经使用过此代码。",
        "greeting": "你好，我能帮你什么？",
        "stats": "我们聊了{count}次。最近怎么样？",
        "remember": "我们聊过各种事情。具体是什么？",
        "new_user_prompt": "我在这里帮忙。你想知道什么？",
        "returning_user_prompt": "告诉我更多。",
        "admin_only": "⛔ 仅限管理员。",
        "code_generated": "🎟️ **代码已生成**\n\n`{code}`\n时长：{duration}\n过期：{expires}\n使用次数：{uses}",
        "no_codes": "没有活跃代码。",
        "active_codes": "🎟️ **活跃代码：**\n\n",
        "data_deleted": "🗑️ 数据已删除。",
        "rate_limit": "⏱️ 消息太多。慢一点！",
        "error": "❌ 错误。再试一次。",
        "language_set": "✅ 语言设置为中文",
        "language_prompt": "🌍 **选择语言：**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "ar": {
        "welcome_new": "مرحباً! أنا مساعدك الذكي. اسألني أي شيء - أتذكر محادثاتنا. ما الذي يدور في ذهنك؟",
        "welcome_back": "مرحباً {name}! ما الأخبار؟",
        "welcome_back_long": "مرحباً {name}! منذ زمن. ما الأخبار؟",
        "access_denied": "🔒 **بوت خاص**\n\nبالدعوة فقط.\n\n🔑 `/code الكود`",
        "code_prompt": "🔑 `/code الكود`",
        "code_accepted": "✅ **أهلاً بك!**\n\nأنا مساعدك. اسألني أي شيء\n• التكنولوجيا، العلوم، الأعمال، البرمجة\n• النصائح، الكتابة، التحليل\n• الرياضة، التاريخ، أسئلة الحياة\n\nماذا تريد أن تتحدث عن؟",
        "already_authorized": "✅ لديك صلاحية بالفعل!",
        "invalid_code": "❌ كود غير صالح.",
        "code_expired": "❌ الكود منتهي الصلاحية.",
        "code_max_uses": "❌ تم الوصول للحد الأقصى للاستخدام.",
        "code_used": "❌ لقد استخدمت هذا الكود مسبقاً.",
        "greeting": "مرحباً، كيف يمكنني مساعدتك؟",
        "stats": "تحدثنا {count} مرة. ما الأخبار؟",
        "remember": "تحدثنا عن أشياء مختلفة. ما بالتحديد؟",
        "new_user_prompt": "أنا هنا للمساعدة. ماذا تريد أن تعرف؟",
        "returning_user_prompt": "أخبرني المزيد.",
        "admin_only": "⛔ للمسؤول فقط.",
        "code_generated": "🎟️ **تم إنشاء الكود**\n\n`{code}`\nالمدة: {duration}\nالانتهاء: {expires}\nالاستخدامات: {uses}",
        "no_codes": "لا توجد أكواد نشطة.",
        "active_codes": "🎟️ **الأكواد النشطة:**\n\n",
        "data_deleted": "🗑️ تم حذف البيانات.",
        "rate_limit": "⏱️ رسائل كثيرة جداً. أبطأ!",
        "error": "❌ خطأ. حاول مرة أخرى.",
        "language_set": "✅ تم تعيين اللغة على العربية",
        "language_prompt": "🌍 **اختر اللغة：**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "hi": {
        "welcome_new": "नमस्ते! मैं आपका AI सहायक हूं। मुझसे कुछ भी पूछें - मुझे हमारी बातचीत याद है। आप क्या सोच रहे हैं?",
        "welcome_back": "हाय {name}! क्या चल रहा है?",
        "welcome_back_long": "हाय {name}! बहुत समय हो गया। क्या चल रहा है?",
        "access_denied": "🔒 **निजी बॉट**\n\nकेवल निमंत्रण पर।\n\n🔑 `/code आपका_कोड`",
        "code_prompt": "🔑 `/code आपका_कोड`",
        "code_accepted": "✅ **स्वागत है!**\n\nमैं आपका सहायक हूं। मुझसे कुछ भी पूछें\n• तकनीक, विज्ञान, व्यवसाय, कोडिंग\n• सलाह, लेखन, विश्लेषण\n• खेल, इतिहास, जीवन के सवाल\n\nआप किस बारे में बात करना चाहेंगे?",
        "already_authorized": "✅ आपके पास पहले से ही पहुंच है!",
        "invalid_code": "❌ अमान्य कोड।",
        "code_expired": "❌ कोड समाप्त हो गया।",
        "code_max_uses": "❌ अधिकतम उपयोग पहुंच गया।",
        "code_used": "❌ आप पहले ही इस कोड का उपयोग कर चुके हैं।",
        "greeting": "नमस्ते, मैं आपकी कैसे मदद कर सकता हूं?",
        "stats": "हमने {count} बार बातचीत की है। क्या चल रहा है?",
        "remember": "हमने विभिन्न चीजों के बारे में बात की है। विशेष रूप से क्या?",
        "new_user_prompt": "मैं मदद के लिए यहां हूं। आप क्या जानना चाहेंगे?",
        "returning_user_prompt": "मुझे और बताएं।",
        "admin_only": "⛔ केवल एडमिन।",
        "code_generated": "🎟️ **कोड बनाया गया**\n\n`{code}`\nअवधि: {duration}\nसमाप्ति: {expires}\nउपयोग: {uses}",
        "no_codes": "कोई सक्रिय कोड नहीं।",
        "active_codes": "🎟️ **सक्रिय कोड:**\n\n",
        "data_deleted": "🗑️ डेटा हटा दिया गया।",
        "rate_limit": "⏱️ बहुत सारे संदेश। धीमे!",
        "error": "❌ त्रुटि। फिर से प्रयास करें।",
        "language_set": "✅ भाषा हिंदी में सेट की गई",
        "language_prompt": "🌍 **भाषा चुनें：**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "nd": {
        "welcome_new": "Sawubona! Ngiyisibindi sakho se-AI. Ngibuze noma yini - ngiyakukhumbula ukuxoxisana kwethu. Yini oyicingayo?",
        "welcome_back": "Sawubona {name}! Kuhamba kanjani?",
        "welcome_back_long": "Sawubona {name}! Kudala ngakubona. Kuhamba kanjani?",
        "access_denied": "🔒 **Ibhothi Elizimele**\n\nImvume kuphela.\n\n🔑 `/code IKHODI YAKHO`",
        "code_prompt": "🔑 `/code IKHODI YAKHO`",
        "code_accepted": "✅ **Wamukelekile!**\n\nNgiyisibindi sakho. Ngibuze noma yini\n• Ithekhi, sayensi, ibhizinisi, ukubhala amakhodi\n• Iseluleko, ukubhala, ukuhlaziya\n• Ezamakhono, umlando, imibuzo yempilo\n\nUngathanda ukukhuluma ngani?",
        "already_authorized": "✅ Usuvele unemvume!",
        "invalid_code": "❌ Ikhodi engavumelekile.",
        "code_expired": "❌ Ikhodi iphelelwe yisikhathi.",
        "code_max_uses": "❌ Ukusetshenziswa okuningi kufikiwe.",
        "code_used": "❌ Usuvele usebenzise le khodi.",
        "greeting": "Sawubona, ngingakusiza kanjani?",
        "stats": "SIXOXISANE izikhathi ezingama-{count}. Kuhamba kanjani?",
        "remember": "Sikhulumisane ngokuningi. Ngokukhethekile ngakuphi na?",
        "new_user_prompt": "Ngingakusiza. Ungathanda ukwazi ini?",
        "returning_user_prompt": "Ngitshele okuningi.",
        "admin_only": "⛔ Abalawuli kuphela.",
        "code_generated": "🎟️ **Ikhodi Ikilwe**\n\n`{code}`\nIsikhathi: {duration}\nIphelelwa yisikhathi: {expires}\nUkusebenzisa: {uses}",
        "no_codes": "Azikho amakhodi asebenzayo.",
        "active_codes": "🎟️ **Amakhodi Asebenzayo:**\n\n",
        "data_deleted": "🗑️ Idatha icishiwe.",
        "rate_limit": "⏱️ Imiyalezo eminingi kakhulu. Yethula!",
        "error": "❌ Iphutha. Zama futhi.",
        "language_set": "✅ Ulimi lusetshwe yi-Ndebele",
        "language_prompt": "🌍 **Khetha Ulimi:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "sn": {
        "welcome_new": "Makadii! Ndiri mushandiri wako we-AI. Buditsa zvose - ndinokumbura zvataurirana. Unei mupfungwa?",
        "welcome_back": "Hezvo {name}! Muri sei?",
        "welcome_back_long": "Hezvo {name}! Yakareba isingonboni. Muri sei?",
        "access_denied": "🔒 **Bot Yemunhu**\n\nKungobvumidzwa vakakokwa.\n\n🔑 `/code KODI YAKO`",
        "code_prompt": "🔑 `/code KODI YAKO`",
        "code_accepted": "✅ **Makasununguka!**\n\nNdiri mushandiri wako. Buditsa zvose\n• Tech, science, bhizinesi, kutonga\n• Zano, kunyora, kutsanangura\n• Maso, nhoroondo, mibvunzo yepenyu\n\nUnoda kutaura nezvei?",
        "already_authorized": "✅ Makabvumidzwa kale!",
        "invalid_code": "❌ Kodi isina maturo.",
        "code_expired": "❌ Kodi yapera.",
        "code_max_uses": "❌ Kusvika kwemazana okushandisa.",
        "code_used": "❌ Makashandisa kodi iyi kale.",
        "greeting": "Makadii, ndinokubatsirei?",
        "stats": "Tataura {count} zvakare. Muri sei?",
        "remember": "Tataura nezvezvinhu zvakasiyana. Nezvei zvakakodzera?",
        "new_user_prompt": "Ndiri kuno kukubatsira. Unoda kuzivei?",
        "returning_user_prompt": "Ndiudzei zvimwe.",
        "admin_only": "⛔ Vatungamiri chete.",
        "code_generated": "🎟️ **Kodi Yagadzirwa**\n\n`{code}`\nNguva: {duration}\nInopera: {expires}\nKushandiswa: {uses}",
        "no_codes": "Hapana kodi iri kushanda.",
        "active_codes": "🎟️ **Kodhi dziri kushanda:**\n\n",
        "data_deleted": "🗑️ Ruzivo rwabviswa.",
        "rate_limit": "⏱️ Mameseji akawanda. Miremerere!",
        "error": "❌ Kukanganiswa. Edzazve.",
        "language_set": "✅ Mutauro wakaiswa chiShona",
        "language_prompt": "🌍 **Sarudza Mutauro:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "tn": {
        "welcome_new": "Dumela! Ke ene moithuti wa gago wa AI. Mpotsa sengwe - ke gakologelwa dipuisano tsa rona. O akarelse eng?",
        "welcome_back": "Dumela {name}! O tsogile jang?",
        "welcome_back_long": "Dumela {name}! E e kgalega ke sa go bone. O tsogile jang?",
        "access_denied": "🔒 **Bot ya Poraefete**\n\nTaelo fela.\n\n🔑 `/code KHOUTU YA GAGO`",
        "code_prompt": "🔑 `/code KHOUTU YA GAGO`",
        "code_accepted": "✅ **O Amogelesegile!**\n\nKe mothusi wa gago. Mpotsa sengwe\n• Thekenoloji, saense, kgwebo, khoutu\n• Keletso, go ngwala, go tlhotlhona\n• Metshameko, histori, dipotso tsa bophelo\n\nO ka rata go bua ka eng?",
        "already_authorized": "✅ O šetše o na le tumelelo!",
        "invalid_code": "❌ Khoutu e e sa siamang.",
        "code_expired": "❌ Khoutu e feletse getsela.",
        "code_max_uses": "❌ Matlhao a tse dingwe a fihletse.",
        "code_used": "❌ O šetše o šomiše khoutu e.",
        "greeting": "Dumela, nka go thusa jang?",
        "stats": "Re buisane makgetlo a {count}. O tsogile jang?",
        "remember": "Re buisane ka dilo tse dintsi. Ka tsela e e rileng?",
        "new_user_prompt": "Ke fa gona go go thusa. O ka rata go itse eng?",
        "returning_user_prompt": "Mpotselele tse dingwe.",
        "admin_only": "⛔ Babusi fela.",
        "code_generated": "🎟️ **Khoutu e Hlahilweng**\n\n`{code}`\nNako: {duration}\nE felelwa ke nako: {expires}\nMashomo: {uses}",
        "no_codes": "Ga go na dikhowe tse di dirisang.",
        "active_codes": "🎟️ **Dikhowe tse di Dirang:**\n\n",
        "data_deleted": "🗑️ Tshedimosetso e phimotswe.",
        "rate_limit": "⏱️ Molaetsa o montsi thata. Nnosa boleng!",
        "error": "❌ Phoso. Leka gape.",
        "language_set": "✅ Puo e beilwe mo Setswaneng",
        "language_prompt": "🌍 **Tlhopha Puo:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "tw": {
        "welcome_new": "Mahama! Me yɛ wo AI boafo. Bisa me biribiara - mebɛkae yɛn nkɔmmɔ. Dɛn na wore dwen ho?",
        "welcome_back": "Mahama {name}! Wo ho te sɛn?",
        "welcome_back_long": "Mahama {name}! Afei bi a yɛanhyia. Wo ho te sɛn?",
        "access_denied": "🔒 **Bot a wɔnhu**\n\nƆkyerɛsite kɛkɛ.\n\n🔑 `/code WO KOODU`",
        "code_prompt": "🔑 `/code WO KOODU`",
        "code_accepted": "✅ **Akwaaba!**\n\nMe yɛ wo boafo. Bisa me biribiara\n• Teknɔlɔji, sɛnea ade yɛ, adwuma, koodu\n• Afotu, kyerɛw, nkyerɛkyerɛ\n• Agoro, abakɔsɛm, nkontabuo a asɛe\n\nWopɛ sɛ wokasa ho dɛn?",
        "already_authorized": "✅ Wo wɔ kwan dedaw!",
        "invalid_code": "❌ Koodu no nni mu.",
        "code_expired": "❌ Koodu no adwuma.",
        "code_max_uses": "❌ Koodu no adwuma pɛɛ.",
        "code_used": "❌ Wo de koodu no adi dwuma dadaw.",
        "greeting": "Mahama, mebɛtumi aboa wo dɛn?",
        "stats": "Yɛakasa bere {count}. Wo ho te sɛn?",
        "remember": "Yɛakasa ho nneɛma pii. Dɛn na wɔfa ho?",
        "new_user_prompt": "Mewɔ ha sɛ meboa wo. Wopɛ sɛ wuhu dɛn?",
        "returning_user_prompt": "Kyerɛ me bi.",
        "admin_only": "⛔ Panyin kɛkɛ.",
        "code_generated": "🎟️ **Koodu no aba**\n\n`{code}`\nBere: {duration}\nƐkɔ awiei: {expires}\nAdwumaye: {uses}",
        "no_codes": "Koodu biara nni hɔ.",
        "active_codes": "🎟️ **Koodu a edi mu:**\n\n",
        "data_deleted": "🗑️ Data a wɛpepa.",
        "rate_limit": "⏱️ Nkrato pii. San no yɛ!",
        "error": "❌ Yɛde. San bi.",
        "language_set": "✅ Kasakoa ahyɛ Twi mu",
        "language_prompt": "🌍 **Paw Kasakoa:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    },
    "sw": {
        "welcome_new": "Habari! Mimi ni msaidizi wako wa AI. Uliza chochote - ninakumbuka mazungumzo yetu. Unafikiria nini?",
        "welcome_back": "Habari {name}! Vipi?",
        "welcome_back_long": "Habari {name}! Muda mrefu sijaona. Vipi?",
        "access_denied": "🔒 **Bot ya Kibinafsi**\n\nAlika tu.\n\n🔑 `/code KODI YAKO`",
        "code_prompt": "🔑 `/code KODI YAKO`",
        "code_accepted": "✅ **Karibu!**\n\nMimi ni msaidizi wako. Uliza chochote\n• Teknolojia, sayansi, biashara, programu\n• Ushauri, uandishi, uchanganuzi\n• Michezo, historia, masuala ya maisha\n\nUngependa kuzungumza kuhusu nini?",
        "already_authorized": "✅ Tayari una idhini!",
        "invalid_code": "❌ Kodi batili.",
        "code_expired": "❌ Kodi imeisha.",
        "code_max_uses": "❌ Matumizi yamefikia kikomo.",
        "code_used": "❌ Tayari umetumia kodi hii.",
        "greeting": "Habari, ninaweza kukusaidia vipi?",
        "stats": "Tumezungumza mara {count}. Vipi?",
        "remember": "Tumezungumza mambo mbalimbali. Hasa nini?",
        "new_user_prompt": "Nipo hapa kusaidia. Ungependa kujua nini?",
        "returning_user_prompt": "Niambie zaidi.",
        "admin_only": "⛔ Msimamizi tu.",
        "code_generated": "🎟️ **Kodi Imetengenezwa**\n\n`{code}`\nMuda: {duration}\nInaisha: {expires}\nMatumizi: {uses}",
        "no_codes": "Hakuna kodi zinazotumika.",
        "active_codes": "🎟️ **Kodi Zinazotumika:**\n\n",
        "data_deleted": "🗑️ Data imefutwa.",
        "rate_limit": "⏱️ Ujumbe mwingi sana. Pole pole!",
        "error": "❌ Hitilafu. Jaribu tena.",
        "language_set": "✅ Lugha imewekwa kuwa Kiswahili",
        "language_prompt": "🌍 **Chagua Lugha:**\n\n🇬🇧 English - /lang en\n🇿🇦 Afrikaans - /lang af\n🇫🇷 French - /lang fr\n🇪🇸 Spanish - /lang es\n🇩🇪 German - /lang de\n🇵🇹 Portuguese - /lang pt\n🇨🇳 Chinese - /lang zh\n🇦🇪 Arabic - /lang ar\n🇮🇳 Hindi - /lang hi\n🇿🇼 Ndebele - /lang nd\n🇿🇼 Shona - /lang sn\n🇧🇼 Tswana - /lang tn\n🇬🇭 Twi - /lang tw\n🇹🇿 Swahili - /lang sw",
    }
}

def get_text(key: str, lang: str = "en", **kwargs) -> str:
    """Get translated text"""
    if lang not in TRANSLATIONS:
        lang = "en"
    text = TRANSLATIONS[lang].get(key, TRANSLATIONS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

def get_user_language(telegram_id: str) -> str:
    """Get user's preferred language"""
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        return user.language if user else "en"
    finally:
        db.close()

def set_user_language(telegram_id: str, language: str) -> bool:
    """Set user's preferred language"""
    if language not in TRANSLATIONS:
        return False
    
    db = get_db()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.language = language
            db.commit()
            return True
        return False
    except Exception as e:
        logger.error(f"Error setting language: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def get_database_url():
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    return DATABASE_URL

# Simple cache implementation
class SimpleCache:
    def __init__(self, ttl_seconds=60):
        self._cache = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
    
    def get(self, key):
        with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if time.time() < expiry:
                    return value
                else:
                    del self._cache[key]
            return None
    
    def set(self, key, value, ttl=None):
        if ttl is None:
            ttl = self._ttl
        with self._lock:
            self._cache[key] = (value, time.time() + ttl)
    
    def delete(self, key):
        with self._lock:
            if key in self._cache:
                del self._cache[key]

# Connection pooling
engine = create_engine(
    get_database_url(),
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True
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
            if 'language' not in columns:
                try:
                    db.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(10) DEFAULT 'en'"))
                    db.commit()
                except:
                    db.rollback()
        Base.metadata.create_all(engine)
        logger.info("Database ready with multi-language support!")
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

# Caches
auth_cache = SimpleCache(ttl_seconds=60)
memory_cache = SimpleCache(ttl_seconds=30)
rate_limit_cache = SimpleCache(ttl_seconds=60)

def is_user_authorized(telegram_id: str):
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
            lang = get_user_language(telegram_id)
            await update.message.reply_text(get_text("access_denied", lang), parse_mode='Markdown')
            return
        
        if not check_rate_limit(telegram_id):
            lang = get_user_language(telegram_id)
            await update.message.reply_text(get_text("rate_limit", lang))
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

def format_duration(td: timedelta, lang: str = "en") -> str:
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
            return False, "invalid_code"
        if not ref.is_active:
            return False, "code_deactivated"
        if datetime.utcnow() > ref.expires_at:
            ref.is_active = False
            db.commit()
            return False, "code_expired"
        if ref.used_count >= ref.max_uses:
            return False, "code_max_uses"
        used_by_list = ref.used_by.split(",") if ref.used_by else []
        if user_id in used_by_list:
            return False, "code_used"
        return True, "valid"
    except Exception as e:
        logger.error(f"Error: {e}")
        return False, "error"
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
            "is_authorized": user.is_authorized if user else False,
            "language": user.language if user else "en"
        }
    finally:
        db.close()

def is_greeting(message: str) -> bool:
    greetings = ["hi", "hello", "hey", "greetings", "good morning", 
                "good afternoon", "good evening", "yo", "sup", "what's up",
                "howdy", "hi there", "hello there", "hey there",
                # European & Asian languages
                "hola", "bonjour", "guten tag", "olá", "ciao", "namaste",
                "marhaba", "salaam", "konnichiwa", "ni hao", "annyeong",
                # African languages
                "sawubona", "salibonani", "makadii", "mhoroi",  # Ndebele/Shona
                "dumela", "dumelang",  # Tswana
                "mahama", "etisen", "agoo",  # Twi
                "habari", "jambo", "hujambo", "mambo", "vipi"]  # Swahili
    msg_lower = message.lower().strip()
    for greeting in greetings:
        if msg_lower == greeting or msg_lower.startswith(greeting + " "):
            return True
    return False

def get_llm_response(user_message: str, conversation_history: list, user_name: str, language: str, is_new_user: bool = False) -> str:
    messages = []
    
    # Multi-language system prompt
    language_names = {
        "en": "English", "af": "Afrikaans", "fr": "French", "es": "Spanish",
        "de": "German", "pt": "Portuguese", "zh": "Chinese", "ar": "Arabic", "hi": "Hindi",
        "nd": "Ndebele", "sn": "Shona", "tn": "Tswana", "tw": "Twi", "sw": "Swahili"
    }
    lang_name = language_names.get(language, "English")
    
    system_prompt = f"""You are a helpful AI assistant. Respond in {lang_name}.
You can discuss any topic knowledgeably.
You remember past conversations with {user_name} and maintain continuity.
Be concise, helpful, and natural. If unsure, say so. Respond in {lang_name} only."""
    
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
                max_tokens=400,
                temperature=0.7,
                request_timeout=10
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
                json={"model": "llama2", "prompt": prompt, "stream": False, "max_tokens": 400},
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
    lang = get_user_language(telegram_id)
    
    if not is_user_authorized(telegram_id) and not check_admin(user.id):
        log_unauthorized_attempt(telegram_id, user.username, user.first_name, "Started bot")
        await update.message.reply_text(get_text("access_denied", lang), parse_mode='Markdown')
        return
    
    memory = get_memory_summary(telegram_id)
    
    if memory["is_new_user"]:
        welcome = get_text("welcome_new", lang)
    else:
        if memory["time_since_last"] and memory["time_since_last"].days > 7:
            welcome = get_text("welcome_back_long", lang, name=memory['user_name'])
        else:
            welcome = get_text("welcome_back", lang, name=memory['user_name'])
    
    await update.message.reply_text(welcome)

async def enter_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    lang = get_user_language(telegram_id)
    
    if not context.args:
        await update.message.reply_text(get_text("code_prompt", lang), parse_mode='Markdown')
        return
    
    code = context.args[0].upper()
    user_id_str = str(telegram_id)
    
    if is_user_authorized(telegram_id):
        await update.message.reply_text(get_text("already_authorized", lang))
        return
    
    is_valid, error_key = validate_referral_code(code, user_id_str)
    
    if not is_valid:
        log_unauthorized_attempt(telegram_id, user.username, user.first_name, 
                               f"Bad code: {code}")
        await update.message.reply_text(get_text(error_key, lang))
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
                    is_authorized=True,
                    language=lang
                )
                db.add(user_db)
            else:
                user_db.is_authorized = True
            db.commit()
            
            await update.message.reply_text(get_text("code_accepted", lang))
        except Exception as e:
            logger.error(f"Error: {e}")
            db.rollback()
            await update.message.reply_text(get_text("error", lang))
        finally:
            db.close()
    else:
        await update.message.reply_text(get_text("error", lang))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change language command"""
    user = update.effective_user
    telegram_id = str(user.id)
    
    if not context.args:
        lang = get_user_language(telegram_id)
        await update.message.reply_text(get_text("language_prompt", lang), parse_mode='Markdown')
        return
    
    new_lang = context.args[0].lower()
    if new_lang not in TRANSLATIONS:
        await update.message.reply_text("❌ Invalid language. Use: en, af, fr, es, de, pt, zh, ar, hi, nd, sn, tn, tw, sw")
        return
    
    if set_user_language(telegram_id, new_lang):
        await update.message.reply_text(get_text("language_set", new_lang))
    else:
        await update.message.reply_text("❌ Error setting language. Try again later.")

@require_auth
async def generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = get_user_language(str(user.id))
    
    if not check_admin(user.id):
        await update.message.reply_text(get_text("admin_only", lang))
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
        duration_readable = format_duration(result['duration'], lang)
        
        await update.message.reply_text(
            get_text("code_generated", lang, 
                    code=result['code'], 
                    duration=duration_readable,
                    expires=expires_str,
                    uses=result['max_uses']),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(get_text("error", lang))

@require_auth
async def list_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = get_user_language(str(user.id))
    
    if not check_admin(user.id):
        await update.message.reply_text(get_text("admin_only", lang))
        return
    
    db = get_db()
    try:
        codes = db.query(ReferralCode).filter_by(is_active=True).all()
        if not codes:
            await update.message.reply_text(get_text("no_codes", lang))
            return
        
        msg = get_text("active_codes", lang)
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
        await update.message.reply_text(get_text("error", lang))
    finally:
        db.close()

@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    current_message = update.message.text
    lang = get_user_language(telegram_id)
    
    history = get_recent_memory(telegram_id, max_messages=6)
    memory = get_memory_summary(telegram_id)
    current_lower = current_message.lower()
    
    if is_greeting(current_message):
        response = get_text("greeting", lang)
    
    elif any(x in current_lower for x in ["stats", "history", "memory"]):
        response = get_text("stats", lang, count=memory['total_messages'])
    
    elif any(x in current_lower for x in ["remember", "recall"]):
        if history:
            response = get_text("remember", lang)
        else:
            response = get_text("new_user_prompt", lang)
    
    else:
        llm_response = get_llm_response(current_message, history, memory['user_name'], lang, memory['is_new_user'])
        if llm_response:
            response = llm_response
        else:
            response = get_text("new_user_prompt", lang) if memory["is_new_user"] else get_text("returning_user_prompt", lang)
    
    await update.message.reply_text(response)
    
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
                    is_authorized=True,
                    language=lang
                )
                db.add(user_db)
            
            user_db.message_count = memory['total_messages'] + 1
            user_db.last_active = datetime.utcnow()
            db.commit()
            memory_cache.delete(f"mem_{telegram_id}")
        except Exception as e:
            logger.error(f"Error saving: {e}")
            db.rollback()
        finally:
            db.close()
    
    asyncio.create_task(save_conversation())

@require_auth
async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = str(user.id)
    lang = get_user_language(telegram_id)
    
    if not check_admin(user.id):
        await update.message.reply_text(get_text("admin_only", lang))
        return
    
    db = get_db()
    try:
        db.query(Conversation).filter_by(telegram_id=telegram_id).delete()
        db.query(User).filter_by(telegram_id=telegram_id).delete()
        db.commit()
        auth_cache.delete(f"auth_{telegram_id}")
        memory_cache.delete(f"mem_{telegram_id}")
        await update.message.reply_text(get_text("data_deleted", lang))
    except Exception as e:
        logger.error(f"Error: {e}")
        db.rollback()
        await update.message.reply_text(get_text("error", lang))
    finally:
        db.close()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}")
    
    if isinstance(context.error, RetryAfter):
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
    
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(20)
        .pool_timeout(30.0)
        .build()
    )
    
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("code", enter_code))
    application.add_handler(CommandHandler("lang", language_cmd))
    application.add_handler(CommandHandler("language", language_cmd))
    application.add_handler(CommandHandler("gencode", generate_code))
    application.add_handler(CommandHandler("codes", list_codes))
    application.add_handler(CommandHandler("delete_my_data", delete_my_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🚀 MULTI-LANGUAGE BOT RUNNING!")
    logger.info("Supported: EN, AF, FR, ES, DE, PT, ZH, AR, HI, ND, SN, TN, TW, SW")
    
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
