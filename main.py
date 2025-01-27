import os
import json
import logging
import httpx
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext, Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler
from dotenv import load_dotenv
import base64
import asyncio

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more detailed logs
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler('telegram_service.log')  # Also log to file
    ]
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Telegram Service",
    description="Service for sending trading signals via Telegram",
    version="1.0.0"
)

# Initialize Telegram bot
BOT_TOKEN = "7583525993:AAFp90r7UqCY2KdGufKgHHjjslBy7AnY_Sg"  # Using same token as subscriber matcher
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

# Service URLs
SIGNAL_AI_SERVICE = os.getenv("SIGNAL_AI_SERVICE", "https://tradingview-signal-ai-service-production.up.railway.app")
NEWS_AI_SERVICE = os.getenv("NEWS_AI_SERVICE", "https://tradingview-news-ai-service-production.up.railway.app")
CHART_SERVICE = os.getenv("CHART_SERVICE", "https://tradingview-chart-service-production.up.railway.app")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://tradingview-telegram-service-production.up.railway.app/webhook")

logger.info(f"Initialized with services: SIGNAL={SIGNAL_AI_SERVICE}, NEWS={NEWS_AI_SERVICE}, CHART={CHART_SERVICE}")

# Initialize bot
bot = Bot(token=BOT_TOKEN)

# Store user states
user_states = {}

# Market specific settings
MARKET_SETTINGS = {
    # Forex pairs (1 pip = 0.0001)
    "EURUSD": {"pip_value": 0.0001, "decimals": 4},
    "GBPUSD": {"pip_value": 0.0001, "decimals": 4},
    "USDJPY": {"pip_value": 0.01, "decimals": 3},    # JPY pairs use 2 decimals
    "AUDUSD": {"pip_value": 0.0001, "decimals": 4},
    "USDCAD": {"pip_value": 0.0001, "decimals": 4},
    
    # Crypto (different point values)
    "BTCUSD": {"pip_value": 1, "decimals": 1},      # 1 point = $1
    "ETHUSD": {"pip_value": 0.1, "decimals": 2},    # 0.1 point = $0.1
    "XRPUSD": {"pip_value": 0.0001, "decimals": 4}, # More precise for lower value coins
    
    # Indices
    "US30": {"pip_value": 1, "decimals": 0},        # 1 point = $1
    "SPX500": {"pip_value": 0.25, "decimals": 2},   # 0.25 points
    "NAS100": {"pip_value": 0.25, "decimals": 2},   # 0.25 points
    
    # Commodities
    "XAUUSD": {"pip_value": 0.1, "decimals": 2},    # Gold (0.1 point = $0.1)
    "XAGUSD": {"pip_value": 0.01, "decimals": 3},   # Silver (0.01 point)
    "WTIUSD": {"pip_value": 0.01, "decimals": 2},   # Oil (0.01 point)
}

def calculate_rr_levels(instrument: str, entry_price: float, direction: str, risk_pips: float = None, risk_points: float = None) -> dict:
    """
    Calculate take profit level based on 1:1 risk-reward ratio.
    
    Args:
        instrument: Trading instrument (e.g., 'EURUSD', 'BTCUSD')
        entry_price: Entry price for the trade
        direction: Trade direction ('buy' or 'sell')
        risk_pips: Risk in pips (for forex)
        risk_points: Risk in points (for other markets)
    
    Returns:
        dict: Contains calculated stop loss and take profit levels
    """
    try:
        # Get market settings
        settings = MARKET_SETTINGS.get(instrument.upper())
        if not settings:
            logger.warning(f"No settings found for {instrument}, using default forex settings")
            settings = {"pip_value": 0.0001, "decimals": 4}
        
        pip_value = settings["pip_value"]
        decimals = settings["decimals"]
        
        # Calculate risk in points if given in pips
        if risk_pips is not None:
            risk_points = risk_pips * pip_value
        
        # Calculate stop loss and take profit
        if direction.lower() == "buy":
            stop_loss = round(entry_price - risk_points, decimals)
            take_profit = round(entry_price + risk_points, decimals)
        else:  # sell
            stop_loss = round(entry_price + risk_points, decimals)
            take_profit = round(entry_price - risk_points, decimals)
        
        return {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_points": risk_points,
            "risk_pips": risk_points / pip_value if pip_value else None,
            "pip_value": pip_value
        }
    except Exception as e:
        logger.error(f"Error calculating RR levels: {str(e)}")
        return None

async def get_tradingview_url(symbol: str, timeframe: str) -> str:
    """Generate TradingView chart URL"""
    # Map common symbols to TradingView format
    tv_symbol_map = {
        "EURUSD": "FX:EURUSD",
        "GBPUSD": "FX:GBPUSD",
        "USDJPY": "FX:USDJPY",
        "BTCUSD": "BINANCE:BTCUSDT",
        "ETHUSD": "BINANCE:ETHUSDT",
        "US30": "DJ:DJI",
        "SPX500": "SP:SPX",
        "NAS100": "NASDAQ:NDX",
        "XAUUSD": "OANDA:XAUUSD"
    }
    
    tv_timeframe_map = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "D",
        "1w": "W"
    }
    
    tv_symbol = tv_symbol_map.get(symbol.upper(), symbol)
    tv_timeframe = tv_timeframe_map.get(timeframe.lower(), "60")
    
    # Generate TradingView chart URL
    url = f"https://www.tradingview.com/chart/?symbol={tv_symbol}&interval={tv_timeframe}"
    logger.info(f"Generated TradingView URL: {url}")
    return url

async def get_tradingview_widget_html(symbol: str, timeframe: str) -> str:
    """Generate TradingView widget HTML"""
    # Map common symbols to TradingView format
    tv_symbol_map = {
        "EURUSD": "FX:EURUSD",
        "GBPUSD": "FX:GBPUSD",
        "USDJPY": "FX:USDJPY",
        "BTCUSD": "BINANCE:BTCUSDT",
        "ETHUSD": "BINANCE:ETHUSDT",
        "US30": "DJ:DJI",
        "SPX500": "SP:SPX",
        "NAS100": "NASDAQ:NDX",
        "XAUUSD": "OANDA:XAUUSD"
    }
    
    tv_timeframe_map = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "D",
        "1w": "W"
    }
    
    tv_symbol = tv_symbol_map.get(symbol.upper(), symbol)
    tv_timeframe = tv_timeframe_map.get(timeframe.lower(), "60")
    
    # Generate TradingView widget HTML
    widget_html = f'''
    <!-- TradingView Widget BEGIN -->
    <div class="tradingview-widget-container">
      <div id="tradingview_chart"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget(
      {{
        "width": "100%",
        "height": 500,
        "symbol": "{tv_symbol}",
        "interval": "{tv_timeframe}",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "toolbar_bg": "#f1f3f6",
        "enable_publishing": false,
        "hide_side_toolbar": false,
        "allow_symbol_change": true,
        "container_id": "tradingview_chart"
      }});
      </script>
    </div>
    <!-- TradingView Widget END -->
    '''
    return widget_html

async def get_chart_image(instrument: str, timeframe: str) -> Optional[str]:
    """Get chart screenshot from Chart Service"""
    try:
        logger.info(f"Requesting chart for {instrument} {timeframe}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{CHART_SERVICE}/capture-chart",
                json={"symbol": instrument, "timeframe": timeframe},
                timeout=30.0
            )
            response.raise_for_status()
            data = response.json()
            return data.get("image")
    except Exception as e:
        logger.exception(f"Error getting chart image: {str(e)}")
        return None

@app.post("/calculate-rr")
async def calculate_risk_reward(
    instrument: str,
    entry_price: float,
    direction: str,
    risk_pips: float = None,
    risk_points: float = None
):
    """Calculate 1:1 risk-reward levels for a given trade setup"""
    try:
        if not risk_pips and not risk_points:
            raise HTTPException(status_code=400, detail="Either risk_pips or risk_points must be provided")
            
        result = calculate_rr_levels(
            instrument=instrument,
            entry_price=entry_price,
            direction=direction,
            risk_pips=risk_pips,
            risk_points=risk_points
        )
        
        if not result:
            raise HTTPException(status_code=500, detail="Error calculating RR levels")
            
        return {
            "status": "success",
            "data": {
                "instrument": instrument,
                "direction": direction,
                "levels": result,
                "message": f"Calculated 1:1 RR levels for {instrument} {direction} trade"
            }
        }
    except Exception as e:
        logger.error(f"Error in calculate_risk_reward endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Initialize bot and set webhook
async def init_webhook():
    """Initialize bot and set webhook"""
    try:
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Current webhook info: {webhook_info.url}")
        
        if webhook_info.url != WEBHOOK_URL:
            logger.info(f"Setting webhook to: {WEBHOOK_URL}")
            await bot.set_webhook(url=WEBHOOK_URL)
            logger.info("Webhook set successfully")
    except Exception as e:
        logger.error(f"Failed to set webhook: {str(e)}")
        raise

@app.on_event("startup")
async def startup():
    """Run startup tasks"""
    await init_webhook()

class SignalRequest(BaseModel):
    chat_id: int
    signal_data: Dict[str, Any]
    news_data: Optional[Dict[str, Any]] = None

@app.post("/send-signal")
async def send_signal(signal_request: SignalRequest) -> dict:
    """Send a trading signal to Telegram"""
    try:
        logger.info(f"Received signal request for chat_id: {signal_request.chat_id}")
        
        # Store signal data in user state
        if signal_request.chat_id not in user_states:
            user_states[signal_request.chat_id] = {}
        
        user_states[signal_request.chat_id].update({
            "signal_data": signal_request.signal_data,
            "news_data": signal_request.news_data
        })
        
        # Get signal data
        signal_data = signal_request.signal_data
        
        # Send signal to Signal AI Service for formatting if not already formatted
        if "formatted_message" not in signal_data:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        f"{SIGNAL_AI_SERVICE}/format-signal",
                        json=signal_data
                    )
                    response.raise_for_status()
                    formatted_message = response.json()["formatted_message"]
            except Exception as e:
                logger.error(f"Failed to get formatted message from Signal AI Service: {e}")
                # Fallback to basic formatting
                formatted_message = format_signal_message(signal_data)
        else:
            formatted_message = signal_data["formatted_message"]
            
        # Get news analysis if news data is provided
        if signal_request.news_data:
            try:
                news_analysis = await get_news_analysis(
                    signal_request.news_data["instrument"],
                    signal_request.news_data["articles"]
                )
                formatted_message += f"\n\n{news_analysis}"
            except Exception as e:
                logger.error(f"Failed to get news analysis: {e}")
        
        # Create keyboard markup
        keyboard = [
            [
                InlineKeyboardButton("Market Sentiment 📊", callback_data=f"sentiment_{signal_data['instrument']}"),
                InlineKeyboardButton("Technical Analysis 📈", callback_data=f"chart_{signal_data['instrument']}_{signal_data['timeframe']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message
        message = await bot.send_message(
            chat_id=signal_request.chat_id,
            text=formatted_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        # Store original message for back button
        user_states[signal_request.chat_id].update({
            "original_message_id": message.message_id,
            "original_text": formatted_message,
            "original_markup": reply_markup
        })
        
        return {"status": "success", "message": "Signal sent successfully"}
        
    except Exception as e:
        logger.error(f"Error sending signal: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to send signal: {str(e)}")

async def generate_and_store_chart(chat_id: int, instrument: str, timeframe: str):
    """Generate chart image and store in user state"""
    try:
        logger.info(f"Generating chart for {instrument} {timeframe}")
        
        # Call chart service
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{CHART_SERVICE}/capture-chart",
                json={"symbol": instrument, "timeframe": timeframe}
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == "success":
                chart_image = data.get("image")
                if chart_image and chat_id in user_states:
                    user_states[chat_id]["chart_image"] = chart_image
                    logger.info(f"Chart image stored for chat_id {chat_id}")
                else:
                    logger.error(f"Failed to store chart for chat_id {chat_id}")
            else:
                logger.error(f"Chart service returned error: {data}")
            
    except Exception as e:
        logger.exception("Error generating chart")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        data = await request.json()
        logger.info(f"Received webhook data: {json.dumps(data, indent=2)}")
        update = Update.de_json(data, bot)
        
        if update.callback_query:
            query = update.callback_query
            chat_id = query.message.chat.id
            message_id = query.message.message_id
            callback_data = query.data
            logger.info(f"Received callback query - chat_id: {chat_id}, callback_data: {callback_data}")
            
            if chat_id not in user_states:
                logger.info(f"Creating new state for chat_id {chat_id}")
                user_states[chat_id] = {}
            
            # Handle sentiment button
            if callback_data.startswith("sentiment_"):
                instrument = callback_data.split("_")[1]
                logger.info(f"Processing sentiment analysis for {instrument}")
                
                try:
                    # Get latest news
                    news_data = {
                        "instrument": instrument,
                        "articles": [
                            {"title": "COMMENT-EUR/USD longs need Fed, data to validate bullish techs, spreads", 
                             "content": "COMMENT-EUR/USD longs need Fed, data to validate bullish techs, spreads"},
                            {"title": "FX options wrap - Safe-haven vols surge as risk aversion bites",
                             "content": "FX options wrap - Safe-haven vols surge as risk aversion bites"}
                        ]
                    }
                    
                    # Get sentiment analysis
                    analysis = await get_news_analysis(instrument, news_data["articles"])
                    
                    # Create back button
                    keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # Send analysis
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=analysis,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                    
                except Exception as e:
                    logger.error(f"Error processing sentiment: {str(e)}")
                    await query.answer("Error processing sentiment analysis")
            
            # Handle chart button
            elif callback_data.startswith("chart_"):
                logger.info(f"Processing technical analysis request: {callback_data}")
                try:
                    parts = callback_data.split("_")
                    if len(parts) != 3:
                        raise ValueError(f"Invalid callback data format: {callback_data}")
                        
                    instrument = parts[1]
                    timeframe = parts[2]
                    
                    # Create back button
                    keyboard = [[InlineKeyboardButton("« Back to Signal", callback_data="back_to_signal")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # Show loading message
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"📊 Loading chart for {instrument} ({timeframe})...",
                        reply_markup=reply_markup
                    )
                    
                    # Get chart image
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.post(
                            f"{CHART_SERVICE}/capture-chart",
                            json={"symbol": instrument, "timeframe": timeframe}
                        )
                        response.raise_for_status()
                        chart_data = response.json()
                        
                    if "image" in chart_data:
                        # Convert base64 to bytes
                        image_bytes = base64.b64decode(chart_data["image"])
                        
                        # Send chart
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=image_bytes,
                            caption=f"📈 Technical Analysis for {instrument} ({timeframe})",
                            reply_markup=reply_markup
                        )
                    else:
                        raise ValueError("No image data in response")
                        
                except Exception as e:
                    logger.error(f"Error processing chart: {str(e)}")
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"❌ Error generating chart: {str(e)}",
                        reply_markup=reply_markup
                    )
            
            # Handle back button
            elif callback_data == "back_to_signal":
                logger.info("Processing back to signal request")
                try:
                    # Get original message
                    state = user_states.get(chat_id, {})
                    original_message = state.get("original_message")
                    
                    if not original_message:
                        await query.answer("Original message not found")
                        return
                    
                    # Recreate original keyboard
                    keyboard = [
                        [
                            InlineKeyboardButton("Market Sentiment 📊", callback_data=f"sentiment_{instrument}"),
                            InlineKeyboardButton("Technical Analysis 📈", callback_data=f"chart_{instrument}_{timeframe}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # Send original message
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=original_message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logger.error(f"Error returning to signal: {str(e)}")
                    await query.answer("Error returning to signal")
            
            await query.answer()
            return {"status": "success"}
            
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}

async def get_news_analysis(instrument: str, articles: List[Dict[str, str]]) -> Dict[str, Any]:
    """Get news analysis from News AI Service"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NEWS_AI_SERVICE}/analyze-news",
                json={"instrument": instrument, "articles": articles}
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"News AI Service failed: {response.text}")
                return None
    except Exception as e:
        logger.warning(f"Error getting news analysis: {str(e)}")
        return None

def format_signal_message(signal_data: Dict[str, Any]) -> str:
    """Format signal message"""
    try:
        # Create basic signal format
        direction_emoji = "📈" if signal_data.get("direction", "").lower() == "buy" else "📉"
        
        message = f"""🚨 New Trading Signal 🚨

Instrument: {signal_data.get('instrument', 'Unknown')}
Action: {signal_data.get('direction', 'Unknown').upper()} {direction_emoji}

Entry Price: {signal_data.get('entry_price', 'Unknown')}
Stop Loss: {signal_data.get('stop_loss', 'Unknown')} 🛑
Take Profit: {signal_data.get('entry_price', 0) + 200} 🎯

Timeframe: {signal_data.get('timeframe', 'Unknown')}
Strategy: {signal_data.get('strategy', 'Unknown')}

--------------------

Risk Management:
• Position size: 1-2% max
• Use proper stop loss
• Follow your trading plan

--------------------

🤖 SigmaPips AI Verdict:
This {signal_data.get('instrument', '')} {signal_data.get('direction', '').lower()} signal follows a trend-following strategy within a {signal_data.get('timeframe', '')}-hour timeframe, aiming for a tight profit margin with a calculated risk/reward ratio. The setup suggests confidence in the current uptrend's continuity, making it a promising short-term trade."""
        
        return message
    except Exception as e:
        logger.exception("Error formatting signal message")
        return "Error formatting signal message"

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    return {"status": "ok", "service": "tradingview-telegram-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
