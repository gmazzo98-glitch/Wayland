"""
Utility functions for Entity Resolution, Handelsregister-Nummer normalization,
and Signal Freshness evaluation.
"""

import re
from datetime import datetime, timedelta

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
    
    clean = str(raw_nr).upper().strip()
    
    # Extract type (HRB or HRA) and digits
    match = re.search(r'\b(HRB|HRA)[\s\-_]*(\d+)', clean)
    if match:
        reg_type = match.group(1)
        reg_digits = match.group(2)
        return f"{reg_type}-{reg_digits}"
    
    # Fallback clean alphanumeric
    cleaned_str = re.sub(r'[^A-Z0-9]', '', clean)
    return cleaned_str


def normalize_registration_nr(raw_nr: str, country: str = "Germany") -> str:
    """
    Normalizes registration numbers based on company country:
    - Germany: HRB-xxxx / HRA-xxxx (Handelsregister)
    - Italy: Partita IVA (11 digits e.g. IT12345678901 / 12345678901), Codice Fiscale, or REA (e.g. REA MI-123456)
    """
    if not raw_nr:
        return ""

    raw_str = str(raw_nr).strip()
    if country.lower() == "italy":
        clean = raw_str.upper().strip()
        # REA format: e.g. REA MI-123456 or REA 123456
        rea_match = re.search(r'\bREA[\s\-_]*([A-Z]{2})?[\s\-_]*(\d+)', clean)
        if rea_match:
            prov = rea_match.group(1) or ""
            num = rea_match.group(2)
            return f"REA-{prov}-{num}" if prov else f"REA-{num}"
        # Partita IVA: 11 digits (with or without 'IT' prefix)
        piva_match = re.search(r'\b(?:IT)?(\d{11})\b', clean)
        if piva_match:
            return f"IT{piva_match.group(1)}"
        # Codice Fiscale: 16 alphanumeric characters
        cf_match = re.search(r'\b([A-Z0-9]{16})\b', clean)
        if cf_match:
            return cf_match.group(1)
        # Fallback cleaned uppercase alphanumeric
        return re.sub(r'[^A-Z0-9\-]', '', clean)
    else:
        # Default German Handelsregister
        return normalize_handelsregister_nr(raw_str)

def is_signal_stale(freshness_days: int, fetched_at: datetime) -> bool:
    """
    Determines if a signal record is stale based on its specific freshness window.
    """
    if not fetched_at:
        return True

    window_days = freshness_days if freshness_days is not None else 90
    cutoff_date = datetime.utcnow() - timedelta(days=window_days)
    return fetched_at < cutoff_date

def get_signal_display_status(status: str, freshness_days: int, fetched_at: datetime) -> str:
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
        if is_signal_stale(freshness_days, fetched_at):
            return "stale"
        return "present"
    return status
