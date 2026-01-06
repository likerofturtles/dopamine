import re
from datetime import datetime, timedelta, timezone
from typing import Optional


def get_duration_to_seconds(duration_str: str) -> int:
    total_seconds = 0
    patterns = {
        'mon': 2592000,
        'w': 604800,
        'd': 86400,
        'h': 3600,
        'm': 60,
        's': 1
    }

    matches = re.findall(r'(\d+)\s*(mon|w|d|h|m|s)', duration_str.lower())

    for amount, unit in matches:
        total_seconds += int(amount) * patterns[unit]

    return total_seconds

def get_now_plus_seconds_unix(seconds: int) -> int:
    future_dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return int(future_dt.timestamp())