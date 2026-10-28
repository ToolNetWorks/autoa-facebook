"""Safe account-name extraction; generic Facebook controls are not display names."""
import re


def display_name(value: str) -> str:
    text = re.sub(r'^\(\d+\)\s*','',str(value or '')).strip()
    text = re.sub(r'\s*[|–-]\s*Facebook$','',text,flags=re.I).strip()
    if (not text or len(text)>120 or '\n' in text or
            re.search(r'your (?:account|profile)|tài khoản.*bạn|quản lý trang cá nhân|đoạn chat',text,re.I) or
            text.casefold() in {'facebook','home','trang chủ','account','profile'}):
        return ''
    return text


def matches_session(observed: dict, fingerprint: str, secret: str) -> bool:
    if not fingerprint:
        return True  # Legacy payload compatibility; new assignments always carry a fingerprint.
    import hashlib
    import hmac
    uid = str(observed.get('uid') or '')
    return bool(secret and uid.isdigit() and hmac.compare_digest(
        hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest(),fingerprint))


def session_observation(observed: dict, secret: str) -> dict:
    """Private worker proof: never return raw UID or cookie values."""
    import hashlib
    import hmac
    uid = str(observed.get('uid') or '')
    fingerprint = hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest() if secret and uid.isdigit() else ''
    return {'status':observed.get('status','unknown'),'fingerprint':fingerprint}
