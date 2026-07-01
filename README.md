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
  "version": "1.1.0",
  "download_url": "https://github.com/chnnic/Courier-Scan-Manager/releases/download/v1.1.0/CourierScanManager.exe",
  "sha256": ""
}
```

## Notes

- The application stores user data outside the exe folder, so updating the exe does not remove shipment records.
- If you want stronger download verification, fill in the `sha256` field with the exe checksum for each release.
