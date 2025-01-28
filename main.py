import os
import json
import logging
import httpx
import base64
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
import traceback

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
    chat_id: str

def format_signal_message(signal_data: Dict[str, Any]) -> str:
    """Format the signal message"""
    direction = "BUY 📈" if signal_data["direction"].upper() == "LONG" else "SELL 📉"
    
    message = f"""🎯 New Trading Signal 🎯

Instrument: {signal_data['instrument']}
Action: {direction}

Entry Price: {signal_data['entry']}
Stop Loss: {signal_data['sl']} 🛑
Take Profit: {signal_data['tp']} 🎯

Timeframe: {signal_data['timeframe']}
Strategy: Test Strategy

--------------------

Risk Management:
• Position size: 1-2% max
• Use proper stop loss
• Follow your trading plan

--------------------

🤖 SigmaPips AI Verdict:
The {signal_data['instrument']} {direction.split()[0].lower()} signal aligns with a bullish momentum confirmed by short-term indicators, suggesting an upward move. With a tight stop loss and a favorable risk/reward ratio, this setup offers a promising opportunity for disciplined traders."""
    return message

@app.post("/send-signal")
async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a trading signal to Telegram"""
    try:
        # Format message
        message = format_signal_message(signal_request.signal_data)
        
        # Create keyboard markup
        keyboard = [
            [
                InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis"),
                InlineKeyboardButton("📰 Market Sentiment", callback_data="market_sentiment")
            ],
            [
                InlineKeyboardButton("📅 Economic Calendar", callback_data="economic_calendar")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message
        sent_message = await bot.send_message(
            chat_id=signal_request.chat_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Store original message
        messages[str(sent_message.message_id)] = {
            "original_text": message,
            "text": message,
            "symbol": signal_request.signal_data["instrument"],
            "timeframe": signal_request.signal_data["timeframe"]
        }
        
        # Save messages to file
        save_messages(messages)
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error sending signal: {str(e)}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button presses"""
    try:
        query = update.callback_query
        chat_id = query.message.chat.id
        message_id = str(query.message.message_id)

        # Load latest messages
        global messages
        messages = load_messages()

        if query.data == "technical_analysis":
            try:
                # Immediately acknowledge the button press
                await query.answer()
                
                # Get symbol from stored message
                message_data = messages.get(message_id)
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text("❌ Message expired. Please request a new signal.")
                    return

                logger.info(f"Getting chart for symbol: {message_data['symbol']}")
                
                # Update the current message to show loading
                await query.edit_message_text(
                    text="🔄 Generating technical analysis chart...",
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
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        
                        # Send as new message and store reference
                        chart_message = await query.get_bot().send_photo(
                            chat_id=query.message.chat_id,
                            photo=response.content,
                            caption=f"📊 Technical Analysis for {message_data['symbol']}",
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
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        await query.edit_message_text(
                            text=f"❌ Failed to get chart: {str(e)}",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

            except Exception as e:
                logger.error(f"Error in technical analysis handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                    await query.edit_message_text(
                        text="❌ An error occurred",
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
                message_data = messages.get(message_id)
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text("❌ Message expired. Please request a new signal.")
                    return

                logger.info(f"Getting news for symbol: {message_data['symbol']}")
                
                # Update the current message to show loading
                await query.edit_message_text(
                    text="🔄 Analyzing market sentiment...",
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
                            keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text="❌ Failed to get news",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return
                        
                        news_data = response.json()
                        if not news_data.get("articles"):
                            keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text="❌ No news articles found",
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
                            keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text="❌ Failed to get sentiment",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return
                        
                        data = response.json()

                        # Create keyboard with Back button
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        
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
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        await query.edit_message_text(
                            text="❌ An error occurred",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

            except Exception as e:
                logger.error(f"Error in market sentiment handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                    await query.edit_message_text(
                        text="❌ An error occurred",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except:
                    pass
                    
        elif query.data == "economic_calendar":
            try:
                # Immediately acknowledge the button press
                await query.answer()
                
                # Get symbol from stored message
                message_data = messages.get(message_id)
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text("❌ Message expired. Please request a new signal.")
                    return

                logger.info("Getting economic calendar data")
                
                # Update the current message to show loading
                await query.edit_message_text(
                    text="🔄 Loading economic calendar...",
                    parse_mode='Markdown'
                )
                
                # Get calendar data from economic calendar service
                calendar_service_url = "https://tradingview-economic-calendar-se-production.up.railway.app/calendar"
                async with httpx.AsyncClient(timeout=60.0) as client:
                    try:
                        response = await client.get(calendar_service_url)
                        response.raise_for_status()
                        data = response.json()
                        
                        if not data.get("events"):
                            keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                            await query.edit_message_text(
                                text="❌ No economic events found",
                                parse_mode='Markdown',
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            return
                        
                        # Format the calendar data
                        calendar_text = "📅 *Economic Calendar*\n\n"
                        calendar_text += "\n".join(data["events"])
                        
                        # Create keyboard with Back button
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        
                        # Send as new message and store reference
                        calendar_message = await query.get_bot().send_message(
                            chat_id=query.message.chat_id,
                            text=calendar_text,
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                        
                        # Store reference to calendar message
                        messages[str(calendar_message.message_id)] = {
                            "original_text": message_data["original_text"],
                            "text": message_data["text"],
                            "symbol": message_data["symbol"],
                            "timeframe": message_data["timeframe"],
                            "is_calendar": True
                        }
                        save_messages(messages)
                        
                        # Delete the original message
                        await query.message.delete()
                        
                    except Exception as e:
                        logger.error(f"Error getting calendar: {str(e)}")
                        keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                        await query.edit_message_text(
                            text="❌ Failed to get economic calendar data",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )

            except Exception as e:
                logger.error(f"Error in economic calendar handler: {str(e)}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                try:
                    keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                    await query.edit_message_text(
                        text="❌ An error occurred",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except:
                    pass

        elif query.data == "back_to_signal":
            try:
                # Get original message data
                message_data = messages.get(message_id)
                if not message_data:
                    logger.error("No message data found")
                    await query.edit_message_text("❌ Message expired. Please request a new signal.")
                    return
                
                # Create new message with original content
                keyboard = [
                    [
                        InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis"),
                        InlineKeyboardButton("📰 Market Sentiment", callback_data="market_sentiment")
                    ],
                    [
                        InlineKeyboardButton("📅 Economic Calendar", callback_data="economic_calendar")
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
                        text="❌ An error occurred",
                        parse_mode='Markdown'
                    )
                except:
                    pass

        await query.answer()
        
    except Exception as e:
        logger.error(f"Error in button handler: {str(e)}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        
        if update.callback_query:
            await button_handler(update, None)
            
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}

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

@app.on_event("startup")
async def startup():
    """Set webhook on startup"""
    try:
        webhook_url = "https://tradingview-telegram-service-production.up.railway.app/webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {str(e)}")
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
