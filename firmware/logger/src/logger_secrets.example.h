// Copy to logger_secrets.h (gitignored) and fill in.
//
// Wi-Fi: a network that reaches the car where it's parked. The board logs
// without it and uploads the next time it's in range.
#define WIFI_SSID "your-network"
#define WIFI_PASS "your-password"
// Optional: a backup network (a phone hotspot, say) for when the first isn't
// working. The board joins WIFI_SSID when it can, the backup otherwise (and
// moves back when the first is in range again), and tries again every 30 s
// while it has neither.
// #define WIFI_SSID2 "backup-network"
// #define WIFI_PASS2 "backup-password"
// Optional: an access point "m550-logger" when WIFI_SSID isn't in range at
// boot, for the terminal (TCP 23 at 192.168.4.1). Uploads need WIFI_SSID.
#define AP_PASS "change-me-8-plus"

// The cloud Worker (cloud/README.md) and an Access service token made for
// the board (Zero Trust > Access > Service credentials), added to the Access
// application's Service Auth policy. Leave WORKER_HOST empty to only log.
#define WORKER_HOST "n63read-cloud.your-subdomain.workers.dev"
#define ACCESS_CLIENT_ID "xxxxxxxx.access"
#define ACCESS_CLIENT_SECRET "xxxxxxxx"

// Local time for the logs' Clock column and file names (a POSIX TZ string).
// US Central: "CST6CDT,M3.2.0,M11.1.0"; Eastern: "EST5EDT,M3.2.0,M11.1.0".
#define TZ_INFO "CST6CDT,M3.2.0,M11.1.0"
