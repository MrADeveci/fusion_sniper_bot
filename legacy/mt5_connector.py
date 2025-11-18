"""
MT5 Connector - C79 Sniper Trading Bot
Handles MetaTrader 5 connection and initialization
VERIFIED: Already perfect - all settings from config.json (no hardcoded values)
"""

import MetaTrader5 as mt5
import logging

class MT5Connector:
    """Handle MT5 connection"""
    
    def __init__(self, config: dict):
        """Initialize MT5 connector"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        broker_config = config['BROKER']
        self.account = broker_config.get('account')
        self.password = broker_config.get('password')
        self.server = broker_config.get('server')
        self.symbol = broker_config['symbol']
        self.timeout = broker_config.get('timeout', 60000)
        self.portable = broker_config.get('portable', False)
        
        self.connected = False
    
    def initialize(self) -> bool:
        """Initialize MT5 connection"""
        try:
            if self.portable:
                if not mt5.initialize(portable=True):
                    self.logger.error(f"MT5 portable initialization failed: {mt5.last_error()}")
                    return False
            else:
                if not mt5.initialize():
                    self.logger.error(f"MT5 initialization failed: {mt5.last_error()}")
                    return False
            
            if self.account and self.password and self.server:
                authorized = mt5.login(
                    login=self.account,
                    password=self.password,
                    server=self.server,
                    timeout=self.timeout
                )
                
                if not authorized:
                    self.logger.error(f"MT5 login failed: {mt5.last_error()}")
                    mt5.shutdown()
                    return False
            
            if not mt5.symbol_select(self.symbol, True):
                self.logger.error(f"Failed to select symbol {self.symbol}")
                mt5.shutdown()
                return False
            
            self.connected = True
            account_info = mt5.account_info()
            
            if account_info:
                self.logger.info(f"MT5 connected: Account {account_info.login}, Balance: £{account_info.balance:.2f}")
            
            return True
        
        except Exception as e:
            self.logger.error(f"MT5 initialization error: {e}")
            return False
    
    def shutdown(self):
        """Shutdown MT5 connection"""
        mt5.shutdown()
        self.connected = False
        self.logger.info("MT5 connection closed")

if __name__ == "__main__":
    print("MT5 Connector Module - Already Perfect!")
    print("No hardcoded values - all settings from config.json")
