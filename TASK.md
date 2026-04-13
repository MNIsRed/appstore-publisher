# App Store Publisher - Cross-Platform Auto-Publish Tool

## Goal
Build a CLI tool in Python that automatically publishes APK files to Chinese Android app stores.

## Core Feature
- User provides APK files with channel suffixes in filename, e.g.:
  - `release-vivo.apk` → vivo store
  - `release-oppo.apk` → OPPO store
  - `release-huawei.apk` → Huawei AppGallery
  - `release-xiaomi.apk` → Xiaomi store
  - `release-honor.apk` → Honor store
  - `release-yingyongbao.apk` → Tencent Yingyongbao
- Tool auto-detects channel from filename, then uploads to corresponding store

## Architecture

### Language & Dependencies
- Python 3.10+
- Use `requests` for HTTP
- Use `click` for CLI
- Use `rich` for nice terminal output
- Use `toml` for config (or `tomllib` from stdlib in 3.11+)

### Project Structure
```
appstore-publisher/
├── pyproject.toml
├── config.example.toml        # Example config with all stores
├── src/
│   └── appstore_publisher/
│       ├── __init__.py
│       ├── cli.py              # Click CLI entry point
│       ├── config.py           # Config loading/validation
│       ├── models.py           # Data models
│       ├── publisher.py        # Main orchestrator
│       ├── channel_detector.py # Detect channel from filename
│       ├── stores/
│       │   ├── __init__.py
│       │   ├── base.py         # Abstract base class for stores
│       │   ├── yingyongbao.py  # Tencent (应用宝)
│       │   ├── huawei.py       # Huawei AppGallery
│       │   ├── honor.py        # Honor
│       │   ├── vivo.py         # vivo
│       │   ├── oppo.py         # OPPO
│       │   └── xiaomi.py       # Xiaomi
│       └── utils.py            # Signing, hashing helpers
└── tests/
    └── ...
```

## Store Implementation Details

### Common Flow
1. Authenticate (get token/sign credentials)
2. Upload APK file
3. Submit update info (version, changelog, etc.)
4. Return result (success/fail + message)

### Channel Detection
- Filename pattern: `*-{channel}.apk` or `*-{channel}-signed.apk`
- Channel name mapping:
  - `vivo` → vivo store
  - `oppo` → OPPO store  
  - `huawei` → Huawei AppGallery
  - `honor` → Honor store
  - `xiaomi` → Xiaomi store
  - `yingyongbao` / `tencent` / `qq` → Tencent Yingyongbao
  - `default` → skip (no channel = no upload)

### Store-Specific Implementation

#### 1. Tencent Yingyongbao (应用宝)
- Auth: UserID + RSA signature (sign all params with MD5+RSA)
- Endpoints:
  - POST `/get_file_upload_info` → get COS pre-signed URL
  - Upload to COS URL
  - POST `/update_app` → submit update
- Special: No review needed, instant publish
- Rate limit: 100 file uploads/day, 50 updates/day

#### 2. Huawei AppGallery
- Auth: OAuth2 (client_id + client_secret → access_token, 48h TTL)
- Endpoints:
  - POST `/api/oauth2/v1/token` → get token
  - GET `/api/publish/v2/upload-url?appId={id}&suffix=apk` → upload URL
  - POST upload URL (multipart) → upload APK
  - PUT `/api/publish/v2/app-file-info?appId={id}` → update file info
- Needs: appId (per-app, from config)
- Review: Manual, 1-3 days

#### 3. Honor (荣耀)
- Same API structure as Huawei
- Different domain: `connect-api.cloud.honor.com`
- Same auth flow (OAuth2)

#### 4. vivo
- Auth: access_key + access_secret (HMAC-SHA256 signature)
- Single endpoint: `https://developer-api.vivo.com.cn/router/rest`
- Methods:
  - `app.upload.file` → upload APK
  - `app.update.app` → submit update
- Params: method, access_key, timestamp, sign_method, sign + business params

#### 5. OPPO
- Auth: OAuth2 (client_id + client_secret → access_token)
- Similar to Huawei flow
- Upload APK → get file ID → submit update

#### 6. Xiaomi (小米)
- Auth: username + access_password + RSA signature
- Endpoints:
  - POST `/dev/query` → query app info
  - POST `/dev/push` → push update (synchroType=1 for update)
- multipart/form-data with appInfo JSON + files
- Supports: APK, icon, screenshots

## Config File (TOML)

```toml
[app]
# Common app info
package_name = "com.example.app"
app_name = "My App"

[changelog]
default = "Bug fixes and improvements"

# Per-store credentials and settings

[stores.yingyongbao]
enabled = true
user_id = "123456"
private_key_path = "./keys/yingyongbao_private.pem"

[stores.huawei]
enabled = true
client_id = "your_client_id"
client_secret = "your_client_secret"
app_id = "your_app_id"

[stores.honor]
enabled = true
client_id = "your_client_id"
client_secret = "your_client_secret"
app_id = "your_app_id"

[stores.vivo]
enabled = true
access_key = "your_access_key"
access_secret = "your_access_secret"

[stores.oppo]
enabled = true
client_id = "your_client_id"
client_secret = "your_client_secret"

[stores.xiaomi]
enabled = true
username = "dev@example.com"
access_password = "your_access_password"
```

## CLI Interface

```bash
# Publish all channel APKs in current directory
appstore-publisher publish ./release-*.apk

# Publish specific files
appstore-publisher publish release-vivo.apk release-oppo.apk

# With custom config
appstore-publisher --config myconfig.toml publish ./apks/

# Dry run (detect channels, show plan, don't upload)
appstore-publisher publish --dry-run ./release-*.apk

# Verbose mode
appstore-publisher -v publish ./release-*.apk
```

## Output Format
Use rich tables to show:
- Detected APK files and their channels
- Upload progress per store
- Final results (success/fail per store)

## Error Handling
- Graceful failure per store (one fails, others continue)
- Detailed error messages
- Retry logic with exponential backoff for network errors
- Validate APK exists and is readable before starting

## Important Notes
- All API calls must use HTTPS
- Implement proper signing for each store
- Handle token refresh (especially Huawei 48h token)
- Log all API calls for debugging
- Support both file paths and glob patterns
