"""Constants for the iLetComfort integration."""

DOMAIN = "iletcomfort"
PLATFORMS = ["climate", "sensor", "switch", "select", "binary_sensor", "number"]

CONF_APPLIANCE_CODE = "appliance_code"
CONF_REGION = "region"

REGION_US = "us"
REGION_EU = "eu"
REGION_URLS = {
    REGION_US: "https://us.dollin.net",
    REGION_EU: "https://eu.dollin.net",
}
DEFAULT_REGION = REGION_US

DEFAULT_SCAN_INTERVAL = 60

# --- Operation modes / MQTT real-time push (issue #55) ---------------------
# Legacy v0.9-v0.11 option. True migrates at runtime to phone-app mode and is
# removed the next time the options form is saved.
CONF_ENABLE_MQTT_PUSH = "enable_mqtt_push"
DEFAULT_ENABLE_MQTT_PUSH = False

# HA-primary is the default and preserves the original polling + write behavior.
# Phone-app mode uses session-independent certificate push only: no periodic
# account polling and no writes that could evict the official app's session.
CONF_OPERATION_MODE = "operation_mode"
OPERATION_MODE_HA_PRIMARY = "ha_primary"
OPERATION_MODE_PHONE_APP = "phone_app"
DEFAULT_OPERATION_MODE = OPERATION_MODE_HA_PRIMARY

# --- Connection mode: cloud vs local LAN ------------------------------------
# Local mode sends the same C3 frames straight to the heat pump's Midea Wi-Fi
# module over the V3 LAN protocol (TCP 6444, token/key authenticated) instead
# of through the Dollin cloud. The cloud account is then only used, best-effort,
# to discover the model code (sn8); polling and control never touch it.
CONF_CONNECTION_MODE = "connection_mode"
CONNECTION_MODE_CLOUD = "cloud"
CONNECTION_MODE_LOCAL = "local"
DEFAULT_CONNECTION_MODE = CONNECTION_MODE_CLOUD
CONF_LOCAL_HOST = "local_host"
CONF_LOCAL_TOKEN = "local_token"
CONF_LOCAL_KEY = "local_key"
LOCAL_PORT = 6444

# sn8 model code persisted in entry data once discovered from the cloud, so
# local mode keeps the right decode profile when the cloud is unreachable.
CONF_SN8 = "sn8"

# AWS IoT MQTT broker (TLS, mutual-cert auth). Endpoint/port come from the cert
# response; this port is the documented default and a fallback.
MQTT_DEFAULT_PORT = 8883
MQTT_KEEPALIVE = 60

# C3 devices publish full status snapshots while idle, commonly around every
# 15 minutes. Captures also show occasional missed cycles, so require three
# missed cycles before treating an otherwise-connected push channel as stale.
MQTT_STATUS_STALE_AFTER = 45 * 60
