"""
Utility functions for Entity Resolution, Handelsregister-Nummer normalization,
and Signal Freshness evaluation.
"""

import re
from datetime import datetime, timedelta
from config import FRESHNESS_WINDOWS

def normalize_handelsregister_nr(raw_nr: str) -> str:
    """
    Normalizes a German Handelsregister registration number.
    Example inputs:
      'HRB 123456', 'HRB-123456', 'Amtsgericht München HRB 123456', 'HRA 9876'
    Returns standard string:
      'HRB-123456'
    """
    if not raw_nr:
        return ""
    
    clean = raw_nr.upper().strip()
    
    # Extract type (HRB or HRA) and digits
    match = re.search(r'\b(HRB|HRA)[\s\-_]*(\d+)', clean)
    if match:
        reg_type = match.group(1)
        reg_digits = match.group(2)
        return f"{reg_type}-{reg_digits}"
    
    # Fallback clean alphanumeric
    cleaned_str = re.sub(r'[^A-Z0-9]', '', clean)
    return cleaned_str

def is_signal_stale(signal_key: str, fetched_at: datetime) -> bool:
    """
    Determines if a signal record is stale based on its specific freshness window.
    """
    if not fetched_at:
        return True
    
    window_days = FRESHNESS_WINDOWS.get(signal_key, 90)
    cutoff_date = datetime.utcnow() - timedelta(days=window_days)
    return fetched_at < cutoff_date

def get_signal_display_status(status: str, signal_key: str, fetched_at: datetime) -> str:
    """
    Returns the dynamic tri-state status badge text:
    - 'present'
    - 'absent'
    - 'not_yet_checked'
    - 'stale'
    """
    if status == "not_yet_checked":
        return "not_yet_checked"
    if status == "absent":
        return "absent"
    if status == "present":
        if is_signal_stale(signal_key, fetched_at):
            return "stale"
        return "present"
    return status
