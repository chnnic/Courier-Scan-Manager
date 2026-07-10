import importlib.util
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("courier_app", APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load application module from {APP_PATH}")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class TemporaryApplicationDataTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.temp_dir.name)
        self.original_paths = {
            name: getattr(app, name)
            for name in ("APP_DIR", "CONFIG_DB_PATH", "BACKUP_DIR", "ARCHIVE_DIR", "EXPORT_DIR", "UPDATE_DIR")
        }
        app.APP_DIR = self.app_dir
        app.CONFIG_DB_PATH = self.app_dir / app.CONFIG_DB_NAME
        app.BACKUP_DIR = self.app_dir / "backups"
        app.ARCHIVE_DIR = self.app_dir / "archives"
        app.EXPORT_DIR = self.app_dir / "exports"
        app.UPDATE_DIR = self.app_dir / "updates"
        self.manager = app.MonthlyDatabaseManager(
            app.CONFIG_DB_PATH,
            lambda key: {"unrecognized": "Unrecognized"}.get(key, key),
        )

    def tearDown(self) -> None:
        try:
            self.manager.close()
        except (OSError, sqlite3.Error):
            pass
        for name, value in self.original_paths.items():
            setattr(app, name, value)
        self.temp_dir.cleanup()

    def insert_unrecognized(self, month_key: str, tracking_number: str, day: str, company_name: str | None = None) -> None:
        month_db = self.manager._ensure_month_db(month_key)
        cursor = month_db.conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipments
                (tracking_number, company_id, company_name, operator_name, shipped_at, shipping_day)
            VALUES (?, NULL, ?, 'operator', ?, ?)
            """,
            (
                tracking_number,
                company_name or app.UNRECOGNIZED_COMPANY_VALUE,
                f"{day} 10:00:00",
                day,
            ),
        )
        cursor.execute(
            """
            INSERT INTO unrecognized_shipments
                (shipment_id, tracking_number, operator_name, scanned_at)
            VALUES (?, ?, 'operator', ?)
            """,
            (cursor.lastrowid, tracking_number, f"{day} 10:00:00"),
        )
        month_db.conn.commit()


class ReportAndMigrationTests(TemporaryApplicationDataTestCase):
    def test_daily_report_filters_unrecognized_rows_by_day(self) -> None:
        self.insert_unrecognized("2026-07", "OLD-JULY", "2026-07-01")
        self.insert_unrecognized("2026-07", "REPORT-DAY", "2026-07-09")

        _summary_path, detail_path = app.ReportExporter(self.manager).export_report(
            app.EXPORT_DIR,
            "2026-07-09",
            "2026-07-09",
            selected_month="2026-07",
            report_tag="daily",
        )

        detail_text = detail_path.read_text(encoding="utf-8-sig")
        self.assertIn("REPORT-DAY", detail_text)
        self.assertNotIn("OLD-JULY", detail_text)

    def test_legacy_localized_unrecognized_values_are_normalized(self) -> None:
        self.insert_unrecognized("2026-07", "ZH-UNKNOWN", "2026-07-01", "未识别")
        self.insert_unrecognized("2026-07", "EN-UNKNOWN", "2026-07-02", "Unrecognized")
        self.manager.close()

        self.manager = app.MonthlyDatabaseManager(app.CONFIG_DB_PATH, lambda key: "Unrecognized")
        rows = self.manager.get_company_stats(selected_month="2026-07")

        self.assertEqual(rows, [{"company_name": "Unrecognized", "total": 2, "color": app.UNRECOGNIZED_COLOR}])

    def test_existing_1_2_17_month_database_upgrades_in_place(self) -> None:
        legacy_path = self.app_dir / "courier_2025_12.db"
        legacy_db = app.Database(legacy_path, mode="full")
        legacy_db.set_setting("telegram_targets", "demo|LEGACY_SECRET|123")
        cursor = legacy_db.conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipments
                (tracking_number, company_id, company_name, operator_name, shipped_at, shipping_day)
            VALUES ('LEGACY-ROW', NULL, 'Tidak Dikenali', 'operator', '2025-12-01 10:00:00', '2025-12-01')
            """
        )
        cursor.execute(
            """
            INSERT INTO unrecognized_shipments
                (shipment_id, tracking_number, operator_name, scanned_at)
            VALUES (?, 'LEGACY-ROW', 'operator', '2025-12-01 10:00:00')
            """,
            (cursor.lastrowid,),
        )
        legacy_db.conn.commit()
        legacy_db.close()

        upgraded_db = self.manager._ensure_month_db("2025-12")
        upgraded_row = upgraded_db.conn.execute(
            "SELECT tracking_number, company_name FROM shipments WHERE tracking_number = 'LEGACY-ROW'"
        ).fetchone()
        upgraded_tables = {
            row[0]
            for row in upgraded_db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        self.assertEqual(tuple(upgraded_row), ("LEGACY-ROW", app.UNRECOGNIZED_COMPANY_VALUE))
        self.assertNotIn("settings", upgraded_tables)
        self.assertNotIn("blocked_tracking_numbers", upgraded_tables)


class ArchiveTests(TemporaryApplicationDataTestCase):
    def test_partial_month_archive_moves_only_eligible_rows_and_excludes_secrets(self) -> None:
        self.manager.set_setting("telegram_targets", "demo|SECRET_BOT_TOKEN|123")
        self.insert_unrecognized("2026-07", "OLD-JULY", "2026-07-01")
        self.insert_unrecognized("2026-07", "KEEP-JULY", "2026-07-09")

        archive_path = self.manager.archive_old_data("2026-07-05", app.ARCHIVE_DIR)

        self.assertIsNotNone(archive_path)
        month_db = self.manager._ensure_month_db("2026-07")
        remaining = month_db.conn.execute("SELECT tracking_number FROM shipments").fetchall()
        self.assertEqual([row[0] for row in remaining], ["KEEP-JULY"])
        with zipfile.ZipFile(archive_path) as archive_file, tempfile.TemporaryDirectory() as extract_dir:
            archive_file.extract("courier_2026_07.db", extract_dir)
            archive_conn = sqlite3.connect(Path(extract_dir) / "courier_2026_07.db")
            try:
                tables = {
                    row[0]
                    for row in archive_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                archived = archive_conn.execute("SELECT tracking_number FROM shipments").fetchall()
            finally:
                archive_conn.close()
        self.assertEqual(archived, [("OLD-JULY",)])
        self.assertNotIn("settings", tables)
        self.assertNotIn("blocked_tracking_numbers", tables)

    def test_fully_archived_old_month_file_is_removed(self) -> None:
        self.insert_unrecognized("2026-06", "OLD-JUNE", "2026-06-01")

        archive_path = self.manager.archive_old_data("2026-07-01", app.ARCHIVE_DIR)

        self.assertIsNotNone(archive_path)
        self.assertFalse((self.app_dir / "courier_2026_06.db").exists())


class BackupAndRestoreTests(TemporaryApplicationDataTestCase):
    def test_backup_uses_valid_snapshots_without_monthly_settings(self) -> None:
        self.manager.set_setting("telegram_targets", "demo|SECRET_BOT_TOKEN|123")
        self.insert_unrecognized("2026-07", "BACKUP-ROW", "2026-07-01")

        backup_path = app.BackupManager(self.manager, app.BACKUP_DIR).create_backup()

        with zipfile.ZipFile(backup_path) as archive_file, tempfile.TemporaryDirectory() as extract_dir:
            self.assertIsNone(archive_file.testzip())
            archive_file.extractall(extract_dir)
            config_conn = sqlite3.connect(Path(extract_dir) / app.CONFIG_DB_NAME)
            month_conn = sqlite3.connect(Path(extract_dir) / "courier_2026_07.db")
            try:
                stored_targets = config_conn.execute(
                    "SELECT value FROM settings WHERE key = 'telegram_targets'"
                ).fetchone()[0]
                month_tables = {
                    row[0]
                    for row in month_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                config_conn.close()
                month_conn.close()
        self.assertIn("SECRET_BOT_TOKEN", stored_targets)
        self.assertNotIn("settings", month_tables)

    def test_restore_rolls_back_after_replacement_has_started(self) -> None:
        self.insert_unrecognized("2026-06", "ORIGINAL", "2026-06-01")
        replacement_dir = self.app_dir / "replacement"
        snapshots = self.manager.backup_databases_to(replacement_dir)
        replacements = {path.name: path for path in snapshots if path.name in {app.CONFIG_DB_NAME, "courier_2026_06.db"}}
        self.insert_unrecognized("2026-06", "PRESERVE-AFTER-SNAPSHOT", "2026-06-02")
        original_replace = os.replace

        def fail_second_install(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            source_path = Path(source)
            target_path = Path(target)
            if source_path.parent.name == "staged" and target_path.name == "courier_2026_06.db":
                raise OSError("simulated install failure")
            original_replace(source, target)

        with mock.patch.object(app.os, "replace", side_effect=fail_second_install):
            with self.assertRaises(OSError):
                self.manager.replace_databases(replacements)

        month_conn = sqlite3.connect(self.app_dir / "courier_2026_06.db")
        try:
            preserved = month_conn.execute(
                "SELECT COUNT(*) FROM shipments WHERE tracking_number = 'PRESERVE-AFTER-SNAPSHOT'"
            ).fetchone()[0]
        finally:
            month_conn.close()
        self.assertEqual(preserved, 1)

    def test_restore_replaces_complete_database_set(self) -> None:
        self.insert_unrecognized("2026-06", "IN-BACKUP", "2026-06-01")
        backup_path = app.BackupManager(self.manager, app.BACKUP_DIR).create_backup()
        self.insert_unrecognized("2026-06", "AFTER-BACKUP", "2026-06-02")

        with tempfile.TemporaryDirectory() as extract_dir:
            with zipfile.ZipFile(backup_path) as archive_file:
                archive_file.extractall(extract_dir)
            replacements = {
                path.name: path
                for path in Path(extract_dir).glob("*.db")
                if path.name == app.CONFIG_DB_NAME or app.month_key_from_db_path(path)
            }
            self.manager.replace_databases(replacements)

        self.manager = app.MonthlyDatabaseManager(app.CONFIG_DB_PATH)
        restored_db = self.manager._ensure_month_db("2026-06")
        tracking_numbers = {
            row[0] for row in restored_db.conn.execute("SELECT tracking_number FROM shipments").fetchall()
        }
        self.assertIn("IN-BACKUP", tracking_numbers)
        self.assertNotIn("AFTER-BACKUP", tracking_numbers)

    def test_restore_rejects_empty_replacement_set(self) -> None:
        with self.assertRaisesRegex(OSError, "does not contain"):
            self.manager.replace_databases({})


class UpdateManagerTests(TemporaryApplicationDataTestCase):
    def test_update_rejects_non_https_and_missing_hash(self) -> None:
        update_manager = app.UpdateManager(app.APP_DIR, app.UPDATE_DIR)

        with self.assertRaisesRegex(ValueError, "manifest_url_must_use_https"):
            update_manager.fetch_manifest("http://example.com/manifest.json")
        with self.assertRaisesRegex(ValueError, "download_url_must_use_https"):
            update_manager.download_update_package("http://example.com/update.exe", "0" * 64)
        with self.assertRaisesRegex(ValueError, "valid_sha256_required"):
            update_manager.download_update_package("https://example.com/update.exe", None)


if __name__ == "__main__":
    unittest.main()
