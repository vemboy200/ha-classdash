"""Constants for the ClassDash integration."""

from datetime import timedelta

DOMAIN = "classdash"

DEFAULT_PORT = 8734
UPDATE_INTERVAL = timedelta(minutes=5)

CONF_CERT_PEM = "cert_pem"
CONF_FINGERPRINT = "fingerprint"

# Attribute lists are capped so a busy class list doesn't blow past the
# recorder's per-attribute size warning threshold.
MAX_LIST_ATTRIBUTES = 10
