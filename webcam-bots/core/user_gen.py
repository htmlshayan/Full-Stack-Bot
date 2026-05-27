import random
import string

def generate_user():
    """Generates random user data for Chaturbate registration."""
    adjectives = ["cool", "hot", "fast", "sweet", "wild", "smart", "funky"]
    nouns = ["user", "star", "pro", "bot", "vip", "fan", "guest"]
    
    username = random.choice(adjectives) + random.choice(nouns) + str(random.randint(100, 9999))
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    email = f"{username}@tempmail.com" # Placeholder, will be replaced by actual TempMail logic
    
    return {
        "username": username,
        "password": password,
        "email": email,
        "month": str(random.randint(1, 12)).zfill(2),
        "day": str(random.randint(1, 28)).zfill(2),
        "year": str(random.randint(1990, 2004)),
        "gender": random.choice(["m", "f", "c", "t"])
    }
