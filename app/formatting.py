def format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 MB"

    megabytes = num_bytes / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.2f} GB"
    return f"{megabytes:.1f} MB"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
