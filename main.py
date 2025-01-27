import os
import json
import logging
import httpx
import base64
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI()

# Initialize Telegram bot
BOT_TOKEN = "7583525993:AAFp90r7UqCY2KdGufKgHHjjslBy7AnY_Sg"
bot = Bot(BOT_TOKEN)

# Store original messages
messages = {}

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
The {signal_data['instrument']} {direction.split()[0].lower()} signal aligns with a bullish momentum confirmed by short-term indicators, suggesting an upward move. With a tight stop loss and a favorable risk/reward ratio, this setup offers a promising opportunity for disciplined traders.

Remember:
- Keep it concise and professional
- Use emojis sparingly
- Format numbers correctly"""
    return message

@app.post("/send-signal")
async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a trading signal to Telegram"""
    try:
        # Format message
        message = format_signal_message(signal_request.signal_data)
        
        # Create keyboard markup
        keyboard = [[
            InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis")
        ]]
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
            "text": message,
            "symbol": signal_request.signal_data["instrument"]
        }
        
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

        if query.data == "technical_analysis":
            # Get symbol from stored message
            symbol = messages[message_id]["symbol"]
            
            # Get chart screenshot
            chart_url = f"https://tradingview-chart-service-production.up.railway.app/screenshot?symbol={symbol}&interval=15m"
            async with httpx.AsyncClient() as client:
                response = await client.get(chart_url)
                if response.status_code != 200:
                    logger.error(f"Failed to get chart: {response.status_code}")
                    await query.answer("Failed to get chart")
                    return
                
                data = response.json()
                if data.get("status") != "success":
                    logger.error(f"Chart service error: {data}")
                    await query.answer("Chart service error")
                    return
                
                # Convert base64 image
                image_data = base64.b64decode(data["image"])
                
                # Create keyboard with Back button
                keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                
                # Send photo with Back button
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_data,
                    caption=f"Technical Analysis for {symbol} (15m)",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                await query.answer()

        elif query.data == "back_to_signal":
            # Get original message
            original_message = messages[message_id]["text"]
            
            # Restore original message with Technical Analysis button
            keyboard = [[
                InlineKeyboardButton("📊 Technical Analysis", callback_data="technical_analysis")
            ]]
            
            await query.message.edit_text(
                text=original_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
            await query.answer()
            
    except Exception as e:
        logger.error(f"Error in button handler: {str(e)}")
        await query.answer("An error occurred")

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
