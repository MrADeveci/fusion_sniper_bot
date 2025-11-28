"""
Economic News Filter - Fusion Sniper Bot
Filters high-impact economic news to avoid volatile periods
UPDATED: All settings now read from config.json - ForexFactory XML format
"""

import requests
import xml.etree.ElementTree as ET
import logging
from datetime import datetime, timedelta
from pathlib import Path
import json
import time

class EconomicNewsFilter:
    """Filter high-impact economic news events"""
    
    def __init__(self, config: dict):
        """Initialize news filter"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Load news filter config
        news_config = config.get('NEWS_FILTER', {})
        self.enabled = news_config.get('enabled', True)
        self.api_url = news_config.get('api_url', 'https://nfs.faireconomy.media/ff_calendar_thisweek.xml')
        self.buffer_before = news_config.get('buffer_before_minutes', 30)
        self.buffer_after = news_config.get('buffer_after_minutes', 30)
        self.check_interval = news_config.get('check_interval_seconds', 300)
        self.impact_levels = news_config.get('impact_levels', ['High'])
        self.monitored_currencies = news_config.get('monitored_currencies', ['USD'])
        
        # Holiday-specific buffer (convert hours to minutes)
        self.holiday_buffer = news_config.get('holiday_buffer_hours', 12) * 60
        
        # Cache settings from config
        self.cache_dir = Path(news_config.get('cache_directory', 'cache'))
        self.cache_max_age = news_config.get('cache_max_age_minutes', 10)
        self.cache_retention_days = news_config.get('cache_retention_days', 7)
        self.api_timeout = news_config.get('api_timeout_seconds', 10)
        
        # Retry settings from config
        self.max_retries = news_config.get('max_retries', 3)
        self.retry_delay = news_config.get('retry_delay_seconds', 2)
        
        # Create cache directory
        self.cache_dir.mkdir(exist_ok=True)
        
        # State
        self.events = []
        self.last_fetch = None
        
        self.logger.info(f"EconomicNewsFilter initialized")
        self.logger.info(f"Impact levels: {self.impact_levels}")
        self.logger.info(f"Monitored currencies: {self.monitored_currencies}")
        self.logger.info(f"Buffer: {self.buffer_before}min before, {self.buffer_after}min after")
    
    def fetch_news(self) -> bool:
        """Fetch news from ForexFactory XML API with retry logic"""
        if not self.enabled:
            return True
        
        for attempt in range(self.max_retries):
            try:
                self.logger.info(f"Fetching news from ForexFactory (attempt {attempt + 1}/{self.max_retries})...")
                
                response = requests.get(self.api_url, timeout=self.api_timeout)
                
                if response.status_code != 200:
                    self.logger.warning(f"API returned status {response.status_code}")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    return False
                
                # Parse XML
                root = ET.fromstring(response.content)
                
                # Clear existing events
                self.events = []
                
                # Parse events
                for event in root.findall('.//event'):
                    try:
                        title = event.find('title').text if event.find('title') is not None else ''
                        country = event.find('country').text if event.find('country') is not None else ''
                        date_str = event.find('date').text if event.find('date') is not None else ''
                        time_str = event.find('time').text if event.find('time') is not None else ''
                        impact = event.find('impact').text if event.find('impact') is not None else ''
                        url = event.find('url').text if event.find('url') is not None else ''
                        
                        # Skip if no time or date (but keep "All Day" for holidays)
                        if not date_str or not time_str or time_str == 'Tentative':
                            continue
                        
                        # Parse datetime - handle "All Day" events specially (holidays)
                        if time_str == 'All Day':
                            # Parse as noon on the date for full day coverage
                            event_datetime = datetime.strptime(f"{date_str} 12:00PM", "%m-%d-%Y %I:%M%p")
                        else:
                            event_datetime = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
                        
                        # Only track monitored currencies and impact levels
                        if country in self.monitored_currencies and impact in self.impact_levels:
                            self.events.append({
                                'title': title,
                                'currency': country,
                                'time': event_datetime.isoformat(),
                                'impact': impact,
                                'url': url
                            })
                    
                    except Exception as e:
                        self.logger.debug(f"Error parsing event: {e}")
                        continue
                
                self.last_fetch = datetime.now()
                self.logger.info(f"Fetched {len(self.events)} relevant news events")
                
                # Cache events
                self.cache_events()
                
                return True
            
            except requests.exceptions.Timeout:
                self.logger.warning(f"API timeout (attempt {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
            
            except Exception as e:
                self.logger.error(f"Error fetching news: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
        
        # All retries failed
        self.logger.error(f"Failed to fetch news after {self.max_retries} attempts")
        return False
    
    def cache_events(self):
        """Cache events to file"""
        try:
            cache_file = self.cache_dir / 'news_events.json'
            cache_data = {
                'fetched_at': datetime.now().isoformat(),
                'events': self.events
            }
            
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
            
            self.logger.debug(f"Events cached to {cache_file}")
        except Exception as e:
            self.logger.error(f"Error caching events: {e}")
    
    def load_cached_events(self) -> bool:
        """Load events from cache"""
        try:
            cache_file = self.cache_dir / 'news_events.json'
            
            if not cache_file.exists():
                return False
            
            # Check cache age
            cache_age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
            if cache_age.total_seconds() / 60 > self.cache_max_age:
                self.logger.debug("Cache expired")
                return False
            
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            self.events = cache_data['events']
            self.last_fetch = datetime.fromisoformat(cache_data['fetched_at'])
            
            self.logger.info(f"Loaded {len(self.events)} events from cache")
            return True
        
        except Exception as e:
            self.logger.error(f"Error loading cache: {e}")
            return False
    
    def cleanup_old_cache(self):
        """Clean up old cache files"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.cache_retention_days)
            deleted_count = 0
            
            for cache_file in self.cache_dir.glob('*.json'):
                file_mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if file_mtime < cutoff_date:
                    cache_file.unlink()
                    deleted_count += 1
            
            if deleted_count > 0:
                self.logger.info(f"Cleaned up {deleted_count} old cache files")
        except Exception as e:
            self.logger.error(f"Error cleaning cache: {e}")
    
    def should_avoid_trading(self) -> tuple:
        """Check if should avoid trading due to upcoming news"""
        if not self.enabled:
            return False, None
        
        # Check if need to fetch news
        if self.last_fetch is None:
            if not self.load_cached_events():
                self.fetch_news()
        else:
            time_since_fetch = (datetime.now() - self.last_fetch).total_seconds()
            if time_since_fetch > self.check_interval:
                self.fetch_news()
        
        # Check upcoming events
        now = datetime.now()
        
        for event in self.events:
            try:
                event_time = datetime.fromisoformat(event['time'])
                
                # Calculate buffer window - use extended buffer for Holiday impact
                if event.get('impact') == 'Holiday':
                    # Extended 12-hour buffer for holidays (full day coverage)
                    avoid_start = event_time - timedelta(minutes=self.holiday_buffer)
                    avoid_end = event_time + timedelta(minutes=self.holiday_buffer)
                else:
                    # Normal buffers for timed events
                    avoid_start = event_time - timedelta(minutes=self.buffer_before)
                    avoid_end = event_time + timedelta(minutes=self.buffer_after)
                
                # Check if within avoidance window
                if avoid_start <= now <= avoid_end:
                    return True, event
            
            except Exception as e:
                self.logger.debug(f"Error checking event: {e}")
                continue
        
        return False, None
    
    def get_upcoming_events(self, hours_ahead=24) -> list:
        """Get upcoming events within specified hours"""
        if not self.enabled or not self.events:
            return []
        
        now = datetime.now()
        cutoff = now + timedelta(hours=hours_ahead)
        
        upcoming = []
        for event in self.events:
            try:
                event_time = datetime.fromisoformat(event['time'])
                if now <= event_time <= cutoff:
                    upcoming.append(event)
            except:
                continue
        
        return upcoming


if __name__ == "__main__":
    # Test module
    print("Fusion Sniper Bot - Economic News Filter Module")
    print("This module filters high-impact economic news")
    print("Import this into your main bot to use news filtering")
