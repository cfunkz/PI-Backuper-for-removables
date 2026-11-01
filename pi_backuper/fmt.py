def fmt_bytes(n):
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} PB"


def fmt_eta(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m left"
    if m:
        return f"{m}m {s:02d}s left"
    return f"{s}s left"
