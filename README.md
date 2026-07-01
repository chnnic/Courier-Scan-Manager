# Courier Scan Manager Releases

This repository is used for Windows release delivery and online update metadata.

## Files

- `manifest.json`: online update manifest read by the desktop application
- GitHub Releases assets:
  - `CourierScanManager.exe`

## Update Flow

1. Build the Windows exe with PyInstaller
2. Upload `CourierScanManager.exe` to a GitHub Release
3. Update `manifest.json` with the new version and release asset URL
4. Commit the updated `manifest.json` to `main`

## Manifest Example

```json
{
  "version": "1.2.0",
  "download_url": "https://github.com/chnnic/Courier-Scan-Manager/releases/download/v1.2.0/CourierScanManager.exe",
  "sha256": ""
}
```

## Notes

- The application now stores `courier_config.db` and monthly databases like `courier_2026_07.db` beside the exe.
- Replacing only `CourierScanManager.exe` does not remove shipment records.
- Deleting the whole app folder will also delete the local databases, so releases should favor in-app upgrade or exe-only replacement.
- If you want stronger download verification, fill in the `sha256` field with the exe checksum for each release.
