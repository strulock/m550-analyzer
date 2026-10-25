// Copy to wifi_secrets.h (gitignored) for the Wi-Fi terminal on TCP port 23.
//
// WIFI_SSID: a network to join, e.g. home Wi-Fi reaching the garage. Then
//   pio device monitor --port socket://m550-spike.local:23
// AP_PASS: if WIFI_SSID isn't in range at boot (or isn't set), the board
// starts its own access point "m550-spike" with this password (8+ chars).
// Join it from the laptop, then
//   pio device monitor --port socket://192.168.4.1:23
//
// Leave both out for USB only.
#define WIFI_SSID "your-network"
#define WIFI_PASS "your-password"
#define AP_PASS "change-me-8-plus"
