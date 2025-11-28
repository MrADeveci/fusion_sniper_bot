"""
Risk Manager - Fusion Sniper Bot
Manages position sizing and risk parameters
UPDATED: All settings now read from config.json
"""

import MetaTrader5 as mt5
import logging

class RiskManager:
    """Manage trading risk and position sizing"""
    
    def __init__(self, config: dict):
        """Initialize risk manager"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Core risk settings
        risk_config = config['RISK']
        self.max_risk_per_trade = risk_config.get('max_risk_per_trade', 2.0)
        self.max_daily_loss = risk_config.get('max_daily_loss', 5.0)
        self.max_drawdown = risk_config.get('max_drawdown_percent', 10.0)
        self.max_positions = risk_config.get('max_positions_per_bot', 1)
        
        # Confidence-based sizing (optional)
        confidence_config = risk_config.get('confidence_based_sizing', {})
        self.use_confidence_sizing = confidence_config.get('enabled', False)
        self.min_confidence = confidence_config.get('min_confidence', 0.6)
        self.high_confidence_threshold = confidence_config.get('high_confidence_threshold', 0.8)
        
        # Confidence scaling range from config
        scaling_range = confidence_config.get('scaling_range', {})
        self.confidence_min_multiplier = scaling_range.get('min_multiplier', 0.5)
        self.confidence_max_multiplier = scaling_range.get('max_multiplier', 1.0)
        
        # Symbol info
        self.symbol = config['BROKER']['symbol']
        self.magic_number = config['BROKER']['magic_number']
        
        self.logger.info(f"RiskManager initialized")
        self.logger.info(f"Max risk per trade: {self.max_risk_per_trade}%")
        self.logger.info(f"Max positions: {self.max_positions}")
        if self.use_confidence_sizing:
            self.logger.info(f"Confidence sizing: ENABLED (range: {self.confidence_min_multiplier}-{self.confidence_max_multiplier})")
    
    def can_trade(self) -> bool:
        """Check if we can open new trades"""
        try:
            # Check position limit
            positions = mt5.positions_get(symbol=self.symbol)
            if positions is not None:
                my_positions = [p for p in positions if p.magic == self.magic_number]
                if len(my_positions) >= self.max_positions:
                    self.logger.debug(f"Position limit reached: {len(my_positions)}/{self.max_positions}")
                    return False
            
            # Check daily loss limit
            if self.max_daily_loss > 0:
                today_profit = self.get_daily_profit()
                if today_profit < 0 and abs(today_profit) >= self.max_daily_loss:
                    self.logger.warning(f"Daily loss limit reached: £{today_profit:.2f}")
                    return False
            
            # Check drawdown
            account_info = mt5.account_info()
            if account_info:
                balance = account_info.balance
                equity = account_info.equity
                
                if balance > 0:
                    drawdown_pct = ((balance - equity) / balance) * 100
                    if drawdown_pct >= self.max_drawdown:
                        self.logger.warning(f"Max drawdown reached: {drawdown_pct:.2f}%")
                        return False
            
            return True
        
        except Exception as e:
            self.logger.error(f"Error in can_trade: {e}")
            return False
    
    def get_daily_profit(self) -> float:
        """Get today's profit/loss"""
        try:
            from datetime import datetime
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            
            deals = mt5.history_deals_get(today_start, datetime.now())
            
            if deals is None:
                return 0.0
            
            daily_profit = sum([deal.profit for deal in deals if deal.magic == self.magic_number])
            return daily_profit
        
        except Exception as e:
            self.logger.error(f"Error calculating daily profit: {e}")
            return 0.0
    
    def calculate_atr_based_stops(self, entry_price: float, atr: float, direction: str):
        """Calculate ATR-based stop loss and take profit"""
        try:
            # Get multipliers from config
            trading_config = self.config['TRADING']
            sl_multiple = trading_config.get('stop_loss_atr_multiple', 1.0)
            tp_multiple = trading_config.get('take_profit_atr_multiple', 2.0)
            
            sl_distance = atr * sl_multiple
            tp_distance = atr * tp_multiple
            
            if direction == 'BUY':
                sl = entry_price - sl_distance
                tp = entry_price + tp_distance
            else:  # SELL
                sl = entry_price + sl_distance
                tp = entry_price - tp_distance
            
            return sl, tp
        
        except Exception as e:
            self.logger.error(f"Error calculating ATR stops: {e}")
            return 0, 0
    
    def validate_trade(self, order_type: str, price: float, sl: float, tp: float) -> bool:
        """Validate trade parameters"""
        try:
            # Basic validation
            if price <= 0 or sl <= 0 or tp <= 0:
                self.logger.warning("Invalid price/sl/tp values")
                return False
            
            # Check direction logic
            if order_type == 'BUY':
                if sl >= price:
                    self.logger.warning("BUY: SL must be below entry")
                    return False
                if tp <= price:
                    self.logger.warning("BUY: TP must be above entry")
                    return False
            else:  # SELL
                if sl <= price:
                    self.logger.warning("SELL: SL must be above entry")
                    return False
                if tp >= price:
                    self.logger.warning("SELL: TP must be below entry")
                    return False
            
            return True
        
        except Exception as e:
            self.logger.error(f"Error validating trade: {e}")
            return False
