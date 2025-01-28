# Initialize FastAPI app
app = FastAPI()

import os
import json
import logging
import httpx
import base64
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, constants, Message
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
import traceback
import uuid

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.handlers.RotatingFileHandler(
            '/tmp/telegram_service.log',  # Use /tmp for Railway
            maxBytes=10485760,  # 10MB
            backupCount=5
        )
    ]
)
logger = logging.getLogger(__name__)

# Initialize Telegram bot
BOT_TOKEN = "7583525993:AAFp90r7UqCY2KdGufKgHHjjslBy7AnY_Sg"
bot = Bot(BOT_TOKEN)

# Store original messages
MESSAGES_FILE = '/tmp/messages.json'

def load_messages() -> Dict:
    """Load messages from file"""
    try:
        if os.path.exists(MESSAGES_FILE):
            with open(MESSAGES_FILE, 'r') as f:
                return json.load(f)
        else:
            # Create empty file if it doesn't exist
            save_messages({})
    except Exception as e:
        logger.error(f"Error loading messages: {str(e)}")
    return {}

def save_messages(messages: Dict) -> None:
    """Save messages to file"""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(MESSAGES_FILE), exist_ok=True)
        with open(MESSAGES_FILE, 'w') as f:
            json.dump(messages, f)
    except Exception as e:
        logger.error(f"Error saving messages: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

messages = load_messages()

class SignalRequest(BaseModel):
    signal_data: Dict[str, Any]
    chat_ids: List[str]

class CalendarRequest(BaseModel):
    message: str
    chat_id: Optional[str] = None

def escape_markdown(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_signal_message(signal_data: Dict[str, Any]) -> str:
    """Format signal data into a Telegram message."""
    try:
        # Get required fields
        instrument = signal_data.get("instrument", "Unknown")
        direction = signal_data.get("direction", "Unknown")
        entry_price = signal_data.get("entry_price", "0.0")
        stop_loss = signal_data.get("stop_loss", "0.0")
        take_profit = signal_data.get("take_profit", "0.0")
        timeframe = signal_data.get("timeframe", "Unknown")
        strategy = signal_data.get("strategy", "Unknown")
        
        # Format base message with emojis
        message = f"🎯 New Trading Signal 🎯\n\n"
        message += f"Instrument: {instrument}\n"
        message += f"Action: {direction} ⬇️\n\n"  # Use ⬆️ for BUY
        
        message += f"Entry Price: {entry_price}\n"
        message += f"Stop Loss: {stop_loss} 🔴\n"
        message += f"Take Profit: {take_profit} 🎯\n\n"
        
        message += f"Timeframe: {timeframe}\n"
        message += f"Strategy: {strategy}\n\n"
        
        message += "--------------------\n\n"
        
        message += "Risk Management:\n"
        message += "• Position size: 1-2% max\n"
        message += "• Use proper stop loss\n"
        message += "• Follow your trading plan\n\n"
        
        message += "--------------------\n\n"
        
        # Add AI analysis if available
        if "ai_verdict" in signal_data:
            message += f"🤖 SigmaPips AI Verdict:\n"
            message += f"{signal_data['ai_verdict']}\n\n"
            
            if "risk_reward_ratio" in signal_data:
                message += f"Risk/Reward Ratio: {signal_data['risk_reward_ratio']}"
        
        return message
        
    except Exception as e:
        logger.error(f"Error formatting message: {str(e)}")
        return "Error formatting signal message"

async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a signal to Telegram chat."""
    try:
        # Create keyboard markup with buttons
        keyboard = [
            [
                InlineKeyboardButton(text="📊 Technical Analysis", callback_data="technical"),
                InlineKeyboardButton(text="📰 Market Sentiment", callback_data="sentiment")
            ],
            [
                InlineKeyboardButton(text="📅 Economic Calendar", callback_data="calendar")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        # Format message
        message = format_signal_message(signal_request.signal_data)
        
        # Send message to each chat ID
        for chat_id in signal_request.chat_ids:
            try:
                # Remove any whitespace and escape special characters
                chat_id = chat_id.strip()
                
                sent_message = await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    reply_markup=reply_markup,
                    parse_mode=None  # Don't use markdown formatting
                )
                logger.info(f"Message sent to chat {chat_id}")
                
            except Exception as e:
                logger.error(f"Error sending to chat {chat_id}: {str(e)}")
                continue
                
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-signal")
async def send_signal_endpoint(signal_request: SignalRequest):
    """Send a signal to Telegram chat."""
    try:
        await send_signal(signal_request)
        return {"status": "success", "message": "Signal sent successfully"}
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-calendar")
async def send_calendar(calendar_request: CalendarRequest):
    """Send an economic calendar message to Telegram."""
    try:
        # Get chat IDs from Supabase if not provided
        if not calendar_request.chat_id:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://utigkgjcyqnrhpndzqhs.supabase.co/rest/v1/subscribers",
                    headers={
                        'apikey': os.getenv("SUPABASE_KEY"),
                        'Authorization': f'Bearer {os.getenv("SUPABASE_KEY")}'
                    }
                )
                subscribers = response.json()
                chat_ids = [sub['chat_id'] for sub in subscribers]
        else:
            chat_ids = [calendar_request.chat_id]

        # Store message for later retrieval
        message_id = str(uuid.uuid4())
        messages[message_id] = {
            'type': 'calendar',
            'content': calendar_request.message
        }
        save_messages(messages)

        # Send to all chat IDs
        for chat_id in chat_ids:
            await bot.send_message(
                chat_id=chat_id,
                text=calendar_request.message,
                parse_mode=constants.ParseMode.HTML
            )

        return {"status": "success", "message": "Calendar sent successfully"}

    except Exception as e:
        logger.error(f"Error sending calendar: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

async def show_loading_message(message: Message, action: str) -> Message:
    """Show a loading message while processing."""
    loading_text = f"⏳ Loading {action}... Please wait"
    return await message.reply_text(loading_text)

@app.on_callback_query()
async def handle_callback(query: CallbackQuery):
    """Handle callback queries from inline buttons."""
    await query.answer()

    try:
        original_message = query.message
        loading_message = None

        if query.data == "technical":
            try:
                # Show loading message
                loading_message = await show_loading_message(original_message, "Technical Analysis")
                
                # Get chart from chart service
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"https://tradingview-chart-service-production.up.railway.app/chart",
                        params={
                            "symbol": "EURUSD",  # Get this from stored message data
                            "interval": "15m",
                            "theme": "dark"
                        }
                    )
                    response.raise_for_status()
                    
                    # Send chart as photo
                    await bot.send_photo(
                        chat_id=original_message.chat.id,
                        photo=response.content,
                        caption="📊 Technical Analysis Chart"
                    )
                    
            except Exception as e:
                logger.error(f"Error getting technical analysis: {str(e)}")
                await original_message.reply_text("❌ Error loading technical analysis. Please try again later.")
            finally:
                if loading_message:
                    await loading_message.delete()

        elif query.data == "sentiment":
            try:
                # Show loading message
                loading_message = await show_loading_message(original_message, "Market Sentiment")
                
                # Get sentiment from news AI service
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"https://tradingview-news-ai-service-production.up.railway.app/analyze-news",
                        params={
                            "instrument": "EURUSD"  # Get this from stored message data
                        }
                    )
                    response.raise_for_status()
                    sentiment_data = response.json()
                    
                    # Send sentiment analysis
                    await original_message.reply_text(
                        f"📰 Market Sentiment Analysis\n\n{sentiment_data['sentiment']}"
                    )
                    
            except Exception as e:
                logger.error(f"Error getting market sentiment: {str(e)}")
                await original_message.reply_text("❌ Error loading market sentiment. Please try again later.")
            finally:
                if loading_message:
                    await loading_message.delete()

        elif query.data == "calendar":
            try:
                # Show loading message
                loading_message = await show_loading_message(original_message, "Economic Calendar")
                
                # Get calendar data
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"https://tradingview-calendar-service-production.up.railway.app/calendar",
                        params={
                            "instrument": "EURUSD",  # Get this from stored message data
                            "timeframe": "15m"
                        }
                    )
                    response.raise_for_status()
                    calendar_data = response.json()
                    
                    # Format and send calendar data
                    calendar_text = "📅 Economic Calendar\n\n"
                    for event in calendar_data["events"]:
                        calendar_text += f"🕒 {event['time']}\n"
                        calendar_text += f"📊 {event['event']}\n"
                        calendar_text += f"🎯 Impact: {event['impact']}\n\n"
                        
                    await original_message.reply_text(calendar_text)
                    
            except Exception as e:
                logger.error(f"Error getting economic calendar: {str(e)}")
                await original_message.reply_text("❌ Error loading economic calendar. Please try again later.")
            finally:
                if loading_message:
                    await loading_message.delete()

    except Exception as e:
        logger.error(f"Error handling callback: {str(e)}")
        await query.message.reply_text("❌ An error occurred. Please try again later.")

# Create application and add handlers
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)
application.add_handler(CallbackQueryHandler(handle_callback))

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        
        # Process the update
        await application.process_update(update)
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook update: {str(e)}")
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}

@app.on_event("startup")
async def startup():
    """Set webhook on startup"""
    try:
        # Delete any existing webhook and stop polling
        await bot.delete_webhook(drop_pending_updates=True)
        
        # Start the bot in webhook mode
        webhook_url = "https://tradingview-telegram-service-production.up.railway.app/telegram-webhook"
        await bot.set_webhook(url=webhook_url)
        
        logger.info("Telegram bot webhook set successfully")
    except Exception as e:
        logger.error(f"Error starting Telegram bot: {str(e)}")
        raise

@app.on_event("shutdown")
async def shutdown():
    """Stop the bot when shutting down"""
    try:
        await bot.delete_webhook()
        await application.stop()
        logger.info("Telegram bot stopped successfully")
    except Exception as e:
        logger.error(f"Error stopping Telegram bot: {str(e)}")

@app.get("/logs")
async def get_logs():
    """Get the last 100 lines of logs"""
    try:
        with open("/tmp/telegram_service.log", "r") as f:
            logs = f.readlines()[-100:]  # Get last 100 lines
            return {"logs": "".join(logs)}
    except Exception as e:
        logger.error(f"Error reading logs: {str(e)}")
        return {"logs": f"Error reading logs: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
