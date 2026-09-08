"""RSA keypair for signing/verifying license tokens (RS256).

Private key stays server-side only (this directory, gitignored) and signs
licenses. Public key is safe to ship inside every client's desktop app —
it can verify a license's signature and expiry completely offline, and only
needs to phone home to check revocation.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS_DIR = Path(__file__).resolve().parent / "keys"
PRIVATE_KEY_FILE = KEYS_DIR / "license_private_key.pem"
PUBLIC_KEY_FILE = KEYS_DIR / "license_public_key.pem"


def _generate():
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    PRIVATE_KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    PUBLIC_KEY_FILE.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))


def load_private_key():
    if not PRIVATE_KEY_FILE.exists():
        _generate()
    return PRIVATE_KEY_FILE.read_text()


def load_public_key():
    if not PUBLIC_KEY_FILE.exists():
        _generate()
    return PUBLIC_KEY_FILE.read_text()
