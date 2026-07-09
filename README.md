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
- In-app upgrades require HTTPS URLs and a valid SHA-256 value in `manifest.json`. The app creates and verifies a pre-update backup before replacing the exe.
- Full backups include `courier_config.db`, which can contain Telegram credentials. Treat backup ZIP files as sensitive.
- Data archives contain shipment databases only. Operational settings, Telegram credentials, and blocked-number lists stay in `courier_config.db`.
- Restoring a backup stages and validates every database before replacement. If replacement fails, the original database files are restored automatically.

## Verification

Run the core database, archive, report, restore, and update-policy tests with:

```bash
python -m unittest discover -s app/tests -p "test_*.py" -v
```
