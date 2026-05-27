import requests
import hashlib
import random
import string

API_URL = 'https://api4.temp-mail.org'

def get_email_hash(email):
    """Returns MD5 hash of the email."""
    return hashlib.md5(email.encode()).hexdigest()

def get_available_domains():
    """Fetches available domains from TempMail."""
    try:
        response = requests.get(f"{API_URL}/request/domains/format/json/")
        return response.json()
    except Exception as e:
        print(f"Error fetching domains: {e}")
        return []

def generate_email(length=10):
    """Generates a random email address using available domains."""
    domains = get_available_domains()
    if not domains:
        return None
    
    name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    domain = random.choice(domains)
    return f"{name}{domain}"

def get_inbox(email):
    """Retrieves the inbox for a given email."""
    if not email:
        return None
    
    email_hash = get_email_hash(email)
    try:
        response = requests.get(f"{API_URL}/request/mail/id/{email_hash}/format/json/")
        return response.json()
    except Exception as e:
        # 404 means no mail yet
        return []
