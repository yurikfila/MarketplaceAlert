"""Background scanning: one central loop that runs due saved searches.

Not one OS process or thread per saved search - `BackgroundScanner` is a
single loop that can manage many. See `scanner.py` and `guard.py`.
"""
