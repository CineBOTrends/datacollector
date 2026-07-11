"""
Centralized Logger Module for BMS Scraper
Box/Banner style text-based logging with proper log levels
Uses plain text format for reliable log visibility
"""
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

IST = timezone(timedelta(hours=5, minutes=30))


class FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every emit for immediate log visibility"""
    
    def emit(self, record):
        super().emit(record)
        self.flush()


class BoxBannerFormatter(logging.Formatter):
    """Custom formatter with Box/Banner style log levels (plain text)"""
    
    LEVEL_BANNERS = {
        'DEBUG':    '[======= DEBUG ======]',
        'INFO':     '[======= INFO =======]',
        'WARNING':  '[====== WARNING =====]',
        'ERROR':    '[======= ERROR ======]',
        'CRITICAL': '[====== CRITICAL ====]',
        'SUCCESS':  '[====== SUCCESS =====]',
        'START':    '[======= START ======]',
        'DONE':     '[======== DONE ======]',
        'WAIT':     '[======== WAIT ======]',
        'PROGRESS': '[===== PROGRESS =====]',
        'RATE_LIMIT': '[==== RATE LIMIT ====]',
        'STATS':    '[======= STATS ======]',
    }
    
    def format(self, record):
        # Get IST timestamp
        ist_time = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
        
        # Get banner for level (default to INFO style if custom level)
        level_name = getattr(record, 'custom_level', record.levelname)
        banner = self.LEVEL_BANNERS.get(level_name, f'[======= {level_name:^7} ======]')
        
        # Get shard context if available
        shard = getattr(record, 'shard', '')
        shard_prefix = f'[SHARD{shard}] ' if shard else ''
        
        return f"[{ist_time}] {banner} {shard_prefix}{record.getMessage()}"


class BMSLogger:
    """Logger class for BMS Scraper with Box/Banner formatting"""
    
    def __init__(self, shard_id=None, log_file=None):
        self.shard_id = shard_id
        self.logger = logging.getLogger(f'bms_shard_{shard_id}' if shard_id else 'bms_main')
        self.logger.setLevel(logging.DEBUG)
        
        # Prevent duplicate handlers
        if self.logger.handlers:
            return
        
        # Use plain text BoxBanner format for all environments
        console_formatter = BoxBannerFormatter()
        
        # Console handler with auto-flush for immediate visibility
        console_handler = FlushingStreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        # File handler (if log_file provided)
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=5*1024*1024,  # 5MB
                backupCount=3,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(BoxBannerFormatter())
            self.logger.addHandler(file_handler)
    
    def _log(self, level, msg, custom_level=None):
        """Internal log method with shard context"""
        extra = {'shard': self.shard_id}
        if custom_level:
            extra['custom_level'] = custom_level
        self.logger.log(level, msg, extra=extra)
    
    def debug(self, msg):
        """Debug level log"""
        self._log(logging.DEBUG, msg)
    
    def info(self, msg):
        """Info level log"""
        self._log(logging.INFO, msg)
    
    def warn(self, msg):
        """Warning level log"""
        self._log(logging.WARNING, msg)
    
    def error(self, msg):
        """Error level log"""
        self._log(logging.ERROR, msg)
    
    def critical(self, msg):
        """Critical level log"""
        self._log(logging.CRITICAL, msg)
    
    # Custom semantic log levels
    def success(self, msg):
        """Success log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='SUCCESS')
    
    def start(self, msg):
        """Start log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='START')
    
    def done(self, msg):
        """Done/completion log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='DONE')
    
    def wait(self, msg):
        """Waiting log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='WAIT')
    
    def progress(self, msg):
        """Progress log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='PROGRESS')
    
    def rate_limit(self, msg):
        """Rate limit specific log (uses WARNING level with custom banner)"""
        self._log(logging.WARNING, msg, custom_level='RATE_LIMIT')
    
    def stats(self, msg):
        """Statistics/metrics log (uses INFO level with custom banner)"""
        self._log(logging.INFO, msg, custom_level='STATS')
    
    def separator(self, char="-", length=60):
        """Print a separator line without banner formatting"""
        line = char * length
        # Output directly to stdout with flush for immediate visibility
        print(line, flush=True)
        # Also write to file if there's a file handler
        for handler in self.logger.handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.stream.write(line + "\n")
                handler.stream.flush()


def get_logger(shard_id=None, log_file=None):
    """Factory function to get a configured logger"""
    return BMSLogger(shard_id=shard_id, log_file=log_file)
