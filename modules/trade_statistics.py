"""
Trade Statistics Tracker - Fusion Sniper Bot
Tracks and analyzes trading performance
UPDATED: All settings now read from config.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

class TradeStatistics:
    """Track and analyze trade performance"""
    
    def __init__(self, config: dict):
        """Initialize statistics tracker"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Get symbol for file naming
        self.symbol = config['BROKER']['symbol']
        
        # Load statistics config
        stats_config = config.get('STATISTICS', {})
        self.enabled = stats_config.get('enabled', True)
        
        # Get stats file path from config (with symbol replacement)
        stats_file_pattern = stats_config.get('log_file', 'logs/trade_statistics_{symbol}.json')
        self.stats_file = stats_file_pattern.replace('{symbol}', self.symbol)
        
        self.max_trades_history = stats_config.get('max_trades_history', 100)
        
        # Tracking flags
        self.track_mae = stats_config.get('track_mae', True)
        self.track_mfe = stats_config.get('track_mfe', True)
        self.track_session = stats_config.get('track_session_performance', True)
        self.track_exit_reasons = stats_config.get('track_exit_reasons', True)
        
        # Current trade tracking
        self.current_trade = None
        
        # Load existing statistics
        self.stats = self.load_stats()
        
        self.logger.info(f"TradeStatistics initialized")
        self.logger.info(f"Stats file: {self.stats_file}")
        self.logger.info(f"Max history: {self.max_trades_history} trades")
    
    def load_stats(self) -> Dict:
        """Load statistics from file"""
        try:
            stats_path = Path(self.stats_file)
            if stats_path.exists():
                with open(stats_path, 'r') as f:
                    return json.load(f)
            else:
                return self.create_new_stats()
        except Exception as e:
            self.logger.error(f"Error loading stats: {e}")
            return self.create_new_stats()
    
    def create_new_stats(self) -> Dict:
        """Create new statistics structure"""
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_profit': 0.0,
            'total_loss': 0.0,
            'win_rate': 0.0,
            'average_profit': 0.0,
            'average_win': 0.0,
            'average_loss': 0.0,
            'best_trade': 0.0,
            'worst_trade': 0.0,
            'average_mae': 0.0,
            'average_mfe': 0.0,
            'profit_factor': 0.0,
            'trades_by_session': {
                'london': 0,
                'new_york': 0,
                'asia': 0,
                'unknown': 0
            },
            'exit_reasons': {
                'take_profit': 0,
                'stop_loss': 0,
                'trailing': 0,
                'breakeven': 0,
                'manual': 0,
                'other': 0
            },
            'trade_history': []
        }
    
    def save_stats(self):
        """Save statistics to file"""
        try:
            stats_path = Path(self.stats_file)
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(stats_path, 'w') as f:
                json.dump(self.stats, f, indent=2)
            
            self.logger.debug(f"Statistics saved to {self.stats_file}")
        except Exception as e:
            self.logger.error(f"Error saving stats: {e}")
    
    def start_trade(self, trade_info: Dict):
        """Start tracking a new trade"""
        if not self.enabled:
            return
        
        try:
            self.current_trade = {
                'ticket': trade_info.get('ticket'),
                'order_type': trade_info.get('order_type'),
                'entry_price': trade_info.get('entry_price'),
                'lot_size': trade_info.get('lot_size'),
                'stop_loss': trade_info.get('stop_loss'),
                'take_profit': trade_info.get('take_profit'),
                'atr': trade_info.get('atr'),
                'spread': trade_info.get('spread'),
                'conditions_met': trade_info.get('conditions_met', 0),
                'conditions_detail': trade_info.get('conditions_detail', []),
                'confidence': trade_info.get('confidence', 1.0),
                'session': trade_info.get('session', 'unknown'),
                'volatility_mode': trade_info.get('volatility_mode', 'normal'),
                'entry_time': datetime.now().isoformat(),
                'max_adverse_excursion': 0.0,
                'max_favorable_excursion': 0.0,
            }
            
            self.logger.info(f"Started tracking trade #{trade_info.get('ticket')}")
        except Exception as e:
            self.logger.error(f"Error starting trade tracking: {e}")
    
    def update_trade(self, update_info: Dict):
        """Update current trade with real-time data"""
        if not self.enabled or self.current_trade is None:
            return
        
        try:
            current_profit = update_info.get('current_profit', 0)
            
            # Track MAE (Max Adverse Excursion) if enabled
            if self.track_mae and current_profit < self.current_trade['max_adverse_excursion']:
                self.current_trade['max_adverse_excursion'] = current_profit
            
            # Track MFE (Max Favorable Excursion) if enabled
            if self.track_mfe and current_profit > self.current_trade['max_favorable_excursion']:
                self.current_trade['max_favorable_excursion'] = current_profit
        
        except Exception as e:
            self.logger.error(f"Error updating trade: {e}")
    
    def end_trade(self, close_info: Dict):
        """End trade tracking and update statistics"""
        if not self.enabled or self.current_trade is None:
            return
        
        try:
            # Complete trade info
            self.current_trade.update({
                'exit_price': close_info.get('exit_price'),
                'exit_reason': close_info.get('exit_reason', 'unknown'),
                'profit': close_info.get('profit'),
                'profit_pips': close_info.get('profit_pips'),
                'expected_exit': close_info.get('expected_exit'),
                'exit_time': datetime.now().isoformat()
            })
            
            # Update overall statistics
            self.update_overall_stats(self.current_trade)
            
            # Add to history (maintain max history size)
            self.stats['trade_history'].append(self.current_trade)
            if len(self.stats['trade_history']) > self.max_trades_history:
                self.stats['trade_history'] = self.stats['trade_history'][-self.max_trades_history:]
            
            # Save updated stats
            self.save_stats()
            
            self.logger.info(f"Ended tracking trade #{self.current_trade['ticket']}")
            self.current_trade = None
        
        except Exception as e:
            self.logger.error(f"Error ending trade tracking: {e}")
    
    def update_overall_stats(self, trade: Dict):
        """Update overall statistics with completed trade"""
        try:
            profit = trade['profit']
            
            # Update totals
            self.stats['total_trades'] += 1
            
            if profit > 0:
                self.stats['winning_trades'] += 1
                self.stats['total_profit'] += profit
                
                # Update best trade
                if profit > self.stats['best_trade']:
                    self.stats['best_trade'] = profit
            else:
                self.stats['losing_trades'] += 1
                self.stats['total_loss'] += abs(profit)
                
                # Update worst trade
                if profit < self.stats['worst_trade']:
                    self.stats['worst_trade'] = profit
            
            # Calculate averages
            if self.stats['total_trades'] > 0:
                net_profit = self.stats['total_profit'] - self.stats['total_loss']
                self.stats['average_profit'] = net_profit / self.stats['total_trades']
                
                if self.stats['winning_trades'] > 0:
                    self.stats['average_win'] = self.stats['total_profit'] / self.stats['winning_trades']
                
                if self.stats['losing_trades'] > 0:
                    self.stats['average_loss'] = self.stats['total_loss'] / self.stats['losing_trades']
                
                # Win rate
                self.stats['win_rate'] = (self.stats['winning_trades'] / self.stats['total_trades']) * 100
                
                # Profit factor
                if self.stats['total_loss'] > 0:
                    self.stats['profit_factor'] = self.stats['total_profit'] / self.stats['total_loss']
            
            # Update MAE/MFE averages if enabled
            if self.track_mae:
                total_mae = sum([t.get('max_adverse_excursion', 0) for t in self.stats['trade_history']])
                self.stats['average_mae'] = total_mae / len(self.stats['trade_history']) if self.stats['trade_history'] else 0
            
            if self.track_mfe:
                total_mfe = sum([t.get('max_favorable_excursion', 0) for t in self.stats['trade_history']])
                self.stats['average_mfe'] = total_mfe / len(self.stats['trade_history']) if self.stats['trade_history'] else 0
            
            # Update session statistics if enabled
            if self.track_session:
                session = trade.get('session', 'unknown')
                if session in self.stats['trades_by_session']:
                    self.stats['trades_by_session'][session] += 1
            
            # Update exit reasons if enabled
            if self.track_exit_reasons:
                exit_reason = trade.get('exit_reason', 'other').lower()
                
                # Map exit reasons to categories
                if 'take profit' in exit_reason or 'tp' in exit_reason:
                    self.stats['exit_reasons']['take_profit'] += 1
                elif 'stop loss' in exit_reason or 'sl' in exit_reason:
                    self.stats['exit_reasons']['stop_loss'] += 1
                elif 'trail' in exit_reason:
                    self.stats['exit_reasons']['trailing'] += 1
                elif 'breakeven' in exit_reason or 'break-even' in exit_reason:
                    self.stats['exit_reasons']['breakeven'] += 1
                elif 'manual' in exit_reason:
                    self.stats['exit_reasons']['manual'] += 1
                else:
                    self.stats['exit_reasons']['other'] += 1
        
        except Exception as e:
            self.logger.error(f"Error updating overall stats: {e}")


if __name__ == "__main__":
    # Test module
    print("Fusion Sniper Bot - Trade Statistics Module")
    print("This module tracks and analyzes trading performance")
    print("Import this into your main bot to use statistics tracking")
