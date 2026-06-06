"""
Fusion Sniper - Telegram Notification Module v5.0.0
Handles all Telegram message sending for trading bot notifications
"""

import requests
from datetime import datetime
import logging

class TelegramNotifier:
    """Handles Telegram notifications for Fusion Sniper Bot"""
    
    def __init__(self, bot_token, chat_id, enabled=True):
        """
        Initialize Telegram notifier
        
        Args:
            bot_token (str): Telegram bot API token
            chat_id (str): Telegram chat ID to send messages to
            enabled (bool): Whether notifications are enabled
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.logger = logging.getLogger(__name__)
        
        if self.enabled and self.bot_token and self.chat_id:
            self.logger.info("Telegram notifications ENABLED")
            self.send_test_connection()
        else:
            self.logger.info("Telegram notifications DISABLED")
    
    def send_message(self, message, parse_mode='HTML'):
        """
        Send a message via Telegram
        
        Args:
            message (str): Message text to send
            parse_mode (str): Message format (HTML or Markdown)
        
        Returns:
            bool: True if sent successfully
        """
        if not self.enabled:
            return False
        
        if not self.bot_token or not self.chat_id:
            self.logger.warning("Telegram not configured - skipping notification")
            return False
        
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': parse_mode
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                return True
            else:
                self.logger.error(f"Telegram send failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            self.logger.error(f"Telegram send error: {e}")
            return False
    
    def send_test_connection(self):
        """Send a test message to verify connection"""
        message = "🤖 <b>Connected</b>\n\nTelegram notifications connected successfully!"
        return self.send_message(message)
    
    def notify_bot_started(self, symbol, recovered_trades=0, recovered_pnl=0.0):
        """
        Notify when bot starts
        
        Args:
            symbol (str): Trading pair symbol
            recovered_trades (int): Number of recovered positions
            recovered_pnl (float): Recovered P&L amount
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        recovery_text = ""
        if recovered_trades > 0:
            pnl_symbol = "+" if recovered_pnl >= 0 else ""
            recovery_text = f"\n\n📊 <b>Recovery Status:</b>\n• Positions: {recovered_trades}\n• P&L: {pnl_symbol}£{recovered_pnl:.2f}"
        
        message = f"""🤖 <b>Started</b>

📈 Pair: <b>{symbol}</b>
⏰ Time: {timestamp}{recovery_text}

Status: <i>Active and monitoring</i>"""
        
        self.send_message(message)
    
    def notify_trade_opened(self, symbol, direction, lot_size, entry_price, sl_price, tp_price, magic_number=None):
        """
        Notify when a trade is opened - UPDATED WITH EMOJIS

        Args:
            symbol (str): Trading pair
            direction (str): BUY or SELL
            lot_size (float): Position size in lots
            entry_price (float | str): Entry price
            sl_price (float | str): Stop loss price
            tp_price (float | str): Take profit price
            magic_number (int): Magic number (optional, not used in message)
        """
        # Use arrow indicators
        direction_emoji = "📈" if direction == "BUY" else "📉"

        # Work out how many decimal places to show from the entry price
        entry_str = str(entry_price)
        if "." in entry_str:
            decimals = len(entry_str.split(".")[1])
        else:
            decimals = 0

        price_format = f"{{:.{decimals}f}}"

        # Format SL / TP to match the entry precision
        if isinstance(sl_price, (int, float)):
            sl_str = price_format.format(sl_price)
        else:
            sl_str = str(sl_price)

        if isinstance(tp_price, (int, float)):
            tp_str = price_format.format(tp_price)
        else:
            tp_str = str(tp_price)

        message = f"""{direction_emoji} <b>Trade Opened</b>

📊 <b>Pair:</b> {symbol}
{direction_emoji} <b>Direction:</b> {direction}

📦 <b>Lot Size:</b> {lot_size}

🎯 <b>Entry:</b> {entry_str}

🛡️ <b>Stop Loss:</b> {sl_str}
💰 <b>Take Profit:</b> {tp_str}"""

        self.send_message(message)
    
    def notify_trade_closed(self, symbol, direction, lot_size, entry_price, exit_price, profit, reason):
        """
        Notify when a trade is closed - UPDATED WITH EMOJIS, REMOVED REASON LINE
        
        Args:
            symbol (str): Trading pair
            direction (str): BUY or SELL
            lot_size (float): Position size
            entry_price (float): Entry price
            exit_price (float): Exit price
            profit (float): Profit/loss amount
            reason (str): Close reason (not shown in message)
        """
        # Determine emoji based on profit
        if profit > 0:
            result_emoji = "✅"
            profit_text = f"+£{profit:.2f}"
        elif profit < 0:
            result_emoji = "❌"
            profit_text = f"-£{abs(profit):.2f}"
        else:
            result_emoji = "⚪"
            profit_text = "£0.00"
        
        # Use arrow indicators
        direction_emoji = "📈" if direction == "BUY" else "📉"
        
        message = f"""{result_emoji} <b>Trade Closed</b>

📊 <b>Pair:</b> {symbol}
{direction_emoji} <b>Direction:</b> {direction}

📦 <b>Lot Size:</b> {lot_size}

🎯 <b>Entry:</b> {entry_price}
🚪 <b>Exit:</b> {exit_price}

💵 <b>Result:</b> {profit_text}"""
        
        self.send_message(message)
    
    def notify_breakeven_activated(self, symbol, position_id, current_price):
        """
        REMOVED - Break-even notifications disabled per user request
        This function is kept for compatibility but does nothing
        
        Args:
            symbol (str): Trading pair
            position_id (int): Position ticket
            current_price (float): Current market price
        """
        # Do nothing - break-even notifications disabled
        pass
    
    def notify_paper_mode(self, symbol):
        """v5.0.0: announce that the bot is running in PAPER (dry-run) mode."""
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        message = f"""🧪 <b>PAPER MODE ACTIVE</b>

📊 <b>Pair:</b> {symbol}
⚠️ <b>No real orders will be sent.</b> Orders and SL/TP changes are SIMULATED.
⏰ <b>Time:</b> {timestamp}

<i>This is a dry run. Do not confuse with live trading.</i>"""
        self.send_message(message)

    def notify_daily_target_reached(self, symbol, daily_profit, target):
        """
        Notify when daily profit target is reached
        
        Args:
            symbol (str): Trading pair
            daily_profit (float): Current daily profit
            target (float): Daily profit target
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        message = f"""🎯 <b>Daily Target Reached!</b>

📊 <b>Pair:</b> {symbol}

💰 <b>Profit:</b> +£{daily_profit:.2f}
🎯 <b>Target:</b> £{target:.2f}

⏰ <b>Time:</b> {timestamp}

<i>Bot stopped for the day - target achieved. Good work!</i>"""
        
        self.send_message(message)
    
    def notify_daily_loss_limit(self, symbol, daily_loss, limit):
        """
        Notify when daily loss limit is reached
        
        Args:
            symbol (str): Trading pair
            daily_loss (float): Current daily loss (negative value)
            limit (float): Daily loss limit (positive value)
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        message = f"""⚠️ <b>Daily Loss Limit Reached</b>

📊 <b>Pair:</b> {symbol}

💸 <b>Loss:</b> -£{abs(daily_loss):.2f}
⚠️ <b>Limit:</b> £{limit:.2f}

⏰ <b>Time:</b> {timestamp}

<i>Bot stopped for the day - We win some, we lose some.</i>"""
        
        self.send_message(message)
    
    def notify_error(self, symbol, error_type, error_message):
        """
        Notify on critical errors
        
        Args:
            symbol (str): Trading pair
            error_type (str): Type of error
            error_message (str): Error details
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        message = f"""❌ <b>Error Alert</b>

📊 <b>Pair:</b> {symbol}

❌ <b>Type:</b> {error_type}
📝 <b>Details:</b> {error_message}

⏰ <b>Time:</b> {timestamp}

<i>Check bot logs for more details</i>"""
        
        self.send_message(message)


    def notify_weekly_profit_limit(self, symbol, weekly_profit, limit):
        """
        Notify when the weekly profit cap is reached

        Args:
            symbol (str): Trading pair
            weekly_profit (float): Current net weekly profit
            limit (float): Weekly profit cap
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")

        message = f"""✅ <b>Weekly Profit Limit Reached</b>

📊 <b>Pair:</b> {symbol}

💰 <b>Net weekly profit:</b> +£{weekly_profit:.2f}
🎯 <b>Weekly profit cap:</b> £{limit:.2f}

⏰ <b>Time:</b> {timestamp}

<i>Great job this week, now go treat yourself.</i>"""

        self.send_message(message)

    def notify_weekly_loss_limit(self, symbol, weekly_profit, limit):
        """
        Notify when the weekly loss cap is reached

        Args:
            symbol (str): Trading pair
            weekly_profit (float): Current net weekly profit. negative value
            limit (float): Weekly loss cap as positive number
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")

        message = f"""⚠️ <b>Weekly Loss Limit Reached</b>

📊 <b>Pair:</b> {symbol}

💸 <b>Net weekly profit:</b> £{weekly_profit:.2f}
⚠️ <b>Weekly loss cap:</b> £{limit:.2f}

⏰ <b>Time:</b> {timestamp}

<i>Sometimes, shit happens. Next week, we go again!</i>"""

        self.send_message(message)
     
    def notify_connection_lost(self, symbol):
        """
        Notify when MT5 connection is lost
        
        Args:
            symbol (str): Trading pair
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        message = f"""⚠️ <b>Connection Lost</b>

📊 <b>Pair:</b> {symbol}
❌ <b>Status:</b> MT5 connection disconnected
⏰ <b>Time:</b> {timestamp}

<i>Bot attempting to reconnect</i>"""
        
        self.send_message(message)
    
    def notify_shutdown(self, symbol):
        """
        Notify when bot is shutting down
        
        Args:
            symbol (str): Trading pair
        """
        timestamp = datetime.now().strftime("%d/%m/%Y %I:%M:%S %p")
        
        message = f"""🤖 <b>Shutdown</b>

📊 <b>Pair:</b> {symbol}
⏰ <b>Time:</b> {timestamp}

<i>Bot stopped successfully</i>"""
        
        self.send_message(message)
    
    # v5.0.0 (L4): removed unused notifiers send_daily_summary, notify_daily_progress,
    # notify_trade_closed_with_progress, notify_target_reached, notify_midnight_reset,
    # notify_friday_warning, notify_trailing_activated (none were called by the bot).

    def notify_news_avoidance(self, news_event):
        """
        Notify when trading is paused due to news event
        Sends once per event to avoid spam
        
        Args:
            news_event (dict): News event dictionary with title, time, currency, impact
        """
        try:
            event_time = datetime.fromisoformat(news_event['time'])
            time_str = event_time.strftime('%d/%m/%Y %I:%M %p')
            
            # Calculate when avoidance ends (buffer depends on impact type)
            from datetime import timedelta
            if news_event.get('impact') == 'Holiday':
                # Holiday events: 12 hour buffer (720 minutes)
                avoidance_end = event_time + timedelta(minutes=720)
                end_time_str = avoidance_end.strftime('%a %d/%m %I:%M %p')  # Include day for 12hr buffer
            else:
                # High impact events: 30 minute buffer
                avoidance_end = event_time + timedelta(minutes=30)
                end_time_str = avoidance_end.strftime('%I:%M %p')
            
            # Get URL if available
            event_url = news_event.get('url', '')
            url_line = f"🔗 <a href='{event_url}'>View Details on ForexFactory</a>\n\n" if event_url else ""

            message = f"""⚠️ <b>Trading Paused - News Event</b>

📰 <b>Event:</b> {news_event['title']}
🌍 <b>Currency:</b> {news_event['currency']}
🔴 <b>Impact:</b> {news_event['impact']}
{url_line}⏰ <b>Event Time:</b> {time_str}
🔒 <b>Trading Resumes:</b> {end_time_str}

<i>Bot active, monitoring will resume after buffer period</i>"""
            
            self.send_message(message)
        
        except Exception as e:
            self.logger.error(f"Error sending news avoidance notification: {e}")
    
    def send_weekly_news_summary(self, events):
        """
        Send weekly summary of upcoming news events
        Sent once per week on Sunday before market open
        
        Args:
            events (list): List of upcoming event dictionaries
        """
        try:
            today_str = datetime.now().strftime('%d/%m/%Y')
            
            if not events:
                message = f"""📅 <b>WEEKLY NEWS SUMMARY</b> - Week of {today_str}
━━━━━━━━━━━━━━━━━━━━━━

✅ <b>No high-impact news this week</b>

<i>Clear to trade - no scheduled events</i>
Markets open Monday 1am"""
                self.send_message(message)
                return
            
            message = f"""📅 <b>WEEKLY NEWS SUMMARY</b> - Week of {today_str}
━━━━━━━━━━━━━━━━━━━━━━

📰 <b>High Impact Events (This Week):</b>

"""

            for event in events[:10]:  # Limit to 10 events for weekly view
                event_time = datetime.fromisoformat(event['time'])
                day_str = event_time.strftime('%a')  # Mon, Tue, Wed, etc.
                time_str = event_time.strftime('%I:%M %p')
                
                # Impact emoji
                impact_emoji = "🔴" if event['impact'] == "High" else "🏖️"
                
                # Get URL if available
                event_url = event.get('url', '')
                url_line = f"   🔗 <a href='{event_url}'>View on ForexFactory</a>\n" if event_url else ""
                
                message += f"""{impact_emoji} <b>{day_str} {time_str}</b> | {event['currency']}
    {event['title']}
{url_line}   🔒 Trading paused: 30min before/after

"""

            event_count = len(events)
            message += f"""━━━━━━━━━━━━━━━━━━━━━━
<b>Total Events:</b> {event_count}
<i>Markets open Monday 01:00</i>"""

            self.send_message(message)
        
        except Exception as e:
            self.logger.error(f"Error sending weekly news summary: {e}")


if __name__ == "__main__":
    # Test module
    print("Fusion Sniper - Telegram Notifier Module v5.0.0")
    print("This module handles all Telegram notifications")
    print("Import this into your main bot to use notifications")