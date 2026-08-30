"""Constants for the ClassDash integration."""

DOMAIN = "classdash"

DEFAULT_PORT = 8734

CONF_CERT_PEM = "cert_pem"
CONF_FINGERPRINT = "fingerprint"

# Attribute lists are capped so a busy class list doesn't blow past the
# recorder's per-attribute size warning threshold.
MAX_LIST_ATTRIBUTES = 10

# /api/stream reconnect backoff — starts quick (a restart of ClassDash's
# own process is often over in seconds), caps well short of "forgotten".
STREAM_RECONNECT_MIN_SECONDS = 5
STREAM_RECONNECT_MAX_SECONDS = 300
# Only flip entities to unavailable once backoff has grown past this —
# a single missed reconnect shouldn't flash the whole device unavailable.
STREAM_UNAVAILABLE_THRESHOLD_SECONDS = 60
# How long the first connection gets before first refresh gives up and
# lets Home Assistant retry setup later.
STREAM_FIRST_CONNECT_TIMEOUT = 15
