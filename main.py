import os
import json
import logging
import httpx
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI()

# Initialize Telegram bot
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-news-ai-service-production.up.railway.app")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

# Store user states (for back button functionality)
user_states: Dict[int, Dict[str, Any]] = {}

class SignalMessage(BaseModel):
    chat_id: int
    signal_data: Dict[str, Any]
    news_data: Optional[Dict[str, Any]] = None

async def format_signal(signal_data: Dict[str, Any]) -> str:
    """Format signal using the Signal AI Service"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SIGNAL_AI_SERVICE}/format-signal",
            json=signal_data
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error formatting signal")
        return response.json()["formatted_message"]

async def get_news_analysis(instrument: str, articles: list) -> Dict[str, Any]:
    """Get news analysis from News AI Service"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{NEWS_AI_SERVICE}/analyze-news",
            json={"instrument": instrument, "articles": articles}
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error analyzing news")
        return response.json()

async def send_initial_message(chat_id: int, signal_text: str) -> None:
    """Send initial signal message with options"""
    keyboard = [
        [
            InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
            InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await bot.send_message(
        chat_id=chat_id,
        text=signal_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

@app.post("/callback/{callback_data}")
async def handle_callback(callback_data: str, chat_id: int, message_id: int):
    """Handle callback queries from Telegram"""
    try:
        if callback_data == "sentiment":
            if chat_id not in user_states:
                return {"status": "error", "message": "Session expired"}
            
            news_data = user_states[chat_id].get("news_data")
            if not news_data:
                return {"status": "error", "message": "No news analysis available"}
            
            # Create keyboard with back button
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=news_data["analysis"],
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
        elif callback_data == "technical":
            # Create keyboard with back button
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="📊 Technical Analysis feature coming soon!",
                reply_markup=reply_markup
            )
            
        elif callback_data == "back":
            if chat_id not in user_states:
                return {"status": "error", "message": "Session expired"}
            
            # Restore original message with both options
            keyboard = [
                [
                    InlineKeyboardButton("Market Sentiment 📊", callback_data="sentiment"),
                    InlineKeyboardButton("Technical Analysis 📈", callback_data="technical")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            signal_text = user_states[chat_id].get("signal_text", "Signal not available")
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=signal_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        
        return {"status": "success"}
            
    except Exception as e:
        logger.error(f"Error handling callback: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-signal")
async def send_signal(message: SignalMessage):
    """Send a signal message to a specific chat with interactive options"""
    try:
        # Format signal
        signal_text = await format_signal(message.signal_data)
        
        # Get news analysis if provided
        news_analysis = None
        if message.news_data:
            news_analysis = await get_news_analysis(
                message.news_data["instrument"],
                message.news_data["articles"]
            )
        
        # Store in user state
        user_states[message.chat_id] = {
            "signal_text": signal_text,
            "news_data": news_analysis
        }
        
        # Send initial message with options
        await send_initial_message(message.chat_id, signal_text)
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
