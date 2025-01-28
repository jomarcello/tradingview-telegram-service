import os
import json
import logging
import httpx
import base64
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, constants
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

# Initialize FastAPI app
app = FastAPI()

# Initialize Telegram bot
BOT_TOKEN = "7583525993:AAFp90r7UqCY2KdGufKgHHjjslBy7AnY_Sg"
bot = Bot(BOT_TOKEN)

# Create application
application = Application.builder().token(BOT_TOKEN).build()

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

def format_signal_message(signal_data: Dict[str, Any]) -> str:
    """Format the signal message"""
    direction = "BUY " if signal_data["direction"].upper() == "BUY" else "SELL "
    
    message = f""" New Trading Signal 

Instrument: {signal_data['instrument']}
Action: {direction}

Entry Price: {signal_data['entry_price']}
Stop Loss: {signal_data['stop_loss']} 
Take Profit: {signal_data['take_profit']} 

Timeframe: {signal_data['timeframe']}
Strategy: {signal_data['strategy']}

--------------------

Risk Management:
• Position size: 1\-2% max
• Use proper stop loss
• Follow your trading plan

--------------------

 SigmaPips AI Verdict:
{signal_data.get('ai_verdict', 'AI analysis not available')}

Risk/Reward Ratio: {signal_data.get('risk_reward_ratio', 'Not available')}"""
    return message

def escape_markdown(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

@app.post("/send-signal")
async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a trading signal to Telegram"""
    try:
        # Format message
        message = format_signal_message(signal_request.signal_data)
        
        # Create keyboard markup
        keyboard = [
            [
                InlineKeyboardButton("", callback_data="technical_analysis"),
                InlineKeyboardButton("", callback_data="market_sentiment")
            ],
            [
                InlineKeyboardButton("", callback_data="economic_calendar")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message to all chat IDs
        sent_messages = []
        for chat_id in signal_request.chat_ids:
            try:
                sent_message = await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.MARKDOWN_V2
                )
                sent_messages.append(sent_message)
                
                # Store original message
                messages[str(sent_message.message_id)] = {
                    "original_text": message,
                    "text": message,
                    "symbol": signal_request.signal_data["instrument"],
                    "timeframe": signal_request.signal_data["timeframe"]
                }
            except Exception as e:
                logger.error(f"Error sending to chat {chat_id}: {str(e)}")
                continue
        
        # Save messages to file
        save_messages(messages)
        
        return {"status": "success", "message": f"Signal sent to {len(sent_messages)} chats"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error sending signal: {str(e)}")

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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses."""
    query = update.callback_query
    await query.answer()

    try:
        if query.data == "technical_analysis":
            try:
                # Immediately acknowledge the button press
                await query.answer()
                
                # Get symbol from stored message
                message_data = messages.get(str(query.message.message_id))
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text(" Message expired. Please request a new signal.")
                    return

                logger.info(f"Getting chart for symbol: {message_data['symbol']}")
                
                # Update the current message to show loading
                await query.edit_message_text(
                    text=" Generating technical analysis chart...",
                    parse_mode='Markdown'
                )
                
                # Get chart from chart service
                chart_service_url = "https://tradingview-chart-service-production.up.railway.app/chart"
                async with httpx.AsyncClient(timeout=60.0) as client:
                    try:
                        # Get timeframe from message data
                        timeframe = message_data.get('timeframe', '15m')
                        
                        # Get chart
                        response = await client.get(
                            f"{chart_service_url}?symbol={message_data['symbol']}&interval={timeframe}"
                        )
                        response.raise_for_status()
                        
                        # Create keyboard with Back button
                        keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                        
                        # Send as new message and store reference
                        chart_message = await query.get_bot().send_photo(
                            chat_id=query.message.chat_id,
                            photo=response.content,
                            caption=f" Technical Analysis for {message_data['symbol']}",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                        
                        # Store reference to chart message
                        messages[str(chart_message.message_id)] = {
                            "original_text": message_data["original_text"],
                            "text": message_data["text"],
                            "symbol": message_data["symbol"],
                            "timeframe": message_data["timeframe"],
                            "is_chart": True
                        }
                        save_messages(messages)
                        
                        # Delete the original message
                        await query.message.delete()
                        
                    except Exception as e:
                        logger.error(f"Error getting chart: {str(e)}")
                        keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                        await query.edit_message_text(
                            text=f" Failed to get chart: {str(e)}",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

            except Exception as e:
                logger.error(f"Error in technical analysis handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                    await query.edit_message_text(
                        text=" An error occurred",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except:
                    pass

        elif query.data == "market_sentiment":
            try:
                # Immediately acknowledge the button press
                await query.answer()
                
                # Get symbol from stored message
                message_data = messages.get(str(query.message.message_id))
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text(" Message expired. Please request a new signal.")
                    return

                logger.info(f"Getting news for symbol: {message_data['symbol']}")
                
                # Update the current message to show loading
                await query.edit_message_text(
                    text=" Analyzing market sentiment...",
                    parse_mode='Markdown'
                )
                
                # Get news from signal processor
                signal_processor_url = "https://tradingview-signal-processor-production.up.railway.app/get-news"
                async with httpx.AsyncClient(timeout=60.0) as client:
                    try:
                        response = await client.get(
                            signal_processor_url,
                            params={"instrument": message_data["symbol"]}
                        )
                        
                        if response.status_code != 200:
                            keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text=" Failed to get news",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return
                        
                        news_data = response.json()
                        if not news_data.get("articles"):
                            keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text=" No news articles found",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return

                        # Send news to AI service for analysis
                        sentiment_url = "https://tradingview-news-ai-service-production.up.railway.app/analyze-news"
                        response = await client.post(
                            sentiment_url,
                            json={
                                "instrument": message_data["symbol"],
                                "articles": news_data["articles"]
                            }
                        )
                        
                        if response.status_code != 200:
                            keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text=" Failed to get sentiment",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return
                        
                        data = response.json()

                        # Create keyboard with Back button
                        keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                        
                        # Send as new message and store reference
                        sentiment_message = await query.get_bot().send_message(
                            chat_id=query.message.chat_id,
                            text=data["analysis"],
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                        
                        # Store reference to sentiment message
                        messages[str(sentiment_message.message_id)] = {
                            "original_text": message_data["original_text"],
                            "text": message_data["text"],
                            "symbol": message_data["symbol"],
                            "timeframe": message_data["timeframe"],
                            "is_sentiment": True
                        }
                        save_messages(messages)
                        
                        # Delete the original message
                        await query.message.delete()
                        
                    except Exception as e:
                        logger.error(f"Error getting sentiment: {str(e)}")
                        keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                        await query.edit_message_text(
                            text=" An error occurred",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

            except Exception as e:
                logger.error(f"Error in market sentiment handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    keyboard = [[InlineKeyboardButton("", callback_data="back_to_signal")]]
                    await query.edit_message_text(
                        text=" An error occurred",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except:
                    pass
                    
        elif query.data == "economic_calendar":
            try:
                # Get calendar data from the calendar service
                async with httpx.AsyncClient() as client:
                    response = await client.get("https://tradingview-calendar-service-production.up.railway.app/calendar")
                    if response.status_code == 200:
                        calendar_data = response.json()
                        await query.edit_message_text(
                            text=calendar_data["data"],
                            parse_mode=constants.ParseMode.HTML
                        )
                    else:
                        await query.edit_message_text(
                            text=" Error fetching calendar data. Please try again later.",
                            parse_mode=constants.ParseMode.HTML
                        )
            except Exception as e:
                logger.error(f"Error in economic calendar handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                await query.edit_message_text(
                    text=" An error occurred. Please try again later.",
                    parse_mode=constants.ParseMode.HTML
                )

        elif query.data == "back_to_signal":
            try:
                # Get original message data
                message_data = messages.get(str(query.message.message_id))
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text(" Message expired. Please request a new signal.")
                    return
                
                # Create new message with original content
                keyboard = [
                    [
                        InlineKeyboardButton("", callback_data="technical_analysis"),
                        InlineKeyboardButton("", callback_data="market_sentiment")
                    ],
                    [
                        InlineKeyboardButton("", callback_data="economic_calendar")
                    ]
                ]
                
                # Send new message with original content
                new_message = await query.get_bot().send_message(
                    chat_id=query.message.chat_id,
                    text=message_data["original_text"],
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                # Store new message
                messages[str(new_message.message_id)] = {
                    "original_text": message_data["original_text"],
                    "text": message_data["text"],
                    "symbol": message_data["symbol"],
                    "timeframe": message_data["timeframe"]
                }
                save_messages(messages)
                
                # Delete the current message
                await query.message.delete()
                
            except Exception as e:
                logger.error(f"Error in back to signal handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    await query.edit_message_text(
                        text=" An error occurred",
                        parse_mode='Markdown'
                    )
                except:
                    pass

        await query.answer()
        
    except Exception as e:
        logger.error(f"Error in button handler: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

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

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook update: {str(e)}")
        return {"status": "error", "message": str(e)}

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
