"""Constants for the ClassDash integration."""

DOMAIN = "classdash"

DEFAULT_PORT = 8734

CONF_CERT_PEM = "cert_pem"
CONF_FINGERPRINT = "fingerprint"

# Attribute lists are capped so a busy class list doesn't blow past the
# recorder's per-attribute size warning threshold.
MAX_LIST_ATTRIBUTES = 10

# /api/stream reconnect backoff — starts quick (a restart of ClassDash's
# own process is often over in seconds), caps at a minute: an attempt is
# one cheap request on the LAN, and a longer cap meant ClassDash could be
# back for minutes before Home Assistant noticed.
STREAM_RECONNECT_MIN_SECONDS = 5
STREAM_RECONNECT_MAX_SECONDS = 60
# Only flip entities to unavailable once backoff has grown to this (must
# stay <= STREAM_RECONNECT_MAX_SECONDS, or it's never reached) —
# a single missed reconnect shouldn't flash the whole device unavailable.
STREAM_UNAVAILABLE_THRESHOLD_SECONDS = 60
